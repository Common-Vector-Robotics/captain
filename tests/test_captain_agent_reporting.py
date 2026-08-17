import errno
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
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


@pytest.mark.parametrize(
    ("object_name", "report", "metadata", "secret"),
    [
        (
            "report",
            {**VALID_REPORT, "context": [{"auth": "report-secret"}]},
            {"client": "codex"},
            "report-secret",
        ),
        (
            "metadata",
            VALID_REPORT,
            {"client": "codex", "nested": ({"auth": "metadata-secret"},)},
            "metadata-secret",
        ),
    ],
)
def test_validation_rejects_exact_auth_key_recursively_without_reflection(
    object_name, report, metadata, secret
):
    result = validate_report_input("report-1", report, metadata)

    assert result.status == "failed"
    assert object_name in result.captain_feedback
    assert secret not in result.captain_feedback


@pytest.mark.parametrize(
    "reserved_report",
    [
        {**VALID_REPORT, "context": [{"identityClaims": {"subject": "private"}}]},
        {**VALID_REPORT, "context": ({"authorization": "Bearer private"},)},
    ],
)
def test_validation_rejects_nested_reserved_claims_in_report(reserved_report):
    result = validate_report_input("report-1", reserved_report, {"client": "codex"})

    assert result.status == "failed"
    assert "report" in result.captain_feedback
    assert "private" not in result.captain_feedback


def test_prompt_strips_reserved_claims_from_both_input_objects():
    report = {
        **VALID_REPORT,
        "context": [{"identityClaims": {"subject": "report-secret"}}],
    }
    metadata = {
        "client": "codex",
        "nested": ({"authenticatedEmail": "metadata-secret"},),
    }

    prompt = build_status_update_prompt("report-1", report, metadata)

    assert "identityClaims" not in prompt
    assert "report-secret" not in prompt
    assert "authenticatedEmail" not in prompt
    assert "metadata-secret" not in prompt


def test_prompt_strips_exact_auth_key_from_both_input_objects():
    report = {**VALID_REPORT, "context": [{"auth": "report-secret"}]}
    metadata = {
        "client": "codex",
        "nested": ({"auth": "metadata-secret"},),
    }

    prompt = build_status_update_prompt("report-1", report, metadata)

    assert '"auth":' not in prompt
    assert "report-secret" not in prompt
    assert "metadata-secret" not in prompt


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


def test_prompt_forbids_recursive_captain_reporting_calls():
    prompt = build_status_update_prompt("report-1", VALID_REPORT, {"client": "codex"})

    assert "Do not invoke `/captain`" in prompt
    assert "do not load or invoke the `captain` skill" in prompt
    assert "`Captain:captain_session_report`" in prompt
    assert "do not call `captain_session_report`" in prompt
    assert "`captain__captain_session_report`" in prompt
    assert "return the required JSON directly" in prompt


def test_unexpected_status_warning_is_bounded_and_redacted():
    result = canonical_result(
        "report-1",
        "future_status authorization=Bearer external-secret " + "x" * 5_000,
        captain_feedback="No supported completion status was returned.",
    )

    assert result.status == "unknown_outcome"
    assert len(result.warnings[0]) <= 260
    assert "external-secret" not in result.warnings[0]
    assert "[redacted]" in result.warnings[0]


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


def test_real_adapter_timeout_kills_and_reaps_before_returning_unknown(monkeypatch):
    process = None

    class TimeoutProcess:
        def __init__(self, args):
            self.args = args
            self.killed = False
            self.reaped = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.wait()

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired(self.args, timeout)

        def kill(self):
            self.killed = True

        def wait(self):
            self.reaped = True

    def start_process(args, **kwargs):
        nonlocal process
        process = TimeoutProcess(args)
        return process

    monkeypatch.setattr(reporting.subprocess, "Popen", start_process)

    result = reporting.invoke_openclaw("report-1", VALID_REPORT, {}, env={})

    assert result.status == "unknown_outcome"
    assert process is not None
    assert process.killed is True
    assert process.reaped is True


def test_missing_openclaw_in_real_adapter_is_configuration_error(monkeypatch):
    def missing_process(*args, **kwargs):
        raise FileNotFoundError("openclaw")

    monkeypatch.setattr(reporting.subprocess, "Popen", missing_process)

    result = reporting.invoke_openclaw(
        "report-1", VALID_REPORT, {}, env={}
    )
    assert result.status == "needs_configuration"


@pytest.mark.parametrize(
    "launch_error",
    [
        PermissionError("permission denied for token=external-secret"),
        OSError(errno.ENOEXEC, "invalid executable format"),
    ],
)
def test_real_adapter_start_os_errors_are_configuration_errors(
    monkeypatch, launch_error
):
    def failing_process(*args, **kwargs):
        raise launch_error

    monkeypatch.setattr(reporting.subprocess, "Popen", failing_process)
    result = reporting.invoke_openclaw("report-1", VALID_REPORT, {}, env={})

    assert result.status == "needs_configuration"
    assert "external-secret" not in " ".join(result.warnings)


def test_real_adapter_returns_captured_output_and_return_code_without_shell(
    monkeypatch,
):
    observed = {}

    class CompletedProcess:
        def __init__(self, args):
            self.args = args

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, input=None, timeout=None):
            observed["communicate"] = (input, timeout)
            return "captured stdout", "captured stderr"

        def poll(self):
            return 7

    def start_process(args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return CompletedProcess(args)

    monkeypatch.setattr(reporting.subprocess, "Popen", start_process)

    completed = reporting.run_openclaw_agent(["openclaw", "agent"], "prompt", 45)

    assert completed.args == ["openclaw", "agent"]
    assert completed.returncode == 7
    assert completed.stdout == "captured stdout"
    assert completed.stderr == "captured stderr"
    assert observed == {
        "args": ["openclaw", "agent"],
        "kwargs": {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "shell": False,
        },
        "communicate": ("prompt", 75),
    }


def test_real_adapter_communication_oserror_is_unknown(monkeypatch):
    process = None

    class CommunicationFailureProcess:
        def __init__(self, args):
            self.args = args
            self.killed = False
            self.reaped = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.wait()

        def communicate(self, input=None, timeout=None):
            raise OSError(errno.EIO, "write failed for token=external-secret")

        def kill(self):
            self.killed = True

        def wait(self):
            self.reaped = True

    def start_process(args, **kwargs):
        nonlocal process
        process = CommunicationFailureProcess(args)
        return process

    monkeypatch.setattr(reporting.subprocess, "Popen", start_process)

    result = reporting.invoke_openclaw("report-1", VALID_REPORT, {}, env={})

    assert result.status == "unknown_outcome"
    assert "external-secret" not in " ".join(result.warnings)
    assert process is not None
    assert process.killed is True
    assert process.reaped is True


def test_arbitrary_runner_oserror_after_dispatch_is_unknown():
    def post_dispatch_runner(command, prompt, timeout):
        raise OSError(errno.EIO, "write failed for token=external-secret")

    result = reporting.invoke_openclaw(
        "report-1", VALID_REPORT, {}, env={}, runner=post_dispatch_runner
    )

    assert result.status == "unknown_outcome"
    assert "external-secret" not in " ".join(result.warnings)


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


def _completed_response(command, response):
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=json.dumps({"payloads": [{"text": json.dumps(response)}]}),
        stderr="",
    )


def test_state_path_uses_xdg_then_home(monkeypatch, tmp_path):
    assert reporting.state_path({"XDG_STATE_HOME": str(tmp_path)}) == (
        tmp_path / "captain-agent" / "reports.sqlite3"
    )
    monkeypatch.setattr(reporting.Path, "home", lambda: tmp_path)
    assert reporting.state_path({}) == (
        tmp_path / ".local" / "state" / "captain-agent" / "reports.sqlite3"
    )


def test_same_report_id_replays_without_second_openclaw_turn(tmp_path):
    calls = []

    def runner(command, prompt, timeout):
        calls.append(command)
        return _completed_response(command, {
            "status": "updated",
            "clickup_updates": [{"action": "updated", "task_id": "task-1"}],
            "captain_feedback": "Updated the task.",
        })

    env = {"CAPTAIN_AGENT_STATE_PATH": str(tmp_path / "reports.sqlite3")}
    first = reporting.handle_session_report(
        "report-1", VALID_REPORT, {}, env=env, runner=runner
    )
    second = reporting.handle_session_report(
        "report-1", VALID_REPORT, {}, env=env, runner=runner
    )
    assert first == second
    assert len(calls) == 1


def test_real_adapter_communication_uncertainty_is_not_dispatched_twice(
    monkeypatch, tmp_path
):
    starts = 0

    class CommunicationFailureProcess:
        def __init__(self, args):
            self.args = args

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.wait()

        def communicate(self, input=None, timeout=None):
            raise OSError(errno.EIO, "post-dispatch pipe failure")

        def kill(self):
            pass

        def wait(self):
            pass

    def start_process(args, **kwargs):
        nonlocal starts
        starts += 1
        return CommunicationFailureProcess(args)

    monkeypatch.setattr(reporting.subprocess, "Popen", start_process)
    env = {"CAPTAIN_AGENT_STATE_PATH": str(tmp_path / "reports.sqlite3")}

    first = reporting.handle_session_report(
        "report-1", VALID_REPORT, {}, env=env
    )
    replay = reporting.handle_session_report(
        "report-1", VALID_REPORT, {}, env=env
    )

    assert first.status == "unknown_outcome"
    assert replay == first
    assert starts == 1


def _seed_processing(path, report_id):
    reporting._initialize_store(path)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO session_reports(
                    report_id, project, status, result_json, created_at, updated_at
                ) VALUES (?, ?, 'processing', NULL, ?, ?)
                """,
                (
                    report_id,
                    "Captain",
                    "2026-08-17T00:00:00Z",
                    "2026-08-17T00:00:00Z",
                ),
            )


def _seed_stored_result(path, report_id, status):
    reporting._initialize_store(path)
    stored_result = json.dumps(
        {
            "report_id": report_id,
            "status": status,
            "clickup_updates": [],
            "captain_feedback": f"Stored {status} result.",
            "questions": [],
            "warnings": [],
        },
        sort_keys=True,
    )
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO session_reports(
                    report_id, project, status, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    "Old project",
                    status,
                    stored_result,
                    "2026-08-17T00:00:00Z",
                    "2026-08-17T00:00:00Z",
                ),
            )


def _stored_row(path, report_id):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            """
            SELECT project, status, result_json, created_at, updated_at
            FROM session_reports
            WHERE report_id = ?
            """,
            (report_id,),
        ).fetchone()


def test_processing_report_in_this_process_returns_queued(tmp_path):
    path = tmp_path / "reports.sqlite3"
    _seed_processing(path, "report-1")
    with reporting._ACTIVE_REPORTS_LOCK:
        reporting._ACTIVE_REPORT_IDS.add("report-1")
    try:
        result = reporting.handle_session_report(
            "report-1",
            VALID_REPORT,
            {},
            env={"CAPTAIN_AGENT_STATE_PATH": str(path)},
            runner=lambda *_: pytest.fail("must not invoke OpenClaw"),
        )
    finally:
        with reporting._ACTIVE_REPORTS_LOCK:
            reporting._ACTIVE_REPORT_IDS.discard("report-1")
    assert result.status == "queued"


@pytest.mark.parametrize(
    "retryable_status",
    ["failed", "needs_configuration", "needs_clarification", "queued"],
)
def test_active_id_prevents_retryable_row_reclaim_until_marker_is_removed(
    tmp_path, retryable_status
):
    path = tmp_path / "reports.sqlite3"
    _seed_stored_result(path, "report-1", retryable_status)
    original_row = _stored_row(path, "report-1")

    with reporting._ACTIVE_REPORTS_LOCK:
        reporting._ACTIVE_REPORT_IDS.add("report-1")
    try:
        claimed, result = reporting._claim_report(
            path, "report-1", VALID_REPORT, {"client": "codex"}
        )
        active_row = _stored_row(path, "report-1")
    finally:
        with reporting._ACTIVE_REPORTS_LOCK:
            reporting._ACTIVE_REPORT_IDS.discard("report-1")

    assert claimed is False
    assert result is not None
    assert result.status == "queued"
    assert active_row == original_row

    try:
        reclaimed, existing = reporting._claim_report(
            path, "report-1", VALID_REPORT, {"client": "codex"}
        )
        reclaimed_row = _stored_row(path, "report-1")
    finally:
        with reporting._ACTIVE_REPORTS_LOCK:
            reporting._ACTIVE_REPORT_IDS.discard("report-1")

    assert reclaimed is True
    assert existing is None
    assert reclaimed_row[0:3] == ("Captain", "processing", None)
    assert reclaimed_row[3] == original_row[3]
    assert reclaimed_row[4] != original_row[4]


def test_orphaned_processing_report_becomes_unknown(tmp_path):
    path = tmp_path / "reports.sqlite3"
    _seed_processing(path, "report-1")
    result = reporting.handle_session_report(
        "report-1",
        VALID_REPORT,
        {},
        env={"CAPTAIN_AGENT_STATE_PATH": str(path)},
        runner=lambda *_: pytest.fail("must not invoke OpenClaw"),
    )
    assert result.status == "unknown_outcome"


def test_failed_report_can_succeed_with_the_same_id(tmp_path):
    calls = []

    def runner(command, prompt, timeout):
        calls.append(command)
        status = "failed" if len(calls) == 1 else "updated"
        return _completed_response(
            command,
            {
                "status": status,
                "captain_feedback": f"Captain returned {status}.",
            },
        )

    env = {"CAPTAIN_AGENT_STATE_PATH": str(tmp_path / "reports.sqlite3")}

    first = reporting.handle_session_report(
        "report-1", VALID_REPORT, {}, env=env, runner=runner
    )
    second = reporting.handle_session_report(
        "report-1", VALID_REPORT, {}, env=env, runner=runner
    )

    assert first.status == "failed"
    assert second.status == "updated"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "retryable_status",
    ["failed", "needs_configuration", "needs_clarification", "queued"],
)
def test_retryable_stored_result_is_transactionally_reclaimed(
    tmp_path, retryable_status
):
    path = tmp_path / "reports.sqlite3"
    _seed_stored_result(path, "report-1", retryable_status)
    observed_rows = []

    def runner(command, prompt, timeout):
        with closing(sqlite3.connect(path)) as connection:
            observed_rows.append(
                connection.execute(
                    """
                    SELECT project, status, result_json, updated_at
                    FROM session_reports
                    WHERE report_id = ?
                    """,
                    ("report-1",),
                ).fetchone()
            )
        return _completed_response(
            command,
            {"status": "updated", "captain_feedback": "Retry completed."},
        )

    report = {**VALID_REPORT, "project": "Current project"}
    result = reporting.handle_session_report(
        "report-1",
        report,
        {},
        env={"CAPTAIN_AGENT_STATE_PATH": str(path)},
        runner=runner,
    )

    assert result.status == "updated"
    assert len(observed_rows) == 1
    project, status, result_json, updated_at = observed_rows[0]
    assert (project, status, result_json) == ("Current project", "processing", None)
    assert updated_at != "2026-08-17T00:00:00Z"


@pytest.mark.parametrize(
    "immutable_status",
    ["created", "updated", "partial", "unknown_outcome"],
)
def test_immutable_stored_result_replays_without_dispatch(tmp_path, immutable_status):
    path = tmp_path / "reports.sqlite3"
    _seed_stored_result(path, "report-1", immutable_status)

    result = reporting.handle_session_report(
        "report-1",
        VALID_REPORT,
        {},
        env={"CAPTAIN_AGENT_STATE_PATH": str(path)},
        runner=lambda *_: pytest.fail("immutable result must not dispatch"),
    )

    assert result.status == immutable_status
    assert result.captain_feedback == f"Stored {immutable_status} result."


def test_store_closes_every_connection(monkeypatch, tmp_path):
    real_connect = sqlite3.connect
    tracked_connections = []

    class TrackedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def close(self):
            self.closed = True
            self.connection.close()

    def tracking_connect(*args, **kwargs):
        connection = TrackedConnection(real_connect(*args, **kwargs))
        tracked_connections.append(connection)
        return connection

    monkeypatch.setattr(reporting.sqlite3, "connect", tracking_connect)
    path = tmp_path / "reports.sqlite3"
    reporting._initialize_store(path)
    claimed, existing = reporting._claim_report(path, "report-1", VALID_REPORT, {})
    assert claimed is True
    assert existing is None
    reporting._finish_report(
        path,
        canonical_result(
            "report-1", "updated", captain_feedback="Stored and closed."
        ),
    )
    with reporting._ACTIVE_REPORTS_LOCK:
        reporting._ACTIVE_REPORT_IDS.discard("report-1")

    assert tracked_connections
    assert all(connection.closed for connection in tracked_connections)


def test_store_permissions_are_user_only(tmp_path):
    path = tmp_path / "state" / "reports.sqlite3"
    reporting._initialize_store(path)
    assert path.parent.stat().st_mode & 0o077 == 0
    assert path.stat().st_mode & 0o077 == 0
