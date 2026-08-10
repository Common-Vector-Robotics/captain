#!/usr/bin/env python3
"""Preview and execute audited ClickUp task changes for Captain.

The command supports task creation, task updates, comments, and JSON batches.
Without ``--execute`` it prints the request it would make. Executed changes
respect the DailyLoop shadow-mode safety brake, use the shared ClickUp
credential loader, and leave audit records for every supported mutation.

Examples:
    python3 scripts/clickup_write.py create-task --list-id 123 --name "Inspect rover"
    python3 scripts/clickup_write.py --execute comment-task --task-id abc --text "Bench test passed"

This module also exposes the validation and execution helpers used by tests
and other Captain scripts. Network access is injected through ``request_fn``
where practical so those callers can exercise the workflow without contacting
ClickUp.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from captain_db import audit, init_db
from clickup_credentials import MissingClickUpCredentials, load_clickup_credentials
import captain_modes

API = "https://api.clickup.com/api/v2"
STATUS_ALIASES = {"intake": "to do"}

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


def parse_assignees(raw):
    """Convert assignee identifiers to ClickUp's numeric user-ID format.

    Blank items are ignored. A non-numeric value raises an actionable error
    that directs callers to the Owners-label fallback.

    Example input: ["123", " 456 "]
    Example output: [123, 456]
    """
    values = []

    # Normalize every repeated ``--assignee`` value independently.
    for item in raw or []:
        item = str(item).strip()
        if not item:
            continue

        try:
            values.append(int(item))
        except ValueError:
            raise ValueError(
                "ClickUp assignees must be numeric user IDs; a non-numeric "
                "--assignee value was given. If this person cannot be a "
                "built-in ClickUp assignee, use --owner \"<name>\" instead "
                "to fall back to the Owners custom labels field."
            )

    return values


# Owners-label fallback
#
# Prefer ClickUp's built-in numeric assignees. When a person cannot be a
# built-in assignee, preserve ownership in the list's ``Owners`` labels field
# instead of burying it in a task description or other free text.
#
# ClickUp can create a labels field with initial options, but its public API
# cannot append an option to an existing labels field. A missing owner option
# therefore becomes an actionable ``needs_owner_label`` result rather than
# silently losing the owner. Creating a task can set custom fields inline.
# Updating a task requires the separate custom-field endpoint, which overwrites
# the field, so updates read and union existing option IDs before writing.

OWNER_LABEL_PALETTE = [
    "#2ecd6f",
    "#1bbc9c",
    "#3398dc",
    "#9b59b6",
    "#e67e22",
    "#e74c3c",
    "#f1c40f",
    "#667684",
]


def owner_label_color(owner_name):
    """Choose a stable palette color for an owner's label.

    Hashing the raw name makes repeated uses select the same color without
    storing additional state.
    """
    digest_prefix = hashlib.sha1(
        (owner_name or "Owner").encode("utf-8")
    ).hexdigest()[:2]
    idx = int(digest_prefix, 16) % len(OWNER_LABEL_PALETTE)
    return OWNER_LABEL_PALETTE[idx]


def normalize_label_text(value):
    """Normalize punctuation, spacing, and case for owner-name comparisons.

    Example input: "  Ada-Lovelace  "
    Example output: "ada lovelace"
    """
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def clickup_list_fields(list_id, token, request_fn):
    """Return the custom fields configured for one ClickUp list."""
    return (request_fn("GET", "/list/{}/field".format(list_id), token) or {}).get("fields") or []


def find_owners_field(fields):
    """Find the custom field named Owners, if the list has one."""
    for field in fields:
        if normalize_label_text(field.get("name")) == "owners":
            return field

    return None


def owners_label_option(field, owner_name):
    """Find the Owners label option matching a person's normalized name."""
    want = normalize_label_text(owner_name)

    for opt in ((field.get("type_config") or {}).get("options") or []):
        if normalize_label_text(opt.get("label") or opt.get("name")) == want:
            return opt

    return None


def owner_fallback_applies(operation):
    """Return whether an operation needs the Owners-label fallback.

    A valid numeric assignee takes precedence for ``create-task``. The
    ``update-task`` command does not support assignees, so an unrelated
    ``assignee`` key in batch input must not suppress its owner fallback.

    Example output: (True, ["Ada Lovelace"])
    """
    # Normalize owner names and ignore empty repeated arguments.
    owners = [str(name).strip() for name in (operation.get("owner") or []) if str(name).strip()]
    if not owners:
        return False, []

    # A built-in create-task assignee is the preferred ownership mechanism.
    if operation.get("command") == "create-task" and parse_assignees(operation.get("assignee", [])):
        return False, []

    return True, owners


def resolve_owner_field(list_id, owner_names, token, request_fn, audit_fn, source, evidence):
    """Resolve an Owners field and label option IDs for the requested names.

    Returns ``(field, option_ids, needs_owner_label)``. The field may be
    created here, so this lookup is not always read-only. When a pre-existing
    field lacks an option, ``needs_owner_label`` tells a human which options
    to add because ClickUp's API cannot add them.

    A pre-existing ``Owners`` field with the wrong type raises ``ValueError``
    before this function mutates it. A newly created field whose response does
    not contain the requested option IDs also raises instead of falsely
    claiming that a human merely needs to add an option.
    """
    # Reuse the list's Owners field, or create it with every requested option.
    fields = clickup_list_fields(list_id, token, request_fn)
    field = find_owners_field(fields)
    created = False

    if not field:
        payload = {
            "name": "Owners",
            "type": "labels",
            "type_config": {
                "sorting": "manual",
                "options": [
                    {"label": name, "color": owner_label_color(name), "orderindex": index}
                    for index, name in enumerate(owner_names)
                ],
            },
        }

        # Audit both accepted and rejected field-creation attempts.
        try:
            result = request_fn("POST", "/list/{}/field".format(list_id), token, payload)
        except (ClickUpRequestError, ClickUpUnavailableError):
            audit_fn(
                "clickup_custom_field_create_attempt",
                list_id=list_id,
                payload=payload,
                result_id=None,
                ok=False,
                source=source,
                evidence=evidence,
            )
            raise

        field = result.get("field") or result
        created = True

        audit_fn(
            "clickup_custom_field_create_attempt",
            list_id=list_id,
            payload=payload,
            result_id=field.get("id"),
            ok=True,
            source=source,
            evidence=evidence,
        )

    # Refuse to reinterpret a user's pre-existing field with an incompatible type.
    if not created and (field.get("type") or "") != "labels":
        raise ValueError(
            "Owners custom field on list {} is type {!r}, expected 'labels'".format(
                list_id,
                field.get("type"),
            )
        )

    # Resolve every requested name while retaining all unresolved names.
    option_ids, missing = [], []
    for name in owner_names:
        opt = owners_label_option(field, name)
        if opt and opt.get("id"):
            option_ids.append(opt.get("id"))
        else:
            missing.append(name)

    needs_owner_label = None

    if missing:
        # A new field should contain the options just sent in its create payload.
        if created:
            raise ValueError(
                "ClickUp created the Owners labels field on list {} but its response did not "
                "include usable option ids for {!r}; refusing to guess at ownership".format(
                    list_id, missing
                )
            )

        # Existing fields cannot receive new options through ClickUp's API.
        for name in missing:
            audit_fn(
                "clickup_owner_label_missing",
                list_id=list_id,
                field_id=field.get("id"),
                owner=name,
                source=source,
                evidence=evidence,
                needed="Add this owner as an option to the existing Owners labels field on this "
                       "list -- the public ClickUp API cannot append a new option to an existing "
                       "labels field.",
            )

        needs_owner_label = {"list_id": list_id, "owner": missing[0], "owners": missing}

    return field, option_ids, needs_owner_label


def owner_field_existing_value(existing_custom_fields, field_id):
    """Return existing option IDs for one field from a task response.

    A missing field or a value with an unexpected shape returns an empty list.
    """
    for custom_field in existing_custom_fields or []:
        if custom_field.get("id") == field_id:
            value = custom_field.get("value")
            return [item for item in value if item is not None] if isinstance(value, list) else []

    return []


def dedupe_preserve_order(values):
    """Remove repeated values while preserving first-seen order.

    Example input: ["one", "two", "one"]
    Example output: ["one", "two"]
    """
    seen = set()
    result = []

    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)

    return result


# Status and operation preparation


def allowed_statuses(token, list_id, request_fn=request):
    """Return the workflow status names allowed by a ClickUp list."""
    result = request_fn("GET", f"/list/{list_id}", token)
    return [item.get("status") for item in result.get("statuses", []) if item.get("status")]


def resolve_status(requested_status, statuses):
    """Match a requested status to a list status or explain the mismatch.

    Comparisons ignore case and surrounding whitespace. The known ``intake``
    alias maps to ``to do``. A missing Blocked status becomes an actionable
    marker; any other invalid name becomes a validation error.
    """
    # No requested status means the operation should leave status unchanged.
    if requested_status is None:
        return {"applied": None, "requested": None, "mapped": False}

    # First prefer an exact normalized match to a status on the destination list.
    normalized = str(requested_status).strip().casefold()
    by_normalized_name = {str(status).strip().casefold(): status for status in statuses}

    if normalized in by_normalized_name:
        return {
            "applied": by_normalized_name[normalized],
            "requested": requested_status,
            "mapped": False,
        }

    # Then consult Captain's small set of intentional workflow aliases.
    alias = STATUS_ALIASES.get(normalized)
    if alias and alias in by_normalized_name:
        return {"applied": by_normalized_name[alias], "requested": requested_status, "mapped": True}

    # Blocked is special: preserve the request as follow-up work for the list.
    if normalized == "blocked":
        return {
            "applied": None,
            "requested": requested_status,
            "mapped": False,
            "needs_blocked_status": True,
        }

    # Other unknown statuses are ordinary validation failures.
    return {
        "applied": None,
        "requested": requested_status,
        "mapped": False,
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


def operation_fields(operation):
    """Validate an operation and describe its primary ClickUp request.

    The returned metadata contains the HTTP method, API path, audit event, and
    task identity used by later preparation and execution phases.
    """
    command = operation.get("command")

    # New tasks require both a destination list and a human-readable name.
    if command == "create-task":
        list_id = operation.get("list_id")
        name = operation.get("name")

        if not list_id or not name:
            raise ValueError("create-task requires list_id and name")

        return {
            "method": "POST",
            "path": "/list/{}/task".format(list_id),
            "list_id": str(list_id),
            "task_name": name,
            "audit_event": "clickup_task_create",
            "due_date_followup_required": operation.get("due_date_ms") is None,
        }

    # Updates already know their task ID; the destination list may be resolved later.
    if command == "update-task":
        task_id = operation.get("task_id")

        if not task_id:
            raise ValueError("update-task requires task_id")

        return {
            "method": "PUT",
            "path": "/task/{}".format(task_id),
            "list_id": operation.get("list_id"),
            "task_name": operation.get("name") or str(task_id),
            "task_id": str(task_id),
            "audit_event": "clickup_task_update",
            "due_date_followup_required": False,
        }

    # Comments use a separate endpoint and cannot carry Owners-label changes.
    if command == "comment-task":
        task_id = operation.get("task_id")

        if not task_id:
            raise ValueError("comment-task requires task_id")
        if not operation.get("comment_text"):
            raise ValueError("comment-task requires comment_text")

        if operation.get("owner"):
            raise ValueError(
                "comment-task does not support --owner/owner -- the Owners-label fallback only "
                "applies to create-task and update-task, and comment-task has no list_id to "
                "resolve it against"
            )

        return {
            "method": "POST",
            "path": "/task/{}/comment".format(task_id),
            "list_id": None,
            "task_name": str(task_id),
            "task_id": str(task_id),
            "audit_event": "clickup_task_comment",
            "due_date_followup_required": False,
        }

    raise ValueError("unsupported batch command: {}".format(command))


def operation_payload(operation, resolution, existing_description=None, custom_fields_value=None):
    """Build the request body for the operation's primary write.

    ``custom_fields_value`` is valid only for task creation. ClickUp's Update
    Task endpoint does not accept custom fields, so existing tasks receive an
    Owners-field follow-up request only after their primary update succeeds.
    """
    command = operation["command"]

    # Comments have a minimal, command-specific request shape.
    if command == "comment-task":
        return {"comment_text": operation.get("comment_text")}

    # Status mapping may require retaining or annotating the old description.
    description = operation.get("description")
    if description is None and (resolution.get("mapped") or resolution.get("needs_blocked_status")):
        description = existing_description

    description = add_status_note(description, resolution)

    # Create Task accepts assignees and inline custom fields.
    if command == "create-task":
        return clean_payload({
            "name": operation.get("name"),
            "description": description,
            "status": resolution.get("applied"),
            "priority": operation.get("priority"),
            "assignees": parse_assignees(operation.get("assignee", [])),
            "due_date": operation.get("due_date_ms"),
            "due_date_time": False if operation.get("due_date_ms") is not None else None,
            "custom_fields": custom_fields_value,
        })

    # Update Task omits custom fields; those use a dedicated endpoint later.
    return clean_payload({
        "name": operation.get("name"),
        "description": description,
        "status": resolution.get("applied"),
        "priority": operation.get("priority"),
        "due_date": operation.get("due_date_ms"),
        "due_date_time": False if operation.get("due_date_ms") is not None else None,
        "parent": operation.get("parent_task_id"),
    })


def prepare_operation(operation, token, request_fn, status_cache, audit_fn=audit):
    """Validate and assemble one operation before its primary write.

    The result is ``(fields, payload, resolution, validation_error)``. Status
    lookups are cached per list. Resolving the Owners fallback can create the
    list's custom field, so this preparation phase is not always read-only.
    """
    # Validate command-specific required fields before making API requests.
    fields = operation_fields(operation)
    command = operation.get("command")
    requested_status = operation.get("status")
    needs_owner_fallback, owner_names = owner_fallback_applies(operation)

    existing_description = None
    existing_custom_fields = []
    list_id = fields.get("list_id")

    # Updates need their current task when status or owner handling depends on it.
    if command == "update-task" and (requested_status is not None or needs_owner_fallback):
        task = request_fn("GET", "/task/{}".format(fields["task_id"]), token)
        list_id = ((task.get("list") or {}).get("id"))

        if not list_id:
            raise ValueError(
                "could not identify destination list for task {}".format(
                    fields["task_id"]
                )
            )

        fields["task_name"] = operation.get("name") or task.get("name") or fields["task_id"]
        fields["list_id"] = list_id
        existing_description = task.get("description")
        existing_custom_fields = task.get("custom_fields") or []

    # Resolve a requested status against the destination list's real workflow.
    if requested_status is not None:
        if list_id not in status_cache:
            status_cache[list_id] = allowed_statuses(token, list_id, request_fn=request_fn)

        resolution = resolve_status(requested_status, status_cache[list_id])
        if resolution.get("error"):
            return fields, None, None, resolution["error"]
    else:
        resolution = {"applied": None, "requested": None, "mapped": False}

    # Prepare Owners data either inline for creates or as a later update request.
    custom_fields_value = None
    owner_field_update = None

    if needs_owner_fallback:
        # Defense in depth: never make a guaranteed-bad ``/list/None/field``
        # request, even if a new command reaches this helper unexpectedly.
        if not list_id:
            raise ValueError(
                "cannot resolve the Owners-label fallback without a list_id "
                "(operation_id={})".format(operation.get("operation_id"))
            )

        field, option_ids, needs_owner_label = resolve_owner_field(
            list_id, owner_names, token, request_fn, audit_fn,
            operation.get("source", "captain"), operation.get("evidence", []),
        )

        if needs_owner_label:
            resolution["needs_owner_label"] = needs_owner_label

        if command == "create-task":
            # Create Task supports setting the resolved Owners options inline.
            custom_fields_value = [{"id": field["id"], "value": option_ids}] if option_ids else None
        else:
            # The custom-field endpoint overwrites values. Union first so a
            # new owner never deletes an existing one, and skip no-op writes.
            existing_ids = owner_field_existing_value(existing_custom_fields, field["id"])
            union_ids = dedupe_preserve_order(list(existing_ids) + list(option_ids))

            if option_ids and set(union_ids) != set(existing_ids):
                owner_field_update = {"field_id": field["id"], "value": union_ids}

    # Attach any follow-up field write to the status/owner resolution record.
    resolution["owner_field_update"] = owner_field_update

    payload = operation_payload(
        operation,
        resolution,
        existing_description,
        custom_fields_value,
    )
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
    """Execute one prepared operation, audit it, and verify follow-up data.

    The primary write happens first. An Owners-field update and due-date
    verification are separate follow-up requests, each with its own audit
    result so partial success remains visible.
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
        needs_owner_label=resolution.get("needs_owner_label"),
    )

    # Owners on an existing task require a second request. Catch its failure
    # here because the primary task write has already landed; ``execute_batch``
    # reports that partial outcome as both a succeeded task write and a
    # dedicated failed owner-field write.
    owner_field_write = None
    owner_field_update = resolution.get("owner_field_update")

    if owner_field_update:
        field_path = "/task/{}/field/{}".format(task_id, owner_field_update["field_id"])
        audit_kwargs = dict(
            task_id=task_id,
            field_id=owner_field_update["field_id"],
            path=field_path,
            value=owner_field_update["value"],
            source=operation.get("source", "captain"),
            evidence=operation.get("evidence", []),
            operation_id=operation.get("operation_id"),
            needs_owner_label=resolution.get("needs_owner_label"),
        )

        try:
            request_fn("POST", field_path, token, {"value": owner_field_update["value"]})
            owner_field_write = {
                "attempted": True,
                "ok": True,
                "field_id": owner_field_update["field_id"],
                "value": owner_field_update["value"],
            }
            audit_fn("clickup_owner_field_update", ok=True, **audit_kwargs)
        except ClickUpRequestError as error:
            error_data = error.as_error(fields["task_name"])
            owner_field_write = {
                "attempted": True,
                "ok": False,
                "field_id": owner_field_update["field_id"],
                "value": owner_field_update["value"],
                "error": error_data,
            }
            audit_fn("clickup_owner_field_update", ok=False, error=error_data, **audit_kwargs)
        except ClickUpUnavailableError as error:
            error_data = error.as_error()
            owner_field_write = {
                "attempted": True,
                "ok": False,
                "field_id": owner_field_update["field_id"],
                "value": owner_field_update["value"],
                "error": error_data,
                "unavailable": True,
            }
            audit_fn(
                "clickup_owner_field_update",
                ok=False,
                error=error_data,
                unavailable=True,
                **audit_kwargs,
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
        "needs_owner_label": resolution.get("needs_owner_label"),
        "owner_field_write": owner_field_write,
    }


def execute_batch(operations, token, request_fn=request, audit_fn=audit):
    """Preflight all operations, then execute every valid mutation exactly once.

    Preflight is not always read-only: resolving an Owners fallback may create
    a list-level custom field. If ClickUp becomes unavailable during either
    phase, the result identifies the uncertain operation and every operation
    that was not attempted instead of dropping them from the report.
    """
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
                audit_fn=audit_fn,
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

            # Prepared items may already have created Owners fields, but their
            # primary writes did not run. Report them and all later items.
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

        # A failed owner follow-up is partial success: retain the primary write
        # in ``succeeded`` and add a dedicated failure for retry workflows.
        owner_field_write = op_result.get("owner_field_write")
        if owner_field_write and not owner_field_write.get("ok"):
            error_data = dict(owner_field_write.get("error") or {})

            if owner_field_write.get("unavailable"):
                error_data.update({"unavailable": True, "unknown": True, "retryable": False})
            else:
                error_data["retryable"] = True

            failed.append({
                "operation_id": "{}:owner-field".format(operation["operation_id"]),
                "task_name": fields["task_name"],
                "error": error_data,
            })

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

    # Creation needs the destination list plus the complete initial task shape.
    if args.command == "create-task":
        return dict(
            common,
            list_id=args.list_id,
            name=args.name,
            description=args.description,
            status=args.status,
            priority=args.priority,
            assignee=args.assignee,
            owner=args.owner,
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
        owner=args.owner,
        due_date_ms=args.due_date_ms,
        parent_task_id=args.parent_task_id,
    )


def main():
    """Preview or execute audited ClickUp changes from the command line."""
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
    sub = parser.add_subparsers(dest="command", required=True)

    # create-task accepts built-in assignees or the Owners-label fallback.
    create = sub.add_parser("create-task")
    create.add_argument("--list-id", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--description")
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
            "Numeric ClickUp user ID. Repeat for multiple assignees. Always preferred over "
            "--owner: when both are given, --assignee wins and no Owners label is set."
        ),
    )
    create.add_argument(
        "--owner",
        action="append",
        default=[],
        help=(
            "Human name/label for ClickUp's Owners custom labels field, used only as a "
            "fallback when no numeric --assignee is available for this person. Creates the "
            "Owners field on the list if it does not exist yet. Repeat for multiple owners."
        ),
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
        "--owner",
        action="append",
        default=[],
        help=(
            "Same Owners-label fallback as create-task's --owner (update-task has no "
            "--assignee option, so this is applied whenever given)."
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

    args = parser.parse_args()

    # Parse batch input or normalize one subcommand into a single-item batch.
    if args.command == "batch":
        try:
            operations = parse_operations_file(args.operations_file)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit("Malformed batch input: {}".format(error))
        if not args.execute:
            print(json.dumps({"dry_run": True, "operations": operations}, indent=2))
            return 0
    else:
        operations = [operation_from_args(args)]

        if not args.execute:
            operation = operations[0]

            # A dry run validates only local request shape and makes no API calls.
            try:
                fields = operation_fields(operation)
                payload = operation_payload(
                    operation,
                    {
                        "applied": operation.get("status"),
                        "requested": operation.get("status"),
                        "mapped": False,
                    },
                )
            except ValueError as error:
                # Treat CLI mistakes as normal validation failures. Letting one
                # reach the telemetry guard would incorrectly page on a typo.
                print(json.dumps({"ok": False, "error": {"message": str(error)}}, indent=2))
                return 2

            preview = {
                "dry_run": True,
                "planned_request": {
                    "method": fields["method"],
                    "path": fields["path"],
                    "payload": payload,
                    "execute": False,
                },
            }
            needs_owner_fallback, owner_names = owner_fallback_applies(operation)

            if needs_owner_fallback:
                # Resolving Owners IDs requires ClickUp. Surface pending names
                # and the possible field creation instead of guessing offline.
                preview["owner_label_pending"] = owner_names
                preview["owner_field_create_warning"] = (
                    "If the destination list has no 'Owners' labels custom field yet, running "
                    "this with --execute will CREATE one -- a new list-level custom field. That "
                    "is the one irreversible mutation this dry run cannot rule out without a "
                    "real ClickUp GET."
                )

            print(json.dumps(preview, indent=2))
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
        result = execute_batch(operations, token)
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
