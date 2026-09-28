#!/usr/bin/env python3
"""Preview and execute audited ClickUp task changes for Captain.

The command supports task creation, task updates, comments, and JSON batches.
Without ``--execute`` it prints the request it would make. Executed changes
respect the DailyLoop shadow-mode safety brake, use the shared ClickUp
credential loader, and leave audit records for every supported mutation.

Workspace rules (the private config at ``CAPTAIN_CLICKUP_WORKSPACE_CONFIG``, read by
``clickup_workspace``; the writer refuses to run without it):

- Tasks are created only in the configured Inbox list. Archived lists are
  never written, not even by an update or comment.
- A created task's description starts with ``Done when:`` and names a
  ``Proposed folder:``.
- Ownership is exactly one native ClickUp assignee. An update replaces the
  current assignee instead of adding another. Custom fields are never
  created or written.
- Only the ``safety`` and ``customer-visible`` tags are written; other
  requested tags are dropped and reported.

Examples:
    python3 scripts/clickup_write.py create-task --name "Inspect rover" \
        --description $'Done when: rover inspected\nProposed folder: GL-1/Chassis'
    python3 scripts/clickup_write.py --execute comment-task --task-id abc --text "Bench test passed"

This module also exposes the validation and execution helpers used by tests
and other Captain scripts. Network access is injected through ``request_fn``
where practical so those callers can exercise the workflow without contacting
ClickUp.
"""


# Requirements

import argparse
import contextlib
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import captain_telemetry


# Root path and shared modules
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Shared database and clickup helpers
from captain_db import audit, init_db
from clickup_credentials import MissingClickUpCredentials, load_clickup_credentials
import captain_modes
import clickup_workspace

# API endpoint for ClickUp tasks
API = "https://api.clickup.com/api/v2"

# Operation keys that would write custom fields, Owners labels, or task types.
# Captain never writes any of them; an operation carrying one is refused.
BANNED_OPERATION_KEYS = ("owner", "owners", "custom_fields", "custom_field", "custom_item_id")

# A deliberate manual write can bypass shadow mode through this environment
# variable or the matching ``--force-live-write`` command-line option.
SHADOW_ESCAPE_ENV = "CAPTAIN_CLICKUP_FORCE_LIVE_WRITE"


# Shadow-mode safety


def dailyloop_audience():
    """Return the configured DailyLoop audience from the mode file.

    A missing file or ``DailyLoop`` key means ``off``. That means the automated
    loop is inert, not that manual ClickUp tooling is banned.

    An existing file that cannot be read or parsed fails closed to ``shadow``
    and emits telemetry. Successfully parsed data is expected to be the mapping
    written by ``captain_modes``; an incompatible shape surfaces an error.
    """
    # A genuinely absent configuration does not restrict manual writes.
    if not captain_modes.MODE_PATH.exists():
        return "off"

    # An unreadable existing configuration fails closed instead of guessing.
    try:
        modes = captain_modes.load_modes()
    except (OSError, ValueError):
        captain_telemetry.capture_message(
            "data/captain-modes.json exists but could not be parsed; "
            "failing DailyLoop shadow brake closed (treating as 'shadow')",
            level="error",
        )
        return "shadow"

    return (modes.get("DailyLoop") or {}).get("audience") or "off"


def shadow_write_block_message(force=False):
    """Return a refusal message when DailyLoop shadow mode blocks a write.

    ``force`` and ``CAPTAIN_CLICKUP_FORCE_LIVE_WRITE`` are explicit operator
    escape hatches. The function returns ``None`` when the write may proceed.

    Example input: force=True
    Example output: None
    """
    # An explicit operator override always permits the requested write.
    if force or os.environ.get(SHADOW_ESCAPE_ENV):
        return None

    # Only shadow mode suppresses writes; off and live continue normally.
    if dailyloop_audience() != "shadow":
        return None

    return (
        "Refused: DailyLoop is in shadow mode (data/captain-modes.json -> "
        "DailyLoop.audience). No ClickUp mutation was made and no clickup_* audit row "
        "was written. To force a deliberate manual write while in shadow, pass "
        "--force-live-write or set {}=1.".format(SHADOW_ESCAPE_ENV)
    )


def now():
    """Return the current UTC time as a timezone-aware ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ClickUp errors and HTTP requests


class ClickUpRequestError(Exception):
    """Represent a request ClickUp received but rejected."""

    def __init__(self, http_status, clickup_message, path):
        """Remember the details of a request that ClickUp rejected."""
        super().__init__(clickup_message)
        self.http_status = http_status
        self.clickup_message = clickup_message
        self.path = path

    def as_error(self, task_name):
        """Return the rejection details in a form suitable for a report."""
        return {
            "http_status": self.http_status,
            "clickup_message": self.clickup_message,
            "path": self.path,
            "task_name": task_name,
        }


class ClickUpUnavailableError(Exception):
    """Represent an outage, timeout, or throttled ClickUp request."""

    def __init__(self, message, path):
        """Remember why ClickUp could not be reached for this request."""
        super().__init__(message)
        self.message = message
        self.path = path

    def as_error(self):
        """Return the connection failure in a form suitable for a report."""
        return {"message": self.message, "path": self.path}


def clickup_error_message(error):
    """Extract ClickUp's most useful explanation from an HTTP error response.

    ClickUp has used several keys for API error text, so this helper checks
    ``err``, ``message``, and ``error`` in that order.
    """
    # A malformed or unreadable response still receives a stable fallback.
    try:
        raw = error.read().decode("utf-8", errors="replace")
        body = json.loads(raw) if raw else {}
    except (OSError, ValueError, UnicodeError):
        body = {}

    # Return the first non-empty message in ClickUp's known response shapes.
    if isinstance(body, dict):
        for key in ("err", "message", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "ClickUp rejected the request"


def request(method, path, token, payload=None):
    """Send one authenticated request and translate ClickUp failures.

    Successful JSON responses become dictionaries. HTTP 429 and 5xx responses
    are classified as temporary unavailability; other HTTP errors retain
    ClickUp's rejection details.

    Example input: method="GET", path="/task/abc", payload=None
    """
    # Encode a body only for operations that supplied a payload.
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=body,
        method=method,
        headers={"Authorization": token, "Content-Type": "application/json"},
    )

    # Keep transport details and error classification in one shared boundary.
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        message = clickup_error_message(error)

        # Throttling and server errors are retry/outage conditions, not bad input.
        if error.code >= 500 or error.code == 429:
            raise ClickUpUnavailableError(message, path)

        raise ClickUpRequestError(error.code, message, path)
    except (urllib.error.URLError, TimeoutError) as error:
        raise ClickUpUnavailableError("ClickUp API is unavailable", path) from error


def verify_due_date(token, task_id, expected_due_date_ms, request_fn=request):
    """Read a task back and report whether ClickUp saved its due date.

    A missing expected due date requires no verification and returns
    ``{"checked": False}`` without making a request.
    """
    if expected_due_date_ms is None:
        return {"checked": False}

    # ClickUp returns due dates as strings, so normalize before comparing.
    task = request_fn("GET", f"/task/{task_id}", token)
    actual = task.get("due_date")
    actual_int = int(actual) if actual is not None else None
    ok = actual_int == expected_due_date_ms

    return {
        "checked": True,
        "ok": ok,
        "expected_due_date": expected_due_date_ms,
        "actual_due_date": actual_int,
        "actual_due_date_time": task.get("due_date_time"),
    }


def clean_payload(values):
    """Remove absent values so ClickUp receives only intentional changes.

    ``None`` and empty lists are omitted. Other false-like values, including
    ``False``, ``0``, and empty strings, retain their existing meaning.
    """
    return {key: value for key, value in values.items() if value is not None and value != []}



# Workspace rules: ownership, tags, destinations, and descriptions


def assignee_values(raw):
    """Normalize an assignee value (scalar, list, or absent) to a list of strings."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [str(item).strip() for item in items if str(item).strip()]


def resolve_single_assignee(raw, config):
    """Resolve at most one assignee to a numeric ClickUp user ID.

    A name or alias resolves through the identity map. A numeric ID must also
    be in that map. More than one assignee, an unknown or departed person,
    and a never-assignable member all raise ``ValueError``.

    Example input: "person-a"
    Example output: 10000001
    """
    values = assignee_values(raw)
    if not values:
        return None
    if len(values) > 1:
        raise ValueError(
            "a task has exactly one owner: pass one assignee, not {}".format(len(values))
        )
    return clickup_workspace.resolve_assignee_id(values[0], config)


def reject_banned_keys(operation):
    """Refuse operations that would write custom fields or Owners labels."""
    present = [key for key in BANNED_OPERATION_KEYS if operation.get(key) not in (None, [], {}, "")]
    if present:
        raise ValueError(
            "refused: {} would write a ClickUp custom field or task type. Captain never "
            "creates or writes custom fields; use one assignee, or name the proposed owner "
            "in the Inbox task description".format(", ".join(present))
        )


def refuse_archived(list_id, config, what):
    """Raise when ``list_id`` is on the archived-list blocklist."""
    if list_id and str(list_id) in clickup_workspace.archived_list_ids(config):
        raise ValueError(
            "refused: list {} is archived and must never be written ({})".format(list_id, what)
        )


# Status and operation preparation


def allowed_statuses(token, list_id, request_fn=request):
    """Return the workflow status names allowed by a ClickUp list."""
    result = request_fn("GET", f"/list/{list_id}", token)
    return [item.get("status") for item in result.get("statuses", []) if item.get("status")]


def resolve_status(requested_status, statuses):
    """Match a requested status to a list status or explain the mismatch.

    Comparisons ignore case and surrounding whitespace. There are no aliases:
    a status that the list does not define is never rewritten to another one.
    A missing Blocked status becomes an actionable marker; any other invalid
    name becomes a validation error.
    """
    # No requested status means the operation should leave status unchanged.
    if requested_status is None:
        return {"applied": None, "requested": None}

    normalized = str(requested_status).strip().casefold()
    by_normalized_name = {str(status).strip().casefold(): status for status in statuses}

    if normalized in by_normalized_name:
        return {"applied": by_normalized_name[normalized], "requested": requested_status}

    # Blocked is special: preserve the request as follow-up work for the list.
    if normalized == "blocked":
        return {"applied": None, "requested": requested_status, "needs_blocked_status": True}

    # Other unknown statuses are ordinary validation failures.
    return {
        "applied": None,
        "requested": requested_status,
        "error": {"invalid_status": requested_status, "allowed_statuses": statuses},
    }


def add_status_note(description, resolution):
    """Append a task note when the requested Blocked status is unavailable.

    Existing notes are not duplicated, and an absent description becomes just
    the note.
    """
    if not resolution.get("needs_blocked_status"):
        return description

    note = ("Requested workflow state: Blocked — this list has no Blocked status yet. "
            "Add it in List/Space settings; status left unchanged.")

    if description and note in description:
        return description

    return "\n\n".join(part for part in (description, note) if part)


def operation_fields(operation, config):
    """Validate an operation and describe its primary ClickUp request.

    Every check here is local, so a dry run applies the same refusals as a
    real write. The returned metadata contains the HTTP method, API path,
    audit event, task identity, resolved assignee, and tag decisions.
    """
    command = operation.get("command")
    reject_banned_keys(operation)

    # New tasks go only to the Inbox and must say when they are done.
    if command == "create-task":
        name = operation.get("name")
        if not name:
            raise ValueError("create-task requires name")

        inbox = clickup_workspace.inbox_list_id(config)
        list_id = str(operation.get("list_id") or inbox)
        refuse_archived(list_id, config, "create-task")
        if list_id != inbox:
            raise ValueError(
                "refused: create-task may only target the Inbox list {} (got {}); "
                "put the destination in a 'Proposed folder:' line instead".format(inbox, list_id)
            )

        problems = clickup_workspace.description_problems(operation.get("description"))
        if problems:
            raise ValueError("refused create-task: " + "; ".join(problems))

        kept_tags, dropped_tags = clickup_workspace.filter_tags(operation.get("tags"), config)
        return {
            "method": "POST",
            "path": "/list/{}/task".format(list_id),
            "list_id": list_id,
            "task_name": name,
            "audit_event": "clickup_task_create",
            "due_date_followup_required": operation.get("due_date_ms") is None,
            "assignee_id": resolve_single_assignee(operation.get("assignee"), config),
            "tags": kept_tags,
            "dropped_tags": dropped_tags,
        }

    # Tags are only accepted where ClickUp takes them inline: on create.
    if operation.get("tags"):
        raise ValueError("{} does not accept tags; tags are set only on create-task".format(command))

    # Updates already know their task ID; the destination list is read later.
    if command == "update-task":
        task_id = operation.get("task_id")

        if not task_id:
            raise ValueError("update-task requires task_id")
        refuse_archived(operation.get("list_id"), config, "update-task")

        return {
            "method": "PUT",
            "path": "/task/{}".format(task_id),
            "list_id": operation.get("list_id"),
            "task_name": operation.get("name") or str(task_id),
            "task_id": str(task_id),
            "audit_event": "clickup_task_update",
            "due_date_followup_required": False,
            "assignee_id": resolve_single_assignee(operation.get("assignee"), config),
            "tags": [],
            "dropped_tags": [],
        }

    # Comments use a separate endpoint and change no task fields.
    if command == "comment-task":
        task_id = operation.get("task_id")

        if not task_id:
            raise ValueError("comment-task requires task_id")
        if not operation.get("comment_text"):
            raise ValueError("comment-task requires comment_text")
        if assignee_values(operation.get("assignee")):
            raise ValueError("comment-task does not change assignees; use update-task")

        return {
            "method": "POST",
            "path": "/task/{}/comment".format(task_id),
            "list_id": None,
            "task_name": str(task_id),
            "task_id": str(task_id),
            "audit_event": "clickup_task_comment",
            "due_date_followup_required": False,
            "assignee_id": None,
            "tags": [],
            "dropped_tags": [],
        }

    raise ValueError("unsupported batch command: {}".format(command))


def assignee_change(new_id, current_ids):
    """Return the ClickUp ``assignees`` update that leaves exactly ``new_id``.

    Every current assignee other than ``new_id`` is removed, so ownership is
    replaced rather than accumulated. ``None`` means nothing needs to change.

    Example input: new_id=2, current_ids=[1]
    Example output: {"add": [2], "rem": [1]}
    """
    if new_id is None:
        return None
    current = [int(item) for item in current_ids]
    add = [] if new_id in current else [new_id]
    rem = [item for item in current if item != new_id]
    if not add and not rem:
        return None
    return {"add": add, "rem": rem}


def operation_payload(operation, fields, resolution, existing_description=None, assignees_update=None):
    """Build the request body for the operation's primary write."""
    command = operation["command"]

    # Comments have a minimal, command-specific request shape.
    if command == "comment-task":
        return {"comment_text": operation.get("comment_text")}

    # A missing Blocked status is recorded on the task instead of being dropped.
    description = operation.get("description")
    if description is None and resolution.get("needs_blocked_status"):
        description = existing_description

    description = add_status_note(description, resolution)

    # Create Task takes one assignee and the allowed tags inline.
    if command == "create-task":
        return clean_payload({
            "name": operation.get("name"),
            "description": description,
            "status": resolution.get("applied"),
            "priority": operation.get("priority"),
            "assignees": [fields["assignee_id"]] if fields.get("assignee_id") is not None else None,
            "tags": fields.get("tags"),
            "due_date": operation.get("due_date_ms"),
            "due_date_time": False if operation.get("due_date_ms") is not None else None,
        })

    # Update Task replaces the assignee with ``add``/``rem``.
    return clean_payload({
        "name": operation.get("name"),
        "description": description,
        "status": resolution.get("applied"),
        "priority": operation.get("priority"),
        "assignees": assignees_update,
        "due_date": operation.get("due_date_ms"),
        "due_date_time": False if operation.get("due_date_ms") is not None else None,
        "parent": operation.get("parent_task_id"),
    })


def prepare_operation(operation, token, request_fn, status_cache, config):
    """Validate and assemble one operation before its primary write.

    The result is ``(fields, payload, resolution, validation_error)``. This
    phase only reads from ClickUp. Updates and comments read their task first
    so a task in an archived list is refused before anything is written.
    """
    fields = operation_fields(operation, config)
    command = operation.get("command")
    requested_status = operation.get("status")

    existing_description = None
    current_assignees = []
    list_id = fields.get("list_id")

    # Existing tasks: confirm the list is writable and learn current state.
    if command in ("update-task", "comment-task"):
        task = request_fn("GET", "/task/{}".format(fields["task_id"]), token)
        list_id = ((task.get("list") or {}).get("id"))

        if not list_id:
            raise ValueError(
                "could not identify destination list for task {}".format(fields["task_id"])
            )
        refuse_archived(list_id, config, "{} {}".format(command, fields["task_id"]))

        if command == "update-task":
            fields["task_name"] = operation.get("name") or task.get("name") or fields["task_id"]
        fields["list_id"] = str(list_id)
        existing_description = task.get("description")
        current_assignees = [
            assignee.get("id") for assignee in task.get("assignees") or []
            if assignee.get("id") is not None
        ]

    # Resolve a requested status against the destination list's real workflow.
    if requested_status is not None:
        if list_id not in status_cache:
            status_cache[list_id] = allowed_statuses(token, list_id, request_fn=request_fn)

        resolution = resolve_status(requested_status, status_cache[list_id])
        if resolution.get("error"):
            return fields, None, None, resolution["error"]
    else:
        resolution = {"applied": None, "requested": None}

    assignees_update = None
    if command == "update-task":
        assignees_update = assignee_change(fields.get("assignee_id"), current_assignees)
    resolution["assignee_change"] = assignees_update

    payload = operation_payload(operation, fields, resolution, existing_description, assignees_update)
    return fields, payload, resolution, None


# Audited execution


def resolve_audited_task_id(fields, result):
    """Return the task ID that should receive the operation's audit record.

    Only ``create-task`` learns its task ID from the response. Other commands
    already know their target. In particular, a comment response contains a
    comment ID, which must never be mistaken for the task ID.
    """
    if fields.get("task_id"):
        return fields["task_id"]

    return result.get("id")


def execute_prepared_operation(operation, fields, payload, resolution, token, request_fn, audit_fn):
    """Execute one prepared operation, audit it, and verify its due date.

    The primary write happens first. Due-date verification is a separate
    read with its own audit record so partial success remains visible.
    """
    # Perform the primary task or comment mutation exactly once.
    result = request_fn(fields["method"], fields["path"], token, payload)
    task_id = resolve_audited_task_id(fields, result)

    # Record the primary mutation before attempting independent follow-ups.
    due_date_verification = {"checked": False}
    audit_fn(
        fields["audit_event"],
        task_id=task_id,
        list_id=fields.get("list_id"),
        path=fields["path"],
        source=operation.get("source", "captain"),
        evidence=operation.get("evidence", []),
        payload=payload,
        result_url=result.get("url"),
        operation_id=operation.get("operation_id"),
        due_date_verification=due_date_verification,
        due_date_followup_required=fields["due_date_followup_required"],
        needs_blocked_status=bool(resolution.get("needs_blocked_status")),
        dropped_tags=fields.get("dropped_tags") or [],
    )

    # Read due dates back because ClickUp may accept but normalize the value.
    if operation.get("due_date_ms") is not None:
        try:
            due_date_verification = verify_due_date(
                token,
                task_id,
                operation["due_date_ms"],
                request_fn=request_fn,
            )
        except ClickUpRequestError as error:
            due_date_verification = {
                "checked": True,
                "ok": False,
                "error": error.as_error(fields["task_name"]),
            }
        except ClickUpUnavailableError as error:
            due_date_verification = {"checked": True, "ok": False, "error": error.as_error()}

        audit_fn(
            "clickup_due_date_verification",
            task_id=task_id,
            path="/task/{}".format(task_id),
            source=operation.get("source", "captain"),
            operation_id=operation.get("operation_id"),
            due_date_verification=due_date_verification,
        )

    # Return one complete operation record for batch aggregation and callers.
    return {
        "operation_id": operation.get("operation_id"),
        "task_id": task_id,
        "task_name": fields["task_name"],
        "list_id": fields.get("list_id"),
        "url": result.get("url"),
        "status_resolution": resolution,
        "due_date_verification": due_date_verification,
        "due_date_followup_required": fields["due_date_followup_required"],
        "needs_blocked_status": bool(resolution.get("needs_blocked_status")),
        "assignee_change": resolution.get("assignee_change"),
        "dropped_tags": fields.get("dropped_tags") or [],
    }


def execute_batch(operations, token, request_fn=request, audit_fn=audit, config=None):
    """Preflight all operations, then execute every valid mutation exactly once.

    Preflight only reads from ClickUp. If ClickUp becomes unavailable during
    either phase, the result identifies the uncertain operation and every
    operation that was not attempted instead of dropping them from the report.
    """
    # The workspace rules are required; a missing config refuses every write.
    config = clickup_workspace.load_workspace() if config is None else config

    # Copy inputs so internal preparation never mutates caller-owned mappings.
    operations = [dict(operation) for operation in operations]
    operation_ids = [operation.get("operation_id") for operation in operations]

    # Stable, unique IDs are required to trace every partial batch outcome.
    if any(not operation_id for operation_id in operation_ids):
        raise ValueError("every batch operation requires a non-empty operation_id")
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("batch operation_id values must be unique")

    succeeded, failed, prepared, status_cache = [], [], [], {}

    # Phase 1: validate and prepare each operation before primary writes begin.
    for index, operation in enumerate(operations):
        try:
            fields, payload, resolution, validation_error = prepare_operation(
                operation,
                token,
                request_fn,
                status_cache,
                config,
            )
        except ClickUpRequestError as error:
            task_name = operation.get("name") or operation.get("task_id")
            error_data = error.as_error(task_name)
            error_data["retryable"] = True
            failed.append({
                "operation_id": operation["operation_id"],
                "task_name": task_name,
                "error": error_data,
            })
            continue
        except ClickUpUnavailableError as error:
            task_name = operation.get("name") or operation.get("task_id")
            error_data = error.as_error()
            error_data.update({"unavailable": True, "unknown": True, "retryable": False})
            failed.append({
                "operation_id": operation["operation_id"],
                "task_name": task_name,
                "error": error_data,
            })

            # Prepared items have not written anything yet. Report them and
            # all later items as not attempted.
            not_attempted = [
                prepared_operation
                for prepared_operation, _, _, _ in prepared
            ] + operations[index + 1:]

            for pending_operation in not_attempted:
                failed.append({
                    "operation_id": pending_operation["operation_id"],
                    "task_name": pending_operation.get("name") or pending_operation.get("task_id"),
                    "error": {
                        "message": "Not attempted because ClickUp API became unavailable",
                        "unavailable": True,
                        "retryable": True,
                    },
                })

            return {
                "ok": False,
                "succeeded": succeeded,
                "failed": failed,
                "unavailable": error.as_error(),
            }
        except ValueError as error:
            failed.append({
                "operation_id": operation["operation_id"],
                "task_name": operation.get("name"),
                "error": {"message": str(error)},
            })
            continue

        if validation_error:
            validation_error["retryable"] = True
            failed.append({
                "operation_id": operation["operation_id"],
                "task_name": fields["task_name"],
                "error": validation_error,
            })
            continue

        prepared.append((operation, fields, payload, resolution))

    # Phase 2: execute every successfully prepared primary write in order.
    for index, (operation, fields, payload, resolution) in enumerate(prepared):
        try:
            op_result = execute_prepared_operation(
                operation,
                fields,
                payload,
                resolution,
                token,
                request_fn,
                audit_fn,
            )
        except ClickUpRequestError as error:
            error_data = error.as_error(fields["task_name"])
            error_data["retryable"] = True
            failed.append({
                "operation_id": operation["operation_id"],
                "task_name": fields["task_name"],
                "error": error_data,
            })
            continue
        except ClickUpUnavailableError as error:
            error_data = error.as_error()
            error_data.update({"unavailable": True, "unknown": True, "retryable": False})
            failed.append({
                "operation_id": operation["operation_id"],
                "task_name": fields["task_name"],
                "error": error_data,
            })

            # Stop primary writes after an outage and retain all pending IDs.
            for pending_operation, pending_fields, _, _ in prepared[index + 1:]:
                failed.append({
                    "operation_id": pending_operation["operation_id"],
                    "task_name": pending_fields["task_name"],
                    "error": {
                        "message": "Not attempted because ClickUp API became unavailable",
                        "unavailable": True,
                        "retryable": True,
                    },
                })

            return {
                "ok": False,
                "succeeded": succeeded,
                "failed": failed,
                "unavailable": error.as_error(),
            }

        succeeded.append(op_result)

    return {"ok": not failed, "succeeded": succeeded, "failed": failed}


# Command-line input and workflow


def parse_operations_file(path):
    """Read a batch operation array from a JSON file or standard input.

    The input may be a bare array or an object with an ``operations`` array.
    Passing ``-`` reads the JSON document from standard input.
    """
    # Load the complete document from the selected input source.
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)

    # Normalize both accepted top-level shapes to a single list.
    operations = data.get("operations") if isinstance(data, dict) else data
    if not isinstance(operations, list):
        raise ValueError("batch input must be a JSON array or an object with an operations array")

    return operations




def operation_from_args(args):
    """Convert parsed command-line arguments to an internal operation record."""
    # Every single-operation command carries the same audit and source metadata.
    common = {
        "operation_id": "single",
        "command": args.command,
        "source": args.source,
        "evidence": args.evidence,
    }

    # Creation needs the complete initial task shape; the list is the Inbox.
    if args.command == "create-task":
        return dict(
            common,
            list_id=args.list_id,
            name=args.name,
            description=args.description,
            status=args.status,
            priority=args.priority,
            assignee=args.assignee,
            tags=args.tag,
            due_date_ms=args.due_date_ms,
        )

    # Comments carry only a target task and the comment body beyond shared data.
    if args.command == "comment-task":
        return dict(common, task_id=args.task_id, comment_text=args.comment_text)

    # The remaining single-operation command is an update with optional changes.
    return dict(
        common,
        task_id=args.task_id,
        name=args.name,
        description=args.description,
        status=args.status,
        priority=args.priority,
        assignee=args.assignee,
        due_date_ms=args.due_date_ms,
        parent_task_id=args.parent_task_id,
    )


def main():
    """Preview or execute audited ClickUp changes from the command line."""


    # ---------------- Parse command-line arguments ----------------

    # Define global safety options before command-specific arguments.
    parser = argparse.ArgumentParser(
        description=(
            "Audited Captain ClickUp writes. Use only for explicit human "
            "requests or approved proposals."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually mutate ClickUp. Without this, prints the planned request only.",
    )
    parser.add_argument(
        "--force-live-write",
        action="store_true",
        help=(
            "Escape hatch: override DailyLoop shadow-mode write suppression "
            "for a deliberate manual write."
        ),
    )

    # Add subparsers
    sub = parser.add_subparsers(dest="command", required=True)

    # Create-Task: Inbox only, with a Done-when line and a proposed folder.
    create = sub.add_parser("create-task")
    create.add_argument(
        "--list-id",
        help=(
            "Must be the configured Inbox list (CAPTAIN_INBOX_LIST_ID or "
            "the private workspace config). Defaults to it; any other list is refused."
        ),
    )
    create.add_argument("--name", required=True)
    create.add_argument(
        "--description",
        help="Required. First line 'Done when: ...'; include a 'Proposed folder: ...' line.",
    )
    create.add_argument("--status")
    create.add_argument(
        "--priority",
        type=int,
        choices=[1, 2, 3, 4],
        help="1 urgent, 2 high, 3 normal, 4 low",
    )
    create.add_argument(
        "--assignee",
        action="append",
        default=[],
        help=(
            "The one owner: a name, alias, or numeric ClickUp ID from the identity map. "
            "Give it at most once; unknown or departed people are refused."
        ),
    )
    create.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Only 'safety' and 'customer-visible' are written; other tags are dropped.",
    )
    create.add_argument("--due-date-ms", type=int, help="ClickUp due date in Unix milliseconds")
    create.add_argument("--source", default="captain")
    create.add_argument("--evidence", action="append", default=[])

    # update-task can also move a task beneath a new parent when explicitly asked.
    update = sub.add_parser("update-task")
    update.add_argument("--task-id", required=True)
    update.add_argument("--name")
    update.add_argument("--description")
    update.add_argument("--status")
    update.add_argument("--priority", type=int, choices=[1, 2, 3, 4])
    update.add_argument(
        "--assignee",
        action="append",
        default=[],
        help=(
            "Replace the task's owner with this one person (name, alias, or numeric ID). "
            "Every other current assignee is removed."
        ),
    )
    update.add_argument("--due-date-ms", type=int)
    update.add_argument(
        "--parent-task-id",
        help=(
            "Set/move task under this ClickUp parent task ID. Use only for "
            "explicit structural-change requests."
        ),
    )
    update.add_argument("--source", default="captain")
    update.add_argument("--evidence", action="append", default=[])

    # comment-task appends plain text without changing task fields.
    comment = sub.add_parser("comment-task")
    comment.add_argument("--task-id", required=True)
    comment.add_argument("--text", dest="comment_text", required=True)
    comment.add_argument("--source", default="captain")
    comment.add_argument("--evidence", action="append", default=[])

    # batch accepts the same operation records used by the internal API.
    batch = sub.add_parser("batch")
    batch.add_argument(
        "--operations-file",
        default="-",
        help="JSON array or {operations:[...]}; use - for stdin",
    )

    # Parse Args
    args = parser.parse_args()


    # ---------------- Execute the requested command ----------------

    # The workspace rules gate every path, including dry runs.
    try:
        config = clickup_workspace.load_workspace()
    except clickup_workspace.WorkspaceConfigError as error:
        print(json.dumps({"ok": False, "error": {"message": str(error)}}, indent=2))
        return 2

    # Parse batch input or normalize one subcommand into a single-item batch.
    if args.command == "batch":
        try:
            operations = parse_operations_file(args.operations_file)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit("Malformed batch input: {}".format(error))
    else:
        operations = [operation_from_args(args)]

    if not args.execute:
        # A dry run applies every local refusal and makes no API calls.
        previews = []
        for operation in operations:
            try:
                fields = operation_fields(operation, config)
                preview_assignees = None
                if operation.get("command") == "update-task" and fields.get("assignee_id") is not None:
                    preview_assignees = {
                        "add": [fields["assignee_id"]],
                        "rem": "every other current assignee (read from the task on --execute)",
                    }
                payload = operation_payload(
                    operation,
                    fields,
                    {"applied": operation.get("status"), "requested": operation.get("status")},
                    assignees_update=preview_assignees,
                )
            except ValueError as error:
                # Treat CLI mistakes as normal validation failures. Letting one
                # reach the telemetry guard would incorrectly page on a typo.
                previews.append({
                    "operation_id": operation.get("operation_id"),
                    "ok": False,
                    "error": {"message": str(error)},
                })
                continue

            preview = {
                "operation_id": operation.get("operation_id"),
                "method": fields["method"],
                "path": fields["path"],
                "payload": payload,
                "execute": False,
            }
            if fields.get("dropped_tags"):
                preview["dropped_tags"] = fields["dropped_tags"]
            previews.append(preview)

        if args.command != "batch":
            only = previews[0]
            if only.get("ok") is False:
                print(json.dumps({"ok": False, "error": only["error"]}, indent=2))
                return 2
            only.pop("operation_id", None)
            dropped = only.pop("dropped_tags", None)
            result = {"dry_run": True, "planned_request": only}
            if dropped:
                result["dropped_tags"] = dropped
            print(json.dumps(result, indent=2))
            return 0

        print(json.dumps({"dry_run": True, "operations": previews}, indent=2))
        return 0

    # From here on, ``--execute`` is active. Apply the shadow brake before
    # loading credentials, initializing audit storage, or touching ClickUp.
    block_message = shadow_write_block_message(force=args.force_live_write)
    if block_message:
        print(block_message)
        return 1

    # Load the credential only after all no-write exit paths have completed.
    try:
        token = load_clickup_credentials(("CLICKUP_API_KEY",))["CLICKUP_API_KEY"]
    except MissingClickUpCredentials as error:
        raise SystemExit(str(error))

    # Database setup is intentionally quiet so stdout remains machine-readable.
    with contextlib.redirect_stdout(io.StringIO()):
        init_db()

    # Execute the prepared batch and translate expected failures to JSON exits.
    try:
        result = execute_batch(operations, token, config=config)
    except ClickUpUnavailableError as error:
        print(json.dumps({"ok": False, "error": error.as_error()}, indent=2))
        return 1
    except ValueError as error:
        print(json.dumps({"ok": False, "error": {"message": str(error)}}, indent=2))
        return 2

    print(json.dumps(result, indent=2))
    return 1 if result.get("unavailable") else 0


if __name__ == "__main__":
    with captain_telemetry.guard("clickup_write"):
        sys.exit(main())
