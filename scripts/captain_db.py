#!/usr/bin/env python3
"""Create and maintain Captain's local SQLite and audit storage.

Other Captain scripts import this module for shared database paths, audit-log
writes, and approval-queue writes. Run it directly to initialize the storage or
print basic table counts.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("CAPTAIN_DB_PATH", str(ROOT / "data" / "captain.sqlite")))
AUDIT = Path(
    os.environ.get("CAPTAIN_AUDIT_LOG", str(ROOT / "data" / "audit-log.jsonl"))
)
APPROVALS = ROOT / "data" / "approval-queue.jsonl"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS commitments (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_path TEXT,
  source_line INTEGER,
  text TEXT NOT NULL,
  owner TEXT,
  approver TEXT,
  due_date TEXT,
  definition_of_done TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  confidence REAL NOT NULL DEFAULT 0.5,
  linked_clickup_task_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clickup_tasks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT,
  assignees TEXT NOT NULL DEFAULT '[]',
  due_date TEXT,
  list_id TEXT,
  url TEXT,
  raw_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  reason TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '[]',
  payload TEXT NOT NULL,
  approval_status TEXT NOT NULL DEFAULT 'pending',
  approver TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


# Shared storage helpers


def now():
    """Return the current time in a standard, timezone-aware text format."""
    return datetime.now(timezone.utc).isoformat()


def conn():
    """Open Captain's local database, creating its folder when needed."""
    # SQLite cannot create a missing parent directory itself.
    DB.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB)


def init_db():
    """Create Captain's database tables and tracking files if they are missing."""
    # executescript applies the complete idempotent schema in one connection.
    with conn() as c:
        c.executescript(SCHEMA)

    # The append-only JSONL stores must exist before other scripts can use them.
    for path in [AUDIT, APPROVALS]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

    print(f"initialized {DB}")


def audit(event, **fields):
    """Append one timestamped action to Captain's permanent JSONL audit log."""
    record = {"ts": now(), "event": event, **fields}

    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as audit_file:
        audit_file.write(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        )


def queue_approval(proposal):
    """Append a proposal for human review and audit that queue change."""
    # Keep proposals as one JSON object per line for safe append-only writes.
    APPROVALS.parent.mkdir(parents=True, exist_ok=True)
    with APPROVALS.open("a", encoding="utf-8") as approvals_file:
        approvals_file.write(
            json.dumps(proposal, ensure_ascii=False, sort_keys=True) + "\n"
        )

    audit(
        "approval_queued",
        proposal_id=proposal.get("id"),
        type=proposal.get("type"),
    )


# Command-line interface


def main():
    """Run the requested database setup or show basic record counts."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "stats"])
    args = parser.parse_args()

    if args.command == "init":
        init_db()
    elif args.command == "stats":
        # Initialization makes stats safe on a brand-new workspace.
        init_db()

        with conn() as c:
            for table in ["commitments", "clickup_tasks", "proposals"]:
                count = c.execute(f"select count(*) from {table}").fetchone()[0]
                print(f"{table}: {count}")


if __name__ == "__main__":
    with captain_telemetry.guard("captain_db"):
        main()
