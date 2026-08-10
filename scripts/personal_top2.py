#!/usr/bin/env python3
"""Rank each person's open ClickUp tasks for Captain's morning cycle.

The morning cycle posts one team-wide top-3 brief; this is the per-person half.
It reads the same board fetch that brief is built from, so the two cannot
disagree about board state.

Ranking here is deliberately mechanical and testable. Critical-path membership,
due dates, ClickUp priority, and blocked status all come from structured data.
Judgment that needs context absent from the board -- such as company memory or
the previous Slack sweep -- belongs to the cron prompt. That prompt may reorder
a person's two items, but must record the override (see
``cron-prompts/daily-morning-cycle.md`` step 6b).

Examples::

    python3 scripts/personal_top2.py rank --clickup /tmp/tasks.json
    python3 scripts/personal_top2.py get --date 2026-08-10
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import captain_telemetry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from captain_db import DB as DEFAULT_DB  # noqa: E402
import blocker_ledger  # noqa: E402
import daily_context  # noqa: E402
import daily_cycle  # noqa: E402

# Owner resolution

# The ClickUp custom **labels** field carries owners who cannot be real ClickUp
# assignees. ``clickup_write.py`` implements the write side through ``--owner``
# and ``resolve_owner_field``.
OWNERS_FIELD_NAME = "owners"


def owner_label_names(task):
    """Return names selected in a task's ``Owners`` labels field.

    The task's existing ``custom_fields`` data contains both selected option
    IDs and their labels, so this lookup makes no ClickUp request. A missing or
    unset well-formed Owners field returns an empty list; structurally malformed
    field data surfaces as an error instead of being guessed around.

    Example output: ``["Gavin", "Jordan"]``
    """
    # Find the custom field named ``Owners`` without assuming field-name case.
    for field in task.get("custom_fields") or []:
        if (field.get("name") or "").strip().lower() != OWNERS_FIELD_NAME:
            continue

        # Only ClickUp label fields have the option shape used below.
        if (field.get("type") or "").strip().lower() != "labels":
            continue

        # Unset and malformed label selections contain no usable owners.
        value = field.get("value")
        if not isinstance(value, list):
            return []

        # Map selected option IDs back to the human-readable labels.
        options = {}
        for option in (field.get("type_config") or {}).get("options") or []:
            if option.get("id") is not None and option.get("label"):
                options[option["id"]] = option["label"]

        return [options[v] for v in value if v in options]

    return []


def assignee_identity(assignee):
    """Build a stable board identity for one ClickUp assignee.

    The grouping key prefers email because Slack member lookup accepts it. A
    username or numeric ClickUp ID provides a stable fallback. An assignee with
    none of those values returns ``None``.
    """
    # Normalize optional ClickUp fields before choosing a grouping key.
    email = (assignee.get("email") or "").strip() or None
    username = (assignee.get("username") or "").strip() or None
    user_id = assignee.get("id")

    # Prefer the identity that downstream Slack resolution can use directly.
    key = email or username or (str(user_id) if user_id is not None else None)
    if key is None:
        return None

    return {"key": key, "source": "assignee", "clickup_user_id": user_id,
            "username": username, "email": email}


def group_by_person(tasks):
    """Group open tasks under each person's board identity.

    A task with several assignees lands on each of their plates -- shared work
    genuinely is shared. Owners labels are used only when a task has no real
    ClickUp assignees; combining both sources would invent extra owners. Tasks
    with neither source yield no recipient because ``daily_context.py`` already
    surfaces those owner gaps in the team brief.
    """
    people = {}

    for task in tasks:
        # This helper remains safe when callers pass an unfiltered task list.
        if not daily_context.is_open(task):
            continue

        # Real ClickUp assignees are the authoritative ownership source.
        identities = [i for i in
                      (assignee_identity(a) for a in task.get("assignees") or [])
                      if i is not None]

        # Fall back to Owners labels only when no usable assignee exists.
        if not identities:
            identities = [{"key": name, "source": "owners_label",
                           "clickup_user_id": None, "username": None,
                           "email": None}
                          for name in owner_label_names(task)]

        # Shared tasks deliberately appear in every identified person's group.
        for identity in identities:
            person = people.setdefault(identity["key"], dict(identity, tasks=[]))
            person["tasks"].append(task)

    return people


# Ranking signals and tier rules

BLOCKED_STATUS = "blocked"
PRIORITY_URGENT = 1
PRIORITY_HIGH = 2
HOT_PATH_PRIORITIES = ("critical",)
WARM_PATH_PRIORITIES = ("critical", "high")
LOWEST_TIER = 6


def critical_path_index(critical_paths):
    """Map each task ID to its highest-scoring critical path.

    ``critical_paths.py`` records exact task membership, priority, and score.
    When a task appears on multiple paths, the highest score wins because that
    path has the largest documented cost of slippage.

    A non-mapping top level returns an empty index, and non-mapping path entries
    are skipped. Otherwise the function expects the documented path schema.
    """
    index = {}

    # A non-mapping top level disables path ranking; deeper data uses the
    # documented schema and may surface incompatible collection shapes.
    if not isinstance(critical_paths, dict):
        return index

    for path in critical_paths.get("paths") or []:
        if not isinstance(path, dict):
            continue

        # Normalize score so malformed values cannot abort the full ranking.
        try:
            score = int(path.get("score") or 0)
        except (TypeError, ValueError):
            score = 0

        # Keep only the path details needed for ranking and explanations.
        entry = {"name": path.get("name") or "",
                 "priority": (path.get("priority") or "normal").strip().lower(),
                 "score": score}

        # Retain the hottest known path for each task.
        for task_id in path.get("task_ids") or []:
            key = str(task_id)
            current = index.get(key)
            if current is None or entry["score"] > current["score"]:
                index[key] = entry

    return index


def blocked_task_ids(db_path):
    """Return ClickUp task IDs carrying an open ledger blocker.

    Blockers without a ClickUp task link are intentionally excluded because
    they cannot affect any task in this ranking.
    """
    return {str(b["clickup_task_id"])
            for b in blocker_ledger.open_blockers(db_path)
            if b.get("clickup_task_id")}


def is_stuck(task, blocked_ids):
    """Return whether ClickUp or the local ledger marks a task as stuck."""
    # The board's explicit status is sufficient even without a ledger entry.
    status, _ = daily_context.status_of(task)
    if status == BLOCKED_STATUS:
        return True

    # The ledger catches blockers represented outside the ClickUp status.
    return str(task.get("id")) in blocked_ids


def priority_value(task):
    """Return ClickUp priority as ``1`` (urgent) through ``4`` (low).

    ClickUp may return a nested priority object or omit the field. Missing and
    malformed values return ``None`` so ranking can continue without this
    optional signal.
    """
    # Accept the nested shape used by the ClickUp API as well as scalar fixtures.
    raw = task.get("priority")
    if isinstance(raw, dict):
        raw = raw.get("id") or raw.get("orderindex")

    # Treat an absent or non-numeric priority as an unavailable signal.
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def overdue_days(task, today):
    """Return whole days past due, or zero when a task is not overdue."""
    due = daily_context.due_local_date(task)
    if due is None or due >= today:
        return 0

    return (today - due).days


def _days_phrase(days):
    """Describe lateness using natural singular or plural wording."""
    return "1 day past due" if days == 1 else "%d days past due" % days


def tier_and_reason(task, today, path, stuck):
    """Assign a ranking tier and its plain-language explanation.

    Tier 1 is highest and tier 6 is lowest. Tiers are ordered rules rather than
    a weighted score so the result has one understandable reason. The employee
    text deliberately avoids the internal term "blocker," so stuck work is
    demoted without naming that signal.
    """
    # Derive every ranking signal once before applying the tier rules.
    due = daily_context.due_local_date(task)
    past_due = overdue_days(task, today)
    due_today = due is not None and due == today
    hot = path is not None and path["priority"] in HOT_PATH_PRIORITIES
    warm = path is not None and path["priority"] in WARM_PATH_PRIORITIES
    priority = priority_value(task)

    # Apply the documented tiers from strongest delivery signal to weakest.
    if hot and (past_due or due_today):
        when = "due today" if due_today else _days_phrase(past_due)
        tier, reason = 1, '%s, and on critical path "%s"' % (when, path["name"])
    elif past_due:
        tier, reason = 2, _days_phrase(past_due)
    elif due_today:
        tier, reason = 3, "due today"
    elif warm:
        tier, reason = 4, 'on critical path "%s"' % path["name"]
    elif priority in (PRIORITY_URGENT, PRIORITY_HIGH):
        label = "urgent" if priority == PRIORITY_URGENT else "high priority"
        tier, reason = 5, "marked %s in ClickUp" % label
    elif due is None:
        tier, reason = LOWEST_TIER, "open, no due date set"
    else:
        tier, reason = LOWEST_TIER, "due %s" % due.isoformat()

    # Stuck work moves down one tier, capped at tier 6; the reason stays safe.
    if stuck:
        tier = min(tier + 1, LOWEST_TIER)

    return tier, reason


NO_DUE_SORT = "9999-12-31"
NO_PRIORITY_SORT = 99


def rank_person(person, today, path_index, blocked_ids):
    """Rank one person's open tasks and identify their top two IDs.

    Sort key, in order: tier; more overdue first; earliest due date (undated
    last); highest ClickUp priority (unprioritized last); highest critical-path
    score; then task ID. Task ID makes the order deterministic when all delivery
    signals tie, regardless of ClickUp pagination order.
    """
    ranked = []

    # Turn each task's delivery signals into one deterministic sort key.
    for task in person["tasks"]:
        task_id = str(task.get("id"))
        path = path_index.get(task_id)
        stuck = is_stuck(task, blocked_ids)
        tier, reason = tier_and_reason(task, today, path, stuck)
        due = daily_context.due_local_date(task)
        priority = priority_value(task)
        sort_key = (
            tier,
            -overdue_days(task, today),
            due.isoformat() if due is not None else NO_DUE_SORT,
            priority if priority is not None else NO_PRIORITY_SORT,
            -(path["score"] if path else 0),
            task_id,
        )

        # Keep the readable candidate data beside its internal sort key.
        ranked.append((sort_key, {
            "task_id": task_id,
            "name": task.get("name") or "",
            "url": task.get("url"),
            "due": due.isoformat() if due is not None else None,
            "tier": tier,
            "stuck": stuck,
            "reason": reason,
        }))

    # Sort once, then remove the private keys from the returned representation.
    ranked.sort(key=lambda pair: pair[0])
    candidates = [candidate for _, candidate in ranked]

    # Preserve identity metadata while replacing raw tasks with ranked results.
    out = {k: v for k, v in person.items() if k != "tasks"}
    out["candidates"] = candidates
    out["top2"] = [c["task_id"] for c in candidates[:2]]

    return out


# Input loading and report assembly

def _load_tasks(clickup_path):
    """Read tasks from a plain list or a ClickUp-style JSON object.

    Example accepted shapes: ``[{"id": "1"}]`` or
    ``{"tasks": [{"id": "1"}]}``.
    """
    data = json.loads(Path(clickup_path).expanduser().read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("tasks", [])


def _load_critical_paths(critical_paths_path):
    """Return critical-path data and whether that optional input is missing.

    An unset, absent, unreadable, or malformed-JSON file is a degraded but valid
    run. Any successfully parsed JSON value is returned to ``critical_path_index``.
    """
    # Treat an unset or absent path the same as unreadable optional input.
    if not critical_paths_path or not Path(critical_paths_path).exists():
        return None, True

    # Unreadable or malformed-JSON optional data disables only its own signal.
    try:
        return json.loads(Path(critical_paths_path).read_text(encoding="utf-8")), False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, True


def build_rank(clickup_path, db_path, critical_paths_path, date_str):
    """Rank every person for one date and report missing ranking signals."""
    # Load and normalize the date-specific ranking inputs.
    tasks = _load_tasks(clickup_path)
    today = datetime.strptime(date_str, "%Y-%m-%d").date()
    critical_paths, critical_paths_missing = _load_critical_paths(critical_paths_path)
    path_index = critical_path_index(critical_paths)
    blocked_ids = blocked_task_ids(db_path)

    # Group open work by owner, then rank people in stable identity-key order.
    open_tasks = [t for t in tasks if daily_context.is_open(t)]
    people = group_by_person(open_tasks)
    ranked = [rank_person(person, today, path_index, blocked_ids)
              for _, person in sorted(people.items())]

    # Include gaps so the morning cycle can describe degraded ranking inputs.
    return {
        "date": date_str,
        "people": ranked,
        "gaps": {
            "critical_paths_missing": critical_paths_missing,
            # Guarded on `open_tasks` being non-empty on purpose: an empty board
            # means there was nothing to inspect, which is NOT the same claim as
            # having inspected the board and found the field absent.
            "owners_labels_unavailable": bool(open_tasks) and not any(
                t.get("custom_fields") for t in open_tasks),
            "priority_absent": bool(open_tasks) and all(
                priority_value(t) is None for t in open_tasks),
        },
    }


# Command-line interface

def main():
    """Run the ``rank``, ``set``, or ``get`` command."""
    # Define the three operations and their command-specific inputs.
    ap = argparse.ArgumentParser(description="Captain per-person top-2 ranking")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_rank = sub.add_parser("rank", help="print per-person candidates; writes nothing")
    p_rank.add_argument("--clickup", required=True)
    p_rank.add_argument("--db", default=str(DEFAULT_DB))
    p_rank.add_argument("--critical-paths",
                        default=str(ROOT / "data" / "critical-paths.json"))
    p_rank.add_argument("--date",
                        default=datetime.now(daily_context.TZ).date().isoformat())

    p_set = sub.add_parser("set", help="persist the top-2 that was actually sent")
    p_set.add_argument("--db", default=str(DEFAULT_DB))
    p_set.add_argument("--date", required=True)
    p_set.add_argument("--items", required=True,
                       help="JSON array of {slack_user_id, key, task_ids, "
                            "overridden, override_reason}")

    p_get = sub.add_parser("get", help="read back what was persisted")
    p_get.add_argument("--db", default=str(DEFAULT_DB))
    p_get.add_argument("--date", required=True)

    args = ap.parse_args()

    # Dispatch the selected operation and preserve its existing JSON output.
    try:
        if args.cmd == "rank":
            print(json.dumps(build_rank(args.clickup, args.db,
                                        args.critical_paths, args.date), indent=2))
        elif args.cmd == "set":
            print(json.dumps(daily_cycle.set_personal_top2(
                args.db, args.date, json.loads(args.items)), indent=2))
        else:
            row = daily_cycle.get_cycle(args.db, args.date)
            print(json.dumps(row["personal_top2"] if row else None, indent=2))
    except (ValueError, KeyError, OSError) as err:
        # Convert expected input and file failures into concise command errors.
        msg = str(err) if isinstance(err, OSError) else (err.args[0] if err.args else str(err))
        raise SystemExit(str(msg)) from err


if __name__ == "__main__":
    with captain_telemetry.guard("personal_top2"):
        main()
