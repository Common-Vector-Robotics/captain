# Captain coding-agent plugin

Use `/captain` to give your locally installed Captain agent concise evidence
about completed coding work. The data path is **coding agent → local MCP process → local Captain → ClickUp**. No Intermode or CVR server handles the report.

This bundle runs only on your machine. It opens no HTTP listener and requires
no hosted account, Intermode/CVR server, or remote telemetry.

## Prerequisites

- A local OpenClaw Gateway with the `captain` agent installed and configured.
- Either [`uv`](https://docs.astral.sh/uv/) or Python 3 with this bundle's
  requirements installed.

The MCP server starts the local `captain` OpenClaw agent, which uses your local
Captain and ClickUp configuration. It does not install or configure those
dependencies for you.

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

For another MCP host, configure its stdio command from the repository root as:

```bash
./agent-plugin/bin/captain-agent-mcp
```

The launcher uses `CAPTAIN_AGENT_PYTHON` when set, then
`agent-plugin/.venv/bin/python`, then `python3` with the MCP SDK installed. If
none is ready, it uses local `uv` to resolve the bundled requirements.

To create the optional isolated environment:

```bash
python3 -m venv agent-plugin/.venv
agent-plugin/.venv/bin/python -m pip install -r agent-plugin/requirements.txt
```

## Operation

Invoke `/captain` after completed coding work. The included skill gathers a
short, redacted Git and verification report, creates one stable report ID, and
calls the local MCP tool. It never sends credentials, customer PII, unrelated
personal data, credentialed URLs, or raw transcripts.

The adapter calls your local OpenClaw CLI with these defaults:

| Override | Default | Purpose |
| --- | --- | --- |
| `CAPTAIN_AGENT_OPENCLAW_COMMAND` | `openclaw` | OpenClaw executable |
| `CAPTAIN_AGENT_ID` | `captain` | Local Captain agent name |
| `CAPTAIN_AGENT_THINKING` | `high` | OpenClaw thinking level |
| `CAPTAIN_AGENT_TIMEOUT_SECONDS` | `300` | Report timeout, bounded to 30–3600 seconds |
| `CAPTAIN_AGENT_PYTHON` | unset | Python interpreter for the launcher |

Report replay state is a user-only local SQLite database at
`$XDG_STATE_HOME/captain-agent/reports.sqlite3`; if `XDG_STATE_HOME` is unset,
the default is `~/.local/state/captain-agent/reports.sqlite3`.
`CAPTAIN_AGENT_STATE_PATH` overrides the complete database path.

## Troubleshooting

- **`openclaw` is missing:** install OpenClaw, make its CLI available on
  `PATH`, or set `CAPTAIN_AGENT_OPENCLAW_COMMAND` to its local executable.
- **MCP SDK or `uv` is missing:** create the optional venv above, or install
  `uv` and run the launcher again. The launcher needs one local way to import
  the bundled MCP SDK.
- **`needs_configuration`:** configure and start the local OpenClaw Gateway
  and `captain` agent, including Captain's normal ClickUp configuration, then
  retry with the same report ID.
- **`unknown_outcome`:** the local adapter cannot prove whether Captain
  completed the handoff. Do not claim success or create a fresh ID. Check
  ClickUp first; if a replay is needed, reuse the same report ID.
