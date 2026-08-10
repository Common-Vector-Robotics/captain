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
  STATE   -- top-level boolean flags currently `True`, even when the file also
             has `runs[]`. A last-run-only file falls back to `last_run_at`,
             so degraded or legacy state remains visible without run history.
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
that breaks this script. The four source descriptions above define each output
line kind and the evidence behind it.
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


# Feed data model

class BadHoursArgument(ValueError):
    """Identify an invalid ``HOURS`` value for ``main`` to report cleanly."""


class Event(tuple):
    """Store one feed entry as readable text plus structured details.

    ``Event`` behaves like the existing ``(timestamp, rendered_line)`` tuple,
    so callers can keep unpacking it normally. It also exposes ``kind`` and
    ``raw`` attributes for consumers that need structured counts or grouping.

    This lets ``daily_activity_digest.py`` reuse collection results without
    parsing the human-readable line or duplicating collection logic.
    """

    # No `__slots__` here (deliberately): CPython does not allow a non-empty
    # `__slots__` on a subclass of a variable-length builtin like `tuple`, so
    # this subclass keeps the default per-instance `__dict__` and stores
    # `kind`/`raw` there instead.

    def __new__(cls, ts, line, kind, raw):
        """Create a tuple-compatible feed item with structured metadata."""
        obj = super().__new__(cls, (ts, line))
        obj.kind = kind
        obj.raw = raw

        return obj


# Defensive parsing helpers
#
# Malformed OpenClaw output, state files, and audit rows are expected inputs.
# These helpers return ``None`` or skip unusable data instead of raising.

def _first_json_object(text):
    """Parse the first JSON object after any OpenClaw banner text.

    Missing or invalid JSON returns ``None``.
    """
    # Only string output can contain the expected JSON object.
    if not isinstance(text, str):
        return None

    # OpenClaw may place log or banner text before the first opening brace.
    idx = text.find("{")
    if idx == -1:
        return None

    # Treat malformed command output as unavailable diagnostic data.
    try:
        return json.loads(text[idx:])
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_ts(value):
    """Parse a supported timestamp into an aware UTC datetime.

    Accepted values are ISO-8601 strings and numeric epochs in seconds or
    milliseconds. Unsupported or invalid values return ``None``; notably,
    booleans are not accepted as numeric epochs.
    """
    # ``bool`` is a subclass of ``int`` but is never a meaningful timestamp.
    if isinstance(value, bool):
        return None

    # Normalize ISO-8601 text, including the common trailing ``Z`` form.
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

        # Naive timestamps use UTC so every returned value is comparable.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    # Detect millisecond epochs by magnitude before converting to UTC.
    if isinstance(value, (int, float)):
        try:
            seconds = value / 1000.0 if abs(value) >= 1_000_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    return None


def _first_parsed_ts(d, keys):
    """Return the first usable timestamp under the preferred field names."""
    if not isinstance(d, dict):
        return None

    # Field order matters because upstream payloads may carry several clocks.
    for key in keys:
        if key in d:
            ts = _parse_ts(d.get(key))
            if ts is not None:
                return ts

    return None


def _duration_seconds(run):
    """Return a scheduled run's duration in seconds, when available."""
    # Prefer fields already expressed in seconds.
    for key in ("durationSeconds", "duration_seconds"):
        val = run.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)

    # Convert the millisecond fields used by current OpenClaw responses.
    for key in ("durationMs", "duration_ms", "duration"):
        val = run.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val) / 1000.0

    # Fall back to the difference between explicit start and end timestamps.
    start = _first_parsed_ts(run, _RUN_START_KEYS)
    end = _first_parsed_ts(run, _RUN_END_KEYS)
    if start is not None and end is not None:
        return (end - start).total_seconds()

    return None


def _extract_runs(payload):
    """Extract scheduled-run objects from supported response shapes."""
    # Different OpenClaw versions have used several top-level collection names.
    if isinstance(payload, dict):
        for key in ("entries", "runs", "data", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]

        return []

    # Tests and defensive callers may provide the records as a bare list.
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


# OpenClaw subprocess boundary
#
# Tests replace these functions with fakes that use the same
# ``(payload_or_none, error_or_none)`` return convention.

def run_openclaw(args, openclaw_bin="openclaw", timeout=30):
    """Run an OpenClaw subcommand and return parsed JSON or an error.

    Success returns ``(parsed_json, None)``. A missing binary, timeout,
    non-zero exit, or malformed response returns ``(None, error_message)``.
    Expected command failures never propagate as exceptions.
    """
    # Capture the command so errors become feed warnings instead of tracebacks.
    try:
        result = subprocess.run(
            [openclaw_bin] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return None, "openclaw binary not found (%s)" % openclaw_bin
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)

    # Preserve a bounded stderr excerpt when OpenClaw reports a failure.
    if result.returncode != 0:
        return None, "exit %s: %s" % (
            result.returncode, (result.stderr or "").strip()[:300]
        )

    # Parse around any non-JSON banner text emitted before the payload.
    parsed = _first_json_object(result.stdout)
    if parsed is None:
        return None, "unparseable JSON output"

    return parsed, None


def _default_cron_list_fn(openclaw_bin):
    """Build a zero-argument function that lists scheduled jobs."""

    def fn():
        """Request and return OpenClaw's current scheduled-job list."""
        return run_openclaw(["cron", "list", "--json"], openclaw_bin=openclaw_bin)

    return fn


def _default_cron_runs_fn(openclaw_bin):
    """Build a function that fetches one scheduled job's run history."""

    def fn(job_id):
        """Request and return the run history for one scheduled job."""
        return run_openclaw(
            ["cron", "runs", "--id", str(job_id)], openclaw_bin=openclaw_bin
        )

    return fn


# Activity collectors
#
# Collection helpers return data plus warnings: the job-list helper returns
# ``(jobs, warnings)`` and event collectors return ``(events, warnings)``.

def list_captain_jobs(cron_list_fn):
    """Return all registered Captain jobs and any listing warnings.

    Jobs are returned even when they have no run history. That distinction lets
    ``daily_activity_digest.py`` detect registered jobs that did not run;
    ``collect_cron_events`` alone cannot represent them.
    """
    # Fetch the listing through an injectable boundary for deterministic tests.
    listing, err = cron_list_fn()
    if err is not None:
        return [], ["openclaw cron list --json: %s" % err]

    # Accept both the current object shape and a defensive bare-list shape.
    if isinstance(listing, dict):
        jobs = listing.get("jobs")
    else:
        jobs = listing

    if not isinstance(jobs, list):
        return [], ["openclaw cron list --json: unexpected shape (no jobs array)"]

    # Ignore other agents' scheduled work and malformed entries.
    return [j for j in jobs if isinstance(j, dict) and j.get("agentId") == "captain"], []


def collect_cron_events(cutoff, cron_list_fn, cron_runs_fn):
    """Collect recent scheduled-job runs and warnings for the activity feed."""
    events = []
    captain_jobs, warnings = list_captain_jobs(cron_list_fn)

    # Fetch and render each Captain job's run history independently.
    for job in captain_jobs:
        job_id = job.get("id")
        name = str(job.get("name") or job_id or "unknown-job")

        # A job without an ID cannot be used in the run-history command.
        if not job_id:
            warnings.append("cron job %r has no id; skipping run history" % name)
            continue

        runs_payload, err = cron_runs_fn(job_id)
        if err is not None:
            warnings.append(
                "openclaw cron runs --id %s (%s): %s" % (job_id, name, err)
            )
            continue

        # Keep only parseable runs inside the requested time window.
        for run in _extract_runs(runs_payload):
            ts = _first_parsed_ts(run, _RUN_TS_KEYS)
            if ts is None or ts < cutoff:
                continue

            # Render the available run fields without requiring optional data.
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

    # Discover every state-file variant without assuming a fixed inventory.
    try:
        paths = sorted(data_dir.glob("*state*.json"))
    except OSError as exc:
        warnings.append("data/: could not list state files (%s)" % exc)
        return events, warnings

    # Parse files independently so one invalid file cannot hide the rest.
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

        # History-style files expose each recorded decision, including no-ops.
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

                # Older entries may carry counts instead of a reason string.
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

        # History and top-level warning flags are independent. Real daily-loop
        # state files carry both, so checking flags only when ``runs`` is absent
        # would hide degraded conditions from the activity feed.
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
            # Last-run-only files retain their existing output without a flags
            # suffix when no warning flag is active.
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

    # A missing audit log means there are no durable actions to show.
    if not path.exists():
        return events, warnings

    # Read once so malformed lines can be counted without aborting the file.
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append("%s: unreadable, skipped (%s)" % (path.name, exc))
        return events, warnings

    skipped = 0

    # Parse the JSONL stream one independent row at a time.
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

        # Audit writers do not all use the same timestamp type. The shared
        # parser safely rejects unrecognized values.
        ts = _first_parsed_ts(row, ("ts",))
        if ts is None or ts < cutoff:
            continue

        # Render the stable core fields and retain the complete row in ``raw``.
        event = row.get("event", "unknown")
        task_id = row.get("task_id") or row.get("blocker_id") or "-"
        source = row.get("source", "-")
        line = "ACTED   %s | %s | task=%s | source=%s" % (
            _local(ts), event, task_id, source,
        )
        events.append(Event(ts, line, "ACTED", dict(row)))

    # Report malformed rows once instead of emitting repetitive warnings.
    if skipped:
        warnings.append(
            "data/audit-log.jsonl: skipped %d malformed line(s)" % skipped
        )

    return events, warnings


# Report assembly

def build_report(hours, root=None, cron_list_fn=None, cron_runs_fn=None, now=None):
    """Combine scheduled runs, saved decisions, and actions into one timeline."""
    # Resolve injectable dependencies and the requested time window.
    root = root or ROOT
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    openclaw_bin = os.environ.get("OPENCLAW_BIN", "openclaw")
    cron_list_fn = cron_list_fn or _default_cron_list_fn(openclaw_bin)
    cron_runs_fn = cron_runs_fn or _default_cron_runs_fn(openclaw_bin)

    # Merge all three sources while preserving every non-fatal warning.
    events = []
    warnings = []
    for collect_events, collect_warnings in (
        collect_cron_events(cutoff, cron_list_fn, cron_runs_fn),
        collect_state_events(cutoff, root / "data"),
        collect_audit_events(cutoff, root / "data" / "audit-log.jsonl"),
    ):
        events.extend(collect_events)
        warnings.extend(collect_warnings)

    # Present different event kinds together in one chronological feed.
    events.sort(key=lambda pair: pair[0])
    return events, warnings, now


# Command-line interface

def _parse_hours_argument(argv):
    """Return a positive hours value or raise ``BadHoursArgument``.

    The dedicated exception lets ``main`` turn invalid user input into a clean
    command error instead of an incident traceback.
    """
    # An omitted argument uses the documented 24-hour window.
    if not argv:
        return DEFAULT_HOURS

    # Parse only the first positional value, matching the existing CLI contract.
    raw = argv[0]
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        raise BadHoursArgument("HOURS must be an integer, got %r" % (raw,))

    # Zero and negative windows cannot describe recent activity.
    if hours <= 0:
        raise BadHoursArgument("HOURS must be positive, got %r" % (raw,))

    return hours


def main(argv=None, root=None, cron_list_fn=None, cron_runs_fn=None,
         now=None, stdout=None, stderr=None):
    """Print Captain's recent activity as a readable command-line report."""
    # Use real command-line streams by default while keeping tests injectable.
    argv = sys.argv[1:] if argv is None else list(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    # The module docstring is the canonical long-form CLI help text.
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip() if __doc__ else "usage: captain_activity.py [HOURS]",
              file=stdout)
        return 0

    # Validate user input before entering the telemetry incident path.
    try:
        hours = _parse_hours_argument(argv)
    except BadHoursArgument as exc:
        # A typo'd HOURS argument is a user-input error, not a Captain
        # incident -- fail cleanly here so it never reaches
        # captain_telemetry.guard's exception path and pages anyone.
        print("captain_activity: %s" % exc, file=stderr)
        return 2

    # Collect every source for the same time window.
    events, warnings, now = build_report(
        hours, root=root, cron_list_fn=cron_list_fn,
        cron_runs_fn=cron_runs_fn, now=now,
    )

    # Print a stable header, then warnings and chronological feed entries.
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
