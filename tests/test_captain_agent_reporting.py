import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-plugin"))

from captain_agent import reporting
from captain_agent.reporting import (
    CaptainReportResult,
    MAX_REPORT_BYTES,
    build_status_update_prompt,
    canonical_result,
    validate_report_input,
)


VALID_REPORT = {
    "project": "Captain",
    "context": {"cwd": "/work/captain", "branch": "feature"},
    "summary": ["Implemented the local Captain agent plugin."],
    "changed_files": ["agent-plugin/captain_agent/reporting.py"],
    "verification": [{"command": "pytest", "result": "pass"}],
    "decisions": ["Use local MCP stdio only."],
    "blockers": [],
    "risks": [],
    "next_steps": ["Open a pull request."],
}


def test_canonical_result_has_the_public_shape():
    result = canonical_result(
        "report-1",
        "updated",
        captain_feedback="Updated the matching task.",
        clickup_updates=[{"action": "updated", "task_id": "task-1"}],
    )
    assert result == CaptainReportResult(
        report_id="report-1",
        status="updated",
        clickup_updates=[{"action": "updated", "task_id": "task-1"}],
        captain_feedback="Updated the matching task.",
        questions=[],
        warnings=[],
    )


def test_validation_accepts_a_concise_report():
    assert validate_report_input("report-1", VALID_REPORT, {"client": "codex"}) is None


def test_validation_rejects_unsafe_report_id():
    result = validate_report_input("report 1/../../x", VALID_REPORT, {})
    assert result.status == "failed"
    assert "report_id" in result.captain_feedback


def test_validation_asks_for_a_missing_summary():
    result = validate_report_input("report-1", {"summary": []}, {})
    assert result.status == "needs_clarification"
    assert result.questions == ["What changed in this session?"]


def test_validation_rejects_more_than_one_megabyte():
    report = {"summary": ["x" * 1_000_001]}
    result = validate_report_input("report-1", report, {})
    assert result.status == "failed"
    assert "1,000,000" in result.captain_feedback


def test_validation_accepts_report_at_exact_byte_limit():
    metadata = {"client": "codex"}

    def payload_size(summary_length):
        report = {"summary": ["x" * summary_length]}
        return sum(
            len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            for value in (report, metadata)
        )

    low, high = 0, MAX_REPORT_BYTES
    while low < high:
        midpoint = (low + high + 1) // 2
        if payload_size(midpoint) <= MAX_REPORT_BYTES:
            low = midpoint
        else:
            high = midpoint - 1
    report = {"summary": ["x" * low]}
    assert payload_size(low) == MAX_REPORT_BYTES
    assert validate_report_input("report-1", report, metadata) is None


def test_validation_rejects_nested_reserved_auth_metadata():
    result = validate_report_input(
        "report-1",
        VALID_REPORT,
        {"client": "codex", "context": {"authenticated_email": "user@example.com"}},
    )
    assert result.status == "failed"
    assert "authentication" in result.captain_feedback


def test_prompt_strips_reserved_auth_metadata():
    prompt = build_status_update_prompt(
        "report-1",
        VALID_REPORT,
        {"client": "codex", "nested": {"authorization": "Bearer secret"}},
    )
    assert "authorization" not in prompt
    assert "Bearer secret" not in prompt


def test_validation_rejects_camel_case_reserved_identity_metadata():
    result = validate_report_input(
        "report-1",
        VALID_REPORT,
        {"client": "codex", "identityClaims": {"subject": "user-1"}},
    )
    assert result.status == "failed"
    assert "authentication" in result.captain_feedback


def test_validation_rejects_reserved_auth_metadata_nested_in_tuple():
    result = validate_report_input(
        "report-1",
        VALID_REPORT,
        {"client": "codex", "nested": ({"authenticatedEmail": "user@example.com"},)},
    )
    assert result.status == "failed"
    assert "authentication" in result.captain_feedback


def test_prompt_delimits_local_report_without_identity_claims():
    prompt = build_status_update_prompt(
        "report-1", VALID_REPORT, {"client": "codex", "repo": "captain"}
    )
    assert "local `/captain` status update" in prompt
    assert "report-1" in prompt
    assert "Audit every ClickUp write." in prompt
    assert "authenticated_email" not in prompt
    assert "Intermode" not in prompt
    assert json.dumps(VALID_REPORT, indent=2, sort_keys=True) in prompt


def test_openclaw_command_uses_safe_defaults_and_no_report_text():
    command, timeout = reporting.build_openclaw_command("report-1", {})
    assert command == [
        "openclaw", "agent", "--agent", "captain",
        "--session-id", "captain-report-report-1",
        "--thinking", "high", "--timeout", "300",
        "--json", "--message-file", "-",
    ]
    assert timeout == 300
    assert "Implemented the local Captain agent plugin" not in " ".join(command)


def test_openclaw_command_uses_bounded_environment_overrides():
    command, timeout = reporting.build_openclaw_command(
        "report-1",
        {
            "CAPTAIN_AGENT_OPENCLAW_COMMAND": "/usr/local/bin/openclaw",
            "CAPTAIN_AGENT_ID": "project-captain",
            "CAPTAIN_AGENT_THINKING": "medium",
            "CAPTAIN_AGENT_TIMEOUT_SECONDS": "45",
        },
    )
    assert command[0] == "/usr/local/bin/openclaw"
    assert command[command.index("--agent") + 1] == "project-captain"
    assert command[command.index("--thinking") + 1] == "medium"
    assert command[command.index("--timeout") + 1] == "45"
    assert timeout == 45


def test_direct_captain_json_is_normalized():
    result = reporting.normalize_captain_agent_response(
        "report-1",
        {
            "status": "updated",
            "clickup_updates": [{"action": "updated", "task_id": "task-1"}],
            "captain_feedback": "Updated the task.",
            "questions": [],
            "warnings": [],
        },
    )
    assert result.status == "updated"
    assert result.clickup_updates[0]["task_id"] == "task-1"


def test_nested_openclaw_envelope_is_normalized():
    response = {
        "status": "ok",
        "result": {
            "payloads": [{"text": json.dumps({
                "status": "created",
                "clickup_updates": [{"action": "created", "task_id": "task-2"}],
                "captain_feedback": "Created the task.",
            })}]
        },
    }
    result = reporting.normalize_captain_agent_response("report-1", response)
    assert result.status == "created"


def test_fenced_captain_response_wins_over_leading_diagnostic_json():
    response = {
        "result": {
            "payloads": [{"text": """{"level": "debug"}
```json
{
  "status": "failed",
  "captain_feedback": "ClickUp update failed."
}
```"""}]
        }
    }

    result = reporting.normalize_captain_agent_response("report-1", response)

    assert result.status == "failed"
    assert result.captain_feedback == "ClickUp update failed."


def test_timeout_after_dispatch_is_unknown():
    def timeout_runner(command, prompt, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    result = reporting.invoke_openclaw(
        "report-1", VALID_REPORT, {}, env={}, runner=timeout_runner
    )
    assert result.status == "unknown_outcome"


def test_missing_openclaw_is_configuration_error():
    def missing_runner(command, prompt, timeout):
        raise FileNotFoundError("openclaw")

    result = reporting.invoke_openclaw(
        "report-1", VALID_REPORT, {}, env={}, runner=missing_runner
    )
    assert result.status == "needs_configuration"


@pytest.mark.parametrize(
    "completed",
    [
        subprocess.CompletedProcess(["openclaw"], 1, stdout="", stderr="gateway stopped"),
        subprocess.CompletedProcess(["openclaw"], 0, stdout="not-json", stderr=""),
    ],
)
def test_unproven_completion_is_unknown_and_bounded(completed):
    result = reporting.invoke_openclaw(
        "report-1",
        VALID_REPORT,
        {},
        env={},
        runner=lambda command, prompt, timeout: completed,
    )
    assert result.status == "unknown_outcome"
    assert len(result.warnings[0]) <= 1_020
