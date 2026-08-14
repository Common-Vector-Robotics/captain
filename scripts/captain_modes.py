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
CHANNELS_PATH = ROOT / "data" / "captain-channels.json"
sys.path.insert(0, str(ROOT / "scripts"))

# Shared database and audit functions
from captain_db import audit, init_db  # noqa: E402

# -------- Helper functions --------

def now_iso():
    """Return the current time in a standard, timezone-aware text format."""
    return datetime.now(timezone.utc).isoformat()


def load_toggle_users():
    """Load private DailyLoop operators, keyed by Slack user ID."""
    try:
        config = json.loads(CHANNELS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Cannot load mode_toggle_users: {error}") from error

    if not isinstance(config, dict):
        raise SystemExit("mode_toggle_users must be a non-empty name-to-Slack-ID object")
    raw = config.get("mode_toggle_users")
    if not isinstance(raw, dict) or not raw:
        raise SystemExit("mode_toggle_users must be a non-empty name-to-Slack-ID object")

    users = {}
    for name, user_id in raw.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(user_id, str)
            or not user_id.strip()
        ):
            raise SystemExit("mode_toggle_users contains an invalid name or Slack ID")
        if user_id in users:
            raise SystemExit(f"mode_toggle_users repeats Slack ID {user_id}")
        users[user_id] = name
    return users


def load_modes():
    """Read Captain's saved operating modes, or return an empty setup."""
    # A missing file represents a workspace with no modes configured yet.
    if MODE_PATH.exists():
        return json.loads(MODE_PATH.read_text(encoding="utf-8"))

    return {}


def save_modes(modes):
    """Atomically replace the saved operating-mode file with owner-only access.

    Input example: {"DailyLoop": {"audience": "live", "updated_at": "2023-10-01T12:00:00Z"}}
    Returns: None
    """

    MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MODE_PATH.with_suffix(".tmp")
    temporary.touch(mode=0o600, exist_ok=False)
    try:
        temporary.write_text(
            json.dumps(modes, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(MODE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


DAILYLOOP_AUDIENCES = ("off", "shadow", "live")


def set_dailyloop(audience, user_id, source):
    """Set DailyLoop to off, shadow, or live for a configured Slack operator.

    The single audit row is precommit authorization, not proof that persistence
    completed. It is appended before the atomic mode-file replacement so an
    audit failure can never leave an unaudited mode active. The mode file is the
    authoritative outcome: if its later replacement fails, the row remains a
    documented but uncommitted attempt.
    """

    # Input Validation: Reject invalid modes.
    if audience not in DAILYLOOP_AUDIENCES:
        raise SystemExit(
            "Invalid DailyLoop audience: %s (choose from %s)"
            % (audience, ", ".join(DAILYLOOP_AUDIENCES))
        )

    authorized = load_toggle_users()
    if not user_id or user_id not in authorized:
        raise SystemExit(f"Unauthorized DailyLoop toggle user: {user_id}")

    # Preserve unrelated modes while replacing the DailyLoop record.
    modes = load_modes()
    modes["DailyLoop"] = {
        "audience": audience,
        "updated_at": now_iso(),
        "updated_by_slack_user": user_id,
        "updated_by": authorized[user_id],
    }

    init_db()  # Ensure the database and audit log are initialized.
    audit(
        "captain_mode_toggle",
        mode="DailyLoop",
        audience=audience,
        source=source,
        slack_user=user_id,
        display_name=authorized[user_id],
        phase="precommit",
        state_authoritative=True,
        authoritative_state="mode_file",
    )  # Record the authorized mode-change attempt in the audit log.
    save_modes(modes)  # Activate the mode only after auditing succeeds.

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
