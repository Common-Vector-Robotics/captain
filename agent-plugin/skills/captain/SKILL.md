---
name: captain
description: Use when the user asks to report completed coding-agent work to their local Captain project manager.
---

# Report coding work to Captain

Use this workflow once the user asks to report completed coding-agent work.

1. Gather the Git root, branch and upstream, short status, recent commits, diff
   stats, completed work, changed files, verification actually run, decisions,
   blockers, risks, and next steps. Do this once; keep the result concise.
2. Make one stable `report_id`: use the host session identifier when available;
   otherwise generate a local UUID. Reuse that exact ID for every replay.
3. Remove tokens, passwords, private keys, OAuth material, credentialed URLs,
   customer PII, unrelated personal data, and raw transcripts. Do not put an
   email, identity claim, authorization claim, or credential in `metadata`.
4. Immediately call the only reporting tool,
   `Captain:captain_session_report`, with objects in this shape:

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
     "metadata": {"client": "coding-agent", "repository": "safe repository name", "branch": "branch", "timestamp": "ISO-8601 timestamp", "host_session_id": "when available"}
   }
   ```

   The tool is the only route: do not call Captain, ClickUp, or an endpoint
   directly. `report` and `metadata` must be structured objects, not prose.
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
| `needs_clarification`, `needs_configuration` | `CAPTAIN REPORT NOT SENT` | Resolve the listed questions or local configuration, then reuse the same `report_id`. |
| `failed` | `CAPTAIN REPORT FAILED` | Correct the reported failure, then retry with the same `report_id` when safe. |
| `unknown_outcome` | `CAPTAIN OUTCOME UNKNOWN` | Do not claim success. Check ClickUp before retrying; if retrying, reuse the same `report_id`. |

Do not render a final success or failure block while the result is `queued`.
