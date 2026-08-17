"""Validate, dispatch, and replay local Captain session reports.

The public MCP tool calls :func:`handle_session_report`. This module rejects or
strips reserved authentication, authorization, identity, and claims fields,
runs one local OpenClaw turn, normalizes its reply, and stores enough local
state to make retries idempotent.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

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

# The ReportStatus values above form part of the public result contract. The
# constants below control validation, dispatch, and replay persistence.
ALLOWED_STATUSES = set(ReportStatus.__args__)
REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_REPORT_BYTES = 1_000_000
DEFAULT_OPENCLAW_COMMAND = "openclaw"
DEFAULT_AGENT_ID = "captain"
DEFAULT_THINKING = "high"
DEFAULT_TIMEOUT_SECONDS = 300
Runner = Callable[[Sequence[str], str, int], subprocess.CompletedProcess[str]]

# SQLite persists replay state across processes. This in-memory set prevents
# concurrent calls inside one MCP process from reclaiming the same report.
_ACTIVE_REPORT_IDS: set[str] = set()
_ACTIVE_REPORTS_LOCK = threading.Lock()

# Correctable results may be dispatched again with the same report ID. Proven
# or uncertain outcomes are replayed as-is to avoid duplicate external writes.
RETRYABLE_STORED_STATUSES = {
    "failed",
    "needs_clarification",
    "needs_configuration",
    "queued",
}
IMMUTABLE_STORED_STATUSES = {
    "created",
    "partial",
    "unknown_outcome",
    "updated",
}
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


class _ProcessStartError(Exception):
    """The real subprocess adapter could not start OpenClaw."""


def _redact_external_text(value: str) -> str:
    """Remove common credential forms from untrusted diagnostics."""
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [redacted]",
        value,
    )
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@/\s]+@",
        r"\1[redacted]@",
        text,
    )
    return re.sub(
        r"(?i)\b(access[_-]?token|api[_-]?key|authorization|auth|password|secret|token)"
        r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1\2[redacted]",
        text,
    )


def _bounded_external_text(value: Any, *, limit: int = 1_000) -> str:
    """Redact untrusted text and cap its length for public diagnostics."""

    text = _redact_external_text(str(value))
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


# This model is the stable result returned by the public MCP tool. A comment is
# used instead of a class docstring because Pydantic exports docstrings in JSON
# Schema, which would silently change the public MCP output schema.
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
    """Build a public result and downgrade unknown statuses to uncertainty."""

    active_warnings = list(warnings or [])
    if status not in ALLOWED_STATUSES:
        safe_status = _bounded_external_text(status, limit=200)
        active_warnings.append(f"Unexpected Captain status: {safe_status}")
        status = "unknown_outcome"
    return CaptainReportResult(
        report_id=report_id,
        status=status,
        clickup_updates=clickup_updates or [],
        captain_feedback=captain_feedback,
        questions=questions or [],
        warnings=active_warnings,
    )


def _summary_lines(report: Mapping[str, Any]) -> list[str]:
    """Return non-empty summary items in a consistent list form."""

    summary = report.get("summary")
    if isinstance(summary, list):
        return [str(item).strip() for item in summary if str(item).strip()]
    if isinstance(summary, str) and summary.strip():
        return [summary.strip()]
    return []


def _is_reserved_input_key(key: Any) -> bool:
    """Recognize authentication and identity keys in common spellings."""

    if not isinstance(key, str):
        return False
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")
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
    """Find the first reserved key anywhere in a nested input value."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _is_reserved_input_key(key):
                return str(key)
            found = _reserved_input_key(nested)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _reserved_input_key(nested)
            if found:
                return found
    return None


def _strip_reserved_input(value: Any) -> Any:
    """Remove reserved keys recursively as a prompt-safety backstop."""

    if isinstance(value, Mapping):
        return {
            key: _strip_reserved_input(nested)
            for key, nested in value.items()
            if not _is_reserved_input_key(key)
        }
    if isinstance(value, list):
        return [_strip_reserved_input(nested) for nested in value]
    if isinstance(value, tuple):
        return tuple(_strip_reserved_input(nested) for nested in value)
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

    safe_id = report_id if isinstance(report_id, str) else "invalid-report"
    if not isinstance(report_id, str) or not REPORT_ID_PATTERN.fullmatch(report_id):
        return canonical_result(
            safe_id,
            "failed",
            captain_feedback=(
                "report_id must contain 1-128 ASCII letters, numbers, '.', '_', or '-'."
            ),
        )
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
    if not _summary_lines(report):
        return canonical_result(
            report_id,
            "needs_clarification",
            captain_feedback="report.summary must include at least one item.",
            questions=["What changed in this session?"],
        )
    return None


def build_status_update_prompt(
    report_id: str,
    report: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> str:
    """Build the terminal Captain prompt from already validated input.

    Reserved fields are stripped again here because this helper can also be
    called directly. This defense-in-depth step keeps claims and credentials
    out of the subprocess input even if a future caller skips validation.
    """

    report_json = json.dumps(
        _strip_reserved_input(report),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    metadata_json = json.dumps(
        _strip_reserved_input(metadata),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return f"""You are Captain preparing a local `/captain` status update for a user-operated workspace.

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
    """Read and clamp the user-configurable OpenClaw timeout."""

    try:
        value = int(str(env.get("CAPTAIN_AGENT_TIMEOUT_SECONDS", "300")))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return min(max(value, 30), 3_600)


def build_openclaw_command(
    report_id: str,
    env: Mapping[str, str],
) -> tuple[list[str], int]:
    """Build the local OpenClaw command and return its effective timeout."""

    timeout = _timeout_seconds(env)
    command = str(env.get("CAPTAIN_AGENT_OPENCLAW_COMMAND", "")).strip()
    agent_id = str(env.get("CAPTAIN_AGENT_ID", "")).strip()
    thinking = str(env.get("CAPTAIN_AGENT_THINKING", "")).strip()
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


def run_openclaw_agent(
    command: Sequence[str],
    prompt: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run one local OpenClaw turn without invoking a shell.

    Process construction is deliberately separate from communication. A start
    error proves that nothing ran, while an error after ``Popen`` succeeds has
    an uncertain outcome and must not be advertised as safely retryable.
    """

    try:
        # Catch only construction failures as known configuration problems.
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

    with process:
        try:
            stdout, stderr = process.communicate(
                prompt,
                timeout=timeout_seconds + 30,
            )
        except subprocess.TimeoutExpired as error:
            process.kill()

            # Preserve the adapter's platform-specific cleanup: Windows drains
            # pipe-reader threads, while POSIX reaps the terminated child.
            if os.name == "nt":
                error.stdout, error.stderr = process.communicate()
            else:
                process.wait()
            raise
        except BaseException:
            # Do not leave a child process behind on I/O errors or interrupts.
            process.kill()
            raise

        return subprocess.CompletedProcess(
            process.args,
            process.poll(),
            stdout,
            stderr,
        )


def _json_object_from_text(value: str) -> Mapping[str, Any] | None:
    """Extract a direct, fenced, or fallback JSON object in that order."""
    decoder = json.JSONDecoder()
    try:
        parsed = json.loads(value.strip())
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        return parsed

    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", value, re.IGNORECASE)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            return parsed

    first_object = value.find("{")
    if first_object == -1:
        return None
    try:
        parsed, _ = decoder.raw_decode(value[first_object:])
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, Mapping):
        return parsed
    return None


def _response_text(response: Mapping[str, Any]) -> str | None:
    """Find assistant result text after unwrapping OpenClaw envelopes."""

    current = response
    while isinstance(current.get("result"), Mapping):
        current = current["result"]

    payloads = current.get("payloads")
    if isinstance(payloads, list):
        for payload in payloads:
            if isinstance(payload, Mapping) and isinstance(payload.get("text"), str):
                return payload["text"]

    meta = current.get("meta")
    if isinstance(meta, Mapping):
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            if isinstance(meta.get(key), str):
                return meta[key]

    for key in ("text", "reply", "output"):
        if isinstance(current.get(key), str):
            return current[key]
    return None


def _captain_response_object(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return a direct Captain object or parse one from response text."""

    if isinstance(response.get("status"), str) and "captain_feedback" in response:
        return response
    response_text = _response_text(response)
    if response_text is None:
        return None
    return _json_object_from_text(response_text)


def _string_list(value: Any) -> list[str] | None:
    """Normalize an optional string list, rejecting malformed values."""

    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _clickup_updates(value: Any) -> list[dict[str, Any]] | None:
    """Normalize optional ClickUp update objects into plain dictionaries."""

    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        return None
    return [dict(item) for item in value]


def normalize_captain_agent_response(
    report_id: str,
    response: Mapping[str, Any],
) -> CaptainReportResult:
    """Convert OpenClaw output into the conservative public result contract."""

    captain_response = _captain_response_object(response)
    if captain_response is None:
        return canonical_result(
            report_id,
            "unknown_outcome",
            captain_feedback="OpenClaw did not return a complete Captain JSON response.",
        )

    status = captain_response.get("status")
    feedback = captain_response.get("captain_feedback")
    updates = _clickup_updates(captain_response.get("clickup_updates"))
    questions = _string_list(captain_response.get("questions"))
    warnings = _string_list(captain_response.get("warnings"))
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
    """Return bounded diagnostics when completion cannot be proven."""

    message = reason
    if str(detail):
        # Keep the entire warning compact while safely preserving CLI evidence.
        safe_detail = _bounded_external_text(
            detail,
            limit=1_000 - len(reason) - 2,
        )
        message = f"{reason}: {safe_detail}"
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
    """Validate, dispatch, and normalize one local OpenClaw request.

    Only a process-start failure is known to be safe to retry immediately.
    Every failure after launch becomes ``unknown_outcome`` because Captain may
    already have performed an external write.
    """

    validation_result = validate_report_input(report_id, report, metadata)
    if validation_result is not None:
        return validation_result

    prompt = build_status_update_prompt(report_id, report, metadata)
    command, timeout = build_openclaw_command(report_id, env)
    try:
        completed = runner(command, prompt, timeout)
    except subprocess.TimeoutExpired as error:
        return _unknown_outcome(report_id, "OpenClaw timed out", error)
    except _ProcessStartError as error:
        return canonical_result(
            report_id,
            "needs_configuration",
            captain_feedback="The OpenClaw process could not start.",
            warnings=[_bounded_external_text(error)],
        )
    except Exception as error:
        return _unknown_outcome(report_id, "OpenClaw runner failed", error)

    if completed.returncode != 0:
        detail = completed.stderr or completed.stdout or f"exit code {completed.returncode}"
        return _unknown_outcome(report_id, "OpenClaw exited unsuccessfully", detail)

    response = _json_object_from_text(completed.stdout or "")
    if response is None:
        return _unknown_outcome(
            report_id,
            "OpenClaw stdout was not a JSON object",
            completed.stdout or completed.stderr,
        )
    return normalize_captain_agent_response(report_id, response)


def state_path(env: Mapping[str, str]) -> Path:
    """Resolve the local SQLite path from an override or the XDG default."""

    override = str(env.get("CAPTAIN_AGENT_STATE_PATH", "")).strip()
    if override:
        return Path(override).expanduser()
    root = str(env.get("XDG_STATE_HOME", "")).strip()
    base = Path(root).expanduser() if root else Path.home() / ".local" / "state"
    return base / "captain-agent" / "reports.sqlite3"


def _initialize_store(path: Path) -> None:
    """Create the private local replay database when it does not exist."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
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
    path.chmod(0o600)


def _now() -> str:
    """Return a UTC timestamp in the format stored by the replay database."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stored_project(
    report: Mapping[str, Any], metadata: Mapping[str, Any]
) -> str:
    """Choose a readable project label from the supplied report context."""

    candidates = (
        report.get("project"),
        metadata.get("project"),
        metadata.get("repo"),
    )
    for value in candidates:
        project = str(value).strip() if value is not None else ""
        if project:
            return project
    return "Session report"


def _result_json(result: CaptainReportResult) -> str:
    """Serialize a public result for deterministic SQLite replay."""

    return json.dumps(result.model_dump(mode="json"), sort_keys=True)


def _claim_report(
    path: Path,
    report_id: str,
    report: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> tuple[bool, CaptainReportResult | None]:
    """Claim a report for dispatch or return its safe existing result.

    The boolean is ``True`` only when the caller owns the next OpenClaw turn.
    Otherwise the second value contains a queued, replayed, or uncertain
    result that can be returned without dispatching.
    """

    # Keep the process lock outside SQLite so every caller takes locks in the
    # same order. This prevents two local threads from deadlocking each other.
    with _ACTIVE_REPORTS_LOCK:
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            added_active_id = False
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT status, result_json
                    FROM session_reports
                    WHERE report_id = ?
                    """,
                    (report_id,),
                ).fetchone()

                if report_id in _ACTIVE_REPORT_IDS:
                    # Process memory wins over stored retryability. The first
                    # caller is still responsible for finishing this report.
                    outcome = (
                        False,
                        canonical_result(
                            report_id,
                            "queued",
                            captain_feedback="This report is already processing.",
                        ),
                    )
                elif row is None:
                    # Create the row and marker before commit and dispatch so
                    # another caller in this process sees this report as owned.
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
                    stored_status, stored_result = row
                    if stored_status in RETRYABLE_STORED_STATUSES:
                        # Corrected input reuses the stable report ID and row.
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
                        # Success, partial success, and uncertainty are replayed
                        # verbatim so the same ID cannot duplicate a write.
                        outcome = (
                            False,
                            CaptainReportResult.model_validate_json(stored_result),
                        )
                    else:
                        # A processing row without this process's active marker
                        # may belong to an interrupted earlier attempt.
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
                connection.commit()
            except Exception:
                # Keep the in-memory marker consistent with a failed claim.
                connection.rollback()
                if added_active_id:
                    _ACTIVE_REPORT_IDS.discard(report_id)
                raise
            return outcome


def _finish_report(path: Path, result: CaptainReportResult) -> None:
    """Persist the final public result before releasing the active marker."""

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


def handle_session_report(
    report_id: Any,
    report: Any,
    metadata: Any,
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner = run_openclaw_agent,
) -> CaptainReportResult:
    """Handle the complete idempotent lifecycle for one session report.

    This is the production entrypoint used by the MCP server. It validates the
    payload, claims its stable ID, runs at most one OpenClaw turn, stores the
    result, and always releases the process-local active marker.
    """

    active_env = os.environ if env is None else env
    validation = validate_report_input(report_id, report, metadata)
    if validation is not None:
        return validation

    path = state_path(active_env)
    _initialize_store(path)
    claimed, existing = _claim_report(path, report_id, report, metadata)
    if not claimed:
        assert existing is not None
        return existing

    try:
        result = invoke_openclaw(
            report_id, report, metadata, env=active_env, runner=runner
        )
        _finish_report(path, result)
        return result
    finally:
        with _ACTIVE_REPORTS_LOCK:
            _ACTIVE_REPORT_IDS.discard(report_id)
