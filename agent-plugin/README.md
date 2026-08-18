# Captain coding-agent plugin

Use `/captain` in Codex, OpenCode, or OpenClaw—or `/captain:captain` in Claude
Code—to give your Captain agent concise evidence about completed coding work.
The data path is **coding agent → local MCP process → local OpenClaw CLI →
configured Gateway → Captain → ClickUp**.

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

## Set up a remote team

A Gateway is a shared trust boundary. Anyone with access may be able to use
Captain's sessions, tools, credentials, and files. OpenClaw's profile, device,
and session-owner labels show who started work, but they do not restrict
access. Only share a Gateway with people you trust with all of those resources.

The plugin does not create Captain-specific user accounts. Team access has
three parts:

1. An SSH or Tailscale identity controls who can reach the Captain host.
2. An OpenClaw device record identifies an approved client when pairing is
   required.
3. The shared Gateway credential authorizes the connection.

The report itself does not carry identity or authorization claims. If two
people should not share Captain's sessions, tools, credentials, or files, run
separate Gateways for them. OpenClaw documents this boundary in its
[multi-user guide](https://docs.openclaw.ai/concepts/multi-user) and
[operator-scope reference](https://docs.openclaw.ai/gateway/operator-scopes).

The steps below use an SSH tunnel because it works with OpenClaw's default
loopback-only Gateway. Teams that already use Tailscale can use the same flow
with [Tailscale SSH access rules](https://tailscale.com/docs/features/tailscale-ssh),
or follow OpenClaw's
[direct tailnet instructions](https://docs.openclaw.ai/gateway/remote).

### 1. Prepare the Captain host

Complete the main [Captain installation](../README.md#install-and-set-up) on an
always-on computer. Run these checks on that computer:

```bash
# Confirm that the Gateway is running.
openclaw gateway status

# Confirm that the installed agent list contains an id of "captain".
openclaw agents list --json

# Check the Gateway configuration and its exposed capabilities.
openclaw security audit --deep
```

Keep the Gateway on its default loopback bind when using an SSH tunnel. Token
authentication is recommended even on loopback. Display the configured token
in an interactive terminal when you are ready to configure a team member:

```bash
openclaw gateway auth-token --show
```

If OpenClaw reports that no token is configured, generate one and restart the
Gateway before continuing:

```bash
openclaw doctor --generate-gateway-token
openclaw gateway restart
```

Give the token to the team member through your team's credential-sharing
method. They will enter it into OpenClaw's masked onboarding prompt.

### 2. Give a team member access

Give each member their own SSH identity so you can remove one person's access
without affecting everyone else. Depending on your server setup, this can be
a separate operating-system account, a separate public key in
`authorized_keys`, or a Tailscale user allowed by an access-control rule.

For an existing SSH server, create a non-administrator login for each member
or install one public key per member. For Tailscale SSH, add the member with
their own Tailscale login, then use **Access Controls** to allow only that user
or team group to reach the Captain host as a non-root user. Preview the rule
for that user before saving it. Tailscale's
[access-control guide](https://tailscale.com/kb/1338/acl-edit) shows where to
edit and preview these rules.

Only grant access to people who may use all of Captain's configured
capabilities. Give the member these three values:

- the SSH hostname of the Captain computer;
- their SSH username;
- the Gateway token from step 1.

Have the member confirm ordinary SSH access first:

```bash
ssh user@gateway-host
```

Then have them open the tunnel below and leave that terminal running. The
remote Gateway will appear on their computer as `127.0.0.1:18789`.

```bash
ssh -N -L 18789:127.0.0.1:18789 user@gateway-host
```

If port `18789` is already in use on the member's computer, choose another
local port, such as `28789`, while keeping the destination unchanged:

```bash
ssh -N -L 28789:127.0.0.1:18789 user@gateway-host
```

### 3. Configure the team member's computer

Install the [OpenClaw CLI](https://docs.openclaw.ai/install) on the member's
computer. With the SSH tunnel still running, start the guided remote setup:

```bash
openclaw onboard --classic --mode remote
```

Enter the following values when prompted:

- **Gateway URL:** `ws://127.0.0.1:18789`, or the alternate local port chosen
  in step 2;
- **Authentication:** token;
- **Token:** the Gateway token from the Captain operator.

Choose SecretRef storage when the wizard offers it. The remote setup changes
only the member's computer; it does not install or modify the Captain host.

Check the connection and agent list:

```bash
openclaw status --deep
openclaw agents list --json
```

The first command must show a reachable Gateway. The second must include an
agent whose `id` is `captain`.

Direct tailnet and LAN connections are remote connections, so OpenClaw may ask
the Captain operator to approve the member's device. If the member receives a
`PAIRING_REQUIRED` response, run these commands on the Captain host:

```bash
# Review the current request and its requested role and scopes.
openclaw devices list

# Approve the exact request only after matching it to the new member.
openclaw devices approve <requestId>
```

Ordinary `openclaw agent` turns require `operator.write`. Do not approve
broader scopes unless that member needs them. The member can retry
`openclaw status --deep` after approval.

After the device appears in `openclaw devices list`, give it a label that
identifies the member and computer. This makes later reviews and offboarding
unambiguous:

```bash
openclaw devices rename --device <deviceId> --name "Member - Work laptop"
```

### 4. Install the coding-agent plugin

On the member's computer, follow one matching section under [Install](#install):

- [Codex marketplace](#codex-marketplace)
- [Claude Code marketplace](#claude-code-marketplace)
- [OpenCode](#opencode)
- [OpenClaw](#openclaw)

Restart the coding-agent application after installation so it reloads the
skill and MCP server.

### 5. Verify the complete connection

First, check the same route the plugin will use without making a project
management update:

```bash
openclaw agent --agent captain \
  --message "Connectivity check only. Make no external changes. Reply READY." \
  --json
```

A JSON response containing Captain's reply confirms that the member's local
CLI can run the remote `captain` agent. A connection, authentication, or
pairing error must be resolved before testing the plugin.

Next, complete a small coding task in a test repository, record the verification
you actually ran, and invoke the command for the installed host:

| Host | Command |
| --- | --- |
| Codex | `/captain` |
| Claude Code | `/captain:captain` |
| OpenCode | `/captain` |
| OpenClaw | `/captain` |

`CAPTAIN REPORT SENT` confirms the full plugin-to-Captain path. A
`CAPTAIN REPORT NOT SENT` result does not complete the verification. Follow
its feedback, correct the input or connection, and reuse the same report ID.
See [Troubleshooting](#troubleshooting) for the meaning of each result.

### Remove a team member

1. Remove the person's SSH key, operating-system account, or Tailscale access.
2. Review the Gateway's paired devices and revoke that member's operator token:

   ```bash
   openclaw devices list
   openclaw devices revoke --device <deviceId> --role operator
   ```

3. Because the member knew the shared Gateway credential, replace it through
   OpenClaw's Gateway configuration, restart the Gateway, and update each
   remaining member's remote configuration:

   ```bash
   openclaw configure --section gateway
   openclaw gateway restart
   ```

4. Confirm that the old credential no longer connects, then run:

   ```bash
   openclaw security audit --deep
   ```

OpenClaw's [security guide](https://docs.openclaw.ai/gateway/security) provides
the complete credential-rotation checklist.

## Install

### Codex marketplace

```bash
codex plugin marketplace add Common-Vector-Robotics/captain --ref main
codex plugin add captain@captain
```

### Claude Code marketplace

```bash
claude plugin marketplace add Common-Vector-Robotics/captain
claude plugin install captain@captain
```

Invoke the installed skill as `/captain:captain`.

### OpenCode

Clone the repository into a location you intend to keep, then run the
user-scoped installer from the repository root:

```bash
git clone https://github.com/Common-Vector-Robotics/captain.git
cd captain
./agent-plugin/bin/install-opencode
```

The installer uses OpenCode's own CLI to add the MCP server and copies the
shared skill into OpenCode's global skill directory. It stops instead of
replacing a different `captain` skill or MCP definition. Preview its work with
`./agent-plugin/bin/install-opencode --dry-run`.

OpenCode stores the launcher's absolute path. If you move the checkout, run the
installer again from its new location after removing the old `captain` MCP
entry. Invoke the installed skill as `/captain`.

### OpenClaw

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

Invoke the installed skill as `/captain`.

### Other MCP hosts

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
not require a separate Gateway URL setting. For a complete operator and team
member walkthrough, use [Set up a remote team](#set-up-a-remote-team).

## Operation

Invoke the host-native command after completed coding work. The included skill
gathers a short, redacted Git and verification report, creates one stable
report ID, and calls exactly one tool name from the current host:

| Host | Command | Tool name |
| --- | --- | --- |
| Codex | `/captain` | `Captain:captain_session_report` |
| Claude Code | `/captain:captain` | `mcp__captain__captain_session_report` |
| OpenCode | `/captain` | `captain_captain_session_report` |
| OpenClaw | `/captain` | `captain__captain_session_report` |

The skill never guesses an alias or calls more than one match. OpenClaw's name
follows its documented
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
