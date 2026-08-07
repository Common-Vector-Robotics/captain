# Captain

Captain is an openclaw-based project manager for technical teams. Captain automatically manages Clickup tasks, checks in with team members over Slack to receive status updates, infers critical paths, and coordinates team members to remove blockers and prioritize important tasks. Captain also reads and infers tasks based on meeting transcripts sent to his configured email address.

## Install and set up

Captain is packaged as an OpenClaw Claw (note: .claw packages are still experimental). It installs Captain as a new agent with four weekday daily-loop jobs plus daily reporting in every`DailyLoop` mode, including `off`. The four operational jobs remain off until you configure them locally.

### Prerequisites

- A working [OpenClaw installation](https://docs.openclaw.ai/) with its Gateway and Slack channel configured
- Python 3
- A ClickUp API key and ClickUp team ID
- A Slack account dedicated to Captain, plus the user and channel IDs used in`data/captain-channels.json`

### 1. Install the Claw

Run these commands from this package directory (the directory containing `CLAW.md`). Before creating the install plan, set `AUTHORIZED_TOGGLE_USERS` to the Slack user IDs and names allowed to switch Captain between `off`, `shadow`, and `live`:

```bash
${EDITOR:-vi} scripts/captain_modes.py
```

Then inspect and preview the package:

```bash
export OPENCLAW_EXPERIMENTAL_CLAWS=1
openclaw claws inspect .
openclaw claws add . --dry-run --json
```

Review every action in the dry-run output and copy its `planIntegrity` value.
Replace `SHA256_FROM_DRY_RUN` below with that value, then apply the exact plan:

```bash
openclaw claws add . \
  --yes \
  --plan-integrity SHA256_FROM_DRY_RUN
```

`--yes` alone is intentionally insufficient. OpenClaw rejects the install if the package, destination, or live configuration changed after the dry run.

Confirm the installed agent and note the workspace path reported by OpenClaw:

```bash
openclaw claws status captain --json
openclaw doctor
```

The default workspace is `~/.openclaw/workspace-captain`. If the install plan reported a different path, use that path in the remaining commands.

### 2. Install the Python dependency

```bash
cd ~/.openclaw/workspace-captain
python3 -m pip install --user -r requirements.txt
```

Homebrew-managed Python may reject that command with `error: externally-managed-environment`. In that case, install into its user site explicitly:

```bash
python3 -m pip install --user --break-system-packages -r requirements.txt
```

### 3. Configure ClickUp

Create a local secrets file without committing it:

```bash
mkdir -p .secrets
chmod 700 .secrets
cat > .secrets/clickup.env <<'EOF'
CLICKUP_API_KEY=replace-with-your-clickup-api-key
CLICKUP_TEAM_ID=replace-with-your-clickup-team-id
EOF
chmod 600 .secrets/clickup.env
```

Verify the credentials with a read-only board fetch:

```bash
python3 scripts/fetch_clickup_tasks.py \
  --out /tmp/captain-clickup-smoke.json
```

### 4. Configure Slack routing and operators

```bash
cp data/captain-channels.example.json data/captain-channels.json
${EDITOR:-vi} data/captain-channels.json
python3 -m json.tool data/captain-channels.json >/dev/null
```

Replace every placeholder in `data/captain-channels.json`. Keep configured files local and do not commit credentials or live routing details.

### 5. Validate in shadow mode

Confirm that Captain starts in `off`, then enable `shadow` using an authorized Slack user ID:

```bash
python3 scripts/captain_modes.py status
python3 scripts/captain_modes.py dailyloop \
  --audience shadow \
  --user-id U0123456789 \
  --source initial-setup
openclaw cron list --agent captain
```

To test immediately, copy one Captain job ID from the cron list and run it:

```bash
openclaw cron run CAPTAIN_CRON_JOB_ID \
  --wait \
  --wait-timeout 10m
```

Inspect the configured shadow destination. Confirm that Captain uses the right ClickUp workspace, Slack account, recipients, and program channel before enabling live actions:

```bash
python3 scripts/captain_modes.py dailyloop \
  --audience live \
  --user-id U0123456789 \
  --source initial-setup
```

To stop operational actions while keeping the daily read-only activity report:

```bash
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

The cron bridge runs every 10 minutes via launchd on the Captain host, diffs `openclaw cron list --json` error counters, and heartbeats the `captain-openclaw-bridge` Sentry monitor (dead-man's switch — a missed check-in means the host, OpenClaw, or the bridge is down).

Deploy/refresh on the Captain host:

```bash
python3 -m pip install --user -r requirements.txt
```

On Homebrew-managed Python this fails outright with `error: externally-managed-environment` (PEP 668). Fix:

```bash
python3 -m pip install --user --break-system-packages -r requirements.txt
```

`--break-system-packages` installs into a Homebrew-managed Python's user site directory, which is why pip guards it by default.

Create `.secrets/sentry.env` (never committed; without it telemetry is a silent no-op):

```bash
mkdir -p .secrets
cat > .secrets/sentry.env <<'EOF'
SENTRY_DSN=<your project's Sentry DSN>
# SENTRY_ENVIRONMENT=captain-host   # optional, defaults to captain-host
EOF
```

`SENTRY_DSN` is required. Use the real DSN from the Sentry project settings — do not paste a placeholder that looks like a real one into any committed file.

```bash
mkdir -p logs  # logs/ is gitignored; launchd will not create it and the plist fails to start without it
```

launchd runs the bridge via `/usr/bin/env python3` with`PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`, so `python3` there resolves to whichever interpreter is first on that PATH — not necessarily the one you just installed `sentry-sdk` for. If they differ, the bridge still runs (the SDK import is lazy and no-ops on failure) but silently sends no events or check-ins, and the dead-man's-switch monitor will report the bridge as down. Verify with the same interpreter resolution launchd uses before (or alongside) loading the plist:

```bash
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin /usr/bin/env python3 -c "import sentry_sdk; print(sentry_sdk.VERSION)"
```

If this fails with `ModuleNotFoundError`, install the dependency for that specific interpreter (rerun the pip command above using that same PATH) — installing it for your interactive shell's `python3` is not enough.

Before enabling the timer, run the bridge against the real, installed`openclaw` on this host and confirm the cron JSON field names actually parse:

```bash
python3 scripts/openclaw_cron_sentry_bridge.py --dry-run
```

Check the output: `jobs` should be greater than 0 and `counters_missing` should be `[]`. If every job shows up in `counters_missing`, OpenClaw's field names don't match what `job_view()` looks for, and the bridge will silently report zero failures forever while the dead-man's-switch still says it's healthy — fix the field mapping in `scripts/openclaw_cron_sentry_bridge.py` before loading the plist, not after.

`launchd/com.intermode.captain-sentry-bridge.plist` hardcodes `WorkingDirectory` and both `StandardOutPath`/`StandardErrorPath` to `/Users/owen/.openclaw/workspace-captain`. If you are deploying from a different clone or a different user's home directory, edit those three paths in the plist before copying it in.

```bash
cp launchd/com.intermode.captain-sentry-bridge.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.intermode.captain-sentry-bridge.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.intermode.captain-sentry-bridge.plist
```

Telemetry is inert without `.secrets/sentry.env` (never committed).

## OpenClaw Slack DM read hotfix

OpenClaw 2026.7.1 incorrectly applies Slack's group-channel policy to DM read targets. Captain's reviewed hotfix and exact compatible versions are recorded in`config/openclaw-slack-dm-read-hotfix.json`.

Preview or deploy the hotfix on the Captain host:

```bash
python3 scripts/deploy_openclaw_slack_dm_read_hotfix.py --dry-run
python3 scripts/deploy_openclaw_slack_dm_read_hotfix.py
```

The deployment only authorizes DM channel IDs already present in Captain's weekly check-in state. It also updates the weekly cron instruction to read the stored `D...` conversation ID through the Captain account; OpenClaw 2026.7.1 cannot resolve a `user:...` read target to an approved DM. Unlisted DMs and group/channel reads remain blocked, and Captain's account-level DM disable controls remain authoritative. The script backs up the installed Slack runtime, OpenClaw config, and original cron message before applying the patch. Deployment uses an exclusive lock, phase journal, checksummed postconditions, and compensating rollback before recording a local audit event.

To restore the exact pre-hotfix runtime and channel entries:

```bash
python3 scripts/deploy_openclaw_slack_dm_read_hotfix.py --rollback
```

The live Slack DM authorization regression test is opt-in because it requires
Captain's local Slack credentials and mutable status state:

```bash
CAPTAIN_LIVE_SLACK_TEST=1 python3 tests/test_openclaw_slack_dm_reads.py
```
