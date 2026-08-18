# Captain coding-agent plugin

This plugin lets a coding agent report completed work to an existing
[Captain](../README.md) installation. It supports Codex, Claude Code, OpenCode,
and OpenClaw.

## What this plugin does

After you finish a coding task, run `/captain` in your coding agent. Claude Code
uses `/captain:captain` instead. The plugin then follows this path:

```text
Coding agent -> local plugin -> OpenClaw Gateway -> Captain -> ClickUp
```

1. Your coding agent summarizes the Git changes and checks it completed.
2. The plugin receives that summary through MCP, the connection used by the
   coding agent to call local tools.
3. The plugin asks your local OpenClaw command-line program to run Captain.
4. Captain uses the report to update ClickUp or ask for missing information.

The plugin and OpenClaw command-line program run on your computer. The OpenClaw
Gateway, Captain, and ClickUp connection may run on the same computer or on a
remote computer.

## Before you install

You need:

- A working Captain installation. If you do not have one, first
  [install Captain](../README.md#install-and-set-up).
- The [OpenClaw command-line program](https://docs.openclaw.ai/install) on the
  same computer as your coding agent.
- Your local OpenClaw configuration connected to a Gateway that contains an
  agent whose ID is `captain`.
- [`uv`](https://docs.astral.sh/uv/), which starts the plugin's Python code and
  installs its small Python dependency when needed.

Check the OpenClaw connection before installing the plugin:

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
`CAPTAIN REPORT NOT SENT`, follow the message's instructions and retry with the
same report ID. Reusing the ID prevents the same report from being applied
twice.

## Connect to a remote Captain

Skip this section when OpenClaw and Captain run on your computer.

A remote Gateway is a shared trust boundary. OpenClaw does not create
Captain-specific user accounts. People who use the same Gateway may be able to
use the same sessions, tools, credentials, and files. Use separate Gateways for
people who should not share those resources.

### 1. Prepare the Captain computer

On an always-on computer, [install Captain](../README.md#install-and-set-up),
then run:

```bash
openclaw gateway status
openclaw agents list --json
openclaw security audit --deep
openclaw gateway auth-token --show
```

The agent list must contain `captain`. If no Gateway token exists, create one:

```bash
openclaw doctor --generate-gateway-token
openclaw gateway restart
```

Give the token to the team member through your team's credential-sharing
method. The shared Gateway credential authorizes the connection.

### 2. Give the team member SSH access

Give each member their own SSH account or public key. Have the member confirm
access, then open a tunnel and leave that terminal running:

```bash
ssh user@gateway-host
ssh -N -L 18789:127.0.0.1:18789 user@gateway-host
```

The tunnel makes the remote Gateway appear at `127.0.0.1:18789` on the member's
computer.

### 3. Connect the team member's computer

Install the [OpenClaw command-line program](https://docs.openclaw.ai/install)
on the team member's computer. With the SSH tunnel running, start remote setup:

```bash
openclaw onboard --classic --mode remote
```

Enter these values when asked:

- **Gateway URL:** `ws://127.0.0.1:18789`
- **Authentication:** token
- **Token:** the token from the Captain computer

If the wizard asks where to save the token, choose SecretRef, OpenClaw's
protected token storage. Then check the connection:

```bash
openclaw status --deep
openclaw agents list --json
```

After both checks pass, follow [Install](#install) and
[Verify the setup](#verify-the-setup).

### 4. Approve the device if OpenClaw asks

If the member sees `PAIRING_REQUIRED`, run these commands on the Captain
computer after matching the request to the correct person:

```bash
openclaw devices list
openclaw devices approve <requestId>
openclaw devices rename --device <deviceId> --name "Member - Work laptop"
```

Captain reports require `operator.write`. Do not approve broader permissions
unless the member needs them.

### Remove a team member

1. Remove the person's SSH key, account, or Tailscale access.
2. On the Captain computer, revoke the device and replace the shared token:

   ```bash
   openclaw devices list
   openclaw devices revoke --device <deviceId> --role operator
   openclaw configure --section gateway
   openclaw gateway restart
   openclaw security audit --deep
   ```

3. Give the new token to the remaining members.

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

Do not create a second `captain` agent. An explicit `skills` list replaces the
default list, so keep Captain's other required skills in it.

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
  will not send the report again.
