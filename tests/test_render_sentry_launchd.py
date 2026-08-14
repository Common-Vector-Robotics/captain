import plistlib
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from render_sentry_launchd import (
    build_launchd_config,
    build_systemd_units,
    write_plist,
    write_systemd_units,
)


def test_build_launchd_config_uses_supplied_absolute_paths(tmp_path):
    workspace = tmp_path / "workspace-captain"
    python = tmp_path / "venv" / "bin" / "python"
    config = build_launchd_config(workspace, python, "/opt/homebrew/bin:/usr/bin:/bin")
    assert config["Label"] == "ai.openclaw.captain-sentry-bridge"
    assert config["WorkingDirectory"] == str(workspace.resolve())
    assert config["ProgramArguments"] == [str(python.resolve()), "scripts/openclaw_cron_sentry_bridge.py"]
    assert config["StartInterval"] == 600
    assert config["Umask"] == 0o077
    assert config["StandardOutPath"] == str(workspace.resolve() / "logs" / "sentry-bridge.out.log")


def test_write_plist_is_private_and_parseable(tmp_path):
    output = tmp_path / "LaunchAgents" / "ai.openclaw.captain-sentry-bridge.plist"
    config = build_launchd_config(tmp_path / "workspace", Path(sys.executable), "/usr/bin:/bin")
    write_plist(output, config)
    assert plistlib.loads(output.read_bytes()) == config
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_build_systemd_units_runs_bridge_every_ten_minutes(tmp_path):
    workspace = (tmp_path / "workspace captain").resolve()
    python = (tmp_path / "venv" / "bin" / "python").resolve()
    units = build_systemd_units(workspace, python, "/usr/local/bin:/usr/bin:/bin")

    service = units["ai.openclaw.captain-sentry-bridge.service"]
    assert f'WorkingDirectory="{workspace}"' in service
    assert f'ExecStart="{python}" "{workspace}/scripts/openclaw_cron_sentry_bridge.py"' in service
    assert "UMask=0077" in service
    assert 'Environment="PATH=/usr/local/bin:/usr/bin:/bin"' in service

    timer = units["ai.openclaw.captain-sentry-bridge.timer"]
    assert "OnBootSec=0" in timer
    assert "OnUnitInactiveSec=10min" in timer
    assert "WantedBy=timers.target" in timer


def test_write_systemd_units_is_private(tmp_path):
    units = build_systemd_units(tmp_path / "workspace", Path(sys.executable), "/usr/bin:/bin")
    output = tmp_path / ".config" / "systemd" / "user"
    write_systemd_units(output, units)

    assert {path.name for path in output.iterdir()} == set(units)
    for path in output.iterdir():
        assert path.read_text(encoding="utf-8") == units[path.name]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
