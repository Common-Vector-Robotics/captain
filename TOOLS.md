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

Captain has ClickUp API access via `.secrets/clickup.env` symlinked to Owen’s local ClickUp credential file. Do not print this file or expose token values.

The ClickUp scripts prefer exported environment variables and otherwise load these keys from `.secrets/clickup.env` automatically. Run the documented commands directly; manual `source`/`set -a` bootstrapping is not required.

Optional pilot filters:

- `CAPTAIN_CLICKUP_LIST_IDS` comma-separated list IDs
- `CAPTAIN_CLICKUP_SPACE_IDS` comma-separated space IDs

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
- `scripts/ingest_standup_notes.py <source-file>`
- `scripts/fetch_clickup_tasks.py --out fixtures/clickup_tasks.json`
- `scripts/reconcile_clickup.py --clickup fixtures/clickup_tasks.json`
- `scripts/clickup_write.py --execute create-task --list-id <list_id> --name <name>`
- `scripts/clickup_write.py --execute update-task --task-id <task_id> --status <status>`
- `scripts/clickup_write.py --execute comment-task --task-id <task_id> --text <comment_text>`
- `scripts/clickup_write.py --execute batch --operations-file <batch.json>`
- `scripts/audit_report.py`
- `scripts/blocker_ledger.py add|update|list` — same-cycle blocker ledger (daily loop)
- `scripts/daily_cycle.py set-top3|set-tomorrow|stamp|get` — per-date top-3 and phase stamps
- `scripts/daily_context.py --clickup <export>` — morning board buckets + snapshot
- `scripts/personal_top2.py rank|set|get` — per-person top-2 ranking for the morning
  cycle's step 6b personal texts. `rank --clickup <export> [--date] [--critical-paths]`
  prints each person's ranked candidates with a plain-language reason and writes
  nothing; `set --date <d> --items <json>` persists the top-2 actually sent to the
  `daily_cycle.personal_top2` column; `get --date <d>` reads it back. Read-only toward
  ClickUp — it never writes to the board. See `docs/daily-loop.md`'s "Personal top-2
  texts" for the tier ladder and the recipient-resolution rules.
- `scripts/daily_wrap.py --morning <snap> --eod <export>` — EOD deltas + milestone risk
- `scripts/captain_modes.py dailyloop --audience off|shadow|live --user-id <id>`
- `scripts/captain_activity.py [HOURS]` — read-only chronological viewer merging cron
  runs, per-cron `runs[]` decision history (this is where no-ops surface), last-run/flag
  state, and the audit log into one time-sorted feed; default 24 hours. See
  `docs/daily-loop.md`'s "Seeing what Captain did" for details.
- `scripts/daily_activity_digest.py [--hours N] [--json] [--post]` — Action summary
  reporting posted to #dry-dock, mechanically generated (never LLM-written) from
  `captain_activity.py`'s own
  collectors. Runs in EVERY `DailyLoop` audience, including `off` — the one deliberate
  exception to the mode gate, safe because it is strictly read-only and its only side effect
  is one Slack post. `--post` is not the default (print-only, matching `--execute`
  elsewhere). Any Slack user id in the rendered text is shown as `Name (Uxxxxxxxx)` via
  `scripts/slack_user_names.py`'s resolver (never for `--json`, which stays raw). See
  `docs/daily-loop.md`'s "Action summary reporting" and "Slack name rendering" subsections.
- `scripts/slack_user_names.py` — shared library (no CLI of its own) behind the id -> name
  rendering above: `SlackNameResolver` resolves a Slack user id to `Name (Uxxxxxxxx)` via
  `admin_recipients` in `data/captain-channels.json`, then `data/slack-user-cache.json`, then
  falls back to the bare id — never fabricating a name. See `docs/daily-loop.md`'s "Slack name
  rendering" section.
- `scripts/refresh_slack_user_cache.py [--user-id ID ...] [--execute]` — operator-run (not on
  a cron) script that populates `data/slack-user-cache.json` by asking OpenClaw for member
  info, as the `captain` Slack account. Writes only `{id: name}` pairs, never emails. Dry run
  by default (matching `--execute`/`--post` elsewhere); fails soft, leaving any existing cache
  untouched, if OpenClaw is unavailable or nothing resolves. See `docs/daily-loop.md`'s "Slack
  name rendering" section for the staleness/degradation story.

## ClickUp batch writes

Use one batch command rather than a shell loop. The JSON input is an array (or an object with an `operations` array) of `create-task`, `update-task`, or `comment-task` objects. Every object needs an `operation_id` so the result can identify the safe retry subset.

```json
{
  "operations": [
    {
      "operation_id": "task-1",
      "command": "create-task",
      "list_id": "901327546010",
      "name": "Investigate controller fault",
      "status": "to do",
      "owner": ["Gavin"],
      "source": "explicit Slack request"
    }
  ]
}
```

Ownership goes through the `owner` key (below), never into `description` prose — that is exactly
the anti-pattern the Owners custom-field fallback exists to remove. `owner` is a JSON array of
name strings and is accepted on `create-task` and `update-task` operations the same way `--owner`
is accepted on the CLI (repeatable becomes a list); it is rejected with a clear error on
`comment-task`, which has no `list_id` to resolve it against.

The writer first validates every requested status against the destination list. `intake` maps to `to do`. A requested `blocked` status that the destination list does not support is left unchanged (`needs_blocked_status` is set on the audit record and the operation result, naming the list so it can be flagged for a human to add the status) — the task's status is never silently redirected to `to do`. Other unsupported statuses are returned in `failed` with the allowed values and are never sent to ClickUp.

## Owners custom-field fallback

Per MEMORY.md's standing rule, ownership should never live only in a task's description or free
text. `create-task` and `update-task` both accept `--owner "<name>"` (repeatable) as a fallback
for when the person cannot be a built-in ClickUp assignee: `--assignee` (numeric ClickUp user ID)
is always preferred, and if both are given, `--assignee` wins and no Owners label is set.

`--owner` resolves against the list's `Owners` custom **labels** field, ported from
`scripts/weekly_slack_clickup_status.py` (see `docs/daily-loop.md`'s Deprecations table — that
script is removed at the Task 12 Phase B cutover, so this capability now lives here). Three cases:

1. **Owners field missing on the list** → create it with this owner's label as an initial
   option, then set it. Audited as `clickup_custom_field_create_attempt`.
2. **Field exists and the owner's label exists** → set it. No create attempted.
3. **Field exists but the owner's label does not** → the public ClickUp API can create a labels
   field with initial options and can set an existing option's value, but it cannot append a
   new option to an *existing* labels field (an open ClickUp feature request, not a gap in this
   tooling). No ownership write happens and nothing is written into the description — instead
   the operation result and audit record carry a `needs_owner_label` marker (`{list_id, owner,
   owners}`), the same shape as `needs_blocked_status` above, so a cron digest can tell a human
   to add the label option in ClickUp settings. The task/update itself still succeeds — a task
   created without its owner label is better than no task at all.

**Write shape differs by command.** `create-task`'s `POST /list/{id}/task` accepts inline
`custom_fields` in the create body — a documented Create Task parameter — so create-task sets
Owners that way: `custom_fields: [{"id": field_id, "value": [option_id, ...]}]`. `update-task`'s
`PUT /task/{id}` does **not** support `custom_fields` as an update parameter (ClickUp does not
document it there), and the actual endpoint for setting a custom field on an existing task —
`POST /task/{id}/field/{field_id}` with `{"value": [...]}` — **overwrites** whatever option ids
are already on the field. So update-task always reads the task's current Owners value first (from
the same task GET already done for status resolution) and writes the **union** of the existing
option ids and the newly resolved ones, as a second request made after the primary task write
succeeds. Setting Priya's label never removes Arnold's. If the union doesn't add anything new, no
follow-up request is made at all. If the primary task write succeeds but this follow-up field-value
call fails, the operation result still reports the task as succeeded (it exists) but carries an
`owner_field_write: {attempted: true, ok: false, error: {...}}`, and a matching
`"<operation_id>:owner-field"` entry appears in `failed` too — check `owner_field_write` on any
`update-task` result that used `owner`, not just top-level `ok`.

A list whose *pre-existing* `Owners` field is some other type (not `labels`) fails that operation
cleanly (no mutation attempted) rather than risk corrupting the field — this check only applies
when the field already existed before this call; an Owners field this call just created is never
treated as wrong-typed (we requested type `labels` ourselves) or as missing one of the labels it
was just given.

A non-numeric `--assignee` value is still rejected (assignees must be numeric ClickUp user IDs),
and the error message now points at `--owner` as the fallback.

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

Dependency: `sentry-sdk` (see `requirements.txt`):

```bash
python3 -m pip install --user -r requirements.txt
```

On Homebrew-managed Python this fails with `error: externally-managed-environment`
(PEP 668). Fix:

```bash
python3 -m pip install --user --break-system-packages -r requirements.txt
```

`--break-system-packages` installs into a Homebrew-managed Python's user site
directory, which is why pip guards it by default. If you'd rather not override
the guard, use a venv instead:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

A venv changes which interpreter must run the scripts — see the launchd
interpreter note in README.md before choosing this route for the cron bridge.

Commands:

- `python3 scripts/captain_telemetry.py --self-test` — send one test event.
- `python3 scripts/openclaw_cron_sentry_bridge.py --dry-run` — show what the
  cron bridge would report, without sending any failure events or check-ins.
  The entrypoint's `captain_telemetry.guard(...)` wrap is still active during
  a dry run, so an unexpected crash can still send an exception event.

Rules:

- Every CLI entrypoint wraps `main()` in `captain_telemetry.guard("<name>")`
  (enforced by `tests/test_telemetry_wrap_lint.py`). New scripts must do the
  same.
- Never print or send secret values; the scrubber redacts known secrets from
  events, and `include_local_variables` is off.
