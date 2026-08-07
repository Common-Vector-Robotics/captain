# Captain daily EOD wrap

Purpose: the last run of the day. Pull final status from all of Captain's sources —
program memory, Slack, the ClickUp board, and today's standup notes — check whether any
milestone looks at risk, draft replan options for humans to decide on (Captain never
replans the board itself), refresh the risk register, then post the EOD summary, sync
final task states to ClickUp, persist today's learnings to program memory, and set
tomorrow's top 3.

Schedule intent:
- Weekdays 17:45 America/Detroit, isolated session.

Mode gate (do this first):
1. Read `data/captain-modes.json`. Let `audience = DailyLoop.audience` (missing → `off`).
2. If `off`: final response `NO_REPLY`, do nothing else.
3. If `shadow`: DRY RUN — the wrap is never posted to `#captains-quarters`; it is sent only
   to `shadow_recipient` from `data/captain-channels.json` with a `SHADOW` prefix, and every
   ClickUp write this cron would make is described as a `SHADOW (would write): <task id>
   <operation>` line (e.g. `status → Blocked`, `comment added`, or `create-task in list
   <list id>`) instead of executed, so the preview alone says which task and what would
   change. Delivery: the wrap and every ClickUp-write preview line above is sent as one or
   more Slack posts to `shadow_recipient` via `message(action=send, channel=slack,
   account=slack_account, target=shadow_recipient, message=...)`, passing the `slack_account`
   value from `data/captain-channels.json` and the `shadow_recipient` config value through
   verbatim — the delivery itself is a channel post, not a DM; only a resolved recipient named inside a
   `SHADOW (would DM ...)` line describes what would have been a DM — rendered as
   `Name (Uxxxxxxxx)` by resolving the name via `message(action=member-info)` (an existing
   capability, already used elsewhere in this daily loop for owner resolution). **Never
   fabricate a name:** if `member-info` fails, returns nothing, or returns only an email,
   render the bare id exactly as today — a plausible-but-wrong name attached to a message
   about someone's work is worse than an id, since it could make a reviewer approve a message
   aimed at the wrong person. Never render an email address into a Slack post; email is a
   lookup input only, never rendered output. If `shadow_recipient`
   itself cannot be resolved (e.g. Captain's bot is not a member of the channel), record
   `{shadow_recipient_unresolved: true}` in `data/daily-eod-wrap-state.json` and send nothing
   further this run rather than failing silently. Local state (the blocker ledger,
   `data/daily-eod-wrap-state.json`,
   `data/captain.sqlite` via `daily_cycle.py`, `data/critical-paths.json` via step 2b's
   `critical_paths.py write-state`, and `memory/daily/`) is still written for real in
   `shadow`, because that makes the preview realistic and lets tomorrow's morning cycle start
   informed. In `data/audit-log.jsonl`: no `clickup_*` audit rows are produced in `shadow`
   (no real ClickUp write happens, so there is nothing to audit under those event names) —
   but ledger and cycle events (`blocker_added`, `blocker_updated`, `daily_cycle_top3_set`,
   `daily_cycle_tomorrow_top3_set`, `daily_cycle_stamp`) ARE written normally, since they are
   local state, not an external effect. This distinction matters because the Friday
   stale-task briefing counts `clickup_*` audit rows as evidence work moved; a shadow preview
   producing those rows would corrupt that report, while ledger/cycle rows are harmless
   because nothing downstream treats them as ClickUp evidence.
4. If `live`: the wrap posts for real to `#captains-quarters` and ClickUp writes execute for
   real.
5. If `audience` is any value not listed above (not `off`/`shadow`/`live`): treat it as `off`.
   The fail-safe action is: record `{audience_unrecognized: <value>}` in
   `data/daily-eod-wrap-state.json` so the misconfiguration is visible rather than silent,
   then give final response `NO_REPLY` and do nothing else.

Hard rules:
1. ClickUp writes in this cron are autonomous and audited: in `live` audience, apply
   evidence-backed status updates from today's check-in/bench replies via
   `scripts/clickup_write.py --execute update-task --task-id <id> --status <status>`, create
   tasks from confirmed action items via `scripts/clickup_write.py --execute create-task
   --list-id <id> --name "<name>"` (the `due_date_followup_required` rule applies to any
   task created without a due date), and set Blocked status + comment per the Blocked-task
   rule via `--execute update-task --task-id <id> --status Blocked` followed by `--execute
   comment-task --task-id <id> --text "Blocked: <what/since when/what's needed>"`. Every
   task created here has an owner: prefer a numeric `--assignee <id>` when the owner is a
   known ClickUp member, otherwise pass `--owner "<name>"` so ownership lands on the Owners
   custom-labels field rather than only in the task name/description, per MEMORY.md's
   standing rule. In `shadow` audience none of these execute — each becomes a `SHADOW (would
   write): <task id> <operation>` line to `shadow_recipient` instead. If the result carries
   `needs_blocked_status`, the status
   was left unchanged (this list has no `Blocked` status yet) — this is the sanctioned V1
   posture, not an error: keep the explanatory comment, and flag the list id (the tooling
   exposes `list_id` only; show the id when no human-readable name is at hand) in the wrap's
   `needs Blocked status added` line (live: in the `#captains-quarters` post; shadow: in the
   `shadow_recipient` preview) the same way `daily-blocker-chase.md` does, and record it in
   state under `needs_blocked_status`. If a create-task result instead carries
   `needs_owner_label` (the Owners field exists on that list but this owner has no label
   option there yet, and the public ClickUp API cannot add one), the task itself is still
   created — do not treat this as a failure — but flag it the same way as
   `needs Blocked status added`: a `needs Owners label added` line in the wrap naming the
   list id and owner, and record it in state under `needs_owner_label`.
   Hold only ambiguous owner/task matches as digest questions (live: in the
   `#captains-quarters` post; shadow: in the `shadow_recipient` preview) rather than
   guessing. Replans NEVER mutate the board in either audience — Captain drafts options,
   humans decide. Every `clickup_write.py` invocation prints `{ok, succeeded, failed}` and
   exits 0 even when `failed` is non-empty — always inspect `failed`, never rely on exit
   code; treat any operation in `failed` as not done, list it in the wrap's `Failed:` section
   (live: posted to `#captains-quarters`; shadow: in the `shadow_recipient` preview) with the
   task id and error, and do not advance that item's ledger status past what actually
   happened.
2. `message` tool only; no employee DMs from this cron, with exactly one named exception:
   the due-date-followup ask in step 4 below, sent live audience only (shadow previews it
   instead). That single exception must still respect the one-ping-per-owner-per-day
   discipline shared with the 15:15 blocker chase and the bench-truth/channel watch cron —
   check and record `pings_sent` in `data/daily-blocker-chase-state.json` the same way
   `daily-bench-truth-watch.md` Hard rule 2 does: read `pings_sent` from that state file
   (maps owner id to the date they were last pinged) and compare that date against today's
   date, not merely whether the key is present; if that state file is missing or
   unreadable, treat it as no prior pings today and record `chase_state_unreadable: true`
   in this cron's state so the gap is visible. If the owner already has a ping recorded for
   today from any of these crons, do NOT send the due-date ask — defer it to tomorrow and
   note the deferral in the wrap instead. If the ask proceeds, immediately after sending (or
   after the `SHADOW (would DM <owner>): ...` line in shadow) record
   `pings_sent[owner_slack_id] = <date>` in `data/daily-blocker-chase-state.json` and
   persist it, matching `daily-blocker-chase.md`'s per-send persistence. Never DM
   `excluded_user_ids` (`U0AJEB4K7FT`), in either audience.
3. Never print secrets; reference `.secrets/clickup.env` by path only. Final response
   exactly `NO_REPLY`.
4. Every Slack send passes `account=slack_account` (from `data/captain-channels.json`)
   alongside `channel=slack` and `target=...`. OpenClaw has more than one Slack account
   configured; omitting the account sends as the wrong app and fails with a misleading
   Slack `channel_not_found` error.

State file: `data/daily-eod-wrap-state.json`
(keys: last_run_at, audience, audience_unrecognized, morning_snapshot_missing,
board_fetch_failed, critical_paths_file_missing, critical_paths_refresh_failed,
program_channel_unresolved, clickup_write_failed: [{task_id or name, error}],
needs_blocked_status: [list ids] (the tooling exposes `list_id` only, not a list name; the
digest may show the id when no human-readable name is at hand), needs_owner_label: [{list_id,
owner}] (from a create-task result's `needs_owner_label` marker — the Owners field exists on
that list but not yet this owner's label), last_wrap_ts, replan_triggered: bool,
shadow_recipient_unresolved, runs[] — see step 9)

Workflow:
1. Work from `/Users/owen/.openclaw/workspace-captain`. Read `MEMORY.md`.
2. Load `.secrets/clickup.env`; fetch EOD board read-only: `scripts/fetch_clickup_tasks.py
   --include-closed --out data/board-snapshots/$(date +%F)-eod.json` (`--include-closed` is
   required here — without it, tasks closed today are absent from the export entirely, not
   present with a done status, so `daily_wrap.py`'s closed-today delta would silently compute
   as empty every run). If that exits non-zero (e.g. missing ClickUp credentials), record
   `{board_fetch_failed: true}` in state, say so in the wrap's gaps line (live: in the
   `#captains-quarters` post; shadow: in the `shadow_recipient` preview) — `board fetch
   failed — deltas and milestone risk unavailable this run` — and skip only the steps that
   need board data: skip step 4 (board sync) entirely, and in step 3 build the wrap with an
   empty placeholder tasks file (`{"tasks": []}`) for both `--morning` and `--eod` instead of
   the missing EOD export, so `wrap["blockers"]` and `wrap["mutations"]` (which read the
   ledger/audit log, not the board) are still produced; `wrap["board"]` and
   `wrap["milestone"]["at_risk_paths"]` will come back empty as an artifact of the placeholder,
   not as a real "0 closed / no risk" reading, so skip the milestone diamond in step 5 too and
   do not report either field as real. Still run step 6 (wrap post) with the board line
   omitted and the gaps line included, then step 7 (persist) and step 8 (tomorrow), using only
   memory and the ledger for content that doesn't depend on the board.
2b. Critical-path refresh (read-only toward ClickUp, writes only local
   `data/critical-paths.json`, so it runs in both `live` and `shadow` — this is local state,
   not an external effect): run `python3 scripts/critical_paths.py write-state`. If it exits
   non-zero (e.g. missing ClickUp credentials), keep the existing `data/critical-paths.json`
   file unchanged, record `{critical_paths_refresh_failed: true}` in state, and note in the
   wrap (live: in the `#captains-quarters` post; shadow: in the `shadow_recipient` preview)
   that milestone risk used a stale critical-paths snapshot this run.
3. Build the wrap: `python3 scripts/daily_wrap.py
   --morning data/board-snapshots/$(date +%F)-morning.json
   --eod data/board-snapshots/$(date +%F)-eod.json` and keep the JSON. If the morning
   snapshot file is missing (morning cron skipped or failed), record
   `{morning_snapshot_missing: true}` in state, then rerun `daily_wrap.py` with the EOD file
   passed as both `--morning` and `--eod` so at least milestone risk and audit/blocker
   rollups are produced, and say
   `board deltas unavailable — morning snapshot missing (closed/new-overdue lines omitted)`
   in the wrap (live: in the `#captains-quarters` post; shadow: in the `shadow_recipient`
   preview). If `data/critical-paths.json` is also missing, `daily_wrap.py` already returns
   an empty `at_risk_paths` list for that reason (the library layer handles the missing
   file); additionally record `{critical_paths_file_missing: true}` in state and note
   `milestone risk unavailable — no critical-paths file` in the wrap (live: in the
   `#captains-quarters` post; shadow: in the `shadow_recipient` preview) so the gap is visible
   rather than silently read as "no risk." If `daily_wrap.py` itself exits non-zero (e.g. a
   malformed snapshot JSON that fails to parse), record `{board_fetch_failed: true}` in state
   — same field as a fetch failure, since the effect on the wrap is identical — say so in the
   gaps line (live: in the `#captains-quarters` post; shadow: in the `shadow_recipient`
   preview) as `wrap build failed — deltas and milestone risk unavailable this run`, and follow
   the same fallback as a board-fetch failure: in step 3, retry with an empty placeholder tasks
   file (`{"tasks": []}`) for both `--morning` and `--eod` so `wrap["blockers"]` and
   `wrap["mutations"]` are still produced; then skip step 4 and the milestone diamond in step
   5, still post step 6 with the board line omitted, then steps 7-8.
4. Board sync: apply pending evidence-backed status
   updates gathered today (bench replies, check-in replies visible in today's threads) and
   create tasks from confirmed new action items, all through audited tooling in `live`
   audience for real, and as `SHADOW (would write): <task id> <operation>` lines to
   `shadow_recipient` in `shadow` audience. Tasks created without a due date trigger the
   `due_date_followup_required` rule (ask the owner via the approved messaging lane — this
   is the single named exception to Hard rule 2, subject to that rule's `pings_sent`
   discipline — live audience only; shadow audience previews the ask instead of sending it).
   Hold only genuinely ambiguous items as digest questions rather than guessing.
5. Milestone diamond: if `wrap["milestone"]["at_risk_paths"]` is non-empty, set
   `replan_triggered: true` in state and add a `Replan options` section to the wrap (live:
   in the `#captains-quarters` post; shadow: in the `shadow_recipient` preview) — for each
   at-risk path give 2 concrete options with trade-offs (e.g. resequence which tasks,
   drop/defer what, who decides), citing the path's `reasons` from the JSON. End the section
   with `Decision needed from Admins — Captain will not touch the board.` This section is
   drafted the same way in both audiences; only its destination (real post vs. shadow
   preview) differs. If `at_risk_paths` is empty, set `replan_triggered: false` and omit the
   section entirely.
6. Wrap post (live audience: one message posted to `#captains-quarters`; shadow audience:
   the same content as one `SHADOW (would post to #captains-quarters): ...` message sent
   only to `shadow_recipient`), ≤ 14 lines: resolve the destination channel id from
   `program_channel` in `data/captain-channels.json`; if the id is empty, resolve
   `#captains-quarters` by name through the `message` tool; if that fails too, record
   `{program_channel_unresolved: true}` in state and, in `live` audience, DM Gavin
   (`user:U0B4G00QXT8`) the full wrap content plus the literal blocker text
   `program channel unresolved` instead of letting it go unposted (in `shadow` audience this
   fallback is itself previewed as a `SHADOW (would DM Gavin (U0B4G00QXT8)): ...` line to
   `shadow_recipient`, since no real post or DM happens in shadow regardless). Content:
   `Captain EOD wrap — <date>`; today's top-3 with done/not-done (from
   `scripts/daily_cycle.py get --date $(date +%F)`); board line (closed today, new overdue,
   open count) — omitted if `morning_snapshot_missing` or `board_fetch_failed`; gaps line
   (failure notes when board-fetch or wrap-build failed, plus step 2b's stale-critical-paths
   note when `critical_paths_refresh_failed` was recorded this run); blockers line
   (opened/cleared/escalated/still open, from `wrap["blockers"]` — this reads the blocker
   ledger as of right now, so any blocker the 15:15 chase already marked `cleared` today
   (including one cleared because a human resolved it directly in ClickUp) is already
   reflected correctly here and never double-counted as still open; a blocker a human
   resolves in ClickUp *after* today's 15:15 chase has already run is not re-checked by
   this wrap — that resolution is picked up at tomorrow's chase, per that cron's documented
   latency, not retroactively by this one); mutation counts from `wrap["mutations"]`;
   material bench/channel findings from today's memory notes; the `Replan options` section
   from step 5 when triggered; any `Failed:` section per Hard rule 1; any
   `needs Blocked status added` line listing list ids flagged per Hard rule 1; any
   `needs Owners label added` line listing list id + owner pairs flagged per Hard rule 1;
   `Tomorrow's top 3:` list.
7. Persist (risk register + memory write-back; Cognee indexes these overnight;
   written in both `live` and `shadow` audiences, since this is local state, not an external
   effect): append `memory/daily/$(date +%F).md` with sections `## Decisions`, `## Risks`
   (open/aged/new — from the ledger and `wrap["milestone"]["at_risk_paths"]`), `## Learnings`,
   `## Bench-truth discrepancies`. If any of `morning_snapshot_missing`, `board_fetch_failed`,
   `critical_paths_file_missing`, or `critical_paths_refresh_failed` was recorded this run, add a one-line note under
   `## Risks` naming the gap so tomorrow's morning cycle starts informed rather than assuming
   a clean day. If a durable operating rule changed today, add a one-line entry to
   `MEMORY.md` following its existing style.
8. Tomorrow: `python3 scripts/daily_cycle.py set-tomorrow --date $(date +%F) --items <json>`
   with the top-3 for tomorrow (both audiences — local state); then `python3
   scripts/daily_cycle.py stamp --date $(date +%F) --phase eod` (both audiences — local
   state).
9. Update `data/daily-eod-wrap-state.json` atomically (both audiences write state; the
   live/shadow difference is external effects only, per the Mode gate). Also append one
   compact record to a `runs[]` array in this state file — same shape as
   `standup-transcript-clickup-reconciliation-state.json` already produces — so a run is
   durable evidence of what happened, not silence indistinguishable from breakage: `{run_at:
   <UTC ISO timestamp>, audience, action: <short verb summarizing this run, e.g. "wrapped"
   normally>, reason: <one line, only when a gap like board_fetch_failed or
   morning_snapshot_missing was recorded this run>, counts: {closed_today, new_overdue, open,
   blockers_cleared}}`. After appending, keep only the most recent 30 entries (drop the oldest
   first) so this file cannot grow without bound. Reply `NO_REPLY`.
