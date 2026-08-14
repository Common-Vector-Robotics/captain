# Captain meeting transcript → ClickUp reconciliation

Purpose: discover configured Gemini meeting notes from Gmail, read both the raw
Google Docs Transcript and the Notes summary, and reconcile clear execution changes
into ClickUp through Captain's audited tooling.

## Mode gate — do this before mailbox access

1. Read `data/captain-modes.json` and set `audience` from
   `DailyLoop.audience`. A missing value means `off`.
2. `off`: do not read Gmail or Google Docs, send Slack, mutate ClickUp, or update
   the blocker ledger. Finish with exactly `NO_REPLY`.
3. An audience other than `off`, `shadow`, or `live` is treated as `off`. If an
   existing state file is writable, record `audience_unrecognized` there without
   accessing the mailbox or making an external change. Finish `NO_REPLY`.
4. `shadow`: perform discovery and analysis and persist local state and blocker-ledger
   evidence. Do not mutate ClickUp and do not create any `clickup_*` row in
   `data/audit-log.jsonl`. Send every preview and setup/access blocker only to
   `shadow_recipient` from `data/captain-channels.json`.
5. `live`: execute only unambiguous ClickUp writes through audited Captain tooling,
   post the reconciliation digest to the configured `program_channel`, and route
   setup or document-access blockers to configured `admin_recipients`.

## Configuration

After the mode gate permits work, load:

- `data/meeting-ingestion.json`
- `data/captain-channels.json`

The ingestion file must contain:

- `google_cli`: non-empty command or absolute executable path for the authenticated
  Google CLI. Invoke exactly that executable; do not search for a different account.
- `google_account`: non-empty Gmail address already authenticated for Gmail, Drive,
  and Docs.
- `sender`: non-empty meeting-notes sender address.
- `subject_prefixes`: non-empty array of non-empty subject prefixes.
- `meeting_title_patterns`: non-empty array of non-empty meeting-title fragments.
- `lookback_days`: integer from 1 through 30.
- `local_summary_directory`: either `null` or a readable directory path.

The channel file must provide `slack_account`, `program_channel`,
`shadow_recipient`, `admin_recipients`, and `excluded_user_ids`. `program_channel` may
be either a non-empty string or an object with non-empty string `name` and `id` fields.
Normalize it once into separate presentation and routing values:

| Configured `program_channel` | `program_channel_display` | `program_channel_target` |
| --- | --- | --- |
| `{"name":"captains-quarters","id":"C0123456789"}` | `#captains-quarters` | `C0123456789` |
| `"captains-quarters"` | `#captains-quarters` | `captains-quarters` |

For an object, remove at most one leading `#` from `name` before adding exactly one
for `program_channel_display`, and copy `id` verbatim to `program_channel_target`.
Never render the object itself and never use its display name as the live target. For
a string, copy the complete string verbatim to `program_channel_target`; for display,
keep one existing leading `#` or add one when absent. Display normalization never
changes the configured live destination. Use every other routing value from the files
verbatim. Never replace a missing value with a guessed person, account, channel, path,
or meeting title.

If configuration is missing or invalid, record `configuration_error` in the state file
when possible and make no ClickUp write. In `shadow`, send one concise setup blocker to
`shadow_recipient`. In `live`, send it to each configured administrator unless the
channel configuration itself is invalid; in that case record the routing failure and
send nothing. Never start an interactive OAuth flow from this cron.

## State and idempotency

Use `data/meeting-transcript-clickup-reconciliation-state.json`. Create it when missing.
Maintain:

- `candidates`: document id, email message/thread id, meeting title/date, source
  availability, `partial`, `partial_expired`, attribution evidence, stable proposal
  ids, and proposal outcomes.
- `last_report_at`, `last_report_target`, `last_counts`, and current degraded flags.
- `runs[]`: append one record for every permitted run, including legitimate no-ops,
  with timestamp, audience, candidates checked, proposals, writes, held items,
  blockers, and routing result. Retain only the most recent 30 entries.

A fully processed candidate is not processed again. A candidate with only one source
is `partial: true` and is checked again on every run while its meeting date remains
inside `lookback_days`. Once it ages out, set `partial_expired: true` and stop retrying.

Generate a stable proposal id from the document id, normalized action, matched ClickUp
task id (or destination list id for a new task), and operation type. Before any write,
check both candidate proposal history and `data/audit-log.jsonl`; never repeat a
successful operation.

## Gmail and Google Docs access

Use only `google_cli` with `google_account` for every Gmail, Docs, and Drive
operation. The account token must grant only the required Gmail read-only, Drive
read-only, and Docs read-only scopes. Never use a browser, another executable,
another account, or a broader-scope token as a fallback.

1. Search the configured Gmail account with the configured `google_cli`, using
   `--no-input` or the CLI's equivalent non-interactive option. Limit the query to the
   configured sender and lookback window.
2. Keep only messages whose subject begins with a configured `subject_prefixes` value
   and contains a configured `meeting_title_patterns` value. Treat matching as
   case-insensitive. Process every unprocessed match, newest first.
3. Read each email only far enough to obtain its meeting title/date and the Google Docs
   `Open meeting notes` link. If the normal body omits the URL, fetch the message as raw
   MIME with exactly
   `<google_cli> gmail get <message-id> --format raw --json --account <google_account> --no-input`,
   decode the HTML part, and extract only a
   `https://docs.google.com/document/d/<DOC_ID>/...` link.
4. Read a Google Docs document as text by invoking the configured executable and
   account in exactly this form (replace only the bracketed values):
   `<google_cli> docs cat <docId> --account <google_account> --no-input`. Use its
   stdout in memory. If the configured executable and account cannot read both document
   tabs with least-privilege read-only authorization, record a source-access blocker and
   fail closed.
5. Only for a Drive file confirmed not to be a Google Docs document, choose a
   regular file inside an owner-only temporary directory whose explicit name already
   includes the expected extension. Invoke exactly
   `<google_cli> drive download <fileId> --out <temporary-file-with-extension> --account <google_account> --no-input`.
   Do not use `--format` for a non-Docs file, do not use a device or stream path for
   `--out`, and do not depend on the CLI to append an extension. Require the result to
   be a regular file inside that directory, read only the needed text, and remove the
   exact temporary directory immediately afterward. If a safe filename or file type
   cannot be established, record a source-access blocker instead of downloading.
6. Extract `Transcript` and `Notes` into separate in-memory sources. Do not save their
   raw contents in state, audit logs, Slack, or tracked files. When
   `local_summary_directory` is configured, look for a clearly matching curated summary
   and treat it as additional Notes evidence, never as a replacement for Transcript.
7. Record `transcript_status` and `notes_status` separately. Analyze Transcript first,
   then make an independent Notes pass for decisions or actions the transcript pass
   missed. Notes is a compression, not ground truth; when the two readings conflict,
   conversational evidence in Transcript wins.

If authentication fails, record `authentication_required` and ask the routed recipient
to authenticate `google_account`; do not launch OAuth. If the email exists but the Doc
returns forbidden or not-found, record `permission_denied`, document id, and exact error
class, and ask that the document be shared with `google_account`. In `shadow`, that ask
goes only to `shadow_recipient`; in `live`, it goes only to configured administrators.

If only Notes is available, continue but mark every resulting proposal
`summary_only: true`; summary-only evidence cannot establish an owner, so hold any write
that needs owner attribution. If only Transcript is available, continue from Transcript
and mark the candidate partial. Report a source blocker only when an expected matching
meeting has neither source.

## Speaker and owner attribution

A transcript speaker label can name a device or meeting-room account rather than the
person speaking. It is a hint, never sufficient ownership evidence.

Use these signals in order:

1. Hand-offs establish the floor: phrases such as “you are next,” “over to you,” or
   “go ahead” identify who owns the following report until the next hand-off or clear
   topic change.
2. First-person language commits the current floor-holder. Second-person language is a
   request, not acceptance.
3. Acceptance creates ownership. Look for “I will,” “got it,” “on me,” or the addressee
   beginning a concrete plan. An unanswered, deferred, or rejected request has no owner.
4. Self-identification and named conversational references can anchor the floor.
5. Existing ClickUp ownership and subject-matter match corroborate an attribution but
   cannot replace acceptance.
6. Roll-call order may corroborate later segments in the same meeting, but never carry
   that inferred order into another meeting.
7. Notes attribution is an independent weak signal. It never overrides conflicting
   transcript conversation and cannot independently establish an owner.

For each proposal record:

```text
attribution: {
  owner,
  basis: handoff | first-person | acceptance | self-identification |
         content-match | summary | unresolved,
  speaker_label,
  confidence,
  document_id,
  ask_ref,
  acceptance_ref
}
```

`ask_ref` and `acceptance_ref` contain a timestamp or short paraphrase, never a raw
transcript block. Hold the proposal if the task match, destination list, owner, status,
or acceptance remains genuinely uncertain. State exactly what evidence is missing.
Never create or assign work to a room/device label, a summary-only name, or the person
addressed by an unaccepted request.

## ClickUp reconciliation

1. Fetch a fresh board snapshot with `scripts/fetch_clickup_tasks.py`. Use configured
   ClickUp list/space filters when present.
2. Extract explicit commitments, accepted actions, status changes, blockers,
   dependencies, due dates, and definitions of done from the two-source analysis.
3. Match each item against current tasks. Prefer updating an existing task over creating
   a duplicate. Hold the item if more than one task or destination list is plausible.
4. Infer a due date or definition of done only when meeting context makes it concrete.
   Do not expand scope beyond the meeting evidence.
5. A real blocker updates the original task to `Blocked` and adds a concise explanatory
   comment. If the destination list has no Blocked status, preserve the current status,
   carry `needs_blocked_status`, and report the missing status rather than mapping it to
   another value.
6. Add or update each current blocker in the local ledger with
   `scripts/blocker_ledger.py`; clear it when the meeting explicitly resolves it.
7. Divide proposals into an unambiguous write set and a held set. One held item never
   stops unrelated safe items.
8. In `shadow`, render each proposed mutation as
   `SHADOW (would write): <task-id-or-new-task> <operation>` and send it only to
   `shadow_recipient`. Do not call the ClickUp writer.
9. In `live`, write the safe set with `scripts/clickup_write.py`, preferring one audited
   batch operation. Every actual create, update, or comment must append its normal
   `clickup_task_create`, `clickup_task_update`, or `clickup_task_comment` event to
   `data/audit-log.jsonl`, including source document id, proposal id, payload, result,
   and timestamp. A write without its audit row is a failure and must not be reported
   as completed.
10. If a write partly fails, record each result independently. Retry only operations
    that have no successful audit record.

## Slack reporting

Every Slack send uses the OpenClaw `message` tool with `channel=slack` and
`account=slack_account`, where the account value is copied verbatim from configuration.
Never use raw Slack HTTP calls. Never send to a value listed in `excluded_user_ids`.

Render the reconciliation digest in Slack mrkdwn, at most 10 lines:

```text
Captain meeting reconciliation — <meeting date/title>
Wrote: <audited changes, or none>
Held: <only decision-relevant ambiguities, or none>
Blockers: <active blockers/access failures, or none>
Evidence: <document id and timestamped paraphrase references>
```

Do not expose raw emails, transcripts, summaries, email addresses, or long quotations.
Use the minimum timestamped paraphrase needed to make a decision reviewable.

In `shadow`, prefix the digest
`SHADOW (would post to <program_channel_display>):` but send it only to the exact
configured `shadow_recipient`. If that recipient cannot be resolved, record
`shadow_recipient_unresolved: true` and send nothing else.

In `live`, send the digest only to `program_channel_target`, with
`account=slack_account`; do not resolve either value to a different destination or account.
Include held questions there when they are useful to the team. Send a separate
administrator message only for a setup, authentication, document-access, or routing
decision that prevents safe processing. Resolve administrator names for display when
possible, but never fabricate a name or render an email address; use the bare Slack id
when name resolution fails.

After state is durably updated and reporting is complete—or after a legitimate no-op—return
exactly `NO_REPLY`.
