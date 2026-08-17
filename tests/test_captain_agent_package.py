import json
import os
import stat
import subprocess
import sys
from uuid import uuid4
from pathlib import Path

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
        "agent-plugin/.codex-plugin/plugin.json",
        "agent-plugin/.mcp.json",
        "agent-plugin/bin/captain-agent-mcp",
        "agent-plugin/captain_agent/server.py",
        "agent-plugin/requirements.txt",
    }.issubset(packaged)
    assert after == before
