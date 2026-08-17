# Captain Agent Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a user-operated `/captain` Codex/OpenClaw plugin that reports coding-agent work through the user's configured OpenClaw Gateway over MCP `stdio`, then open a pull request into `Common-Vector-Robotics/captain:main`.

**Architecture:** A Codex-format bundle exposes one typed MCP tool. The tool validates a structured report, claims its identifier in a user-local SQLite database, invokes the local OpenClaw CLI without a shell, and lets that CLI route through its configured local or remote Gateway. It normalizes Captain's JSON response and replays stored results without duplicate OpenClaw turns. A portable `SKILL.md` owns report gathering and user-facing output; Captain retains all ClickUp judgment and writes.

**Tech Stack:** Python 3.10+, MCP Python SDK `mcp>=2,<3`, Pydantic v2, SQLite, pytest, POSIX shell launcher, Codex plugin marketplace manifests, OpenClaw CLI.

**Spec:** `docs/superpowers/specs/2026-08-17-captain-agent-plugin-design.md`

## Global Constraints

- Repository and PR target: `git@github.com:Common-Vector-Robotics/captain.git`, base branch `main`.
- Start from a freshly fetched `origin/main`; fetch and incorporate upstream again immediately before PR creation.
- Use MCP `stdio` only. Do not add HTTP, OAuth, bearer tokens, A2A, or a hosted gateway.
- Expose exactly one MCP tool: `captain_session_report(report_id, report, metadata)`.
- Accept `report_id` only when it is 1-128 characters from `[A-Za-z0-9._-]`.
- Reject serialized `report` plus `metadata` content larger than 1,000,000 bytes.
- Public statuses are exactly `created`, `updated`, `queued`, `needs_clarification`, `needs_configuration`, `partial`, `failed`, and `unknown_outcome`.
- `failed` is definitive. Once OpenClaw may have received the report, missing or malformed completion evidence maps to `unknown_outcome`.
- The calling agent cannot supply an authenticated email or authorization claim.
- The report prompt travels through subprocess standard input and never appears in the process arguments.
- State is local SQLite at `$XDG_STATE_HOME/captain-agent/reports.sqlite3`, falling back to `~/.local/state/captain-agent/reports.sqlite3`, with `CAPTAIN_AGENT_STATE_PATH` as the complete-path override.
- Defaults: command `openclaw`, agent `captain`, thinking `high`, timeout 300 seconds.
- Do not claim a live OpenClaw/ClickUp write unless one is separately authorized and actually run.

### Final-review contract amendments

- Recursively reject authentication, authorization, identity, and claims keys
  from both `report` and `metadata`, including camelCase keys and keys nested in
  lists or tuples. Strip them from both objects again before serialization.
- Stored `failed`, `needs_configuration`, `needs_clarification`, and `queued`
  results are retryable same-ID claims. Stored `created`, `updated`, `partial`,
  and `unknown_outcome` results are immutable replays. Keep active-ID and
  orphaned-`processing` behavior process-local; V1 adds no cross-process lease.
- The skill selects exactly one host-catalog name:
  `Captain:captain_session_report` for Codex or
  `captain__captain_session_report` for OpenClaw. Neither or both is
  `needs_configuration`, not a reason to guess or call both.
- Unsafe host session identifiers become stable `captain-<sha256>` report IDs
  and are not included or displayed. Missing host IDs still use a UUID.
- The terminal Captain prompt forbids `/captain`, the `captain` skill, and both
  reporting-tool names. The `id: "captain"` OpenClaw agent excludes the skill
  and denies `captain__captain_session_report`; deny wins.
- A system `python3` is eligible only when `from mcp.server import MCPServer`
  works and `importlib.metadata.version("mcp")` has major version 2.
- Pre-launch `OSError` values are `needs_configuration`. Timeouts, launched
  non-zero exits, malformed output, and uncertain post-dispatch exceptions stay
  `unknown_outcome`. All external status and process diagnostics are bounded
  and redacted.

## File Map

- `.agents/plugins/marketplace.json`: makes the repository installable as a Codex marketplace.
- `agent-plugin/.codex-plugin/plugin.json`: bundle identity, skill root, and MCP-server declaration.
- `agent-plugin/.mcp.json`: one relative local MCP-server command.
- `agent-plugin/requirements.txt`: optional/local runtime dependency pin `mcp>=2,<3`.
- `agent-plugin/bin/captain-agent-mcp`: cache-safe launcher selecting ready Python or local `uv` resolution.
- `agent-plugin/captain_agent/__init__.py`: package marker and public result export.
- `agent-plugin/captain_agent/reporting.py`: contract, validation, prompt, OpenClaw runner, normalization, SQLite claim/replay, orchestration.
- `agent-plugin/captain_agent/server.py`: MCP registration and `stdio` entrypoint only.
- `agent-plugin/skills/captain/SKILL.md`: portable `/captain` workflow and final response rules.
- `agent-plugin/README.md`: plugin-specific prerequisites, setup, data path, and troubleshooting.
- `README.md`: top-level discovery and installation links.
- `package.json`: include the marketplace and plugin artifacts in the published package.
- `.gitignore`: ignore optional plugin-local `.venv` and local report state.
- `tests/test_captain_agent_reporting.py`: focused contract, subprocess, normalization, persistence, and idempotency tests.
- `tests/test_captain_agent_package.py`: manifests, launcher, skill, package files, and in-process MCP tests.
- `tests/test_public_package_contract.py`: recurse into packaged directories so public-safety checks cover the plugin.

---

### Task 1: Define and Validate the Public Report Contract

**Files:**
- Create: `agent-plugin/requirements.txt`
- Create: `agent-plugin/captain_agent/__init__.py`
- Create: `agent-plugin/captain_agent/reporting.py`
- Create: `tests/test_captain_agent_reporting.py`

**Interfaces:**
- Produces: `CaptainReportResult(BaseModel)` with fields `report_id`, `status`, `clickup_updates`, `captain_feedback`, `questions`, and `warnings`.
- Produces: `canonical_result(report_id, status, *, captain_feedback, clickup_updates=None, questions=None, warnings=None) -> CaptainReportResult`.
- Produces: `validate_report_input(report_id, report, metadata) -> CaptainReportResult | None`.
- Produces: `build_status_update_prompt(report_id, report, metadata) -> str`.

- [ ] **Step 1: Add the MCP dependency pin and failing contract tests**

Create `agent-plugin/requirements.txt`:

```text
mcp>=2,<3
```

Create `tests/test_captain_agent_reporting.py` with the shared import setup and initial tests:

```python
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-plugin"))

from captain_agent.reporting import (
    CaptainReportResult,
    build_status_update_prompt,
    canonical_result,
    validate_report_input,
)


VALID_REPORT = {
    "project": "Captain",
    "context": {"cwd": "/work/captain", "branch": "feature"},
    "summary": ["Implemented the Captain agent plugin."],
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


def test_prompt_delimits_user_operated_report_without_identity_claims():
    prompt = build_status_update_prompt(
        "report-1", VALID_REPORT, {"client": "codex", "repo": "captain"}
    )
    assert "user-operated `/captain` status update" in prompt
    assert "report-1" in prompt
    assert "Audit every ClickUp write." in prompt
    assert "authenticated_email" not in prompt
    assert json.dumps(VALID_REPORT, indent=2, sort_keys=True) in prompt
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run:

```bash
$CAPTAIN_AGENT_PYTHON \
  -m pytest tests/test_captain_agent_reporting.py -q
```

Expected: collection fails because `captain_agent.reporting` does not exist.

- [ ] **Step 3: Implement the typed result, validation, and prompt**

Create `agent-plugin/captain_agent/reporting.py` with these public definitions and constants:

```python
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
            report_id, "needs_clarification", captain_feedback="metadata must be an object."
        )
    payload_size = len(
        json.dumps(
            {"report": report, "metadata": metadata},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
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
```

Implement `build_status_update_prompt()` in the same file. It must describe a user-operated report, include the exact output contract, embed `report_id`, `report`, and `metadata` as sorted indented JSON, instruct Captain to use normal PM judgment, audit every ClickUp write, and return JSON only. It must not include an email, hosted gateway, or authenticated-user claim. It must also tell terminal-recipient Captain not to invoke `/captain`, load the `captain` skill, or call either reporting-tool name; Captain processes the report with normal PM capabilities and returns the required JSON directly.

Create `agent-plugin/captain_agent/__init__.py`:

```python
"""Local MCP integration for the open-source Captain agent."""

from .reporting import CaptainReportResult

__all__ = ["CaptainReportResult"]
```

- [ ] **Step 4: Run the focused tests**

Run the Task 1 pytest command again.

Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit the report contract**

```bash
git add agent-plugin/requirements.txt agent-plugin/captain_agent \
  tests/test_captain_agent_reporting.py
git commit -m "feat: define Captain agent report contract"
```

### Task 2: Invoke OpenClaw Safely and Normalize Its Result

**Files:**
- Modify: `agent-plugin/captain_agent/reporting.py`
- Modify: `tests/test_captain_agent_reporting.py`

**Interfaces:**
- Consumes: `CaptainReportResult`, `canonical_result()`, and `build_status_update_prompt()` from Task 1.
- Produces: `build_openclaw_command(report_id, env) -> tuple[list[str], int]`.
- Produces: `run_openclaw_agent(command, prompt, timeout_seconds) -> subprocess.CompletedProcess[str]`.
- Produces: `normalize_captain_agent_response(report_id, response) -> CaptainReportResult`.
- Produces: `invoke_openclaw(report_id, report, metadata, *, env, runner=run_openclaw_agent) -> CaptainReportResult`.

- [ ] **Step 1: Add failing command, normalization, and uncertainty tests**

Append tests that prove:

```python
import subprocess

import pytest

from captain_agent import reporting


def test_openclaw_command_uses_safe_defaults_and_no_report_text():
    command, timeout = reporting.build_openclaw_command("report-1", {})
    assert command == [
        "openclaw", "agent", "--agent", "captain",
        "--session-id", "captain-report-report-1",
        "--thinking", "high", "--timeout", "300",
        "--json", "--message-file", "-",
    ]
    assert timeout == 300
    assert "Implemented the Captain agent plugin" not in " ".join(command)


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
```

- [ ] **Step 2: Run the new tests and verify missing functions**

Run:

```bash
$CAPTAIN_AGENT_PYTHON \
  -m pytest tests/test_captain_agent_reporting.py -q
```

Expected: failures name `build_openclaw_command`, `normalize_captain_agent_response`, and `invoke_openclaw`.

- [ ] **Step 3: Implement command construction and subprocess execution**

Add these defaults and helpers to `reporting.py`:

```python
import os
import subprocess
from collections.abc import Callable, Sequence

DEFAULT_OPENCLAW_COMMAND = "openclaw"
DEFAULT_AGENT_ID = "captain"
DEFAULT_THINKING = "high"
DEFAULT_TIMEOUT_SECONDS = 300
Runner = Callable[[Sequence[str], str, int], subprocess.CompletedProcess[str]]


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
            "agent", "--agent", agent_id or DEFAULT_AGENT_ID,
            "--session-id", f"captain-report-{report_id}",
            "--thinking", thinking or DEFAULT_THINKING,
            "--timeout", str(timeout),
            "--json", "--message-file", "-",
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
```

- [ ] **Step 4: Implement response extraction, normalization, and safe uncertainty**

Port only the observable response-envelope behavior into new code: recurse through a mapping-valued `result`, prefer `payloads[*].text`, then `meta.finalAssistantVisibleText`, `meta.finalAssistantRawText`, `text`, `reply`, or `output`; parse a direct JSON object, a fenced JSON object, or the first outer `{...}` object. Do not import or copy private service modules.

Implement `invoke_openclaw()` with this decision table:

```text
FileNotFoundError/PermissionError/
other pre-launch OSError               -> needs_configuration
subprocess.TimeoutExpired              -> unknown_outcome
uncertain post-dispatch exception       -> unknown_outcome
non-zero process exit                  -> unknown_outcome
CLI stdout is not a JSON object         -> unknown_outcome
Captain canonical status failed         -> failed
Captain canonical terminal response     -> preserve it
```

Bound reflected stderr/stdout/exception text to 1,000 characters plus a `... [truncated]` suffix. Build the prompt before calling the runner, pass it only as the runner's `prompt` argument, and never add it to `command`.

- [ ] **Step 5: Run the focused tests**

Run the Task 2 pytest command again.

Expected: all report-contract and OpenClaw tests pass.

- [ ] **Step 6: Commit the OpenClaw adapter**

```bash
git add agent-plugin/captain_agent/reporting.py \
  tests/test_captain_agent_reporting.py
git commit -m "feat: forward Captain reports through OpenClaw"
```

### Task 3: Add Local Idempotency and Safe Replay

**Files:**
- Modify: `agent-plugin/captain_agent/reporting.py`
- Modify: `tests/test_captain_agent_reporting.py`

**Interfaces:**
- Consumes: `validate_report_input()` and `invoke_openclaw()` from Tasks 1-2.
- Produces: `state_path(env) -> Path`.
- Produces: `handle_session_report(report_id, report, metadata, *, env=None, runner=run_openclaw_agent) -> CaptainReportResult`.
- Internal persistence table: `session_reports(report_id PRIMARY KEY, project, status, result_json, created_at, updated_at)`.

- [ ] **Step 1: Add failing state and idempotency tests**

Append tests using `tmp_path` and a counting fake runner:

```python
import os
import sqlite3


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


def _seed_processing(path, report_id):
    reporting._initialize_store(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO session_reports(
                report_id, project, status, result_json, created_at, updated_at
            ) VALUES (?, ?, 'processing', NULL, ?, ?)
            """,
            (report_id, "Captain", "2026-08-17T00:00:00Z", "2026-08-17T00:00:00Z"),
        )


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


def test_store_permissions_are_user_only(tmp_path):
    path = tmp_path / "state" / "reports.sqlite3"
    reporting._initialize_store(path)
    assert path.parent.stat().st_mode & 0o077 == 0
    assert path.stat().st_mode & 0o077 == 0
```

- [ ] **Step 2: Run the tests and verify missing persistence functions**

Run the focused reporting test file.

Expected: failures name `state_path`, `_initialize_store`, and `handle_session_report`.

- [ ] **Step 3: Implement the SQLite store and atomic claim**

Add `sqlite3`, `threading`, `datetime`, `timezone`, and `Path` imports. Define:

```python
_ACTIVE_REPORT_IDS: set[str] = set()
_ACTIVE_REPORTS_LOCK = threading.Lock()


def state_path(env: Mapping[str, str]) -> Path:
    override = str(env.get("CAPTAIN_AGENT_STATE_PATH", "")).strip()
    if override:
        return Path(override).expanduser()
    root = str(env.get("XDG_STATE_HOME", "")).strip()
    base = Path(root).expanduser() if root else Path.home() / ".local" / "state"
    return base / "captain-agent" / "reports.sqlite3"
```

`_initialize_store(path)` must create the parent with mode `0o700`, create the database/table, and enforce database mode `0o600` with `chmod`. Use SQLite context managers and parameterized statements only.

Implement an internal atomic claim while `_ACTIVE_REPORTS_LOCK` is held:

```text
BEGIN IMMEDIATE
no row                    -> insert status=processing; add id to active set; commit; claimed
retryable stored status   -> set processing, clear result, refresh project/time; claim
immutable stored status   -> deserialize CaptainReportResult; commit; replay
processing + active       -> commit; return queued
processing + absent       -> persist/return unknown_outcome; commit
```

The stored project is `report.project`, then `metadata.project`, then `metadata.repo`, then `Session report`.

- [ ] **Step 4: Implement `handle_session_report()` orchestration**

Use this exact order:

```python
def handle_session_report(
    report_id: Any,
    report: Any,
    metadata: Any,
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner = run_openclaw_agent,
) -> CaptainReportResult:
    active_env = os.environ if env is None else env
    validation = validate_report_input(report_id, report, metadata)
    if validation is not None:
        return validation

    path = state_path(active_env)
    _initialize_store(path)
    claimed, existing = _claim_report(path, report_id, report, metadata)
    if not claimed:
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
```

`_finish_report()` serializes `result.model_dump(mode="json")` with sorted keys and updates `status`, `result_json`, and `updated_at` in one transaction.

- [ ] **Step 5: Run the focused tests and inspect the database**

Run:

```bash
$CAPTAIN_AGENT_PYTHON \
  -m pytest tests/test_captain_agent_reporting.py -q
```

Expected: all tests pass. The tests must use only temporary databases; `git status --short` must show no runtime database.

- [ ] **Step 6: Commit local idempotency**

```bash
git add agent-plugin/captain_agent/reporting.py \
  tests/test_captain_agent_reporting.py
git commit -m "feat: make Captain report handoffs idempotent"
```

### Task 4: Package the MCP Server as a Codex/OpenClaw Plugin

**Files:**
- Create: `.agents/plugins/marketplace.json`
- Create: `agent-plugin/.codex-plugin/plugin.json`
- Create: `agent-plugin/.mcp.json`
- Create: `agent-plugin/bin/captain-agent-mcp`
- Create: `agent-plugin/captain_agent/server.py`
- Create: `tests/test_captain_agent_package.py`
- Modify: `.gitignore`
- Modify: `package.json`
- Modify: `tests/test_gitignore_privacy_contract.py`
- Modify: `tests/test_public_package_contract.py`

**Interfaces:**
- Consumes: `CaptainReportResult` and `handle_session_report()` from Tasks 1-3.
- Produces: MCP tool `captain_session_report(report_id: str, report: dict[str, Any], metadata: dict[str, Any]) -> CaptainReportResult`.
- Produces: `captain_agent.server:mcp` and `captain_agent.server:main()`.
- Produces: executable `agent-plugin/bin/captain-agent-mcp`.

- [ ] **Step 1: Add failing package and in-process MCP tests**

Create `tests/test_captain_agent_package.py`:

```python
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "agent-plugin"
sys.path.insert(0, str(PLUGIN))


def test_marketplace_points_to_local_captain_plugin():
    marketplace = json.loads(
        (ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
    )
    entry = marketplace["plugins"][0]
    assert marketplace["name"] == "captain"
    assert entry["name"] == "captain"
    assert entry["source"] == {"source": "local", "path": "./agent-plugin"}


def test_plugin_manifest_declares_skill_and_mcp_server():
    manifest = json.loads(
        (PLUGIN / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "captain"
    assert manifest["license"] == "MIT"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"


def test_mcp_manifest_uses_only_the_relative_local_launcher():
    manifest = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    assert manifest == {
        "mcpServers": {
            "captain": {
                "command": "./bin/captain-agent-mcp",
                "args": [],
                "cwd": ".",
            }
        }
    }


def test_launcher_is_executable_and_valid_shell():
    launcher = PLUGIN / "bin/captain-agent-mcp"
    assert launcher.stat().st_mode & stat.S_IXUSR
    subprocess.run(["sh", "-n", str(launcher)], check=True)


@pytest.mark.anyio
async def test_mcp_tool_returns_structured_validation_result(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPTAIN_AGENT_STATE_PATH", str(tmp_path / "reports.sqlite3"))
    from captain_agent.server import mcp

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "captain_session_report",
            {"report_id": "report-1", "report": {"summary": []}, "metadata": {}},
        )
    assert result.is_error is False
    assert result.structured_content["report_id"] == "report-1"
    assert result.structured_content["status"] == "needs_clarification"


def test_root_package_includes_marketplace_and_plugin():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert ".agents/plugins/marketplace.json" in package["files"]
    assert "agent-plugin" in package["files"]
```

- [ ] **Step 2: Run package tests and verify missing files**

Run:

```bash
$CAPTAIN_AGENT_PYTHON \
  -m pytest tests/test_captain_agent_package.py -q
```

Expected: failures identify the missing marketplace, plugin manifests, launcher, server, and package entries.

- [ ] **Step 3: Create the marketplace and plugin manifests**

Create `.agents/plugins/marketplace.json`:

```json
{
  "name": "captain",
  "interface": {"displayName": "Captain"},
  "plugins": [
    {
      "name": "captain",
      "source": {"source": "local", "path": "./agent-plugin"},
      "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
      "category": "Productivity"
    }
  ]
}
```

Create `agent-plugin/.mcp.json` with the exact object asserted by the test. Create `agent-plugin/.codex-plugin/plugin.json` by following the installed Codex manifest shape with:

```json
{
  "name": "captain",
  "version": "0.1.0",
  "description": "Report coding-agent work to your own Captain project manager.",
  "author": {
    "name": "Common Vector Robotics",
    "url": "https://github.com/Common-Vector-Robotics"
  },
  "homepage": "https://github.com/Common-Vector-Robotics/captain",
  "repository": "https://github.com/Common-Vector-Robotics/captain",
  "license": "MIT",
  "keywords": ["captain", "project-management", "mcp", "openclaw", "clickup"],
  "skills": "./skills/",
  "mcpServers": "./.mcp.json",
  "interface": {
    "displayName": "Captain",
    "shortDescription": "Report completed work to your Captain agent",
    "longDescription": "Use the /captain skill and a local MCP server to report completed coding-agent work through your configured OpenClaw Gateway.",
    "developerName": "Common Vector Robotics",
    "category": "Productivity",
    "capabilities": ["Interactive", "Write"],
    "websiteURL": "https://github.com/Common-Vector-Robotics/captain",
    "defaultPrompt": ["Report this session to my Captain agent"],
    "brandColor": "#0F766E",
    "screenshots": []
  }
}
```

- [ ] **Step 4: Create the cache-safe launcher**

Create executable `agent-plugin/bin/captain-agent-mcp`:

```sh
#!/bin/sh
set -eu

plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)

run_python() {
  cd "$plugin_root"
  exec "$1" -m captain_agent.server
}

if [ -n "${CAPTAIN_AGENT_PYTHON:-}" ]; then
  run_python "$CAPTAIN_AGENT_PYTHON"
fi

if [ -x "$plugin_root/.venv/bin/python" ]; then
  run_python "$plugin_root/.venv/bin/python"
fi

if command -v python3 >/dev/null 2>&1 && python3 -c '
from importlib.metadata import version
from mcp.server import MCPServer
raise SystemExit(0 if version("mcp").split(".", 1)[0] == "2" else 1)
' >/dev/null 2>&1; then
  run_python "$(command -v python3)"
fi

if command -v uv >/dev/null 2>&1; then
  cd "$plugin_root"
  exec uv run --quiet --no-project \
    --with-requirements "$plugin_root/requirements.txt" \
    python -m captain_agent.server
fi

printf '%s\n' \
  'Captain agent plugin needs uv or Python with agent-plugin/requirements.txt installed.' \
  >&2
exit 1
```

Run `chmod 755 agent-plugin/bin/captain-agent-mcp`.

- [ ] **Step 5: Register the one MCP tool**

Create `agent-plugin/captain_agent/server.py`:

```python
"""MCP stdio entrypoint for Captain session reports."""

from typing import Any

from mcp.server import MCPServer

from .reporting import CaptainReportResult, handle_session_report

mcp = MCPServer(
    "Captain",
    instructions=(
        "Report completed coding-agent work to the user's configured Captain agent."
    ),
)


@mcp.tool()
def captain_session_report(
    report_id: str,
    report: dict[str, Any],
    metadata: dict[str, Any],
) -> CaptainReportResult:
    """Send one idempotent, redacted session report to Captain."""

    return handle_session_report(report_id, report, metadata)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Include and privacy-scan the plugin package**

Add `.agents/plugins/marketplace.json` and `agent-plugin` to `package.json#files`. Add these lines to `.gitignore`:

```text
# Optional Captain coding-agent plugin environment and local state
agent-plugin/.venv/
agent-plugin/*.sqlite3*
```

Change `product_text_paths()` in `tests/test_public_package_contract.py` so a packaged directory is traversed:

```python
def product_text_paths():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    paths = []
    for name in package["files"]:
        path = ROOT / name
        if path.is_dir():
            paths.extend(
                child for child in path.rglob("*")
                if child.is_file() and child.suffix in TEXT_SUFFIXES
            )
        elif path.is_file() and path.suffix in TEXT_SUFFIXES:
            paths.append(path)
    paths.extend((ROOT / "README.md", ROOT / "BOOTSTRAP.md"))
    return sorted(set(paths))
```

Add `".sh"` to `TEXT_SUFFIXES` so launchers are also scanned.

Add these two cases to the runtime-private parameter list in
`tests/test_gitignore_privacy_contract.py`:

```python
"agent-plugin/.venv/bin/python",
"agent-plugin/local.sqlite3",
```

- [ ] **Step 7: Run package and public-safety tests**

Run:

```bash
$CAPTAIN_AGENT_PYTHON -m pytest \
  tests/test_captain_agent_package.py \
  tests/test_public_package_contract.py \
  tests/test_gitignore_privacy_contract.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit the installable plugin package**

```bash
git add .agents agent-plugin/.codex-plugin agent-plugin/.mcp.json \
  agent-plugin/bin agent-plugin/captain_agent/server.py .gitignore package.json \
  tests/test_captain_agent_package.py tests/test_gitignore_privacy_contract.py \
  tests/test_public_package_contract.py
git commit -m "feat: package the Captain agent plugin"
```

### Task 5: Add the `/captain` Skill and User Documentation

**Files:**
- Create: `agent-plugin/skills/captain/SKILL.md`
- Create: `agent-plugin/README.md`
- Modify: `README.md`
- Modify: `tests/test_captain_agent_package.py`

**Interfaces:**
- Consumes: MCP tool `captain_session_report` from Task 4.
- Produces: user-invoked `/captain` workflow with stable report ID, redaction, immediate send, and canonical result rendering.

- [ ] **Step 1: Add failing skill and documentation contract tests**

Append:

```python
def test_captain_skill_uses_only_the_local_tool():
    skill = (PLUGIN / "skills/captain/SKILL.md").read_text(encoding="utf-8")
    assert "name: captain" in skill
    assert "captain_session_report" in skill
    assert "report_id" in skill
    assert "unknown_outcome" in skill
    assert "authenticated_email" not in skill
    assert "Google OAuth" not in skill


def test_plugin_docs_make_the_gateway_topology_explicit():
    docs = (PLUGIN / "README.md").read_text(encoding="utf-8")
    assert "coding agent → local MCP process → local OpenClaw CLI" in docs
    assert "configured Gateway → Captain → ClickUp" in docs
    assert "Gateway and Captain agent can run on that machine or on a remote host" in docs
    assert "codex plugin marketplace add Common-Vector-Robotics/captain --ref main" in docs
    assert "codex plugin add captain@captain" in docs
    assert "openclaw plugins install ./agent-plugin" in docs
```

- [ ] **Step 2: Run package tests and verify missing docs**

Run the package test file.

Expected: failures identify the missing skill and plugin README.

- [ ] **Step 3: Write the portable `/captain` skill**

Create `agent-plugin/skills/captain/SKILL.md` with frontmatter:

```yaml
---
name: captain
description: Report this coding session to the user's Captain agent so Captain can reconcile the work into ClickUp.
---
```

The body must instruct the calling agent to:

1. Gather Git root, branch/upstream, short status, recent commits, diff stats, completed work, changed files, verification actually run, decisions, blockers, risks, and next steps once.
2. Use a host session identifier directly only when it matches `[A-Za-z0-9._-]{1,128}`. Hash an unsafe identifier into stable `captain-<sha256>` form without exposing it; use a UUID only when no host identifier exists. Reuse the safe ID for every retry.
3. Exclude tokens, passwords, private keys, OAuth material, credentialed URLs, customer PII, unrelated personal data, and raw transcripts.
4. Inspect the host catalog and call exactly one available name: `Captain:captain_session_report` in Codex or `captain__captain_session_report` in OpenClaw. Neither or both returns `needs_configuration`; never guess or call both. Never call Captain, ClickUp, or a private endpoint directly.
5. Wait for a terminal result. Do not claim `queued` is complete. Treat `unknown_outcome` as uncertain and advise checking ClickUp before any new report identifier is used.
6. Render one of `CAPTAIN REPORT SENT`, `CAPTAIN REPORT FAILED`, `CAPTAIN OUTCOME UNKNOWN`, or `CAPTAIN REPORT NOT SENT`, followed by status, ClickUp summary, Captain feedback, questions, warnings, and safe retry guidance.

Use the concise report object from the design spec. Do not include a user email or identity claim in the tool arguments.

- [ ] **Step 4: Write installation and operation documentation**

Create `agent-plugin/README.md` with:

- the exact local-MCP/configured-Gateway data path asserted by the test;
- prerequisites: a local OpenClaw CLI configured for a local or remote Gateway with the `captain` agent installed, and either `uv` or Python 3 with nested requirements installed;
- Codex marketplace commands from the test;
- `openclaw plugins install ./agent-plugin` for a cloned repository;
- generic MCP-host command `./agent-plugin/bin/captain-agent-mcp`;
- optional venv setup:

```bash
python3 -m venv agent-plugin/.venv
agent-plugin/.venv/bin/python -m pip install -r agent-plugin/requirements.txt
```

- configuration overrides and exact defaults;
- local state paths and `CAPTAIN_AGENT_STATE_PATH`;
- troubleshooting for missing `openclaw`, missing MCP SDK/`uv`, `needs_configuration`, and `unknown_outcome`.

Add a short “Report coding-agent work with `/captain`” section to root `README.md` linking to `agent-plugin/README.md`. Keep the full setup details in the nested README.

- [ ] **Step 5: Run focused documentation and privacy tests**

Run:

```bash
$CAPTAIN_AGENT_PYTHON -m pytest \
  tests/test_captain_agent_package.py tests/test_public_package_contract.py -q
```

Expected: all tests pass and the public scanner reports no private deployment literals.

- [ ] **Step 6: Commit the skill and docs**

```bash
git add agent-plugin/skills agent-plugin/README.md README.md \
  tests/test_captain_agent_package.py
git commit -m "docs: add Captain reporting workflow"
```

### Task 6: Verify the Release and Open the GitHub Pull Request

**Files:**
- Modify if verification exposes defects: only files already in this plan.
- No live runtime credentials or state may be added.

**Interfaces:**
- Consumes: the complete plugin, skill, documentation, and tests from Tasks 1-5.
- Produces: pushed branch `codex/captain-agent-plugin` and a PR into `Common-Vector-Robotics/captain:main`.

- [ ] **Step 1: Run static format and manifest checks**

```bash
git diff --check origin/main...HEAD
python3 -m json.tool .agents/plugins/marketplace.json >/dev/null
python3 -m json.tool agent-plugin/.codex-plugin/plugin.json >/dev/null
python3 -m json.tool agent-plugin/.mcp.json >/dev/null
sh -n agent-plugin/bin/captain-agent-mcp
$CAPTAIN_AGENT_PYTHON \
  -m compileall -q agent-plugin/captain_agent
```

Expected: every command exits zero with no output other than normal command summaries.

- [ ] **Step 2: Run focused tests**

```bash
$CAPTAIN_AGENT_PYTHON -m pytest \
  tests/test_captain_agent_reporting.py tests/test_captain_agent_package.py -q
```

Expected: all focused tests pass.

- [ ] **Step 3: Run the complete Captain suite**

```bash
$CAPTAIN_AGENT_PYTHON -m pytest -q
```

Expected: the baseline 123 tests plus 2 subtests and all new tests pass.

- [ ] **Step 4: Verify the published package contents**

```bash
npm pack --dry-run --json
```

Inspect the JSON and verify it includes `.agents/plugins/marketplace.json` and every tracked file under `agent-plugin/`, while excluding `.venv`, SQLite files, secrets, logs, memory, and reports. The design and implementation plan remain repository documentation and do not need to ship in the npm package.

- [ ] **Step 5: Run an MCP protocol smoke test**

```bash
npx -y @modelcontextprotocol/inspector --cli \
  ./agent-plugin/bin/captain-agent-mcp --method tools/list
```

Expected: the Inspector connects over `stdio` and lists exactly one product tool named `captain_session_report`. This does not invoke OpenClaw or write ClickUp.

- [ ] **Step 6: Review the complete diff and commit any verification-only fixes**

```bash
git status --short
git diff --stat origin/main...HEAD
git diff origin/main...HEAD
git log --oneline origin/main..HEAD
```

Confirm there are no private host paths, credentials, hosted gateway URLs, unrelated edits, or uncommitted files. If a focused correction was required, rerun its failing test, the focused suite, and the full suite before committing it with a narrow message.

- [ ] **Step 7: Incorporate the latest upstream main and rerun verification**

```bash
git fetch --prune origin
git rebase origin/main
```

If the rebase changed `HEAD`, rerun Steps 1-5. Confirm:

```bash
git rev-list --left-right --count HEAD...origin/main
```

Expected before push: the right-hand count is `0`; only this branch's reviewed commits are ahead.

- [ ] **Step 8: Push the feature branch**

```bash
git push -u origin codex/captain-agent-plugin
```

Expected: GitHub reports the new branch under `Common-Vector-Robotics/captain`.

- [ ] **Step 9: Create the PR against `main`**

```bash
gh pr create \
  --repo Common-Vector-Robotics/captain \
  --base main \
  --head codex/captain-agent-plugin \
  --title "Add a Captain coding-agent plugin" \
  --body-file /tmp/captain-agent-plugin-pr.md
```

Create `/tmp/captain-agent-plugin-pr.md` with `apply_patch` before running the command. The body must summarize the local MCP and CLI boundary, configured local-or-remote Gateway routing, one-tool MCP contract, idempotency and uncertainty behavior, installation paths, focused/full test counts, MCP Inspector result, and whether a live ClickUp write was separately authorized and run.

- [ ] **Step 10: Inspect the PR and initial checks**

```bash
gh pr view --repo Common-Vector-Robotics/captain --json number,url,title,baseRefName,headRefName,state
gh pr checks --repo Common-Vector-Robotics/captain
```

Report the PR URL, exact branch and commits, latest-base proof, focused/full verification, package/Inspector evidence, hosted CI result, live-write status, and unrelated-change status. Do not merge the PR unless the user separately asks.
