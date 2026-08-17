# Claude Code and OpenCode Support Design

**Date:** 2026-08-17

**Status:** Approved

## Objective

Extend the existing Captain coding-agent plugin so Claude Code and OpenCode can
install and invoke the same reporting workflow without manual MCP configuration.

The host-native commands are:

- Claude Code: `/captain:captain`
- OpenCode: `/captain`

Both hosts continue to use the existing Python MCP server, local replay
database, OpenClaw CLI adapter, and configured local or remote Gateway. This
change adds packaging and host detection only; it does not create another
reporting implementation.

## Design Choice

Claude Code receives a native marketplace definition because Claude Code can
discover skills and start bundled MCP servers directly from a marketplace
plugin.

OpenCode receives a small installer that uses OpenCode's native global skill
and MCP configuration. The installer is preferable to OpenCode's in-process V2
plugin API because that API is currently beta and would require a second
JavaScript tool adapter. Keeping OpenCode on its documented skill and MCP
interfaces preserves one reporting runtime and one public tool contract.

## Shared Reporting Workflow

The existing `agent-plugin/skills/captain/SKILL.md` remains the canonical skill.
It recognizes exactly these host-exposed tool names:

| Host | Tool name |
| --- | --- |
| Codex | `Captain:captain_session_report` |
| OpenClaw | `captain__captain_session_report` |
| Claude Code | `mcp__captain__captain_session_report` |
| OpenCode | `captain_captain_session_report` |

The skill inspects the current tool catalog and calls exactly one matching
name. Zero matches or more than one match produces `needs_configuration`
without making a tool call. This preserves the existing fail-closed behavior.

The report schema, redaction rules, stable report identifier, terminal status
handling, and rendered result remain unchanged.

## Claude Code Package

Add `.claude-plugin/marketplace.json` with a `captain` plugin entry whose source
is `./agent-plugin`. The entry uses `strict: false` and explicitly declares the
existing `./skills/` directory, which exposes the namespaced command
`/captain:captain`.

The marketplace entry declares the existing MCP server inline and starts:

```text
${CLAUDE_PLUGIN_ROOT}/bin/captain-agent-mcp
```

Using `CLAUDE_PLUGIN_ROOT` is required because Claude Code starts plugin MCP
servers from the user's active project rather than from the plugin directory.
With `strict: false`, this marketplace definition is authoritative for Claude
Code, so its explicit server does not compete with the bundle's Codex/OpenClaw
`.mcp.json`. That existing file remains unchanged.

Installation uses Claude Code's native marketplace flow against this GitHub
repository. The repository gains a Claude marketplace catalog alongside the
existing Codex marketplace catalog, with `agent-plugin/` as the plugin source.

## OpenCode Package

Add `agent-plugin/bin/install-opencode`, a readable Python entrypoint that
performs a user-scoped installation:

1. Resolve the absolute path of the bundled `captain-agent-mcp` launcher.
2. Read OpenCode's effective configuration through
   `opencode debug config --pure` before any write.
3. Add the `captain` MCP server through OpenCode's own
   `opencode mcp add captain -- <launcher>` command.
4. Atomically install the canonical skill at
   `~/.config/opencode/skills/captain/SKILL.md`, or the equivalent directory
   under `XDG_CONFIG_HOME`.
5. Print the exact verification commands and `/captain` invocation.

The installer supports `--dry-run` and accepts an OpenCode executable override
for nonstandard installations. It treats an identical existing Captain skill
or MCP entry as already installed. If either destination exists with different
content or a different command, it stops with an actionable message rather
than overwriting user configuration. It never edits `opencode.json` itself.

The installed skill is copied rather than symlinked so moving or deleting the
checkout does not silently remove `/captain`. The MCP entry intentionally keeps
an absolute launcher path, so the README tells users to retain the checkout at
that location or reinstall after moving it.

OpenCode discovers the installed skill natively and presents it as `/captain`.
Its MCP naming convention exposes the tool as
`captain_captain_session_report`.

## Repository Layout

```text
agent-plugin/
  .mcp.json
  bin/
    captain-agent-mcp
    install-opencode
  skills/
    captain/
      SKILL.md
.claude-plugin/
  marketplace.json
tests/
  test_captain_agent_package.py
  test_captain_agent_opencode_install.py
```

No host-specific copy of the reporting module is added.

## Installation Documentation

The plugin README gains separate installation sections:

- Codex marketplace
- Claude Code marketplace
- OpenCode user-scoped installer
- OpenClaw bundle
- Generic MCP host

The operation section explains the four exact tool names and the two new
host-native commands. Gateway-location documentation remains shared because
every host ultimately invokes the same local OpenClaw CLI.

## Validation

Automated package tests will verify:

- the Claude marketplace catalog parses and points at the existing skill and
  launcher with `strict: false`;
- Claude's MCP command uses `${CLAUDE_PLUGIN_ROOT}`;
- the shared skill names all four exact tools and retains the zero-or-many
  fail-closed rule;
- the OpenCode installer writes only the expected user-scoped skill and MCP
  entry in an isolated temporary configuration directory;
- dry-run makes no changes;
- identical installs are idempotent;
- conflicting skill or MCP configuration is preserved and reported;
- paths containing spaces are passed as one executable argument;
- the public package contains every new artifact and no local test output.

Fresh verification will include the focused package/installer tests, the full
repository test suite, package dry-run inspection, Claude's plugin validator
when its CLI is available, and OpenCode configuration inspection when its CLI
is available. Unavailable host CLIs will be reported as unrun environment
checks rather than claimed as passes.

## Non-Goals

- No hosted transport or Captain service.
- No second JavaScript reporting adapter.
- No HTTP MCP server.
- No automatic OpenClaw, Captain, Gateway, or ClickUp setup.
- No overwrite of unrelated Claude Code or OpenCode configuration.
- No change to Captain's ClickUp judgment or write-audit behavior.
