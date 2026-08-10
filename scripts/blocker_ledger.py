#!/usr/bin/env python3
"""Track blocker chase and escalation state across Captain's daily loop.

The ledger stores blockers in Captain's local SQLite database so morning,
afternoon, and end-of-day runs share the same status, owner, ClickUp link, and
latest action. Run this module directly to add, update, or list blockers.
"""

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
COLS = (
    "id",
    "text",
    "source",
    "source_ref",
    "owner",
    "clickup_task_id",
    "status",
    "last_action",
    "last_action_at",
    "evidence",
    "opened_at",
    "updated_at",
)

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


# Database helpers


def _now():
    """Return the current time in a standard, timezone-aware text format."""
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db_path):
    """Create the blocker database and table if they do not exist yet."""
    # SQLite creates the file but not a missing parent directory.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(db_path)) as connection:
        connection.executescript(SCHEMA)


def _row_to_dict(row):
    """Pair one positional SQLite row with the ledger's column names."""
    return dict(zip(COLS, row))


def add_blocker(
    db_path,
    text,
    source,
    source_ref=None,
    owner=None,
    clickup_task_id=None,
    evidence=None,
):
    """Create a blocker or refresh the matching existing record.

    The stable identifier is derived from ``source`` and ``text``, making
    repeated observations of the same blocker an update rather than a duplicate.
    """
    ensure_schema(db_path)

    # Use a deterministic, compact ID so the same evidence finds the same row.
    blocker_id = "captain_blk_" + hashlib.sha1(
        ("%s:%s" % (source, text)).encode("utf-8")
    ).hexdigest()[:12]
    timestamp = _now()

    # On a repeated observation, preserve existing optional fields unless the
    # caller supplies newer values.
    with sqlite3.connect(str(db_path)) as connection:
        connection.execute(
            """INSERT INTO blockers(
                                    id,text,source,source_ref,owner,clickup_task_id,
                                    status,last_action,last_action_at,evidence,
                                    opened_at,updated_at)
               VALUES(?,?,?,?,?,?,'open',NULL,NULL,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 owner=COALESCE(excluded.owner, blockers.owner),
                 clickup_task_id=COALESCE(
                     excluded.clickup_task_id, blockers.clickup_task_id),
                 updated_at=excluded.updated_at""",
            (
                blocker_id,
                text,
                source,
                source_ref,
                owner,
                clickup_task_id,
                json.dumps(evidence or []),
                timestamp,
                timestamp,
            ),
        )

    audit(
        "blocker_added",
        blocker_id=blocker_id,
        source=source,
        owner=owner,
        clickup_task_id=clickup_task_id,
    )
    return blocker_id


def update_blocker(
    db_path,
    blocker_id,
    status=None,
    action_note=None,
    owner=None,
    clickup_task_id=None,
):
    """Update the status, owner, task link, or latest action for a blocker."""
    # Validate direct function calls as well as argparse-driven calls.
    if status is not None and status not in STATUSES:
        raise ValueError(
            "invalid status: %s (choose from %s)"
            % (status, ",".join(STATUSES))
        )

    ensure_schema(db_path)
    timestamp = _now()

    with sqlite3.connect(str(db_path)) as connection:
        # Load the current row so omitted arguments can preserve existing values.
        row = connection.execute(
            "SELECT %s FROM blockers WHERE id=?" % ",".join(COLS),
            (blocker_id,),
        ).fetchone()
        if row is None:
            raise KeyError("unknown blocker id: %s" % blocker_id)

        current = _row_to_dict(row)
        new_status = status or current["status"]
        new_owner = owner if owner is not None else current["owner"]
        new_task = (
            clickup_task_id
            if clickup_task_id is not None
            else current["clickup_task_id"]
        )
        last_action = (
            action_note if action_note is not None else current["last_action"]
        )
        last_action_at = (
            timestamp if action_note is not None else current["last_action_at"]
        )

        # Save the resolved values and then return the authoritative stored row.
        connection.execute(
            """UPDATE blockers SET status=?, owner=?, clickup_task_id=?,
                                   last_action=?, last_action_at=?, updated_at=?
               WHERE id=?""",
            (
                new_status,
                new_owner,
                new_task,
                last_action,
                last_action_at,
                timestamp,
                blocker_id,
            ),
        )
        updated = _row_to_dict(
            connection.execute(
                "SELECT %s FROM blockers WHERE id=?" % ",".join(COLS),
                (blocker_id,),
            ).fetchone()
        )

    audit(
        "blocker_updated",
        blocker_id=blocker_id,
        status=new_status,
        action_note=action_note,
    )
    return updated


def open_blockers(db_path):
    """Return every blocker that has not been marked as cleared."""
    ensure_schema(db_path)

    with sqlite3.connect(str(db_path)) as connection:
        rows = connection.execute(
            "SELECT %s FROM blockers WHERE status != 'cleared' ORDER BY opened_at"
            % ",".join(COLS)
        ).fetchall()

    return [_row_to_dict(row) for row in rows]


# Command-line interface


def main():
    """Read the command-line request and add, update, or list blockers."""
    parser = argparse.ArgumentParser(description="Captain blocker ledger")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("--db", default=str(DEFAULT_DB))
    add_parser.add_argument("--text", required=True)
    add_parser.add_argument("--source", required=True)
    add_parser.add_argument("--source-ref")
    add_parser.add_argument("--owner")
    add_parser.add_argument("--clickup-task-id")

    update_parser = subparsers.add_parser("update")
    update_parser.add_argument("--db", default=str(DEFAULT_DB))
    update_parser.add_argument("--id", required=True)
    update_parser.add_argument("--status", choices=STATUSES)
    update_parser.add_argument("--action-note")
    update_parser.add_argument("--owner")
    update_parser.add_argument("--clickup-task-id")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()

    # Dispatch to the requested ledger operation and convert expected user
    # errors into concise command-line messages.
    if args.cmd == "add":
        try:
            blocker_id = add_blocker(
                args.db,
                args.text,
                args.source,
                args.source_ref,
                args.owner,
                args.clickup_task_id,
            )
        except (ValueError, KeyError, OSError) as err:
            # --db is user supplied, so path failures deserve the same clean
            # one-line exit as invalid IDs and statuses.
            message = (
                str(err)
                if isinstance(err, OSError)
                else (err.args[0] if err.args else str(err))
            )
            raise SystemExit(str(message)) from err

        print(json.dumps({"id": blocker_id}))

    elif args.cmd == "update":
        try:
            result = update_blocker(
                args.db,
                args.id,
                args.status,
                args.action_note,
                args.owner,
                args.clickup_task_id,
            )
        except (ValueError, KeyError, OSError) as err:
            message = (
                str(err)
                if isinstance(err, OSError)
                else (err.args[0] if err.args else str(err))
            )
            raise SystemExit(str(message)) from err

        print(json.dumps(result, indent=2))

    else:
        try:
            blockers = open_blockers(args.db)
        except (ValueError, KeyError, OSError) as err:
            message = (
                str(err)
                if isinstance(err, OSError)
                else (err.args[0] if err.args else str(err))
            )
            raise SystemExit(str(message)) from err

        print(json.dumps(blockers, indent=2))


if __name__ == "__main__":
    with captain_telemetry.guard("blocker_ledger"):
        main()
