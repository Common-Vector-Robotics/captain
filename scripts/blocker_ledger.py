#!/usr/bin/env python3
"""Captain blocker ledger: same-cycle chase/escalate state for the daily loop."""
import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from captain_db import DB as DEFAULT_DB, audit  # noqa: E402

STATUSES = ("open", "chasing", "escalated", "cleared")
COLS = ("id", "text", "source", "source_ref", "owner", "clickup_task_id",
        "status", "last_action", "last_action_at", "evidence",
        "opened_at", "updated_at")

SCHEMA = """
CREATE TABLE IF NOT EXISTS blockers (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  source_ref TEXT,
  owner TEXT,
  clickup_task_id TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  last_action TEXT,
  last_action_at TEXT,
  evidence TEXT NOT NULL DEFAULT '[]',
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _now():
    """Return the current time in a standard, timezone-aware text format."""
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db_path):
    """Create the blocker database and table if they do not exist yet."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as c:
        c.executescript(SCHEMA)


def _row_to_dict(row):
    """Turn one database row into a record labeled with readable field names."""
    return dict(zip(COLS, row))


def add_blocker(db_path, text, source, source_ref=None, owner=None,
                clickup_task_id=None, evidence=None):
    """Record a new blocker, or refresh the matching blocker already on file."""
    ensure_schema(db_path)
    bid = "captain_blk_" + hashlib.sha1(
        ("%s:%s" % (source, text)).encode("utf-8")).hexdigest()[:12]
    t = _now()
    with sqlite3.connect(str(db_path)) as c:
        c.execute(
            """INSERT INTO blockers(id,text,source,source_ref,owner,clickup_task_id,
                                    status,last_action,last_action_at,evidence,opened_at,updated_at)
               VALUES(?,?,?,?,?,?,'open',NULL,NULL,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 owner=COALESCE(excluded.owner, blockers.owner),
                 clickup_task_id=COALESCE(excluded.clickup_task_id, blockers.clickup_task_id),
                 updated_at=excluded.updated_at""",
            (bid, text, source, source_ref, owner, clickup_task_id,
             json.dumps(evidence or []), t, t))
    audit("blocker_added", blocker_id=bid, source=source, owner=owner,
          clickup_task_id=clickup_task_id)
    return bid


def update_blocker(db_path, blocker_id, status=None, action_note=None,
                   owner=None, clickup_task_id=None):
    """Update the status, owner, task link, or latest action for a blocker."""
    if status is not None and status not in STATUSES:
        raise ValueError("invalid status: %s (choose from %s)" % (status, ",".join(STATUSES)))
    ensure_schema(db_path)
    t = _now()
    with sqlite3.connect(str(db_path)) as c:
        row = c.execute("SELECT %s FROM blockers WHERE id=?" % ",".join(COLS),
                        (blocker_id,)).fetchone()
        if row is None:
            raise KeyError("unknown blocker id: %s" % blocker_id)
        cur = _row_to_dict(row)
        new_status = status or cur["status"]
        new_owner = owner if owner is not None else cur["owner"]
        new_task = clickup_task_id if clickup_task_id is not None else cur["clickup_task_id"]
        last_action = action_note if action_note is not None else cur["last_action"]
        last_action_at = t if action_note is not None else cur["last_action_at"]
        c.execute(
            """UPDATE blockers SET status=?, owner=?, clickup_task_id=?,
                                   last_action=?, last_action_at=?, updated_at=?
               WHERE id=?""",
            (new_status, new_owner, new_task, last_action, last_action_at, t, blocker_id))
        out = _row_to_dict(c.execute(
            "SELECT %s FROM blockers WHERE id=?" % ",".join(COLS), (blocker_id,)).fetchone())
    audit("blocker_updated", blocker_id=blocker_id, status=new_status,
          action_note=action_note)
    return out


def open_blockers(db_path):
    """Return every blocker that has not been marked as cleared."""
    ensure_schema(db_path)
    with sqlite3.connect(str(db_path)) as c:
        rows = c.execute(
            "SELECT %s FROM blockers WHERE status != 'cleared' ORDER BY opened_at" % ",".join(COLS)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def main():
    """Read the command-line request and add, update, or list blockers."""
    ap = argparse.ArgumentParser(description="Captain blocker ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("--db", default=str(DEFAULT_DB))
    p_add.add_argument("--text", required=True)
    p_add.add_argument("--source", required=True)
    p_add.add_argument("--source-ref")
    p_add.add_argument("--owner")
    p_add.add_argument("--clickup-task-id")
    p_upd = sub.add_parser("update")
    p_upd.add_argument("--db", default=str(DEFAULT_DB))
    p_upd.add_argument("--id", required=True)
    p_upd.add_argument("--status", choices=STATUSES)
    p_upd.add_argument("--action-note")
    p_upd.add_argument("--owner")
    p_upd.add_argument("--clickup-task-id")
    p_list = sub.add_parser("list")
    p_list.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()
    if args.cmd == "add":
        try:
            bid = add_blocker(args.db, args.text, args.source, args.source_ref,
                              args.owner, args.clickup_task_id)
        # OSError is in the tuple because --db is a user-supplied path:
        # ensure_schema() mkdirs its parent and opens the file, so an
        # unwritable/nonexistent location is a user error deserving the same
        # clean one-line exit as a bad status, not a traceback.
        except (ValueError, KeyError, OSError) as err:
            msg = str(err) if isinstance(err, OSError) else (err.args[0] if err.args else str(err))
            raise SystemExit(str(msg)) from err
        print(json.dumps({"id": bid}))
    elif args.cmd == "update":
        try:
            result = update_blocker(args.db, args.id, args.status,
                                    args.action_note, args.owner,
                                    args.clickup_task_id)
        # OSError for the same reason as the `add` branch above: --db is a
        # user-supplied path that update_blocker() mkdirs and opens.
        except (ValueError, KeyError, OSError) as err:
            msg = str(err) if isinstance(err, OSError) else (err.args[0] if err.args else str(err))
            raise SystemExit(str(msg)) from err
        print(json.dumps(result, indent=2))
    else:
        try:
            blockers = open_blockers(args.db)
        # OSError for the same reason as the `add` and `update` branches
        # above: --db is a user-supplied path that open_blockers() opens.
        except (ValueError, KeyError, OSError) as err:
            msg = str(err) if isinstance(err, OSError) else (err.args[0] if err.args else str(err))
            raise SystemExit(str(msg)) from err
        print(json.dumps(blockers, indent=2))


if __name__ == "__main__":
    with captain_telemetry.guard("blocker_ledger"):
        main()
