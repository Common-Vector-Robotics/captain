# Captain daily morning cycle

Purpose: Captain's first run each weekday morning. It reloads program memory and the
current ClickUp board, sweeps overnight Slack for anything urgent or safety-related,
pages a human immediately on any genuine safety issue, then sets today's top 3
priorities and posts a morning brief to Slack so the team starts the day oriented.

Schedule intent:
- Weekdays 07:30 America/Detroit, isolated session.

Mode gate (do this first):
1. Read `data/captain-modes.json`. Let `audience = DailyLoop.audience` (missing → `off`).
2. If `off`: final response `NO_REPLY`, do nothing else.
3. If `shadow`: DRY RUN. No channel posts, no eng-lead/employee DMs, no ClickUp writes.
   Everything this prompt would send goes ONLY to `shadow_recipient` from
   `data/captain-channels.json`, prefixed `SHADOW (would post to <program_channel>):` or
   `SHADOW (would DM <target>):` — `<target>` is always the actual resolved Slack user id
   (or channel id) being addressed, never a bare role label like "the owner". When `<target>`
   names a person, render it as `Name (Uxxxxxxxx)` by resolving the name via
   `message(action=member-info)` — the same lookup this prompt already uses for owner
   resolution in the safety gate below, so this is an existing capability, not a new one.
   **Never fabricate a name:** if `member-info` fails, returns nothing, or returns only an
   email, render the bare id exactly as today — a plausible-but-wrong name attached to a
   message about someone's work is worse than an id, since it could make a reviewer approve a
   message aimed at the wrong person. Never render an email address into a Slack post; email
   is a lookup input only (e.g. ClickUp assignee email -> Slack user), never rendered output.
   Every ClickUp
   write becomes a `SHADOW (would write): <task id> <operation>` line (e.g. `status →
   Blocked`, `comment added`, or `create-task in list <list id>`) instead of executing, so
   the preview alone says which task and what would change. Delivery: every preview above is
   sent as one or more Slack posts to `shadow_recipient` via `message(action=send,
   channel=slack, account=slack_account, target=shadow_recipient, message=...)`, passing the
   `slack_account` value from `data/captain-channels.json` and the `shadow_recipient` config
   value through verbatim — the delivery itself is a channel post, not a DM; only the resolved
   `<target>`/`<owner>`/`<admin>` named inside a `SHADOW (would DM ...)` line describes what
   would have been a DM. If `shadow_recipient` itself cannot be resolved (e.g. Captain's bot
   is not a member of the channel), record `{shadow_recipient_unresolved: true}` in state and
   send nothing further this run rather than failing silently. In `data/audit-log.jsonl`: no
   `clickup_*` audit rows are
   produced in `shadow` (no real ClickUp write happens, so there is nothing to audit under
   those event names) — but ledger events (`blocker_added`, `blocker_updated`) and cycle
   events (`daily_cycle_top3_set`, `daily_cycle_stamp`, `daily_cycle_personal_top2_set`)
   ARE written normally, since they are local state, not an external effect. That cycle
   list is exhaustive, not an example: every `daily_cycle_*` event this cron can emit is
   named there, so a reader checking an event against it gets a correct answer instead of
   guessing from a partial set. This distinction matters because the Friday
   stale-task briefing counts `clickup_*` audit rows as evidence work moved; a shadow
   preview producing those rows would corrupt that report, while ledger/cycle rows are
   harmless because nothing downstream treats them as ClickUp evidence.
4. If `live`: posts go to `program_channel`, pages go to eng leads, ClickUp writes execute.
5. If `audience` is any value not listed above: record `{audience_unrecognized: <value>}` in state file, final response `NO_REPLY`, do nothing else.

Hard rules:
1. ClickUp writes in this cron: safety-gate urgent task creation only, via audited tooling,
   live audience only. Everything else read-only.
2. Every Slack send passes `account=slack_account` (from `data/captain-channels.json`)
   alongside `channel=slack` and `target=...`. OpenClaw has more than one Slack account
   configured; omitting the account sends as the wrong app and fails with a misleading
   Slack `channel_not_found` error.
3. Slack via OpenClaw `message` tool only. Never DM excluded_user_ids.
4. Never print secrets; load `.secrets/clickup.env` without echoing.
5. Final response exactly `NO_REPLY`.

State file: `data/daily-morning-cycle-state.json`
(keys: last_run_at, last_scan_ts per channel id, last_brief_ts, blockers_paged,
channel_enumeration_unavailable, shadow_recipient_unresolved, personal_texts_sent
{slack_user_id: date}, personal_texts_failed {slack_user_id: error},
personal_texts_unresolved [board identities that did not resolve to exactly one
Slack user], personal_top2_failed, personal_top2_record_failed,
owners_labels_unavailable,
critical_paths_missing, priority_absent, runs[] — see step 8)

Workflow:
1. Work from the installed Captain workspace root (the directory containing `CLAW.md`).
   Read `MEMORY.md` when it exists; its absence is valid. Recall relevant program
   context with OpenClaw memory search tools when available. Otherwise read existing
   `memory/daily/` files from the last three days; an absent private memory overlay is
   valid and must not stop the workflow.
2. Board pull: load `.secrets/clickup.env`; run
   `scripts/fetch_clickup_tasks.py --out data/board-snapshots/$(date +%F)-morning-fetch.json`;
   then `scripts/daily_context.py --clickup data/board-snapshots/$(date +%F)-morning-fetch.json
   --snapshot-out data/board-snapshots/$(date +%F)-morning.json` and keep the JSON output.
3. Overnight Slack sweep: per `watch` in `data/captain-channels.json`
   (`{"mode": "all_except", "exclude_names": [...], "exclude_ids": [...]}`), enumerate every
   channel the Slack workspace exposes to Captain via the OpenClaw `message` tool's channel
   listing — Captain's bot only sees channels it is a member of, so "all" means all channels
   visible to that membership, not literally every channel in the workspace — and exclude any
   matching the configured `exclude_names`/`exclude_ids`. For each remaining channel,
   read new messages since that channel's `last_scan_ts` (or last 16 hours if unset) using the
   OpenClaw `message` tool channel-history reads. If history reads are unavailable or a channel
   is unreadable, record `{channel_id: "unreadable"}` in state and continue. If channel
   enumeration itself is unavailable this run, record the exact literal
   `channel_enumeration_unavailable: true` in state and fall back to scanning the channel ids
   listed in `watch.fallback_include_ids` in `data/captain-channels.json`, then continue; if
   that list is also empty, this sweep covers zero channels this run — note "no watch
   channels configured" once in state and continue (this is surfaced in the brief per step 7
   below, not silently absorbed).
3b. Read the threads of Captain's own posts from the last 2 days in `program_channel`
   (brief, wrap, digests). Treat replies as input: corrections get applied via audited
   tooling (live audience) and reflected in today's top-3; questions get answered in the
   same thread; anything ambiguous becomes a held item in today's brief. Replies never go
   unread.
4. Classify swept messages: `safety` if any `safety_keywords` match in context (read the actual
   message, don't keyword-fire blindly — "fire drill scheduled" is not an incident);
   `urgent` if `urgent_keywords` match and the thread shows real stoppage; else `routine`.
5. Safety gate: for each genuine safety/critical item:
   a. Page the engineering leads NOW, priority order: if `eng_leads` in
      `data/captain-channels.json` is non-empty, one Slack DM each to those ids; otherwise
      resolve the ClickUp assignees of the affected task/project (the task/project the
      safety item concerns) to Slack users — assignee email -> `message(action=member-info)`
      lookup, same pattern used elsewhere for owner resolution — and DM each resolved user;
      if no assignee can be resolved for anyone found (or no task/project is identifiable),
      that IS the fallback trigger, not a silent skip — DM `admin_recipients` instead. Never
      DM `excluded_user_ids`. Message: `Captain safety page:` + one-line summary + permalink +
      who appears involved.
   b. Open the incident thread: post the same summary to `program_channel` and use that
      message's thread as the incident thread — record its ts in state and post follow-ups
      there.
   c. File the urgent task directly: `scripts/clickup_write.py --execute create-task
      --list-id <most relevant list> --name "INCIDENT: <summary>" --priority 1` with a
      description containing the Slack permalink. If step (a) resolved a specific owner
      (an eng lead or the affected task/project's assignee), add them too: a numeric
      `--assignee <id>` when they are a known ClickUp member, otherwise `--owner "<name>"`
      so ownership lands on the Owners custom-labels field rather than only in the
      description, under the ownership rule in `TOOLS.md`. No due date known → the
      `due_date_followup_required` rule applies (ask the owner). Audited automatically. If
      the result carries `needs_owner_label` (Owners field exists on that list but not yet
      this owner's label — the public API cannot add one), the task is still created; note
      the gap in the incident thread so a human can add the label option.
   d. `scripts/blocker_ledger.py add --text <summary> --source slack:<channel_id>
      --source-ref <message_ts> --clickup-task-id <the id from step c's create-task result>`
      so this blocker already has its ClickUp home instead of waiting on the 15:15 chase to
      find and link it, and record the id in state `blockers_paged`.
6. Top-3: combine yesterday's `tomorrow_top3` (from context JSON `yesterday`), overdue and
   due-today tasks, open blockers, and critical-path constraints into today's three highest-
   leverage items. Store: `scripts/daily_cycle.py set-top3 --date $(date +%F) --items <json>`.
   Then `scripts/daily_cycle.py stamp --date $(date +%F) --phase morning`.
6b. Personal top-2 texts — one Slack DM per person naming their own two highest-
   leverage tasks. This is the per-person half of the brief in step 7; it runs
   BEFORE that post so the brief can report what it did.
   Before anything else in this step, clear this run's own outcome keys: set
   `personal_texts_failed` to `{}`, `personal_texts_unresolved` to `[]`, and
   `personal_top2_failed` and `personal_top2_record_failed` to `false`. These four
   are written only when something goes wrong, so nothing else ever clears them,
   and `scripts/captain_activity.py` surfaces any top-level state key whose value is
   literally `true` — leave them and one bad morning flags every later run's STATE
   line and digest as still broken. The `gaps` flags in (a) need no such clear
   because (a) copies them out of `rank` on every run, `false` included, so they
   self-clear; these four have no such writer. Clear them here, not in step 7, so
   every count the brief reads is this run's own.
   a. Rank: `python3 scripts/personal_top2.py rank --clickup
      data/board-snapshots/$(date +%F)-morning-fetch.json --date $(date +%F)`,
      reusing step 2's fetch — no second ClickUp call. If it exits non-zero,
      record `{personal_top2_failed: true}` in state, send no texts this run, note
      it in the brief, and continue to step 7. Copy every flag from the output's
      `gaps` object into state — `critical_paths_missing`,
      `owners_labels_unavailable`, `priority_absent` — so a degraded run is visible
      in `scripts/captain_activity.py`'s STATE lines and the daily digest without
      re-running the script.
   b. Resolve each person in `people[]` to exactly one Slack user id. Try the two
      paths below in that order, then apply the text-nobody rule to whatever they
      returned.
      i. Offline shortcut: use `admin_recipients` in `data/captain-channels.json`
         only on EXACT string equality — that person's `key` is character-for-
         character identical, case included, nothing trimmed, expanded or
         reformatted, to one of the KEYS of `admin_recipients`. Then use that
         key's mapped id and
         skip the lookup. Anything short of exact equality is NOT a match and
         falls through to (ii): a `people[]` key is an email, a ClickUp username,
         or an Owners label, so `Name Lastname`, `name`, and
         `name@example.com` all fail this test.
         Never substring-match, never compare first names only, never match
         against the ids on the right-hand side. A loose match here would send one
         person's tasks to a different human who merely shares a first name, and
         nothing downstream would catch it, because a hardcoded id always looks
         like a successful resolution. What exact equality cannot do is tell two
         humans apart when both present the SAME key: a second employee whose
         Owners label is also `Name` matches the key `Name` character-for-character
         and gets the configured id for that key. Do not read that case as caught here — it
         is not, and it is not detectable in this step either, because the
         ranking script groups people BY `key`, so two humans sharing one label
         already arrive as a single `people[]` entry with their tasks pooled. The
         only defense is that every `admin_recipients` key must name exactly one
         human; if you ever learn two people share one, say so in the brief so a
         human can re-key the config, and do not try to split them here.
      ii. Otherwise look the person up: for `source: "assignee"` look up their
         `email`; for `source: "owners_label"` look up the `key` (the label
         name) — both via `message(action=member-info)`, the same lookup the
         safety gate in step 5 already uses. **An `assignee` entry whose
         `email` is `null` has no usable lookup input.** That is a normal
         input, not a bug: `scripts/personal_top2.py` builds a person's `key`
         as email, else ClickUp username, else the numeric user id, and stores
         `email: null` whenever ClickUp exposes no email for that member — so
         `people[]` legitimately contains entries with `email: null` and a
         non-email `key`. For those, text nobody, record the identity in state
         `personal_texts_unresolved`, and never fall back to `username` or
         `clickup_user_id` as the lookup input instead. A ClickUp username or
         numeric id fed to `member-info` matches on display name, and a display
         name can belong to a different human — the same hazard the exact-
         equality rule in (i) closes for `admin_recipients`, except worse here:
         a single coincidental match returns exactly one unambiguous user, so
         it looks like clean resolution, the text-nobody rule below never
         fires, and nothing downstream notices that one person just got
         another person's tasks. This rule governs a null-`email` `assignee`
         entry no matter which path would have resolved it, (i) included: when
         `email` is `null` the `key` itself IS the username or the numeric id,
         so matching that key against `admin_recipients` is the very
         display-name match this rule forbids, merely done offline. It does not
         touch `source: "owners_label"` entries, whose `email` is always `null`
         by construction and whose `key` — the label — is their intended and
         only lookup input.
      **The text-nobody rule governs EVERY resolution path above — the
      `admin_recipients` shortcut in (i) exactly as much as the `member-info`
      lookup in (ii): if a board identity does not resolve to exactly one
      unambiguous Slack user — zero matches, several matches, only an email back,
      or a near-miss against an `admin_recipients` key that (ii) then could not
      resolve either — text nobody for that identity.** Record it in state
      `personal_texts_unresolved` and name it in the brief. A best-guess match
      would tell one person about another person's work, which is strictly worse
      than an unsent text. Skip anyone in `excluded_user_ids` in both audiences,
      `live` and `shadow` alike (the list lives in `data/captain-channels.json`):
      that list is who Captain must never message at all, and a shadow preview of
      a DM `live` would never send is a false rehearsal that invites a reviewer to
      approve a send that must not happen.
   c. Collapse duplicate board identities into one recipient. Two `people[]`
      entries can be the same human: Owners labels are read only for tasks with no
      assignees, so someone who owns one assigned task and one label-only task
      appears twice — once with `source: "assignee"`, once with
      `source: "owners_label"` — and both resolve to the same Slack user id. After
      resolution and before delivery, merge every group of entries that resolved to
      the same Slack user id into a single recipient. Merge their `candidates`
      lists stably instead of re-sorting from scratch: each list already arrives
      ranked by `rank`, so repeatedly take whichever list's head compares lower
      on `tier` ascending, then earliest `due` first with undated last, then
      `task_id` ascending; break an exact tie on those three by taking the
      `source: "assignee"` list's head first, and a tie between two lists of the
      same `source` by whichever list's `key` sorts first, so three or more
      merged identities are as deterministic as two; skip a task id already
      taken.
      Because this only ever pops heads, each identity's own ranked order
      survives intact, and every comparison is decided by fields `candidates`
      actually carries, so the result is deterministic and total — the `task_id`
      terminator leaves no pair undecided, and a re-run produces the same two
      tasks. Do not claim this reproduces `rank`'s own order, because it cannot:
      `rank` sorts on ClickUp priority and critical-path score as well, and
      `candidates` emits neither, so two tasks tied on `tier` and `due` come out
      here in `task_id` order where `rank` would have preferred the higher
      ClickUp priority. Preserving each already-ranked list is the fix for
      exactly that gap — it keeps the fuller order `rank` computed rather than
      re-deriving a coarser one from the three fields that survive. Then take
      the top 2 of the merged list as
      that person's two. Keep every merged `key` with the recipient so the brief
      and the record in (g) can still name the board identities the ranking came
      from. Without this merge one human gets two DMs the same morning, (g)
      stores two rows for one Slack id, and which identity's top 2 wins is
      arbitrary.
      Then cross-check the merged recipients before delivery, because merging on
      "same Slack id" cannot catch the opposite failure — one human resolving to
      TWO ids. The assignee path may resolve `name@example.com` to `U111` while
      the Owners-label path resolves the label `Name` to `U222`: another person, a
      guest account, a deactivated duplicate. Each lookup returned exactly one
      unambiguous user, so text-nobody does not fire and the merge above finds
      nothing to merge, and the morning sends two DMs — one telling the wrong
      person about the first person's label-only tasks. So: if two entries the board
      shows as the same human (an `owners_label` entry whose label matches an
      `assignee` entry's `username` or its resolved display name) resolved to
      different Slack ids, or if a resolved id is already claimed by another
      recipient this run, treat that resolution as ambiguous — text nobody for
      the `owners_label` entry, record it in state `personal_texts_unresolved`,
      and name it in the brief; keep the `assignee` entry, because a real
      ClickUp assignment is the authoritative owner (the same rule that makes
      the ranking script read Owners labels only for tasks with no assignees).
      If neither entry in a same-id collision is an `owners_label` entry, the
      merge above would already have combined them, so a surviving duplicate
      means resolution itself is unreliable this run — text nobody for both and
      record both. This second rule is also what makes the within-run
      no-duplicate property enforced rather than merely impossible by
      construction: after this step no two recipients may hold the same
      `slack_user_id`, and if one ever does, that is a resolution bug to
      record, not a person to DM twice.
   d. Optional override: if step 3's overnight sweep or recalled memory shows the
      board is wrong about someone's day — a task finished last night, superseded,
      or newly urgent per a Slack thread — reorder that person's two. The override
      may promote any task from that recipient's own merged `candidates` list; it
      may NOT introduce a task absent from `candidates`, because that would mean
      texting someone about work the board does not show them owning. Set
      `overridden: true` and a one-line `override_reason` for that person.
   e. Compose one message per recipient. Keep it lightweight under this prompt's
      check-in wording rule: no jargon — "blocker" is internal vocabulary and is
      never used with employees, which is why a stuck task is simply ranked lower
      rather than described as stuck — no demanded reply format, and no ask. These
      texts invite no reply; corrections arrive through the
      14:00 standup reconciliation and 15:15 chase as they already do. Shape:

       ```
       Morning. Two things worth your attention today:
       1. <task name> — <reason from the candidate>
          <task url>
       2. <task name> — <reason from the candidate>
          <task url>
       ```

      A person with one candidate gets a one-item message; a person with none is
      not a recipient at all.
   f. Deliver one recipient at a time, and do the already-texted check and the
      guard write INSIDE this loop — they are part of delivering a person, not a
      later pass over the finished list. One text per person per day, enforced
      within this run and across runs. Reading (f) as "send them all, then do the
      bookkeeping" is the exact bug this ordering forbids: a crash mid-loop would
      leave the state file claiming nobody was texted, and the next run would text
      everyone it had already reached a second time. For each recipient, in order:
      i. Check `personal_texts_sent[slack_user_id]` in state before doing anything
         else for this person. If it already holds today's date, that person is
         finished for today — no DM, no preview, no state write. Count them as
         already-texted for the brief and move to the next person.
      ii. In `live`: send exactly one DM via `message(action=send, channel=slack,
         account=slack_account, target=user:<resolved id>, message=...)`. The send
         has happened once that call returns success, and not before. Then record
         `personal_texts_sent[slack_user_id] = <date>` in state and persist the
         state file before moving to the next person — do not batch these writes to
         the end.
      iii. In `shadow`: send nothing to the person. Emit
         `SHADOW (would DM <target>): <the full message, both items and both
         reasons>` to `shadow_recipient` instead, batching blocks into as few posts
         as fit so `shadow_recipient` is not flooded (start a new post before one would
         exceed 3,500 characters; never split one person's block across two posts).
         Batching changes when a person's guard write happens, never whether it
         does: a person's entry is written only after the post containing that
         person's block has been sent and returned success — never when the block
         is merely composed or appended to a pending post, since an unsent post has
         previewed nothing. Concretely: accumulate blocks until the next one would
         overflow the post, send that post, then record
         `personal_texts_sent[slack_user_id] = <date>` for every person whose block
         that post carried and persist the state file before starting the next post
         — do not batch these writes to the end. Send the final pending post the
         same way before leaving this loop and record its people's entries as soon
         as it succeeds, so no one's preview is left unrecorded. Preview the full
         text, not a summary: the only question shadow review exists to answer is
         whether Captain would text the right person the right two things.
         `<target>` follows the same `Name (Uxxxxxxxx)` convention as the Mode gate
         above, including its no-fabrication rule.
      iv. If a send fails — a `live` DM, or a `shadow` post — record
         `personal_texts_failed[slack_user_id] = <error>` for that person (for
         every person whose block a failed shadow post carried), leave their
         `personal_texts_sent` entry unwritten so a later run can still reach them,
         continue with the next person, and report the count in the brief.
   g. Only now, with delivery finished, persist the day's durable record of what
      was actually sent: `python3 scripts/personal_top2.py set --date $(date +%F)
      --items <json>`, where each entry is `{slack_user_id, key, task_ids,
      overridden, override_reason}` for a recipient this run really DMed in `live`
      or really previewed in `shadow`. `key` is the board identity the ranking came
      from; for a recipient merged in (c) it is the JSON array of all its merged
      keys, so one row still traces back to every board identity that fed it
      instead of silently naming one and dropping the other. This runs AFTER (f),
      never before: the
      already-texted skips in (f.i) and the failures in (f.iv) are only known once
      the loop is done, and a `set` before delivery would record texts that were
      then correctly never sent — on a re-run after a partial failure, precisely
      the run someone reads this record to understand. `set` replaces the whole
      row for that date, so pass the union of three sources. (1) What `python3
      scripts/personal_top2.py get --date $(date +%F)` already holds for today,
      minus any entry whose `slack_user_id` this run delivered again — that
      command prints `null` when no row exists for the date and `[]` when a row
      exists with no personal_top2 yet, so treat `null` and `[]` alike as no
      prior record and never try to union `null`. (2) This run's own deliveries.
      (3) An entry, composed by this run, for every recipient the pre-send check
      in (f.i) skipped as already-texted today whose `slack_user_id` is absent
      from (1) — same five fields as any other entry, carrying this run's own
      ranking for that person, which is the closest honest reconstruction
      available: the text they actually received came from an earlier run whose
      `task_ids` were never written down, and recording today's ranking for a
      person who really was texted beats recording nothing and implying they
      were not. Source (3) is not redundant with (1): an earlier run persists
      `personal_texts_sent` inside the (f) loop, so it can die after texting
      people but before reaching this step — then today's date is in the guard
      while `get` returns no row at all. Those people really were texted, (f.i)
      therefore skips them so they are absent from (2), and (1) has nothing for
      them either, so without (3) this `set` would overwrite the day's durable
      record with only the people this run happened to reach and erase the rest.
      Without the union at all, a re-run would erase the earlier run's
      record of texts people really received. People who were skipped as
      unresolved, or whose send failed, are not entries. This is local state, so it runs for real in both
      `live` and `shadow`; only the DM in (f.ii) is audience-dependent.
      If this `set` exits non-zero — `data/captain.sqlite` locked by another
      process, or an `override_reason` this run composed carrying a quote or a
      newline that breaks `--items <json>` shell quoting — record
      `{personal_top2_record_failed: true}` in state, name it in step 7's brief as
      `personal top-2 record not persisted`, and CONTINUE to step 7. Do not halt.
      The DMs in (f) have already gone out by the time this runs, which is what
      makes this the one failure in step 6b that must not stop the run: halting
      here would cost the brief as well as the record, leaving a morning where real
      texts reached real people and Captain said nothing about them anywhere. A
      lost durable record is reconstructible from `personal_texts_sent` and the
      brief; an unposted brief is not reconstructible at all.
   h. This lane is deliberately SEPARATE from the 15:15 blocker chase's
      `pings_sent` budget in `data/daily-blocker-chase-state.json`, and neither
      cron reads the other's key. The morning text is a
      no-reply briefing; a chase ping is a real ask. Sharing one budget would let
      a briefing suppress a chase and break that cron's "no open blocker ends the
      day unowned" guarantee. Do not unify them.
7. Morning brief (one message, ≤ 10 lines, Slack mrkdwn) posted to `program_channel`:
   resolve the channel id from `program_channel` in `data/captain-channels.json`; if the id
   is empty, resolve `program_channel` by name through the `message` tool; if that fails,
   send one fallback to every configured administrator with the blocker `program channel
   unresolved`. If no administrator resolves, record the routing failure and send
   nothing rather than guessing a person. Content:
   `Captain morning brief — <date>`; Top 3 with owners; counts line
   (open/overdue/due-today/owner-gaps from context JSON);
   a personal-texts line — `Personal texts: <n> sent, <n> already texted today,
   <n> unresolved (<the unresolved board identities>), <n> failed` — where ALL FOUR
   counts are this run's own, not a running total: "sent" counts this run's own DMs
   (or shadow previews); "already texted today" counts the people step 6b (f.i)
   skipped because state already held today's date for them; and "unresolved" and
   "failed" count only what THIS run recorded in `personal_texts_unresolved` and
   `personal_texts_failed`, which is exactly what step 6b's start-of-step clear
   makes true — read those two keys as they stand now, and never add in a prior
   run's. Plus `ranking unavailable` when `personal_top2_failed` was recorded this
   run, `personal top-2 record not persisted` when `personal_top2_record_failed`
   was, `critical-path data unavailable` when `critical_paths_missing` was, and
   `Owners-label owners not reachable this run` when `owners_labels_unavailable`
   was;
   open blockers with status;
   anything material from the sweep with permalinks; a gaps line when
   `channel_enumeration_unavailable` was recorded this run — e.g. `overnight sweep covered 0
   channels — enumeration unavailable, no fallback channels configured`; end with
   `Reply in thread with corrections; standup reconciliation runs at 14:00.`
8. Update state atomically (last_run_at, per-channel last_scan_ts, brief message ts). Also
   append one compact record to a `runs[]` array in this state file — same shape as
   `standup-transcript-clickup-reconciliation-state.json` already produces — so a run that
   posted nothing unusual is still durable evidence the cron fired and what it decided, not
   silence indistinguishable from breakage: `{run_at: <UTC ISO timestamp>, audience, action:
   <short verb, e.g. "posted_brief" normally or "escalated" when the safety gate paged this
   run>, reason: <one line, only when action is "escalated" or a gap like
   channel_enumeration_unavailable was recorded this run>, counts: {top3, overdue,
   due_today, owner_gaps, blockers_paged, personal_texts}}`. After appending, keep only the most recent 30
   entries (drop the oldest first) so this file cannot grow without bound. This is local
   state, so — like the rest of this state file — it is written for real in both `live` and
   `shadow` audiences, not only `live`.
9. Reply `NO_REPLY`.
