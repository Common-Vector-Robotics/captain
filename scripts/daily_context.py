#!/usr/bin/env python3
"""Build the structured context used by Captain's morning review.

The script combines a ClickUp task export with Captain's local blocker and
daily-cycle records. Its JSON output highlights open work that is overdue, due
today, or missing an owner, while preserving yesterday's cycle and the current
critical paths for downstream reporting.

An optional snapshot copies the source ClickUp export unchanged so the exact
board state behind a report can be inspected later.
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]

# Allow direct script execution to import neighboring Captain helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from captain_db import DB as DEFAULT_DB  # noqa: E402
import blocker_ledger  # noqa: E402
import daily_cycle  # noqa: E402

# ClickUp may describe a finished task by either its display name or its broad
# status type. Both sets are checked before a task is included in the report.
CLOSED_NAMES = {"complete", "done", "closed", "rejected", "passed", "hired"}
CLOSED_TYPES = {"done", "closed"}


def status_of(task):
    """Return a task's normalized ``(status name, status type)`` pair.

    ClickUp exports may store ``status`` as either a mapping or a plain string.

    Example input: ``{"status": {"status": "In Progress", "type": "custom"}}``
    Example output: ``("in progress", "custom")``
    """
    s = task.get("status")

    # The full ClickUp shape carries both a user-facing name and a type.
    if isinstance(s, dict):
        return (s.get("status") or "").lower(), (s.get("type") or "").lower()

    # Older or simplified exports may contain only the status name.
    return (s or "").lower(), ""


def is_open(task):
    """Return whether a task is outside every known closed status."""
    name, typ = status_of(task)
    return name not in CLOSED_NAMES and typ not in CLOSED_TYPES


def due_local_date(task):
    """Return a task's due date in the host timezone, or ``None`` when unavailable.

    ClickUp stores due dates as Unix time in milliseconds. Missing and ordinary
    non-numeric values return ``None``; timestamps outside the host platform's
    supported range may still raise a conversion error.
    """
    raw = task.get("due_date")
    if not raw:
        return None

    try:
        return datetime.fromtimestamp(
            int(raw) / 1000.0,
            tz=timezone.utc,
        ).astimezone().date()
    except (TypeError, ValueError):
        return None


def slim(task):
    """Return the small, stable task shape used in the morning report.

    Assignees prefer a display name, then email, then ClickUp id. The result is
    JSON-ready and omits the much larger task payload from the source export.
    """
    # Normalize the status once for the report's compact representation.
    name, _ = status_of(task)

    # Keep a readable identity for every assignee, even in sparse exports.
    assignees = []
    for a in task.get("assignees") or []:
        assignees.append(a.get("username") or a.get("email") or str(a.get("id")))

    # Convert ClickUp's millisecond timestamp to a simple local calendar date.
    due = due_local_date(task)

    return {"id": task.get("id"), "name": task.get("name") or "",
            "status": name, "due": due.isoformat() if due else None,
            "assignees": assignees, "url": task.get("url")}


def build_context(clickup_path, db_path, date_str, critical_paths_path,
                  snapshot_out=None):
    """Build one morning-context mapping from board and local state.

    ``clickup_path`` may contain either a top-level task list or an object with
    a ``tasks`` list. ``date_str`` uses ``YYYY-MM-DD`` and determines which
    open tasks are overdue or due today.

    When ``snapshot_out`` is supplied, the original ClickUp export is copied
    there after the context has been assembled.
    """
    # Load either supported ClickUp export shape.
    data = json.loads(Path(clickup_path).expanduser().read_text(encoding="utf-8"))
    tasks = data if isinstance(data, list) else data.get("tasks", [])

    # Establish the report date and exclude work ClickUp already considers done.
    today = datetime.strptime(date_str, "%Y-%m-%d").date()
    open_tasks = [t for t in tasks if is_open(t)]

    # Classify each open task into the decision-relevant board views.
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

    # Add Captain's local continuity from the previous day.
    yesterday = daily_cycle.get_cycle(db_path, (today - timedelta(days=1)).isoformat())

    # Critical paths are optional; absence is represented explicitly as null.
    critical = None
    if critical_paths_path and Path(critical_paths_path).exists():
        critical = json.loads(Path(critical_paths_path).read_text(encoding="utf-8"))

    # Preserve the exact source export when the caller requests an audit snapshot.
    if snapshot_out is not None:
        Path(snapshot_out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(clickup_path), str(snapshot_out))

    # Keep the output shape stable for the morning-report consumer.
    return {
        "date": date_str,
        "board": {"open_count": len(open_tasks), "overdue": overdue,
                  "due_today": due_today, "owner_gaps": owner_gaps},
        "blockers": blocker_ledger.open_blockers(db_path),
        "yesterday": yesterday,
        "critical_paths": critical,
    }


def main():
    """Parse CLI arguments and print the resulting morning context as JSON."""
    # Define file inputs and the date used to classify due work.
    ap = argparse.ArgumentParser(description="Captain morning context builder")
    ap.add_argument("--clickup", required=True)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--date", default=datetime.now().astimezone().date().isoformat())
    ap.add_argument("--critical-paths", default=str(ROOT / "data" / "critical-paths.json"))
    ap.add_argument("--snapshot-out")
    args = ap.parse_args()

    # Convert expected input and filesystem failures into a concise CLI error.
    try:
        ctx = build_context(args.clickup, args.db, args.date, args.critical_paths,
                            args.snapshot_out)
    except (ValueError, KeyError, OSError) as err:
        msg = str(err) if isinstance(err, OSError) else (err.args[0] if err.args else str(err))
        raise SystemExit(str(msg)) from err

    # JSON is the public output consumed by the morning-cycle prompt.
    print(json.dumps(ctx, indent=2))


if __name__ == "__main__":
    with captain_telemetry.guard("daily_context"):
        main()
