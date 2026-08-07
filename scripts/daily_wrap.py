#!/usr/bin/env python3
"""Captain EOD wrap: day deltas, audit rollup, milestone risk."""
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
    """Read tasks from either a plain list or a ClickUp-style JSON file."""
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return data.get("tasks", [])


def _audit_counts(audit_path, date_str):
    """Count the task and blocker actions recorded during the chosen local day."""
    # "Today" here is defined as the full America/Detroit local calendar day
    # (00:00 to the next 00:00) — this must agree with `_blocker_day` below, which
    # compares `_local_date(...)` (a local calendar date) against `date_str` with no
    # time-of-day cutoff at all. An earlier version of this function used a 04:00
    # local cutoff instead, which silently disagreed with `_blocker_day` on whether an
    # overnight incident between midnight and 04:00 counted as "today" or "yesterday".
    start = datetime.combine(datetime.strptime(date_str, "%Y-%m-%d").date(),
                             time(0, 0), tzinfo=TZ)
    end = start + timedelta(days=1)
    counts = {}
    p = Path(audit_path)
    if not p.exists():
        return counts
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict):
                continue
            raw_ts = rec.get("ts", "")
            # OpenClaw event timestamps can be Unix milliseconds while Captain's
            # own audit entries use ISO-8601.  Ignore neither representation.
            if isinstance(raw_ts, (int, float)):
                ts = datetime.fromtimestamp(
                    raw_ts / 1000 if raw_ts > 10_000_000_000 else raw_ts,
                    tz=TZ,
                )
            else:
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ts.tzinfo is None or ts < start or ts >= end:
            continue
        ev = rec.get("event", "unknown")
        if ev.startswith("clickup_") or ev.startswith("blocker_") or ev.endswith("_sent"):
            counts[ev] = counts.get(ev, 0) + 1
    return counts


def _local_date(iso_ts):
    """UTC/offset ISO timestamp -> America/Detroit calendar date string, or None."""
    try:
        return datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00")) \
            .astimezone(TZ).date().isoformat()
    except ValueError:
        return None


def _blocker_day(db_path, date_str):
    """Summarize blockers opened, cleared, escalated, or still open that day."""
    out = {"opened_today": 0, "cleared_today": 0, "escalated_open": 0, "still_open": 0}
    if not Path(db_path).exists():
        return out
    with sqlite3.connect(str(db_path)) as c:
        try:
            rows = c.execute("SELECT status, opened_at, updated_at FROM blockers").fetchall()
        except sqlite3.OperationalError as error:
            # A missing/corrupt `blockers` table is not the same as a genuinely quiet
            # day -- the all-zero counts below must not be indistinguishable from "no
            # blockers today". Surface the degradation: a field the wrap/prompt can
            # show, and a Sentry message so someone learns the DB is broken.
            captain_telemetry.capture_message(
                "daily_wrap: blockers table unreadable ({}); reporting degraded "
                "all-zero blocker counts for {}".format(error, date_str),
                level="error",
            )
            out["degraded"] = True
            out["degraded_reason"] = str(error)
            return out
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
    if not critical_paths_path or not Path(critical_paths_path).exists():
        return {"at_risk_paths": []}
    cfg = json.loads(Path(critical_paths_path).read_text(encoding="utf-8"))
    by_id = {t.get("id"): t for t in eod_tasks}
    at_risk = []
    for path in cfg.get("paths", []):
        reasons = []
        for tid in path.get("task_ids", []):
            t = by_id.get(tid)
            if t is None or not is_open(t):
                continue
            slim_task = slim(t)
            due = due_local_date(t)
            if due is not None and due < today:
                reasons.append("%s overdue since %s" % (tid, due.isoformat()))
            elif due is not None and due <= today + timedelta(days=5) and slim_task["status"] in NON_PROGRESS:
                reasons.append("%s due %s but status '%s'" % (tid, due.isoformat(), slim_task["status"]))
            if not slim_task["assignees"] and due is not None and due <= today + timedelta(days=7):
                reasons.append("%s has no owner with due date %s" % (tid, due.isoformat()))
        if reasons:
            at_risk.append({"name": path.get("name") or "unnamed path", "reasons": reasons})
    return {"at_risk_paths": at_risk}


def build_wrap(morning_snapshot_path, eod_clickup_path, audit_path, db_path,
               date_str, critical_paths_path):
    """Compare morning and evening data to build the end-of-day summary."""
    today = datetime.strptime(date_str, "%Y-%m-%d").date()
    morning = _tasks(morning_snapshot_path)
    eod = _tasks(eod_clickup_path)
    eod_by_id = {t.get("id"): t for t in eod}
    closed_today = sorted((t.get("id") for t in morning
                          if is_open(t) and t.get("id") in eod_by_id
                          and not is_open(eod_by_id[t.get("id")])),
                         key=lambda tid: (tid is None, tid))
    was_overdue = {t.get("id") for t in morning if is_open(t)
                   and (due_local_date(t) or today) < today}
    new_overdue = sorted((t.get("id") for t in eod if is_open(t)
                         and (due_local_date(t) or today) < today
                         and t.get("id") not in was_overdue),
                        key=lambda tid: (tid is None, tid))
    return {
        "date": date_str,
        "mutations": _audit_counts(audit_path, date_str),
        "blockers": _blocker_day(db_path, date_str),
        "board": {"closed_today": closed_today, "new_overdue": new_overdue,
                  "open_count": sum(1 for t in eod if is_open(t))},
        "milestone": _milestone_risk(eod, critical_paths_path, today),
    }


def main():
    """Read command-line options, build the daily wrap, and print it as JSON."""
    ap = argparse.ArgumentParser(description="Captain EOD wrap builder")
    ap.add_argument("--morning", required=True)
    ap.add_argument("--eod", required=True)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--audit", default=str(DEFAULT_AUDIT))
    ap.add_argument("--date", default=datetime.now(TZ).date().isoformat())
    ap.add_argument("--critical-paths", default=str(ROOT / "data" / "critical-paths.json"))
    args = ap.parse_args()
    try:
        wrap = build_wrap(args.morning, args.eod, args.audit, args.db,
                          args.date, args.critical_paths)
    except (ValueError, KeyError, OSError) as err:
        msg = str(err) if isinstance(err, OSError) else (err.args[0] if err.args else str(err))
        raise SystemExit(str(msg)) from err
    print(json.dumps(wrap, indent=2))


if __name__ == "__main__":
    with captain_telemetry.guard("daily_wrap"):
        main()
