import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from install_heartbeat_policy import install_policy


def _run_heartbeat_install(tmp_path, *, alter_readback=False):
    policy_bytes = "# policy\nHARD GATE — exact UTF-8\n".encode("utf-8")
    (tmp_path / "HEARTBEAT.md").write_bytes(policy_bytes)
    calls = []
    state = {}

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["openclaw", "config", "set"]:
            decoded = json.loads(command[4])
            if "--dry-run" not in command:
                state["prompt"] = decoded
            return subprocess.CompletedProcess(command, 0)
        assert command[:3] == ["openclaw", "config", "get"]
        prompt = state["prompt"] + ("changed" if alter_readback else "")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(prompt))

    verified_hash = install_policy(
        tmp_path / "HEARTBEAT.md",
        run=fake_run,
    )
    return policy_bytes, calls, state, verified_hash


def test_claw_references_its_openclaw_runtime_profile():
    manifest = (ROOT / "CLAW.md").read_text(encoding="utf-8")
    assert re.search(
        r"(?m)^metadata:\n  openclaw\.config: profiles/openclaw\.yml$",
        manifest,
    )


def test_claw_owns_exact_scheduled_jobs():
    manifest = (ROOT / "CLAW.md").read_text(encoding="utf-8")
    ids = set(re.findall(r"^  - id: ([a-z0-9-]+)$", manifest, re.MULTILINE))
    assert ids == {
        "morning-cycle", "meeting-transcript-reconciliation", "blocker-chase",
        "bench-truth-watch", "eod-wrap", "action-summary-reporting",
    }
    assert "timezone:" not in manifest


def test_runtime_code_contains_no_fixed_deployment_timezone():
    runtime_paths = (
        "scripts/daily_activity_digest.py",
        "scripts/daily_context.py",
        "scripts/daily_wrap.py",
        "scripts/openclaw_cron_sentry_bridge.py",
        "scripts/personal_top2.py",
    )
    for relative_path in runtime_paths:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "America/Detroit" not in source


def test_profile_declares_hourly_isolated_lightweight_heartbeat():
    profile = (ROOT / "profiles" / "openclaw.yml").read_text(encoding="utf-8")
    assert re.search(
        r"(?ms)^  heartbeat:\n    every: 60m\n    lightContext: true\n"
        r"    isolatedSession: true\n    timeoutSeconds: 120$",
        profile,
    )


def test_claw_profile_leaves_operator_heartbeat_prompt_outside_its_digest():
    profile = (ROOT / "profiles" / "openclaw.yml").read_text(encoding="utf-8")
    assert "    prompt:" not in profile


def test_heartbeat_policy_is_packaged_for_active_mode_loading():
    manifest = (ROOT / "CLAW.md").read_text(encoding="utf-8")
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert re.search(
        r"(?m)^    HEARTBEAT\.md:\n      source: HEARTBEAT\.md$",
        manifest,
    )
    assert "HEARTBEAT.md" in package["files"]


def test_setup_docs_reference_packaged_heartbeat_installer():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    bootstrap = (ROOT / "BOOTSTRAP.md").read_text(encoding="utf-8")

    for document in (readme, bootstrap):
        assert "python3 scripts/install_heartbeat_policy.py" in document
        assert 'policy_path = Path("HEARTBEAT.md")' not in document

    script = (ROOT / "scripts" / "install_heartbeat_policy.py").read_text(encoding="utf-8")
    assert 'CONFIG_PATH = "agents.entries.captain.heartbeat.prompt"' in script
    assert 'json.dumps(policy, ensure_ascii=False)' in script
    assert 'run([*set_command, "--dry-run"], check=True)' in script
    assert "run(set_command, check=True)" in script
    assert '"config", "get", CONFIG_PATH, "--json"' in script
    assert "actual_bytes != policy_bytes" in script
    assert "shell=True" not in script
    assert script.index('"--dry-run"') < script.index("run(set_command")
    assert script.index("run(set_command") < script.index('"config", "get"')


def test_heartbeat_installer_dry_runs_applies_and_reads_back_exactly(
    tmp_path,
):
    policy_bytes, calls, state, verified_hash = _run_heartbeat_install(tmp_path)

    assert len(calls) == 3
    dry_run, apply, read_back = (call[0] for call in calls)
    assert dry_run[-2:] == ["--strict-json", "--dry-run"]
    assert apply[-1] == "--strict-json"
    assert "--dry-run" not in apply
    assert read_back == [
        "openclaw", "config", "get",
        "agents.entries.captain.heartbeat.prompt", "--json",
    ]
    assert state["prompt"].encode("utf-8") == policy_bytes
    assert all(call[1].get("check") is True for call in calls)
    assert verified_hash == hashlib.sha256(policy_bytes).hexdigest()


def test_heartbeat_installer_fails_closed_on_readback_mismatch(
    tmp_path,
):
    with pytest.raises(SystemExit, match="does not exactly match"):
        _run_heartbeat_install(
            tmp_path,
            alter_readback=True,
        )
