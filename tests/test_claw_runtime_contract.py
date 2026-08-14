import json
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _heartbeat_install_block(document: str) -> str:
    section = document.split("#### Install the operator-owned heartbeat policy", 1)[1]
    return section.split("```bash\n", 1)[1].split("\n```", 1)[0]


def _heartbeat_install_source(document: str) -> str:
    block = _heartbeat_install_block(document)
    assert block.startswith("python3 - <<'PY'\n")
    assert block.endswith("\nPY")
    return block.removeprefix("python3 - <<'PY'\n").removesuffix("\nPY")


def _run_documented_heartbeat_install(tmp_path, monkeypatch, *, alter_readback=False):
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

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    source = _heartbeat_install_source(readme)
    exec(compile(source, "README.md heartbeat installer", "exec"), {})
    return policy_bytes, calls, state


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


def test_setup_docs_install_and_verify_operator_heartbeat_prompt_fail_closed():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    bootstrap = (ROOT / "BOOTSTRAP.md").read_text(encoding="utf-8")

    for document in (readme, bootstrap):
        normalized = " ".join(document.split())
        assert "`lightContext: true`" in normalized
        assert "the Claw profile schema does not own `heartbeat.prompt`" in normalized
        assert (
            "Do not enable or run Captain's heartbeat or scheduled jobs until this "
            "verification succeeds."
        ) in normalized
        assert "Reapply and reverify the prompt after every Claw update" in normalized
        assert "operator-controlled modification" in normalized

        assert "```bash\npython3 - <<'PY'\n" in document
        source = _heartbeat_install_source(document)
        assert 'policy_path = Path("HEARTBEAT.md").resolve(strict=True)' in source
        assert "policy_bytes = policy_path.read_bytes()" in source
        assert 'policy = policy_bytes.decode("utf-8")' in source
        assert "json.dumps(policy, ensure_ascii=False)" in source
        assert '"agents.entries.captain.heartbeat.prompt"' in source
        assert '"--strict-json"' in source
        assert 'subprocess.run([*set_command, "--dry-run"], check=True)' in source
        assert "subprocess.run(set_command, check=True)" in source
        assert '"config", "get", config_path, "--json"' in source
        assert "actual_bytes != policy_bytes" in source
        assert 'hashlib.sha256(policy_bytes).hexdigest()' in source
        assert 'hashlib.sha256(actual_bytes).hexdigest()' in source
        assert "shell=True" not in source
        assert source.index('"--dry-run"') < source.index("subprocess.run(set_command")
        assert source.index("subprocess.run(set_command") < source.index('"config", "get"')

    assert _heartbeat_install_block(readme) == _heartbeat_install_block(bootstrap)
    fail_closed = "Do not enable or run Captain's heartbeat or scheduled jobs"
    assert readme.index(fail_closed) < readme.index("openclaw claws add . --yes")
    assert bootstrap.index(fail_closed) < bootstrap.index("1. Install")


def test_documented_heartbeat_installer_dry_runs_applies_and_reads_back_exactly(
    tmp_path, monkeypatch
):
    policy_bytes, calls, state = _run_documented_heartbeat_install(tmp_path, monkeypatch)

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


def test_documented_heartbeat_installer_fails_closed_on_readback_mismatch(
    tmp_path, monkeypatch
):
    with pytest.raises(SystemExit, match="does not exactly match"):
        _run_documented_heartbeat_install(
            tmp_path,
            monkeypatch,
            alter_readback=True,
        )
