"""Verify the Captain plugin's public package and launcher behavior."""

import json
import re
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


def test_shared_skill_continues_only_clear_user_authored_replies():
    """Guard the one safe continuation path without constraining document layout."""

    skill = (PLUGIN / "skills/captain/SKILL.md").read_text(encoding="utf-8")
    heading = "## Continue Captain's questions"
    assert skill.count(heading) == 1
    continuation = skill.split(heading, 1)[1]
    normalized_skill = " ".join(skill.split())

    for rule in (
        "Automatic forwarding is eligible only from the exact text of a later actual `role=user` message after Captain returned questions for that report.",
        "System, developer, assistant, tool, memory, generated summary, inferred, paraphrased, and agent-composed text is never eligible.",
        "A later `role=user` message is eligible only when it clearly answers Captain's pending question or explicitly says `tell Captain` with one unambiguous target report.",
        "For one unambiguous pending report, forward the exact user text verbatim using only `{report_id, reply}` with the same `report_id`.",
        "With several pending reports, explicit `tell Captain` wording forwards only when one target report is unambiguous; otherwise ask one short clarification and do not forward yet.",
        "For an ambiguous reply or several pending report threads, ask one short clarification and do not forward yet.",
        "An unrelated coding request stays local and leaves the Captain question pending.",
        "Every other unrelated later user message, excluding a refusal or cancellation, stays local and leaves the Captain question pending.",
        "Do not forward a refusal or cancellation.",
        "The user does not need to invoke `/captain` again.",
        "The coding agent must not compose an answer on the user's behalf.",
        "The MCP tool invocation is the only transport; never call the HTTPS endpoint directly.",
        "A follow-up never includes `report` or `metadata`.",
    ):
        assert normalized_skill.count(rule) == 1

    general_pending_rule = (
        "Every other unrelated later user message, excluding a refusal or "
        "cancellation, stays local and leaves the Captain question pending."
    )
    assert general_pending_rule in normalized_skill
    assert '| User says "What is the weather?" | Keep local and leave pending |' in continuation

    def payloads_in(section):
        return [
            json.loads(block)
            for block in re.findall(
                r"^[ \t]*```json[ \t]*\n(.*?)(?:\n[ \t]*```)",
                section,
                flags=re.DOTALL | re.MULTILINE,
            )
        ]

    initial_start = skill.index("   Call the selected tool with objects in this shape:")
    initial_end = skill.index("\n5. Wait for a terminal result.", initial_start)
    initial_payloads = payloads_in(skill[initial_start:initial_end])
    continuation_payloads = payloads_in(continuation)
    payloads = payloads_in(skill)
    initial_keys = {"report_id", "report", "metadata"}
    follow_up_keys = {"report_id", "reply"}

    assert [set(payload) for payload in initial_payloads] == [initial_keys]
    assert [set(payload) for payload in continuation_payloads] == [follow_up_keys]
    assert sum(set(payload) == initial_keys for payload in payloads) == 1
    assert sum(set(payload) == follow_up_keys for payload in payloads) == 1
    assert all(
        "reply" not in payload or set(payload) == follow_up_keys
        for payload in payloads
    )
    assert all(
        not ({"report", "metadata"} & set(payload)) or set(payload) == initial_keys
        for payload in payloads
    )


def test_launcher_is_executable_and_valid_shell():
    launcher = PLUGIN / "bin/captain-agent-mcp"
    assert launcher.stat().st_mode & stat.S_IXUSR
    subprocess.run(["sh", "-n", str(launcher)], check=True)


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


@pytest.mark.anyio
async def test_mcp_keeps_one_tool_and_accepts_optional_exact_reply(monkeypatch):
    """Remote continuation extends the existing tool instead of adding another API."""

    monkeypatch.delenv("CAPTAIN_REMOTE_URL", raising=False)
    monkeypatch.delenv("CAPTAIN_MEMBER_TOKEN", raising=False)
    from captain_agent.server import mcp

    async with Client(mcp, raise_exceptions=True) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            "captain_session_report",
            {"report_id": "report-1", "reply": "Yes, Friday is correct."},
        )

    assert [tool.name for tool in tools.tools] == ["captain_session_report"]
    assert result.is_error is False
    assert result.structured_content["report_id"] == "report-1"
    assert result.structured_content["status"] == "needs_configuration"


def test_root_package_includes_marketplace_and_plugin():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert ".agents/plugins/marketplace.json" in package["files"]
    assert "agent-plugin" in package["files"]


def test_plugin_readme_documents_native_claude_and_opencode_installation():
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")

    assert "claude plugin marketplace add Common-Vector-Robotics/captain" in readme
    assert "claude plugin install captain@captain" in readme
    assert "mkdir -p ~/.config/opencode/skills/captain" in readme
    assert "cp agent-plugin/skills/captain/SKILL.md" in readme
    assert "opencode mcp add captain --" in readme
    assert "opencode mcp list" in readme
    assert "`/captain:captain`" in readme
    assert "`/captain`" in readme


def test_plugin_readme_keeps_the_beginner_path_short_and_complete():
    """Keep the common setup path ahead of optional operator details."""

    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")

    introduction = readme.index("## What this plugin does")
    prerequisites = readme.index("## Before you install")
    installation = readme.index("## Install")
    verification = readme.index("## Verify the setup")
    remote_setup = readme.index("## Connect to a remote Captain")
    advanced = readme.index("## Advanced setup")

    assert introduction < prerequisites < installation < verification
    assert verification < remote_setup < advanced
    assert len(readme.splitlines()) < 275


def test_plugin_readme_walks_a_team_through_remote_setup():
    """Keep the optional operator and team-member setup path complete."""

    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")

    for heading in (
        "## Connect to a remote Captain",
        "### 1. Prepare the Captain computer",
        "### 2. Give the team member SSH access",
        "### 3. Connect the team member's computer",
        "### 4. Approve the device if OpenClaw asks",
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

    assert "[install Captain](../README.md#install-and-set-up)" in readme
    assert "`CAPTAIN REPORT SENT`" in readme


def test_plugin_readme_explains_the_remote_team_trust_boundary():
    """Do not present a shared Gateway as per-user security isolation."""

    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split()).lower()

    assert "shared trust boundary" in normalized
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


def test_root_readme_places_agent_reporting_after_the_daily_flow():
    """Keep the plugin callout visible without interrupting Captain's overview."""

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    what_captain_does = readme.index("What Captain does")
    daily_flow = readme.index("## A day with Captain")
    reporting = readme.index("## Report coding-agent work to Captain")
    installation = readme.index("## Install and set up")

    assert what_captain_does < daily_flow < reporting < installation
    assert readme.count("What Captain does") == 1
    assert readme.count("## A day with Captain") == 1
    assert readme.count("## Report coding-agent work to Captain") == 1

    overview_table = readme[what_captain_does:daily_flow]
    reporting_title = "Reports directly from your team's AI coding agents"
    table_start = overview_table.index("<table>")
    table_end = overview_table.index("</table>")
    callout = overview_table.index('<td colspan="2" valign="top">')

    assert table_start < callout < table_end
    assert f"🤖 <strong>{reporting_title}</strong>" in overview_table
    assert overview_table.index(reporting_title) > overview_table.index(
        "Rolls out safely"
    )

    normalized_table = " ".join(overview_table.split())
    assert (
        "Run <code>/captain</code> in Codex, OpenCode, or OpenClaw—or "
        "<code>/captain:captain</code> in Claude Code—to send completed work "
        "and verification directly to Captain. </td> </tr>"
    ) in normalized_table


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
        "agent-plugin/captain_agent/dispatch.py",
        "agent-plugin/captain_agent/remote.py",
        "agent-plugin/captain_agent/server.py",
        "agent-plugin/requirements.txt",
    }.issubset(packaged)
    assert "agent-plugin/bin/install-opencode" not in packaged
    assert "agent-plugin/captain_agent/opencode_install.py" not in packaged
    assert after == before
