#!/usr/bin/env python3
"""Captain persisted mode controls."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
MODE_PATH = ROOT / "data" / "captain-modes.json"
sys.path.insert(0, str(ROOT / "scripts"))
from captain_db import audit, init_db  # noqa: E402

AUTHORIZED_TOGGLE_USERS = {
    "U0B4G00QXT8": "Gavin",
    "U043AKSJC85": "Arnold",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_modes():
    if MODE_PATH.exists():
        return json.loads(MODE_PATH.read_text(encoding="utf-8"))
    return {}


def save_modes(modes):
    MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(modes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(MODE_PATH)


DAILYLOOP_AUDIENCES = ("off", "shadow", "live")


def set_dailyloop(audience, user_id, source):
    if audience not in DAILYLOOP_AUDIENCES:
        raise SystemExit("Invalid DailyLoop audience: %s (choose from %s)"
                         % (audience, ", ".join(DAILYLOOP_AUDIENCES)))
    if user_id and user_id not in AUTHORIZED_TOGGLE_USERS:
        raise SystemExit("Unauthorized DailyLoop toggle user: %s" % user_id)
    modes = load_modes()
    modes["DailyLoop"] = {
        "audience": audience,
        "updated_at": now_iso(),
        "updated_by_slack_user": user_id,
        "updated_by": AUTHORIZED_TOGGLE_USERS.get(user_id) if user_id else None,
    }
    save_modes(modes)
    init_db()
    audit("captain_mode_toggle", mode="DailyLoop", audience=audience,
          source=source, slack_user=user_id)
    return modes


def main():
    ap = argparse.ArgumentParser(description="Read or update persisted Captain modes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p_dl = sub.add_parser("dailyloop")
    p_dl.add_argument("--audience", required=True, choices=DAILYLOOP_AUDIENCES)
    p_dl.add_argument("--user-id", required=True, help="Slack user id requesting the toggle")
    p_dl.add_argument("--source", default="manual")
    args = ap.parse_args()
    if args.cmd == "status":
        print(json.dumps(load_modes(), indent=2, sort_keys=True))
        return
    if args.cmd == "dailyloop":
        print(json.dumps(set_dailyloop(args.audience, args.user_id, args.source), indent=2))
        return


if __name__ == "__main__":
    with captain_telemetry.guard("captain_modes"):
        main()
