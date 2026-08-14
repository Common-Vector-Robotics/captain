import plistlib
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from render_sentry_launchd import build_launchd_config, write_plist


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
