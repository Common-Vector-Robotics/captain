# Set up Captain

Captain is installed safely with `DailyLoop` off. Do not switch it to `shadow`
or `live` until all of the following are complete.

Daily reporting remains enabled in `off`, `shadow`, and `live` so you always
have a read-only monitor of what Captain did.

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
   shadow destination, daily reporting destination, administrators, and any
   excluded users. Do not commit this configured file.

5. Review the five installed weekday schedules. They run at 07:30, 14:00,
   15:15, 15:45, and 17:45 in `America/Detroit`. The 14:00 meeting job must run
   after Gemini has produced the Transcript. Edit `CLAW.md` before installation
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
