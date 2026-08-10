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

# User IDs of Slack users who are allowed to toggle DailyLoop modes, mapped to their names.
# Loop options: off, shadow, live.
AUTHORIZED_TOGGLE_USERS = {
    "SLACK_USER_ID": "Name",
    "SLACK_USER_ID": "Name",
}


def now_iso():
    """Return the current time in a standard, timezone-aware text format."""
    return datetime.now(timezone.utc).isoformat()


def load_modes():
    """Read Captain's saved operating modes, or return an empty setup."""
    if MODE_PATH.exists():
        return json.loads(MODE_PATH.read_text(encoding="utf-8"))
    return {}


def save_modes(modes):
    """Safely replace the saved operating-mode file with new settings."""
    MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(modes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(MODE_PATH)


DAILYLOOP_AUDIENCES = ("off", "shadow", "live")


def set_dailyloop(audience, user_id, source):
    """Set the daily loop to off, preview-only, or live for an authorized user."""
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
    """Show the current modes or apply a requested daily-loop mode change."""
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
