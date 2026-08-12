#!/usr/bin/env python3
"""Discover and score Captain critical paths from ClickUp.

This script is read-only against ClickUp. It writes only local Captain state when
`write-state` is used. Critical-path state is intentionally separate from the
weekly check-in cron so the cron can fall back to legacy behavior if discovery
has not run or confidence is low.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
API_CLICKUP = "https://api.clickup.com/api/v2"
STATE_PATH = ROOT / "data" / "critical-paths.json"
OVERRIDES_PATH = ROOT / "data" / "critical-path-overrides.json"

sys.path.insert(0, str(ROOT / "scripts"))
from captain_db import audit  # noqa: E402


# Configuration and ClickUp access


def now_iso():
    """Return the current time in a standard, timezone-aware text format."""
    return datetime.now(timezone.utc).isoformat()


def normalize(s):
    """Normalize text for case- and punctuation-insensitive comparisons.

    Example input: "Customer Demo: Phase 1"
    Example output: "customer demo phase 1"
    """
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def load_env_file(path):
    """Load missing process settings from a simple local environment file."""
    env_path = Path(path).expanduser()

    # This optional file contributes nothing when it is absent.
    if not env_path.exists():
        return

    # Existing environment variables win over file values through setdefault.
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def get_clickup_credentials():
    """Return ClickUp credentials or exit with all missing setting names."""
    # Local secrets fill only values not already present in the environment.
    load_env_file(ROOT / ".secrets" / "clickup.env")
    token = os.environ.get("CLICKUP_API_KEY")
    team = os.environ.get("CLICKUP_TEAM_ID")

    # Report missing values together so setup takes one correction cycle.
    missing = []
    if not token:
        missing.append("CLICKUP_API_KEY")
    if not team:
        missing.append("CLICKUP_TEAM_ID")
    if missing:
        raise SystemExit("Missing required credentials: " + ", ".join(missing))

    return token, team


def http_json(method, url, headers=None, payload=None, timeout=45):
    """Send one HTTP request and decode its JSON response body."""
    # Encode request data only when the caller supplied a JSON payload.
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=headers or {},
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")

    return json.loads(body) if body else {}


def clickup_req(token, method, path, payload=None):
    """Send an authenticated request to a ClickUp API path."""
    return http_json(
        method,
        API_CLICKUP + path,
        headers={"Authorization": token, "Content-Type": "application/json"},
        payload=payload,
    )


# ClickUp task normalization


def status_name(task):
    """Return the human-readable status name for a task."""
    status = task.get("status") or {}
    return status.get("status") if isinstance(status, dict) else str(status)


def status_type(task):
    """Return ClickUp's broader status category for a task."""
    status = task.get("status") or {}
    return status.get("type") if isinstance(status, dict) else ""


def is_open_task(task):
    """Tell whether a ClickUp task still needs work."""
    status_category = (status_type(task) or "").lower()
    status_label = (status_name(task) or "").lower()

    # ClickUp installations can express completion in either status field.
    return (
        status_category not in {"closed", "done", "complete"}
        and status_label not in {"closed", "complete", "completed", "done"}
    )


def fetch_clickup_tasks(token, team_id):
    """Download every open task, including subtasks and additional pages."""
    # Optional list IDs narrow discovery; otherwise query the whole team.
    list_ids = [
        value.strip()
        for value in os.environ.get("CAPTAIN_CLICKUP_LIST_IDS", "").split(",")
        if value.strip()
    ]
    bases = (
        [f"/list/{list_id}/task" for list_id in list_ids]
        if list_ids
        else [f"/team/{team_id}/task"]
    )
    tasks = []

    # Fetch every page from every selected task collection.
    for base in bases:
        page = 0
        while True:
            query = urllib.parse.urlencode(
                {
                    "subtasks": "true",
                    "include_closed": "false",
                    "page": str(page),
                }
            )
            data = clickup_req(token, "GET", f"{base}?{query}")
            tasks.extend(data.get("tasks") or [])

            if data.get("last_page", True):
                break

            page += 1
            time.sleep(0.7)

    # Defensively filter closed tasks even though the API request excludes them.
    return [task for task in tasks if is_open_task(task)]


def due_dt(task):
    """Convert a ClickUp millisecond due date to UTC, or return ``None``."""
    due = task.get("due_date")
    if not due:
        return None

    # Malformed remote values should make the task undated, not stop discovery.
    try:
        return datetime.fromtimestamp(int(due) / 1000, timezone.utc)
    except Exception:
        return None


def priority_name(task):
    """Return a task's priority name in a consistent lowercase form."""
    priority = task.get("priority") or {}

    if isinstance(priority, dict):
        return (priority.get("priority") or priority.get("name") or "").lower()

    return str(priority or "").lower()


def task_list_id(task):
    """Return the ClickUp list identifier that contains the task."""
    task_list = task.get("list") or {}
    return str(task_list.get("id") or task.get("list_id") or "")


def task_list_name(task):
    """Return the name of the ClickUp list that contains the task."""
    task_list = task.get("list") or {}
    return task_list.get("name") or "Unlisted"


def task_space_id(task):
    """Return the ClickUp space identifier that contains the task."""
    space = task.get("space") or {}
    return str(space.get("id") or "")


def task_space_name(task):
    """Return the name of the ClickUp space that contains the task."""
    space = task.get("space") or {}
    return space.get("name") or "Unknown space"


def task_project_key(task):
    """Choose a stable project grouping and readable label for a task."""
    # A top-level parent groups related subtasks more precisely than their list.
    parent = task.get("top_level_parent") or task.get("parent")
    if parent:
        return f"parent:{parent}", f"Parent {parent}"

    # Without a parent, the ClickUp list provides the next-best project boundary.
    list_id = task_list_id(task)
    if list_id:
        return f"list:{list_id}", f"{task_space_name(task)} / {task_list_name(task)}"

    # Keep tasks with incomplete location metadata visible in one fallback group.
    return "workspace:unscoped", "Unscoped tasks"


def task_risk_signals(task, now):
    """Score a task's delivery risk and list the facts that raised the score."""
    signals = []
    score = 0

    # Due-date pressure contributes most strongly to risk.
    due = due_dt(task)
    if due:
        days = (due.date() - now.date()).days
        if days < 0:
            signals.append(f"overdue:{abs(days)}d")
            score += 40
        elif days <= 14:
            signals.append(f"due_soon:{days}d")
            score += max(5, 25 - days)

    # ClickUp priority adds an explicit human signal to the inferred score.
    priority = priority_name(task)
    if priority in {"urgent", "high"}:
        signals.append(f"priority:{priority}")
        score += 18 if priority == "urgent" else 12

    # Dependencies and subtasks indicate coordination or remaining work.
    if task.get("dependencies") or task.get("linked_tasks"):
        signals.append("has_dependencies")
        score += 10
    if task.get("subtasks"):
        signals.append("has_subtasks")
        score += 6

    # Delivery-language keywords raise tasks likely tied to a visible milestone.
    name = normalize(task.get("name"))
    if re.search(
        r"\b(ship|demo|customer|release|launch|milestone|block|critical|urgent|"
        r"deadline)\b",
        name,
    ):
        signals.append("strategic_keyword")
        score += 8

    # Ownerless work is risky unless an owner custom field supplies the missing
    # data. Check custom fields only for ownerless tasks, preserving the original
    # short-circuit when an assignee already establishes ownership.
    if not task.get("assignees"):
        has_owner_field = any(
            normalize(field.get("name")) in {"owner", "owners"}
            and field.get("value")
            for field in task.get("custom_fields") or []
        )
        if not has_owner_field:
            signals.append("missing_owner")
            score += 6

    return score, signals


def load_overrides(path=OVERRIDES_PATH):
    """Read optional human choices that include, exclude, or regroup tasks."""
    # Absence means discovery should proceed using inference alone.
    if not path.exists():
        return {"force_include_task_ids": [], "force_exclude_task_ids": [], "paths": []}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("override file must contain an object")

        # Fill optional collections so downstream discovery can iterate directly.
        data.setdefault("force_include_task_ids", [])
        data.setdefault("force_exclude_task_ids", [])
        data.setdefault("paths", [])
        return data
    except Exception as exc:
        # A malformed human override must be visible rather than silently ignored.
        raise SystemExit(f"Invalid {path}: {exc}")


def discover_paths(tasks, max_paths=5, overrides=None):
    """Group risky tasks into the highest-scoring delivery paths to watch.

    Automatic grouping uses a task's top-level parent or ClickUp list. Human
    overrides may exclude tasks, force their inclusion, or define custom paths.
    """
    # Normalize inputs and build a lookup for validating human overrides.
    overrides = overrides or {}
    now = datetime.now(timezone.utc)
    by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
    excluded = {
        str(task_id)
        for task_id in overrides.get("force_exclude_task_ids") or []
    }
    forced = {
        str(task_id)
        for task_id in overrides.get("force_include_task_ids") or []
    }
    groups = {}

    # Score and group every eligible task from the ClickUp dataset.
    for task in tasks:
        task_id = str(task.get("id") or "")
        if not task_id or task_id in excluded:
            continue

        key, label = task_project_key(task)
        score, signals = task_risk_signals(task, now)

        # A forced task receives enough weight to survive normal filtering.
        if task_id in forced:
            signals.append("admin_force_include")
            score += 100

        if score <= 0:
            continue

        group = groups.setdefault(
            key,
            {
                "key": key,
                "name": label,
                "tasks": [],
                "score": 0,
                "signals": set(),
                "source": "inferred",
            },
        )
        group["tasks"].append(task)
        group["score"] += score
        group["signals"].update(signals)

    # Surface override IDs that no longer match an available open task.
    invalid_override_task_ids = sorted((forced | excluded) - set(by_id.keys()))

    # Add or augment explicitly defined human paths after inferred grouping.
    for idx, override in enumerate(overrides.get("paths") or []):
        task_ids = [
            str(task_id)
            for task_id in override.get("task_ids") or []
            if str(task_id) in by_id and str(task_id) not in excluded
        ]
        if not task_ids:
            continue

        key = f"override:{override.get('id') or idx}"
        group = groups.setdefault(
            key,
            {
                "key": key,
                "name": override.get("name") or f"Override path {idx + 1}",
                "tasks": [],
                "score": 0,
                "signals": set(),
                "source": "admin_override",
            },
        )

        existing = {str(task.get("id")) for task in group["tasks"]}
        for task_id in task_ids:
            if task_id not in existing:
                group["tasks"].append(by_id[task_id])

        group["score"] += int(override.get("score_boost") or 100)
        group["signals"].add("admin_override")

        if override.get("priority"):
            group["priority_override"] = override.get("priority")
        if override.get("target_date"):
            group["target_date_override"] = override.get("target_date")

    # Convert internal groups into stable, serializable path records.
    paths = []
    for key, group in groups.items():
        # Within a path, dated tasks come first, then name breaks ties.
        sorted_tasks = sorted(
            group["tasks"],
            key=lambda task: (
                due_dt(task) is None,
                int(task.get("due_date") or 0),
                task.get("name") or "",
            ),
        )
        task_ids = [
            str(task.get("id")) for task in sorted_tasks if task.get("id")
        ]
        due_dates = [
            due_dt(task).date().isoformat()
            for task in sorted_tasks
            if due_dt(task)
        ]

        # Human values win; inferred priority follows aggregate group score.
        priority = group.get("priority_override") or (
            "critical"
            if group["score"] >= 90
            else "high"
            if group["score"] >= 45
            else "normal"
        )

        dependency_task_ids = sorted(
            {
                str(dependency.get("task_id") or dependency.get("id"))
                for task in sorted_tasks
                for dependency in (task.get("dependencies") or [])
                if dependency.get("task_id") or dependency.get("id")
            }
        )

        # Evidence is intentionally capped to keep the state readable and bounded.
        evidence = [
            {
                "task_id": str(task.get("id")),
                "name": task.get("name"),
                "status": status_name(task),
                "due_date": (
                    due_dt(task).date().isoformat() if due_dt(task) else None
                ),
                "url": task.get("url"),
                "list": task_list_name(task),
            }
            for task in sorted_tasks[:10]
        ]

        paths.append(
            {
                "id": re.sub(r"[^a-z0-9_:-]+", "-", key.lower()),
                "name": group["name"],
                "source": group.get("source") or "inferred",
                "priority": priority,
                "target_date": group.get("target_date_override")
                or (min(due_dates) if due_dates else None),
                "task_ids": task_ids[:25],
                "dependency_task_ids": dependency_task_ids,
                "risk_signals": sorted(group["signals"]),
                "score": group["score"],
                "confidence": (
                    0.9
                    if group.get("source") == "admin_override"
                    else min(0.85, 0.45 + (group["score"] / 120))
                ),
                "last_evaluated_at": now_iso(),
                "evidence": evidence,
            }
        )

    # Highest score wins; target date and name provide deterministic tie-breaks.
    paths = sorted(
        paths,
        key=lambda path: (
            -path["score"],
            path.get("target_date") or "9999-12-31",
            path["name"],
        ),
    )[:max_paths]

    return {
        "version": 1,
        "generated_at": now_iso(),
        "max_paths": max_paths,
        "paths": paths,
        "invalid_override_task_ids": invalid_override_task_ids,
    }


def write_state(state):
    """Atomically save the discovered critical-path state."""
    # Write beside the destination so replacement cannot expose partial JSON.
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def print_summary(state):
    """Print the discovered path document as stable, readable JSON."""
    print(json.dumps(state, indent=2, sort_keys=True))


def main():
    """Fetch or load tasks, discover critical paths, and print or save the result."""


    # Start arg parser
    parser = argparse.ArgumentParser(
        description="Discover Captain critical paths from ClickUp"
    )

    # Command
    parser.add_argument("command", choices=["discover", "score", "write-state"])

    # Max-Paths
    parser.add_argument(
        "--max-paths",
        type=int,
        default=int(os.environ.get("CAPTAIN_MAX_CRITICAL_PATHS", "5")),
    )

    # Parse Args
    args = parser.parse_args()

    # Fetch current ClickUp truh
    token, team_id = get_clickup_credentials()
    tasks = fetch_clickup_tasks(token, team_id)

    # Apply human overrides
    overrides = load_overrides()

    # Discover and score critical paths
    state = discover_paths(tasks, max_paths=args.max_paths, overrides=overrides)

    # Only write-state mutates local state; discover and score remain read-only.
    if args.command == "write-state":
        write_state(state) # Write the discovered critical paths to the local state file

        # Audit the write-state operation for telemetry and monitoring purposes
        audit(
            "critical_paths_state_written",
            source="scripts/critical_paths.py",
            path=str(STATE_PATH.relative_to(ROOT)),
            path_count=len(state.get("paths") or []),
            task_count=sum(
                len(path.get("task_ids") or [])
                for path in state.get("paths") or []
            ),

            # Log any invalid override task IDs that were detected during discovery
            invalid_override_task_ids=state.get("invalid_override_task_ids") or [],
        )

    # Print the discovered critical paths to stdout for visibility and debugging
    print_summary(state)

# Run within telemetry guard
if __name__ == "__main__":
    with captain_telemetry.guard("critical_paths"):
        main()
