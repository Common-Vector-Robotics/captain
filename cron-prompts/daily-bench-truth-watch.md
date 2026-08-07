# Captain daily bench-truth and channel watch

Purpose: runs after the blocker chase to do two things. First, watch Slack for material
signals — supply-chain lead-time changes, QA/test results, design-review decisions.
Second, ask engineers directly how work actually went (a "bench-truth check"): when an
engineer's answer disagrees with what the ClickUp board or a standup summary says, the
engineer's answer wins and the discrepancy is logged, never averaged with the board or
deferred pending confirmation.

Schedule intent:
- Weekdays 15:45 America/Detroit (after the 15:15 blocker chase), isolated session.

Mode gate (do this first):
1. Read `data/captain-modes.json`. Let `audience = DailyLoop.audience` (missing → `off`).
2. If `off`: final response `NO_REPLY`, do nothing else.
3. If `shadow`: DRY RUN — no employee DMs send, no channel post goes out. Every bench
   question this prompt would send becomes a `SHADOW (would DM <owner>): ...` line —
   `<owner>` is always the actual resolved Slack user id being asked, never a role label,
   rendered as `Name (Uxxxxxxxx)` by resolving the name via `message(action=member-info)` —
   the same lookup step 4 already uses to resolve each candidate's owner, so this is an
   existing capability, not a new one. **Never fabricate a name:** if `member-info` fails,
   returns nothing, or returns only an email, render the bare id exactly as today — a
   plausible-but-wrong name attached to a message about someone's work is worse than an id,
   since it could make a reviewer approve a message aimed at the wrong person. Never render an
   email address into a Slack post; email is a lookup input only (e.g. ClickUp assignee email
   -> Slack user), never rendered output.
   The digest becomes a `SHADOW (would post to #captains-quarters): ...` message, both
   sent only to `shadow_recipient` from `data/captain-channels.json`. Delivery: every preview
   line above is sent as one or more Slack posts to `shadow_recipient` via
   `message(action=send, channel=slack, account=slack_account, target=shadow_recipient,
   message=...)`, passing the `slack_account` value from `data/captain-channels.json` and the
   `shadow_recipient` config value through verbatim — the delivery itself is a channel post, not a DM; only the
   resolved `<owner>` named inside a `SHADOW (would DM ...)` line describes what would have
   been a DM. If `shadow_recipient` itself cannot be resolved (e.g. Captain's bot is not a
   member of the channel), record `{shadow_recipient_unresolved: true}` in
   `data/daily-bench-truth-state.json` and send nothing further this run rather than failing
   silently. Local state (the blocker ledger
   and `data/daily-bench-truth-state.json`) is still written for real in `shadow`, because
   that makes the preview realistic and lets tomorrow's reconciliation step work. In
   `data/audit-log.jsonl`: no `clickup_*` audit rows are produced in `shadow` (no real
   ClickUp write happens, so there is nothing to audit under those event names) — but
   ledger events (`blocker_added`, `blocker_updated`) ARE written normally, since they are
   local state, not an external effect. This distinction matters because the Friday
   stale-task briefing counts `clickup_*` audit rows as evidence work moved; a shadow
   preview producing those rows would corrupt that report, while ledger rows are harmless
   because nothing downstream treats them as ClickUp evidence. (The only ClickUp write this
   cron ever makes is the step 3 safety task, and that write executes in `live` audience
   only — so in `shadow` no `clickup_*` audit row is ever produced by this cron regardless.)
4. If `live`: bench questions send for real to the owner's Slack DM, and the digest posts
   for real to `#captains-quarters`.
5. If `audience` is any value not listed above (not `off`/`shadow`/`live`): treat it as
   `off`. The fail-safe action is: record `{audience_unrecognized: <value>}` in
   `data/daily-bench-truth-state.json` so the misconfiguration is visible rather than
   silent, then give final response `NO_REPLY` and do nothing else.

Hard rules:
1. Read-only toward ClickUp in this cron: this cron makes no routine ClickUp writes — it
   fetches the board read-only via `scripts/fetch_clickup_tasks.py --out <path>` to compare
   against bench answers, and logs discrepancies to the local blocker ledger, never to
   ClickUp. The single exception is the safety gate in step 3 below: a genuine safety item
   files an urgent ClickUp task directly, live audience only for the write itself. No other
   writes, not even comments, in any audience.
2. Bench questions (live audience: sent for real to the owner's Slack DM; shadow audience:
   emitted as a `SHADOW (would DM <owner>): ...` line to `shadow_recipient` only) use the
   check-in lane rules from `MEMORY.md`: one short plain question, no internal jargon (never
   say "blocker" to an employee), no format demands — voice or text replies both fine. Max 3
   questions per day, max 1 per owner per day — count today's existing `asks` entries from
   `data/daily-bench-truth-state.json` toward this cap, not just this run's new sends, to
   prevent a crashed or retried run from exceeding 3. Before selecting candidates, read this
   cron's own `asks` from state and exclude any owner with an `asks` entry whose `date`
   equals today's date — this covers same-day re-runs and retries, not just repeats within
   a single run. Also skip any owner already pinged today by the 15:15 blocker chase (read
   `data/daily-blocker-chase-state.json` `pings_sent`, which maps owner id to the date they
   were last pinged — compare that date against today's date, not merely whether the key is
   present, since a key from an earlier day must not suppress today's question; if that state
   file is missing or unreadable, treat it as no prior pings today and record
   `chase_state_unreadable: true` in this cron's state so the gap is visible). Persist
   `data/daily-bench-truth-state.json` immediately after each send (real or SHADOW), not
   batched to the final step, so a crash or a retried run sees today's asks already recorded
   and will not re-ask the same people — matching `daily-blocker-chase.md`'s per-send
   persistence for `pings_sent`.
3. Never DM `excluded_user_ids` (live or shadow — this applies regardless of audience).
   Slack via the OpenClaw `message` tool only, never raw API or curl. Never print secrets;
   reference `.secrets/clickup.env` by path only. Final response exactly `NO_REPLY`.
4. **The bench wins.** When an owner's reply about a task's real-world state contradicts
   what the board says (or what memory's latest standup summary says), the discrepancy is
   logged to the blocker ledger with the bench's answer recorded as the truth — never
   averaged with the board, never deferred pending further confirmation.
5. Every Slack send passes `account=slack_account` (from `data/captain-channels.json`)
   alongside `channel=slack` and `target=...`. OpenClaw has more than one Slack account
   configured; omitting the account sends as the wrong app and fails with a misleading
   Slack `channel_not_found` error.

State file: `data/daily-bench-truth-state.json`
(keys: last_run_at, last_scan_ts per channel id, asks: [{date, owner_id, task_id, question,
message_ts, reply_status: pending|reconciled, note}], audience,
audience_unrecognized, chase_state_unreadable, incident_thread_ts, safety ledger id (from
step 3d's `blocker_ledger.py add`), `{channel_id: "unreadable"}` entries from step 3, the
"no watch channels configured" note from step 3, `channel_enumeration_unavailable`,
`critical_paths_file_missing`,
`{task_id: "owner_unresolvable"}` entries from step 4, `board_fetch_failed`,
`clickup_write_failed`, `shadow_recipient_unresolved`, `runs[]` — see step 6)

Workflow:
1. Work from `/Users/owen/.openclaw/workspace-captain`. Read `MEMORY.md` for the check-in
   wording rules and recipient boundaries.
2. Reconcile yesterday's asks first (this is local reasoning only — no external effect yet).
   First, for any ask still `pending` whose `message_ts` is the literal string
   `shadow-placeholder` (no DM was ever actually sent for it), mark it `reconciled` with the
   note `shadow ask, no reply expected` and skip it — never chase or wait on a reply for a
   question nobody was asked.
   For every remaining ask in state with `reply_status: pending`, read the DM thread via the
   `message` tool.
   - If the owner has not replied: leave `reply_status: pending` and note it in the digest's
     "still waiting on" list (live: posted to `#captains-quarters`; shadow: included in the
     `shadow_recipient` preview) — a pending ask is not silently dropped.
   - If the owner replied: fetch the board read-only via
     `scripts/fetch_clickup_tasks.py --out data/board-snapshots/$(date +%F)-bench-fetch.json`.
     If that exits non-zero (e.g. missing ClickUp credentials), record
     `{board_fetch_failed: true}` in state, note the gap in the digest rather than dropping
     it silently, and fall back to comparing the reply only against the latest standup
     summary in memory — do not treat the ask as unreconcilable just because the fetch
     failed. Otherwise compare the reply against that task's reported status and against the
     latest standup summary in memory.
     - Mismatch (bench wins): e.g. board says "in progress", bench says "rig's been down for
       two weeks" —
       `python3 scripts/blocker_ledger.py add --text "<bench answer, treated as truth>"
        --source "bench-truth" --source-ref "<date>" --owner "<owner>"
        --clickup-task-id "<task id>"`
       (this ledger write happens in both `live` and `shadow` — it is local state, not an
       external effect). Use the stable channel `bench-truth` for `--source` and put the
       date in `--source-ref` (excluded from the id hash) — a date-embedded `--source`
       would mint a new blocker id every day for the same real blocker and defeat
       repeat-escalation. Mark the ask `reconciled` and add the discrepancy to today's
       `memory/daily/` entry for the EOD wrap to persist.
     - Match (board and bench agree): mark the ask `reconciled`, no ledger entry needed.
     - Owner replied but the answer is unusable (off-topic, no signal on the task's actual
       state): mark the ask `reconciled` and note "unresolvable reply" in the digest — do
       not guess at a status.
3. Channel watch: per `watch` in `data/captain-channels.json`
   (`{"mode": "all_except", "exclude_names": [...], "exclude_ids": [...]}`), enumerate every
   channel the Slack workspace exposes to Captain via the OpenClaw `message` tool's channel
   listing — Captain's bot only sees channels it is a member of, so "all" means all channels
   visible to that membership, not literally every channel in the workspace — and exclude any
   matching `exclude_names`/`exclude_ids` (currently `#random`). If channel enumeration itself
   is unavailable this run, record the exact literal `channel_enumeration_unavailable: true`
   in state and fall back to scanning the channel ids listed in `watch.fallback_include_ids`
   in `data/captain-channels.json`, then continue; if that list is also empty, this sweep
   covers zero channels this run — record "no watch channels configured" once in state and
   continue (this cron has nothing further to do for channel watch this run; this gap is
   surfaced in the digest per step 5 below, not silently absorbed). For
   each remaining channel, read new messages since that channel's `last_scan_ts` (or last 24
   hours if unset) using the OpenClaw `message` tool channel-history reads — this is a
   read-only step, no send involved. If a channel is unreadable, record
   `{channel_id: "unreadable"}` in state and continue to the next channel. Channel categories
   are no longer per-channel config (channels aren't tagged by category any more — watching is
   all-channels-minus-exclusions); classify each material message by its own content into the
   category below using the existing keyword lists as candidate filters, not by which channel
   it came from:
   - `supply_chain`: lead-time changes, backorders, missed shipments, PO updates.
   - `qa_test`: test passes/failures, rig availability/downtime.
   - `design_review`: decisions made or still needed.
   - `alerts`: check every message against `safety_keywords` and `urgent_keywords` (read the
     actual message in context, don't keyword-fire blindly). For any genuine safety item,
     follow the morning prompt's safety-gate pattern in full, live audience only for the
     external effects (shadow audience previews each of these using this prompt's
     established forms — `SHADOW (would DM <resolved-id>): ...` for the page/fallback DMs
     in (a), rendering `<resolved-id>` as `Name (Uxxxxxxxx)` per this prompt's Mode gate
     name-rendering rule above (bare id if unresolvable — never a guess), `SHADOW (would post
     to #captains-quarters): ...` (or `SHADOW (would DM Gavin (U0B4G00QXT8)):
     ...` if the channel is unresolvable) for the incident thread in (b), `SHADOW (would
     write): <would-be task> create-task in list <list id>` for the ClickUp write in (c) —
     to `shadow_recipient` instead of sending/writing for real; the ledger entry in
     step (d) is local state and happens for real in both audiences):
     a. Page the engineering leads now, priority order: if `eng_leads` in
        `data/captain-channels.json` is non-empty, one Slack DM each to those ids; otherwise
        resolve the ClickUp assignees of the affected task/project to Slack users (assignee
        email -> `message(action=member-info)` lookup) and DM each resolved user; if no
        assignee can be resolved for anyone found (or no task/project is identifiable), that
        IS the fallback trigger — DM `admin_recipients` instead, never a silent skip. Never
        DM `excluded_user_ids`. Message: `Captain safety page:` + one-line summary + permalink
        + who appears involved.
     b. Open the incident thread: resolve the destination channel id from `program_channel`
        in `data/captain-channels.json`; if the id is empty, resolve `#captains-quarters` by
        name through the `message` tool; if that fails too, DM Gavin (`user:U0B4G00QXT8`)
        the incident summary plus the blocker `program channel unresolved` instead — the
        incident must never go unposted. As with the rest of this step, this posts for real
        in `live` audience and is previewed as a `SHADOW (would post to #captains-quarters):
        ...` line (or `SHADOW (would DM Gavin (U0B4G00QXT8)): ...` on the unresolved-channel fallback) to
        `shadow_recipient` in `shadow` audience. Post the same summary there and use that
        message's thread as the incident thread; record its ts in state (`incident_thread_ts`)
        — in `live` audience use the real thread ts, in `shadow` audience record the literal
        string `shadow-placeholder`.
     c. File the urgent task directly (live audience only — shadow audience emits
        `SHADOW (would write): <would-be task> create-task in list <list id>` to
        `shadow_recipient` instead of executing):
        `scripts/clickup_write.py --execute create-task --list-id <most relevant list>
        --name "INCIDENT: <summary>" --priority 1` with a description containing the Slack
        permalink. If step (a) resolved a specific owner (an eng lead or the affected
        task/project's assignee), add them too: a numeric `--assignee <id>` when they are a
        known ClickUp member, otherwise `--owner "<name>"` so ownership lands on the Owners
        custom-labels field rather than only in the description, per MEMORY.md's standing
        rule. No due date known → the `due_date_followup_required` rule applies (ask
        the owner). This is the sole exception to Hard rule 1's read-only stance — the
        safety gate exists to eliminate latency, and deferring to the next write-capable
        cron would leave a genuine incident unticketed for up to ~16 hours. Audited
        automatically. This write only executes in `live` audience, so this failure path
        applies there only (a `shadow` preview cannot fail): if the write exits non-zero or
        `failed` is non-empty in its printed JSON, do not treat the incident as ticketed —
        record `{clickup_write_failed: "<summary>"}` in state and, in `live` audience,
        immediately escalate by DMing Gavin (`user:U0B4G00QXT8`) that the incident ClickUp
        task failed to file and needs manual creation. A failed safety-task write must
        escalate, never vanish silently. If the write instead succeeds but the result
        carries `needs_owner_label` (Owners field exists on that list but not yet this
        owner's label — the public API cannot add one), the task is still ticketed; note
        the gap in the incident thread so a human can add the label option.
     d. `python3 scripts/blocker_ledger.py add --text "<summary>" --source
        "slack:<channel_id>" --source-ref "<message_ts>" --clickup-task-id <the id from
        step c's create-task result, live audience only — omit it in shadow, since no task
        was actually created>` (local state, both audiences) and record the id in state.
     Never let a safety signal in an `alerts` channel go unaddressed or silent, in either
     audience.
   Extract material signals only — routine chatter is not logged or reported.
4. Bench-truth selection: from the read-only board fetch in step 2 (or a fresh
   `scripts/fetch_clickup_tasks.py --out <path>` call if no board snapshot was produced
   earlier in this run, for any reason — e.g. step 2 was skipped because there were no
   pending asks, or there were pending asks but nobody replied so the fetch never ran), pick
   up to 3 tasks in priority order. If `fetch_clickup_tasks.py` exits non-zero (e.g. missing
   ClickUp credentials), record `{board_fetch_failed: true}` in state, note the gap in the
   digest's channel/board-gaps line rather than dropping it silently, and fall back to
   selecting candidates from criteria (b) and (c) below using only memory and prior board
   snapshots — do not stall bench-truth selection on a single failed fetch:
   a. On a critical path — if `data/critical-paths.json` exists, load it and treat
      `paths[].task_ids` as the current critical-path set; if the file does not exist,
      record `critical_paths_file_missing: true` in state and fall through to the next
      criterion instead of stalling this step.
   b. In a testing-like status (e.g. "In Test", "QA", "Testing").
   c. Discussed in the last standup summary in memory.
   Each candidate must have a resolvable owner (memory, `message(action=member-info)`, or
   ClickUp assignee/Owners field) — if an owner cannot be resolved for a candidate, skip that
   candidate and record `{task_id: "owner_unresolvable"}` in state, then move to the next
   candidate rather than sending an unaddressed question. Compose one plain question per
   selected task, e.g. `Quick check on <task name> — how did today's run actually go?
   Anything in the way?`. Enforce the caps from Hard rule 2 (max 3 total, max 1 per owner,
   skip anyone already pinged today by the blocker chase). Send each question to the owner's
   Slack DM in `live` audience; in `shadow` audience emit it as a
   `SHADOW (would DM <owner>): ...` line to `shadow_recipient` instead of sending — either
   way, append `{date, owner_id, task_id, question, message_ts, reply_status: pending}` to
   `asks` in state immediately after (real message_ts in live; the literal string
   `shadow-placeholder` in shadow).
5. Digest (live audience: one message posted to `#captains-quarters`; shadow audience: the
   same content as one `SHADOW (would post to #captains-quarters): ...` message sent only to
   `shadow_recipient`): resolve the destination channel id from `program_channel` in
   `data/captain-channels.json`; if the id is empty, resolve `#captains-quarters` by name
   through the `message` tool; if that fails too, in `live` audience DM Gavin
   (`user:U0B4G00QXT8`) the digest plus the blocker `program channel unresolved` instead of
   letting it go unposted, and in `shadow` audience add that same fallback DM to the
   `shadow_recipient` preview as a
   `SHADOW (would DM Gavin (U0B4G00QXT8)): ...` line. ≤ 12
   lines: `Captain bench & channels — <date>`; material channel
   signals grouped by category with permalinks (or "none" per category); yesterday's bench
   answers and any board mismatches (bench-wins items) with their ledger ids; still-pending
   asks from step 2; today's questions sent (or their SHADOW lines); any
   `owner_unresolvable` or `unreadable` channel gaps from this run; a gaps line when
   `channel_enumeration_unavailable` was recorded this run — e.g. `channel watch covered 0
   channels — enumeration unavailable, no fallback channels configured`. If there is genuinely
   nothing material to report and no asks were sent or are pending, skip the post entirely
   (both audiences).
6. Update `data/daily-bench-truth-state.json` atomically (both audiences write state; the
   live/shadow difference is external effects only, per the Mode gate). Also append one
   compact record to a `runs[]` array in this state file — same shape as
   `standup-transcript-clickup-reconciliation-state.json` already produces — so a run that
   found nothing material (step 5's skip-the-post case) is durable evidence of what happened,
   not silence indistinguishable from breakage: `{run_at: <UTC ISO timestamp>, audience,
   action: <short verb summarizing this run's dominant outcome, e.g. "posted_digest" when
   step 5's digest went out, or "no_op" when it was skipped entirely>, reason: <one line, only
   when action is "no_op" or a gap like channel_enumeration_unavailable/board_fetch_failed was
   recorded this run>, counts: {signals, asks_sent, asks_pending, mismatches}}`. After
   appending, keep only the most recent 30 entries (drop the oldest first) so this file cannot
   grow without bound. Reply `NO_REPLY`.
