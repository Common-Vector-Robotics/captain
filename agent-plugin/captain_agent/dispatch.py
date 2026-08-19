"""Choose the unchanged local report flow or the complete remote turn flow."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from typing import Any

from .client_state import RemoteClientState, RemoteStateConflict, remote_state_path
from .remote import (
    RemoteCaptainClient,
    RemoteConfigurationError,
    read_remote_config,
    remote_profile_id,
    serialize_remote_payload,
)
from .reporting import (
    REPORT_ID_PATTERN,
    CaptainReportResult,
    canonical_result,
    handle_session_report,
    validate_report_input,
)


PREFLIGHT_TURN_ID = "00000000-0000-4000-8000-000000000000"


def _result(
    report_id: Any,
    status: str,
    feedback: str,
) -> CaptainReportResult:
    """Build a fixed diagnostic without reflecting input or configuration."""

    safe_report_id = report_id if isinstance(report_id, str) else "invalid-report"
    return canonical_result(
        safe_report_id,
        status,
        captain_feedback=feedback,
    )


def _report_digest(report: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    """Hash canonical report content without storing either input object."""

    canonical = json.dumps(
        {"metadata": metadata, "report": report},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _reply_digest(reply: str) -> str:
    """Hash the exact user-authored text without trimming or normalization."""

    return hashlib.sha256(reply.encode("utf-8")).hexdigest()


def _update_pending(
    state: RemoteClientState,
    report_id: str,
    turn_id: str,
    result: CaptainReportResult,
) -> CaptainReportResult:
    """Apply question state only after a validated terminal remote response."""

    try:
        if result.questions:
            state.replace_pending(report_id, turn_id, result.questions)
        else:
            state.clear_pending(report_id)
    except (OSError, ValueError, sqlite3.Error, RemoteStateConflict):
        return canonical_result(
            report_id,
            "unknown_outcome",
            captain_feedback=(
                "Captain completed remotely, but continuation state could not be saved."
            ),
        )
    return result


def handle_captain_turn(
    report_id: Any,
    report: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    reply: str | None = None,
    env: Mapping[str, str] | None = None,
    cancel_pending: bool = False,
) -> CaptainReportResult:
    """Send one report or one exact follow-up through the selected transport."""

    if not isinstance(cancel_pending, bool) or (
        cancel_pending and (report is not None or reply is not None)
    ) or (report is not None and reply is not None):
        return _result(
            report_id,
            "failed",
            "Provide exactly one report, reply, or local cancellation.",
        )

    environment = os.environ if env is None else env
    try:
        remote_config = read_remote_config(environment)
    except RemoteConfigurationError:
        return _result(
            report_id,
            "needs_configuration",
            "Set both CAPTAIN_REMOTE_URL and CAPTAIN_MEMBER_TOKEN for remote mode.",
        )

    if remote_config is None:
        if reply is not None or cancel_pending:
            return _result(
                report_id,
                "needs_configuration",
                "Automatic Captain continuation requires remote mode.",
            )
        # Preserve the established local function call when no injected env exists.
        if env is None:
            return handle_session_report(report_id, report, metadata)
        return handle_session_report(report_id, report, metadata, env=environment)

    if not isinstance(report_id, str) or REPORT_ID_PATTERN.fullmatch(report_id) is None:
        return _result(
            report_id,
            "failed",
            "report_id must contain 1-128 ASCII letters, numbers, '.', '_', or '-'.",
        )

    remote_metadata: Mapping[str, Any] = {} if metadata is None else metadata
    if cancel_pending:
        pass
    elif reply is not None:
        if not isinstance(reply, str) or not reply.strip():
            return _result(
                report_id,
                "needs_clarification",
                "reply must be nonempty user-authored text.",
            )
    else:
        try:
            validation = validate_report_input(report_id, report, remote_metadata)
        except (TypeError, ValueError, RecursionError):
            return _result(
                report_id,
                "failed",
                "report and metadata must contain valid JSON values.",
            )
        if validation is not None:
            return validation

    preflight_payload: Mapping[str, Any]
    if cancel_pending:
        preflight_payload = {}
    elif reply is not None:
        preflight_payload = {
            "turn_id": PREFLIGHT_TURN_ID,
            "kind": "reply",
            "reply": reply,
        }
    else:
        assert report is not None
        preflight_payload = {
            "turn_id": PREFLIGHT_TURN_ID,
            "kind": "report",
            "report": report,
            "metadata": remote_metadata,
        }
    if not cancel_pending:
        try:
            serialize_remote_payload(preflight_payload, PREFLIGHT_TURN_ID)
        except ValueError:
            return _result(
                report_id,
                "failed",
                "The remote Captain request must be valid JSON up to 262,144 bytes.",
            )

    try:
        state = RemoteClientState(
            remote_state_path(environment),
            profile_id=remote_profile_id(remote_config),
            env=environment,
        )
    except (OSError, ValueError, sqlite3.Error, RemoteStateConflict):
        return _result(
            report_id,
            "needs_configuration",
            "Captain remote continuation state could not be opened.",
        )

    if cancel_pending:
        try:
            state.clear_pending(report_id)
        except (OSError, ValueError, sqlite3.Error, RemoteStateConflict):
            return _result(
                report_id,
                "failed",
                "Captain continuation state could not be cleared.",
            )
        return _result(
            report_id,
            "needs_clarification",
            "Pending Captain reply was cleared locally; nothing was sent.",
        )

    if reply is not None:
        try:
            pending = state.get_pending(report_id)
        except (OSError, ValueError, sqlite3.Error, RemoteStateConflict):
            pending = None
        if pending is None:
            return _result(
                report_id,
                "needs_clarification",
                "No current Captain question is waiting for a reply.",
            )
        try:
            turn_id = state.get_or_create_reply_turn(
                report_id,
                pending.parent_turn_id,
                _reply_digest(reply),
            )
        except (OSError, ValueError, sqlite3.Error, RemoteStateConflict):
            return _result(
                report_id,
                "failed",
                "The reply no longer matches the current Captain question.",
            )
        payload: Mapping[str, Any] = {
            "turn_id": turn_id,
            "kind": "reply",
            "reply": reply,
        }
    else:
        assert report is not None
        try:
            digest = _report_digest(report, remote_metadata)
            turn_id = state.get_or_create_report_turn(report_id, digest)
        except (OSError, TypeError, ValueError, sqlite3.Error, RemoteStateConflict):
            return _result(
                report_id,
                "failed",
                "This report ID does not match its previously saved remote report.",
            )
        payload = {
            "turn_id": turn_id,
            "kind": "report",
            "report": report,
            "metadata": remote_metadata,
        }

    client = RemoteCaptainClient(remote_config)
    result = client.submit_and_poll(report_id, turn_id, payload)
    if not client.terminal_response or result.status == "queued":
        return result
    return _update_pending(state, report_id, turn_id, result)
