#!/usr/bin/env python3
"""Store the small amount of state that connects Captain's daily phases.

Each date can hold the team's current top three priorities, tomorrow's top
three, the per-person top two that were sent or previewed, and timestamps for
the completed morning and end-of-day phases. The module exposes the same
operations as Python helpers and as a compact command-line interface.

Every public state-update helper also writes to Captain's local audit log.
"""

# Requirements

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import captain_telemetry

# Root path of Captain project
ROOT = Path(__file__).resolve().parents[1]

# Allow direct script execution to import the neighboring database helper.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from captain_db import DB as DEFAULT_DB, audit  # noqa: E402

# Define the SQLite schema for the daily_cycle table, which stores the daily cycle state.
SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_cycle (
  date TEXT PRIMARY KEY,
  top3 TEXT NOT NULL DEFAULT '[]',
  tomorrow_top3 TEXT NOT NULL DEFAULT '[]',
  morning_done_at TEXT,
  eod_done_at TEXT,
  updated_at TEXT NOT NULL,
  personal_top2 TEXT NOT NULL DEFAULT '[]'
);
"""

# Columns added in later schema versions, which are applied if missing from an older database.
ADDED_COLUMNS = (
    ("personal_top2", "TEXT NOT NULL DEFAULT '[]'"),
)

# Phrasees that can be used to mark the completion of daily phases, which are recorded in the database.
PHASES = ("morning", "eod")


def _now():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _ensure(db_path):
    """Create the daily-cycle table and apply any additive migrations.

    Calling this repeatedly is safe. It lets every public helper work with a
    new database as well as one created before later columns were introduced.
    """
    # SQLite cannot create the database until its parent directory exists.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(db_path)) as connection:
        # Create the complete current schema for a new database.
        connection.executescript(SCHEMA)

        # Add any newer columns missing from an older database.
        existing = {row[1] for row in connection.execute("PRAGMA table_info(daily_cycle)")}
        for name, decl in ADDED_COLUMNS:
            if name in existing:
                continue

            # SQL identifiers cannot use parameter placeholders. Both values
            # come from ``ADDED_COLUMNS`` above, never from user input.
            connection.execute("ALTER TABLE daily_cycle ADD COLUMN %s %s" % (name, decl))


def _upsert(db_path, date_str, **updates):
    """Create one date's row if needed, then apply the requested fields.

    Example input: ``_upsert(db, "2026-08-10", top3='["Ship"]')``
    Example output: the complete deserialized cycle mapping for that date.
    """

    # Ensure both the table and any additive migrations are present.
    _ensure(db_path)
    t = _now()

    with sqlite3.connect(str(db_path)) as connection:
        # Insert the day's base row once; later calls reuse it.
        connection.execute("INSERT INTO daily_cycle(date, updated_at) VALUES(?, ?) "
                  "ON CONFLICT(date) DO NOTHING", (date_str, t))

        # Apply each supplied field while sharing one update timestamp.
        for k, v in updates.items():
            # SQL identifiers cannot use placeholders. ``k`` always comes from
            # fixed internal field names, never directly from user input.
            connection.execute("UPDATE daily_cycle SET %s=?, updated_at=? WHERE date=?" % k,
                      (v, t, date_str))

    # Read through the public path so callers always receive the full row shape.
    return get_cycle(db_path, date_str)


def set_top3(db_path, date_str, items):
    """Save today's selected priorities and return the full daily cycle."""
    # Lists are stored as JSON so item order is preserved in one SQLite field.
    out = _upsert(db_path, date_str, top3=json.dumps(list(items)))

    # Audit local state changes independently from the database transaction.
    audit("daily_cycle_top3_set", date=date_str, count=len(items))
    return out


def set_tomorrow_top3(db_path, date_str, items):
    """Save tomorrow's selected priorities and return the full daily cycle."""
    # Lists are stored as JSON so item order is preserved in one SQLite field.
    out = _upsert(db_path, date_str, tomorrow_top3=json.dumps(list(items)))

    # Record how many priorities Captain selected without logging their text.
    audit("daily_cycle_tomorrow_top3_set", date=date_str, count=len(items))
    return out


def set_personal_top2(db_path, date_str, items):
    """Save the per-person top two that Captain sent or previewed.

    Each item contains ``slack_user_id``, ``key``, ``task_ids``, ``overridden``,
    and ``override_reason``. ``key`` preserves the ClickUp board identity even
    when Slack resolution changes.

    This is local cycle state, so it is written in both ``shadow`` and ``live``.
    Only ``clickup_*`` audit actions are suppressed in shadow mode.
    """
    # Preserve the complete per-person records as ordered JSON.
    out = _upsert(db_path, date_str, personal_top2=json.dumps(list(items)))

    # Audit only the record count; the detailed selection remains in the table.
    audit("daily_cycle_personal_top2_set", date=date_str, count=len(items))
    return out


def stamp(db_path, date_str, phase):
    """Record when an allowed daily phase finished and return the cycle."""
    # Reject unknown names before constructing the matching column name.
    if phase not in PHASES:
        raise ValueError("invalid phase: %s (choose from %s)" % (phase, ",".join(PHASES)))

    # ``phase`` is now known to map to a fixed ``*_done_at`` column.
    out = _upsert(db_path, date_str, **{phase + "_done_at": _now()})

    # Keep a separate audit trail of phase completion.
    audit("daily_cycle_stamp", date=date_str, phase=phase)
    return out


def get_cycle(db_path, date_str):
    """Return a date's deserialized cycle, or ``None`` when it has no row."""
    # Reads also initialize or migrate the schema for first-run callers.
    _ensure(db_path)

    # Select by column name so schema order is not behaviorally significant.
    with sqlite3.connect(str(db_path)) as connection:
        row = connection.execute(
            "SELECT date,top3,tomorrow_top3,personal_top2,morning_done_at,"
            "eod_done_at,updated_at FROM daily_cycle WHERE date=?",
            (date_str,)).fetchone()

    if row is None:
        return None

    # Convert JSON-backed list fields to the Python values callers expect.
    return {"date": row[0], "top3": json.loads(row[1]),
            "tomorrow_top3": json.loads(row[2]),
            "personal_top2": json.loads(row[3]),
            "morning_done_at": row[4], "eod_done_at": row[5],
            "updated_at": row[6]}


def main():
    """Parse and execute one daily-cycle command, printing JSON output."""


    # Define the shared command parser and its operation-specific arguments.
    ap = argparse.ArgumentParser(description="Captain daily cycle store")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # Set-top-3 and Set-tomorrow
    for name in ("set-top3", "set-tomorrow"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=str(DEFAULT_DB))
        p.add_argument("--date", required=True)
        p.add_argument("--items", required=True, help="JSON array of strings")

    # Stamp
    p_stamp = sub.add_parser("stamp")
    p_stamp.add_argument("--db", default=str(DEFAULT_DB))
    p_stamp.add_argument("--date", required=True)
    p_stamp.add_argument("--phase", required=True, choices=PHASES)

    # Get
    p_get = sub.add_parser("get")
    p_get.add_argument("--db", default=str(DEFAULT_DB))
    p_get.add_argument("--date", required=True)

    # Parse args
    args = ap.parse_args()

    # Dispatch to the Python helper matching the selected subcommand.
    try:
        if args.cmd == "set-top3":
            print(json.dumps(set_top3(args.db, args.date, json.loads(args.items)), indent=2))
        elif args.cmd == "set-tomorrow":
            result = set_tomorrow_top3(args.db, args.date,json.loads(args.items))
            print(json.dumps(result, indent=2))
        elif args.cmd == "stamp":
            print(json.dumps(stamp(args.db, args.date, args.phase), indent=2))
        else:
            print(json.dumps(get_cycle(args.db, args.date), indent=2))

    # Invalid JSON and ordinary filesystem errors become concise command errors.
    # SQLite operational failures remain unexpected and reach the telemetry guard.
    except (ValueError, OSError) as err:
        msg = str(err) if isinstance(err, OSError) else (err.args[0] if err.args else str(err))
        raise SystemExit(str(msg)) from err


# Entry point
if __name__ == "__main__":
    with captain_telemetry.guard("daily_cycle"):
        main()
