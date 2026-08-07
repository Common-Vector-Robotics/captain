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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def load_env_file(path):
    p = Path(path).expanduser()
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def get_clickup_credentials():
    load_env_file(ROOT / ".secrets" / "clickup.env")
    token = os.environ.get("CLICKUP_API_KEY")
    team = os.environ.get("CLICKUP_TEAM_ID")
    missing = []
    if not token:
        missing.append("CLICKUP_API_KEY")
    if not team:
        missing.append("CLICKUP_TEAM_ID")
    if missing:
        raise SystemExit("Missing required credentials: " + ", ".join(missing))
    return token, team


def http_json(method, url, headers=None, payload=None, timeout=45):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def clickup_req(token, method, path, payload=None):
    return http_json(
        method,
        API_CLICKUP + path,
        headers={"Authorization": token, "Content-Type": "application/json"},
        payload=payload,
    )


def status_name(task):
    status = task.get("status") or {}
    return status.get("status") if isinstance(status, dict) else str(status)


def status_type(task):
    status = task.get("status") or {}
    return status.get("type") if isinstance(status, dict) else ""


def is_open_task(task):
    stype = (status_type(task) or "").lower()
    sname = (status_name(task) or "").lower()
    return stype not in {"closed", "done", "complete"} and sname not in {"closed", "complete", "completed", "done"}


def fetch_clickup_tasks(token, team_id):
    list_ids = [x.strip() for x in os.environ.get("CAPTAIN_CLICKUP_LIST_IDS", "").split(",") if x.strip()]
    bases = [f"/list/{lid}/task" for lid in list_ids] if list_ids else [f"/team/{team_id}/task"]
    tasks = []
    for base in bases:
        page = 0
        while True:
            qs = urllib.parse.urlencode({"subtasks": "true", "include_closed": "false", "page": str(page)})
            data = clickup_req(token, "GET", f"{base}?{qs}")
            tasks.extend(data.get("tasks") or [])
            if data.get("last_page", True):
                break
            page += 1
            time.sleep(0.7)
    return [t for t in tasks if is_open_task(t)]


def due_dt(task):
    due = task.get("due_date")
    if not due:
        return None
    try:
        return datetime.fromtimestamp(int(due) / 1000, timezone.utc)
    except Exception:
        return None


def priority_name(task):
    pri = task.get("priority") or {}
    if isinstance(pri, dict):
        return (pri.get("priority") or pri.get("name") or "").lower()
    return str(pri or "").lower()


def task_list_id(task):
    li = task.get("list") or {}
    return str(li.get("id") or task.get("list_id") or "")


def task_list_name(task):
    li = task.get("list") or {}
    return li.get("name") or "Unlisted"


def task_space_id(task):
    sp = task.get("space") or {}
    return str(sp.get("id") or "")


def task_space_name(task):
    sp = task.get("space") or {}
    return sp.get("name") or "Unknown space"


def task_project_key(task):
    parent = task.get("top_level_parent") or task.get("parent")
    if parent:
        return f"parent:{parent}", f"Parent {parent}"
    list_id = task_list_id(task)
    if list_id:
        return f"list:{list_id}", f"{task_space_name(task)} / {task_list_name(task)}"
    return "workspace:unscoped", "Unscoped tasks"


def task_risk_signals(task, now):
    signals = []
    score = 0
    due = due_dt(task)
    if due:
        days = (due.date() - now.date()).days
        if days < 0:
            signals.append(f"overdue:{abs(days)}d")
            score += 40
        elif days <= 14:
            signals.append(f"due_soon:{days}d")
            score += max(5, 25 - days)
    pri = priority_name(task)
    if pri in {"urgent", "high"}:
        signals.append(f"priority:{pri}")
        score += 18 if pri == "urgent" else 12
    if task.get("dependencies") or task.get("linked_tasks"):
        signals.append("has_dependencies")
        score += 10
    if task.get("subtasks"):
        signals.append("has_subtasks")
        score += 6
    name = normalize(task.get("name"))
    if re.search(r"\b(ship|demo|customer|release|launch|milestone|block|critical|urgent|deadline)\b", name):
        signals.append("strategic_keyword")
        score += 8
    if not task.get("assignees") and not any(normalize(cf.get("name")) in {"owner", "owners"} and cf.get("value") for cf in task.get("custom_fields") or []):
        signals.append("missing_owner")
        score += 6
    return score, signals


def load_overrides(path=OVERRIDES_PATH):
    if not path.exists():
        return {"force_include_task_ids": [], "force_exclude_task_ids": [], "paths": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("override file must contain an object")
        data.setdefault("force_include_task_ids", [])
        data.setdefault("force_exclude_task_ids", [])
        data.setdefault("paths", [])
        return data
    except Exception as exc:
        raise SystemExit(f"Invalid {path}: {exc}")


def discover_paths(tasks, max_paths=5, overrides=None):
    overrides = overrides or {}
    now = datetime.now(timezone.utc)
    by_id = {str(t.get("id")): t for t in tasks if t.get("id")}
    excluded = {str(x) for x in overrides.get("force_exclude_task_ids") or []}
    forced = {str(x) for x in overrides.get("force_include_task_ids") or []}
    groups = {}

    for task in tasks:
        tid = str(task.get("id") or "")
        if not tid or tid in excluded:
            continue
        key, label = task_project_key(task)
        score, signals = task_risk_signals(task, now)
        if tid in forced:
            signals.append("admin_force_include")
            score += 100
        if score <= 0:
            continue
        g = groups.setdefault(key, {"key": key, "name": label, "tasks": [], "score": 0, "signals": set(), "source": "inferred"})
        g["tasks"].append(task)
        g["score"] += score
        g["signals"].update(signals)

    invalid_override_task_ids = sorted((forced | excluded) - set(by_id.keys()))

    for idx, override in enumerate(overrides.get("paths") or []):
        tids = [str(tid) for tid in override.get("task_ids") or [] if str(tid) in by_id and str(tid) not in excluded]
        if not tids:
            continue
        key = f"override:{override.get('id') or idx}"
        g = groups.setdefault(key, {"key": key, "name": override.get("name") or f"Override path {idx+1}", "tasks": [], "score": 0, "signals": set(), "source": "admin_override"})
        existing = {str(t.get("id")) for t in g["tasks"]}
        for tid in tids:
            if tid not in existing:
                g["tasks"].append(by_id[tid])
        g["score"] += int(override.get("score_boost") or 100)
        g["signals"].add("admin_override")
        if override.get("priority"):
            g["priority_override"] = override.get("priority")
        if override.get("target_date"):
            g["target_date_override"] = override.get("target_date")

    paths = []
    for key, g in groups.items():
        sorted_tasks = sorted(g["tasks"], key=lambda t: ((due_dt(t) is None), int(t.get("due_date") or 0), t.get("name") or ""))
        task_ids = [str(t.get("id")) for t in sorted_tasks if t.get("id")]
        due_dates = [due_dt(t).date().isoformat() for t in sorted_tasks if due_dt(t)]
        priority = g.get("priority_override") or ("critical" if g["score"] >= 90 else "high" if g["score"] >= 45 else "normal")
        paths.append({
            "id": re.sub(r"[^a-z0-9_:-]+", "-", key.lower()),
            "name": g["name"],
            "source": g.get("source") or "inferred",
            "priority": priority,
            "target_date": g.get("target_date_override") or (min(due_dates) if due_dates else None),
            "task_ids": task_ids[:25],
            "dependency_task_ids": sorted({str(dep.get("task_id") or dep.get("id")) for t in sorted_tasks for dep in (t.get("dependencies") or []) if dep.get("task_id") or dep.get("id")}),
            "risk_signals": sorted(g["signals"]),
            "score": g["score"],
            "confidence": 0.9 if g.get("source") == "admin_override" else min(0.85, 0.45 + (g["score"] / 120)),
            "last_evaluated_at": now_iso(),
            "evidence": [
                {
                    "task_id": str(t.get("id")),
                    "name": t.get("name"),
                    "status": status_name(t),
                    "due_date": due_dt(t).date().isoformat() if due_dt(t) else None,
                    "url": t.get("url"),
                    "list": task_list_name(t),
                }
                for t in sorted_tasks[:10]
            ],
        })
    paths = sorted(paths, key=lambda p: (-p["score"], p.get("target_date") or "9999-12-31", p["name"]))[:max_paths]
    return {
        "version": 1,
        "generated_at": now_iso(),
        "max_paths": max_paths,
        "paths": paths,
        "invalid_override_task_ids": invalid_override_task_ids,
    }


def write_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def print_summary(state):
    print(json.dumps(state, indent=2, sort_keys=True))


def main():
    ap = argparse.ArgumentParser(description="Discover Captain critical paths from ClickUp")
    ap.add_argument("command", choices=["discover", "score", "write-state"])
    ap.add_argument("--max-paths", type=int, default=int(os.environ.get("CAPTAIN_MAX_CRITICAL_PATHS", "5")))
    args = ap.parse_args()

    token, team_id = get_clickup_credentials()
    tasks = fetch_clickup_tasks(token, team_id)
    overrides = load_overrides()
    state = discover_paths(tasks, max_paths=args.max_paths, overrides=overrides)

    if args.command == "write-state":
        write_state(state)
        audit(
            "critical_paths_state_written",
            source="scripts/critical_paths.py",
            path=str(STATE_PATH.relative_to(ROOT)),
            path_count=len(state.get("paths") or []),
            task_count=sum(len(p.get("task_ids") or []) for p in state.get("paths") or []),
            invalid_override_task_ids=state.get("invalid_override_task_ids") or [],
        )
    print_summary(state)


if __name__ == "__main__":
    with captain_telemetry.guard("critical_paths"):
        main()
