"""Exercise the OpenCode installer without touching a live configuration."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "agent-plugin"
sys.path.insert(0, str(PLUGIN))

from captain_agent.opencode_install import (
    InstallConflict,
    config_root,
    expected_server,
    install_opencode,
)


@dataclass
class FakeOpenCode:
    """Control and inspect a subprocess-compatible fake OpenCode CLI."""

    command: Path
    state: Path
    calls: Path
    add_exit_code: int = 0

    def environment(self, config_home: Path) -> dict[str, str]:
        """Return a complete process environment for installer calls."""

        return {
            **os.environ,
            "XDG_CONFIG_HOME": str(config_home),
            "CAPTAIN_TEST_OPENCODE_STATE": str(self.state),
            "CAPTAIN_TEST_OPENCODE_CALLS": str(self.calls),
            "CAPTAIN_TEST_OPENCODE_ADD_EXIT": str(self.add_exit_code),
        }

    def set_resolved_config(self, config: dict) -> None:
        """Choose the JSON returned by `debug config --pure`."""

        self.state.write_text(json.dumps(config), encoding="utf-8")

    @property
    def recorded_calls(self) -> list[list[str]]:
        """Return argument arrays recorded by mutating fake CLI calls."""

        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]


@pytest.fixture
def fake_opencode(tmp_path: Path) -> FakeOpenCode:
    """Create a fake CLI whose reads and writes use explicit test files."""

    command = tmp_path / "fake-opencode"
    state = tmp_path / "resolved-config.json"
    calls = tmp_path / "calls.jsonl"
    state.write_text("{}", encoding="utf-8")
    command.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
state = Path(os.environ["CAPTAIN_TEST_OPENCODE_STATE"])
calls = Path(os.environ["CAPTAIN_TEST_OPENCODE_CALLS"])

if arguments == ["debug", "config", "--pure"]:
    print(state.read_text(encoding="utf-8"))
    raise SystemExit(0)

with calls.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments) + "\\n")

if arguments[:4] == ["mcp", "add", "captain", "--"]:
    raise SystemExit(int(os.environ["CAPTAIN_TEST_OPENCODE_ADD_EXIT"]))

print("unexpected fake OpenCode arguments", file=sys.stderr)
raise SystemExit(3)
""",
        encoding="utf-8",
    )
    command.chmod(command.stat().st_mode | stat.S_IXUSR)
    return FakeOpenCode(command=command, state=state, calls=calls)


def copy_plugin_to_path_with_spaces(tmp_path: Path) -> Path:
    """Copy only installer inputs into a checkout-like path with spaces."""

    plugin = tmp_path / "Captain Plugin" / "agent-plugin"
    (plugin / "bin").mkdir(parents=True)
    (plugin / "skills/captain").mkdir(parents=True)
    shutil.copy2(PLUGIN / "bin/captain-agent-mcp", plugin / "bin")
    shutil.copy2(
        PLUGIN / "skills/captain/SKILL.md",
        plugin / "skills/captain/SKILL.md",
    )
    return plugin


def test_config_root_honors_xdg_config_home(tmp_path: Path):
    assert config_root({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "opencode"


def test_install_copies_skill_and_adds_mcp_with_one_path_argument(
    tmp_path: Path,
    fake_opencode: FakeOpenCode,
):
    plugin = copy_plugin_to_path_with_spaces(tmp_path)
    config_home = tmp_path / "config"

    messages = install_opencode(
        str(fake_opencode.command),
        dry_run=False,
        environment=fake_opencode.environment(config_home),
        plugin_root=plugin,
    )

    installed = config_home / "opencode/skills/captain/SKILL.md"
    assert installed.read_bytes() == (plugin / "skills/captain/SKILL.md").read_bytes()
    assert fake_opencode.recorded_calls == [
        [
            "mcp",
            "add",
            "captain",
            "--",
            str(plugin / "bin/captain-agent-mcp"),
        ]
    ]
    assert "Installed OpenCode /captain." in messages


def test_dry_run_makes_no_changes(tmp_path: Path, fake_opencode: FakeOpenCode):
    config_home = tmp_path / "config"

    messages = install_opencode(
        str(fake_opencode.command),
        dry_run=True,
        environment=fake_opencode.environment(config_home),
        plugin_root=PLUGIN,
    )

    assert not config_home.exists()
    assert fake_opencode.recorded_calls == []
    assert messages == [
        f"Would install OpenCode skill at {config_home / 'opencode/skills/captain/SKILL.md'}.",
        f"Would add OpenCode MCP server captain using {PLUGIN / 'bin/captain-agent-mcp'}.",
    ]


def test_identical_install_is_idempotent(
    tmp_path: Path,
    fake_opencode: FakeOpenCode,
):
    config_home = tmp_path / "config"
    environment = fake_opencode.environment(config_home)

    install_opencode(
        str(fake_opencode.command),
        dry_run=False,
        environment=environment,
        plugin_root=PLUGIN,
    )
    fake_opencode.set_resolved_config(
        {"mcp": {"captain": expected_server(PLUGIN / "bin/captain-agent-mcp")}}
    )

    messages = install_opencode(
        str(fake_opencode.command),
        dry_run=False,
        environment=environment,
        plugin_root=PLUGIN,
    )

    assert len(fake_opencode.recorded_calls) == 1
    assert messages == [
        "OpenCode /captain is already installed.",
        "OpenCode MCP server captain is already configured.",
    ]


def test_conflicting_skill_is_preserved(
    tmp_path: Path,
    fake_opencode: FakeOpenCode,
):
    config_home = tmp_path / "config"
    destination = config_home / "opencode/skills/captain/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("user content\n", encoding="utf-8")

    with pytest.raises(InstallConflict, match="Captain skill already exists"):
        install_opencode(
            str(fake_opencode.command),
            dry_run=False,
            environment=fake_opencode.environment(config_home),
            plugin_root=PLUGIN,
        )

    assert destination.read_text(encoding="utf-8") == "user content\n"
    assert fake_opencode.recorded_calls == []


def test_conflicting_mcp_server_is_preserved(
    tmp_path: Path,
    fake_opencode: FakeOpenCode,
):
    config_home = tmp_path / "config"
    fake_opencode.set_resolved_config(
        {"mcp": {"captain": {"type": "local", "command": ["other-launcher"]}}}
    )

    with pytest.raises(InstallConflict, match="MCP server already exists"):
        install_opencode(
            str(fake_opencode.command),
            dry_run=False,
            environment=fake_opencode.environment(config_home),
            plugin_root=PLUGIN,
        )

    assert not (config_home / "opencode/skills/captain/SKILL.md").exists()
    assert fake_opencode.recorded_calls == []


def test_disabled_matching_mcp_server_is_a_conflict(
    tmp_path: Path,
    fake_opencode: FakeOpenCode,
):
    config_home = tmp_path / "config"
    server = expected_server(PLUGIN / "bin/captain-agent-mcp")
    server["enabled"] = False
    fake_opencode.set_resolved_config({"mcp": {"captain": server}})

    with pytest.raises(InstallConflict, match="MCP server already exists"):
        install_opencode(
            str(fake_opencode.command),
            dry_run=False,
            environment=fake_opencode.environment(config_home),
            plugin_root=PLUGIN,
        )


def test_mcp_failure_rolls_back_the_new_skill(
    tmp_path: Path,
    fake_opencode: FakeOpenCode,
):
    config_home = tmp_path / "config"
    destination = config_home / "opencode/skills/captain/SKILL.md"
    fake_opencode.add_exit_code = 7

    with pytest.raises(RuntimeError, match="could not add the Captain MCP server"):
        install_opencode(
            str(fake_opencode.command),
            dry_run=False,
            environment=fake_opencode.environment(config_home),
            plugin_root=PLUGIN,
        )

    assert not destination.exists()
