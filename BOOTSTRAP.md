# Set up Captain

Captain is installed safely with `DailyLoop` off. Do not switch it to `shadow`
or `live` until all of the following are complete.

After the heartbeat policy is verified, daily reporting remains enabled in
`off`, `shadow`, and `live` so you always have a read-only monitor of what
Captain did.

Captain's heartbeat uses a lightweight OpenClaw mode. Before enabling Captain,
run the included setup command below. It gives OpenClaw Captain's heartbeat
safety rules and verifies that they were copied correctly.

#### Install Captain's heartbeat safety rules

From the installed Captain workspace, run:

```bash
python3 scripts/install_heartbeat_policy.py
```

Continue only after it prints `Captain heartbeat policy installed and verified`.
If it reports an error, keep the heartbeat and scheduled jobs disabled.

Run the command again after every Claw update, before restarting Captain.
OpenClaw may report Captain as locally modified because this safety setting is
stored on your machine. That is expected; do not remove it to clear the status.

Create blank local `MEMORY.md` and `USER.md` files for Captain to maintain.
Preserve existing files; never replace them during installation or an update.

```bash
for file in MEMORY.md USER.md; do
  if [ ! -e "$file" ]; then
    install -m 600 /dev/null "$file"
  fi
done
```

1. Install Python requirements:

   ```bash
   python3 -m pip install -r requirements.txt
   ```

2. Provide ClickUp credentials through environment variables or
   `.secrets/clickup.env`:

   ```text
   CLICKUP_API_KEY=...
   CLICKUP_TEAM_ID=...
   ```

3. Install and authenticate the configured `gog` Google CLI for Gmail, Drive,
   and Docs. Copy `data/meeting-ingestion.example.json` to
   `data/meeting-ingestion.json`, set the Google account and meeting filters,
   and keep passwords and OAuth material out of that file.

4. Copy `data/captain-channels.example.json` to
   `data/captain-channels.json`, then set your Slack account, program channel,
   shadow destination, daily reporting destination, administrators, authorized
   `mode_toggle_users`, and any excluded users. Do not commit this configured
   file. A missing `data/captain-modes.json` remains fail-closed/off until an
   authorized operator explicitly initializes it with the mode command.

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

5. Review the six scheduled jobs plus one Claw-managed hourly heartbeat. The five
   weekday operational schedules run at 07:30, 14:00, 15:15, 15:45, and 17:45 in
   `America/Detroit`; the daily reporting job runs at 18:30. The 14:00 meeting job
   must run after Gemini has produced the Transcript. The heartbeat continues to route
   through the configured `captain` Slack binding. Edit `CLAW.md` before installation
   if your operating cadence or timezone differs.

6. Run the first cycle in shadow mode and inspect the output destination:

   ```bash
   python3 scripts/captain_modes.py dailyloop --audience shadow \
     --user-id <your-slack-user-id> --source initial-setup
   ```

7. Run `Captain meeting transcript reconciliation` once in shadow. Confirm it
   reads both Transcript and Notes, sends only to `shadow_recipient`, and leaves
   ClickUp unchanged.

8. Switch to `live` only after the shadow runs show the right Google account,
   ClickUp board, Slack account, recipients, and channel.

Captain never carries secrets in the Claw package. Local state, ClickUp exports,
audit logs, configured Slack/mailbox routing, and raw meeting content remain in
the agent workspace.
