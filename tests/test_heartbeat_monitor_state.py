import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import heartbeat_monitor_state


FIXED_NOW = "2026-08-13T12:34:56+00:00"


def test_record_sweep_preserves_fields_and_writes_exact_current_state(tmp_path):
    state_path = tmp_path / "private" / "heartbeat-monitor-state.json"
    state_path.parent.mkdir(mode=0o700)
    state_path.write_text('{"incident":"preserved","channels_scanned":99}\n', encoding="utf-8")

    result = heartbeat_monitor_state.record_sweep(
        enumeration_unavailable=False,
        channels_scanned=3,
        state_path=state_path,
        now_fn=lambda: FIXED_NOW,
    )

    assert result == {
        "incident": "preserved",
        "last_run_at": FIXED_NOW,
        "channel_enumeration_unavailable": False,
        "channels_scanned": 3,
    }
    assert json.loads(state_path.read_text(encoding="utf-8")) == result
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_record_sweep_tightens_owner_controlled_parent_to_0700(tmp_path):
    parent = tmp_path / "state"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)

    heartbeat_monitor_state.record_sweep(
        enumeration_unavailable=False,
        channels_scanned=0,
        state_path=parent / "heartbeat-monitor-state.json",
        now_fn=lambda: FIXED_NOW,
    )

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_main_prints_only_current_sweep_fields(tmp_path, capsys):
    state_path = tmp_path / "state" / "heartbeat-monitor-state.json"

    heartbeat_monitor_state.main(
        [
            "--state-path", str(state_path),
            "--channel-enumeration-unavailable", "true",
            "--channels-scanned", "0",
        ],
        now_fn=lambda: FIXED_NOW,
    )

    assert json.loads(capsys.readouterr().out) == {
        "channel_enumeration_unavailable": True,
        "channels_scanned": 0,
        "last_run_at": FIXED_NOW,
    }


@pytest.mark.parametrize("channels_scanned", [-1, True, "1"])
def test_record_sweep_rejects_invalid_channel_counts(tmp_path, channels_scanned):
    with pytest.raises(ValueError, match="non-negative integer"):
        heartbeat_monitor_state.record_sweep(
            enumeration_unavailable=False,
            channels_scanned=channels_scanned,
            state_path=tmp_path / "state.json",
        )


def test_record_sweep_rejects_malformed_existing_state_without_changing_it(tmp_path):
    state_path = tmp_path / "heartbeat-monitor-state.json"
    original = b"not-json\n"
    state_path.write_bytes(original)

    with pytest.raises(ValueError, match="existing heartbeat monitor state"):
        heartbeat_monitor_state.record_sweep(
            enumeration_unavailable=True,
            channels_scanned=0,
            state_path=state_path,
        )

    assert state_path.read_bytes() == original


@pytest.mark.parametrize("failure_stage", ["write", "fsync", "replace"])
def test_atomic_failure_preserves_original_and_removes_temp(
    tmp_path, monkeypatch, failure_stage
):
    state_path = tmp_path / "heartbeat-monitor-state.json"
    state_path.write_bytes(b'{"preserved":true}\n')

    if failure_stage == "write":
        real_fdopen = os.fdopen

        class FailingWriter:
            def __init__(self, fd, *args, **kwargs):
                self.file = real_fdopen(fd, *args, **kwargs)

            def __enter__(self):
                return self

            def write(self, _value):
                raise OSError("write failed")

            def __exit__(self, *exc_info):
                self.file.close()

        monkeypatch.setattr(os, "fdopen", FailingWriter)
    elif failure_stage == "fsync":
        monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync failed")))
    else:
        monkeypatch.setattr(
            os,
            "replace",
            lambda _source, _destination: (_ for _ in ()).throw(OSError("replace failed")),
        )

    original = state_path.read_bytes()
    with pytest.raises(OSError, match=f"{failure_stage} failed"):
        heartbeat_monitor_state.record_sweep(
            enumeration_unavailable=True,
            channels_scanned=0,
            state_path=state_path,
        )

    assert state_path.read_bytes() == original
    assert sorted(item.name for item in tmp_path.iterdir()) == [state_path.name]


def test_temp_is_random_owner_only_and_beside_target(tmp_path, monkeypatch):
    state_path = tmp_path / "heartbeat-monitor-state.json"
    state_path.write_text("{}\n", encoding="utf-8")
    observed = []

    def inspect_replace(source, destination):
        staged = Path(source)
        observed.append(
            (
                staged.parent,
                staged.name,
                stat.S_IMODE(staged.stat().st_mode),
                Path(destination),
            )
        )
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", inspect_replace)
    for _ in range(2):
        with pytest.raises(OSError, match="replace failed"):
            heartbeat_monitor_state.record_sweep(
                enumeration_unavailable=False,
                channels_scanned=1,
                state_path=state_path,
            )

    assert all(parent == state_path.parent for parent, _, _, _ in observed)
    assert all(destination == state_path for _, _, _, destination in observed)
    assert all(mode == 0o600 for _, _, mode, _ in observed)
    assert all(
        re.fullmatch(r"\.heartbeat-monitor-state\.json\.[A-Za-z0-9_-]+\.tmp", name)
        for _, name, _, _ in observed
    )
    assert len({name for _, name, _, _ in observed}) == 2
    assert sorted(item.name for item in tmp_path.iterdir()) == [state_path.name]


def test_record_sweep_rejects_unsafe_or_symlinked_storage(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    with pytest.raises(OSError, match="owner-controlled directory"):
        heartbeat_monitor_state.record_sweep(
            enumeration_unavailable=True,
            channels_scanned=0,
            state_path=shared / "state.json",
        )

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    link_parent = tmp_path / "linked"
    link_parent.symlink_to(private, target_is_directory=True)
    with pytest.raises(OSError, match="symlink"):
        heartbeat_monitor_state.record_sweep(
            enumeration_unavailable=True,
            channels_scanned=0,
            state_path=link_parent / "state.json",
        )

    target = private / "state.json"
    target.symlink_to(private / "other.json")
    with pytest.raises(OSError, match="symlink"):
        heartbeat_monitor_state.record_sweep(
            enumeration_unavailable=True,
            channels_scanned=0,
            state_path=target,
        )


def test_monitor_helper_is_shipped_but_mutable_state_is_not_claw_managed():
    manifest = (ROOT / "CLAW.md").read_text(encoding="utf-8")
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    for source in (
        "scripts/heartbeat_monitor_state.py",
        "tests/test_heartbeat_monitor_state.py",
    ):
        assert f"source: {source}" in manifest
        assert source in package["files"]
    assert "source: data/heartbeat-monitor-state.json" not in manifest
    assert "data/heartbeat-monitor-state.json" not in package["files"]


@pytest.mark.parametrize(
    "runtime_path",
    [
        "data/heartbeat-monitor-state.json",
        "data/.heartbeat-monitor-state.json.abcd1234.tmp",
    ],
)
def test_monitor_runtime_files_are_gitignored(runtime_path):
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", runtime_path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
