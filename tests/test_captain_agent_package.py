"""Verify the Captain plugin's public package and launcher behavior."""

import json
import stat
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from mcp import Client

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "agent-plugin"
sys.path.insert(0, str(PLUGIN))


@pytest.fixture
def temporary_plugin_runtime_artifacts():
    """Create unique ignored files under the packaged plugin and remove only them."""

    token = f"captain-package-test-{uuid4().hex}"
    artifacts = [
        PLUGIN / ".venv" / "bin" / f"{token}.pyc",
        PLUGIN / f"{token}.sqlite3",
        PLUGIN / "captain_agent" / "__pycache__" / f"{token}.pyc",
        PLUGIN / "captain_agent" / f"{token}.pyc",
    ]
    created_directories = []
    try:
        for artifact in artifacts:
            missing_directories = []
            directory = artifact.parent
            while directory != PLUGIN and not directory.exists():
                missing_directories.append(directory)
                directory = directory.parent
            artifact.parent.mkdir(parents=True, exist_ok=True)
            created_directories.extend(missing_directories)
            artifact.write_bytes(b"runtime-private test artifact")
        yield artifacts
    finally:
        for artifact in artifacts:
            artifact.unlink(missing_ok=True)
        for directory in sorted(
            set(created_directories), key=lambda path: len(path.parts), reverse=True
        ):
            if directory.exists():
                directory.rmdir()


def npm_pack_paths():
    """Return the paths npm would include without creating a tarball."""

    result = subprocess.run(
        ["npm", "pack", "--dry-run", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {entry["path"] for entry in json.loads(result.stdout)[0]["files"]}


def test_marketplace_points_to_local_captain_plugin():
    marketplace = json.loads(
        (ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
    )
    entry = marketplace["plugins"][0]
    assert marketplace["name"] == "captain"
    assert entry["name"] == "captain"
    assert entry["source"] == {"source": "local", "path": "./agent-plugin"}


def test_claude_marketplace_defines_the_local_captain_plugin():
    """Keep Claude's host-specific paths out of the shared MCP manifest."""

    marketplace = json.loads(
        (ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )
    plugin = marketplace["plugins"][0]

    assert marketplace["name"] == "captain"
    assert marketplace["owner"]["name"] == "Common Vector Robotics"
    assert plugin["name"] == "captain"
    assert plugin["source"] == "./agent-plugin"
    assert plugin["strict"] is False
    assert plugin["skills"] == "./skills/"
    assert plugin["mcpServers"] == {
        "captain": {
            "command": "${CLAUDE_PLUGIN_ROOT}/bin/captain-agent-mcp",
            "args": [],
        }
    }


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


def test_shared_skill_names_each_supported_host_tool_once():
    """Require explicit host names so the skill never guesses an alias."""

    skill = (PLUGIN / "skills/captain/SKILL.md").read_text(encoding="utf-8")
    names = (
        "Captain:captain_session_report",
        "captain__captain_session_report",
        "mcp__captain__captain_session_report",
        "captain_captain_session_report",
    )

    for name in names:
        assert skill.count(f"`{name}`") >= 1

    assert "If neither name is available, or if both" not in skill
    assert "If zero or more than one exact name is available" in skill


def test_launcher_is_executable_and_valid_shell():
    launcher = PLUGIN / "bin/captain-agent-mcp"
    assert launcher.stat().st_mode & stat.S_IXUSR
    subprocess.run(["sh", "-n", str(launcher)], check=True)


def test_opencode_installer_is_executable_and_valid_python():
    installer = PLUGIN / "bin/install-opencode"

    assert installer.stat().st_mode & stat.S_IXUSR
    subprocess.run(
        [sys.executable, "-m", "py_compile", str(installer)],
        check=True,
    )


def test_launcher_rejects_importable_mcp_v1_and_falls_back_to_uv(tmp_path):
    plugin = tmp_path / "agent-plugin"
    launcher = plugin / "bin" / "captain-agent-mcp"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes((PLUGIN / "bin/captain-agent-mcp").read_bytes())
    launcher.chmod(0o755)
    (plugin / "requirements.txt").write_text("mcp>=2,<3\n", encoding="utf-8")

    fake_site = tmp_path / "site-packages"
    (fake_site / "mcp/server").mkdir(parents=True)
    (fake_site / "mcp/__init__.py").write_text("", encoding="utf-8")
    (fake_site / "mcp/server/__init__.py").write_text(
        "class MCPServer:\n    pass\n", encoding="utf-8"
    )
    dist_info = fake_site / "mcp-1.9.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: mcp\nVersion: 1.9.0\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(Path(sys.executable))
    uv_marker = tmp_path / "uv-args.txt"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$CAPTAIN_AGENT_UV_MARKER\"\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    result = subprocess.run(
        [str(launcher)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "PYTHONPATH": str(fake_site),
            "CAPTAIN_AGENT_UV_MARKER": str(uv_marker),
        },
    )

    assert result.returncode == 0, result.stderr
    assert uv_marker.read_text(encoding="utf-8").strip() == (
        f"run --quiet --no-project --with-requirements "
        f"{plugin / 'requirements.txt'} python -m captain_agent.server"
    )


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


def test_plugin_readme_documents_native_claude_and_opencode_installation():
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")

    assert "claude plugin marketplace add Common-Vector-Robotics/captain" in readme
    assert "claude plugin install captain@captain" in readme
    assert "./agent-plugin/bin/install-opencode" in readme
    assert "`/captain:captain`" in readme
    assert "`/captain`" in readme
    assert "mcp__captain__captain_session_report" in readme
    assert "captain_captain_session_report" in readme


def test_plugin_readme_walks_a_team_through_remote_setup():
    """Keep the operator and team-member setup paths complete and discoverable."""

    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")

    for heading in (
        "## Set up a remote team",
        "### 1. Prepare the Captain host",
        "### 2. Give a team member access",
        "### 3. Configure the team member's computer",
        "### 4. Install the coding-agent plugin",
        "### 5. Verify the complete connection",
        "### Remove a team member",
    ):
        assert heading in readme

    for command in (
        "openclaw gateway status",
        "openclaw gateway auth-token --show",
        "ssh -N -L 18789:127.0.0.1:18789",
        "openclaw onboard --classic --mode remote",
        "openclaw status --deep",
        "openclaw agents list --json",
        "openclaw devices list",
        "openclaw devices approve <requestId>",
        'openclaw devices rename --device <deviceId> --name "Member - Work laptop"',
        "openclaw devices revoke --device <deviceId> --role operator",
        "openclaw security audit --deep",
    ):
        assert command in readme

    assert "[Captain installation](../README.md#install-and-set-up)" in readme
    assert "does not complete the verification" in readme


def test_plugin_readme_explains_the_remote_team_trust_boundary():
    """Do not present a shared Gateway as per-user security isolation."""

    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split()).lower()

    assert "trusted team" in normalized
    assert "captain-specific user accounts" in normalized
    assert "separate gateways" in normalized
    assert "shared gateway credential" in normalized


def test_root_readme_names_each_supported_coding_agent():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Codex" in readme
    assert "Claude Code" in readme
    assert "OpenCode" in readme
    assert "OpenClaw" in readme
    assert "remote team setup" in readme.lower()


def test_npm_pack_excludes_plugin_runtime_artifacts(
    temporary_plugin_runtime_artifacts,
):
    before = set(ROOT.glob("captain-workspace-*.tgz"))
    packaged = npm_pack_paths()
    after = set(ROOT.glob("captain-workspace-*.tgz"))

    runtime_paths = {
        str(path.relative_to(ROOT)) for path in temporary_plugin_runtime_artifacts
    }
    assert packaged.isdisjoint(runtime_paths)
    assert {
        ".agents/plugins/marketplace.json",
        ".claude-plugin/marketplace.json",
        "agent-plugin/.codex-plugin/plugin.json",
        "agent-plugin/.mcp.json",
        "agent-plugin/bin/captain-agent-mcp",
        "agent-plugin/bin/install-opencode",
        "agent-plugin/captain_agent/opencode_install.py",
        "agent-plugin/captain_agent/server.py",
        "agent-plugin/requirements.txt",
    }.issubset(packaged)
    assert after == before
