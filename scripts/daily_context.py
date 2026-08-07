#!/usr/bin/env python3
"""Build Captain's morning context JSON from a ClickUp export + local ledgers."""
import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from captain_db import DB as DEFAULT_DB  # noqa: E402
import blocker_ledger  # noqa: E402
import daily_cycle  # noqa: E402

TZ = ZoneInfo("America/Detroit")
CLOSED_NAMES = {"complete", "done", "closed", "rejected", "passed", "hired"}
CLOSED_TYPES = {"done", "closed"}


def status_of(task):
    """Return a task's status name and status category in a consistent form."""
    s = task.get("status")
    if isinstance(s, dict):
        return (s.get("status") or "").lower(), (s.get("type") or "").lower()
    return (s or "").lower(), ""


def is_open(task):
    """Tell whether a task still needs work rather than being finished or closed."""
    name, typ = status_of(task)
    return name not in CLOSED_NAMES and typ not in CLOSED_TYPES


def due_local_date(task):
    """Convert a task's due time into its calendar date in Detroit."""
    raw = task.get("due_date")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000.0, tz=TZ).date()
    except (TypeError, ValueError):
        return None


def slim(task):
    """Keep only the task details needed in a concise daily report."""
    name, _ = status_of(task)
    assignees = []
    for a in task.get("assignees") or []:
        assignees.append(a.get("username") or a.get("email") or str(a.get("id")))
    due = due_local_date(task)
    return {"id": task.get("id"), "name": task.get("name") or "",
            "status": name, "due": due.isoformat() if due else None,
            "assignees": assignees, "url": task.get("url")}


def build_context(clickup_path, db_path, date_str, critical_paths_path,
                  snapshot_out=None):
    """Build the morning summary from ClickUp tasks and Captain's local records."""
    data = json.loads(Path(clickup_path).expanduser().read_text(encoding="utf-8"))
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    today = datetime.strptime(date_str, "%Y-%m-%d").date()
    open_tasks = [t for t in tasks if is_open(t)]
    overdue, due_today, owner_gaps = [], [], []
    for t in open_tasks:
        slim_task = slim(t)
        due = due_local_date(t)
        if due is not None and due < today:
            overdue.append(slim_task)
        elif due == today:
            due_today.append(slim_task)
        if not slim_task["assignees"]:
            owner_gaps.append(slim_task)
    yesterday = daily_cycle.get_cycle(db_path, (today - timedelta(days=1)).isoformat())
    critical = None
    if critical_paths_path and Path(critical_paths_path).exists():
        critical = json.loads(Path(critical_paths_path).read_text(encoding="utf-8"))
    if snapshot_out is not None:
        Path(snapshot_out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(clickup_path), str(snapshot_out))
    return {
        "date": date_str,
        "board": {"open_count": len(open_tasks), "overdue": overdue,
                  "due_today": due_today, "owner_gaps": owner_gaps},
        "blockers": blocker_ledger.open_blockers(db_path),
        "yesterday": yesterday,
        "critical_paths": critical,
    }


def main():
    """Read command-line options, build the morning summary, and print it as JSON."""
    ap = argparse.ArgumentParser(description="Captain morning context builder")
    ap.add_argument("--clickup", required=True)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--date", default=datetime.now(TZ).date().isoformat())
    ap.add_argument("--critical-paths", default=str(ROOT / "data" / "critical-paths.json"))
    ap.add_argument("--snapshot-out")
    args = ap.parse_args()
    try:
        ctx = build_context(args.clickup, args.db, args.date, args.critical_paths,
                            args.snapshot_out)
    except (ValueError, KeyError, OSError) as err:
        msg = str(err) if isinstance(err, OSError) else (err.args[0] if err.args else str(err))
        raise SystemExit(str(msg)) from err
    print(json.dumps(ctx, indent=2))


if __name__ == "__main__":
    with captain_telemetry.guard("daily_context"):
        main()
