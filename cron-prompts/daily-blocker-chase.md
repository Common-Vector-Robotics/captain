# Captain daily blocker chase

Purpose: runs after the 14:00 standup reconciliation to work every open blocker for the
day. For each one, first check whether it has actually cleared; otherwise either chase it
by Slack message when Captain can move it forward on its own, or hand it to a human when
it can't. This cron also owns giving every blocker a ClickUp home (see the ClickUp-home
step below), so humans can see it on the board, and a human resolving it directly in
ClickUp is itself a clearing signal Captain checks for. Same-cycle rule: no open blocker
ends the day unowned.

Schedule intent:
- Weekdays 15:15 America/Detroit (after the 14:00 standup reconciliation), isolated session.

Mode gate (do this first):
1. Read `data/captain-modes.json`. Let `audience = DailyLoop.audience` (missing → `off`).
2. If `off`: final response `NO_REPLY`, do nothing else.
3. If `shadow`: DRY RUN — no owner pings send, no channel post goes out, no ClickUp writes
   execute; everything this prompt would send becomes one of the concrete preview forms
   established below — `SHADOW (would DM <owner>): ...` / `SHADOW (would DM
   <admin>): ...` / `SHADOW (would post to #captains-quarters): ...` / `SHADOW (would
   write): <task id> <operation>` — sent only to `shadow_recipient` from
   `data/captain-channels.json`. `<owner>`/`<admin>` are always the actual resolved Slack
   user id, never a generic role label, rendered as `Name (Uxxxxxxxx)` by resolving the name
   via `message(action=member-info)` — the same lookup step 3c.i's CHASE already uses to
   resolve the owner, so this is an existing capability, not a new one. **Never fabricate a
   name:** if `member-info` fails, returns nothing, or returns only an email, render the bare
   id exactly as today — a plausible-but-wrong name attached to a message about someone's
   work is worse than an id, since it could make a reviewer approve a message aimed at the
   wrong person. Never render an email address into a Slack post; email is a lookup input
   only (e.g. ClickUp assignee email -> Slack user), never rendered output. A ClickUp-write
   preview always names the real
   task id and the operation (e.g. `status → Blocked`, `comment added`) so the preview
   alone says who or what would have been touched. Delivery: every preview line above is
   sent as one or more Slack posts to `shadow_recipient` via `message(action=send,
   channel=slack, account=slack_account, target=shadow_recipient, message=...)`, passing the
   `slack_account` value from `data/captain-channels.json` and the `shadow_recipient` config
   value through verbatim — the delivery itself is a channel post, not a DM; only the resolved
   `<owner>`/`<admin>` named inside a `SHADOW (would DM ...)` line describes what would have
   been a DM. If `shadow_recipient` itself cannot be resolved (e.g. Captain's bot is not a
   member of the channel), record `{shadow_recipient_unresolved: true}` in state and send
   nothing further this run rather than failing silently. Local state (blocker ledger,
   state file) is still written in `shadow`, because that makes the preview realistic. In
   `data/audit-log.jsonl`: no `clickup_*` audit rows are produced in `shadow` (no real
   ClickUp write happens, so there is nothing to audit under those event names) — but
   ledger events (`blocker_added`, `blocker_updated`) ARE written normally, since they are
   local state, not an external effect. This distinction matters because the Friday
   stale-task briefing counts `clickup_*` audit rows as evidence work moved; a shadow
   preview producing those rows would corrupt that report, while ledger rows are harmless
   because nothing downstream treats them as ClickUp evidence.
4. If `live`: owner pings send for real, the digest posts to `#captains-quarters`, and
   ClickUp writes execute for real.
5. If `audience` is any value not listed above (not `off`/`shadow`/`live`): treat it as
   `off`. The fail-safe action is: record `{audience_unrecognized: <value>}` in the state
   file so the misconfiguration is visible rather than silent, then give final response
   `NO_REPLY` and do nothing else.

Admin recipients (for "DM the responsible Admin" below):
- Gavin: `user:U0B4G00QXT8`
- Arnold: `user:U043AKSJC85`
- Raj: `user:U09MVE90E4C`

Hard rules:
1. ClickUp writes in this cron (live audience only, all audited; in shadow these become
   `SHADOW (would write): <task id> <operation>` lines to `shadow_recipient` instead of
   executing, e.g. `SHADOW (would write): 86abc123 status → Blocked`): set the linked
   task's status to `Blocked` via `scripts/clickup_write.py --execute update-task
   --task-id <id> --status Blocked`, and add the explanatory comment via `--execute
   comment-task --task-id <id> --text "Blocked: <what/since when/what's needed>"`. If the
   result carries `needs_blocked_status`, the status was left unchanged — keep the comment
   and flag the list in the digest (live: posted to `#captains-quarters`; shadow: in the
   `shadow_recipient` preview) as `needs Blocked status added`. On clear, comment `Cleared:
   <evidence>` via `--execute comment-task --task-id <id> --text "Cleared: <evidence>"`
   (live audience writes it for real; shadow previews it) — status advancement stays with
   the owner/check-in flow. Task creation from this cron is limited to the ClickUp-home
   step below (giving an unlinked blocker a task to live on) — it never creates a separate
   "blocker" task when an original task already exists, per MEMORY.md.
   Every `clickup_write.py` invocation prints `{ok, succeeded, failed}` and exits 0 even when
   `failed` is non-empty — always inspect `failed` in the printed JSON, never rely on exit
   code. Treat any operation in `failed` as not done: do not advance that blocker's ledger
   status past what actually happened, and list it in the digest's `Failed:` section (live:
   posted to `#captains-quarters`; shadow: in the `shadow_recipient` preview) with the
   blocker id and the error.
2. Owner pings (live audience: sent for real to the owner's Slack DM; shadow audience:
   emitted as a `SHADOW (would DM <owner>): ...` line to `shadow_recipient` only) use the
   employee check-in lane rules from MEMORY.md: lightweight, no jargon ("blocker" is
   internal vocabulary — don't use it with employees), no format demands; voice or text
   replies both fine. One ping per owner per day maximum, enforced within this run as well as
   across runs: immediately after each send in `live` audience (or each `SHADOW (would DM
   <owner>): ...` line in `shadow` audience), record `pings_sent[owner_slack_id] = <date>` in
   the in-memory state and persist the state file before moving to the next blocker — do not
   batch all pings_sent writes to the end. Before sending to any owner, check both the
   persisted state (prior runs today) and this run's in-memory pings_sent (prior blockers
   already processed this run). If the owner already has a ping recorded for today from
   either source, do not send a second DM: a message already sent cannot be edited after the
   fact, so the only way to combine blockers is before that first send. If this owner's
   message for today has not yet been sent (i.e. this run has not reached their first
   blocker yet), compose both blockers into that one ask before sending it. If the message
   has already been sent, defer this blocker's ping to tomorrow and note the deferral in the
   digest.
3. Never DM excluded_user_ids (live or shadow — this applies regardless of audience). Slack
   via `message` tool only. Final response `NO_REPLY`.
4. Every Slack send passes `account=slack_account` (from `data/captain-channels.json`)
   alongside `channel=slack` and `target=...`. OpenClaw has more than one Slack account
   configured; omitting the account sends as the wrong app and fails with a misleading
   Slack `channel_not_found` error.

State file: `data/daily-blocker-chase-state.json`
(keys: last_run_at, pings_sent {owner_slack_id: date}, escalations {blocker_id: date},
audience, audience_unrecognized, board_fetch_failed, needs_task_match [blocker ids whose
underlying work is too ambiguous to safely match to an existing task or create a new one
for — surfaced in the digest so a human identifies the task, never left silently
ledger-only], shadow_recipient_unresolved, runs[] — see step 6)

Workflow:
1. Work from `/Users/owen/.openclaw/workspace-captain`. Read `MEMORY.md`.
2. `python3 scripts/blocker_ledger.py list` → open blockers. If empty: update state — including
   the `runs[]` append from step 6 below with `action: "no_op"` and `reason: "no open
   blockers"` — then `NO_REPLY`. A day with nothing to chase is exactly the case this record
   exists to make visible: without it, a legitimate zero-blocker day is indistinguishable from
   the cron never having run.
2b. Board fetch (read-only): `scripts/fetch_clickup_tasks.py --out
   data/board-snapshots/$(date +%F)-chase-fetch.json`. Step 3's CLEAR CHECK and ClickUp-home
   matching both read this, so Captain checks against today's real board state, not
   yesterday's. If it exits non-zero (e.g. missing ClickUp credentials), record
   `{board_fetch_failed: true}` in state, note the gap in the digest, and for this run:
   CLEAR CHECK falls back to the owner-reply signal only (no board-status signal
   available), and the ClickUp-home check below is skipped for every blocker this run —
   never guess a match or create a task against stale/missing board data; those blockers
   stay unlinked and are retried next run.
3. For each blocker:
   a. ClickUp home check (skip entirely this run if `board_fetch_failed` was just
      recorded): if this blocker's `clickup_task_id` is empty, give it a ClickUp home
      before anything else, so it is visible to humans on the board and a human resolving
      it later is detectable by the CLEAR CHECK below. Match first — search the board fetch
      for an existing task this blocker is clearly about (task name/description, owner,
      source context); prefer an existing task, then a parent task, over creating anything
      new, per MEMORY.md's rule that blockers live on the original task, never as
      standalone blocker tickets:
      - Confident match: `python3 scripts/blocker_ledger.py update --id <id>
        --clickup-task-id <matched task id>` (local state, both audiences), then continue
        into CLEAR CHECK below using that task.
      - No confident match: create a task that represents the actual work the blocker is
        about — not a "blocker ticket" — via `scripts/clickup_write.py --execute
        create-task --list-id <most relevant list> --name "<the work item itself, not
        'Blocker: ...'>"` (live audience executes for real; shadow emits `SHADOW (would
        write): <would-be task> create-task in list <list id>` instead of executing). If
        the blocker's `owner` is a known ClickUp member, add their numeric
        `--assignee <id>` to that command; otherwise pass `--owner "<name>"` so ownership
        lands on the Owners custom-labels field rather than only in the task name/prose
        (per MEMORY.md's standing rule — this applies in this cron's own scope, not just
        the check-in flow). No due date known → the `due_date_followup_required` rule
        applies (ask the owner through the approved messaging lane, subject to the
        one-ping-per-owner-per-day discipline in Hard rule 2). In `live` audience, once
        the create succeeds, link it back: `python3 scripts/blocker_ledger.py update --id
        <id> --clickup-task-id <new task id>`. In `shadow` audience no task actually
        exists, so skip the link and note in the digest that a home would be created
        live.
      - Too ambiguous to safely match or create (the standing ambiguity rule — writing
        against the wrong task is worse than a visible gap): do not silently leave it
        ledger-only. Record the blocker id in state `needs_task_match` and list it in the
        digest as needing a human to identify the task.
   b. CLEAR CHECK: look for clearing evidence from the board fetch above — the linked
      ClickUp task has moved to a done-type status, or its `Blocked` status was cleared by
      someone else (the task is no longer `Blocked` even though it hasn't reached a
      done-type status — a human closing the loop directly in ClickUp is clearing evidence
      just as much as an owner's Slack reply is) — or the owner's most recent reply says it
      is unblocked. This check runs against the current board fetch every cycle, so a human
      resolving a blocker in ClickUp is noticed without anyone telling Captain — at the next
      chase run (15:15 daily), not the instant the human edits the task; that latency is
      expected, not a bug. If found: `python3 scripts/blocker_ledger.py update --id <id>
      --status cleared --action-note "cleared per <evidence> <date>"` (this ledger
      transition runs in both `live` and `shadow` — it is local state, not an external
      effect). If a ClickUp task is linked, write the `Cleared: <evidence>` comment per
      Hard rule 1 in `live` audience (real write); in `shadow` audience emit the equivalent
      `SHADOW (would write): <task id> comment added` line instead. Then move on to the
      next blocker — no chase/escalate needed for this one.
   c. Otherwise decide — Captain can chase autonomously when the
      next step is information or a decision reachable by message AND the owner is known:
      i. CHASE: resolve the owner to a Slack user (memory, `message(action=member-info)`,
         ClickUp assignee email). Compose one short ask that includes what's needed and any
         prior art from memory (e.g. "last time this was the vendor portal login"). Send it
         to the owner's Slack DM in `live` audience; in `shadow` audience emit the
         `SHADOW (would DM <owner>): ...` line to `shadow_recipient` instead of sending —
         either way, apply the one-ping-per-owner-per-day enforcement from Hard rule 2
         immediately after. Then `python3 scripts/blocker_ledger.py update --id <id> --status
         chasing --action-note "pinged <owner> <date>"` (this ledger update runs in both
         `live` and `shadow` — it is local state, not an external effect). If a ClickUp task
         is linked: in `live` audience set status `Blocked` + explanatory comment per Hard
         rule 1; in `shadow` audience emit the equivalent `SHADOW (would write): <task id>
         status → Blocked` and `SHADOW (would write): <task id> comment added` lines
         instead.
      ii. ESCALATE when the owner is unknown, the blocker needs money/people/vendor
         authority, the same blocker was already chased yesterday without movement, or it
         sits on a critical path: in `live` audience, DM the responsible Admin (Gavin
         `user:U0B4G00QXT8`, Arnold `user:U043AKSJC85`, or Raj `user:U09MVE90E4C`, whichever
         is responsible for this area) with full context (what, since when, owner, linked
         task, what decision is needed); in `shadow` audience, emit that DM as a
         `SHADOW (would DM <admin>): ...` line to `shadow_recipient` instead of sending it to
         the Admin. In `live` audience set the linked task to `Blocked` + comment per Hard
         rule 1; in `shadow` audience emit the equivalent `SHADOW (would write): <task id>
         status → Blocked` and `SHADOW (would write): <task id> comment added` lines
         instead. Then, in both audiences, `python3 scripts/blocker_ledger.py update --id
         <id> --status escalated --action-note "escalated to admins <date>"`.
4. Chase digest (live audience: posted once to `#captains-quarters`; shadow audience: the
   same content as one `SHADOW (would post to #captains-quarters): ...` message sent only
   to `shadow_recipient`): `Captain blocker chase — <date>`; counts
   (open/chasing/escalated/cleared-today); per-blocker one-liners with age and last action;
   any `needs Blocked status added` lists; any `needs Owners label added` lists (from a
   create-task result whose `needs_owner_label` marker is set — the Owners field exists on
   that list but not yet this owner's label, and the public ClickUp API cannot add one; a
   human adds the label option in ClickUp settings); a `Needs task match:` section listing any
   blocker ids recorded in state `needs_task_match` this run (too ambiguous to match or
   create a ClickUp task for — a human must identify the task); a gaps line when
   `board_fetch_failed` was recorded this run; a `Failed:` section listing any ClickUp
   write whose operation came back in `failed` per Hard rule 1, with the blocker id and
   error; any pings deferred to tomorrow per Hard rule 2. Skip the post only when there are
   zero open blockers.
5. Same-cycle check: after step 3 every open blocker must have status `chasing`,
   `escalated`, or `cleared` with `last_action_at` today. If any slipped through, escalate it
   now: in `live` audience, DM the responsible Admin (Gavin `user:U0B4G00QXT8`, Arnold
   `user:U043AKSJC85`, or Raj `user:U09MVE90E4C`) per Hard rule/step 3c.ii and write ClickUp
   per Hard rule 1, then `python3 scripts/blocker_ledger.py update --id <id> --status
   escalated --action-note "escalated to admins <date>"`; in `shadow` audience, emit the DM
   as `SHADOW (would DM <admin>): ...` and the ClickUp write as `SHADOW (would write): <task
   id> <operation>` lines to `shadow_recipient`, and still run the ledger update for real
   (local state, both audiences).
6. Update state atomically (both audiences write state; live/shadow difference is external
   effects only, per the Mode gate). Also append one compact record to a `runs[]` array in
   this state file — same shape as `standup-transcript-clickup-reconciliation-state.json`
   already produces — so a run that chased or escalated (or found nothing to do) is durable
   evidence of what happened, not silence indistinguishable from breakage: `{run_at: <UTC ISO
   timestamp>, audience, action: <short verb summarizing this run's dominant outcome, e.g.
   "chased", "escalated", or "no_op" per step 2 above>, reason: <one line, only when action is
   "no_op" or a gap like board_fetch_failed was recorded this run>, counts: {open, chasing,
   escalated, cleared_today}}`. After appending, keep only the most recent 30 entries (drop
   the oldest first) so this file cannot grow without bound. Reply `NO_REPLY`.
