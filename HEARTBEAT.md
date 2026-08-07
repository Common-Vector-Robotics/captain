# HEARTBEAT.md - Captain

Pilot heartbeat behavior:

1. If no configured pilot data exists, reply `HEARTBEAT_OK`.
2. Do not perform expensive full reconciliation every heartbeat.
3. Check for urgent stale approval items or failed scheduled runs.
4. Overnight monitor (daily loop): if `data/captain-modes.json` has `DailyLoop.audience`
   set to `shadow` or `live`, also scan channels for new messages matching `safety_keywords`
   since the last heartbeat (track `last_heartbeat_scan_ts` in
   `data/heartbeat-monitor-state.json`). Per `watch` in `data/captain-channels.json`
   (`{"mode": "all_except", "exclude_names": [...], "exclude_ids": [...]}`): enumerate every
   channel the Slack workspace exposes to Captain via the OpenClaw `message` tool's channel
   listing — note Captain's bot only sees channels it is a member of, so "all" means all
   channels visible to that membership, not literally every channel in the workspace — and
   scan all of them except those matching `exclude_names`/`exclude_ids` (currently
   `#random`). If channel enumeration is unavailable this run, record the exact literal
   `channel_enumeration_unavailable: true` in state and fall back to scanning the channel ids
   listed in `watch.fallback_include_ids` in `data/captain-channels.json`, then continue. If
   enumeration failed AND `fallback_include_ids` is empty, this sweep covers zero channels
   this run — a zero-channel overnight sweep is itself material, so report it per the
   material update format below rather than staying silent. Read matches in context; ignore
   non-incidents.
5. Safety-page exception: a genuine safety/critical incident is the ONLY thing a heartbeat
   may send — one Slack DM per incident, priority order: if `eng_leads` in
   `data/captain-channels.json` is non-empty, DM those ids; otherwise resolve the ClickUp
   assignees of the affected task/project to Slack users (assignee email ->
   `message(action=member-info)` lookup, same as the daily prompts) and DM each resolved
   user; if no assignee can be resolved for anyone found (or no task/project is
   identifiable), that IS the fallback trigger — DM `admin_recipients` instead, never a
   silent skip — plus an incident post in `#captains-quarters`. In `live` audience these
   send for real; in `shadow` audience each becomes one of the daily prompts' established
   forms sent only to `shadow_recipient` instead: `SHADOW (would DM <resolved-id>): ...`
   for each page (the actual resolved Slack user id, never a role label), rendered as
   `Name (Uxxxxxxxx)` via the same `message(action=member-info)` lookup used above to resolve
   assignees. **Never fabricate a name:** if `member-info` fails, returns nothing, or returns
   only an email, render the bare id exactly as today — a plausible-but-wrong name attached to
   a safety page is worse than an id, since it could send the page toward the wrong person's
   name being trusted. Never render an email address into a Slack post; email is a lookup
   input only, never rendered output. And `SHADOW
   (would post to #captains-quarters): ...` for the incident post — in the material update
   format below either way. Delivery: every preview line above is sent as one or more Slack
   posts to `shadow_recipient` via `message(action=send, channel=slack,
   account=slack_account, target=shadow_recipient, message=...)`, passing the `slack_account`
   value from `data/captain-channels.json` and the `shadow_recipient` config value through
   verbatim — the delivery itself is a channel post, not a DM. If `shadow_recipient` itself cannot be
   resolved (e.g. Captain's bot is not a member of the channel), record
   `{shadow_recipient_unresolved: true}` in `data/heartbeat-monitor-state.json` and send
   nothing further this heartbeat rather than failing silently. Never DM `excluded_user_ids`. Regardless of audience, also run
   `python3 scripts/blocker_ledger.py add --text "<one-line incident summary>" --source
   slack:<channel_id> --source-ref <message_ts>` for real — the ledger write is local state,
   not an external effect, so it is not redirected to `shadow_recipient` in shadow audience.
   Everything else: stay silent.
6. Never mutate ClickUp from heartbeat — the next morning cycle files the urgent task.
   Never DM task owners/employees from heartbeat, with exactly one sanctioned exception: the
   step 5 safety-page DM to resolved ClickUp assignees on a genuine safety/critical incident.
   Everything else — check-ins, chases, questions, or any non-incident reason — remains
   forbidden from heartbeat.
7. Stay silent unless there is a material blocker, failed job, or approval item needing
   attention.
8. Every Slack send passes `account=slack_account` (from `data/captain-channels.json`)
   alongside `channel=slack` and `target=...`. OpenClaw has more than one Slack account
   configured; omitting the account sends as the wrong app and fails with a misleading
   Slack `channel_not_found` error.

Material update format:

- `Captain:` one-line summary
- `Evidence:` file/task/source
- `Needed:` specific decision/action
