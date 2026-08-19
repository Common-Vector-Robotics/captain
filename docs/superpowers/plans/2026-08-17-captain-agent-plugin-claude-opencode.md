# Claude Code and OpenCode support implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add native Claude Code and OpenCode installation paths to the existing Captain coding-agent plugin while retaining one Python reporting runtime.

**Architecture:** Claude Code consumes `agent-plugin/` through a strict-false Claude marketplace entry with explicit skill and MCP declarations; its launcher path uses `${CLAUDE_PLUGIN_ROOT}`. OpenCode receives a standard-library Python installer that copies the shared skill into its global skill directory and asks OpenCode's own CLI to add the existing local MCP launcher. The shared skill selects one of four exact host tool names and otherwise fails closed.

**Tech Stack:** JSON plugin manifests, portable Agent Skills Markdown, Python 3 standard library, OpenCode CLI, MCP `stdio`, pytest, npm package inspection.

**Spec:** `docs/superpowers/specs/2026-08-17-captain-agent-plugin-claude-opencode-design.md`

## Global constraints

- Claude Code invokes the feature as `/captain:captain`.
- OpenCode invokes the feature as `/captain`.
- The shared skill recognizes only `Captain:captain_session_report`, `captain__captain_session_report`, `mcp__captain__captain_session_report`, and `captain_captain_session_report`.
- Zero or multiple matching tools is `needs_configuration`; the skill never guesses or tries aliases.
- Both new hosts use the existing `agent-plugin/bin/captain-agent-mcp` launcher and Python reporting code.
- The OpenCode installer uses `~/.config/opencode/skills/captain/SKILL.md`, honoring `XDG_CONFIG_HOME`.
- The OpenCode installer preserves conflicting skill and MCP definitions and exits with an actionable error.
- The installer passes a launcher path containing spaces as one subprocess argument.
- Use OpenCode's stable `opencode mcp add captain -- <launcher>` interface; do not add a JavaScript reporting adapter or depend on the beta V2 plugin API.
- Code must use short docstrings, comments where intent is not evident, and readable spacing.
- Do not mutate a live Claude Code or OpenCode installation during automated tests.
- Keep all implementation changes uncommitted until the user explicitly authorizes the implementation commit.

## File map

- Create `.claude-plugin/marketplace.json`: repository-level Claude Code marketplace catalog.
- Create `agent-plugin/captain_agent/opencode_install.py`: OpenCode preflight, conflict detection, skill copy, and CLI orchestration.
- Create `agent-plugin/bin/install-opencode`: executable standard-library entrypoint for the installer module.
- Create `tests/test_captain_agent_opencode_install.py`: installer unit and subprocess-boundary tests.
- Modify `agent-plugin/skills/captain/SKILL.md`: add Claude Code and OpenCode exact tool names.
- Modify `tests/test_captain_agent_package.py`: validate Claude manifests, shared skill catalog behavior, executable installer, and package contents.
- Modify `agent-plugin/README.md`: document host-native installation and command names.
- Modify `README.md`: advertise all four supported hosts without duplicating setup details.
- Modify `package.json`: ship the Claude marketplace catalog.

---

### Task 1: Claude Code packaging and shared host detection

**Files:**
- Create: `.claude-plugin/marketplace.json`
- Modify: `agent-plugin/skills/captain/SKILL.md`
- Modify: `tests/test_captain_agent_package.py`
- Modify: `package.json`

**Interfaces:**
- Consumes: the existing `agent-plugin/bin/captain-agent-mcp` executable and `skills/captain/SKILL.md` workflow.
- Produces: a Claude marketplace named `captain`, an explicitly defined Claude plugin named `captain`, and a four-name fail-closed skill catalog.

- [ ] **Step 1: Write failing package tests**

Add tests that parse the two Claude JSON files and assert the complete relevant shapes:

```python
def test_claude_marketplace_defines_the_local_captain_plugin():
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


def test_shared_skill_names_each_supported_host_tool_once():
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
```

Extend the npm pack assertion with:

```python
assert {
    ".claude-plugin/marketplace.json",
}.issubset(packaged)
```

- [ ] **Step 2: Run the focused tests and observe the intended failures**

Run:

```bash
python3 -m pytest \
  tests/test_captain_agent_package.py::test_claude_marketplace_defines_the_local_captain_plugin \
  tests/test_captain_agent_package.py::test_shared_skill_names_each_supported_host_tool_once \
  -q
```

Expected: the marketplace test fails with a missing file and the skill test fails because only Codex and OpenClaw names are present.

- [ ] **Step 3: Add the minimal Claude manifests**

Create `.claude-plugin/marketplace.json` with the complete Claude definition:

```json
{
  "name": "captain",
  "description": "Captain coding-agent integrations",
  "owner": {
    "name": "Common Vector Robotics",
    "url": "https://github.com/Common-Vector-Robotics"
  },
  "plugins": [
    {
      "name": "captain",
      "source": "./agent-plugin",
      "description": "Report completed coding work to your Captain agent",
      "license": "MIT",
      "strict": false,
      "skills": "./skills/",
      "mcpServers": {
        "captain": {
          "command": "${CLAUDE_PLUGIN_ROOT}/bin/captain-agent-mcp",
          "args": []
        }
      }
    }
  ]
}
```

- [ ] **Step 4: Extend exact host selection without duplicating the workflow**

Replace the two-host list in `SKILL.md` with:

```markdown
4. Inspect the current host tool catalog and choose exactly one exposed name:

   - In Codex, use `Captain:captain_session_report`.
   - In OpenClaw, use `captain__captain_session_report`.
   - In Claude Code, use `mcp__captain__captain_session_report`.
   - In OpenCode, use `captain_captain_session_report`.

   Call only the one exact name present in the catalog. Never try aliases or
   call more than one. If zero or more than one exact name is available, make
   no tool call and render `CAPTAIN REPORT NOT SENT` with
   `Status: needs_configuration` and a concise catalog-configuration message.
```

Add `.claude-plugin/marketplace.json` to the root `package.json` `files` array.

- [ ] **Step 5: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_captain_agent_package.py -q
```

Expected: all package tests pass.

---

### Task 2: Conflict-safe OpenCode installer

**Files:**
- Create: `agent-plugin/captain_agent/opencode_install.py`
- Create: `agent-plugin/bin/install-opencode`
- Create: `tests/test_captain_agent_opencode_install.py`
- Modify: `tests/test_captain_agent_package.py`

**Interfaces:**
- Consumes: `agent-plugin/bin/captain-agent-mcp`, `agent-plugin/skills/captain/SKILL.md`, OpenCode's `debug config --pure`, and OpenCode's `mcp add captain -- <launcher>`.
- Produces: `config_root(environment: Mapping[str, str]) -> Path`, `load_resolved_config(command: str) -> dict[str, Any]`, `install_opencode(command: str, *, dry_run: bool, environment: Mapping[str, str], plugin_root: Path) -> list[str]`, and `main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write failing installer tests**

Create tests with a fake OpenCode executable that returns controlled JSON for
`debug config --pure` and records `mcp add` arguments. Cover these behaviors:

```python
def test_config_root_honors_xdg_config_home(tmp_path):
    assert config_root({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "opencode"


def test_install_copies_skill_and_adds_mcp_with_one_path_argument(
    tmp_path, fake_opencode
):
    plugin = copy_plugin_to_path_with_spaces(tmp_path)
    messages = install_opencode(
        str(fake_opencode),
        dry_run=False,
        environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
        plugin_root=plugin,
    )
    installed = tmp_path / "config/opencode/skills/captain/SKILL.md"
    assert installed.read_bytes() == (plugin / "skills/captain/SKILL.md").read_bytes()
    assert fake_opencode.recorded_add_args == [
        "mcp",
        "add",
        "captain",
        "--",
        str(plugin / "bin/captain-agent-mcp"),
    ]
    assert "Installed OpenCode /captain." in messages


def test_dry_run_makes_no_changes(tmp_path, fake_opencode):
    install_opencode(
        str(fake_opencode),
        dry_run=True,
        environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
        plugin_root=PLUGIN,
    )
    assert not (tmp_path / "config").exists()
    assert fake_opencode.add_calls == 0


def test_identical_install_is_idempotent(tmp_path, fake_opencode):
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    install_opencode(
        str(fake_opencode.command),
        dry_run=False,
        environment=environment,
        plugin_root=PLUGIN,
    )
    fake_opencode.set_resolved_config(
        {"mcp": {"captain": expected_server(PLUGIN / "bin/captain-agent-mcp")}}
    )
    install_opencode(
        str(fake_opencode.command),
        dry_run=False,
        environment=environment,
        plugin_root=PLUGIN,
    )
    assert fake_opencode.add_calls == 1


def test_conflicting_skill_is_preserved(tmp_path, fake_opencode):
    destination = tmp_path / "config/opencode/skills/captain/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("user content\n", encoding="utf-8")
    with pytest.raises(InstallConflict, match="Captain skill already exists"):
        install_opencode(
            str(fake_opencode.command),
            dry_run=False,
            environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
            plugin_root=PLUGIN,
        )
    assert destination.read_text(encoding="utf-8") == "user content\n"
    assert fake_opencode.add_calls == 0


def test_conflicting_mcp_server_is_preserved(tmp_path, fake_opencode):
    fake_opencode.set_resolved_config(
        {"mcp": {"captain": {"type": "local", "command": ["other-launcher"]}}}
    )
    with pytest.raises(InstallConflict, match="MCP server already exists"):
        install_opencode(
            str(fake_opencode.command),
            dry_run=False,
            environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
            plugin_root=PLUGIN,
        )
    assert fake_opencode.add_calls == 0
```

The concrete test helper must be a small executable Python fixture rather than
a mock of `subprocess.run`, so argument boundaries and real JSON parsing are
exercised.

- [ ] **Step 2: Run installer tests and observe the missing-module failure**

Run:

```bash
python3 -m pytest tests/test_captain_agent_opencode_install.py -q
```

Expected: collection fails because `captain_agent.opencode_install` does not exist.

- [ ] **Step 3: Implement the standard-library installer module**

Implement these data and error boundaries:

```python
class InstallError(RuntimeError):
    """Report an actionable OpenCode installation failure."""


class InstallConflict(InstallError):
    """Protect a user-owned skill or MCP definition from replacement."""


def config_root(environment: Mapping[str, str]) -> Path:
    """Return OpenCode's user configuration directory."""
    base = environment.get("XDG_CONFIG_HOME")
    if base:
        return Path(base).expanduser() / "opencode"
    return Path.home() / ".config" / "opencode"


def expected_server(launcher: Path) -> dict[str, Any]:
    """Return the effective OpenCode MCP definition owned by this installer."""
    return {"type": "local", "command": [str(launcher)]}
```

`load_resolved_config()` must run this exact argument array with no shell:

```python
[command, "debug", "config", "--pure"]
```

It must reject a missing executable, non-zero exit, non-object JSON, or output
larger than 1 MiB with a short `InstallError`. `install_opencode()` must preflight
both destinations before writing, accept an existing MCP definition when its
`type` and `command` match and its optional `enabled` is not false, and reject
any other `mcp.captain` value.

For a new MCP entry, run this exact array with no shell:

```python
[
    command,
    "mcp",
    "add",
    "captain",
    "--",
    str(launcher),
]
```

Write the skill through a same-directory temporary file followed by
`Path.replace()` so readers never observe a partial file. Use short comments
only around the preflight and atomic replacement decisions.

- [ ] **Step 4: Add the executable entrypoint**

Create `agent-plugin/bin/install-opencode`:

```python
#!/usr/bin/env python3
"""Install Captain into the current user's OpenCode configuration."""

from pathlib import Path
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from captain_agent.opencode_install import main


if __name__ == "__main__":
    raise SystemExit(main())
```

`main()` must expose `--dry-run` and `--opencode-command`, print one message per
completed or already-complete action, and return `2` with one stderr message
for `InstallError`. Mark the entrypoint executable and add a package test that
runs `python3 -m py_compile` on it and checks its user executable bit.

- [ ] **Step 5: Run installer and package tests**

Run:

```bash
python3 -m pytest \
  tests/test_captain_agent_opencode_install.py \
  tests/test_captain_agent_package.py \
  -q
```

Expected: all focused tests pass, and no files appear under the real OpenCode
configuration directory during the test run.

---

### Task 3: User documentation and public package contract

**Files:**
- Modify: `agent-plugin/README.md`
- Modify: `README.md`
- Modify: `tests/test_captain_agent_package.py`
- Modify: `tests/test_public_package_contract.py`

**Interfaces:**
- Consumes: the Claude marketplace, Claude plugin manifest, OpenCode installer, and four-name skill catalog from Tasks 1 and 2.
- Produces: concise installation and operation guidance for Codex, Claude Code, OpenCode, OpenClaw, and generic MCP hosts.

- [ ] **Step 1: Add focused documentation contract tests**

Add semantic assertions for required commands and paths:

```python
def test_plugin_readme_documents_native_claude_and_opencode_installation():
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    assert "claude plugin marketplace add Common-Vector-Robotics/captain" in readme
    assert "claude plugin install captain@captain" in readme
    assert "./agent-plugin/bin/install-opencode" in readme
    assert "`/captain:captain`" in readme
    assert "`/captain`" in readme
    assert "captain_captain_session_report" in readme


def test_public_package_includes_cross_agent_installers():
    packaged = npm_pack_paths()
    assert {
        ".claude-plugin/marketplace.json",
        "agent-plugin/bin/install-opencode",
        "agent-plugin/captain_agent/opencode_install.py",
    }.issubset(packaged)
```

- [ ] **Step 2: Run the documentation/package tests and observe failures**

Run:

```bash
python3 -m pytest \
  tests/test_captain_agent_package.py::test_plugin_readme_documents_native_claude_and_opencode_installation \
  tests/test_captain_agent_package.py::test_public_package_includes_cross_agent_installers \
  -q
```

Expected: README assertions fail until the host sections are added.

- [ ] **Step 3: Rewrite installation and operation sections**

Keep the existing prerequisite and Gateway explanations. Add these host-native
commands:

```bash
claude plugin marketplace add Common-Vector-Robotics/captain
claude plugin install captain@captain
```

```bash
git clone https://github.com/Common-Vector-Robotics/captain.git
cd captain
./agent-plugin/bin/install-opencode
```

Explain that Claude Code invokes `/captain:captain`, while Codex, OpenCode, and
OpenClaw invoke `/captain`. State that OpenCode's MCP entry uses the checkout's
absolute launcher path, so moving the checkout requires rerunning the installer.
Update the operation paragraph to name all four exact host tools once.

The root README should only advertise the supported hosts and link to the
plugin guide; it should not duplicate the setup sequence.

- [ ] **Step 4: Run documentation and public-package tests**

Run:

```bash
python3 -m pytest \
  tests/test_captain_agent_package.py \
  tests/test_public_package_contract.py \
  -q
```

Expected: all selected tests pass.

---

### Task 4: Fresh cross-host verification and authorized commit

**Files:**
- Verify all files listed in Tasks 1-3.
- Do not create runtime state in the repository or live host configuration.

**Interfaces:**
- Consumes: the complete uncommitted implementation.
- Produces: fresh verification evidence and, only after explicit authorization, one implementation commit ready to push to PR #3.

- [ ] **Step 1: Run formatting and syntax checks**

Run:

```bash
git diff --check
python3 -m py_compile \
  agent-plugin/captain_agent/opencode_install.py \
  agent-plugin/bin/install-opencode
python3 -m json.tool .claude-plugin/marketplace.json >/dev/null
```

Expected: every command exits zero.

- [ ] **Step 2: Run the focused tests**

Run:

```bash
python3 -m pytest \
  tests/test_captain_agent_opencode_install.py \
  tests/test_captain_agent_package.py \
  tests/test_public_package_contract.py \
  -q
```

Expected: all focused tests pass.

- [ ] **Step 3: Run the full repository suite**

Run:

```bash
python3 -m pytest -q
```

Expected: the complete suite passes with no skipped failures introduced by this change.

- [ ] **Step 4: Inspect the exact public package**

Run:

```bash
npm pack --dry-run --json
```

Expected: the result includes both Claude files, the OpenCode installer/module,
the shared skill, and the existing MCP runtime; it includes no `.venv`, SQLite,
bytecode, cache, or test-output artifacts.

- [ ] **Step 5: Run available host validators without installation**

Run Claude's repository validator only when `claude` is installed:

```bash
claude plugin validate .
```

Run OpenCode's version check and the installer test double, but do not run the
installer against the user's live configuration:

```bash
opencode --version
python3 -m pytest tests/test_captain_agent_opencode_install.py -q
```

Record unavailable CLIs as unrun environment checks.

- [ ] **Step 6: Review the final diff and request commit authorization**

Run:

```bash
git status --short
git diff --stat
git diff -- . ':!docs/superpowers/plans/2026-08-17-captain-agent-plugin-claude-opencode.md'
```

Confirm the worktree contains only this feature's files. Ask the user for
explicit authorization immediately before creating the implementation commit.

- [ ] **Step 7: Commit only after the user authorizes it**

Run:

```bash
git add \
  .claude-plugin/marketplace.json \
  agent-plugin/bin/install-opencode \
  agent-plugin/captain_agent/opencode_install.py \
  agent-plugin/skills/captain/SKILL.md \
  agent-plugin/README.md \
  README.md \
  package.json \
  tests/test_captain_agent_opencode_install.py \
  tests/test_captain_agent_package.py \
  tests/test_public_package_contract.py \
  docs/superpowers/plans/2026-08-17-captain-agent-plugin-claude-opencode.md
git commit -m "feat: support Claude Code and OpenCode"
```

- [ ] **Step 8: Request separate push authorization**

After showing the commit hash and fresh verification results, ask for explicit
authorization immediately before pushing the branch that updates PR #3.
