"""Send one coding-session report to Captain.

Start with :func:`handle_session_report` at the bottom of this file. It follows
four steps:

1. Validate the report.
2. Reserve its report ID in a small SQLite database.
3. Ask the local OpenClaw process to run Captain.
4. Save Captain's result so the same report is not sent twice.

A report ID works like a receipt number. If a completed receipt is already in
the database, the saved result is returned instead of repeating Captain's
ClickUp work.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


# These are the only statuses the Captain plugin may return.
ReportStatus = Literal[
    "created",
    "updated",
    "queued",
    "needs_clarification",
    "needs_configuration",
    "partial",
    "failed",
    "unknown_outcome",
]

ALLOWED_STATUSES = set(ReportStatus.__args__)
REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_REPORT_BYTES = 1_000_000
DEFAULT_OPENCLAW_COMMAND = "openclaw"
DEFAULT_AGENT_ID = "captain"
DEFAULT_THINKING = "high"
DEFAULT_TIMEOUT_SECONDS = 300
OpenClawRunner = Callable[
    [Sequence[str], str, int],
    subprocess.CompletedProcess[str],
]

# The database coordinates separate running programs. This set handles report
# requests that are running at the same time inside this program.
_ACTIVE_REPORT_IDS: set[str] = set()
_ACTIVE_REPORTS_LOCK = threading.Lock()

# The caller may try these results again after correcting the input or setup.
STATUSES_THAT_CAN_BE_RETRIED = {
    "failed",
    "needs_clarification",
    "needs_configuration",
    "queued",
}

# Reuse these saved results because sending again could repeat ClickUp work.
STATUSES_THAT_MUST_BE_REUSED = {
    "created",
    "partial",
    "unknown_outcome",
    "updated",
}

# Identity and credentials come from the local OpenClaw setup, never a report.
RESERVED_INPUT_KEYS = {
    "access_token",
    "auth",
    "authentication",
    "authenticated_email",
    "authenticated_user",
    "authorization",
    "auth_claims",
    "claims",
    "identity",
    "identity_claims",
    "user_claims",
}


# Keep error messages safe.
class _ProcessStartError(Exception):
    """Raised when OpenClaw could not be started."""


def _redact_secrets(value: str) -> str:
    """Remove common credential patterns from an error message."""

    # Hide tokens written as ``Bearer <token>``.
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [redacted]",
        value,
    )

    # Hide usernames and passwords written inside a web address.
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@/\s]+@",
        r"\1[redacted]@",
        text,
    )

    # Hide values assigned to common secret names such as ``api_key``.
    return re.sub(
        r"(?i)\b(access[_-]?token|api[_-]?key|authorization|auth|password|secret|token)"
        r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1\2[redacted]",
        text,
    )


def _redact_and_shorten_text(value: Any, *, limit: int = 1_000) -> str:
    """Redact and shorten text received from another process."""

    # Turn the value into text and remove anything that looks like a secret.
    text = _redact_secrets(str(value))

    # Keep error messages small enough to return safely.
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


# Build the result returned to the caller.
# Keep every plugin response in this one consistent shape.
# Do not add a class docstring here. Pydantic would copy it into the plugin's
# public description and change what other programs see.
class CaptainReportResult(BaseModel):
    report_id: str
    status: ReportStatus
    clickup_updates: list[dict[str, Any]] = Field(default_factory=list)
    captain_feedback: str
    questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _ReportReservation:
    """Explain whether this call should send or reuse a saved result."""

    should_send: bool
    saved_result: CaptainReportResult | None


def canonical_result(
    report_id: str,
    status: str,
    *,
    captain_feedback: str,
    clickup_updates: list[dict[str, Any]] | None = None,
    questions: list[str] | None = None,
    warnings: list[str] | None = None,
) -> CaptainReportResult:
    """Build the public result returned by every code path."""

    # Copy the warnings so this function never changes the caller's list.
    result_warnings = list(warnings or [])

    # An unfamiliar status means we cannot be sure what Captain completed.
    if status not in ALLOWED_STATUSES:
        safe_status = _redact_and_shorten_text(status, limit=200)
        result_warnings.append(f"Unexpected Captain status: {safe_status}")
        status = "unknown_outcome"

    # Use empty lists when optional result details were not supplied.
    return CaptainReportResult(
        report_id=report_id,
        status=status,
        clickup_updates=clickup_updates or [],
        captain_feedback=captain_feedback,
        questions=questions or [],
        warnings=result_warnings,
    )


# Check the submitted report.
def _summary_items(report: Mapping[str, Any]) -> list[str]:
    """Return the report's non-empty summary items as a list."""

    # Most reports provide a list of summary items.
    summary = report.get("summary")
    if isinstance(summary, list):
        return [str(item).strip() for item in summary if str(item).strip()]

    # A report may also provide one summary sentence.
    if isinstance(summary, str) and summary.strip():
        return [summary.strip()]

    # Every other value means that no useful summary was provided.
    return []


def _is_reserved_input_key(key: Any) -> bool:
    """Return whether a key names identity or authentication data."""

    # A JSON name should be text. Other Python values cannot match these names.
    if not isinstance(key, str):
        return False

    # Convert names such as ``authClaims`` and ``auth-claims`` to the same form.
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")

    # Check both exact names and common prefixes or endings.
    return (
        normalized in RESERVED_INPUT_KEYS
        or normalized.startswith("authenticated_")
        or normalized.startswith("authentication_")
        or normalized.startswith("authorization_")
        or normalized.startswith("auth_")
        or normalized.startswith("identity_")
        or normalized.endswith("_claims")
    )


def _find_reserved_input_key(value: Any) -> str | None:
    """Find the first reserved key anywhere inside a nested value."""

    # Search every name in a dictionary, then search the value under that name.
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _is_reserved_input_key(key):
                return str(key)
            reserved_key = _find_reserved_input_key(nested)
            if reserved_key:
                return reserved_key

    # Lists and tuples can contain more dictionaries, so search each item.
    elif isinstance(value, (list, tuple)):
        for nested in value:
            reserved_key = _find_reserved_input_key(nested)
            if reserved_key:
                return reserved_key

    # No blocked name was found in this part of the report.
    return None


def _remove_reserved_fields(value: Any) -> Any:
    """Return a copy with reserved keys removed at every nesting level."""

    # Copy a dictionary while leaving out blocked names.
    if isinstance(value, Mapping):
        return {
            key: _remove_reserved_fields(nested)
            for key, nested in value.items()
            if not _is_reserved_input_key(key)
        }

    # Clean every item inside lists and tuples as well.
    if isinstance(value, list):
        return [_remove_reserved_fields(nested) for nested in value]
    if isinstance(value, tuple):
        return tuple(_remove_reserved_fields(nested) for nested in value)

    # Numbers, text, booleans, and null values need no changes.
    return value


def validate_report_input(
    report_id: Any,
    report: Any,
    metadata: Any,
) -> CaptainReportResult | None:
    """Validate public arguments without raising user-facing exceptions.

    Valid input returns ``None``. Invalid input returns the normal public result
    type so the caller always receives the same set of fields.
    """

    # Use a harmless ID in an error result when the supplied ID is not text.
    result_report_id = report_id if isinstance(report_id, str) else "invalid-report"

    # The ID must be short and safe to use in a command and database lookup.
    if not isinstance(report_id, str) or not REPORT_ID_PATTERN.fullmatch(report_id):
        return canonical_result(
            result_report_id,
            "failed",
            captain_feedback=(
                "report_id must contain 1-128 ASCII letters, numbers, '.', '_', or '-'."
            ),
        )

    # The report and its optional background information must be dictionaries.
    if not isinstance(report, Mapping):
        return canonical_result(
            report_id,
            "needs_clarification",
            captain_feedback="report must be an object.",
        )
    if not isinstance(metadata, Mapping):
        return canonical_result(
            report_id,
            "needs_clarification",
            captain_feedback="metadata must be an object.",
        )

    # Reports cannot choose the identity or credentials used by OpenClaw.
    for object_name, value in (("report", report), ("metadata", metadata)):
        if _find_reserved_input_key(value):
            return canonical_result(
                report_id,
                "failed",
                captain_feedback=(
                    f"{object_name} contains a reserved authentication, "
                    "authorization, identity, or claims field."
                ),
            )

    # Measure the exact number of bytes that the two dictionaries will use.
    payload_size = sum(
        len(
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        for value in (report, metadata)
    )
    if payload_size > MAX_REPORT_BYTES:
        return canonical_result(
            report_id,
            "failed",
            captain_feedback="report and metadata must be at most 1,000,000 bytes.",
        )

    # Captain needs at least one summary item to understand what changed.
    if not _summary_items(report):
        return canonical_result(
            report_id,
            "needs_clarification",
            captain_feedback="report.summary must include at least one item.",
            questions=["What changed in this session?"],
        )

    # ``None`` tells the caller that every check passed.
    return None


# Build the message sent to Captain.
def build_status_update_prompt(
    report_id: str,
    report: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> str:
    """Build the Captain prompt from already validated input.

    This helper removes reserved fields again because callers can use it
    directly without first calling ``validate_report_input``.
    """

    # Remove blocked fields, then format the report so Captain can read it.
    report_json = json.dumps(
        _remove_reserved_fields(report),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )

    # Prepare the optional background information in the same way.
    metadata_json = json.dumps(
        _remove_reserved_fields(metadata),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )

    # Combine the instructions, report, and background information into one message.
    return f"""You are Captain preparing a user-operated `/captain` status update.

Process this report with your normal PM capabilities. Use normal PM judgment to identify what changed, what is missing, who owns it, and what decision or action is needed. Audit every ClickUp write. Do not claim identity, authentication, hosted services, or actions that are not supported by the supplied evidence.

Do not invoke `/captain`; do not load or invoke the `captain` skill; and do not call `captain_session_report`, `Captain:captain_session_report`, or `captain__captain_session_report`. Those are sender-side reporting entrypoints, and invoking them here would recurse. Process the supplied report yourself and return the required JSON directly.

Return JSON only, matching this public result contract:
{{
  "report_id": "string",
  "status": "created | updated | queued | needs_clarification | needs_configuration | partial | failed | unknown_outcome",
  "clickup_updates": [{{"action": "string", "task_id": "string"}}],
  "captain_feedback": "string",
  "questions": ["string"],
  "warnings": ["string"]
}}

Report ID: {json.dumps(report_id)}
Report:
{report_json}

Metadata:
{metadata_json}
"""


# Build the OpenClaw command.
def _read_timeout_seconds(env: Mapping[str, str]) -> int:
    """Read the OpenClaw timeout and keep it within safe limits."""

    # Use the configured whole number, or the default when it is not a number.
    try:
        configured_timeout = int(
            str(env.get("CAPTAIN_AGENT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        )
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS

    # Never wait less than 30 seconds or more than one hour.
    return min(max(configured_timeout, 30), 3_600)


def build_openclaw_command(
    report_id: str,
    env: Mapping[str, str],
) -> tuple[list[str], int]:
    """Build the OpenClaw command and return its effective timeout."""

    # Read each setting, leaving it blank when the user did not set it.
    timeout_seconds = _read_timeout_seconds(env)
    openclaw_command = str(
        env.get("CAPTAIN_AGENT_OPENCLAW_COMMAND", "")
    ).strip()
    captain_agent_id = str(env.get("CAPTAIN_AGENT_ID", "")).strip()
    thinking_level = str(env.get("CAPTAIN_AGENT_THINKING", "")).strip()

    # Build the command as a list so no shell has to interpret its contents.
    # The full report will be sent separately through the process input.
    return (
        [
            openclaw_command or DEFAULT_OPENCLAW_COMMAND,
            "agent",
            "--agent",
            captain_agent_id or DEFAULT_AGENT_ID,
            "--session-id",
            f"captain-report-{report_id}",
            "--thinking",
            thinking_level or DEFAULT_THINKING,
            "--timeout",
            str(timeout_seconds),
            "--json",
            "--message-file",
            "-",
        ],
        timeout_seconds,
    )


# Run OpenClaw and collect its response.
def run_openclaw_agent(
    command: Sequence[str],
    prompt: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run one OpenClaw turn without invoking a shell.

    A start error means OpenClaw did not run. An error after the process starts
    is uncertain because Captain may already have acted on the report.
    """

    # Start OpenClaw and prepare to send input and collect its two outputs.
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
    except OSError as error:
        # Reaching this branch proves that OpenClaw never started.
        raise _ProcessStartError(error) from error

    # Send the message and wait for OpenClaw to finish.
    with process:
        try:
            stdout, stderr = process.communicate(
                prompt,
                timeout=timeout_seconds + 30,
            )
        except subprocess.TimeoutExpired as error:
            # Stop OpenClaw when it takes longer than the allowed time.
            process.kill()

            # Finish closing the process correctly on each operating system.
            if os.name == "nt":
                error.stdout, error.stderr = process.communicate()
            else:
                process.wait()
            raise
        except BaseException:
            # Also stop OpenClaw if reading its output fails or the user interrupts.
            process.kill()
            raise

        # The wait above finished, so OpenClaw must now have an exit code.
        return_code = process.poll()
        assert return_code is not None

        # Return the command, exit code, output, and error text together.
        return subprocess.CompletedProcess(
            process.args,
            return_code,
            stdout,
            stderr,
        )


# Read OpenClaw's response.
def _find_json_object(value: str) -> Mapping[str, Any] | None:
    """Find a JSON object in plain, fenced, or surrounding text."""

    decoder = json.JSONDecoder()

    # First, try reading the entire response as one JSON dictionary.
    try:
        parsed = json.loads(value.strip())
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        return parsed

    # Next, try JSON placed inside a Markdown code block.
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", value, re.IGNORECASE)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            return parsed

    # Finally, try the first JSON dictionary found inside surrounding text.
    first_object = value.find("{")
    if first_object == -1:
        return None
    try:
        parsed, _ = decoder.raw_decode(value[first_object:])
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, Mapping):
        return parsed

    # The response did not contain a usable JSON dictionary.
    return None


def _find_assistant_text(response: Mapping[str, Any]) -> str | None:
    """Find the assistant text inside an OpenClaw response."""

    # Some OpenClaw versions wrap the useful response inside ``result``.
    response_body = response
    while isinstance(response_body.get("result"), Mapping):
        response_body = response_body["result"]

    # The usual response stores assistant messages in a list named ``payloads``.
    payloads = response_body.get("payloads")
    if isinstance(payloads, list):
        for payload in payloads:
            if isinstance(payload, Mapping) and isinstance(payload.get("text"), str):
                return payload["text"]

    # Older response shapes may store the final message under ``meta``.
    response_metadata = response_body.get("meta")
    if isinstance(response_metadata, Mapping):
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            if isinstance(response_metadata.get(key), str):
                return response_metadata[key]

    # Simple response shapes may store the message directly under one name.
    for key in ("text", "reply", "output"):
        if isinstance(response_body.get(key), str):
            return response_body[key]
    # No assistant message was present in any supported location.
    return None


def _find_captain_result(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return Captain's result object from direct JSON or response text."""

    # Use the response directly when it already has Captain's required fields.
    if isinstance(response.get("status"), str) and "captain_feedback" in response:
        return response

    # Otherwise, find the assistant's message and read the JSON inside it.
    assistant_text = _find_assistant_text(response)
    if assistant_text is None:
        return None
    return _find_json_object(assistant_text)


def _read_string_list(value: Any) -> list[str] | None:
    """Return a valid string list, or None when the value is malformed."""

    # Missing optional lists mean there are no items.
    if value is None:
        return []

    # Reject values that are not a list of text items.
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None

    return value


def _read_clickup_updates(value: Any) -> list[dict[str, Any]] | None:
    """Return plain ClickUp update objects, or None when malformed."""

    # Missing updates mean Captain did not change any ClickUp tasks.
    if value is None:
        return []

    # Every update must be a dictionary.
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        return None

    # Copy each update into a normal Python dictionary.
    return [dict(item) for item in value]


def normalize_captain_agent_response(
    report_id: str,
    response: Mapping[str, Any],
) -> CaptainReportResult:
    """Convert OpenClaw output into Captain's public result shape."""

    # Find Captain's result inside the different formats OpenClaw may return.
    captain_result = _find_captain_result(response)
    if captain_result is None:
        return canonical_result(
            report_id,
            "unknown_outcome",
            captain_feedback=(
                "OpenClaw did not return a complete Captain JSON response."
            ),
        )

    # Read each field that the public result requires.
    status = captain_result.get("status")
    feedback = captain_result.get("captain_feedback")
    clickup_updates = _read_clickup_updates(captain_result.get("clickup_updates"))
    questions = _read_string_list(captain_result.get("questions"))
    warnings = _read_string_list(captain_result.get("warnings"))

    # Do not guess when a required field has the wrong kind of value.
    if (
        not isinstance(status, str)
        or not isinstance(feedback, str)
        or clickup_updates is None
        or questions is None
        or warnings is None
    ):
        return canonical_result(
            report_id,
            "unknown_outcome",
            captain_feedback="OpenClaw returned malformed Captain completion evidence.",
        )

    # Build the standard set of fields used in every result.
    return canonical_result(
        report_id,
        status,
        captain_feedback=feedback,
        clickup_updates=clickup_updates,
        questions=questions,
        warnings=warnings,
    )


def _build_unknown_outcome(
    report_id: str,
    reason: str,
    detail: Any = "",
) -> CaptainReportResult:
    """Build a short diagnostic when completion cannot be proven."""

    # Begin with the simple reason that completion is uncertain.
    message = reason

    # Add safe process details when they are available.
    if str(detail):
        safe_detail = _redact_and_shorten_text(
            detail,
            limit=1_000 - len(reason) - 2,
        )
        message = f"{reason}: {safe_detail}"

    # Uncertain work must never be reported as a success or a safe retry.
    return canonical_result(
        report_id,
        "unknown_outcome",
        captain_feedback="OpenClaw completion could not be proven.",
        warnings=[message],
    )


# Send one report to Captain.
def invoke_openclaw(
    report_id: str,
    report: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    runner: OpenClawRunner = run_openclaw_agent,
) -> CaptainReportResult:
    """Validate, send, and standardize one OpenClaw request.

    A process-start failure is safe to retry. Any failure after launch is
    uncertain because Captain may already have acted on the report.
    """

    # Stop before starting OpenClaw when the report is invalid.
    validation_result = validate_report_input(report_id, report, metadata)
    if validation_result is not None:
        return validation_result

    # Prepare the message and command for one Captain run.
    prompt = build_status_update_prompt(report_id, report, metadata)
    command, timeout_seconds = build_openclaw_command(report_id, env)

    # Run Captain and turn OpenClaw failures into clear result values.
    try:
        completed_process = runner(command, prompt, timeout_seconds)
    except subprocess.TimeoutExpired as error:
        # A timeout happened after OpenClaw started, so Captain may have acted.
        return _build_unknown_outcome(report_id, "OpenClaw timed out", error)
    except _ProcessStartError as error:
        # A start failure proves that Captain did not receive the report.
        return canonical_result(
            report_id,
            "needs_configuration",
            captain_feedback="The OpenClaw process could not start.",
            warnings=[_redact_and_shorten_text(error)],
        )
    except Exception as error:
        # Any other failure may have happened after Captain received the report.
        return _build_unknown_outcome(report_id, "OpenClaw runner failed", error)

    # When OpenClaw reports failure, Captain may still have finished the work.
    if completed_process.returncode != 0:
        error_detail = (
            completed_process.stderr
            or completed_process.stdout
            or f"exit code {completed_process.returncode}"
        )
        return _build_unknown_outcome(
            report_id,
            "OpenClaw exited unsuccessfully",
            error_detail,
        )

    # Read OpenClaw's output as JSON.
    response = _find_json_object(completed_process.stdout or "")
    if response is None:
        return _build_unknown_outcome(
            report_id,
            "OpenClaw stdout was not a JSON object",
            completed_process.stdout or completed_process.stderr,
        )

    # Convert the OpenClaw response into the plugin's public result.
    return normalize_captain_agent_response(report_id, response)


# Remember which reports were already sent.
def report_store_path(env: Mapping[str, str]) -> Path:
    """Choose the SQLite path used to prevent duplicate report processing."""

    # Use the exact path supplied by the user when one is configured.
    override = str(env.get("CAPTAIN_AGENT_STATE_PATH", "")).strip()
    if override:
        return Path(override).expanduser()

    # Otherwise, use the operating system's normal folder for saved app data.
    xdg_state_home = str(env.get("XDG_STATE_HOME", "")).strip()
    state_directory = (
        Path(xdg_state_home).expanduser()
        if xdg_state_home
        else Path.home() / ".local" / "state"
    )
    return state_directory / "captain-agent" / "reports.sqlite3"


def _initialize_store(path: Path) -> None:
    """Create the report store and its table when needed."""

    # Create a folder that only the current user can open.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)

    # Create the table on the first run. Existing tables are left unchanged.
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_reports(
                    report_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    # Keep the database readable and writable only by the current user.
    path.chmod(0o600)


def _current_utc_time() -> str:
    """Return the current UTC time in the database format."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _choose_project_name(
    report: Mapping[str, Any], metadata: Mapping[str, Any]
) -> str:
    """Choose a readable project name for the database row."""

    # Prefer the report name, then the background project or repository name.
    project_names = (
        report.get("project"),
        metadata.get("project"),
        metadata.get("repo"),
    )
    for project_name in project_names:
        cleaned_name = str(project_name).strip() if project_name is not None else ""
        if cleaned_name:
            return cleaned_name

    # Use a neutral name when the report does not name a project.
    return "Session report"


def _serialize_result(result: CaptainReportResult) -> str:
    """Convert a public result to JSON text that can be returned later."""

    # Sorting the names makes the stored text consistent between runs.
    return json.dumps(result.model_dump(mode="json"), sort_keys=True)


def _reserve_report_id(
    path: Path,
    report_id: str,
    report: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> _ReportReservation:
    """Reserve a report ID for sending, or return its saved result.

    ``should_send`` is true only for the caller that owns the reservation.
    Every other caller receives a result it can return without sending.
    """

    # Always lock memory first, then the database. This prevents two callers
    # from waiting forever for each other to release a lock.
    with _ACTIVE_REPORTS_LOCK:
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            added_active_id = False
            try:
                # Block other writers before checking whether this ID is saved.
                # This prevents two running copies from reserving the same ID.
                connection.execute("BEGIN IMMEDIATE")
                stored_report = connection.execute(
                    """
                    SELECT status, result_json
                    FROM session_reports
                    WHERE report_id = ?
                    """,
                    (report_id,),
                ).fetchone()

                # Another request inside this program is sending this report now.
                if report_id in _ACTIVE_REPORT_IDS:
                    reservation = _ReportReservation(
                        should_send=False,
                        saved_result=canonical_result(
                            report_id,
                            "queued",
                            captain_feedback="This report is already processing.",
                        ),
                    )
                # No saved row means this is the first call for the report ID.
                elif stored_report is None:
                    reservation_time = _current_utc_time()
                    connection.execute(
                        """
                        INSERT INTO session_reports(
                            report_id, project, status, result_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            report_id,
                            _choose_project_name(report, metadata),
                            "processing",
                            None,
                            reservation_time,
                            reservation_time,
                        ),
                    )
                    _ACTIVE_REPORT_IDS.add(report_id)
                    added_active_id = True
                    reservation = _ReportReservation(
                        should_send=True,
                        saved_result=None,
                    )
                # A saved database entry tells us whether to try again or reuse
                # an earlier result.
                else:
                    stored_status, stored_result_json = stored_report

                    # Problems the user can correct are safe to try again.
                    if stored_status in STATUSES_THAT_CAN_BE_RETRIED:
                        connection.execute(
                            """
                            UPDATE session_reports
                            SET project = ?, status = 'processing', result_json = NULL,
                                updated_at = ?
                            WHERE report_id = ?
                            """,
                            (
                                _choose_project_name(report, metadata),
                                _current_utc_time(),
                                report_id,
                            ),
                        )
                        _ACTIVE_REPORT_IDS.add(report_id)
                        added_active_id = True
                        reservation = _ReportReservation(
                            should_send=True,
                            saved_result=None,
                        )
                    # Return finished or uncertain work without sending it again.
                    elif (
                        stored_status in STATUSES_THAT_MUST_BE_REUSED
                        and stored_result_json
                    ):
                        reservation = _ReportReservation(
                            should_send=False,
                            saved_result=CaptainReportResult.model_validate_json(
                                stored_result_json
                            ),
                        )
                    else:
                        # A leftover ``processing`` entry means the prior program
                        # stopped before it could save Captain's result.
                        # Captain may still have acted, so sending again could
                        # duplicate ClickUp work.
                        unknown_result = canonical_result(
                            report_id,
                            "unknown_outcome",
                            captain_feedback=(
                                "A previous attempt may have completed; this report "
                                "was not re-dispatched."
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE session_reports
                            SET status = ?, result_json = ?, updated_at = ?
                            WHERE report_id = ?
                            """,
                            (
                                unknown_result.status,
                                _serialize_result(unknown_result),
                                _current_utc_time(),
                                report_id,
                            ),
                        )
                        reservation = _ReportReservation(
                            should_send=False,
                            saved_result=unknown_result,
                        )

                # Save the decision before this function releases the database lock.
                connection.commit()
            except Exception:
                # If saving fails, undo the database change and active-ID change.
                connection.rollback()
                if added_active_id:
                    _ACTIVE_REPORT_IDS.discard(report_id)
                raise

            # Tell the caller whether to send or return a saved result.
            return reservation


def _save_report_result(path: Path, result: CaptainReportResult) -> None:
    """Replace a processing row with Captain's final public result."""

    # Replace the temporary ``processing`` values with Captain's actual result.
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                """
                UPDATE session_reports
                SET status = ?, result_json = ?, updated_at = ?
                WHERE report_id = ?
                """,
                (
                    result.status,
                    _serialize_result(result),
                    _current_utc_time(),
                    result.report_id,
                ),
            )


# Run the complete reporting process.
def handle_session_report(
    report_id: Any,
    report: Any,
    metadata: Any,
    *,
    env: Mapping[str, str] | None = None,
    runner: OpenClawRunner = run_openclaw_agent,
) -> CaptainReportResult:
    """Run the complete reporting flow for one session report.

    This is the function called when another coding agent submits a report. It
    checks the report, sends it at most once, and remembers Captain's result.
    """

    # Use test settings when supplied; otherwise use this program's real settings.
    environment = os.environ if env is None else env

    # Step 1: Check the report before creating files or starting OpenClaw.
    validation_result = validate_report_input(report_id, report, metadata)
    if validation_result is not None:
        return validation_result

    # Step 2: Open the small database that remembers earlier report IDs.
    store_path = report_store_path(environment)
    _initialize_store(store_path)

    # Step 3: Reserve this ID, or return the result saved by an earlier call.
    reservation = _reserve_report_id(store_path, report_id, report, metadata)
    if not reservation.should_send:
        assert reservation.saved_result is not None
        return reservation.saved_result

    # Step 4: Send the report to Captain and save the returned result.
    try:
        result = invoke_openclaw(
            report_id,
            report,
            metadata,
            env=environment,
            runner=runner,
        )
        _save_report_result(store_path, result)
        return result
    finally:
        # Step 5: Remove this ID from the list of reports running right now.
        with _ACTIVE_REPORTS_LOCK:
            _ACTIVE_REPORT_IDS.discard(report_id)
