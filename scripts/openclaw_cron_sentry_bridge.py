#!/usr/bin/env python3
"""Manually run the optional OpenClaw cron-failure bridge.

When invoked, it diffs each job's error counter in `openclaw cron list --json`
against local state and sends one grouped Sentry event per newly failed job.
The check-in doubles as a dead-man's switch: host asleep / OpenClaw down /
bridge broken => missed check-in alert. This package does not install or
configure a scheduler for the bridge.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = ROOT / "data" / "sentry-bridge-state.json"
MONITOR_SLUG = "captain-openclaw-bridge"
MONITOR_CONFIG = {
    "schedule": {"type": "interval", "value": 10, "unit": "minute"},
    "checkin_margin": 10,
    "max_runtime": 5,
    "timezone": "America/Detroit",
    "failure_issue_threshold": 1,
    "recovery_threshold": 1,
}
_ERROR_COUNT_FIELDS = (
    "errors", "errorCount", "error_count", "consecutiveErrors",
    "consecutive_errors",
)
_LAST_ERROR_FIELDS = ("lastError", "last_error")


class OpenClawCronListError(RuntimeError):
    """Explain why Captain could not read OpenClaw's scheduled-job list."""

    pass


def run_openclaw_cron_list(openclaw_bin):
    """Run ``openclaw cron list --json`` and validate its response shape."""
    # Bound the subprocess so a hung CLI cannot stall monitoring indefinitely.
    try:
        result = subprocess.run(
            [openclaw_bin, "cron", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpenClawCronListError(f"openclaw cron list failed: {exc}") from exc

    # Preserve a short piece of stderr to make CLI failures diagnosable.
    if result.returncode != 0:
        raise OpenClawCronListError(
            f"openclaw exited {result.returncode}: {result.stderr.strip()[:500]}"
        )

    # Successful commands must still contain valid JSON in a supported shape.
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenClawCronListError(f"unparseable cron list JSON: {exc}") from exc

    if isinstance(raw, dict) and isinstance(raw.get("jobs"), list):
        return raw
    if isinstance(raw, list):
        return raw

    raise OpenClawCronListError("cron list JSON has no jobs array")


def extract_jobs(raw):
    """Normalize a cron-list payload to ``(jobs, truncated)``.

    ``openclaw cron list --json`` may return a paginated envelope
    (``jobs``/``total``/``limit``/``hasMore``) and exposes no paging flags,
    so a truncated response cannot be paged through. Confirmed on the Captain
    host that nothing is currently dropped, but reporting ``truncated`` keeps
    a future cap from silently leaving later jobs unmonitored. A bare list is
    treated as complete.
    """
    if isinstance(raw, dict):
        return list(raw.get("jobs") or []), bool(raw.get("hasMore"))

    return list(raw or []), False


def _int_or_none(value):
    """Convert a whole-number value to an integer, or return nothing if invalid."""
    # bool is an int subclass in Python but is not a meaningful error count.
    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.isdigit():
        return int(value)

    return None


def job_view(job):
    """Reduce one OpenClaw job to the fields needed for monitoring.

    Error fields have changed across OpenClaw versions, so the largest valid
    counter found in the job or nested state is used.
    """
    state = job.get("state") if isinstance(job.get("state"), dict) else {}

    # Search every supported counter name at both response levels.
    counters = []
    for source in (job, state):
        for field in _ERROR_COUNT_FIELDS:
            count = _int_or_none(source.get(field))
            if count is not None:
                counters.append(count)

    # Prefer the first non-empty last-error field in the same level order.
    last_error = None
    for source in (job, state):
        for field in _LAST_ERROR_FIELDS:
            value = source.get(field)
            if isinstance(value, str) and value:
                last_error = value
                break
        if last_error:
            break

    # A missing ID falls back to the name so the snapshot still has a stable key.
    name = str(job.get("name") or job.get("id") or "unknown-job")
    key = str(job.get("id") or name)

    return {
        "key": key,
        "name": name,
        "errors": max(counters) if counters else None,
        "last_error": last_error,
    }


def build_state(views):
    """Build the compact per-job snapshot saved between monitoring runs."""
    return {
        "jobs": {
            view["key"]: {
                "name": view["name"],
                "errors": view["errors"],
                "last_error": view["last_error"],
            }
            for view in views
        }
    }


def diff_failures(prev_state, views):
    """Return jobs whose failure counter increased since the prior snapshot."""
    # Get jobs from previous state if available.
    prev_jobs = prev_state.get("jobs", {}) if isinstance(prev_state, dict) else {}

    # The first run creates a baseline and reports no historical failures.
    if not prev_jobs:
        return []

    # Collect jobs whose current counter exceeds the saved counter.
    failures = []
    for view in views:
        # Read the current count, treating a missing value as zero.
        current = view["errors"] or 0

        # Match the prior job by key and default its missing count to zero.
        previous_entry = prev_jobs.get(view["key"], {})
        previous = previous_entry.get("errors") or 0

        # A higher count is a newly observed failure worth reporting.
        if current > previous:
            failures.append(
                {
                    "name": view["name"],
                    "previous": previous,
                    "current": current,
                    "last_error": view["last_error"],
                }
            )

    return failures


def _load_state(path):
    """Read the previous monitoring snapshot, or start empty if unavailable."""
    # Missing, unreadable, or malformed state behaves like a first monitoring run.
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv=None, run_list=None, capture_message_fn=None,
         capture_exception_fn=None, checkin_fn=None, init_fn=None):
    """Check cron jobs, report new failures, and save the latest snapshot.

    Optional function arguments are dependency-injection seams used by tests;
    normal command-line execution uses the real OpenClaw and telemetry helpers.
    """
    # Parse paths, executable selection, and side-effect-free preview mode.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    parser.add_argument(
        "--openclaw-bin", default=os.environ.get("OPENCLAW_BIN", "openclaw")
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    # Fill any uninjected dependency with its production implementation.
    run_list = run_openclaw_cron_list if run_list is None else run_list
    capture_message_fn = (
        captain_telemetry.capture_message
        if capture_message_fn is None else capture_message_fn
    )
    capture_exception_fn = (
        captain_telemetry.capture_exception
        if capture_exception_fn is None else capture_exception_fn
    )
    checkin_fn = (
        captain_telemetry.capture_checkin if checkin_fn is None else checkin_fn
    )
    init_fn = captain_telemetry.init_telemetry if init_fn is None else init_fn

    # Within main, dry runs skip explicit telemetry calls. Direct CLI execution
    # still enters the outer telemetry guard below before calling main.
    if not args.dry_run:
        init_fn("openclaw-cron-bridge")

    # Failure to list jobs is itself a failed monitor run.
    try:
        raw_listing = run_list(args.openclaw_bin)
    except OpenClawCronListError as exc:
        if not args.dry_run:
            capture_exception_fn(exc, component="openclaw-cron-bridge")
            checkin_fn(MONITOR_SLUG, "error", monitor_config=MONITOR_CONFIG)
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    # Normalize the listing, compare it with prior state, and identify schema gaps.
    jobs, truncated = extract_jobs(raw_listing)
    views = [job_view(job) for job in jobs]
    prev_state = _load_state(args.state)
    failures = diff_failures(prev_state, views)
    counters_missing = [v["name"] for v in views if v["errors"] is None]

    # Preview the exact findings without reporting or persisting anything.
    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "jobs": len(views),
                    "would_report": failures,
                    "counters_missing": counters_missing,
                    "truncated": truncated,
                },
                indent=2,
            )
        )
        return 0

    # Report each newly increased counter as its own stable Sentry issue.
    for failure in failures:
        message = (
            f"OpenClaw cron job failed: {failure['name']} "
            f"(errors {failure['previous']} -> {failure['current']})"
        )
        capture_message_fn(
            message,
            level="error",
            fingerprint=["openclaw-cron", failure["name"]],
            extra={"last_error": failure["last_error"] or "unavailable"},
        )

    # Missing counters across the entire listing mean the bridge may be blind
    # after an OpenClaw response-schema change.
    if views and len(counters_missing) == len(views):
        capture_message_fn(
            "openclaw-cron-bridge found no error counters on any job; "
            "verify `openclaw cron list --json` field names",
            level="warning",
            fingerprint=["openclaw-cron-bridge", "no-counters"],
            extra={"job_names": counters_missing[:20]},
        )

    # A paginated envelope is dangerous because the CLI provides no next-page API.
    if truncated:
        capture_message_fn(
            "openclaw-cron-bridge is only seeing the first page of "
            "`openclaw cron list --json` (hasMore=true); jobs beyond it are "
            "unmonitored and the CLI exposes no --limit/--offset to page",
            level="warning",
            fingerprint=["openclaw-cron-bridge", "truncated-listing"],
            extra={"jobs_seen": len(views)},
        )

    # Atomically save the new baseline so interruption cannot leave partial JSON.
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(build_state(views), indent=2), encoding="utf-8"
    )
    os.replace(tmp_path, state_path)

    # A successful check-in keeps Sentry's dead-man monitor healthy.
    checkin_fn(MONITOR_SLUG, "ok", monitor_config=MONITOR_CONFIG)
    print( # Example: {"ok": true, "jobs": 12, "new_failures": ["job1", "job2"]})
        json.dumps(
            {
                "ok": True,
                "jobs": len(views),
                "new_failures": [failure["name"] for failure in failures],
            }
        )
    )
    return 0

# The outer telemetry guard ensures that any uncaught exception is reported to Sentry
if __name__ == "__main__":
    with captain_telemetry.guard("openclaw-cron-bridge"):
        raise SystemExit(main())
