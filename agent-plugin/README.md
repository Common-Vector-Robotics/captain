# Captain coding-agent plugin

This plugin reports coding-agent work to an existing [Captain](../README.md)
installation. It supports Codex, Claude Code, OpenCode, and OpenClaw.

## What this plugin does

After you finish a coding task, run `/captain` in your coding agent. Claude Code
uses `/captain:captain` instead. The plugin then follows this path:

```text
Coding agent -> local plugin -> local OpenClaw or Captain HTTPS -> Captain
```

1. Your coding agent summarizes the Git changes and the checks it completed.
2. The plugin receives that summary through MCP, the connection your coding
   agent uses to call local tools.
3. The plugin uses your local OpenClaw command-line program by default. When
   remote access is configured, it uses Captain's restricted HTTPS adapter.
4. Captain uses the report to update ClickUp or ask for missing information.

The plugin runs on your computer. Local mode also requires the OpenClaw
command-line program and a reachable Gateway. Remote mode requires only the
Captain HTTPS URL and your individual member token.

## Before you install

You need:

- Access to a working Captain installation. For local mode, first
  [install Captain](../README.md#install-and-set-up). For remote mode, ask the
  Captain operator for the HTTPS URL and your individual member token.
- For local mode, the [OpenClaw command-line program](https://docs.openclaw.ai/install)
  on the same computer as your coding agent, connected to a Gateway that
  contains an agent whose ID is `captain`.
- [`uv`](https://docs.astral.sh/uv/), which starts the plugin's Python code and
  installs its small Python dependency when needed.

For local mode, check the OpenClaw connection before installing the plugin:

```bash
openclaw status --deep
openclaw agents list --json
```

The first command should show a reachable Gateway. The second command should
include an agent whose `id` is `captain`.

## Install

Use only the section for your coding agent. Restart the coding agent after
installation so it can load the new command and MCP tool.

### Codex

```bash
codex plugin marketplace add Common-Vector-Robotics/captain --ref main
codex plugin add captain@captain
```

Run the plugin with `/captain`.

### Claude Code

```bash
claude plugin marketplace add Common-Vector-Robotics/captain
claude plugin install captain@captain
```

Run the plugin with `/captain:captain`.

### OpenCode

Clone this repository into a location you plan to keep:

```bash
git clone https://github.com/Common-Vector-Robotics/captain.git
cd captain
```

Copy the Captain skill into OpenCode and register the plugin's MCP launcher:

```bash
mkdir -p ~/.config/opencode/skills/captain
cp agent-plugin/skills/captain/SKILL.md \
  ~/.config/opencode/skills/captain/SKILL.md
opencode mcp add captain -- \
  "$(pwd)/agent-plugin/bin/captain-agent-mcp"
opencode mcp list
```

Run the plugin with `/captain`.

OpenCode stores the full path to the cloned repository. If you move it, run the
`opencode mcp add` command again from the new location. Copy `SKILL.md` again
after updating the repository.

### OpenClaw

Clone the repository, then install its plugin bundle:

```bash
git clone https://github.com/Common-Vector-Robotics/captain.git
cd captain
openclaw plugins install ./agent-plugin
openclaw gateway restart
openclaw plugins inspect captain --runtime --json
```

Run the plugin with `/captain`.

## Verify the setup

1. Complete a small coding task in a test repository.
2. Record the checks you actually ran, such as a test or build command.
3. Run `/captain`, or `/captain:captain` in Claude Code.

`CAPTAIN REPORT SENT` means Captain received the report. If you see
`CAPTAIN REPORT NOT SENT`, follow the message's instructions and retry with
the same report ID, which prevents the report from being applied twice.

## Connect to a remote Captain

Remote mode lets this coding-agent plugin talk only to Captain. It does not
require SSH, local OpenClaw, a Gateway token, or OpenClaw device approval.

### 1. Ask the operator for member access

The operator installs the [Captain Remote OpenClaw plugin](../openclaw-plugin/README.md),
keeps the Gateway on loopback, exposes only `/captain/v1/`, and creates your member:

```bash
openclaw captain members add --name "Sam Lee" --email sam@example.com
```

The operator delivers your revocable individual token once through the team's
credential-sharing method.

### 2. Configure the coding agent

Set both values in a secret or environment store the coding platform supports:

```text
CAPTAIN_REMOTE_URL=https://captain.example.com
CAPTAIN_MEMBER_TOKEN=<your individual member token>
```

Keep the token out of URLs, arguments, source files, shell history, and logs.
One missing value returns `needs_configuration`; neither selects local mode.

Restart the coding agent, follow [Install](#install), and [verify the setup](#verify-the-setup).
You can forward a clear later reply verbatim without running Captain again.

### Remove or rotate a team member

```bash
openclaw captain members revoke <member-id>
openclaw captain members rotate <member-id>
```

Revocation immediately denies reports, replies, and polls. Rotation prints one
replacement and invalidates the old token.

## Advanced legacy: full Gateway access over SSH

This is not the normal Captain-only setup. Full Gateway access is a shared trust boundary
that may reach sessions, tools, credentials, and files. OpenClaw does not create
Captain-specific user accounts. Use separate Gateways for people who should not share.

### 1. Prepare the Captain computer

Verify the full-access Gateway on its host:

```bash
openclaw gateway status
openclaw status --deep
openclaw agents list --json
openclaw security audit --deep
openclaw gateway auth-token --show
```

If the full Gateway has no authentication token, create one and restart it:

```bash
openclaw doctor --generate-gateway-token
openclaw gateway restart
```

The shared Gateway credential is only for a member approved for broader access.

### 2. Give the team member SSH access

Create a dedicated SSH account or public key. The member keeps this tunnel open:

```bash
ssh -N -L 18789:127.0.0.1:18789 user@gateway-host
```

### 3. Connect the team member's computer

Install the OpenClaw command-line program, then run:

```bash
openclaw onboard --classic --mode remote
```

Use `ws://127.0.0.1:18789` and the Gateway token, then rerun the two status
checks from step 1.

### 4. Approve the device if OpenClaw asks

For `PAIRING_REQUIRED`, the operator verifies the request and runs:

```bash
openclaw devices list
openclaw devices approve <requestId>
openclaw devices rename --device <deviceId> --name "Member - Work laptop"
```

### Remove a team member

```bash
openclaw devices revoke --device <deviceId> --role operator
```

Remove the SSH key or account and rotate any shared Gateway token. Use the
restricted HTTPS adapter for Captain-only access, not a shared Gateway token.

## Advanced setup

### Prevent Captain from reporting to itself

Use this protection only if the OpenClaw agent whose ID is `captain` can see
the sender-side plugin. Edit Captain's existing entry in `agents.list`:

```json5
{
  agents: {
    list: [
      {
        id: "captain",
        // Keep Captain's other skills, but exclude "captain".
        skills: [/* existing skills except "captain" */],
        tools: { deny: ["captain__captain_session_report"] },
      },
    ],
  },
}
```

Do not create a second `captain` agent. An explicit `skills` list replaces
the default list, so keep Captain's other required skills in it.

### Configuration overrides

Most installations need no overrides. Advanced users can set
`CAPTAIN_AGENT_OPENCLAW_COMMAND`, `CAPTAIN_AGENT_ID`,
`CAPTAIN_AGENT_TIMEOUT_SECONDS`, or `CAPTAIN_AGENT_PYTHON`. The plugin records
report IDs in `~/.local/state/captain-agent/reports.sqlite3` so retries do not
create duplicates. `CAPTAIN_AGENT_STATE_PATH` changes that path.

## Troubleshooting

- **`openclaw` is missing:** Install the OpenClaw command-line program and make
  sure the `openclaw` command works in a new terminal.
- **The Gateway or `captain` agent is missing:** Run the two checks under
  [Before you install](#before-you-install), then fix the OpenClaw connection.
- **The Python launcher cannot start:** Install `uv`, then restart the coding
  agent.
- **`needs_configuration`:** Captain is missing required Gateway or ClickUp
  configuration. Fix that configuration, then retry with the same report ID.
- **`unknown_outcome`:** The plugin cannot prove whether Captain completed the
  report. Check ClickUp before trying anything else. Reusing the same report ID
  does not send the report again.
