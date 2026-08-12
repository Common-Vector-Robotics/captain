#!/usr/bin/env python3
"""Generate Captain's mechanically sourced daily activity digest.

The scheduled digest is a deliberate exception to the normal DailyLoop mode
gate: it reports in ``off``, ``shadow``, and ``live`` so silence never leaves
operators wondering whether Captain ran or simply had nothing to report.

That exception does not weaken the ``off`` safety boundary. This script reads
mode and channel configuration directly, then uses ``captain_activity`` for
state, audit-log, and OpenClaw cron evidence. It never writes ``data/``, mutates
ClickUp, or sends DMs. Its only possible external effect is one post to
``activity_digest_channel``, and that send still requires ``--post``.

The message is never authored or rewritten by an LLM. Legacy command crons and
the packaged isolated agent both invoke this script directly; the report is
assembled mechanically from recorded evidence, so it cannot invent activity.

Usage:
    python3 scripts/daily_activity_digest.py                # print-only (default, safe)
    python3 scripts/daily_activity_digest.py --hours 4       # shorter window
    python3 scripts/daily_activity_digest.py --json          # raw structured digest
    python3 scripts/daily_activity_digest.py --post          # actually send to Slack

``--post`` follows the repository's ``--execute`` convention: a plain
invocation is side-effect free.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

# Allow direct script execution to import neighboring Captain helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import captain_activity as ca  # noqa: E402
import captain_telemetry  # noqa: E402
import slack_user_names  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("America/Detroit")

# Keep this explanation beside the matching setting, following the
# ``_comment_*`` convention used in ``data/captain-channels.json``. The digest
# needs its own target because it continues after shadow previews stop in live.
_comment_activity_digest_channel = (
    "Fully-qualified `message` CLI target (`channel:C...` or `user:U...`) "
    "for Action summary reporting -- deliberately a SEPARATE key from "
    "shadow_recipient, because this digest must keep posting after the "
    "flip to `live` (when shadow previews stop entirely, per README.md's "
    "shadow-mode rollout). Defaults to channel:C0BKY43FWR5 (#dry-dock) when "
    "data/captain-channels.json has no activity_digest_channel key."
)

# Default to #dry-dock when the channel config does not override the target.
DEFAULT_ACTIVITY_DIGEST_CHANNEL = "channel:C0BKY43FWR5"

# Bound every free-text field folded into the digest.
_BOUND = 240


def _bounded(text, limit=_BOUND):
    """Return text no longer than ``limit``, marking any truncation."""
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - len("...[truncated]")] + "...[truncated]"


# OpenClaw assigns cron ids per host, so the registered job name is the stable
# identifier used to suppress the digest's own successful run from its report.
_SELF_JOB_NAME = "Action summary reporting"


def _is_ok_status(status):
    """Return whether a cron status unambiguously means success.

    Unknown or future status values remain visible as non-successes. This is
    the conservative behavior for a self-reporting guard.
    """
    return str(status or "").strip().lower() == "ok"


# A command cron's summary may be arbitrary multi-line stdout. Normalize every
# summary, not only this job's, so one chatty command cannot flood a digest line.
_CRON_SUMMARY_BOUND = 200


def _bounded_cron_summary(text, limit=_CRON_SUMMARY_BOUND):
    """Shorten a scheduled-job summary and collapse repeated whitespace.

    Example input: ``"first line\n  second line"``
    Example output: ``"first line second line"``
    """
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - len("...[truncated]")] + "...[truncated]"


# A failed digest posts nothing, so the next digest surfaces that failure with
# this fixed note. Never embed the failed job's stdout, which may be a digest.
_SELF_FAILURE_NOTE = (
    "report's own run; a failed report posts nothing, so this failure is "
    "surfaced here after the fact (no stdout shown)"
)


# Configuration and mode helpers

def read_dailyloop_audience(modes_path):
    """Return ``(audience, recognized)`` from Captain's mode file.

    Missing, unreadable, malformed, or unknown values fail safe to
    ``("off", False)``. The flag lets the digest distinguish that degraded
    fallback from an intentional ``off`` setting.
    """
    # A mode-file problem must not prevent the proof-of-life report.
    try:
        data = json.loads(Path(modes_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "off", False

    # The top-level settings document must be a mapping.
    if not isinstance(data, dict):
        return "off", False

    # Recognize only the three audiences documented by the rollout ladder.
    dailyloop = data.get("DailyLoop")
    audience = dailyloop.get("audience") if isinstance(dailyloop, dict) else None
    if audience not in ("off", "shadow", "live"):
        return "off", False

    return audience, True


def read_channels_config(channels_path):
    """Return Captain's Slack channel settings, or an empty mapping."""
    # Missing or invalid optional config falls back to the defaults below.
    try:
        data = json.loads(Path(channels_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}

    return data if isinstance(data, dict) else {}


def resolve_activity_digest_channel(channels_cfg):
    """Return the configured digest target or the #dry-dock default."""
    target = channels_cfg.get("activity_digest_channel")
    return target if isinstance(target, str) and target else DEFAULT_ACTIVITY_DIGEST_CHANNEL


def resolve_slack_account(channels_cfg):
    """Return the Slack account Captain must use for the digest.

    Omitting this account can send as the default AgentOwen app, which is not a
    member of #dry-dock and fails with a misleading ``channel_not_found``.
    """
    account = channels_cfg.get("slack_account")
    return account if isinstance(account, str) and account else "captain"


# OpenClaw message boundary

def _default_send_fn(openclaw_bin):
    """Build the isolated function that sends one digest through OpenClaw.

    This deterministic CLI boundary mirrors the agent tool-call shape and can
    be replaced in tests. OpenClaw 2026.7.1 uses ``message send`` as a
    subcommand and accepts channel or user targets. ``--account`` is required
    to avoid silently using the wrong Slack app.
    """

    def fn(text, target, account, timeout=30):
        """Send one message and return ``(succeeded, error_message)``."""
        # Run one bounded OpenClaw process with all Slack routing explicit.
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

        # Preserve enough stderr to diagnose a send without flooding the caller.
        if result.returncode != 0:
            return False, "exit %s: %s" % (
                result.returncode, (result.stderr or "").strip()[:300]
            )

        return True, None

    return fn


# Source attribution

# Every ``clickup_*`` or ``*_sent`` row records an attempted or completed
# operation outside preview-only behavior. A ``clickup_*`` prefix can represent
# a write, validation, verification read, or failed attempt, so each row's event
# name and ``ok`` fields remain authoritative for its outcome. Shadow previews
# do not create these audit rows.
#
# Daily-loop prompts rely on ``clickup_write.py``'s default source, ``captain``.
# Other explicit sources identify legacy or Slack-driven paths. Missing or
# non-string sources remain unattributed instead of being guessed.
_DAILY_LOOP_CLICKUP_SOURCE = "captain"


def _attribute_source(source):
    """Classify one audit source as daily-loop, other, or unattributed.

    Example input: ``"captain"``
    Example output: ``"daily_loop"``
    """
    # Never infer provenance from missing or malformed evidence.
    if not isinstance(source, str) or not source.strip():
        return "unattributed"

    return "daily_loop" if source == _DAILY_LOOP_CLICKUP_SOURCE else "other"


def _count_by_attribution(events):
    """Count activity events by the Captain source that produced them."""
    counts = {"daily_loop": 0, "other": 0, "unattributed": 0}

    # Preserve an explicit bucket for every event instead of dropping ambiguity.
    for e in events:
        counts[_attribute_source(e.raw.get("source"))] += 1

    return counts


# Missing-run detection

def _cron_due_but_absent(job, now):
    """Return whether a cron with no recorded run was already due.

    ``nextRunAtMs`` comes from OpenClaw's own schedule calculation, so Captain
    does not reimplement cron parsing. A future time means the job was not due.
    A missing or invalid time is undecidable and returns ``False``; silence is
    safer than a false missing-run alarm.
    """
    # Trust the scheduler's next-run timestamp as the schedule evidence.
    next_run = ca._parse_ts(job.get("nextRunAtMs"))
    if next_run is None:
        return False

    return next_run <= now


# Digest construction

def build_digest(hours, root=None, cron_list_fn=None, cron_runs_fn=None, now=None,
                 modes_path=None, channels_cfg=None):
    """Return a JSON-ready summary of recent activity and missing cron runs.

    The optional root, clock, and cron functions are injection points for tests
    and alternate hosts. Event collection remains centralized in
    ``captain_activity`` rather than being reimplemented here.
    """
    # Resolve the workspace and collect every report event in the time window.
    root = root or ROOT
    events, warnings, now = ca.build_report(
        hours, root=root, cron_list_fn=cron_list_fn, cron_runs_fn=cron_runs_fn, now=now,
    )

    # Read registered jobs separately so absent-but-due jobs can be detected.
    captain_jobs, job_warnings = ca.list_captain_jobs(cron_list_fn)

    # ``build_report`` also lists jobs internally. Deduplicate the same listing
    # failure so one underlying problem produces one warning.
    warnings = list(dict.fromkeys(list(warnings) + list(job_warnings)))

    # Read the current mode separately because it is part of the rendered proof.
    audience, recognized = read_dailyloop_audience(
        modes_path or (root / "data" / "captain-modes.json")
    )

    # Partition the shared event stream into the digest's four evidence types.
    cron_events = [e for e in events if e.kind == "CRON"]
    decided_events = [e for e in events if e.kind == "DECIDED"]
    state_events = [e for e in events if e.kind == "STATE"]
    acted_events = [e for e in events if e.kind == "ACTED"]

    # Report only registered jobs that were due and have no run in the window.
    ran_names = {e.raw["name"] for e in cron_events}
    missing_names = set()
    for job in captain_jobs:
        name = str(job.get("name") or job.get("id") or "unknown-job")
        if name in ran_names:
            continue

        if _cron_due_but_absent(job, now):
            missing_names.add(name)

    missing = sorted(missing_names)

    # Separate ClickUp-related audit events and outbound sends from the stream.
    clickup_events = [
        e for e in acted_events if str(e.raw.get("event", "")).startswith("clickup_")
    ]
    sent_events = [
        e for e in acted_events if str(e.raw.get("event", "")).endswith("_sent")
    ]
    # Keep the established ``clickup_writes`` output key, although its count
    # includes every ``clickup_*`` audit event described above.
    clickup_writes = len(clickup_events)
    messages_sent = len(sent_events)

    # Source attribution identifies which Captain path produced each audit event.
    clickup_writes_by_source = _count_by_attribution(clickup_events)
    messages_sent_by_source = _count_by_attribution(sent_events)

    # Summarize blocker lifecycle changes recorded during the same window.
    blockers_opened = sum(1 for e in acted_events if e.raw.get("event") == "blocker_added")
    blockers_cleared = sum(
        1 for e in acted_events
        if e.raw.get("event") == "blocker_updated" and e.raw.get("status") == "cleared"
    )
    blockers_escalated = sum(
        1 for e in acted_events
        if e.raw.get("event") == "blocker_updated" and e.raw.get("status") == "escalated"
    )

    # A successful self-run is self-evident and would add daily noise. Failed
    # self-runs remain counted because they are operationally meaningful.
    counted_cron_events = [
        e for e in cron_events
        if not (e.raw["name"] == _SELF_JOB_NAME and _is_ok_status(e.raw.get("status")))
    ]
    cron_status_counts = dict(
        Counter(str(e.raw.get("status", "unknown")) for e in counted_cron_events)
    )

    # Build bounded cron details while preventing the digest from quoting itself.
    crons = []
    for e in sorted(cron_events, key=lambda e: e[0]):
        name = e.raw["name"]
        status = e.raw["status"]
        is_self = name == _SELF_JOB_NAME

        if is_self and _is_ok_status(status):
            # Reading the digest already proves this successful run happened.
            continue

        if is_self:
            # Keep failed self-runs, but never include their digest-shaped stdout.
            summary = ""
        else:
            summary = _bounded_cron_summary(e.raw.get("summary"))

        crons.append({
            "name": name, "status": status, "summary": summary,
            "is_self": is_self, "ts": e[0].isoformat(),
        })

    # Convert decisions and degraded state into bounded, JSON-ready records.
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

    # Make genuine inactivity explicit instead of leaving an empty report vague.
    nothing_happened = not (cron_events or decided_events or acted_events or degraded)

    # Return the stable structure consumed by both JSON and Slack renderers.
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


# DailyLoop audience controls only the five daily-loop crons. Legacy check-ins
# and Slack-driven intake keep acting for real in every mode, so the counts
# below are never labeled as previews.
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
    """Render the source breakdown appended to one action count.

    Zero totals produce no suffix. The unattributed bucket appears only when
    evidence could not identify an action's path.
    """
    total = sum(by_source.values())
    if total == 0:
        return ""

    # Always show the two known paths when at least one action exists.
    parts = [
        "daily-loop: %d" % by_source["daily_loop"],
        "other Captain paths: %d" % by_source["other"],
    ]

    # Keep the common zero-value ambiguity bucket out of routine reports.
    if by_source["unattributed"]:
        parts.append("unattributed: %d" % by_source["unattributed"])

    return " (%s)" % ", ".join(parts)


def render_mrkdwn(digest):
    """Render one compact Slack mrkdwn message from a structured digest.

    The message includes aggregate audit counts, cron and decision names,
    degraded-state flags, and bounded cron-summary or decision-reason text. It
    does not render raw JSON or scrub arbitrary source text, so upstream evidence
    must not place secrets or private message bodies in those summary fields.
    """
    # Start with the report identity and exact DailyLoop posture.
    hours = digest["hours"]
    audience = digest["audience"]
    lines = [
        "*Action summary reporting — %s*" % digest["date"],
        "DailyLoop audience: %s" % _AUDIENCE_HEADER.get(audience, "*%s*" % audience.upper()),
    ]

    # Make a fail-safe mode fallback visible instead of presenting it as intent.
    if not digest["audience_recognized"]:
        lines.append(
            "_Note: data/captain-modes.json's DailyLoop.audience was missing, "
            "unreadable, or unrecognized -- treated as OFF (fail-safe)._"
        )
    lines.append("")

    # Summarize audited events and cron outcomes in the lookback window.
    counts = digest["counts"]
    lines.append("*Counts (last %dh):*" % hours)
    # Preserve the established user-facing label for compatibility. Its value is
    # every ``clickup_*`` audit event, not only confirmed successful writes.
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

    # Render an explicit ``none`` when no cron statuses were counted.
    if counts["cron_status_counts"]:
        status_str = ", ".join(
            "%s=%d" % (k, v) for k, v in sorted(counts["cron_status_counts"].items())
        )
    else:
        status_str = "none"
    lines.append("• Cron runs by status: %s" % status_str)
    lines.append("")

    # List bounded source summaries, decisions, and genuinely missing runs.
    lines.append("*Crons (last %dh):*" % hours)
    if not (digest["crons"] or digest["decisions"] or digest["missing_crons"]):
        lines.append("• No cron runs or decisions recorded.")
    else:
        for c in digest["crons"]:
            if c.get("is_self"):
                # A failed self-run uses the fixed note, never its stdout.
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

    # Surface state degradation separately from run status.
    lines.append("*Degraded conditions:*")
    if digest["degraded"]:
        for item in digest["degraded"]:
            lines.append("• %s: %s" % (item["name"], ", ".join(item["flags"])))
    else:
        lines.append("• None detected.")

    # Explicitly explain a truly empty evidence window.
    if digest["nothing_happened"]:
        lines.append("")
        lines.append(
            "_Nothing happened in the last %dh — no cron runs, decisions, or "
            "audited actions. This line is the point: silence is exactly what "
            "this digest exists to eliminate._" % hours
        )

    # Keep warning details in JSON while making their existence visible here.
    if digest["warnings"]:
        lines.append("")
        lines.append(
            "_Data-collection warnings: %d (run with --json for detail)._"
            % len(digest["warnings"])
        )

    return "\n".join(lines)


# Command-line interface

def build_arg_parser():
    """Return the parser for digest lookback, output, and send options."""
    # Describe the unusual all-audience behavior where users first discover it.
    ap = argparse.ArgumentParser(
        description=(
            "Mechanically-generated summary of what Captain did in the last "
            "N hours, posted to #dry-dock. Runs in EVERY DailyLoop audience "
            "-- off, shadow, live -- unlike every other daily-loop cron; see "
            "the module docstring for why that exception is safe."
        )
    )

    # The shared type keeps this window's accepted values identical to
    # captain_activity's, and turns a bad value into an argparse usage error
    # rather than an exception that telemetry would report as an incident.
    ap.add_argument(
        "--hours", type=ca.positive_hours, default=ca.DEFAULT_HOURS,
        help="Lookback window in hours (default: %d)" % ca.DEFAULT_HOURS,
    )

    # JSON exposes collection details; default output is Slack-ready mrkdwn.
    ap.add_argument(
        "--json", action="store_true",
        help="Print the raw structured digest as JSON instead of Slack mrkdwn.",
    )

    # Sending remains explicit so a plain invocation is side-effect free.
    ap.add_argument(
        "--post", action="store_true",
        help="Actually send the digest via the OpenClaw message CLI. Without "
             "this flag (the default), the digest is only printed -- nothing "
             "is sent. Matches this repo's --execute convention.",
    )
    return ap


def main(argv=None, root=None, cron_list_fn=None, cron_runs_fn=None, send_fn=None,
         now=None, stdout=None, stderr=None):
    """Build, print, and optionally post Captain's recent-activity digest.

    Optional paths, clocks, collectors, sender, and streams make the complete
    command flow testable without real cron or Slack access.
    """
    # Resolve command and stream defaults while preserving injected test seams.
    argv = sys.argv[1:] if argv is None else list(argv)
    root = root or ROOT
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    # Let argparse format help, validation failures, and invalid options.
    # Redirection keeps that output on the caller's streams, since argparse
    # would otherwise write past the injected ones straight to the real ones.
    parser = build_arg_parser()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        # Treat a malformed lookback as user input, not a Captain incident:
        # returning the exit code here keeps it out of telemetry's exception
        # path so a typo never pages anyone.
        return exc.code if isinstance(exc.code, int) else 2

    hours = args.hours

    # Construct the default OpenClaw collectors only when tests do not inject them.
    openclaw_bin = os.environ.get("OPENCLAW_BIN", "openclaw")
    cron_list_fn = cron_list_fn or ca._default_cron_list_fn(openclaw_bin)
    cron_runs_fn = cron_runs_fn or ca._default_cron_runs_fn(openclaw_bin)

    # Collect a structured digest using the workspace's current configuration.
    channels_cfg = read_channels_config(root / "data" / "captain-channels.json")
    digest = build_digest(
        hours, root=root, cron_list_fn=cron_list_fn, cron_runs_fn=cron_runs_fn, now=now,
        modes_path=root / "data" / "captain-modes.json", channels_cfg=channels_cfg,
    )

    # Render Slack output and resolve known user ids without fabricating names.
    text = render_mrkdwn(digest)
    resolver = slack_user_names.SlackNameResolver(channels_cfg=channels_cfg, root=root)
    text = resolver.scrub_text(text)

    # JSON remains the unmodified evidence payload; default output is name-rendered.
    if args.json:
        print(json.dumps(digest, indent=2, sort_keys=True), file=stdout)
    else:
        print(text, file=stdout)

    # Perform the one possible external effect only when explicitly requested.
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
