#!/usr/bin/env python3
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

# Escape hatch for a deliberate manual write while in shadow (env var or --force-live-write).
SHADOW_ESCAPE_ENV = "CAPTAIN_CLICKUP_FORCE_LIVE_WRITE"


def dailyloop_audience():
    """Read `DailyLoop.audience` from data/captain-modes.json.

    A **missing** file (or a missing `DailyLoop` key) means `off` — `off` means the
    loop is inert, not that manual ClickUp tooling is banned, so this script applies
    no new restriction in that case.

    A file that **exists but cannot be parsed** (corrupt JSON, or an I/O error
    reading a file we just confirmed is there) is a different situation: it means the
    shadow-mode safety brake itself is broken, not that DailyLoop is unconfigured.
    Returning `off` for that case would fail OPEN — silently permitting real ClickUp
    writes while in shadow because the safety file got corrupted. Fail CLOSED
    instead: treat it as `shadow` (refuse writes) and capture_message so someone
    learns the file is corrupt.
    """
    if not captain_modes.MODE_PATH.exists():
        return "off"
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
    """Return a refusal message if this write should be suppressed by DailyLoop shadow
    mode, else None. `force` (CLI --force-live-write or the SHADOW_ESCAPE_ENV env var)
    is the human operator's explicit escape hatch for a deliberate manual write."""
    if force or os.environ.get(SHADOW_ESCAPE_ENV):
        return None
    if dailyloop_audience() != "shadow":
        return None
    return (
        "Refused: DailyLoop is in shadow mode (data/captain-modes.json -> "
        "DailyLoop.audience). No ClickUp mutation was made and no clickup_* audit row "
        "was written. To force a deliberate manual write while in shadow, pass "
        "--force-live-write or set {}=1.".format(SHADOW_ESCAPE_ENV)
    )


def now():
    return datetime.now(timezone.utc).isoformat()


class ClickUpRequestError(Exception):
    def __init__(self, http_status, clickup_message, path):
        super().__init__(clickup_message)
        self.http_status = http_status
        self.clickup_message = clickup_message
        self.path = path

    def as_error(self, task_name):
        return {
            "http_status": self.http_status,
            "clickup_message": self.clickup_message,
            "path": self.path,
            "task_name": task_name,
        }


class ClickUpUnavailableError(Exception):
    def __init__(self, message, path):
        super().__init__(message)
        self.message = message
        self.path = path

    def as_error(self):
        return {"message": self.message, "path": self.path}


def clickup_error_message(error):
    try:
        raw = error.read().decode("utf-8", errors="replace")
        body = json.loads(raw) if raw else {}
    except (OSError, ValueError, UnicodeError):
        body = {}
    if isinstance(body, dict):
        for key in ("err", "message", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "ClickUp rejected the request"


def request(method, path, token, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=body,
        method=method,
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        message = clickup_error_message(error)
        if error.code >= 500 or error.code == 429:
            raise ClickUpUnavailableError(message, path)
        raise ClickUpRequestError(error.code, message, path)
    except (urllib.error.URLError, TimeoutError) as error:
        raise ClickUpUnavailableError("ClickUp API is unavailable", path) from error


def verify_due_date(token, task_id, expected_due_date_ms, request_fn=request):
    if expected_due_date_ms is None:
        return {"checked": False}
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
    return {key: value for key, value in values.items() if value is not None and value != []}


def parse_assignees(raw):
    values = []
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


# --- Owners custom-field fallback -------------------------------------------
#
# Ported from scripts/weekly_slack_clickup_status.py (ensure_owners_field_for_label
# and friends), which is deleted at the Task 12 Phase B cutover. The owner's
# standing rule (MEMORY.md, 2026-06-16 / 06-18 / 06-23): prefer the built-in
# ClickUp assignee field first; if the person cannot be a built-in assignee, use
# the `Owners` custom **labels** field -- creating it on the list if missing,
# adding the needed owner label -- rather than leaving ownership only in the
# task description or free text.
#
# Hard API constraint (verified against ClickUp's developer docs and feedback
# tracker): the public API can create a labels field with initial options, and
# can set an existing option's value on a task, but it cannot append a new
# option to an *existing* labels field. That is an open feature request, not a
# gap in this tooling -- so when the field already exists but lacks the
# requested label, we must not silently drop ownership and must not write it
# into the description (the rule explicitly forbids that). We surface it as an
# actionable `needs_owner_label` marker instead, mirroring how
# `needs_blocked_status` already surfaces an equivalent can't-do-it-via-API gap.
#
# Write shape: `create-task`'s POST /list/{id}/task supports inline
# `custom_fields` in the create body -- that IS a documented Create Task
# parameter, so create-task keeps setting Owners inline. `update-task`'s
# PUT /task/{id} does NOT support `custom_fields` as an update parameter;
# setting a custom field on an existing task is a *different* endpoint,
# POST /task/{id}/field/{field_id} with {"value": [...]}, and per ClickUp's
# docs that call OVERWRITES whatever option ids are already on the task's
# labels field. So update-task must read-and-union: read the task's current
# Owners value (from the GET already performed in prepare_operation) and union
# it with the newly resolved option ids before writing, or a second owner's
# label would silently delete the first owner's label.

OWNER_LABEL_PALETTE = ["#2ecd6f", "#1bbc9c", "#3398dc", "#9b59b6", "#e67e22", "#e74c3c", "#f1c40f", "#667684"]


def owner_label_color(owner_name):
    idx = int(hashlib.sha1((owner_name or "Owner").encode("utf-8")).hexdigest()[:2], 16) % len(OWNER_LABEL_PALETTE)
    return OWNER_LABEL_PALETTE[idx]


def normalize_label_text(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def clickup_list_fields(list_id, token, request_fn):
    return (request_fn("GET", "/list/{}/field".format(list_id), token) or {}).get("fields") or []


def find_owners_field(fields):
    for field in fields:
        if normalize_label_text(field.get("name")) == "owners":
            return field
    return None


def owners_label_option(field, owner_name):
    want = normalize_label_text(owner_name)
    for opt in ((field.get("type_config") or {}).get("options") or []):
        if normalize_label_text(opt.get("label") or opt.get("name")) == want:
            return opt
    return None


def owner_fallback_applies(operation):
    """Decide whether the Owners-label fallback should be consulted for this
    operation. `--assignee` always wins when it names an actual numeric
    ClickUp user AND the command supports setting assignees (only create-task
    does today -- update-task has no `--assignee` option, so a stray
    `assignee` key on an update-task batch operation must not block the
    Owners fallback there)."""
    owners = [str(name).strip() for name in (operation.get("owner") or []) if str(name).strip()]
    if not owners:
        return False, []
    if operation.get("command") == "create-task" and parse_assignees(operation.get("assignee", [])):
        return False, []
    return True, owners


def resolve_owner_field(list_id, owner_names, token, request_fn, audit_fn, source, evidence):
    """Resolve the ClickUp Owners-labels custom field and the option ids for
    `owner_names` on `list_id`. Returns (field, option_ids, needs_owner_label):

    - `field` is the (possibly just-created) Owners field dict, always
      present -- callers use `field["id"]`.
    - `option_ids` is the list of option ids that resolved for `owner_names`
      (empty if none resolved).
    - `needs_owner_label` is None when every requested owner name resolved to
      an existing label option, else `{"list_id": ..., "owner": <first missing
      name>, "owners": [<all missing names>]}` -- the actionable marker for
      case 3 (field exists, but this owner isn't one of its label options; the
      public API cannot add one). Propagated by the caller into both the audit
      record and the per-operation result, mirroring `needs_blocked_status`.

    Important: the wrong-type guard and the "missing option" check are both
    gated on whether *this call* just created the field. A field this call
    created is never reported as wrong-typed (we requested type 'labels'
    ourselves) or as missing its own label (telling a human to add a label
    option that this call just added would be nonsensical) -- gating on
    `created` keeps that guard from running after a real mutation and
    reporting a false "safe, nothing happened" story. If ClickUp's create
    response doesn't give us usable option ids for the labels we just asked
    it to create, that's a malformed-response situation, not a "someone needs
    to add a label" situation, so it raises a distinct ValueError instead of
    a misleading `needs_owner_label`.

    Raises ValueError if the list already has a *pre-existing* `Owners` field
    that is not a `labels` field -- a real misconfiguration. That case is
    raised before any mutation is attempted, so it cannot corrupt data; the
    caller lets it propagate as a normal per-operation failure (same as any
    other ValueError from `prepare_operation`).
    """
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
        try:
            result = request_fn("POST", "/list/{}/field".format(list_id), token, payload)
        except (ClickUpRequestError, ClickUpUnavailableError):
            # Minor: a rejected create must still leave an audit trace -- the
            # event is named `..._create_attempt`, so it must fire on failure
            # too, not only on success.
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
    if not created and (field.get("type") or "") != "labels":
        raise ValueError(
            "Owners custom field on list {} is type {!r}, expected 'labels'".format(list_id, field.get("type"))
        )
    option_ids, missing = [], []
    for name in owner_names:
        opt = owners_label_option(field, name)
        if opt and opt.get("id"):
            option_ids.append(opt.get("id"))
        else:
            missing.append(name)
    needs_owner_label = None
    if missing:
        if created:
            raise ValueError(
                "ClickUp created the Owners labels field on list {} but its response did not "
                "include usable option ids for {!r}; refusing to guess at ownership".format(
                    list_id, missing
                )
            )
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
    """Return the list of option ids already set for `field_id` on a task, from
    the `custom_fields` array of a prior GET /task response. Empty list if the
    field isn't present on the task yet or carries no value."""
    for custom_field in existing_custom_fields or []:
        if custom_field.get("id") == field_id:
            value = custom_field.get("value")
            return [item for item in value if item is not None] if isinstance(value, list) else []
    return []


def dedupe_preserve_order(values):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def allowed_statuses(token, list_id, request_fn=request):
    result = request_fn("GET", f"/list/{list_id}", token)
    return [item.get("status") for item in result.get("statuses", []) if item.get("status")]


def resolve_status(requested_status, statuses):
    if requested_status is None:
        return {"applied": None, "requested": None, "mapped": False}
    normalized = str(requested_status).strip().casefold()
    by_normalized_name = {str(status).strip().casefold(): status for status in statuses}
    if normalized in by_normalized_name:
        return {"applied": by_normalized_name[normalized], "requested": requested_status, "mapped": False}
    alias = STATUS_ALIASES.get(normalized)
    if alias and alias in by_normalized_name:
        return {"applied": by_normalized_name[alias], "requested": requested_status, "mapped": True}
    if normalized == "blocked":
        return {
            "applied": None,
            "requested": requested_status,
            "mapped": False,
            "needs_blocked_status": True,
        }
    return {
        "applied": None,
        "requested": requested_status,
        "mapped": False,
        "error": {"invalid_status": requested_status, "allowed_statuses": statuses},
    }


def add_status_note(description, resolution):
    if not resolution.get("needs_blocked_status"):
        return description
    note = ("Requested workflow state: Blocked — this list has no Blocked status yet. "
            "Add it in List/Space settings; status left unchanged.")
    if description and note in description:
        return description
    return "\n\n".join(part for part in (description, note) if part)


def operation_fields(operation):
    command = operation.get("command")
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

    `custom_fields_value` is ONLY ever used for `create-task`: POST
    /list/{id}/task is a documented Create Task parameter that accepts inline
    `custom_fields`. `update-task`'s PUT /task/{id} has no such parameter --
    ClickUp does not support setting custom fields through Update Task, so
    that body must never carry a `custom_fields` key. Setting the Owners
    field on an existing task goes through a separate POST
    /task/{id}/field/{field_id} call made after this payload's request
    succeeds (see `execute_prepared_operation`'s `owner_field_update`).
    """
    command = operation["command"]
    if command == "comment-task":
        return {"comment_text": operation.get("comment_text")}
    description = operation.get("description")
    if description is None and (resolution.get("mapped") or resolution.get("needs_blocked_status")):
        description = existing_description
    description = add_status_note(description, resolution)
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
    fields = operation_fields(operation)
    command = operation.get("command")
    requested_status = operation.get("status")
    needs_owner_fallback, owner_names = owner_fallback_applies(operation)
    existing_description = None
    existing_custom_fields = []
    list_id = fields.get("list_id")
    if command == "update-task" and (requested_status is not None or needs_owner_fallback):
        task = request_fn("GET", "/task/{}".format(fields["task_id"]), token)
        list_id = ((task.get("list") or {}).get("id"))
        if not list_id:
            raise ValueError("could not identify destination list for task {}".format(fields["task_id"]))
        fields["task_name"] = operation.get("name") or task.get("name") or fields["task_id"]
        fields["list_id"] = list_id
        existing_description = task.get("description")
        existing_custom_fields = task.get("custom_fields") or []

    if requested_status is not None:
        if list_id not in status_cache:
            status_cache[list_id] = allowed_statuses(token, list_id, request_fn=request_fn)
        resolution = resolve_status(requested_status, status_cache[list_id])
        if resolution.get("error"):
            return fields, None, None, resolution["error"]
    else:
        resolution = {"applied": None, "requested": None, "mapped": False}

    custom_fields_value = None
    owner_field_update = None
    if needs_owner_fallback:
        # Defense in depth: operation_fields already rejects `owner` on
        # comment-task (the only command with no list_id), but never attempt
        # the field lookup without a real destination list regardless of
        # which command got here -- a lookup against `/list/None/field` is a
        # guaranteed, permanently-broken 404.
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
            # POST /list/{id}/task supports inline custom_fields -- set directly.
            custom_fields_value = [{"id": field["id"], "value": option_ids}] if option_ids else None
        else:
            # PUT /task/{id} does not support custom_fields. Read-and-union
            # against what's already on the task so setting a new owner never
            # deletes an existing one; skip the follow-up write entirely if
            # the union doesn't actually add anything new.
            existing_ids = owner_field_existing_value(existing_custom_fields, field["id"])
            union_ids = dedupe_preserve_order(list(existing_ids) + list(option_ids))
            if option_ids and set(union_ids) != set(existing_ids):
                owner_field_update = {"field_id": field["id"], "value": union_ids}

    resolution["owner_field_update"] = owner_field_update
    return fields, operation_payload(operation, resolution, existing_description, custom_fields_value), resolution, None


def resolve_audited_task_id(fields, result):
    """Return the task id to attribute this operation to.

    Only `create-task` learns its task id from the response body (the response *is*
    the created task). Every other command already knows its target task id ahead of
    the request — for `comment-task` in particular, the response is the *comment*
    object, whose `id` is a comment id, not a task id, and must never be used here.
    """
    if fields.get("task_id"):
        return fields["task_id"]
    return result.get("id")


def execute_prepared_operation(operation, fields, payload, resolution, token, request_fn, audit_fn):
    result = request_fn(fields["method"], fields["path"], token, payload)
    task_id = resolve_audited_task_id(fields, result)
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

    # The Owners label follow-up for update-task: this is a SECOND request
    # after the primary task write above just succeeded. Ordering matters --
    # if this call fails, the task write already landed and must not be
    # reported as if it (or the whole operation) simply failed; it's a
    # distinct, partial outcome ("task exists, ownership did not land") that
    # the caller needs to be able to tell apart from both blanket success and
    # blanket failure. We catch failures here (rather than let them propagate)
    # so the primary result below is still returned/counted as succeeded;
    # `execute_batch` inspects `owner_field_write` to also surface a
    # dedicated `failed` entry for the ownership half.
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
                "attempted": True, "ok": False, "field_id": owner_field_update["field_id"],
                "value": owner_field_update["value"], "error": error_data,
            }
            audit_fn("clickup_owner_field_update", ok=False, error=error_data, **audit_kwargs)
        except ClickUpUnavailableError as error:
            error_data = error.as_error()
            owner_field_write = {
                "attempted": True, "ok": False, "field_id": owner_field_update["field_id"],
                "value": owner_field_update["value"], "error": error_data, "unavailable": True,
            }
            audit_fn("clickup_owner_field_update", ok=False, error=error_data, unavailable=True, **audit_kwargs)

    if operation.get("due_date_ms") is not None:
        try:
            due_date_verification = verify_due_date(token, task_id, operation["due_date_ms"], request_fn=request_fn)
        except ClickUpRequestError as error:
            due_date_verification = {"checked": True, "ok": False, "error": error.as_error(fields["task_name"])}
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

    The preflight phase is NOT purely read-only: the Owners-label fallback's
    field lookup (`resolve_owner_field`, called from `prepare_operation`) can
    itself create a real ClickUp list-level custom field before the main
    execution loop runs. Callers must not assume anything about preflight is
    inert, and a ClickUp outage encountered during preflight (429/5xx) is
    handled the same way an outage during execution is -- caught here and
    turned into a `failed` entry (plus "not attempted" entries for whatever
    hadn't been preflighted yet) rather than allowed to propagate out of this
    function with no `succeeded`/`failed` arrays for the caller to inspect.
    """
    operations = [dict(operation) for operation in operations]
    operation_ids = [operation.get("operation_id") for operation in operations]
    if any(not operation_id for operation_id in operation_ids):
        raise ValueError("every batch operation requires a non-empty operation_id")
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("batch operation_id values must be unique")
    succeeded, failed, prepared, status_cache = [], [], [], {}
    for index, operation in enumerate(operations):
        try:
            fields, payload, resolution, validation_error = prepare_operation(operation, token, request_fn, status_cache, audit_fn=audit_fn)
        except ClickUpRequestError as error:
            task_name = operation.get("name") or operation.get("task_id")
            error_data = error.as_error(task_name)
            error_data["retryable"] = True
            failed.append({"operation_id": operation["operation_id"], "task_name": task_name, "error": error_data})
            continue
        except ClickUpUnavailableError as error:
            task_name = operation.get("name") or operation.get("task_id")
            error_data = error.as_error()
            error_data.update({"unavailable": True, "unknown": True, "retryable": False})
            failed.append({"operation_id": operation["operation_id"], "task_name": task_name, "error": error_data})
            # Anything already fully preflighted (possibly including a real
            # field-create mutation from its own Owners-label lookup) never
            # reaches the execution loop now -- account for it as "not
            # attempted" too, same as the execution loop does for operations
            # queued behind a failure, so nothing is silently dropped.
            not_attempted = [prepared_operation for prepared_operation, _, _, _ in prepared] + operations[index + 1:]
            for pending_operation in not_attempted:
                failed.append({
                    "operation_id": pending_operation["operation_id"],
                    "task_name": pending_operation.get("name") or pending_operation.get("task_id"),
                    "error": {"message": "Not attempted because ClickUp API became unavailable", "unavailable": True, "retryable": True},
                })
            return {"ok": False, "succeeded": succeeded, "failed": failed, "unavailable": error.as_error()}
        except ValueError as error:
            failed.append({"operation_id": operation["operation_id"], "task_name": operation.get("name"), "error": {"message": str(error)}})
            continue
        if validation_error:
            validation_error["retryable"] = True
            failed.append({"operation_id": operation["operation_id"], "task_name": fields["task_name"], "error": validation_error})
            continue
        prepared.append((operation, fields, payload, resolution))

    for index, (operation, fields, payload, resolution) in enumerate(prepared):
        try:
            op_result = execute_prepared_operation(operation, fields, payload, resolution, token, request_fn, audit_fn)
        except ClickUpRequestError as error:
            error_data = error.as_error(fields["task_name"])
            error_data["retryable"] = True
            failed.append({"operation_id": operation["operation_id"], "task_name": fields["task_name"], "error": error_data})
            continue
        except ClickUpUnavailableError as error:
            error_data = error.as_error()
            error_data.update({"unavailable": True, "unknown": True, "retryable": False})
            failed.append({"operation_id": operation["operation_id"], "task_name": fields["task_name"], "error": error_data})
            for pending_operation, pending_fields, _, _ in prepared[index + 1:]:
                failed.append({
                    "operation_id": pending_operation["operation_id"],
                    "task_name": pending_fields["task_name"],
                    "error": {"message": "Not attempted because ClickUp API became unavailable", "unavailable": True, "retryable": True},
                })
            return {"ok": False, "succeeded": succeeded, "failed": failed, "unavailable": error.as_error()}
        succeeded.append(op_result)
        # The primary task write above already succeeded (that's why we're
        # here at all) -- an owner-field-write failure is a genuine partial
        # outcome, not a blanket success or blanket failure: the task exists
        # (stays in `succeeded`, with `owner_field_write` describing what
        # happened to it) AND a dedicated `failed` entry is added so a caller
        # that only scans `failed` for retryable work still sees it.
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


def parse_operations_file(path):
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    operations = data.get("operations") if isinstance(data, dict) else data
    if not isinstance(operations, list):
        raise ValueError("batch input must be a JSON array or an object with an operations array")
    return operations


def operation_from_args(args):
    common = {"operation_id": "single", "command": args.command, "source": args.source, "evidence": args.evidence}
    if args.command == "create-task":
        return dict(common, list_id=args.list_id, name=args.name, description=args.description, status=args.status,
                    priority=args.priority, assignee=args.assignee, owner=args.owner, due_date_ms=args.due_date_ms)
    if args.command == "comment-task":
        return dict(common, task_id=args.task_id, comment_text=args.comment_text)
    return dict(common, task_id=args.task_id, name=args.name, description=args.description, status=args.status,
                priority=args.priority, owner=args.owner, due_date_ms=args.due_date_ms, parent_task_id=args.parent_task_id)


def main():
    parser = argparse.ArgumentParser(description="Audited Captain ClickUp writes. Use only for explicit human requests or approved proposals.")
    parser.add_argument("--execute", action="store_true", help="Actually mutate ClickUp. Without this, prints the planned request only.")
    parser.add_argument("--force-live-write", action="store_true",
                         help="Escape hatch: override DailyLoop shadow-mode write suppression for a deliberate manual write.")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-task")
    create.add_argument("--list-id", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--description")
    create.add_argument("--status")
    create.add_argument("--priority", type=int, choices=[1, 2, 3, 4], help="1 urgent, 2 high, 3 normal, 4 low")
    create.add_argument("--assignee", action="append", default=[],
                         help="Numeric ClickUp user ID. Repeat for multiple assignees. Always preferred over "
                              "--owner: when both are given, --assignee wins and no Owners label is set.")
    create.add_argument("--owner", action="append", default=[],
                         help="Human name/label for ClickUp's Owners custom labels field, used only as a "
                              "fallback when no numeric --assignee is available for this person. Creates the "
                              "Owners field on the list if it does not exist yet. Repeat for multiple owners.")
    create.add_argument("--due-date-ms", type=int, help="ClickUp due date in Unix milliseconds")
    create.add_argument("--source", default="captain")
    create.add_argument("--evidence", action="append", default=[])

    update = sub.add_parser("update-task")
    update.add_argument("--task-id", required=True)
    update.add_argument("--name")
    update.add_argument("--description")
    update.add_argument("--status")
    update.add_argument("--priority", type=int, choices=[1, 2, 3, 4])
    update.add_argument("--owner", action="append", default=[],
                         help="Same Owners-label fallback as create-task's --owner (update-task has no "
                              "--assignee option, so this is applied whenever given).")
    update.add_argument("--due-date-ms", type=int)
    update.add_argument("--parent-task-id", help="Set/move task under this ClickUp parent task ID. Use only for explicit structural-change requests.")
    update.add_argument("--source", default="captain")
    update.add_argument("--evidence", action="append", default=[])

    comment = sub.add_parser("comment-task")
    comment.add_argument("--task-id", required=True)
    comment.add_argument("--text", dest="comment_text", required=True)
    comment.add_argument("--source", default="captain")
    comment.add_argument("--evidence", action="append", default=[])

    batch = sub.add_parser("batch")
    batch.add_argument("--operations-file", default="-", help="JSON array or {operations:[...]}; use - for stdin")

    args = parser.parse_args()
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
            try:
                fields = operation_fields(operation)
                payload = operation_payload(operation, {"applied": operation.get("status"), "requested": operation.get("status"), "mapped": False})
            except ValueError as error:
                # A plain user/CLI error (e.g. a non-numeric --assignee) must exit
                # non-zero cleanly, the same as the --execute path below -- it must
                # not escape main() and reach the captain_telemetry.guard() wrap
                # around __main__, which would page someone on a typo.
                print(json.dumps({"ok": False, "error": {"message": str(error)}}, indent=2))
                return 2
            preview = {"dry_run": True, "planned_request": {"method": fields["method"], "path": fields["path"], "payload": payload, "execute": False}}
            needs_owner_fallback, owner_names = owner_fallback_applies(operation)
            if needs_owner_fallback:
                # The Owners field/label lookup requires a real ClickUp GET and is only
                # done at --execute time (prepare_operation); this preview cannot resolve
                # a field_id/option_id without a network call, so it surfaces the pending
                # names instead of guessing at custom_fields.
                preview["owner_label_pending"] = owner_names
                preview["owner_field_create_warning"] = (
                    "If the destination list has no 'Owners' labels custom field yet, running "
                    "this with --execute will CREATE one -- a new list-level custom field. That "
                    "is the one irreversible mutation this dry run cannot rule out without a "
                    "real ClickUp GET."
                )
            print(json.dumps(preview, indent=2))
            return 0

    # `args.execute` is True past this point. Shadow mode refuses real writes by
    # construction: no ClickUp mutation, no clickup_* audit row. `off` and `live` (and the
    # escape hatch) proceed exactly as before this brake was added.
    block_message = shadow_write_block_message(force=args.force_live_write)
    if block_message:
        print(block_message)
        return 1

    try:
        token = load_clickup_credentials(("CLICKUP_API_KEY",))["CLICKUP_API_KEY"]
    except MissingClickUpCredentials as error:
        raise SystemExit(str(error))
    with contextlib.redirect_stdout(io.StringIO()):
        init_db()
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
