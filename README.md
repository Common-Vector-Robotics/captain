# Captain

![Captain Logo](image/README/captain_logo_2.png)

Captain is an openclaw-based project manager for technical teams. Captain automatically manages Clickup tasks, checks in with team members over Slack to receive status updates, infers critical paths, and coordinates team members to remove blockers and prioritize important tasks. Captain also reads and infers tasks based on meeting transcripts sent to his configured email address.

## What Captain does

<table>
  <tr>
    <td width="50%" valign="top">
      🎯 <strong>Sets the day's priorities</strong><br>
      Turns ClickUp, Slack, and critical-path context into a focused morning brief.
    </td>
    <td width="50%" valign="top">
      👤 <strong>Gives everyone a personal top two</strong><br>
      Sends each owner the two highest-leverage tasks for their day.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      🚧 <strong>Drives blockers to resolution</strong><br>
      Finds stuck work, follows up with the right owner, and escalates unresolved risks.
    </td>
    <td width="50%" valign="top">
      ✅ <strong>Keeps ClickUp honest</strong><br>
      Creates and updates tasks only when the evidence is clear—and audits every write.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      📡 <strong>Surfaces delivery risk</strong><br>
      Watches project signals, bench results, dependencies, and milestone health.
    </td>
    <td width="50%" valign="top">
      🛟 <strong>Rolls out safely</strong><br>
      Moves from <code>off</code> to <code>shadow</code> to <code>live</code> with visible action reporting.
    </td>
  </tr>
</table>

## A day with Captain

<p align="center">
  <strong>07:30 Align</strong> →
  <strong>14:00 Reconcile</strong> →
  <strong>15:15 Unblock</strong> →
  <strong>15:45 Verify</strong> →
  <strong>17:45 Close</strong>
</p>

```mermaid
flowchart TB
    ALIGN["`**07:30 - Align**
- Read ClickUp and overnight Slack
- Rank the highest-leverage work
- Result: Team top 3 + each owner's top 2`"]
    RECONCILE["`**14:00 - Reconcile**
- Read configured meeting evidence
- Audit every clear ClickUp change
- Result: Updated tasks, blockers + digest`"]
    UNBLOCK["`**15:15 - Unblock**
- Match and recheck every blocker
- Chase its owner or escalate the decision
- Result: No blocker ends the day unowned`"]
    VERIFY["`**15:45 - Verify**
- Watch supply, QA, test, and design signals
- Reconcile bench truth with the board
- Result: Hidden delivery risk surfaced`"]
    CLOSE["`**17:45 - Close**
- Sync final evidence-backed changes
- Check milestones and draft replan options
- Result: EOD wrap + tomorrow's top 3`"]
    REPORT["`**18:30 - Report**
- Read audit, state, and job history
- Surface missing or degraded runs
- Result: Read-only proof-of-life report`"]
    MONITOR["`**Hourly overnight - Monitor**
- Scan Slack and assess safety signals
- Page the responsible lead when genuine
- Result: Escalate immediately or stay silent`"]

    ALIGN --> RECONCILE --> UNBLOCK --> VERIFY --> CLOSE --> REPORT --> MONITOR
    MONITOR -. "Feeds the next 07:30 alignment" .-> ALIGN

    classDef weekday fill:#eff6ff,stroke:#2563eb,color:#0f172a
    classDef daily fill:#f5f3ff,stroke:#7c3aed,color:#0f172a
    classDef hourly fill:#fff7ed,stroke:#ea580c,color:#0f172a
    class ALIGN,RECONCILE,UNBLOCK,VERIFY,CLOSE weekday
    class REPORT daily
    class MONITOR hourly
```

## Install and set up

Captain is packaged as an OpenClaw Claw (note: `.claw` packages are still experimental). It installs Captain as a new agent with six scheduled jobs plus one Claw-managed hourly heartbeat in every `DailyLoop` mode, including `off`. The five weekday operational jobs remain off until you configure them locally.

### Prerequisites

- A working [OpenClaw installation](https://docs.openclaw.ai/) with its Gateway and Slack channel configured
- Python 3
- A ClickUp API key and ClickUp team ID
- A Slack account dedicated to Captain, plus the user and channel IDs used in `data/captain-channels.json`
- The `gog` Google CLI, authenticated to a Gmail account with Gmail, Drive, and Docs access

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

Run these commands from this package directory (the directory containing `CLAW.md`). Before enabling DailyLoop, copy `data/captain-channels.example.json` to the private `data/captain-channels.json` and configure its `mode_toggle_users` name-to-Slack-ID mapping. Only those Slack users can switch Captain between `off`, `shadow`, and `live`.

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

Do not enable or run Captain's heartbeat or scheduled jobs until this verification succeeds.
Keep the Gateway and Captain's scheduler stopped while applying the install plan and the
operator-owned heartbeat policy below.

```bash
# Install the exact package plan you just reviewed.
openclaw claws add . --yes --plan-integrity SHA256_FROM_DRY_RUN
```

`--yes` alone is intentionally insufficient. OpenClaw rejects the install if the package, destination, or live configuration changed after the dry run.

Confirm the installed agent and note the workspace path reported by OpenClaw:

```bash
# Confirm that Captain was installed and find its workspace path.
openclaw claws status captain --json
```

The default workspace is `~/.openclaw/workspace-captain`. If the install plan reported a different path, use that path in the remaining commands.

#### Install Captain's heartbeat safety rules

Move into Captain's installed workspace, then run the included setup command:

```bash
cd ~/.openclaw/workspace-captain
python3 scripts/install_heartbeat_policy.py
```

It safely previews the OpenClaw configuration change, installs the exact rules
from `HEARTBEAT.md`, and reads them back to make sure nothing changed. Success
looks like this:

```text
Captain heartbeat policy installed and verified.
SHA-256: <a long verification code>
```

If the command reports an error, stop and keep Captain's heartbeat and scheduled
jobs disabled. Do not continue until it succeeds.

Run this command again after every Claw update, before restarting Captain.
OpenClaw may then describe Captain as locally modified; that is expected because
this safety setting is stored on your machine. Do not delete it to clear that
status.

```bash
# Check the rest of your OpenClaw setup for problems after prompt verification.
openclaw doctor
```

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

### 4. Configure meeting ingestion

Captain supports Gemini meeting-note emails in Gmail whose links open Notes and Transcript
sections in Google Docs. Copy the example, then replace the sample account and meeting-title
patterns with values for your team:

```bash
# Create the local ingestion configuration in Captain's installed workspace.
cd ~/.openclaw/workspace-captain
cp data/meeting-ingestion.example.json data/meeting-ingestion.json
nano data/meeting-ingestion.json

# Confirm that the configuration is valid JSON.
python3 -m json.tool data/meeting-ingestion.json >/dev/null
```

The configured `google_cli` defaults to `gog`. Authenticate `google_account` with only
the Gmail, Drive, and Docs read scopes Captain needs:

```bash
gog auth add captain@example.com --services gmail,drive,docs --readonly --drive-scope readonly
```

Validate both the refresh token and its stored scopes non-interactively:

```bash
gog auth list --check --account captain@example.com --no-input --json
```

In the returned JSON, locate exactly one record for `captain@example.com`, require
`valid: true`, and require its `scopes` set to contain exactly:

- `email`
- `openid`
- `https://www.googleapis.com/auth/userinfo.email`
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/documents.readonly`

No other stored scope is allowed. If broader historical grants appear, do not proceed.
Remove the local token with `gog auth remove captain@example.com`, revoke the
application's prior Google account grants in Google Account security, then reauthorize
non-incrementally with the exact least-privilege command above. Prefer a dedicated Google
account so unrelated grants cannot accumulate on Captain's identity.

For a headless service, `gog auth keyring file` selects the encrypted file keyring;
`GOG_KEYRING_BACKEND=file` can enforce that backend. Inject `GOG_KEYRING_PASSWORD` from
an owner-only service environment or secret manager. Never put its value in this file,
tracked configuration, command-line arguments, shell history, or logs.

Do not put a password, OAuth token, or client secret in `meeting-ingestion.json`. The
scheduled job never starts an interactive OAuth flow. `sender`, `subject_prefixes`, and
`meeting_title_patterns` control discovery; `lookback_days` controls partial-note retries;
`local_summary_directory` may be a readable local directory or `null`.

The default reconciliation schedule is 14:00 on weekdays in `America/Detroit`. It should
run after Gemini has produced the Transcript. To use another cadence or timezone, edit the
`meeting-transcript-reconciliation` entry in `CLAW.md` before inspecting and installing the
package.

### 5. Connect Captain to Slack

Captain requires a dedicated Slack app and bot. Follow the maintained [OpenClaw Slack setup guide](https://docs.openclaw.ai/channels/slack) to create the app, configure its scopes and events, install the Slack plugin, and store its tokens securely.

For Captain specifically:

- Name the OpenClaw Slack account `captain` (`channels.slack.accounts.captain`).
- Enable DMs so Captain can send owner check-ins and receive replies.
- Invite the bot to the program channel, shadow destination, reporting destination, and
  every channel Captain should monitor. Captain can only see channels the bot has joined.

Recommended channels:

- `#captains-quarters` — the team-facing program channel for morning briefs, blocker and
  bench digests, end-of-day wraps, and incident threads. Use it as `program_channel`.
- `#dry-dock` — a private operator channel for shadow-mode previews and Captain's daily
  activity report. Use it as `shadow_recipient` and, unless you want a separate reporting
  channel, `activity_digest_channel`.
- Keep `"slack_account": "captain"` in `data/captain-channels.json` aligned with the
  OpenClaw account name. A mismatched account or missing channel membership can surface as
  a misleading `channel_not_found` error.
- The Claw-managed hourly heartbeat uses this same configured `captain` Slack binding for
  any permitted incident routing.

Also invite Captain to the team's existing project and operations channels that it should
monitor; those channels do not need Captain-specific names.

Verify the connection before configuring Captain's routing:

```bash
# Confirm that OpenClaw can authenticate the Captain Slack account.
openclaw channels status --probe --json
```

### 6. Configure Slack routing and operators

```bash
# Copy the example Slack settings into a local configuration file.
cp data/captain-channels.example.json data/captain-channels.json

# Open the local configuration and replace its placeholder values.
nano data/captain-channels.json

# Check that the edited file contains valid JSON.
python3 -m json.tool data/captain-channels.json >/dev/null
```

Before continuing, verify that daily reporting has an explicit Slack account
and destination:

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path("data/captain-channels.json")
try:
    config = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"Cannot read {path}: {error}") from error
if not isinstance(config, dict):
    raise SystemExit(f"Captain Slack routing configuration in {path} must be a JSON object")

required = ("activity_digest_channel", "slack_account")
missing = [key for key in required if not isinstance(config.get(key), str) or not config[key]]
if missing:
    raise SystemExit(f"Captain Slack routing configuration missing in {path}: {', '.join(missing)}")
print("Captain Slack routing verified: " + ", ".join(required))
PY
```

Do not continue until this prints `Captain Slack routing verified`.

Replace every placeholder in `data/captain-channels.json`, including the
`mode_toggle_users` name-to-Slack-ID mapping. That private mapping is the only
authorization for DailyLoop mode changes; missing or invalid entries fail
closed. Keep configured files local and do not commit credentials or live
routing details. `program_channel` accepts either the example `{name,id}` object
or a non-empty string. The object form gives shadow previews a readable `#name`
while preserving its exact `id` as the live delivery target; a string remains the
exact live target. The live `data/captain-modes.json` file is runtime state: it
is not installed from this package, remains off when absent, and is created by
the first authorized mode change.

### 7. Validate in shadow mode

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

To test ingestion immediately, copy the job ID shown for
`Captain meeting transcript reconciliation` and run it:

```bash
# Run the meeting reconciliation job now. Replace MEETING_CRON_JOB_ID.
openclaw cron run MEETING_CRON_JOB_ID \
  --wait \
  --wait-timeout 10m
```

Inspect the configured shadow destination. Confirm that the meeting job read both Transcript
and Notes, sent output only to `shadow_recipient`, used the intended Google account and
ClickUp board, and made no ClickUp changes. Then confirm the remaining Captain jobs use the
right Slack account, recipients, and program channel before enabling live actions:

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

The package intentionally excludes credentials, configured Slack and mailbox routing,
runtime state, ClickUp exports, audit logs, local reports, and raw meeting content.

This repository contains Captain's source prompts, persona files, scripts, and non-sensitive fixtures.

Excluded from git by design:

- secrets and environment files
- local OpenClaw runtime state
- SQLite databases and mutable cron state
- raw emails, transcripts, meeting summaries, screenshots, generated reports, and ClickUp exports
- audit and approval queues that may contain live operational details

Runtime state remains on the Captain host unless explicitly exported through a reviewed process.

# Optional Extras

## Sentry telemetry

Captain can report hard failures from Captain scripts and OpenClaw cron jobs to a Sentry project.

Captain's scripts send an event to Sentry when they crash. The optional cron
bridge compares each OpenClaw job's error counter against the previous run and
reports newly failed jobs. You can run it manually or, on macOS, generate a
host-specific `launchd` definition for it.

Each bridge run also checks in with the `captain-openclaw-bridge` Sentry monitor, which acts as a dead-man's switch: a missed check-in means the host, OpenClaw, or the bridge itself is down.

Without `.secrets/sentry.env`, every telemetry call is a silent no-op and Captain behaves exactly as it does today. Run these steps on the Captain host, from its workspace directory (`~/.openclaw/workspace-captain` by default).

### 1. Add your Sentry DSN

Create the settings file. It is never committed:

```bash
# Create the private secrets folder if it does not already exist.
mkdir -p .secrets

# Make the folder accessible only to your user account.
chmod 700 .secrets

# Create the Sentry settings file. Replace the DSN placeholder.
cat > .secrets/sentry.env <<'EOF'
SENTRY_DSN=<your project's Sentry DSN>
# SENTRY_ENVIRONMENT=captain-host   # optional, defaults to captain-host
EOF

# Allow only your user account to read or edit the settings file.
chmod 600 .secrets/sentry.env
```

Telemetry also needs the `sentry-sdk` package, which came from [step 2](#2-install-python-dependencies). If you skipped that step, run `python3 -m pip install --user -r requirements.txt` now, adding `--break-system-packages` if Python reports `error: externally-managed-environment`.

### 2. Confirm that events reach Sentry

```bash
# Send one test event to confirm the Sentry connection works.
python3 scripts/captain_telemetry.py --self-test
```

Expected output is `{"ok": true, "sent": true}`, followed within a minute by a `captain-telemetry self-test` event in your Sentry project. Resolve that event once you see it.

If the output is `{"ok": false, "error": "telemetry inactive ..."}`, one of three things is true: `SENTRY_DSN` is missing or empty, `sentry-sdk` is not installed for this Python, or `CAPTAIN_SENTRY_DISABLED=1` is set in the environment. All three are deliberate no-ops, so nothing else in the output will tell you which one it is — check them in that order.

### 3. Preview the cron-failure bridge

See what the bridge would report before it can send anything:

```bash
# Show what the bridge would report, without sending events or check-ins.
python3 scripts/openclaw_cron_sentry_bridge.py --dry-run
```

Expected output looks like this: `jobs` greater than 0, `counters_missing` empty, `truncated` false:

```json
{
  "ok": true,
  "dry_run": true,
  "jobs": 27,
  "would_report": [],
  "counters_missing": [],
  "truncated": false
}
```

If `truncated` is `true`, OpenClaw returned only the first page of its job list and the jobs beyond it are unmonitored. The bridge reports this to Sentry as a warning too, because `openclaw cron list --json` offers no way to page through the rest.

If every job is listed in `counters_missing`, OpenClaw's field names don't match what `job_view()` looks for, and the bridge will silently report zero failures forever while the dead-man's switch still says it's healthy. Fix the field mapping before relying on the bridge:

```bash
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

# Re-verify.
python3 -m pytest tests/test_openclaw_cron_sentry_bridge.py -v
python3 scripts/openclaw_cron_sentry_bridge.py --dry-run
```

### 4. Run the Sentry bridge automatically (optional)

Skip this section if you are not using Sentry monitoring. The setup command
automatically creates the right service files for macOS or Linux. It does not
start anything until you run the final operating-system command shown below.

#### macOS

From the Captain workspace, run:

```bash
# launchd needs the log directory before the bridge starts.
mkdir -p logs

# Create the macOS service file.
PLIST_PATH="$HOME/Library/LaunchAgents/ai.openclaw.captain-sentry-bridge.plist"
python3 scripts/render_sentry_service.py \
  --workspace "$PWD" \
  --output "$PLIST_PATH"

# Replace an existing bridge job, if any, and load the generated plist.
launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
```

The generated `ai.openclaw.captain-sentry-bridge.plist` runs immediately and
every 10 minutes. To stop it later, run:

```bash
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/ai.openclaw.captain-sentry-bridge.plist"
```

#### Linux

From the Captain workspace, run:

```bash
UNIT_DIR="$HOME/.config/systemd/user"
python3 scripts/render_sentry_service.py \
  --workspace "$PWD" \
  --output "$UNIT_DIR"

systemctl --user daemon-reload
systemctl --user enable --now ai.openclaw.captain-sentry-bridge.timer
```

The timer runs the bridge immediately and then 10 minutes after each completed
run. Inspect it with `journalctl --user -u ai.openclaw.captain-sentry-bridge`.
To stop it later, run:

```bash
systemctl --user disable --now ai.openclaw.captain-sentry-bridge.timer
```

### Turning telemetry off

To silence all telemetry, delete `.secrets/sentry.env` or set `CAPTAIN_SENTRY_DISABLED=1`. Either one returns every telemetry call to being a no-op; nothing else about Captain changes.

See [`TOOLS.md`](TOOLS.md) for the telemetry rules new Captain scripts must follow.
