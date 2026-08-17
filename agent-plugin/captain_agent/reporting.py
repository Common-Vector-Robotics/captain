from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
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
ALLOWED_STATUSES = set(ReportStatus.__args__)
REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_REPORT_BYTES = 1_000_000
DEFAULT_OPENCLAW_COMMAND = "openclaw"
DEFAULT_AGENT_ID = "captain"
DEFAULT_THINKING = "high"
DEFAULT_TIMEOUT_SECONDS = 300
Runner = Callable[[Sequence[str], str, int], subprocess.CompletedProcess[str]]
RESERVED_METADATA_KEYS = {
    "access_token",
    "authenticated_email",
    "authenticated_user",
    "authorization",
    "auth_claims",
    "identity",
    "identity_claims",
    "user_claims",
}


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
    active_warnings = list(warnings or [])
    if status not in ALLOWED_STATUSES:
        active_warnings.append(f"Unexpected Captain status: {status!r}")
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
    summary = report.get("summary")
    if isinstance(summary, list):
        return [str(item).strip() for item in summary if str(item).strip()]
    if isinstance(summary, str) and summary.strip():
        return [summary.strip()]
    return []


def _is_reserved_metadata_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")
    return (
        normalized in RESERVED_METADATA_KEYS
        or normalized.startswith("authenticated_")
        or normalized.startswith("authorization_")
        or normalized.startswith("auth_")
        or normalized.endswith("_claims")
    )


def _reserved_metadata_key(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _is_reserved_metadata_key(key):
                return str(key)
            found = _reserved_metadata_key(nested)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _reserved_metadata_key(nested)
            if found:
                return found
    return None


def _strip_reserved_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _strip_reserved_metadata(nested)
            for key, nested in value.items()
            if not _is_reserved_metadata_key(key)
        }
    if isinstance(value, list):
        return [_strip_reserved_metadata(nested) for nested in value]
    if isinstance(value, tuple):
        return tuple(_strip_reserved_metadata(nested) for nested in value)
    return value


def validate_report_input(
    report_id: Any,
    report: Any,
    metadata: Any,
) -> CaptainReportResult | None:
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
    reserved_key = _reserved_metadata_key(metadata)
    if reserved_key:
        return canonical_result(
            report_id,
            "failed",
            captain_feedback=(
                f"metadata contains reserved authentication field {reserved_key!r}."
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
    report_json = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    metadata_json = json.dumps(
        _strip_reserved_metadata(metadata),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return f"""You are Captain preparing a local `/captain` status update for a user-operated workspace.

Use normal PM judgment to identify what changed, what is missing, who owns it, and what decision or action is needed. Audit every ClickUp write. Do not claim identity, authentication, hosted services, or actions that are not supported by the supplied evidence.

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
    try:
        value = int(str(env.get("CAPTAIN_AGENT_TIMEOUT_SECONDS", "300")))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return min(max(value, 30), 3_600)


def build_openclaw_command(
    report_id: str,
    env: Mapping[str, str],
) -> tuple[list[str], int]:
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
    return subprocess.run(
        list(command),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 30,
        check=False,
        shell=False,
    )


def _json_object_from_text(value: str) -> Mapping[str, Any] | None:
    """Extract a JSON object from direct, fenced, or surrounding CLI text."""
    decoder = json.JSONDecoder()
    candidates = [value.strip()]
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", value, re.IGNORECASE)
    if fence_match:
        candidates.append(fence_match.group(1).strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            first_object = candidate.find("{")
            if first_object == -1:
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate[first_object:])
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, Mapping):
            return parsed
    return None


def _response_text(response: Mapping[str, Any]) -> str | None:
    """Find the documented visible result text after unwrapping result envelopes."""
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
    if isinstance(response.get("status"), str) and "captain_feedback" in response:
        return response
    response_text = _response_text(response)
    if response_text is None:
        return None
    return _json_object_from_text(response_text)


def _string_list(value: Any) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _clickup_updates(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        return None
    return [dict(item) for item in value]


def normalize_captain_agent_response(
    report_id: str,
    response: Mapping[str, Any],
) -> CaptainReportResult:
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


def _bounded_external_text(value: Any, *, limit: int = 1_000) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def _unknown_outcome(report_id: str, reason: str, detail: Any = "") -> CaptainReportResult:
    message = reason
    if str(detail):
        # Keep the entire warning compact while safely preserving CLI evidence.
        message = f"{reason}: {_bounded_external_text(detail, limit=1_000 - len(reason) - 2)}"
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
    validation_result = validate_report_input(report_id, report, metadata)
    if validation_result is not None:
        return validation_result

    prompt = build_status_update_prompt(report_id, report, metadata)
    command, timeout = build_openclaw_command(report_id, env)
    try:
        completed = runner(command, prompt, timeout)
    except FileNotFoundError as error:
        return canonical_result(
            report_id,
            "needs_configuration",
            captain_feedback="OpenClaw executable is not available.",
            warnings=[_bounded_external_text(error)],
        )
    except subprocess.TimeoutExpired as error:
        return _unknown_outcome(report_id, "OpenClaw timed out", error)
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
