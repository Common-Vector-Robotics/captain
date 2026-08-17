# Captain coding-agent plugin

Use `/captain` to give your Captain agent concise evidence about completed
coding work. The data path is **coding agent → local MCP process → local
OpenClaw CLI → configured Gateway → Captain → ClickUp**.

The MCP process and OpenClaw CLI run on the coding-agent machine. The Gateway
and Captain agent can run on that machine or on a remote host.

## Prerequisites

- An OpenClaw CLI on the coding-agent machine, configured to reach a local or
  remote Gateway with the `captain` agent installed.
- Either [`uv`](https://docs.astral.sh/uv/) or Python 3 with this bundle's
  requirements installed.

The MCP server calls the local OpenClaw CLI. OpenClaw routes the turn through
its configured Gateway to the `captain` agent. The plugin does not install or
configure OpenClaw, Captain, or ClickUp.

## Install

### Codex marketplace

```bash
codex plugin marketplace add Common-Vector-Robotics/captain --ref main
codex plugin add captain@captain
```

### Cloned repository or OpenClaw

From the repository root, install the compatible bundle into OpenClaw:

```bash
openclaw plugins install ./agent-plugin
```

If the Gateway does not reload plugins automatically, activate the mapped skill
and MCP tool, then inspect the runtime registration:

```bash
openclaw gateway restart
openclaw plugins inspect captain --runtime --json
```

For another MCP host, configure its stdio command from the repository root as:

```bash
./agent-plugin/bin/captain-agent-mcp
```

The launcher uses `CAPTAIN_AGENT_PYTHON` when set, then
`agent-plugin/.venv/bin/python`, then `python3` only when it can import
`MCPServer` from `mcp.server` and the installed `mcp` distribution is major
version 2. If none is ready, it uses local `uv` to resolve the bundled
requirements.

To create the optional isolated environment:

```bash
python3 -m venv agent-plugin/.venv
agent-plugin/.venv/bin/python -m pip install -r agent-plugin/requirements.txt
```

## Gateway location

The plugin works with local and remote OpenClaw Gateways. It invokes
[`openclaw agent`](https://docs.openclaw.ai/cli/agent) without `--local`, so
OpenClaw sends the turn through the Gateway selected by the local CLI
configuration.

For a remote Gateway, configure the OpenClaw CLI using the official
[remote access guide](https://docs.openclaw.ai/gateway/remote). The plugin does
not require a separate Gateway URL setting.

## Operation

Invoke `/captain` after completed coding work. The included skill gathers a
short, redacted Git and verification report, creates one stable report ID, and
calls the one name exposed by the current host: `Captain:captain_session_report`
in Codex or `captain__captain_session_report` in OpenClaw. It never guesses or
calls both. OpenClaw's name follows its documented
[`serverName__toolName` bundle convention](https://docs.openclaw.ai/plugins/bundles).
The skill also never sends credentials, customer PII, unrelated personal data,
credentialed URLs, raw transcripts, or identity and authorization claims.

A host session ID is used directly only when it matches
`[A-Za-z0-9._-]{1,128}`. Otherwise the skill derives the stable safe ID
`captain-<sha256>` and does not include or display the unsafe source value.

The adapter calls your local OpenClaw CLI with these defaults:

| Override | Default | Purpose |
| --- | --- | --- |
| `CAPTAIN_AGENT_OPENCLAW_COMMAND` | `openclaw` | OpenClaw executable |
| `CAPTAIN_AGENT_ID` | `captain` | Captain agent name in the configured Gateway |
| `CAPTAIN_AGENT_THINKING` | `high` | OpenClaw thinking level |
| `CAPTAIN_AGENT_TIMEOUT_SECONDS` | `300` | Report timeout, bounded to 30–3600 seconds |
| `CAPTAIN_AGENT_PYTHON` | unset | Python interpreter for the launcher |

Report replay state is a user-only local SQLite database at
`$XDG_STATE_HOME/captain-agent/reports.sqlite3`; if `XDG_STATE_HOME` is unset,
the default is `~/.local/state/captain-agent/reports.sqlite3`.
`CAPTAIN_AGENT_STATE_PATH` overrides the complete database path.

Stored `failed`, `needs_configuration`, `needs_clarification`, and `queued`
results are retryable with the same report ID. A replay reclaims the local row
before one new dispatch. Stored `created`, `updated`, `partial`, and
`unknown_outcome` results are immutable replays; in particular, uncertainty is
never auto-dispatched because Captain may already have completed the write.

## Prevent Captain self-recursion

The OpenClaw agent whose `id` is `captain` must not load this sender-side
`captain` skill or call its bundle tool. Edit that agent's existing entry in
`agents.list`; do not create a second agent entry. The relevant shape is:

```json5
{
  agents: {
    list: [
      {
        id: "captain",
        // Keep this agent's intended skill allowlist, excluding "captain".
        skills: [/* existing allowed skill names except "captain" */],
        tools: { deny: ["captain__captain_session_report"] },
      },
    ],
  },
}
```

An explicit per-agent `skills` list replaces inherited defaults, so preserve
the Captain agent's other intended skills while excluding `captain`. OpenClaw
tool policy applies deny after allow/profile rules, so this exact deny wins.
Together with the terminal-recipient prompt, these two controls prevent the
Captain agent from reporting its received report back into itself. See the
official [agent configuration](https://docs.openclaw.ai/gateway/config-agents),
[tool policy](https://docs.openclaw.ai/gateway/config-tools), and
[skill allowlist](https://docs.openclaw.ai/tools/skills-config) references.

## Troubleshooting

- **`openclaw` is missing:** install OpenClaw, make its CLI available on
  `PATH`, or set `CAPTAIN_AGENT_OPENCLAW_COMMAND` to its local executable.
- **MCP SDK v2 or `uv` is missing:** create the optional venv above, or install
  `uv` and run the launcher again. A system Python is used only for the stable
  MCP 2.x runtime required by this bundle.
- **`needs_configuration`:** make sure the local OpenClaw CLI can reach the
  configured Gateway and that its `captain` agent has the required ClickUp
  configuration, then retry with the same report ID.
- **`unknown_outcome`:** the local adapter cannot prove whether Captain
  completed the handoff. Do not claim success or auto-dispatch with a fresh ID.
  Check ClickUp first; the same report ID replays the stored uncertainty
  without another dispatch.
