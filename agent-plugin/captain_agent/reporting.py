"""Send a coding-session report to Captain and safely replay its result.

The main flow is: validate the input, build an OpenClaw request, run one
Captain turn, normalize the response, and save the result. Saving the result
keeps the same report ID from causing the same work twice.
"""

# Imports
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


# Public result values
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

# Input limits and OpenClaw defaults
ALLOWED_STATUSES = set(ReportStatus.__args__)
REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_REPORT_BYTES = 1_000_000
DEFAULT_OPENCLAW_COMMAND = "openclaw"
DEFAULT_AGENT_ID = "captain"
DEFAULT_THINKING = "high"
DEFAULT_TIMEOUT_SECONDS = 300
Runner = Callable[[Sequence[str], str, int], subprocess.CompletedProcess[str]]

# Track reports already running in this Python process.
_ACTIVE_REPORT_IDS: set[str] = set()
_ACTIVE_REPORTS_LOCK = threading.Lock()

# These results can be tried again after the user corrects the input or setup.
RETRYABLE_STORED_STATUSES = {
    "failed",
    "needs_clarification",
    "needs_configuration",
    "queued",
}

# Replay these results instead of sending the same report again.
IMMUTABLE_STORED_STATUSES = {
    "created",
    "partial",
    "unknown_outcome",
    "updated",
}

# Reports describe work. They must not supply identity or authentication data.
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


# Safe diagnostics
class _ProcessStartError(Exception):
    """Raised when OpenClaw could not be started."""


def _redact_external_text(value: str) -> str:
    """Remove common credential forms from text returned to the caller."""

    # Redact bearer tokens.
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [redacted]",
        value,
    )

    # Redact credentials embedded in URLs.
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@/\s]+@",
        r"\1[redacted]@",
        text,
    )

    # Redact common secret key-value pairs.
    return re.sub(
        r"(?i)\b(access[_-]?token|api[_-]?key|authorization|auth|password|secret|token)"
        r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1\2[redacted]",
        text,
    )


def _bounded_external_text(value: Any, *, limit: int = 1_000) -> str:
    """Make external text safe and short enough for a public result."""

    # Redact.
    text = _redact_external_text(str(value))

    # Truncate.
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


# Public result
# Keep every MCP response in this one stable shape.
# Do not add a class docstring: Pydantic would publish it in the JSON Schema.
class CaptainReportResult(BaseModel):
    report_id: str
    status: ReportStatus
    clickup_updates: list[dict[str, Any]] = Field(default_factory=list)
    captain_feedback: str
    questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def canonical_result(
    report_id: str,
    status: str,
    *,
    captain_feedback: str,
    clickup_updates: list[dict[str, Any]] | None = None,
    questions: list[str] | None = None,
    warnings: list[str] | None = None,
) -> CaptainReportResult:
    """Build the result shape returned by every code path."""

    # Copy warnings so this function never changes the caller's list.
    active_warnings = list(warnings or [])

    # Treat an unexpected status as uncertain instead of guessing what happened.
    if status not in ALLOWED_STATUSES:
        safe_status = _bounded_external_text(status, limit=200)
        active_warnings.append(f"Unexpected Captain status: {safe_status}")
        status = "unknown_outcome"

    # Build the validated public response.
    return CaptainReportResult(
        report_id=report_id,
        status=status,
        clickup_updates=clickup_updates or [],
        captain_feedback=captain_feedback,
        questions=questions or [],
        warnings=active_warnings,
    )


# Input validation
def _summary_lines(report: Mapping[str, Any]) -> list[str]:
    """Return the report summary as a clean list of non-empty lines."""

    # Accept a list of summary items.
    summary = report.get("summary")
    if isinstance(summary, list):
        return [str(item).strip() for item in summary if str(item).strip()]

    # Also accept one summary string.
    if isinstance(summary, str) and summary.strip():
        return [summary.strip()]

    # Any other value means the report has no usable summary.
    return []


def _is_reserved_input_key(key: Any) -> bool:
    """Return whether a key names identity or authentication data."""

    # JSON object keys should be strings, but nested Python input may not be.
    if not isinstance(key, str):
        return False

    # Normalize camelCase, spaces, and punctuation to lower snake_case.
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")

    # Match exact names and common identity/authentication name patterns.
    return (
        normalized in RESERVED_INPUT_KEYS
        or normalized.startswith("authenticated_")
        or normalized.startswith("authentication_")
        or normalized.startswith("authorization_")
        or normalized.startswith("auth_")
        or normalized.startswith("identity_")
        or normalized.endswith("_claims")
    )


def _reserved_input_key(value: Any) -> str | None:
    """Find the first reserved key anywhere inside a nested value."""

    # Search object keys, then recurse into their values.
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _is_reserved_input_key(key):
                return str(key)
            found = _reserved_input_key(nested)
            if found:
                return found

    # Search each item in a list or tuple.
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _reserved_input_key(nested)
            if found:
                return found

    # No reserved key was found in this branch.
    return None


def _strip_reserved_input(value: Any) -> Any:
    """Return a copy with reserved keys removed at every nesting level."""

    # Copy an object while dropping reserved keys.
    if isinstance(value, Mapping):
        return {
            key: _strip_reserved_input(nested)
            for key, nested in value.items()
            if not _is_reserved_input_key(key)
        }

    # Preserve list and tuple shapes while cleaning their items.
    if isinstance(value, list):
        return [_strip_reserved_input(nested) for nested in value]
    if isinstance(value, tuple):
        return tuple(_strip_reserved_input(nested) for nested in value)

    # Primitive values are already safe to copy as-is.
    return value


def validate_report_input(
    report_id: Any,
    report: Any,
    metadata: Any,
) -> CaptainReportResult | None:
    """Validate public arguments without raising user-facing exceptions.

    A valid payload returns ``None``. Invalid JSON-compatible MCP input returns
    the structured result type so callers receive predictable guidance.
    """

    # Keep validation errors renderable even when report_id is not a string.
    safe_id = report_id if isinstance(report_id, str) else "invalid-report"

    # Validate the stable report ID.
    if not isinstance(report_id, str) or not REPORT_ID_PATTERN.fullmatch(report_id):
        return canonical_result(
            safe_id,
            "failed",
            captain_feedback=(
                "report_id must contain 1-128 ASCII letters, numbers, '.', '_', or '-'."
            ),
        )

    # Validate the two top-level objects.
    if not isinstance(report, Mapping):
        return canonical_result(
            report_id, "needs_clarification", captain_feedback="report must be an object."
        )
    if not isinstance(metadata, Mapping):
        return canonical_result(
            report_id,
            "needs_clarification",
            captain_feedback="metadata must be an object.",
        )

    # Reject reserved fields anywhere inside either object.
    for object_name, value in (("report", report), ("metadata", metadata)):
        if _reserved_input_key(value):
            return canonical_result(
                report_id,
                "failed",
                captain_feedback=(
                    f"{object_name} contains a reserved authentication, "
                    "authorization, identity, or claims field."
                ),
            )

    # Measure the report and metadata as compact UTF-8 JSON.
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

    # Require enough information for Captain to understand what changed.
    if not _summary_lines(report):
        return canonical_result(
            report_id,
            "needs_clarification",
            captain_feedback="report.summary must include at least one item.",
            questions=["What changed in this session?"],
        )

    # None means the input passed every check.
    return None


# Prompt and command
def build_status_update_prompt(
    report_id: str,
    report: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> str:
    """Build the Captain prompt from already validated input.

    This helper removes reserved fields again because callers can use it
    directly without first calling ``validate_report_input``.
    """

    # Clean and format the report.
    report_json = json.dumps(
        _strip_reserved_input(report),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )

    # Clean and format the optional context.
    metadata_json = json.dumps(
        _strip_reserved_input(metadata),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )

    # Tell Captain what to do and exactly which JSON shape to return.
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


def _timeout_seconds(env: Mapping[str, str]) -> int:
    """Read the OpenClaw timeout and keep it within safe limits."""

    # Use the configured whole number, or the default when it is invalid.
    try:
        value = int(str(env.get("CAPTAIN_AGENT_TIMEOUT_SECONDS", "300")))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS

    # Allow 30 seconds through one hour.
    return min(max(value, 30), 3_600)


def build_openclaw_command(
    report_id: str,
    env: Mapping[str, str],
) -> tuple[list[str], int]:
    """Build the OpenClaw command and return its effective timeout."""

    # Read the timeout and optional command overrides.
    timeout = _timeout_seconds(env)
    command = str(env.get("CAPTAIN_AGENT_OPENCLAW_COMMAND", "")).strip()
    agent_id = str(env.get("CAPTAIN_AGENT_ID", "")).strip()
    thinking = str(env.get("CAPTAIN_AGENT_THINKING", "")).strip()

    # Pass the prompt through standard input instead of a shell argument.
    return (
        [
            command or DEFAULT_OPENCLAW_COMMAND,
            "agent",
            "--agent",
            agent_id or DEFAULT_AGENT_ID,
            "--session-id",
            f"captain-report-{report_id}",
            "--thinking",
            thinking or DEFAULT_THINKING,
            "--timeout",
            str(timeout),
            "--json",
            "--message-file",
            "-",
        ],
        timeout,
    )


# OpenClaw process
def run_openclaw_agent(
    command: Sequence[str],
    prompt: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run one OpenClaw turn without invoking a shell.

    A start error means OpenClaw did not run. An error after the process starts
    is uncertain because Captain may already have acted on the report.
    """

    # Start OpenClaw and connect its input and output streams.
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
        raise _ProcessStartError(error) from error

    # Close the process streams when this block ends.
    with process:
        try:
            # Send the prompt and wait for OpenClaw's complete response.
            stdout, stderr = process.communicate(
                prompt,
                timeout=timeout_seconds + 30,
            )
        except subprocess.TimeoutExpired as error:
            # Stop a timed-out child process.
            process.kill()

            # Finish reading pipes on Windows, or reap the process on POSIX.
            if os.name == "nt":
                error.stdout, error.stderr = process.communicate()
            else:
                process.wait()
            raise
        except BaseException:
            # Stop the child on an I/O error or interrupt.
            process.kill()
            raise

        # Return the same output shape as subprocess.run.
        return subprocess.CompletedProcess(
            process.args,
            process.poll(),
            stdout,
            stderr,
        )


# OpenClaw response parsing
def _json_object_from_text(value: str) -> Mapping[str, Any] | None:
    """Find a JSON object in plain, fenced, or surrounding text."""

    decoder = json.JSONDecoder()

    # Try the entire string as plain JSON.
    try:
        parsed = json.loads(value.strip())
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        return parsed

    # Try JSON inside a Markdown code fence.
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", value, re.IGNORECASE)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            return parsed

    # Try the first JSON object inside surrounding prose.
    first_object = value.find("{")
    if first_object == -1:
        return None
    try:
        parsed, _ = decoder.raw_decode(value[first_object:])
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, Mapping):
        return parsed

    # Parsed JSON of another type is not a Captain response object.
    return None


def _response_text(response: Mapping[str, Any]) -> str | None:
    """Find the assistant text inside an OpenClaw response."""

    # Remove any nested OpenClaw result wrappers.
    current = response
    while isinstance(current.get("result"), Mapping):
        current = current["result"]

    # Prefer the normal payload text.
    payloads = current.get("payloads")
    if isinstance(payloads, list):
        for payload in payloads:
            if isinstance(payload, Mapping) and isinstance(payload.get("text"), str):
                return payload["text"]

    # Fall back to assistant text stored in metadata.
    meta = current.get("meta")
    if isinstance(meta, Mapping):
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            if isinstance(meta.get(key), str):
                return meta[key]

    # Accept simpler response shapes last.
    for key in ("text", "reply", "output"):
        if isinstance(current.get(key), str):
            return current[key]
    return None


def _captain_response_object(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return Captain's result object from direct JSON or response text."""

    # Accept a response that already matches Captain's top-level shape.
    if isinstance(response.get("status"), str) and "captain_feedback" in response:
        return response

    # Otherwise find the assistant text and parse the JSON inside it.
    response_text = _response_text(response)
    if response_text is None:
        return None
    return _json_object_from_text(response_text)


def _string_list(value: Any) -> list[str] | None:
    """Return a valid string list, or None when the value is malformed."""

    # Missing optional lists become empty lists.
    if value is None:
        return []

    # Reject non-lists and lists containing non-string values.
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None

    # The value already has the expected shape.
    return value


def _clickup_updates(value: Any) -> list[dict[str, Any]] | None:
    """Return plain ClickUp update objects, or None when malformed."""

    # Missing optional updates become an empty list.
    if value is None:
        return []

    # Every update must be an object.
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        return None

    # Copy generic mappings into normal dictionaries.
    return [dict(item) for item in value]


def normalize_captain_agent_response(
    report_id: str,
    response: Mapping[str, Any],
) -> CaptainReportResult:
    """Convert OpenClaw output into Captain's public result shape."""

    # Find Captain's JSON object inside the OpenClaw response.
    captain_response = _captain_response_object(response)
    if captain_response is None:
        return canonical_result(
            report_id,
            "unknown_outcome",
            captain_feedback="OpenClaw did not return a complete Captain JSON response.",
        )

    # Read and normalize every required result field.
    status = captain_response.get("status")
    feedback = captain_response.get("captain_feedback")
    updates = _clickup_updates(captain_response.get("clickup_updates"))
    questions = _string_list(captain_response.get("questions"))
    warnings = _string_list(captain_response.get("warnings"))

    # Return uncertainty when the response is incomplete or malformed.
    if (
        not isinstance(status, str)
        or not isinstance(feedback, str)
        or updates is None
        or questions is None
        or warnings is None
    ):
        return canonical_result(
            report_id,
            "unknown_outcome",
            captain_feedback="OpenClaw returned malformed Captain completion evidence.",
        )

    # Return the validated Captain result.
    return canonical_result(
        report_id,
        status,
        captain_feedback=feedback,
        clickup_updates=updates,
        questions=questions,
        warnings=warnings,
    )


def _unknown_outcome(
    report_id: str,
    reason: str,
    detail: Any = "",
) -> CaptainReportResult:
    """Build a short diagnostic when completion cannot be proven."""

    # Start with the plain reason.
    message = reason

    # Add safe, bounded process details when available.
    if str(detail):
        safe_detail = _bounded_external_text(
            detail,
            limit=1_000 - len(reason) - 2,
        )
        message = f"{reason}: {safe_detail}"

    # Never turn incomplete evidence into a success or retryable result.
    return canonical_result(
        report_id,
        "unknown_outcome",
        captain_feedback="OpenClaw completion could not be proven.",
        warnings=[message],
    )


def invoke_openclaw(
    report_id: str,
    report: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    runner: Runner = run_openclaw_agent,
) -> CaptainReportResult:
    """Validate, send, and normalize one OpenClaw request.

    A process-start failure is safe to retry. Any failure after launch is
    uncertain because Captain may already have acted on the report.
    """

    # Validate before starting another process.
    validation_result = validate_report_input(report_id, report, metadata)
    if validation_result is not None:
        return validation_result

    # Build the prompt and CLI command.
    prompt = build_status_update_prompt(report_id, report, metadata)
    command, timeout = build_openclaw_command(report_id, env)

    # Run one Captain turn.
    try:
        completed = runner(command, prompt, timeout)
    except subprocess.TimeoutExpired as error:
        # A timeout happened after launch, so the outcome is uncertain.
        return _unknown_outcome(report_id, "OpenClaw timed out", error)
    except _ProcessStartError as error:
        # A start failure is a setup problem; Captain did not receive the report.
        return canonical_result(
            report_id,
            "needs_configuration",
            captain_feedback="The OpenClaw process could not start.",
            warnings=[_bounded_external_text(error)],
        )
    except Exception as error:
        # Any other runner failure may have happened after Captain received it.
        return _unknown_outcome(report_id, "OpenClaw runner failed", error)

    # A nonzero exit cannot prove whether Captain completed the work.
    if completed.returncode != 0:
        detail = completed.stderr or completed.stdout or f"exit code {completed.returncode}"
        return _unknown_outcome(report_id, "OpenClaw exited unsuccessfully", detail)

    # Parse OpenClaw's JSON response.
    response = _json_object_from_text(completed.stdout or "")
    if response is None:
        return _unknown_outcome(
            report_id,
            "OpenClaw stdout was not a JSON object",
            completed.stdout or completed.stderr,
        )

    # Convert the response into the public result shape.
    return normalize_captain_agent_response(report_id, response)


# Replay database
def state_path(env: Mapping[str, str]) -> Path:
    """Choose the SQLite replay-database path."""

    # Prefer the explicit Captain path.
    override = str(env.get("CAPTAIN_AGENT_STATE_PATH", "")).strip()
    if override:
        return Path(override).expanduser()

    # Otherwise follow XDG_STATE_HOME, then the normal home-directory default.
    root = str(env.get("XDG_STATE_HOME", "")).strip()
    base = Path(root).expanduser() if root else Path.home() / ".local" / "state"
    return base / "captain-agent" / "reports.sqlite3"


def _initialize_store(path: Path) -> None:
    """Create the replay database and its table when needed."""

    # Create a user-only parent directory.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)

    # Create the report table without changing an existing table.
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

    # Keep the database readable and writable only by its user.
    path.chmod(0o600)


def _now() -> str:
    """Return the current UTC time in the database format."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stored_project(
    report: Mapping[str, Any], metadata: Mapping[str, Any]
) -> str:
    """Choose a readable project name for the database row."""

    # Prefer the report project, then metadata project or repository.
    candidates = (
        report.get("project"),
        metadata.get("project"),
        metadata.get("repo"),
    )
    for value in candidates:
        project = str(value).strip() if value is not None else ""
        if project:
            return project

    # Use a neutral label when the report does not name a project.
    return "Session report"


def _result_json(result: CaptainReportResult) -> str:
    """Convert a public result to stable JSON for later replay."""

    return json.dumps(result.model_dump(mode="json"), sort_keys=True)


def _claim_report(
    path: Path,
    report_id: str,
    report: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> tuple[bool, CaptainReportResult | None]:
    """Claim a report ID, or return the result already saved for it.

    ``True`` means this caller should send the report. ``False`` means the
    caller should return the second value without sending anything.
    """

    # Lock in one order: this process first, then SQLite.
    with _ACTIVE_REPORTS_LOCK:
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            added_active_id = False
            try:
                # Start a write transaction before reading the saved row.
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT status, result_json
                    FROM session_reports
                    WHERE report_id = ?
                    """,
                    (report_id,),
                ).fetchone()

                # Another thread in this process is already sending the report.
                if report_id in _ACTIVE_REPORT_IDS:
                    outcome = (
                        False,
                        canonical_result(
                            report_id,
                            "queued",
                            captain_feedback="This report is already processing.",
                        ),
                    )
                elif row is None:
                    # First call: create a processing row and claim the ID.
                    now = _now()
                    connection.execute(
                        """
                        INSERT INTO session_reports(
                            report_id, project, status, result_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            report_id,
                            _stored_project(report, metadata),
                            "processing",
                            None,
                            now,
                            now,
                        ),
                    )
                    _ACTIVE_REPORT_IDS.add(report_id)
                    added_active_id = True
                    outcome = (True, None)
                else:
                    # A previous call already created this report ID.
                    stored_status, stored_result = row
                    if stored_status in RETRYABLE_STORED_STATUSES:
                        # Retryable result: reset its row and claim the ID again.
                        connection.execute(
                            """
                            UPDATE session_reports
                            SET project = ?, status = 'processing', result_json = NULL,
                                updated_at = ?
                            WHERE report_id = ?
                            """,
                            (_stored_project(report, metadata), _now(), report_id),
                        )
                        _ACTIVE_REPORT_IDS.add(report_id)
                        added_active_id = True
                        outcome = (True, None)
                    elif stored_status in IMMUTABLE_STORED_STATUSES and stored_result:
                        # Final or uncertain result: replay the exact saved result.
                        outcome = (
                            False,
                            CaptainReportResult.model_validate_json(stored_result),
                        )
                    else:
                        # Orphaned processing row: do not risk sending it twice.
                        result = canonical_result(
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
                            (result.status, _result_json(result), _now(), report_id),
                        )
                        outcome = (False, result)

                # Save the claim decision.
                connection.commit()
            except Exception:
                # Undo database and in-memory changes when claiming fails.
                connection.rollback()
                if added_active_id:
                    _ACTIVE_REPORT_IDS.discard(report_id)
                raise

            # Tell the caller whether to send or replay.
            return outcome


def _finish_report(path: Path, result: CaptainReportResult) -> None:
    """Save the final public result in the claimed database row."""

    # Replace the temporary processing state with the returned result.
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                """
                UPDATE session_reports
                SET status = ?, result_json = ?, updated_at = ?
                WHERE report_id = ?
                """,
                (result.status, _result_json(result), _now(), result.report_id),
            )


# Public reporting flow
def handle_session_report(
    report_id: Any,
    report: Any,
    metadata: Any,
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner = run_openclaw_agent,
) -> CaptainReportResult:
    """Run the complete reporting flow for one session report.

    This is the MCP server's entrypoint. It validates the report, claims its
    ID, sends at most once, saves the result, and releases the ID.
    """

    # Use a supplied environment, or the current process environment.
    active_env = os.environ if env is None else env

    # Validate before touching the database.
    validation = validate_report_input(report_id, report, metadata)
    if validation is not None:
        return validation

    # Open the replay store.
    path = state_path(active_env)
    _initialize_store(path)

    # Claim this ID for sending, or load its existing result.
    claimed, existing = _claim_report(path, report_id, report, metadata)
    if not claimed:
        assert existing is not None
        return existing

    # Send, save, and return the new result.
    try:
        result = invoke_openclaw(
            report_id, report, metadata, env=active_env, runner=runner
        )
        _finish_report(path, result)
        return result
    finally:
        # Always release the process-local claim.
        with _ACTIVE_REPORTS_LOCK:
            _ACTIVE_REPORT_IDS.discard(report_id)
