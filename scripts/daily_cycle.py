#!/usr/bin/env python3
"""Captain daily cycle store: per-date top-3 and phase stamps."""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from captain_db import DB as DEFAULT_DB, audit  # noqa: E402

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
# Columns added after this table's original six. `CREATE TABLE IF NOT EXISTS`
# above is a no-op against the database already live on the OpenClaw host, so a
# new column has to be ALTERed in. Listed last in SCHEMA deliberately, so a
# freshly created database and a migrated one end up with identical column
# order. `get_cycle` selects by name, so order is not load-bearing either way.
ADDED_COLUMNS = (
    ("personal_top2", "TEXT NOT NULL DEFAULT '[]'"),
)
PHASES = ("morning", "eod")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as c:
        c.executescript(SCHEMA)
        existing = {row[1] for row in c.execute("PRAGMA table_info(daily_cycle)")}
        for name, decl in ADDED_COLUMNS:
            if name in existing:
                continue
            # name/decl come from the ADDED_COLUMNS literal above, never user
            # input -- same reasoning as the interpolated UPDATE in _upsert.
            c.execute("ALTER TABLE daily_cycle ADD COLUMN %s %s" % (name, decl))


def _upsert(db_path, date_str, **updates):
    _ensure(db_path)
    t = _now()
    with sqlite3.connect(str(db_path)) as c:
        c.execute("INSERT INTO daily_cycle(date, updated_at) VALUES(?, ?) "
                  "ON CONFLICT(date) DO NOTHING", (date_str, t))
        for k, v in updates.items():
            # k is always from fixed internal literals (top3, tomorrow_top3,
            # personal_top2, morning_done_at, eod_done_at); never user input
            c.execute("UPDATE daily_cycle SET %s=?, updated_at=? WHERE date=?" % k,
                      (v, t, date_str))
    return get_cycle(db_path, date_str)


def set_top3(db_path, date_str, items):
    out = _upsert(db_path, date_str, top3=json.dumps(list(items)))
    audit("daily_cycle_top3_set", date=date_str, count=len(items))
    return out


def set_tomorrow_top3(db_path, date_str, items):
    out = _upsert(db_path, date_str, tomorrow_top3=json.dumps(list(items)))
    audit("daily_cycle_tomorrow_top3_set", date=date_str, count=len(items))
    return out


def set_personal_top2(db_path, date_str, items):
    """Persist the per-person top-2 that was actually sent (or previewed).

    `items` is a list of `{slack_user_id, key, task_ids, overridden,
    override_reason}` dicts -- `key` is the board identity the ranking came from,
    kept so a stored row traces back to ClickUp independently of Slack
    resolution. Like every other daily_cycle_* audit row this is LOCAL state, so
    it is written in `shadow` as well as `live`; only `clickup_*` rows are
    suppressed in shadow."""
    out = _upsert(db_path, date_str, personal_top2=json.dumps(list(items)))
    audit("daily_cycle_personal_top2_set", date=date_str, count=len(items))
    return out


def stamp(db_path, date_str, phase):
    if phase not in PHASES:
        raise ValueError("invalid phase: %s (choose from %s)" % (phase, ",".join(PHASES)))
    out = _upsert(db_path, date_str, **{phase + "_done_at": _now()})
    audit("daily_cycle_stamp", date=date_str, phase=phase)
    return out


def get_cycle(db_path, date_str):
    _ensure(db_path)
    with sqlite3.connect(str(db_path)) as c:
        row = c.execute(
            "SELECT date,top3,tomorrow_top3,personal_top2,morning_done_at,"
            "eod_done_at,updated_at FROM daily_cycle WHERE date=?",
            (date_str,)).fetchone()
    if row is None:
        return None
    return {"date": row[0], "top3": json.loads(row[1]),
            "tomorrow_top3": json.loads(row[2]),
            "personal_top2": json.loads(row[3]),
            "morning_done_at": row[4], "eod_done_at": row[5],
            "updated_at": row[6]}


def main():
    ap = argparse.ArgumentParser(description="Captain daily cycle store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("set-top3", "set-tomorrow"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=str(DEFAULT_DB))
        p.add_argument("--date", required=True)
        p.add_argument("--items", required=True, help="JSON array of strings")
    p_st = sub.add_parser("stamp")
    p_st.add_argument("--db", default=str(DEFAULT_DB))
    p_st.add_argument("--date", required=True)
    p_st.add_argument("--phase", required=True, choices=PHASES)
    p_get = sub.add_parser("get")
    p_get.add_argument("--db", default=str(DEFAULT_DB))
    p_get.add_argument("--date", required=True)
    args = ap.parse_args()
    try:
        if args.cmd == "set-top3":
            print(json.dumps(set_top3(args.db, args.date, json.loads(args.items)), indent=2))
        elif args.cmd == "set-tomorrow":
            print(json.dumps(set_tomorrow_top3(args.db, args.date, json.loads(args.items)), indent=2))
        elif args.cmd == "stamp":
            print(json.dumps(stamp(args.db, args.date, args.phase), indent=2))
        else:
            print(json.dumps(get_cycle(args.db, args.date), indent=2))
    # OSError belongs here alongside ValueError: --db is a user-supplied path
    # and _ensure() mkdirs its parent and opens the file, so an unwritable
    # location must exit with the same clean one-line message a bad --items
    # payload gets, not a traceback.
    except (ValueError, OSError) as err:
        msg = str(err) if isinstance(err, OSError) else (err.args[0] if err.args else str(err))
        raise SystemExit(str(msg)) from err


if __name__ == "__main__":
    with captain_telemetry.guard("daily_cycle"):
        main()
