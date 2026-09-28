# TOOLS.md - Captain Local Notes

## ClickUp

Use direct ClickUp REST API for deterministic reads/writes. ClickUp writes are autonomous
and audited per the daily-loop flowchart: create/status/comment/due-date writes execute
directly through the audited tooling below when the evidence is clear, with every real
mutation recorded in `data/audit-log.jsonl`. The approval queue (`data/approval-queue.jsonl`)
is now reserved for genuinely ambiguous items — an uncertain owner/task match, or
conflicting Admin instructions — not a default gate on autonomous writes.

Required env vars for live ClickUp reads:

- `CLICKUP_API_KEY`
- `CLICKUP_TEAM_ID`

Captain reads ClickUp credentials from the local runtime environment or
`.secrets/clickup.env`. That file may be a regular owner-private file or an
operator-managed symlink; never print it or expose token values.

The ClickUp scripts prefer exported environment variables and otherwise load these keys from `.secrets/clickup.env` automatically. Run the documented commands directly; manual `source`/`set -a` bootstrapping is not required.

Optional pilot filter:

- `CAPTAIN_CLICKUP_LIST_IDS` comma-separated list IDs to read (unset reads the whole team)

Workspace rules (read by `scripts/clickup_workspace.py`; see the next section):

- `CAPTAIN_CLICKUP_WORKSPACE_CONFIG` — path to the private workspace config; when unset, `data/clickup-workspace.json` (gitignored) is used
  (identity map, folder owners, Inbox, subsystem `lists`, `folder_keywords`, blocklist,
  statuses, tags). Keep it outside the
  repo. The ClickUp writer refuses to run, even for a dry run, when it is unset or the
  file is missing; read-only scripts fall back to generic rules. Schema and placeholder
  values: `data/clickup-workspace.example.json`.
- `CAPTAIN_INBOX_LIST_ID` — overrides the Inbox list id (default `1400460000001206`)
- `CAPTAIN_ARCHIVED_LIST_IDS` — comma-separated ids added to the archived blocklist

## ClickUp workspace structure (reset 2026-09-27)

Workspace `90132441412` has three spaces:

- **GL-1** (`901313870897`) and **Ghostrunner** (`901313552943`) — product spaces. Each
  subsystem is a folder with exactly one list: Structures, Chassis, Power, Battery,
  Harness, Sensors & Compute, Safety, Autonomy, Firmware, HMI (GL-1 only), Integration,
  Projects.
- **OPS** (`901313619708`) — operations, including the **Inbox** list
  (`1400460000001206`).

Folder owners: each subsystem folder has one default owner (the default `Proposed
owner:`), recorded in the private config's `folder_owners`. Projects has no default
owner.

Statuses:

- Product spaces: `backlog` (open), `ready`, `in progress`, `blocked`, `in review`
  (custom), `cancelled` (done type), `done` (closed).
- OPS: `intake`, `planning`, `in progress`, `blocked`, `in review`, `complete`,
  `cancelled`.
- Scripts decide open vs finished from the ClickUp status `type` (`done` and `closed`
  are finished); status names are only a fallback when an export has no type.
  "Not started" means type `open` or `backlog`, `ready`, `intake`, `planning`.

Milestones: each release has one Milestone task in its Integration list (for example
"V1.1 on ground"). Humans create it and add its dependencies. Captain never creates a
Milestone and never flags one as an owner gap, a blocker, or not-started work; it reads
risk from the dependencies. An overdue Milestone is still reported.

Write rules, enforced by `scripts/clickup_write.py`:

1. **Create in the Inbox unless the folder is certain.** Create with `create-task
   --route-from "<the evidence text>"`: `route_destination` in
   `scripts/clickup_workspace.py` files the task straight into a subsystem folder's list
   (the config's `lists` map) only when the text names the product (GL-1/GL1, Ghostrunner,
   or AFS → GL-1, Newlab → Ghostrunner) and either names exactly one folder (rule
   `named-folder`) or matches exactly one folder in `folder_keywords` while the assignee is
   that folder's owner (rule `keyword-owner`); anything else goes to the Inbox (rule
   `inbox`). The result, dry run, and audit carry `route_rule` and `route_reason`; say in
   the Slack summary which rule fired. A direct filing carries a `Filed directly:
   <Space>/<Folder> (<rule>)` line (added by `--route-from`, required otherwise). Projects,
   OPS, and archived lists are always refused. Humans move Inbox items to their folder.
   Updates and comments on existing tasks are allowed anywhere except the archived lists.
2. **Never write archived lists:** `901326347060`, `901327700142`, `901324583541`,
   `901326084934`, `901326085162`, `901326085192`, `901326085207`, `901326085239`,
   `901327546010`. Updates and comments on tasks in those lists are refused.
3. **Inbox description template.** The writer refuses a create whose description does
   not start with `Done when:` or lacks a `Proposed folder:` line:

   ```text
   Done when: <observable completion condition>
   Proposed folder: <Space>/<Folder>   (or "A or B" when unsure)
   Proposed owner: <name> (folder owner default)
   Evidence: <permalink or source>
   ```

4. **One owner, native assignee only.** At most one `assignee` per task: a name, alias,
   or numeric id from the identity map in the private workspace config. `update-task
   --assignee` replaces the current assignee (every other assignee is removed). Unknown
   or departed people are refused; Captain never guesses. Members marked
   `"assignable": false` (the agent service account) are never assignees. Inbox tasks may stay unassigned; they are not owner gaps.
5. **No custom fields.** Captain never creates or writes a custom field, an Owners
   label, or a task type. Operations carrying `owner`, `owners`, `custom_fields`, or
   `custom_item_id` are refused.
6. **Tags:** only `safety` and `customer-visible`, and only on `create-task`. Other tags
   are dropped and listed in the result's `dropped_tags`; report them.

## Google meeting ingestion

The weekday `meeting-transcript-reconciliation` cron reads
`cron-prompts/meeting-transcript-clickup-reconciliation.md`. It discovers configured
Gemini meeting-note emails through an authenticated `gog` CLI, analyzes the Google Docs
Transcript first and Notes second, then reconciles only unambiguous changes into ClickUp.

- Example configuration: `data/meeting-ingestion.example.json`
- Local configuration: `data/meeting-ingestion.json` (never commit)
- Runtime state: `data/meeting-transcript-clickup-reconciliation-state.json`
- Required Google scopes: Gmail, Drive, and Docs for the configured account
- Default schedule: weekdays at 14:00 `America/Detroit`, editable in `CLAW.md` before install

The configuration stores discovery settings, not credentials. Never store raw email,
Transcript, or Notes content in tracked files, audit logs, or Slack; the prompt uses short
timestamped paraphrases as evidence.

## Storage

- SQLite DB: `data/captain.sqlite`
- Audit log: `data/audit-log.jsonl`
- Approval queue: `data/approval-queue.jsonl`

## Scripts

- `scripts/captain_db.py init`
- `scripts/fetch_clickup_tasks.py --out <relative-output-path>`
- `scripts/clickup_write.py --execute create-task --route-from "<the evidence text>" --name <name> --description <Inbox description> [--assignee <person>] [--tag safety]`
  — a subsystem folder list when certain, otherwise the Inbox (`--list-id` accepts only
  the Inbox or a `lists` entry; batch operations take `route_from`)
- `scripts/clickup_write.py --execute update-task --task-id <task_id> --status <status> [--assignee <person>]`
- `scripts/clickup_write.py --execute comment-task --task-id <task_id> --text <comment_text>`
- `scripts/clickup_write.py --execute batch --operations-file <batch.json>`
- `scripts/blocker_ledger.py add|update|list` — same-cycle blocker ledger (daily loop)
- `scripts/daily_cycle.py set-top3|set-tomorrow|stamp|get` — per-date top-3 and phase stamps
- `scripts/daily_context.py --clickup <export>` — morning board buckets + snapshot
- `scripts/personal_top2.py rank|set|get` — per-person top-2 ranking for the morning
  cycle's step 6b personal texts. `rank --clickup <export> [--date] [--critical-paths]`
  prints each person's ranked candidates with a plain-language reason and writes
  nothing; `set --date <d> --items <json>` persists the top-2 actually sent to the
  `daily_cycle.personal_top2` column; `get --date <d>` reads it back. Read-only toward
  ClickUp — it never writes to the board. The morning-cycle prompt owns the tier ladder
  and recipient-resolution rules.
- `scripts/daily_wrap.py --morning <snap> --eod <export>` — EOD deltas + milestone risk
- `scripts/captain_modes.py dailyloop --audience off|shadow|live --user-id <id>`
- `scripts/captain_activity.py [HOURS]` — read-only chronological viewer merging cron
  runs, per-cron `runs[]` decision history (this is where no-ops surface), last-run/flag
  state, and the audit log into one time-sorted feed; default 24 hours.
- `scripts/daily_activity_digest.py [--hours N] [--json] [--post]` — Action summary
  reporting posted to the configured `activity_digest_channel`, mechanically generated
  (never LLM-written) from
  `captain_activity.py`'s own
  collectors. Runs in EVERY `DailyLoop` audience, including `off` — the one deliberate
  exception to the mode gate, safe because it is strictly read-only and its only side effect
  is one Slack post. `--post` is not the default (print-only, matching `--execute`
  elsewhere). Any Slack user id in the rendered text is shown as `Name (Uxxxxxxxx)` via
  `scripts/slack_user_names.py`'s resolver (never for `--json`, which stays raw).
- `scripts/slack_user_names.py` — shared library (no CLI of its own) behind the id -> name
  rendering above: `SlackNameResolver` resolves a Slack user id to `Name (Uxxxxxxxx)` via
  `admin_recipients` in `data/captain-channels.json`, then `data/slack-user-cache.json`, then
  falls back to the bare id — never fabricating a name.
## ClickUp batch writes

Use one batch command rather than a shell loop. The JSON input is an array (or an object with an `operations` array) of `create-task`, `update-task`, or `comment-task` objects. Every object needs an `operation_id` so the result can identify the safe retry subset.

```json
{
  "operations": [
    {
      "operation_id": "task-1",
      "command": "create-task",
      "list_id": "1400460000001206",
      "name": "Investigate controller fault",
      "description": "Done when: fault root cause is written up and a fix is filed\nProposed folder: GL-1/Firmware\nProposed owner: <name> (folder owner default)\nEvidence: <Slack permalink>",
      "status": "intake",
      "assignee": "person-d",
      "tags": ["safety"],
      "source": "explicit Slack request"
    }
  ]
}
```

`assignee` is one value (a name, alias, or numeric id); a list with more than one entry is
refused. The Inbox is an OPS list, so a new task's `status` must be an OPS status
(`intake` in the example) or omitted to take the list default; with `route_from` the
destination may be a product list, so omit `status` there.

The writer validates every requested status against the destination list's real statuses,
exactly (case-insensitive) and without aliases: a status the list does not define is never
rewritten to another one. A requested `blocked` status that the destination list does not
support is left unchanged (`needs_blocked_status` is set on the audit record and the
operation result so the list can be flagged for a human). Other unsupported statuses are
returned in `failed` with the allowed values and are never sent to ClickUp.

Completed batches always print `{ok, succeeded, failed}` and exit zero when individual operations are known to have failed. Render that JSON directly, retry only entries in `failed` whose error has `retryable: true`, and never rerun `succeeded`. An unavailable API may leave the active write `unknown` with `retryable: false`; reconcile that task in ClickUp before retrying it.

## Sentry (error telemetry)

All Sentry contact goes through `scripts/captain_telemetry.py`. Telemetry is
write-only toward Sentry: monitoring and debugging happen in the Sentry
dashboard (there is no auth token in this workspace). Without
`.secrets/sentry.env` (or with `CAPTAIN_SENTRY_DISABLED=1`), every telemetry
call is a silent no-op and scripts behave exactly as before.

Required env file for live telemetry (`.secrets/sentry.env`):

- `SENTRY_DSN` (required)
- `SENTRY_ENVIRONMENT` (optional, default `captain-host`)

Dependency: `sentry-sdk` (see `requirements.txt`).

Setup and troubleshooting live in one place: the "Sentry telemetry (optional)"
section of README.md. It covers the DSN file, install and interpreter pitfalls,
the launchd cron bridge, and how to turn telemetry off.

Commands:

- `python3 scripts/captain_telemetry.py --self-test` — send one test event.
- `python3 scripts/openclaw_cron_sentry_bridge.py --dry-run` — show what the
  cron bridge would report, without sending any failure events or check-ins.
  The entrypoint's `captain_telemetry.guard(...)` wrap is still active during
  a dry run, so an unexpected crash can still send an exception event.

Rules:

- Scheduled and operational CLI entrypoints wrap `main()` in
  `captain_telemetry.guard("<name>")`. The telemetry self-test initializes
  telemetry directly, while current setup utilities report failures directly to
  their caller. New scheduled and operational scripts should use the guard.
- Never print or send secret values; the scrubber redacts known secrets from
  events, and `include_local_variables` is off.
