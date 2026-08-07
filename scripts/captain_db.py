#!/usr/bin/env python3
import argparse, json, os, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("CAPTAIN_DB_PATH", str(ROOT / "data" / "captain.sqlite")))
AUDIT = Path(os.environ.get("CAPTAIN_AUDIT_LOG", str(ROOT / "data" / "audit-log.jsonl")))
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

def now(): return datetime.now(timezone.utc).isoformat()

def conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB)

def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
    for p in [AUDIT, APPROVALS]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
    print(f"initialized {DB}")

def audit(event, **fields):
    rec = {"ts": now(), "event": event, **fields}
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

def queue_approval(proposal):
    APPROVALS.parent.mkdir(parents=True, exist_ok=True)
    with APPROVALS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(proposal, ensure_ascii=False, sort_keys=True) + "\n")
    audit("approval_queued", proposal_id=proposal.get("id"), type=proposal.get("type"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["init", "stats"])
    args = ap.parse_args()
    if args.command == "init":
        init_db()
    elif args.command == "stats":
        init_db()
        with conn() as c:
            for table in ["commitments", "clickup_tasks", "proposals"]:
                n = c.execute(f"select count(*) from {table}").fetchone()[0]
                print(f"{table}: {n}")

if __name__ == "__main__":
    with captain_telemetry.guard("captain_db"):
        main()
