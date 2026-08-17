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
