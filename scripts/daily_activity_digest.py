#!/usr/bin/env python3
"""Action summary reporting: one post to #dry-dock, every day, in EVERY DailyLoop
audience -- off, shadow, AND live.

This is the one deliberate exception to "`DailyLoop.audience == off` means
every daily-loop cron does nothing." Every other daily-loop cron reads the
mode and goes inert in `off` (see docs/daily-loop.md's Mode-rollout ladder).
This one does not, and that is intentional, not a bug: the whole point of
this digest is proof-of-life -- one post a day that says what Captain
actually did, so silence is never ambiguous, in every posture the team might
have DailyLoop set to (including the "we haven't flipped it on yet" state,
which is exactly when an operator most wants to know Captain is still
alive and honest about doing nothing).

**Why it is safe to be the one thing that ignores the `off` gate:** this
script is strictly read-only. It reads `data/captain-modes.json`,
`data/*state*.json`, `data/audit-log.jsonl`, and `openclaw cron list`/`cron
runs` -- the exact same sources scripts/captain_activity.py already mines --
and produces exactly one Slack message. It never writes to `data/`, never
calls scripts/clickup_write.py or any other mutating tool, and never sends a
DM to an owner, admin, or eng lead; its only possible external effect is one
channel post to `activity_digest_channel`. A read-only script that can only
ever post a summary of what already happened cannot itself cause the harm
the `off` gate exists to prevent (autonomous ClickUp writes, unreviewed
pages/DMs) -- so exempting it from that gate does not reopen the risk the
gate was built to close.

**Never LLM-written.** Legacy hosts run this as a command cron (`openclaw cron
add --command ...`). Claw installations use an isolated agent whose only job
is to invoke this script without summarizing or rewriting its output. In both
paths the report is mechanically generated from the audit log, per-cron state
files, and cron run history via scripts/captain_activity.py's collectors, so
it cannot hallucinate an action Captain didn't take.

Usage:
    python3 scripts/daily_activity_digest.py                # print-only (default, safe)
    python3 scripts/daily_activity_digest.py --hours 4       # shorter window
    python3 scripts/daily_activity_digest.py --json          # raw structured digest
    python3 scripts/daily_activity_digest.py --post          # actually send to Slack

`--post` is never the default, matching this repo's `--execute` convention
(scripts/clickup_write.py, scripts/deprecate_off_spec_crons.sh): the safe,
side-effect-free path is what runs when a human (or a cron) invokes this
script without extra flags.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import captain_activity as ca  # noqa: E402
import captain_telemetry  # noqa: E402
import slack_user_names  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("America/Detroit")

# `_comment` explains this the same way data/captain-channels.json's own
# `_comment_*` keys explain their neighbors. #dry-dock is the same channel
# captain-channels.json already uses for shadow previews.
_comment_activity_digest_channel = (
    "Fully-qualified `message` CLI target (`channel:C...` or `user:U...`) "
    "for Action summary reporting -- deliberately a SEPARATE key from "
    "shadow_recipient, because this digest must keep posting after the "
    "flip to `live` (when shadow previews stop entirely, per docs/daily-loop.md's "
    "Mode-rollout ladder). Defaults to channel:C0BKY43FWR5 (#dry-dock) when "
    "data/captain-channels.json has no activity_digest_channel key."
)
DEFAULT_ACTIVITY_DIGEST_CHANNEL = "channel:C0BKY43FWR5"  # #dry-dock

_BOUND = 240  # max chars for any free-text field folded into the digest


def _bounded(text, limit=_BOUND):
    """Shorten text to a safe report length while clearly marking the cut."""
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - len("...[truncated]")] + "...[truncated]"


# The cron job runs this script under the name below.
# Matched by NAME rather than a hardcoded cron id: ids are assigned per-host
# by openclaw (see list_captain_jobs/collect_cron_events in
# captain_activity.py, which already treat name as the stable identifier),
# so an id would differ across hosts and silently stop matching. Keeping the
# name in one constant means renaming the cron registration
# without updating this one raises the obvious way (the self-suppression
# below simply stops firing) rather than the invisible way.
_SELF_JOB_NAME = "Action summary reporting"


def _is_ok_status(status):
    """True for the one status string that means "ran and succeeded" across
    this codebase's cron-run payloads (see collect_cron_events's `status =
    run.get("status") or run.get("lastRunStatus") or "unknown"`). Anything
    else -- "error", "failed", "unknown", or any other string an unfamiliar
    future status might use -- is treated as NOT ok, i.e. worth surfacing,
    which is the conservative direction for a self-report guard."""
    return str(status or "").strip().lower() == "ok"


# General guard, not specific to the digest's own job: a *command* cron's
# `summary` is that command's raw stdout (collect_cron_events: `summary =
# run.get("summary") or run.get("output") or ""`), and a command can print
# arbitrary multi-line text -- this script's own stdout is exactly the
# digest body, which is what caused the self-quoting bug this file fixes.
# The same class of problem is not unique to this job: any future chatty
# command cron could flood a single bullet with a wall of text. Collapsing
# newlines/whitespace and capping length keeps every cron's summary to one
# short line regardless of source.
_CRON_SUMMARY_BOUND = 200


def _bounded_cron_summary(text, limit=_CRON_SUMMARY_BOUND):
    """Shorten a scheduled-job summary and remove repeated spacing and line breaks."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - len("...[truncated]")] + "...[truncated]"


# Fixed, non-stdout explanation shown for the digest's own FAILED runs only.
# A failed digest run posts nothing (main() only sends when args.post AND
# send_fn succeeds, and a crashed/erroring run never gets that far) -- so
# today's digest is the only place that failure ever becomes visible. This
# string is the entire payload for that line; no summary/stdout is embedded.
_SELF_FAILURE_NOTE = (
    "report's own run; a failed report posts nothing, so this failure is "
    "surfaced here after the fact (no stdout shown)"
)


# --------------------------------------------------------------------------
# Config / mode reads -- each degrades to a safe default rather than raising,
# matching captain_activity.py's own "a typo or a flaky host must never be
# the thing that breaks this script" posture.
# --------------------------------------------------------------------------

def read_dailyloop_audience(modes_path):
    """Return (audience, recognized). A missing file, unreadable JSON, or an
    audience value outside {"off","shadow","live"} all fail safe to "off" --
    the same fail-safe data/captain-modes.json and docs/daily-loop.md already
    document for an unrecognized audience -- with `recognized=False` so the
    digest can say plainly that the read was degraded rather than silently
    reporting an unreadable mode file as a genuine, intentional "off"."""
    try:
        data = json.loads(Path(modes_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "off", False
    if not isinstance(data, dict):
        return "off", False
    dailyloop = data.get("DailyLoop")
    audience = dailyloop.get("audience") if isinstance(dailyloop, dict) else None
    if audience not in ("off", "shadow", "live"):
        return "off", False
    return audience, True


def read_channels_config(channels_path):
    """Read Captain's Slack channel settings from JSON."""
    try:
        data = json.loads(Path(channels_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_activity_digest_channel(channels_cfg):
    """Choose the Slack channel where the activity digest should be posted."""
    target = channels_cfg.get("activity_digest_channel")
    return target if isinstance(target, str) and target else DEFAULT_ACTIVITY_DIGEST_CHANNEL


def resolve_slack_account(channels_cfg):
    """Return the configured Slack account Captain must use for the digest."""
    # Same key data/captain-channels.json's other Slack sends already read;
    # see that file's `_comment_slack_account` for why omitting it fails
    # with a misleading `channel_not_found`.
    account = channels_cfg.get("slack_account")
    return account if isinstance(account, str) and account else "captain"


# --------------------------------------------------------------------------
# openclaw `message` CLI boundary -- isolated exactly like
# captain_activity.run_openclaw, so tests never invoke a real subprocess.
# --------------------------------------------------------------------------

def _default_send_fn(openclaw_bin):
    """Best-effort mapping of the `message(action=send, channel=slack,
    account=..., target=..., message=...)` tool-call shape (used throughout
    cron-prompts/*.md by agent crons) onto an `openclaw message` CLI
    invocation. The legacy command-cron path has no agent/tool-call layer;
    the packaged isolated wrapper deliberately invokes this same deterministic
    CLI path. VERIFIED against OpenClaw 2026.7.1 on the Captain host: `send` is a
    SUBCOMMAND of `openclaw message`, not an `--action` flag, and Slack targets
    accept `<channelId|user:ID|channel:ID>`. Omitting `--account` sends as the
    default `AgentOwen` app, which is not a member of #dry-dock and fails with a
    misleading `channel_not_found` -- see the `slack_account` note in
    data/captain-channels.json. Superseded note about an unverified `openclaw
    message --help` on the host -- do that before the first real --post run,
    the same way TOOLS.md's ClickUp/Sentry sections were verified against
    the real host before being trusted."""

    def fn(text, target, account, timeout=30):
        """Ask OpenClaw to send one digest message to Slack."""
        try:
            result = subprocess.run(
                [openclaw_bin, "message", "send", "--channel", "slack",
                 "--account", account, "--target", target, "--message", text],
                capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            return False, "openclaw binary not found (%s)" % openclaw_bin
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        if result.returncode != 0:
            return False, "exit %s: %s" % (
                result.returncode, (result.stderr or "").strip()[:300]
            )
        return True, None

    return fn


# --------------------------------------------------------------------------
# Source attribution -- BUG 1 fix. Audited actions (clickup_* writes, and
# sends recorded in the audit log) are REAL, full stop, in every
# DailyLoop audience: `clickup_write.py` refuses to execute at all in
# `shadow` (no ClickUp mutation, no clickup_* audit row -- see its
# `shadow_write_block_message`), so a clickup_* row reaching the audit log
# can only ever mean a real write happened. There is no such thing as a
# previewed audit row. What DOES vary by audience is which Captain *path*
# produced the row: the five daily-loop crons (daily-morning-cycle,
# daily-blocker-chase, daily-bench-truth-watch, daily-eod-wrap,
# standup-transcript-clickup-reconciliation) go inert (or preview-only) per
# the Mode-rollout ladder, while legacy check-in crons and Slack-driven
# intake are not mode-gated and act for real regardless of audience.
#
# None of the five daily-loop cron-prompts (see cron-prompts/*.md) ever pass
# an explicit `--source` to `scripts/clickup_write.py` -- every one of their
# create-task/update-task/comment-task invocations relies on that script's
# own `--source` default, the literal string "captain" (see
# `operation_from_args`/the `create`/`update`/`comment` subparsers in
# scripts/clickup_write.py). So `source == "captain"` is the one signal we
# have, straight from the data, that a clickup_* (or `*_sent`) audit row
# came from the daily loop rather than being guessed. Any other explicit
# source string (`weekly_slack_clickup_status*`, `captain_session_report`,
# an ad-hoc "<name> Slack DM ..." string, etc.) is a legacy/Slack-driven
# path. A missing or non-string `source` cannot be attributed either way --
# it is counted plainly, per the fix spec, rather than forced into a
# bucket.
_DAILY_LOOP_CLICKUP_SOURCE = "captain"


def _attribute_source(source):
    """Classify one audited action's `source` field as "daily_loop",
    "other" (legacy check-in crons / Slack-driven intake), or
    "unattributed" (missing/blank/non-string -- counted plainly, not
    guessed). See the module comment above for why `source == "captain"` is
    the daily-loop signal."""
    if not isinstance(source, str) or not source.strip():
        return "unattributed"
    return "daily_loop" if source == _DAILY_LOOP_CLICKUP_SOURCE else "other"


def _count_by_attribution(events):
    """Count activity events by the Captain source that produced them."""
    counts = {"daily_loop": 0, "other": 0, "unattributed": 0}
    for e in events:
        counts[_attribute_source(e.raw.get("source"))] += 1
    return counts


# --------------------------------------------------------------------------
# BUG 2 fix -- only a cron that was genuinely due to run inside the window
# and produced no run event is reported MISSING. A cron whose schedule
# simply never made it due in this window (a Friday-only job checked on a
# Tuesday) must produce no line at all -- see `build_digest`'s use of this
# below.
# --------------------------------------------------------------------------

def _cron_due_but_absent(job, now):
    """True only if a registered cron with zero runs in the window was
    actually due to have fired by `now`.

    `nextRunAtMs` is the scheduler's own record (from `openclaw cron list
    --json`) of this job's next scheduled fire time -- openclaw already
    computes it from the job's cron expression plus its last run, so this
    deliberately does not re-derive a schedule from `lastRunAtMs` itself
    (that would need a cron-expression parser, which this fix is scoped to
    avoid, and would just duplicate a computation openclaw already did).
    If that next-run time is still in the future, the job was simply not
    due yet in this window -- silence is the correct, non-alarming answer,
    not a MISSING line (this is the Friday/Monday-job-on-a-Tuesday case).
    If it is at or before `now`, the scheduler expected a run that never
    showed up in the audit window: a genuine miss.

    If `nextRunAtMs` is absent, non-numeric, or otherwise unparseable, this
    case is genuinely undecidable from the data available here. Per the fix
    spec, silence is preferred over a false alarm, so this returns False
    (not reported missing) rather than guessing at a schedule we cannot see.
    """
    next_run = ca._parse_ts(job.get("nextRunAtMs"))
    if next_run is None:
        return False
    return next_run <= now


# --------------------------------------------------------------------------
# Digest construction -- reuses captain_activity's collectors (via
# build_report and list_captain_jobs) rather than re-mining any of CRON /
# DECIDED / STATE / ACTED itself.
# --------------------------------------------------------------------------

def build_digest(hours, root=None, cron_list_fn=None, cron_runs_fn=None, now=None,
                 modes_path=None, channels_cfg=None):
    """Build a summary of Captain's recent work and missing scheduled runs."""
    root = root or ROOT
    events, warnings, now = ca.build_report(
        hours, root=root, cron_list_fn=cron_list_fn, cron_runs_fn=cron_runs_fn, now=now,
    )
    captain_jobs, job_warnings = ca.list_captain_jobs(cron_list_fn)
    # Deduplicate: build_report's own collect_cron_events already calls
    # list_captain_jobs once internally, so a listing failure would
    # otherwise be warned about twice for the one underlying problem.
    warnings = list(dict.fromkeys(list(warnings) + list(job_warnings)))

    audience, recognized = read_dailyloop_audience(
        modes_path or (root / "data" / "captain-modes.json")
    )

    cron_events = [e for e in events if e.kind == "CRON"]
    decided_events = [e for e in events if e.kind == "DECIDED"]
    state_events = [e for e in events if e.kind == "STATE"]
    acted_events = [e for e in events if e.kind == "ACTED"]

    ran_names = {e.raw["name"] for e in cron_events}
    # BUG 2 fix: a registered-but-absent cron is only reported MISSING if it
    # was genuinely due to have fired by `now` -- see _cron_due_but_absent.
    # A cron that simply wasn't due yet (e.g. a Friday-only job checked on a
    # Tuesday) produces no line at all, per the "noise floor" requirement.
    missing_names = set()
    for job in captain_jobs:
        name = str(job.get("name") or job.get("id") or "unknown-job")
        if name in ran_names:
            continue
        if _cron_due_but_absent(job, now):
            missing_names.add(name)
    missing = sorted(missing_names)

    clickup_events = [
        e for e in acted_events if str(e.raw.get("event", "")).startswith("clickup_")
    ]
    sent_events = [
        e for e in acted_events if str(e.raw.get("event", "")).endswith("_sent")
    ]
    clickup_writes = len(clickup_events)
    messages_sent = len(sent_events)
    # BUG 1 fix: every clickup_* (and *_sent) audit row is a REAL action in
    # every audience -- see the module comment above _DAILY_LOOP_CLICKUP_SOURCE.
    # What we CAN say from the data is which Captain path produced it.
    clickup_writes_by_source = _count_by_attribution(clickup_events)
    messages_sent_by_source = _count_by_attribution(sent_events)
    blockers_opened = sum(1 for e in acted_events if e.raw.get("event") == "blocker_added")
    blockers_cleared = sum(
        1 for e in acted_events
        if e.raw.get("event") == "blocker_updated" and e.raw.get("status") == "cleared"
    )
    blockers_escalated = sum(
        1 for e in acted_events
        if e.raw.get("event") == "blocker_updated" and e.raw.get("status") == "escalated"
    )
    # Self-report fix: the digest's own successful runs must not inflate
    # "Cron runs by status" with a daily `ok` -- that is just this script
    # confirming it ran, every single day, and is not a signal about
    # anything else Captain did. A failed own-run still counts: that IS a
    # real signal (see _SELF_FAILURE_NOTE), the same reason it still gets a
    # line below rather than being dropped outright.
    counted_cron_events = [
        e for e in cron_events
        if not (e.raw["name"] == _SELF_JOB_NAME and _is_ok_status(e.raw.get("status")))
    ]
    cron_status_counts = dict(
        Counter(str(e.raw.get("status", "unknown")) for e in counted_cron_events)
    )

    crons = []
    for e in sorted(cron_events, key=lambda e: e[0]):
        name = e.raw["name"]
        status = e.raw["status"]
        is_self = name == _SELF_JOB_NAME
        if is_self and _is_ok_status(status):
            # Suppress the digest's own successful runs from the Crons
            # section entirely: "the digest ran" is self-evident (you are
            # reading it) and would otherwise add a noise line every day.
            continue
        if is_self:
            # Own FAILED run: status only, no stdout -- never the digest's
            # own summary text (that stdout IS the digest body; embedding it
            # is exactly the self-quoting bug this fix eliminates).
            summary = ""
        else:
            summary = _bounded_cron_summary(e.raw.get("summary"))
        crons.append({
            "name": name, "status": status, "summary": summary,
            "is_self": is_self, "ts": e[0].isoformat(),
        })
    decisions = [
        {
            "name": e.raw["name"], "action": e.raw["action"], "audience": e.raw["audience"],
            "reason": _bounded(e.raw.get("reason")), "ts": e[0].isoformat(),
        }
        for e in sorted(decided_events, key=lambda e: e[0])
    ]
    degraded = [
        {"name": e.raw["name"], "flags": list(e.raw["flags"]), "ts": e[0].isoformat()}
        for e in sorted(state_events, key=lambda e: e[0])
        if e.raw.get("flags")
    ]

    nothing_happened = not (cron_events or decided_events or acted_events or degraded)

    return {
        "date": now.astimezone(TZ).date().isoformat(),
        "hours": hours,
        "generated_at": now.isoformat(),
        "audience": audience,
        "audience_recognized": recognized,
        "counts": {
            "clickup_writes": clickup_writes,
            "clickup_writes_by_source": clickup_writes_by_source,
            "messages_sent": messages_sent,
            "messages_sent_by_source": messages_sent_by_source,
            "blockers_opened": blockers_opened,
            "blockers_cleared": blockers_cleared,
            "blockers_escalated": blockers_escalated,
            "cron_status_counts": cron_status_counts,
        },
        "crons": crons,
        "missing_crons": missing,
        "decisions": decisions,
        "degraded": degraded,
        "warnings": warnings,
        "nothing_happened": nothing_happened,
    }


# These describe what `DailyLoop.audience` actually governs -- the FIVE
# daily-loop crons only (daily-morning-cycle, daily-blocker-chase,
# daily-bench-truth-watch, daily-eod-wrap, standup-transcript-reconciliation)
# -- never a blanket claim about every action in the counts below. Legacy
# check-in crons and Slack-driven intake are not mode-gated and keep acting
# for real regardless of this setting; that is why nothing in the *Counts*
# section is ever labeled a preview (see BUG 1 fix / _attribute_source).
_AUDIENCE_HEADER = {
    "live": "*LIVE* — the daily-loop crons act for real, same as every other "
            "Captain path.",
    "shadow": "*SHADOW* — the daily-loop crons take no real action; what "
              "they would have done is previewed to the shadow channel "
              "instead. Other Captain paths (legacy check-in crons, "
              "Slack-driven intake) are NOT mode-gated and continue to act "
              "for real -- the counts below are never previews, in any "
              "audience.",
    "off": "*OFF* — the daily-loop crons do nothing at all. Other Captain "
           "paths (legacy check-in crons, Slack-driven intake) continue to "
           "act for real; this digest is the one exception that always "
           "posts.",
}


def _attribution_suffix(by_source):
    """Render a `(daily-loop: N, other Captain paths: N[, unattributed: N])`
    suffix for a count, or "" when there is nothing to attribute (count is
    zero across the board). `unattributed` is only shown when non-zero --
    most days it will be 0, since every daily-loop clickup_* row uses the
    `source == "captain"` signal and every other explicit source string is
    attributable to "other"."""
    total = sum(by_source.values())
    if total == 0:
        return ""
    parts = [
        "daily-loop: %d" % by_source["daily_loop"],
        "other Captain paths: %d" % by_source["other"],
    ]
    if by_source["unattributed"]:
        parts.append("unattributed: %d" % by_source["unattributed"])
    return " (%s)" % ", ".join(parts)


def render_mrkdwn(digest):
    """Compact Slack mrkdwn. No raw JSON, no secrets, no employee message
    bodies -- evidence *sources* (task ids, cron/state file names, event
    names) are fine; message contents are not carried here at all."""
    hours = digest["hours"]
    audience = digest["audience"]
    lines = [
        "*Action summary reporting — %s*" % digest["date"],
        "DailyLoop audience: %s" % _AUDIENCE_HEADER.get(audience, "*%s*" % audience.upper()),
    ]
    if not digest["audience_recognized"]:
        lines.append(
            "_Note: data/captain-modes.json's DailyLoop.audience was missing, "
            "unreadable, or unrecognized -- treated as OFF (fail-safe)._"
        )
    lines.append("")

    counts = digest["counts"]
    lines.append("*Counts (last %dh):*" % hours)
    lines.append(
        "• ClickUp writes: %d%s" % (
            counts["clickup_writes"], _attribution_suffix(counts["clickup_writes_by_source"])
        )
    )
    lines.append(
        "• Messages/pages sent: %d%s" % (
            counts["messages_sent"], _attribution_suffix(counts["messages_sent_by_source"])
        )
    )
    lines.append(
        "• Blockers: opened %d, cleared %d, escalated %d" % (
            counts["blockers_opened"], counts["blockers_cleared"], counts["blockers_escalated"],
        )
    )
    if counts["cron_status_counts"]:
        status_str = ", ".join(
            "%s=%d" % (k, v) for k, v in sorted(counts["cron_status_counts"].items())
        )
    else:
        status_str = "none"
    lines.append("• Cron runs by status: %s" % status_str)
    lines.append("")

    lines.append("*Crons (last %dh):*" % hours)
    if not (digest["crons"] or digest["decisions"] or digest["missing_crons"]):
        lines.append("• No cron runs or decisions recorded.")
    else:
        for c in digest["crons"]:
            if c.get("is_self"):
                # Own failed run: status + fixed explanatory note only --
                # never this job's stdout (see _SELF_FAILURE_NOTE).
                lines.append(
                    "• %s: ran, status=%s — %s" % (c["name"], c["status"], _SELF_FAILURE_NOTE)
                )
                continue
            summary_part = " — %s" % c["summary"] if c["summary"] else ""
            lines.append("• %s: ran, status=%s%s" % (c["name"], c["status"], summary_part))
        for d in digest["decisions"]:
            reason_part = " — %s" % d["reason"] if d["reason"] else ""
            lines.append(
                "• %s: %s (audience=%s)%s" % (d["name"], d["action"], d["audience"], reason_part)
            )
        for name in digest["missing_crons"]:
            lines.append(
                "• %s: *MISSING* — was due to run in the last %dh and no run was found"
                % (name, hours)
            )
    lines.append("")

    lines.append("*Degraded conditions:*")
    if digest["degraded"]:
        for item in digest["degraded"]:
            lines.append("• %s: %s" % (item["name"], ", ".join(item["flags"])))
    else:
        lines.append("• None detected.")

    if digest["nothing_happened"]:
        lines.append("")
        lines.append(
            "_Nothing happened in the last %dh — no cron runs, decisions, or "
            "audited actions. This line is the point: silence is exactly what "
            "this digest exists to eliminate._" % hours
        )

    if digest["warnings"]:
        lines.append("")
        lines.append(
            "_Data-collection warnings: %d (run with --json for detail)._"
            % len(digest["warnings"])
        )

    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_arg_parser():
    """Define the command-line options accepted by the digest tool."""
    ap = argparse.ArgumentParser(
        description=(
            "Mechanically-generated summary of what Captain did in the last "
            "N hours, posted to #dry-dock. Runs in EVERY DailyLoop audience "
            "-- off, shadow, live -- unlike every other daily-loop cron; see "
            "the module docstring for why that exception is safe."
        )
    )
    ap.add_argument(
        "--hours", default=str(ca.DEFAULT_HOURS),
        help="Lookback window in hours (default: %d)" % ca.DEFAULT_HOURS,
    )
    ap.add_argument(
        "--json", action="store_true",
        help="Print the raw structured digest as JSON instead of Slack mrkdwn.",
    )
    ap.add_argument(
        "--post", action="store_true",
        help="Actually send the digest via the OpenClaw message CLI. Without "
             "this flag (the default), the digest is only printed -- nothing "
             "is sent. Matches this repo's --execute convention.",
    )
    return ap


def main(argv=None, root=None, cron_list_fn=None, cron_runs_fn=None, send_fn=None,
         now=None, stdout=None, stderr=None):
    """Print or send Captain's recent-activity digest based on command-line choices."""
    argv = sys.argv[1:] if argv is None else list(argv)
    root = root or ROOT
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    parser = build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # A bad flag (not a bad --hours value, which we validate ourselves
        # below) hits argparse's own SystemExit -- let that clean exit
        # through rather than converting it into a traceback.
        return exc.code if isinstance(exc.code, int) else 2

    try:
        hours = ca._parse_hours_argument([args.hours])
    except ca.BadHoursArgument as exc:
        # A typo'd --hours is a user-input error, not a Captain incident --
        # convert it to a clean non-zero exit here, inside main(), so it
        # never reaches captain_telemetry.guard's exception-reporting path
        # and pages anyone over a typo.
        print("daily_activity_digest: %s" % exc, file=stderr)
        return 2

    openclaw_bin = os.environ.get("OPENCLAW_BIN", "openclaw")
    cron_list_fn = cron_list_fn or ca._default_cron_list_fn(openclaw_bin)
    cron_runs_fn = cron_runs_fn or ca._default_cron_runs_fn(openclaw_bin)

    channels_cfg = read_channels_config(root / "data" / "captain-channels.json")
    digest = build_digest(
        hours, root=root, cron_list_fn=cron_list_fn, cron_runs_fn=cron_runs_fn, now=now,
        modes_path=root / "data" / "captain-modes.json", channels_cfg=channels_cfg,
    )

    text = render_mrkdwn(digest)
    # Name-rendering pass: any Slack-user-id-shaped token in the rendered
    # Slack text (a cron's own stdout summary, a decision's `reason`, etc.)
    # is rendered as `Name (Uxxxxxxxx)` when resolvable offline (admin
    # recipients, then data/slack-user-cache.json), else left as the bare id
    # -- never fabricated. See scripts/slack_user_names.py and
    # docs/daily-loop.md's Slack name-rendering convention. Deliberately NOT
    # applied to --json below: that raw structured payload is for debugging
    # and should reflect the underlying data unmodified.
    resolver = slack_user_names.SlackNameResolver(channels_cfg=channels_cfg, root=root)
    text = resolver.scrub_text(text)
    if args.json:
        print(json.dumps(digest, indent=2, sort_keys=True), file=stdout)
    else:
        print(text, file=stdout)

    if args.post:
        send_fn = send_fn or _default_send_fn(openclaw_bin)
        target = resolve_activity_digest_channel(channels_cfg)
        account = resolve_slack_account(channels_cfg)
        ok, err = send_fn(text, target, account)
        if not ok:
            print("daily_activity_digest: send failed: %s" % err, file=stderr)
            return 1
    return 0


if __name__ == "__main__":
    with captain_telemetry.guard("daily_activity_digest"):
        raise SystemExit(main())
