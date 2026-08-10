#!/usr/bin/env python3
"""Build Captain's end-of-day delivery summary.

The wrap compares morning and evening ClickUp snapshots, counts today's audit
and blocker activity, and identifies critical-path risks. It reads local files
and prints one JSON report for the daily-loop prompt to consume.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from captain_db import DB as DEFAULT_DB, AUDIT as DEFAULT_AUDIT  # noqa: E402
from daily_context import is_open, due_local_date, slim  # noqa: E402

TZ = ZoneInfo("America/Detroit")
NON_PROGRESS = {"to do", "backlog", "not started", "intake", "open"}


def _tasks(path):
    """Read tasks from a plain list or a ClickUp-style ``{"tasks": [...]}`` file."""
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))

    if isinstance(data, list):
        return data

    return data.get("tasks", [])


def _audit_counts(audit_path, date_str):
    """Count the task and blocker actions recorded during the chosen local day."""
    # Use the full Detroit calendar day, matching _blocker_day's date comparison.
    # A non-midnight cutoff would classify overnight activity inconsistently.
    start = datetime.combine(
        datetime.strptime(date_str, "%Y-%m-%d").date(),
        time(0, 0),
        tzinfo=TZ,
    )
    end = start + timedelta(days=1)
    counts = {}

    path = Path(audit_path)
    if not path.exists():
        return counts

    # Each valid JSONL record is considered independently so one malformed line
    # cannot hide the rest of the day's activity.
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                continue

            # OpenClaw may use Unix seconds or milliseconds; Captain uses ISO-8601.
            raw_ts = record.get("ts", "")
            if isinstance(raw_ts, (int, float)):
                timestamp = datetime.fromtimestamp(
                    raw_ts / 1000 if raw_ts > 10_000_000_000 else raw_ts,
                    tz=TZ,
                )
            else:
                timestamp = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        # Naive or out-of-window timestamps do not belong in the chosen day.
        if timestamp.tzinfo is None or timestamp < start or timestamp >= end:
            continue

        # The wrap reports only task, blocker, and outbound-send mutations.
        event = record.get("event", "unknown")
        if (
            event.startswith("clickup_")
            or event.startswith("blocker_")
            or event.endswith("_sent")
        ):
            counts[event] = counts.get(event, 0) + 1

    return counts


def _local_date(iso_ts):
    """Convert an ISO timestamp to a Detroit date string, or return ``None``."""
    try:
        timestamp = datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
        return timestamp.astimezone(TZ).date().isoformat()
    except ValueError:
        return None


def _blocker_day(db_path, date_str):
    """Summarize blockers opened, cleared, escalated, or still open that day."""
    out = {"opened_today": 0, "cleared_today": 0, "escalated_open": 0, "still_open": 0}

    # A host without a blocker database has no blocker history to summarize.
    if not Path(db_path).exists():
        return out

    with sqlite3.connect(str(db_path)) as connection:
        try:
            rows = connection.execute(
                "SELECT status, opened_at, updated_at FROM blockers"
            ).fetchall()
        except sqlite3.OperationalError as error:
            # An unreadable table is not a quiet day. Mark the report degraded and
            # send a diagnostic so zero counts are not mistaken for healthy state.
            captain_telemetry.capture_message(
                "daily_wrap: blockers table unreadable ({}); reporting degraded "
                "all-zero blocker counts for {}".format(error, date_str),
                level="error",
            )
            out["degraded"] = True
            out["degraded_reason"] = str(error)
            return out

    # Count daily transitions and the current unresolved blocker state.
    for status, opened_at, updated_at in rows:
        if _local_date(opened_at) == date_str:
            out["opened_today"] += 1
        if status == "cleared" and _local_date(updated_at) == date_str:
            out["cleared_today"] += 1
        if status == "escalated":
            out["escalated_open"] += 1
        if status != "cleared":
            out["still_open"] += 1

    return out


def _milestone_risk(eod_tasks, critical_paths_path, today):
    """Find important work at risk because it is late, idle, or has no owner."""
    # Critical-path analysis is optional; absent state produces an empty result.
    if not critical_paths_path or not Path(critical_paths_path).exists():
        return {"at_risk_paths": []}

    cfg = json.loads(Path(critical_paths_path).read_text(encoding="utf-8"))
    by_id = {t.get("id"): t for t in eod_tasks}
    at_risk = []

    # Evaluate only open tasks named by each configured critical path.
    for path in cfg.get("paths", []):
        reasons = []
        for tid in path.get("task_ids", []):
            task = by_id.get(tid)
            if task is None or not is_open(task):
                continue

            slim_task = slim(task)
            due = due_local_date(task)

            # Late, near-term idle, and near-term ownerless work are explicit risks.
            if due is not None and due < today:
                reasons.append("%s overdue since %s" % (tid, due.isoformat()))
            elif (
                due is not None
                and due <= today + timedelta(days=5)
                and slim_task["status"] in NON_PROGRESS
            ):
                reasons.append(
                    "%s due %s but status '%s'"
                    % (tid, due.isoformat(), slim_task["status"])
                )

            if (
                not slim_task["assignees"]
                and due is not None
                and due <= today + timedelta(days=7)
            ):
                reasons.append(
                    "%s has no owner with due date %s" % (tid, due.isoformat())
                )

        if reasons:
            at_risk.append(
                {"name": path.get("name") or "unnamed path", "reasons": reasons}
            )

    return {"at_risk_paths": at_risk}


def build_wrap(morning_snapshot_path, eod_clickup_path, audit_path, db_path,
               date_str, critical_paths_path):
    """Compare morning and evening data to build the end-of-day summary."""
    # Load both board snapshots and index the final state for fast comparisons.
    today = datetime.strptime(date_str, "%Y-%m-%d").date()
    morning = _tasks(morning_snapshot_path)
    eod = _tasks(eod_clickup_path)
    eod_by_id = {t.get("id"): t for t in eod}

    # A task closed today was open in the morning and closed by the final snapshot.
    closed_today = sorted(
        (
            task.get("id")
            for task in morning
            if is_open(task)
            and task.get("id") in eod_by_id
            and not is_open(eod_by_id[task.get("id")])
        ),
        key=lambda task_id: (task_id is None, task_id),
    )

    # Compare overdue sets so only newly late work appears in the delta.
    was_overdue = {
        task.get("id")
        for task in morning
        if is_open(task) and (due_local_date(task) or today) < today
    }
    new_overdue = sorted(
        (
            task.get("id")
            for task in eod
            if is_open(task)
            and (due_local_date(task) or today) < today
            and task.get("id") not in was_overdue
        ),
        key=lambda task_id: (task_id is None, task_id),
    )

    # Assemble the report from board deltas and supporting local state.
    return {
        "date": date_str,
        "mutations": _audit_counts(audit_path, date_str),
        "blockers": _blocker_day(db_path, date_str),
        "board": {
            "closed_today": closed_today,
            "new_overdue": new_overdue,
            "open_count": sum(1 for task in eod if is_open(task)),
        },
        "milestone": _milestone_risk(eod, critical_paths_path, today),
    }


def main():
    """Read command-line options, build the daily wrap, and print it as JSON."""
    parser = argparse.ArgumentParser(description="Captain EOD wrap builder")
    parser.add_argument("--morning", required=True)
    parser.add_argument("--eod", required=True)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--date", default=datetime.now(TZ).date().isoformat())
    parser.add_argument(
        "--critical-paths",
        default=str(ROOT / "data" / "critical-paths.json"),
    )
    args = parser.parse_args()

    # Expected input and path errors become concise CLI messages; unexpected
    # failures still reach the surrounding telemetry guard.
    try:
        wrap = build_wrap(
            args.morning,
            args.eod,
            args.audit,
            args.db,
            args.date,
            args.critical_paths,
        )
    except (ValueError, KeyError, OSError) as err:
        message = (
            str(err)
            if isinstance(err, OSError)
            else (err.args[0] if err.args else str(err))
        )
        raise SystemExit(str(message)) from err

    print(json.dumps(wrap, indent=2))


if __name__ == "__main__":
    with captain_telemetry.guard("daily_wrap"):
        main()
