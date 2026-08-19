---
name: captain
description: Use when the user asks to report completed coding-agent work to their Captain project manager.
---

# Report coding work to Captain

Use this workflow once the user asks to report completed coding-agent work.

1. Gather the Git root, branch and upstream, short status, recent commits, diff
   stats, completed work, changed files, verification actually run, decisions,
   blockers, risks, and next steps. Do this once; keep the result concise.
   Use only values observed in the session or local inspection. Omit optional
   values that cannot be established; never invent timestamps or diff data.
2. Make one stable, safe `report_id` and reuse it for every replay:

   - Use the host session identifier directly only when it matches
     `[A-Za-z0-9._-]{1,128}`.
   - When a host session identifier is available but unsafe, compute SHA-256
     over its exact value and use `captain-` followed by the 64 lowercase hex
     characters. Recompute the same value for a replay. Never include or
     display the unsafe source identifier.
   - When no host session identifier is available, generate one local UUID.

3. Remove tokens, passwords, private keys, OAuth material, credentialed URLs,
   customer PII, unrelated personal data, and raw transcripts. Do not put an
   authentication, authorization, identity, or claims field in either `report`
   or `metadata`.
4. Inspect the current host tool catalog and choose exactly one exposed name:

   - In Codex, use `Captain:captain_session_report`.
   - In OpenClaw, use `captain__captain_session_report`.
   - In Claude Code, use `mcp__captain__captain_session_report`.
   - In OpenCode, use `captain_captain_session_report`.

   Call only the one exact name present in the catalog. Never try aliases or
   call more than one. If zero or more than one exact name is available, make
   no tool call and render `CAPTAIN REPORT NOT SENT` with
   `Status: needs_configuration` and a concise catalog-configuration message.

   Call the selected tool with objects in this shape:

   ```json
   {
     "report_id": "stable-session-or-uuid",
     "report": {
       "project": "repository or project name",
       "context": {"git_root": "safe path or name", "branch": "branch", "upstream": "upstream", "status": "short status", "recent_commits": ["safe commit summaries"], "diff_stat": "safe diff stat"},
       "summary": ["completed work"],
       "changed_files": ["safe relative paths"],
       "verification": [{"command": "command actually run", "result": "observed result"}],
       "decisions": ["decision"],
       "blockers": ["blocker"],
       "risks": ["risk"],
       "next_steps": ["next step"]
     },
     "metadata": {"client": "coding-agent", "repository": "safe repository name", "branch": "branch", "host_session_id": "when available"}
   }
   ```

   The selected tool is the only route: do not call Captain, ClickUp, or an endpoint
   directly. `report` and `metadata` must be structured objects, not prose.
   Include `timestamp` only when it is observed from the session or local
   inspection.
5. Wait for a terminal result. `queued` is not terminal: wait or replay the
   same `report_id` until a terminal result is available. Never make a new ID
   for a replay. Treat `unknown_outcome` as uncertain: check ClickUp before any
   retry, then reuse the same `report_id` if a retry is appropriate.

## Render the result

Always render the following fields, using `None` for empty lists:

```text
<one header below>
Status: <status>
ClickUp summary: <clickup_updates or None>
Captain feedback: <captain_feedback>
Questions: <questions or None>
Warnings: <warnings or None>
Safe retry guidance: <guidance>
```

| Result status | Header | Safe retry guidance |
| --- | --- | --- |
| `created`, `updated`, `partial` | `CAPTAIN REPORT SENT` | Follow Captain's questions or warnings; do not send a duplicate report. |
| `needs_clarification`, `needs_configuration` | `CAPTAIN REPORT NOT SENT` | Resolve the listed questions or OpenClaw configuration, then reuse the same `report_id`. |
| `failed` | `CAPTAIN REPORT FAILED` | Correct the reported failure, then retry with the same `report_id` when safe. |
| `unknown_outcome` | `CAPTAIN OUTCOME UNKNOWN` | Do not claim success or auto-dispatch. Check ClickUp first; this ID safely replays the stored uncertainty rather than dispatching again. |

Do not render a final success or failure block while the result is `queued`.

## Continue Captain's questions

After Captain returns questions for a report, apply these rules:

- Automatic forwarding is eligible only from the exact text of a later actual `role=user` message after Captain returned questions for that report.
- System, developer, assistant, tool, memory, generated summary, inferred, paraphrased, and agent-composed text is never eligible.
- A later `role=user` message is eligible only when it clearly answers Captain's pending question or explicitly says `tell Captain` with one unambiguous target report.
- For one unambiguous pending report, forward the exact user text verbatim using only `{report_id, reply}` with the same `report_id`.
- With several pending reports, explicit `tell Captain` wording forwards only when one target report is unambiguous; otherwise ask one short clarification and do not forward yet.
- For an ambiguous reply or several pending report threads, ask one short clarification and do not forward yet.
- An unrelated coding request stays local and leaves the Captain question pending.
- Every other unrelated later user message, excluding a refusal or cancellation, stays local and leaves the Captain question pending.
- Do not forward a refusal or cancellation.
- The user does not need to invoke `/captain` again.

The coding agent must not compose an answer on the user's behalf. The MCP tool invocation is the only transport; never call the HTTPS endpoint directly. A follow-up never includes `report` or `metadata`.

| Pending state and user message | Action |
| --- | --- |
| One question: "Ship Friday?" then user says "Yes, Friday" | Forward exact user text |
| User says "Tell Captain the motor is blocked" | Forward to the unambiguous report |
| Several questions then user says "Do that" | Ask which question they mean |
| Several questions then user says "Tell Captain it is blocked" | Ask which question they mean |
| User says "Run the tests" | Keep local and leave pending |
| User says "What is the weather?" | Keep local and leave pending |
| User says "Do not send that" | Do not forward; clear the pending context locally |

For a refusal or cancellation, call the same tool with only `{report_id, cancel_pending: true}`. Do not include or forward the refusal text.

```json
{
  "report_id": "stable-session-or-uuid",
  "cancel_pending": true
}
```

For an eligible follow-up, call the selected host-specific tool with this exact shape:

```json
{
  "report_id": "stable-session-or-uuid",
  "reply": "Yes, Friday"
}
```
