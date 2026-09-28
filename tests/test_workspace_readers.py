"""Pin the board readers to the 2026-09-27 status and task-kind rules."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import clickup_workspace  # noqa: E402
import critical_paths  # noqa: E402
import daily_context  # noqa: E402
import daily_wrap  # noqa: E402
import personal_top2  # noqa: E402

INBOX = "1400460000001206"
TODAY = date(2026, 9, 28)


def due_ms(days):
    moment = datetime.combine(TODAY + timedelta(days=days), datetime.min.time()).replace(
        hour=12
    ).astimezone(timezone.utc)
    return str(int(moment.timestamp() * 1000))


def task(tid, status="in progress", typ="custom", list_id="901300000001", assignees=(), **extra):
    data = {"id": tid, "name": tid, "status": {"status": status, "type": typ},
            "list": {"id": list_id}, "assignees": [{"id": a, "username": str(a)} for a in assignees]}
    data.update(extra)
    return data


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(clickup_workspace.INBOX_ENV, raising=False)
    monkeypatch.setenv(clickup_workspace.CONFIG_ENV, str(clickup_workspace.EXAMPLE_CONFIG_PATH))


# Open / finished by status type


@pytest.mark.parametrize("status,typ,is_open", [
    ("backlog", "open", True),
    ("ready", "custom", True),
    ("in review", "custom", True),
    ("cancelled", "done", False),
    ("done", "closed", False),
    ("complete", "closed", False),
    ("complete", "custom", True),
    ("wrapped", "done", False),
])
def test_status_type_decides_open(status, typ, is_open):
    assert daily_context.is_open(task("t", status, typ)) is is_open
    assert critical_paths.is_open_task(task("t", status, typ)) is is_open


@pytest.mark.parametrize("status,is_open", [
    ("cancelled", False), ("done", False), ("complete", False), ("backlog", True),
    ("intake", True),
])
def test_names_are_only_a_fallback_without_type(status, is_open):
    assert daily_context.is_open({"id": "t", "status": status}) is is_open


@pytest.mark.parametrize("status,typ,expected", [
    ("backlog", "open", True),
    ("ready", "custom", True),
    ("intake", "", True),
    ("intake", "custom", True),
    ("in progress", "custom", False),
    ("in review", "custom", False),
    ("to do", "custom", False),
])
def test_not_started_includes_backlog_ready_and_intake(status, typ, expected):
    assert daily_wrap.is_not_started(task("t", status, typ)) is expected


# Owner gaps and milestones


def write_tasks(tmp_path, tasks):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return path


def test_owner_gaps_skip_milestones_and_inbox_items(tmp_path):
    tasks = [
        task("real-gap"),
        task("milestone", custom_item_id=1),
        task("inbox", "intake", "open", list_id=INBOX),
        task("owned", assignees=(10000004,)),
    ]
    ctx = daily_context.build_context(
        write_tasks(tmp_path, tasks), tmp_path / "db.sqlite", TODAY.isoformat(), None,
    )
    assert [item["id"] for item in ctx["board"]["owner_gaps"]] == ["real-gap"]


def test_milestone_risk_does_not_flag_milestones_or_inbox_as_owner_gaps(tmp_path):
    tasks = [
        task("ms", "backlog", "open", due_date=due_ms(2), custom_item_id=1),
        task("inbox", "intake", "open", list_id=INBOX, due_date=due_ms(2)),
        task("dep", "ready", "custom", due_date=due_ms(2)),
    ]
    paths = tmp_path / "critical-paths.json"
    paths.write_text(json.dumps({"paths": [{"name": "V1.1", "task_ids": ["ms", "inbox", "dep"]}]}),
                     encoding="utf-8")
    risk = daily_wrap._milestone_risk(tasks, paths, TODAY)
    reasons = risk["at_risk_paths"][0]["reasons"]
    assert any(reason.startswith("dep due") and "'ready'" in reason for reason in reasons)
    assert any(reason.startswith("dep has no owner") for reason in reasons)
    assert not any(reason.startswith("ms ") for reason in reasons)
    assert not any(reason.startswith("inbox has no owner") for reason in reasons)


def test_overdue_milestone_is_still_reported(tmp_path):
    tasks = [task("ms", "backlog", "open", due_date=due_ms(-3), custom_item_id=1)]
    paths = tmp_path / "critical-paths.json"
    paths.write_text(json.dumps({"paths": [{"name": "V1.1", "task_ids": ["ms"]}]}), encoding="utf-8")
    reasons = daily_wrap._milestone_risk(tasks, paths, TODAY)["at_risk_paths"][0]["reasons"]
    assert reasons == ["ms overdue since %s" % (TODAY - timedelta(days=3)).isoformat()]


def test_critical_path_signals_skip_missing_owner_for_milestones_and_inbox():
    now = datetime.now(timezone.utc)
    _, gap = critical_paths.task_risk_signals(task("a"), now)
    _, milestone = critical_paths.task_risk_signals(task("b", custom_item_id=1), now)
    _, inbox = critical_paths.task_risk_signals(task("c", list_id=INBOX), now)
    assert "missing_owner" in gap
    assert "missing_owner" not in milestone
    assert "missing_owner" not in inbox


def test_personal_ranking_ignores_milestones():
    people = personal_top2.group_by_person([
        task("ms", assignees=(10000005,), custom_item_id=1),
        task("work", assignees=(10000005,)),
    ])
    assert [t["id"] for t in people["10000005"]["tasks"]] == ["work"]


def test_readers_fall_back_to_generic_rules_without_config(monkeypatch, tmp_path):
    monkeypatch.delenv(clickup_workspace.CONFIG_ENV)
    assert clickup_workspace.reader_config() == {}
    assert daily_context.is_open(task("t", "cancelled", "done")) is False
    assert daily_context.is_open({"id": "t", "status": "cancelled"}) is False
    assert daily_wrap.is_not_started(task("t", "ready", "custom")) is True
    assert daily_context.is_owner_check_exempt(task("m", custom_item_id=1)) is True
