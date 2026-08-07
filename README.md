# Captain

![1786130927208](image/README/1786130927208.png)

Captain is an openclaw-based project manager for technical teams. Captain automatically manages Clickup tasks, checks in with team members over Slack to receive status updates, infers critical paths, and coordinates team members to remove blockers and prioritize important tasks. Captain also reads and infers tasks based on meeting transcripts sent to his configured email address.

## Install and set up

Captain is packaged as an OpenClaw Claw (note: .claw packages are still experimental). It installs Captain as a new agent with four weekday daily-loop jobs plus daily reporting in every`DailyLoop` mode, including `off`. The four operational jobs remain off until you configure them locally.

### Prerequisites

- A working [OpenClaw installation](https://docs.openclaw.ai/) with its Gateway and Slack channel configured
- Python 3
- A ClickUp API key and ClickUp team ID
- A Slack account dedicated to Captain, plus the user and channel IDs used in`data/captain-channels.json`

### 1. Install the Claw

Clone this repository outside of your openclaw folder:

```Shell
# Make a directory (or choose your own)
mkdir -p ~/src

# Clone captain repository
cd ~/src
git clone <repository-url> captain

# Change wd to captain's folder
cd captain
```

Run these commands from this package directory (the directory containing `CLAW.md`). Before installing, set `AUTHORIZED_TOGGLE_USERS` to the Slack user IDs and names allowed to switch Captain between `off`, `shadow`, and `live`:

```bash
# Set authorized users in captain_modes.py (Slack User IDs)
nano scripts/captain_modes.py
```

Then inspect and preview the package:

```bash
# Enable OpenClaw's experimental Claw commands in this terminal.
export OPENCLAW_EXPERIMENTAL_CLAWS=1

# Check the package contents and install settings.
openclaw claws inspect .

# Preview the installation without changing your system.
openclaw claws add . --dry-run --json
```

Review every action in the dry-run output and copy its `planIntegrity` value.
Replace `SHA256_FROM_DRY_RUN` below with that value, then apply the exact plan:

```bash
# Install the exact package plan you just reviewed.
openclaw claws add . --yes --plan-integrity SHA256_FROM_DRY_RUN
```

`--yes` alone is intentionally insufficient. OpenClaw rejects the install if the package, destination, or live configuration changed after the dry run.

Confirm the installed agent and note the workspace path reported by OpenClaw:

```bash
# Confirm that Captain was installed and find its workspace path.
openclaw claws status captain --json

# Check the rest of your OpenClaw setup for problems.
openclaw doctor
```

The default workspace is `~/.openclaw/workspace-captain`. If the install plan reported a different path, use that path in the remaining commands.

### 2. Install Python dependencies

```bash
# Move into Captain's installed workspace.
cd ~/.openclaw/workspace-captain

# Install the Python packages Captain needs.
python3 -m pip install --user -r requirements.txt
```

Homebrew-managed Python may reject that command with `error: externally-managed-environment`. In that case, install into its user site explicitly:

```bash
# Use this version only if Python reports an externally-managed-environment error.
python3 -m pip install --user --break-system-packages -r requirements.txt
```

### 3. Configure ClickUp

Create a local secrets file without committing it:

```bash
# Create a private folder for local secrets.
mkdir -p .secrets

# Make the folder accessible only to your user account.
chmod 700 .secrets

# Create the ClickUp credentials file. Replace both placeholder values.
cat > .secrets/clickup.env <<'EOF'
CLICKUP_API_KEY=replace-with-your-clickup-api-key
CLICKUP_TEAM_ID=replace-with-your-clickup-team-id
EOF

# Allow only your user account to read or edit the credentials file.
chmod 600 .secrets/clickup.env
```

Verify the credentials with a read-only board fetch:

```bash
# Test the ClickUp connection and save the results to a temporary file.
python3 scripts/fetch_clickup_tasks.py --out /tmp/captain-clickup-smoke.json
```

### 4. Configure Slack routing and operators

```bash
# Copy the example Slack settings into a local configuration file.
cp data/captain-channels.example.json data/captain-channels.json

# Open the local configuration and replace its placeholder values.
nano data/captain-channels.json

# Check that the edited file contains valid JSON.
python3 -m json.tool data/captain-channels.json >/dev/null
```

Replace every placeholder in `data/captain-channels.json`. Keep configured files local and do not commit credentials or live routing details.

### 5. Validate in shadow mode

Confirm that Captain starts in `off`, then enable `shadow` using an authorized Slack user ID:

```bash
# Check Captain's current operating mode.
python3 scripts/captain_modes.py status

# Send test actions only to the configured shadow destination.
python3 scripts/captain_modes.py dailyloop \
  --audience shadow \
  --user-id U0123456789 \
  --source initial-setup

# List Captain's scheduled jobs and their IDs.
openclaw cron list --agent captain
```

To test immediately, copy one Captain job ID from the cron list and run it:

```bash
# Run one Captain job now. Replace CAPTAIN_CRON_JOB_ID with an ID from the list.
openclaw cron run CAPTAIN_CRON_JOB_ID \
  --wait \
  --wait-timeout 10m
```

Inspect the configured shadow destination. Confirm that Captain uses the right ClickUp workspace, Slack account, recipients, and program channel before enabling live actions:

```bash
# Enable Captain's real Slack and ClickUp actions after checking shadow mode.
python3 scripts/captain_modes.py dailyloop \
  --audience live \
  --user-id U0123456789 \
  --source initial-setup
```

To stop operational actions while keeping the daily read-only activity report:

```bash
# Stop Captain's operational actions while keeping its daily activity report.
python3 scripts/captain_modes.py dailyloop \
  --audience off \
  --user-id U0123456789 \
  --source manual-stop
```

See [`BOOTSTRAP.md`](BOOTSTRAP.md) for the setup checklist and safety model.

The package intentionally excludes credentials, configured Slack routing, runtime state, ClickUp exports, audit logs, and local reports.

This repository contains Captain's source prompts, persona files, scripts, and non-sensitive fixtures.

Excluded from git by design:

- secrets and environment files
- local OpenClaw runtime state
- SQLite databases and mutable cron state
- raw transcripts, screenshots, generated reports, and ClickUp exports
- audit and approval queues that may contain live operational details

Runtime state remains on the Captain host unless explicitly exported through a reviewed process.

# Extras

## Sentry telemetry

Captain can report hard failures (script crashes, session-report server errors, OpenClaw cron job failures) to Sentry.

The cron bridge runs every 10 minutes via launchd on the Captain host, diffs `openclaw cron list --json` error counters, and heartbeats the `captain-openclaw-bridge` Sentry monitor (dead-man's switch: a missed check-in means the host, OpenClaw, or the bridge is down).

Deploy/refresh on the Captain host:

```bash
# Install the Python packages needed by Captain and Sentry telemetry.
python3 -m pip install --user -r requirements.txt
```

On Homebrew-managed Python this fails outright with `error: externally-managed-environment` (PEP 668). Fix:

```bash
# Use this version only if Python reports an externally-managed-environment error.
python3 -m pip install --user --break-system-packages -r requirements.txt
```

`--break-system-packages` installs into a Homebrew-managed Python's user site directory, which is why pip guards it by default.

Create `.secrets/sentry.env` (never committed; without it telemetry is a silent no-op):

```bash
# Create the private secrets folder if it does not already exist.
mkdir -p .secrets

# Create the Sentry settings file. Replace the DSN placeholder.
cat > .secrets/sentry.env <<'EOF'
SENTRY_DSN=<your project's Sentry DSN>
# SENTRY_ENVIRONMENT=captain-host   # optional, defaults to captain-host
EOF

# Create the local log folder required by the launchd service.
mkdir -p logs
```

`launchd` may use a different Python installation than the one where you installed `sentry-sdk`. Because the bridge silently continues when the SDK is unavailable, it may appear healthy while sending no Sentry events or check-ins. Before loading the plist, verify that `sentry-sdk` is installed for the exact Python interpreter selected by the plist’s `PATH`.

```bash
# Confirm that launchd's Python can import the Sentry package.
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin /usr/bin/env python3 -c "import sentry_sdk; print(sentry_sdk.VERSION)"
```

If this fails with `ModuleNotFoundError`, install the dependencies for that specific interpreter:

```Shell
PYTHON_PATH="$(PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin /usr/bin/env python3 -c 'import sys; print(sys.executable)')"
"$PYTHON_PATH" -m pip install --user -r requirements.txt
```

Before enabling scheduled Sentry checks, test the Sentry-to-OpenClaw connection on this computer. Confirm that OpenClaw accepts the scheduling settings without errors:

```bash
# Test the Sentry bridge without sending telemetry or changing cron jobs.
python3 scripts/openclaw_cron_sentry_bridge.py --dry-run
```

Check the output: `jobs` should be greater than 0 and `counters_missing` should be `[]`.

If every job shows up in `counters_missing`, OpenClaw's field names don't match what `job_view()` looks for, and the bridge will silently report zero failures forever while the dead-man's-switch still says it's healthy! Fix the field mapping in `scripts/openclaw_cron_sentry_bridge.py` before loading the plist:

```Shell
# Inspect the actual counter and error fields returned by OpenClaw.
openclaw cron list --json |
  jq '.jobs[] | {
    name,
    top_level_keys: keys,
    state_keys: (.state // {} | keys),
    state: .state
  }'
  
# Update job_view() to match the error fields returned by `openclaw cron list --json`.
nano scripts/openclaw_cron_sentry_bridge.py

# Re-verify
python3 -m pytest tests/test_openclaw_cron_sentry_bridge.py -v
python3 scripts/openclaw_cron_sentry_bridge.py --dry-run
```

`launchd/com.intermode.captain-sentry-bridge.plist` hardcodes `WorkingDirectory` and both `StandardOutPath`/`StandardErrorPath` to `/Users/owen/.openclaw/workspace-captain`. If you are deploying from a different clone or a different user's home directory, edit those three paths in the plist before copying it in.

```bash
# Copy the launchd service file into your user account.
cp launchd/com.intermode.captain-sentry-bridge.plist ~/Library/LaunchAgents/

# Stop the old service if it is already loaded. No output is expected if it is not.
launchctl unload ~/Library/LaunchAgents/com.intermode.captain-sentry-bridge.plist 2>/dev/null

# Load the service so it runs on its schedule.
launchctl load ~/Library/LaunchAgents/com.intermode.captain-sentry-bridge.plist
```

## OpenClaw Slack DM read hotfix

OpenClaw 2026.7.1 incorrectly applies Slack's group-channel policy to DM read targets. Captain's reviewed hotfix and exact compatible versions are recorded in`config/openclaw-slack-dm-read-hotfix.json`.

Preview or deploy the hotfix on the Captain host:

```bash
# Preview the hotfix without changing the installed OpenClaw files.
python3 scripts/deploy_openclaw_slack_dm_read_hotfix.py --dry-run

# Apply the hotfix after reviewing the preview.
python3 scripts/deploy_openclaw_slack_dm_read_hotfix.py
```

The deployment only authorizes DM channel IDs already present in Captain's weekly check-in state. It also updates the weekly cron instruction to read the stored `D...` conversation ID through the Captain account; OpenClaw 2026.7.1 cannot resolve a `user:...` read target to an approved DM. Unlisted DMs and group/channel reads remain blocked, and Captain's account-level DM disable controls remain authoritative. The script backs up the installed Slack runtime, OpenClaw config, and original cron message before applying the patch. Deployment uses an exclusive lock, phase journal, checksummed postconditions, and compensating rollback before recording a local audit event.

To restore the exact pre-hotfix runtime and channel entries:

```bash
# Restore the files and settings saved before the hotfix was applied.
python3 scripts/deploy_openclaw_slack_dm_read_hotfix.py --rollback
```

The live Slack DM authorization regression test is opt-in because it requires
Captain's local Slack credentials and mutable status state:

```bash
# Run the optional live Slack DM test using Captain's local credentials.
CAPTAIN_LIVE_SLACK_TEST=1 python3 tests/test_openclaw_slack_dm_reads.py
```
