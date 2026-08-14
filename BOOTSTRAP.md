# Set up Captain

Captain is installed safely with `DailyLoop` off. Do not switch it to `shadow`
or `live` until all of the following are complete.

After the heartbeat policy is verified, daily reporting remains enabled in
`off`, `shadow`, and `live` so you always have a read-only monitor of what
Captain did.

Captain's isolated heartbeat uses `lightContext: true`, which does not inject workspace
bootstrap files. In the current experimental Claw format, the Claw profile schema does not
own `heartbeat.prompt`; the managed `HEARTBEAT.md` policy must therefore be installed as the
operator-owned runtime prompt. Do not enable or run Captain's heartbeat or scheduled jobs
until this verification succeeds.

#### Install the operator-owned heartbeat policy

From the installed Captain workspace, run this block exactly before continuing setup. It
passes the JSON value directly as a process argument, dry-runs the configuration change,
applies it, and fails unless the runtime read-back is byte-identical to `HEARTBEAT.md`.

```bash
python3 - <<'PY'
from pathlib import Path
import hashlib
import json
import subprocess

policy_path = Path("HEARTBEAT.md").resolve(strict=True)
policy_bytes = policy_path.read_bytes()
policy = policy_bytes.decode("utf-8")
config_path = "agents.entries.captain.heartbeat.prompt"
encoded_policy = json.dumps(policy, ensure_ascii=False)
set_command = [
    "openclaw", "config", "set", config_path, encoded_policy, "--strict-json",
]

subprocess.run([*set_command, "--dry-run"], check=True)
subprocess.run(set_command, check=True)
result = subprocess.run(
    ["openclaw", "config", "get", config_path, "--json"],
    check=True,
    capture_output=True,
    text=True,
)
actual = json.loads(result.stdout)
if not isinstance(actual, str):
    raise SystemExit("Captain heartbeat prompt read-back is not a string")
actual_bytes = actual.encode("utf-8")
if actual_bytes != policy_bytes:
    raise SystemExit("Captain heartbeat prompt does not exactly match HEARTBEAT.md")
expected_hash = hashlib.sha256(policy_bytes).hexdigest()
actual_hash = hashlib.sha256(actual_bytes).hexdigest()
if actual_hash != expected_hash:
    raise SystemExit("Captain heartbeat prompt SHA-256 verification failed")
print(f"Captain heartbeat prompt verified: sha256={actual_hash}")
PY
```

The prompt is an operator-controlled modification outside the Claw digest. Reapply and
reverify the prompt after every Claw update before restarting Captain; do not remove it only
to make agent state appear unmodified. Continue only after the command prints the verified
SHA-256. Any error or mismatch is a fail-closed stop.

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
