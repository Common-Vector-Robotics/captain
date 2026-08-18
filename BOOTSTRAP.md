# Set up Captain

Captain installs with `DailyLoop` off and its heartbeat disabled at `0m`.
Follow the complete [README installation guide](README.md#install-and-set-up).
Do not switch to `shadow` or `live` until the installation checker passes.
The packaged schedule defaults to `America/Detroit`; choose and validate your
team's timezone in the source package before installing the Claw.

## Installed-workspace checklist

Run these steps from Captain's installed workspace, normally
`~/.openclaw/workspace-captain`.

1. Preserve existing local memory files. Create them only when missing:

   ```bash
   for file in MEMORY.md USER.md; do
     if [ ! -e "$file" ]; then
       install -m 600 /dev/null "$file"
     fi
   done
   ```

2. Add ClickUp credentials to `.secrets/clickup.env` and verify a read-only
   task fetch.

3. Copy `data/meeting-ingestion.example.json` to
   `data/meeting-ingestion.json`. Configure and verify the intended Google
   account with the exact read-only scopes listed in the README.

4. Configure the dedicated OpenClaw Slack account named `captain`. Copy
   `data/captain-channels.example.json` to `data/captain-channels.json` and
   replace every example user and channel ID.

5. Bind the Slack account to the Captain agent:

   ```bash
   openclaw agents bind --agent captain --bind slack:captain
   openclaw gateway restart
   openclaw agents bindings --agent captain --json
   ```

6. Install the heartbeat safety policy while leaving its schedule disabled:

   ```bash
   python3 scripts/install_heartbeat_policy.py
   ```

7. Run the complete read-only installation checker:

   ```bash
   python3 scripts/check_install.py \
     --expect-mode off \
     --expect-heartbeat 0m
   ```

   Continue only after it prints
   `[PASS] Captain is ready for shadow mode.`

8. Set `DailyLoop` to `shadow`, enable the verified heartbeat, and restart the
   Gateway:

   ```bash
   python3 scripts/captain_modes.py dailyloop \
     --audience shadow \
     --user-id YOUR_SLACK_USER_ID \
     --source initial-setup

   python3 scripts/install_heartbeat_policy.py --enable
   openclaw gateway restart
   python3 scripts/check_install.py \
     --expect-mode shadow \
     --expect-heartbeat 60m
   ```

9. Run all six scheduled jobs one at a time in shadow. Confirm the intended
   Slack account, recipients, Google account, and ClickUp board. Confirm that
   no ClickUp task changes occur.

10. Use the README's separate **Going live** checklist only after every shadow
    test passes.

Captain's core installation uses the Python standard library. Install
`requirements.txt` only if you choose the optional Sentry integration.

Configured routing, credentials, runtime state, ClickUp exports, audit logs,
and meeting content remain local to the installed Captain workspace.
