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

3. Copy `data/captain-channels.example.json` to
   `data/captain-channels.json`, then set your Slack account, program channel,
   shadow destination, daily reporting destination, administrators, and any
   excluded users. Do not commit this configured file.

4. Review the four installed weekday schedules. They run at 07:30, 15:15,
   15:45, and 17:45 in `America/Detroit`. Edit the package before installation
   if your operating cadence or timezone differs.

5. Run the first cycle in shadow mode and inspect the output destination:

   ```bash
   python3 scripts/captain_modes.py dailyloop --audience shadow \
     --user-id <your-slack-user-id> --source initial-setup
   ```

6. Switch to `live` only after the shadow run shows the right ClickUp board,
   Slack account, recipients, and channel.

Captain never carries secrets in the Claw package. Local state, ClickUp exports,
audit logs, and configured Slack routing remain in the agent workspace.
