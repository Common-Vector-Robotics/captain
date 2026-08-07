#!/usr/bin/env python3
"""Captain per-person top-2: rank each person's open ClickUp tasks.

The morning cycle posts one team-wide top-3 brief; this is the per-person half.
It reads the SAME board fetch that brief is built from, so the two can never
disagree about board state.

Ranking here is deliberately mechanical and testable. Critical-path membership,
due dates, ClickUp priority and blocked-ness are all structured data, so a
script gets them exactly right every time. Judgment that needs context absent
from the board -- Cognee memory, last night's Slack sweep -- belongs to the
cron prompt, which may re-order a person's two items and must record that it
did (see cron-prompts/daily-morning-cycle.md step 6b).
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

# The ClickUp custom **labels** field that carries owners who cannot be real
# ClickUp assignees (MEMORY.md's standing rule; see clickup_write.py's
# `--owner`/resolve_owner_labels for the write side of the same field).
OWNERS_FIELD_NAME = "owners"


def owner_label_names(task):
    """Owner names from a task's `Owners` custom labels field.

    Resolves entirely offline from the morning fetch: the task's own
    `custom_fields` entry carries both the selected option ids (`value`) and the
    field definition (`type_config.options`, each `{id, label}`), so no ClickUp
    call is needed. A task with no `custom_fields`, no Owners field, a
    non-labels field of that name, or an unset value yields [] -- all normal
    inputs, never errors.
    """
    for field in task.get("custom_fields") or []:
        if (field.get("name") or "").strip().lower() != OWNERS_FIELD_NAME:
            continue
        if (field.get("type") or "").strip().lower() != "labels":
            continue
        value = field.get("value")
        if not isinstance(value, list):
            return []
        options = {}
        for option in (field.get("type_config") or {}).get("options") or []:
            if option.get("id") is not None and option.get("label"):
                options[option["id"]] = option["label"]
        return [options[v] for v in value if v in options]
    return []


def assignee_identity(assignee):
    """Board identity for one ClickUp assignee, or None if it has no usable id.

    `key` prefers email because email is the lookup input
    `message(action=member-info)` needs to resolve a Slack user; it falls back to
    username, then the numeric id as a string, so a member with no email still
    groups under something stable instead of collapsing together with everyone
    else who lacks one.
    """
    email = (assignee.get("email") or "").strip() or None
    username = (assignee.get("username") or "").strip() or None
    user_id = assignee.get("id")
    key = email or username or (str(user_id) if user_id is not None else None)
    if key is None:
        return None
    return {"key": key, "source": "assignee", "clickup_user_id": user_id,
            "username": username, "email": email}


def group_by_person(tasks):
    """Group open tasks by board identity -> identity dict plus a `tasks` list.

    A task with several assignees lands on each of their plates -- shared work
    genuinely is shared. Owners-label names are consulted ONLY for a task with no
    assignees at all: when a real ClickUp member is assigned, that assignment is
    the authoritative owner, and reading the label too would invent a second
    owner for the same task. A task with neither yields no recipient (
    daily_context.py's `owner_gaps` already surfaces those in the brief).
    """
    people = {}
    for task in tasks:
        if not daily_context.is_open(task):
            continue
        identities = [i for i in
                      (assignee_identity(a) for a in task.get("assignees") or [])
                      if i is not None]
        if not identities:
            identities = [{"key": name, "source": "owners_label",
                           "clickup_user_id": None, "username": None,
                           "email": None}
                          for name in owner_label_names(task)]
        for identity in identities:
            person = people.setdefault(identity["key"], dict(identity, tasks=[]))
            person["tasks"].append(task)
    return people


BLOCKED_STATUS = "blocked"
PRIORITY_URGENT = 1
PRIORITY_HIGH = 2
HOT_PATH_PRIORITIES = ("critical",)
WARM_PATH_PRIORITIES = ("critical", "high")
LOWEST_TIER = 6


def critical_path_index(critical_paths):
    """Map task id -> the hottest critical path it belongs to.

    `critical_paths.py` writes each path with `task_ids` (capped at 25),
    `priority` (critical/high/normal) and `score`, so this is exact set
    membership rather than a judgment call -- which is precisely why it lives in
    a script. A task on two paths keeps the higher-scoring one: that is the path
    whose slippage costs most, so it is the one worth naming.

    `None`, a non-dict, or a malformed `paths` entry yields {} -- tiers 1 and 4
    then collapse into the others, the documented degraded behavior when
    data/critical-paths.json is absent or unreadable.
    """
    index = {}
    if not isinstance(critical_paths, dict):
        return index
    for path in critical_paths.get("paths") or []:
        if not isinstance(path, dict):
            continue
        try:
            score = int(path.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        entry = {"name": path.get("name") or "",
                 "priority": (path.get("priority") or "normal").strip().lower(),
                 "score": score}
        for task_id in path.get("task_ids") or []:
            key = str(task_id)
            current = index.get(key)
            if current is None or entry["score"] > current["score"]:
                index[key] = entry
    return index


def blocked_task_ids(db_path):
    """ClickUp task ids carrying an open (non-cleared) ledger blocker."""
    return {str(b["clickup_task_id"])
            for b in blocker_ledger.open_blockers(db_path)
            if b.get("clickup_task_id")}


def is_stuck(task, blocked_ids):
    """True when ClickUp says `Blocked` or an open ledger blocker links here."""
    status, _ = daily_context.status_of(task)
    if status == BLOCKED_STATUS:
        return True
    return str(task.get("id")) in blocked_ids


def priority_value(task):
    """ClickUp numeric priority (1 urgent .. 4 low), or None when absent.

    ClickUp returns priority either as a nested `{id, orderindex, priority}`
    object or omits it entirely; fixtures/clickup_tasks_sample.json carries no
    `priority` at all. Absence is a normal input that costs tier 5 its signal,
    never an error.
    """
    raw = task.get("priority")
    if isinstance(raw, dict):
        raw = raw.get("id") or raw.get("orderindex")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def overdue_days(task, today):
    """Whole days past due, 0 when due today, in the future, or undated."""
    due = daily_context.due_local_date(task)
    if due is None or due >= today:
        return 0
    return (today - due).days


def _days_phrase(days):
    """Describe how many days late a task is using natural singular or plural wording."""
    return "1 day past due" if days == 1 else "%d days past due" % days


def tier_and_reason(task, today, path, stuck):
    """Assign a task's tier (1 highest .. 6 lowest) and its plain-language reason.

    Tiers are lexicographic, not a weighted sum: a tier collapses to a sentence a
    person can read, where a summed score explains nothing. The reason string is
    what actually appears in the text, so it never uses internal vocabulary --
    "blocker" in particular is forbidden with employees (MEMORY.md, Gavin
    2026-06-19), which is why stuck-ness demotes silently instead of being named.
    """
    due = daily_context.due_local_date(task)
    past_due = overdue_days(task, today)
    due_today = due is not None and due == today
    hot = path is not None and path["priority"] in HOT_PATH_PRIORITIES
    warm = path is not None and path["priority"] in WARM_PATH_PRIORITIES
    priority = priority_value(task)

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

    if stuck:
        tier = min(tier + 1, LOWEST_TIER)
    return tier, reason


NO_DUE_SORT = "9999-12-31"
NO_PRIORITY_SORT = 99


def rank_person(person, today, path_index, blocked_ids):
    """Rank one person's open tasks and pick their top 2.

    Sort key, in order: tier; more overdue first; earliest due date (undated
    last); hottest ClickUp priority (unprioritized last); highest critical-path
    score; then task id. That last term is what makes the order TOTAL -- without
    it, two tasks tied on every signal could come out in either order depending
    on how ClickUp happened to page the board, and the same morning would produce
    different texts on a re-run.
    """
    ranked = []
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
        ranked.append((sort_key, {
            "task_id": task_id,
            "name": task.get("name") or "",
            "url": task.get("url"),
            "due": due.isoformat() if due is not None else None,
            "tier": tier,
            "stuck": stuck,
            "reason": reason,
        }))
    ranked.sort(key=lambda pair: pair[0])
    candidates = [candidate for _, candidate in ranked]
    out = {k: v for k, v in person.items() if k != "tasks"}
    out["candidates"] = candidates
    out["top2"] = [c["task_id"] for c in candidates[:2]]
    return out


def _load_tasks(clickup_path):
    """Read tasks from either a plain list or a ClickUp-style JSON file."""
    data = json.loads(Path(clickup_path).expanduser().read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("tasks", [])


def _load_critical_paths(critical_paths_path):
    """Return (payload, missing). A path that is unset, absent, or unparseable is
    a degraded-but-valid run, not a failure: tiers 1 and 4 collapse and the
    caller reports the gap so the morning brief can say so."""
    if not critical_paths_path or not Path(critical_paths_path).exists():
        return None, True
    try:
        return json.loads(Path(critical_paths_path).read_text(encoding="utf-8")), False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, True


def build_rank(clickup_path, db_path, critical_paths_path, date_str):
    """Rank every person on the board for `date_str`, plus the run's gap flags."""
    tasks = _load_tasks(clickup_path)
    today = datetime.strptime(date_str, "%Y-%m-%d").date()
    critical_paths, critical_paths_missing = _load_critical_paths(critical_paths_path)
    path_index = critical_path_index(critical_paths)
    blocked_ids = blocked_task_ids(db_path)

    open_tasks = [t for t in tasks if daily_context.is_open(t)]
    people = group_by_person(open_tasks)
    ranked = [rank_person(person, today, path_index, blocked_ids)
              for _, person in sorted(people.items())]
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


def main():
    """Rank, save, or retrieve each person's two most important tasks."""
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
        msg = str(err) if isinstance(err, OSError) else (err.args[0] if err.args else str(err))
        raise SystemExit(str(msg)) from err


if __name__ == "__main__":
    with captain_telemetry.guard("personal_top2"):
        main()
