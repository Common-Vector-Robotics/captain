from __future__ import annotations

import json
import re
from collections.abc import Mapping
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
