#!/usr/bin/env python3
"""Download ClickUp tasks to a local JSON snapshot without changing ClickUp.

The script reads ClickUp credentials through ``clickup_credentials`` and fetches
every page of tasks, including subtasks. Set ``CAPTAIN_CLICKUP_LIST_IDS`` to a
comma-separated list when only particular lists should be fetched; otherwise,
the script fetches tasks for the configured ClickUp team.

Example:
    python3 scripts/fetch_clickup_tasks.py --out data/clickup-tasks.json
"""

# Requirements

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import captain_telemetry
from clickup_credentials import MissingClickUpCredentials, load_clickup_credentials

# API endpoint for ClickUp tasks
API = "https://api.clickup.com/api/v2"


# ClickUp API helper


def req(url, token):
    """Request and decode one page of task data from ClickUp.

    Example input: url="https://api.clickup.com/api/v2/team/123/task?page=0"
    Example output: {"tasks": [...], "last_page": True}
    """

    # Request the URL with the provided token in the Authorization header.
    request = urllib.request.Request(url, headers={"Authorization": token}) 

    # Bound the network request so an unavailable API cannot hang the script.
    with urllib.request.urlopen(request, timeout=30) as resp:
        return json.loads(resp.read().decode())


# Command-line workflow


def main():
    """Download all requested ClickUp tasks and save them to a JSON file."""

    # Start argument parser
    parser = argparse.ArgumentParser(
        description=(
            "Fetch ClickUp tasks read-only, always including subtasks and "
            "pagination"
        )
    )

    # Add arguments for output file and closed tasks inclusion
    parser.add_argument("--out", required=True)
    parser.add_argument("--include-closed", action="store_true")

    # Parse args
    args = parser.parse_args()

    # Load both values required to authenticate and choose the team endpoint.
    try:
        credentials = load_clickup_credentials(
            ("CLICKUP_API_KEY", "CLICKUP_TEAM_ID")
        )
    except MissingClickUpCredentials as error:
        raise SystemExit(str(error))

    # Extract API key and team ID from the loaded credentials.
    token = credentials["CLICKUP_API_KEY"]
    team = credentials["CLICKUP_TEAM_ID"]

    # Prefer explicitly configured lists; otherwise fetch across the whole team.
    list_ids = [
        item.strip() # Remove whitespace from each list ID
        for item in os.environ.get("CAPTAIN_CLICKUP_LIST_IDS", "").split(",") # Get list IDs from environment variable and split by comma
        if item.strip() # Keep only non-empty strings
    ]

    # Initialize an empty list to hold all fetched tasks.
    all_tasks = []


    if list_ids:
        bases = [f"{API}/list/{list_id}/task" for list_id in list_ids] # Build a task URL for each configured list ID
    else:
        bases = [f"{API}/team/{team}/task"] # Build a single task URL scoped to the team ID

    # Fetch every page for each selected list or for the configured team.
    for base in bases:
        page = 0

        while True:
            query = {
                "subtasks": "true",
                "include_closed": str(args.include_closed).lower(),
                "page": str(page),
            }
            data = req(base + "?" + urllib.parse.urlencode(query), token)
            all_tasks.extend(data.get("tasks", []))

            if data.get("last_page", True):
                break

            page += 1
            time.sleep(0.7)

    # Create the destination directory, then write one complete JSON snapshot.
    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"tasks": all_tasks}, indent=2),
        encoding="utf-8",
    )

    # Print a machine-readable summary for callers and scheduled jobs.
    print(json.dumps({"out": str(out), "tasks": len(all_tasks)}, indent=2))


if __name__ == "__main__":
    with captain_telemetry.guard("fetch_clickup_tasks"):
        main()
