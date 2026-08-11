#!/usr/bin/env python3
"""Read and update Captain's persisted operating modes.

The ``DailyLoop`` mode controls whether scheduled project-management actions
are disabled, previewed in shadow mode, or sent live. Every successful change
is written atomically and recorded in Captain's audit log.
"""

# Requirements
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import captain_telemetry

# Shared storage paths
ROOT = Path(__file__).resolve().parents[1]
MODE_PATH = ROOT / "data" / "captain-modes.json"
sys.path.insert(0, str(ROOT / "scripts"))

# Shared database and audit functions
from captain_db import audit, init_db  # noqa: E402

# Slack users allowed to toggle DailyLoop, mapped from user ID to display name.
AUTHORIZED_TOGGLE_USERS = {
    "SLACK_USER_ID": "Name",
    "SLACK_USER_ID": "Name",
}

# -------- Helper functions --------

def now_iso():
    """Return the current time in a standard, timezone-aware text format."""
    return datetime.now(timezone.utc).isoformat()


def load_modes():
    """Read Captain's saved operating modes, or return an empty setup."""
    # A missing file represents a workspace with no modes configured yet.
    if MODE_PATH.exists():
        return json.loads(MODE_PATH.read_text(encoding="utf-8"))

    return {}


def save_modes(modes):
    """Atomically replace the saved operating-mode file with new settings.

    Input example: {"DailyLoop": {"audience": "live", "updated_at": "2023-10-01T12:00:00Z"}}
    Returns: None
    
    """

    # Write beside the destination so the final replace remains atomic.
    MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(modes, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Atomic replace
    tmp.replace(MODE_PATH)


DAILYLOOP_AUDIENCES = ("off", "shadow", "live")


def set_dailyloop(audience, user_id, source):
    """Set DailyLoop to off, shadow, or live, validating any supplied user.

    The CLI always supplies a Slack user ID. Direct internal calls may pass a
    falsey ID, which records an unattributed change. Returns the complete mode
    document after saving and auditing the change.
    """

    # Input Validation: Reject invalid modes.
    if audience not in DAILYLOOP_AUDIENCES:
        raise SystemExit(
            "Invalid DailyLoop audience: %s (choose from %s)"
            % (audience, ", ".join(DAILYLOOP_AUDIENCES))
        )

    # Ensure user ID is authorized.
    if user_id and user_id not in AUTHORIZED_TOGGLE_USERS:
        raise SystemExit("Unauthorized DailyLoop toggle user: %s" % user_id)

    # Preserve unrelated modes while replacing the DailyLoop record.
    modes = load_modes()
    modes["DailyLoop"] = {
        "audience": audience,
        "updated_at": now_iso(),
        "updated_by_slack_user": user_id,
        "updated_by": AUTHORIZED_TOGGLE_USERS.get(user_id) if user_id else None,
    }

    # Persist first, then ensure audit storage exists and record the change.

    init_db() # Ensure the database and audit log are initialized. 
    save_modes(modes) # Save the updated modes to the file
    audit(
        "captain_mode_toggle",
        mode="DailyLoop",
        audience=audience,
        source=source,
        slack_user=user_id,
    ) # Record the mode change in the audit log

    return modes

# -------- Main function --------

def main():
    """Show the current modes or apply a requested daily-loop mode change."""

    # Define read-only status and authorized DailyLoop update commands.

    # Initialize the argument parser with a description of the script's functionality.
    parser = argparse.ArgumentParser(
        description="Read or update persisted Captain modes"
    ) 
    subparsers = parser.add_subparsers(dest="cmd", required=True)


    # Add status command
    subparsers.add_parser("status")

    # Add dailyloop command
    dailyloop_parser = subparsers.add_parser("dailyloop")
    dailyloop_parser.add_argument(
        "--audience", required=True, choices=DAILYLOOP_AUDIENCES
    )
    dailyloop_parser.add_argument(
        "--user-id",
        required=True,
        help="Slack user id requesting the toggle",
    )
    dailyloop_parser.add_argument("--source", default="manual")

    # Parse the command-line arguments and store them in the 'args' variable.
    args = parser.parse_args()

    # Status prints the complete persisted mode document without changing it.
    if args.cmd == "status":
        print(json.dumps(load_modes(), indent=2, sort_keys=True))
        return

    # The setter performs validation, persistence, and audit logging.
    if args.cmd == "dailyloop":
        modes = set_dailyloop(args.audience, args.user_id, args.source)
        print(json.dumps(modes, indent=2))
        return


# The outer telemetry guard ensures that any uncaught exception is reported to Sentry
if __name__ == "__main__":
    with captain_telemetry.guard("captain_modes"):
        main()
