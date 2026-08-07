#!/usr/bin/env python3
"""Captain activity viewer: one chronological, read-only feed of what Captain
actually did (or deliberately did not do) in the last N hours.

Answers "how can I see all captain actions, even no_op ones?" by merging
three sources of truth that individually cannot answer it:

  CRON    -- `openclaw cron list --json` (agentId == "captain") plus
             `openclaw cron runs --id <id>` per job. Proves a job fired, but
             its `summary` is typically opaque (e.g. "NO_REPLY") -- it never
             says what Captain *decided*.
  DECIDED -- any `data/*state*.json` that has a `runs[]` history array (the
             standup-reconciliation state file is the model this was built
             from). This is where no-ops surface: `action: "no_op"` plus a
             `reason` is durable proof a cron ran and chose to do nothing.
  STATE   -- state files with no `runs[]` array (last-run-only style): falls
             back to `last_run_at` plus any boolean flags currently `True`,
             so a degraded run (e.g. `channel_enumeration_unavailable`) is
             still visible even without per-run history.
  ACTED   -- `data/audit-log.jsonl`: durable record of material (real)
             actions. A no-op leaves no trace here -- that gap is exactly
             why the DECIDED/STATE lines above exist.

Usage:
    python3 scripts/captain_activity.py [HOURS]

    HOURS defaults to 24. Must be a positive integer.

This is a read-only diagnostic tool. It never writes to `data/` (or
anywhere else), and every external call (openclaw subprocess, state file
reads, audit log read) degrades to a visible `WARN` line or a silent skip
rather than a traceback -- a typo or a flaky host must never be the thing
that breaks this script. See docs/daily-loop.md's "Seeing what Captain did"
section for example output and a walkthrough of each line kind.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import captain_telemetry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOURS = 24
TIME_FORMAT = "%m-%d %H:%M"

# Verified against OpenClaw 2026.7.1 on the Captain host: `openclaw cron runs
# --id <id>` returns {"entries": [...]}, and each entry carries `tsIso` (ISO
# string), `runAtIso`, `status`, `summary`, and `durationMs`. Note `ts` is epoch
# MILLISECONDS, not a string, so it must not lead this list. The extra names are
# defensive fallbacks in case the payload shape changes.
_RUN_TS_KEYS = ("tsIso", "runAtIso", "finishedAt", "finished_at",
                "endedAt", "ended_at", "startedAt", "started_at", "timestamp")
_RUN_START_KEYS = ("startedAt", "started_at")
_RUN_END_KEYS = ("finishedAt", "finished_at", "endedAt", "ended_at")


class BadHoursArgument(ValueError):
    """Raised for a bad HOURS argument; always caught inside main()."""


class Event(tuple):
    """A single activity-feed entry.

    Behaves as the plain `(timestamp, rendered_line)` 2-tuple every existing
    call site already unpacks (`for ts, line in events`, `_, line =
    events[0]`) -- every one of those unpack sites, in this module and in
    tests/test_captain_activity.py, keeps working unchanged. On top of that
    it carries `.kind` (one of "CRON"/"DECIDED"/"STATE"/"ACTED") and `.raw`
    (the structured fields the line was rendered from -- e.g. a DECIDED
    event's `.raw` has `name`/`action`/`audience`/`reason`; an ACTED event's
    `.raw` is the full parsed audit-log row).

    This exists so a downstream reuser (scripts/daily_activity_digest.py)
    can compute counts/flags/groupings directly from structured data instead
    of re-parsing the rendered text back into fields -- reuse without
    duplicating the collection logic above, and without changing this
    module's own CLI output or breaking any existing 2-tuple unpack.
    """

    # No `__slots__` here (deliberately): CPython does not allow a non-empty
    # `__slots__` on a subclass of a variable-length builtin like `tuple`, so
    # this subclass keeps the default per-instance `__dict__` and stores
    # `kind`/`raw` there instead.

    def __new__(cls, ts, line, kind, raw):
        """Create one feed item with both readable text and its original details."""
        obj = super().__new__(cls, (ts, line))
        obj.kind = kind
        obj.raw = raw
        return obj


# --------------------------------------------------------------------------
# Small, defensive parsing helpers -- every one of these is written to
# degrade (return None / skip) rather than raise, since malformed input from
# openclaw, state files, or the audit log is the expected steady state this
# tool exists to survive.
# --------------------------------------------------------------------------

def _first_json_object(text):
    """openclaw commands may print banner/log text before the JSON payload;
    find the first `{` and parse from there. Returns None on any failure
    (no `{`, or the text from `{` onward still isn't valid JSON)."""
    if not isinstance(text, str):
        return None
    idx = text.find("{")
    if idx == -1:
        return None
    try:
        return json.loads(text[idx:])
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_ts(value):
    """Parse a timestamp of unknown shape into an aware UTC datetime, or
    None if it cannot be parsed. Accepts an ISO-8601 string (with or without
    a trailing 'Z') or a numeric epoch (seconds or milliseconds). Any other
    type (None, dict, list, bool, ...) yields None rather than raising --
    this is the guard against inconsistent `ts` types in the audit log."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if isinstance(value, (int, float)):
        try:
            seconds = value / 1000.0 if abs(value) >= 1_000_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _first_parsed_ts(d, keys):
    """Return the first usable timestamp found under the preferred field names."""
    if not isinstance(d, dict):
        return None
    for key in keys:
        if key in d:
            ts = _parse_ts(d.get(key))
            if ts is not None:
                return ts
    return None


def _duration_seconds(run):
    """Work out how many seconds a scheduled run took, when the data allows it."""
    for key in ("durationSeconds", "duration_seconds"):
        val = run.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    for key in ("durationMs", "duration_ms", "duration"):
        val = run.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val) / 1000.0
    start = _first_parsed_ts(run, _RUN_START_KEYS)
    end = _first_parsed_ts(run, _RUN_END_KEYS)
    if start is not None and end is not None:
        return (end - start).total_seconds()
    return None


def _extract_runs(payload):
    """Find the list of scheduled-run records in several supported response shapes."""
    if isinstance(payload, dict):
        for key in ("entries", "runs", "data", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


def _state_name(path):
    """Turn a state filename into the short name shown in the activity feed."""
    stem = path.stem
    if stem.endswith("-state"):
        return stem[: -len("-state")]
    return stem


def _local(ts):
    """Format a timestamp in the computer's local time for display."""
    return ts.astimezone().strftime(TIME_FORMAT)


# --------------------------------------------------------------------------
# openclaw subprocess boundary -- isolated so tests never invoke a real
# subprocess (they inject fakes matching this (payload_or_None, error_or_None)
# calling convention instead).
# --------------------------------------------------------------------------

def run_openclaw(args, openclaw_bin="openclaw", timeout=30):
    """Run an openclaw subcommand. Returns (parsed_json, None) on success or
    (None, error_message) on any failure -- missing binary, non-zero exit,
    timeout, or unparseable JSON. Never raises."""
    try:
        result = subprocess.run(
            [openclaw_bin] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return None, "openclaw binary not found (%s)" % openclaw_bin
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, "exit %s: %s" % (
            result.returncode, (result.stderr or "").strip()[:300]
        )
    parsed = _first_json_object(result.stdout)
    if parsed is None:
        return None, "unparseable JSON output"
    return parsed, None


def _default_cron_list_fn(openclaw_bin):
    """Build the function used to ask OpenClaw for its scheduled jobs."""
    def fn():
        """Request and return OpenClaw's current scheduled-job list."""
        return run_openclaw(["cron", "list", "--json"], openclaw_bin=openclaw_bin)
    return fn


def _default_cron_runs_fn(openclaw_bin):
    """Build the function used to ask OpenClaw for a job's run history."""
    def fn(job_id):
        """Request and return the run history for one scheduled job."""
        return run_openclaw(
            ["cron", "runs", "--id", str(job_id)], openclaw_bin=openclaw_bin
        )
    return fn


# --------------------------------------------------------------------------
# Collectors -- each returns (events, warnings) where events is a list of
# (aware_datetime, rendered_line) tuples restricted to the requested window.
# --------------------------------------------------------------------------

def list_captain_jobs(cron_list_fn):
    """Return (jobs, warnings): every job from `openclaw cron list --json`
    with `agentId == "captain"`, regardless of whether it has any run
    history at all -- extracted out of collect_cron_events so a caller that
    needs the full registered set (e.g. daily_activity_digest.py, to notice
    a job that is registered but did not run at all in the window) doesn't
    have to re-run or re-parse the listing itself. collect_cron_events alone
    cannot answer that: it only ever emits an event for a run it found, so a
    job with zero runs in the window is silently invisible to it."""
    listing, err = cron_list_fn()
    if err is not None:
        return [], ["openclaw cron list --json: %s" % err]
    if isinstance(listing, dict):
        jobs = listing.get("jobs")
    else:
        jobs = listing
    if not isinstance(jobs, list):
        return [], ["openclaw cron list --json: unexpected shape (no jobs array)"]
    return [j for j in jobs if isinstance(j, dict) and j.get("agentId") == "captain"], []


def collect_cron_events(cutoff, cron_list_fn, cron_runs_fn):
    """Collect recent scheduled-job runs and warnings for the activity feed."""
    events = []
    captain_jobs, warnings = list_captain_jobs(cron_list_fn)
    for job in captain_jobs:
        job_id = job.get("id")
        name = str(job.get("name") or job_id or "unknown-job")
        if not job_id:
            warnings.append("cron job %r has no id; skipping run history" % name)
            continue
        runs_payload, err = cron_runs_fn(job_id)
        if err is not None:
            warnings.append(
                "openclaw cron runs --id %s (%s): %s" % (job_id, name, err)
            )
            continue
        for run in _extract_runs(runs_payload):
            ts = _first_parsed_ts(run, _RUN_TS_KEYS)
            if ts is None or ts < cutoff:
                continue
            status = run.get("status") or run.get("lastRunStatus") or "unknown"
            summary = run.get("summary") or run.get("output") or ""
            duration = _duration_seconds(run)
            duration_part = " | %.1fs" % duration if duration is not None else ""
            line = "CRON    %s | %s | status=%s | summary=%s%s" % (
                _local(ts), name, status, summary, duration_part,
            )
            events.append(Event(ts, line, "CRON", {
                "name": name, "job_id": job_id, "status": status,
                "summary": summary, "duration": duration,
            }))
    return events, warnings


def collect_state_events(cutoff, data_dir):
    """Collect recent decisions and warning flags saved in Captain's state files."""
    events = []
    warnings = []
    try:
        paths = sorted(data_dir.glob("*state*.json"))
    except OSError as exc:
        warnings.append("data/: could not list state files (%s)" % exc)
        return events, warnings
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            warnings.append("%s: unreadable or invalid JSON, skipped (%s)" % (path.name, exc))
            continue
        if not isinstance(payload, dict):
            warnings.append("%s: not a JSON object, skipped" % path.name)
            continue
        name = _state_name(path)
        runs = payload.get("runs")
        if isinstance(runs, list):
            for entry in runs:
                if not isinstance(entry, dict):
                    continue
                ts = _first_parsed_ts(entry, ("run_at", "ran_at", "ts"))
                if ts is None or ts < cutoff:
                    continue
                action = entry.get("action", "unknown")
                audience = entry.get("audience", "-")
                reason = entry.get("reason")
                if not reason:
                    counts = entry.get("counts")
                    reason = "counts=%s" % (counts,) if counts is not None else ""
                reason_part = " | %s" % reason if reason else ""
                line = "DECIDED %s | %s | action=%s | audience=%s%s" % (
                    _local(ts), name, action, audience, reason_part,
                )
                events.append(Event(ts, line, "DECIDED", {
                    "name": name, "action": action, "audience": audience,
                    "reason": reason, "entry": dict(entry),
                }))
        # A `runs[]` history array and top-level boolean flags are NOT
        # mutually exclusive: every real daily-loop state file (see
        # docs/daily-loop.md's State-file inventory) carries both `runs[]`
        # *and* top-level flags like `channel_enumeration_unavailable`
        # alongside it. Checking flags only in an `else` branch here would
        # make those flags silently invisible for every file that also has
        # run history -- backwards for a tool whose whole point is
        # surfacing degraded conditions nobody would otherwise notice. So
        # this check always runs, independent of whether the `runs[]`
        # branch above also fired for this same file.
        flags = sorted(k for k, v in payload.items() if v is True)
        if flags:
            ts = _first_parsed_ts(payload, ("last_run_at",))
            if ts is not None and ts >= cutoff:
                line = "STATE   %s | last_run_at=%s | flags=%s" % (
                    name, _local(ts), ",".join(flags),
                )
                events.append(Event(ts, line, "STATE", {
                    "name": name, "flags": flags, "payload": dict(payload),
                }))
        elif not isinstance(runs, list):
            # No `runs[]` and no true flags: the pre-existing last-run-only
            # shape, rendered exactly as before (no `flags=` suffix at all).
            ts = _first_parsed_ts(payload, ("last_run_at",))
            if ts is None or ts < cutoff:
                continue
            line = "STATE   %s | last_run_at=%s" % (name, _local(ts))
            events.append(Event(ts, line, "STATE", {
                "name": name, "flags": [], "payload": dict(payload),
            }))
    return events, warnings


def collect_audit_events(cutoff, path):
    """Collect recent real actions from Captain's audit log."""
    events = []
    warnings = []
    if not path.exists():
        return events, warnings
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append("%s: unreadable, skipped (%s)" % (path.name, exc))
        return events, warnings
    skipped = 0
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(row, dict):
            skipped += 1
            continue
        # `ts` in this file is not reliably a string across every writer --
        # _parse_ts guards every type it sees, so a non-string value here
        # can never raise; it is simply treated as unparseable if it isn't
        # a recognized string/epoch shape.
        ts = _first_parsed_ts(row, ("ts",))
        if ts is None or ts < cutoff:
            continue
        event = row.get("event", "unknown")
        task_id = row.get("task_id") or row.get("blocker_id") or "-"
        source = row.get("source", "-")
        line = "ACTED   %s | %s | task=%s | source=%s" % (
            _local(ts), event, task_id, source,
        )
        events.append(Event(ts, line, "ACTED", dict(row)))
    if skipped:
        warnings.append(
            "data/audit-log.jsonl: skipped %d malformed line(s)" % skipped
        )
    return events, warnings


def build_report(hours, root=None, cron_list_fn=None, cron_runs_fn=None, now=None):
    """Combine scheduled runs, saved decisions, and audited actions into one timeline."""
    root = root or ROOT
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    openclaw_bin = os.environ.get("OPENCLAW_BIN", "openclaw")
    cron_list_fn = cron_list_fn or _default_cron_list_fn(openclaw_bin)
    cron_runs_fn = cron_runs_fn or _default_cron_runs_fn(openclaw_bin)

    events = []
    warnings = []
    for collect_events, collect_warnings in (
        collect_cron_events(cutoff, cron_list_fn, cron_runs_fn),
        collect_state_events(cutoff, root / "data"),
        collect_audit_events(cutoff, root / "data" / "audit-log.jsonl"),
    ):
        events.extend(collect_events)
        warnings.extend(collect_warnings)

    events.sort(key=lambda pair: pair[0])
    return events, warnings, now


def _parse_hours_argument(argv):
    """Returns a positive int hours value. Raises BadHoursArgument (never a
    bare ValueError/IndexError) on anything else, so main() can convert a
    typo'd argument into a clean exit instead of a traceback."""
    if not argv:
        return DEFAULT_HOURS
    raw = argv[0]
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        raise BadHoursArgument("HOURS must be an integer, got %r" % (raw,))
    if hours <= 0:
        raise BadHoursArgument("HOURS must be positive, got %r" % (raw,))
    return hours


def main(argv=None, root=None, cron_list_fn=None, cron_runs_fn=None,
         now=None, stdout=None, stderr=None):
    """Print Captain's recent activity as a readable command-line report."""
    argv = sys.argv[1:] if argv is None else list(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip() if __doc__ else "usage: captain_activity.py [HOURS]",
              file=stdout)
        return 0

    try:
        hours = _parse_hours_argument(argv)
    except BadHoursArgument as exc:
        # A typo'd HOURS argument is a user-input error, not a Captain
        # incident -- fail cleanly here so it never reaches
        # captain_telemetry.guard's exception path and pages anyone.
        print("captain_activity: %s" % exc, file=stderr)
        return 2

    events, warnings, now = build_report(
        hours, root=root, cron_list_fn=cron_list_fn,
        cron_runs_fn=cron_runs_fn, now=now,
    )

    print(
        "Captain activity -- last %d hour(s), as of %s (%d event%s)" % (
            hours, _local(now), len(events), "" if len(events) == 1 else "s",
        ),
        file=stdout,
    )
    for warning in warnings:
        print("WARN    %s" % warning, file=stdout)
    if not events:
        print("(nothing in window)", file=stdout)
    for _, line in events:
        print(line, file=stdout)
    return 0


if __name__ == "__main__":
    with captain_telemetry.guard("captain_activity"):
        raise SystemExit(main())
