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
import captain_modes


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_load_toggle_users_inverts_private_name_mapping(tmp_path, monkeypatch):
    path = tmp_path / "captain-channels.json"
    write_json(path, {"mode_toggle_users": {"Operator": "U0123456789"}})
    monkeypatch.setattr(captain_modes, "CHANNELS_PATH", path)
    assert captain_modes.load_toggle_users() == {"U0123456789": "Operator"}


@pytest.mark.parametrize("value", [None, {}, [], {"Operator": ""}])
def test_load_toggle_users_fails_closed(tmp_path, monkeypatch, value):
    path = tmp_path / "captain-channels.json"
    write_json(path, {} if value is None else {"mode_toggle_users": value})
    monkeypatch.setattr(captain_modes, "CHANNELS_PATH", path)
    with pytest.raises(SystemExit, match="mode_toggle_users"):
        captain_modes.load_toggle_users()


def test_set_dailyloop_rejects_blank_or_unknown_user(tmp_path, monkeypatch):
    channels = tmp_path / "captain-channels.json"
    modes = tmp_path / "captain-modes.json"
    write_json(channels, {"mode_toggle_users": {"Operator": "U0123456789"}})
    monkeypatch.setattr(captain_modes, "CHANNELS_PATH", channels)
    monkeypatch.setattr(captain_modes, "MODE_PATH", modes)
    monkeypatch.setattr(captain_modes, "init_db", lambda: None)
    monkeypatch.setattr(captain_modes, "audit", lambda *args, **kwargs: None)
    for user_id in ("", "U9999999999"):
        with pytest.raises(SystemExit, match="Unauthorized"):
            captain_modes.set_dailyloop("shadow", user_id, "test")


def test_set_dailyloop_records_configured_operator(tmp_path, monkeypatch):
    channels = tmp_path / "captain-channels.json"
    modes = tmp_path / "captain-modes.json"
    events = []
    write_json(channels, {"mode_toggle_users": {"Operator": "U0123456789"}})
    monkeypatch.setattr(captain_modes, "CHANNELS_PATH", channels)
    monkeypatch.setattr(captain_modes, "MODE_PATH", modes)
    monkeypatch.setattr(captain_modes, "init_db", lambda: None)
    monkeypatch.setattr(
        captain_modes,
        "audit",
        lambda event, **fields: events.append((event, fields)),
    )
    result = captain_modes.set_dailyloop("shadow", "U0123456789", "test")
    assert result["DailyLoop"]["updated_by"] == "Operator"
    assert json.loads(modes.read_text(encoding="utf-8")) == result
    assert len(events) == 1
    assert events[0][0] == "captain_mode_toggle"
    assert events[0][1]["display_name"] == "Operator"
    assert events[0][1]["source"] == "test"
    assert events[0][1].get("phase") == "precommit"
    assert events[0][1].get("state_authoritative") is True
    assert events[0][1].get("authoritative_state") == "mode_file"


def test_set_dailyloop_does_not_change_mode_file_when_audit_fails(
    tmp_path, monkeypatch
):
    """Catch an unaudited mode transition becoming live after audit failure."""
    channels = tmp_path / "captain-channels.json"
    modes = tmp_path / "captain-modes.json"
    original = b'{"DailyLoop":{"audience":"off"},"Other":{"enabled":true}}\n'
    write_json(channels, {"mode_toggle_users": {"Operator": "U0123456789"}})
    modes.write_bytes(original)
    monkeypatch.setattr(captain_modes, "CHANNELS_PATH", channels)
    monkeypatch.setattr(captain_modes, "MODE_PATH", modes)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(captain_modes, "init_db", lambda: None)
    monkeypatch.setattr(captain_modes, "audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        captain_modes.set_dailyloop("live", "U0123456789", "test")

    assert modes.read_bytes() == original


def test_save_modes_is_owner_only_under_permissive_umask(tmp_path, monkeypatch):
    """Catch mode-state creation that inherits group or world permissions."""
    modes = tmp_path / "captain-modes.json"
    monkeypatch.setattr(captain_modes, "MODE_PATH", modes)
    previous_umask = os.umask(0o022)
    try:
        captain_modes.save_modes({"DailyLoop": {"audience": "shadow"}})
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(modes.stat().st_mode) == 0o600


def test_save_modes_creates_owner_only_parent(tmp_path, monkeypatch):
    """Catch a new mode-state directory that is visible to other users."""
    modes = tmp_path / "private-state" / "captain-modes.json"
    monkeypatch.setattr(captain_modes, "MODE_PATH", modes)
    previous_umask = os.umask(0o022)
    try:
        captain_modes.save_modes({"DailyLoop": {"audience": "shadow"}})
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(modes.parent.stat().st_mode) == 0o700


def test_save_modes_rejects_parent_writable_by_other_users(tmp_path, monkeypatch):
    """Catch staging mode state in a directory another user can replace."""
    parent = tmp_path / "shared-state"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    modes = parent / "captain-modes.json"
    monkeypatch.setattr(captain_modes, "MODE_PATH", modes)

    with pytest.raises(OSError, match="owner-controlled directory"):
        captain_modes.save_modes({"DailyLoop": {"audience": "live"}})

    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("failure_stage", ["write", "fsync", "replace"])
def test_save_modes_failure_preserves_original_and_removes_temp(
    tmp_path, monkeypatch, failure_stage
):
    """Catch partial state or temp-file residue from any commit-stage failure."""
    modes = tmp_path / "captain-modes.json"
    original = b'{"DailyLoop":{"audience":"off"}}\n'
    modes.write_bytes(original)
    monkeypatch.setattr(captain_modes, "MODE_PATH", modes)

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
        def fail_fsync(_fd):
            raise OSError("fsync failed")

        monkeypatch.setattr(os, "fsync", fail_fsync)
    else:
        def fail_replace(_source, _destination):
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match=f"{failure_stage} failed"):
        captain_modes.save_modes({"DailyLoop": {"audience": "live"}})

    assert modes.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == [modes.name]


def test_save_modes_stages_unpredictable_owner_only_temp_beside_target(
    tmp_path, monkeypatch
):
    """Catch predictable, cross-directory, or broadly readable staging files."""
    modes = tmp_path / "captain-modes.json"
    original = b'{"DailyLoop":{"audience":"off"}}\n'
    modes.write_bytes(original)
    monkeypatch.setattr(captain_modes, "MODE_PATH", modes)
    observed = []

    def inspect_replace(source, destination):
        staged = Path(source)
        observed.append(
            {
                "parent": staged.parent,
                "name": staged.name,
                "mode": stat.S_IMODE(staged.stat().st_mode),
                "destination": Path(destination),
            }
        )
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", inspect_replace)

    for audience in ("shadow", "live"):
        with pytest.raises(OSError, match="replace failed"):
            captain_modes.save_modes({"DailyLoop": {"audience": audience}})

    assert {item["parent"] for item in observed} == {modes.parent}
    assert {item["destination"] for item in observed} == {modes}
    assert all(
        re.fullmatch(r"\.captain-modes\.json\.[A-Za-z0-9_-]+\.tmp", item["name"])
        for item in observed
    )
    assert len({item["name"] for item in observed}) == 2
    assert {item["mode"] for item in observed} == {0o600}
    assert modes.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == [modes.name]


def test_mutable_mode_state_is_not_claw_managed():
    manifest = (ROOT / "CLAW.md").read_text(encoding="utf-8")
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert "source: data/captain-modes.json" not in manifest
    assert "data/captain-modes.json" not in package["files"]
    assert "data/captain-modes.example.json" in package["files"]


def test_mutable_mode_state_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "data/captain-modes.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "data/captain-modes.json"


def test_mode_state_staging_files_are_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "data/.captain-modes.json.abcd1234.tmp"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "data/.captain-modes.json.abcd1234.tmp"
