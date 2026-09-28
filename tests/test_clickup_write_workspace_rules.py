"""Pin clickup_write.py to the 2026-09-27 ClickUp workspace rules.

1. No custom field is ever created or written; one native assignee, replaced
   on update.
2. Statuses are matched exactly (no aliases).
3. Creates go only to the Inbox; archived lists are never written.
4. Created descriptions start with "Done when:" and name a proposed folder.
5. Only the safety and customer-visible tags are written.
6. People resolve through the identity map; departed people are refused. The
   private config is mandatory; tests use the public placeholder example.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import clickup_workspace  # noqa: E402
import clickup_write  # noqa: E402

INBOX = "1400460000001206"
ARCHIVED = [
    "901326347060", "901327700142", "901324583541", "901326084934", "901326085162",
    "901326085192", "901326085207", "901326085239", "901327546010",
]
DESCRIPTION = "Done when: harness continuity test passes\nProposed folder: GL-1/Harness"
PRODUCT_STATUSES = [
    {"status": "backlog", "type": "open"},
    {"status": "ready", "type": "custom"},
    {"status": "in progress", "type": "custom"},
    {"status": "blocked", "type": "custom"},
    {"status": "in review", "type": "custom"},
    {"status": "cancelled", "type": "done"},
    {"status": "done", "type": "closed"},
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (clickup_workspace.INBOX_ENV, clickup_workspace.ARCHIVED_ENV):
        monkeypatch.delenv(name, raising=False)
    # The public schema example stands in for the host's private config.
    monkeypatch.setenv(clickup_workspace.CONFIG_ENV, str(clickup_workspace.EXAMPLE_CONFIG_PATH))


@pytest.fixture
def config():
    return clickup_workspace.load_workspace()


class FakeClickUp:
    """Record every request and answer from a small in-memory board."""

    def __init__(self, tasks=None, statuses=None):
        self.calls = []
        self.tasks = tasks or {}
        self.statuses = statuses or PRODUCT_STATUSES

    def __call__(self, method, path, token, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("/task/"):
            return self.tasks[path.split("/")[2]]
        if method == "GET" and path.startswith("/list/"):
            return {"statuses": self.statuses}
        if method == "POST" and path.endswith("/task"):
            return {"id": "new1", "url": "https://app.clickup.com/t/new1"}
        return {"id": "ok"}

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def run(operations, fake, config):
    audits = []
    result = clickup_write.execute_batch(
        operations, "token", request_fn=fake,
        audit_fn=lambda event, **kw: audits.append((event, kw)), config=config,
    )
    return result, audits


def create(**extra):
    op = {"operation_id": "c1", "command": "create-task", "name": "Check harness",
          "description": DESCRIPTION}
    op.update(extra)
    return op


def task(list_id="901300000001", assignees=(), **extra):
    data = {"id": "t1", "name": "Existing", "list": {"id": list_id},
            "assignees": [{"id": item} for item in assignees], "description": "old"}
    data.update(extra)
    return data


# 1. Custom fields and single assignee


def test_writer_has_no_owners_field_code_path():
    source = (ROOT / "scripts" / "clickup_write.py").read_text(encoding="utf-8")
    assert "/field" not in source
    assert "resolve_owner_field" not in source
    assert '"custom_fields":' not in source


@pytest.mark.parametrize("key,value", [
    ("owner", ["person-a"]),
    ("owners", ["person-a"]),
    ("custom_fields", [{"id": "f", "value": ["x"]}]),
    ("custom_item_id", 1),
])
def test_custom_field_and_owner_keys_are_refused_without_any_request(key, value, config):
    fake = FakeClickUp()
    result, audits = run([create(**{key: value})], fake, config)
    assert fake.calls == []
    assert audits == []
    assert result["ok"] is False
    assert "custom field" in result["failed"][0]["error"]["message"]


def test_update_with_owner_key_never_touches_fields(config):
    fake = FakeClickUp(tasks={"t1": task()})
    result, _ = run([{"operation_id": "u1", "command": "update-task", "task_id": "t1",
                      "owner": ["person-a"]}], fake, config)
    assert result["ok"] is False
    assert fake.calls == []


def test_create_sets_exactly_one_native_assignee(config):
    fake = FakeClickUp()
    result, _ = run([create(assignee="person-a")], fake, config)
    assert result["ok"] is True
    (_, path, payload), = fake.writes()
    assert path == "/list/{}/task".format(INBOX)
    assert payload["assignees"] == [10000001]
    assert "custom_fields" not in payload


def test_create_refuses_more_than_one_assignee(config):
    fake = FakeClickUp()
    result, _ = run([create(assignee=["person-a", "person-d"])], fake, config)
    assert result["ok"] is False
    assert "exactly one owner" in result["failed"][0]["error"]["message"]
    assert fake.calls == []


def test_update_replaces_the_assignee_instead_of_accumulating(config):
    fake = FakeClickUp(tasks={"t1": task(assignees=(10000002, 10000003))})
    result, _ = run([{"operation_id": "u1", "command": "update-task", "task_id": "t1",
                      "assignee": "person-d"}], fake, config)
    assert result["ok"] is True
    (method, path, payload), = fake.writes()
    assert (method, path) == ("PUT", "/task/t1")
    assert payload["assignees"] == {"add": [10000004], "rem": [10000002, 10000003]}
    assert result["succeeded"][0]["assignee_change"] == payload["assignees"]


def test_update_to_the_current_sole_assignee_sends_no_assignee_change(config):
    fake = FakeClickUp(tasks={"t1": task(assignees=(10000004,))})
    result, _ = run([{"operation_id": "u1", "command": "update-task", "task_id": "t1",
                      "assignee": "10000004", "priority": 2}], fake, config)
    assert result["ok"] is True
    (_, _, payload), = fake.writes()
    assert "assignees" not in payload


def test_update_keeps_the_new_owner_and_removes_the_others(config):
    fake = FakeClickUp(tasks={"t1": task(assignees=(10000004, 10000005))})
    run([{"operation_id": "u1", "command": "update-task", "task_id": "t1",
          "assignee": "person-d"}], fake, config)
    (_, _, payload), = fake.writes()
    assert payload["assignees"] == {"add": [], "rem": [10000005]}


# 2. Statuses


def test_status_aliases_are_gone():
    assert not hasattr(clickup_write, "STATUS_ALIASES")
    names = [item["status"] for item in PRODUCT_STATUSES]
    for legacy in ("intake", "to do", "complete"):
        resolution = clickup_write.resolve_status(legacy, names)
        assert resolution["applied"] is None
        assert resolution["error"]["invalid_status"] == legacy


def test_product_status_is_matched_exactly(config):
    fake = FakeClickUp(tasks={"t1": task()})
    result, _ = run([{"operation_id": "u1", "command": "update-task", "task_id": "t1",
                      "status": "In Review"}], fake, config)
    assert result["ok"] is True
    assert fake.writes()[0][2]["status"] == "in review"


def test_unknown_status_is_never_sent(config):
    fake = FakeClickUp(tasks={"t1": task()})
    result, _ = run([{"operation_id": "u1", "command": "update-task", "task_id": "t1",
                      "status": "to do"}], fake, config)
    assert result["ok"] is False
    assert fake.writes() == []


# 3. Destinations


def test_create_defaults_to_the_inbox(config):
    fake = FakeClickUp()
    result, _ = run([create()], fake, config)
    assert result["succeeded"][0]["list_id"] == INBOX
    assert fake.writes()[0][1] == "/list/{}/task".format(INBOX)


def test_create_refuses_any_list_other_than_the_inbox(config):
    fake = FakeClickUp()
    result, _ = run([create(list_id="901300000001")], fake, config)
    assert result["ok"] is False
    assert "Inbox" in result["failed"][0]["error"]["message"]
    assert fake.calls == []


@pytest.mark.parametrize("list_id", ARCHIVED)
def test_create_refuses_every_archived_list(list_id, config):
    fake = FakeClickUp()
    result, _ = run([create(list_id=list_id)], fake, config)
    assert "archived" in result["failed"][0]["error"]["message"]
    assert fake.calls == []


def test_archived_blocklist_matches_the_reset():
    assert clickup_workspace.archived_list_ids(clickup_workspace.load_workspace()) == set(ARCHIVED)


@pytest.mark.parametrize("command,extra", [
    ("update-task", {"status": "done"}),
    ("comment-task", {"comment_text": "note"}),
])
def test_existing_tasks_in_archived_lists_are_never_written(command, extra, config):
    fake = FakeClickUp(tasks={"t1": task(list_id="901327546010")})
    result, _ = run([dict({"operation_id": "x", "command": command, "task_id": "t1"}, **extra)],
                    fake, config)
    assert result["ok"] is False
    assert "archived" in result["failed"][0]["error"]["message"]
    assert fake.writes() == []


def test_updates_and_comments_elsewhere_are_allowed(config):
    fake = FakeClickUp(tasks={"t1": task()})
    result, _ = run([
        {"operation_id": "u", "command": "update-task", "task_id": "t1", "priority": 2},
        {"operation_id": "c", "command": "comment-task", "task_id": "t1", "comment_text": "hi"},
    ], fake, config)
    assert result["ok"] is True
    assert [call[:2] for call in fake.writes()] == [("PUT", "/task/t1"), ("POST", "/task/t1/comment")]


def test_inbox_env_override(monkeypatch, config):
    monkeypatch.setenv(clickup_workspace.INBOX_ENV, "555")
    fake = FakeClickUp()
    result, _ = run([create()], fake, config)
    assert fake.writes()[0][1] == "/list/555/task"
    result, _ = run([create(operation_id="c2", list_id=INBOX)], FakeClickUp(), config)
    assert result["ok"] is False


def test_inbox_override_cannot_point_at_an_archived_list(monkeypatch, config):
    monkeypatch.setenv(clickup_workspace.INBOX_ENV, "901326347060")
    with pytest.raises(clickup_workspace.WorkspaceConfigError):
        clickup_workspace.inbox_list_id(config)


def test_archived_env_adds_to_the_blocklist(monkeypatch, config):
    monkeypatch.setenv(clickup_workspace.ARCHIVED_ENV, "777")
    fake = FakeClickUp(tasks={"t1": task(list_id="777")})
    result, _ = run([{"operation_id": "u", "command": "update-task", "task_id": "t1",
                      "priority": 1}], fake, config)
    assert result["ok"] is False
    assert set(ARCHIVED) <= clickup_workspace.archived_list_ids(config)


# 4. Description contract


@pytest.mark.parametrize("description", [
    None,
    "",
    "Proposed folder: GL-1/Harness\nDone when: later",
    "Done when:\nProposed folder: GL-1/Harness",
    "Done when: it works",
    "Done when: it works\nProposed folder:",
])
def test_create_without_done_when_or_folder_is_refused(description, config):
    fake = FakeClickUp()
    result, _ = run([create(description=description)], fake, config)
    assert result["ok"] is False
    assert "refused create-task" in result["failed"][0]["error"]["message"]
    assert fake.calls == []


def test_blocked_note_keeps_done_when_first(config):
    fake = FakeClickUp(statuses=[{"status": "intake"}, {"status": "planning"}])
    result, _ = run([create(status="blocked")], fake, config)
    payload = fake.writes()[0][2]
    assert payload["description"].startswith("Done when:")
    assert result["succeeded"][0]["needs_blocked_status"] is True


# 5. Tags


def test_only_allowed_tags_are_written_and_the_rest_are_reported(config):
    fake = FakeClickUp()
    result, audits = run([create(tags=["Safety", "customer-visible", "R-12", "hardware"])],
                         fake, config)
    assert fake.writes()[0][2]["tags"] == ["safety", "customer-visible"]
    assert result["succeeded"][0]["dropped_tags"] == ["R-12", "hardware"]
    assert audits[0][1]["dropped_tags"] == ["R-12", "hardware"]


def test_no_tags_key_when_every_tag_is_dropped(config):
    fake = FakeClickUp()
    run([create(tags=["R-12"])], fake, config)
    assert "tags" not in fake.writes()[0][2]


def test_tags_on_update_are_refused(config):
    fake = FakeClickUp(tasks={"t1": task()})
    result, _ = run([{"operation_id": "u", "command": "update-task", "task_id": "t1",
                      "tags": ["safety"]}], fake, config)
    assert result["ok"] is False
    assert fake.calls == []


# 6. Identity


@pytest.mark.parametrize("person,expected", [
    ("Person A", 10000001), ("person-a", 10000001), ("PA", 10000001), ("person", None),
    ("person-b", 10000002), ("pb", 10000002), ("Person C", 10000003),
    ("person-d", 10000004), ("pd", 10000004), ("person-e", 10000005), ("10000005", 10000005),
])
def test_identity_map_resolves_names_aliases_and_ids(person, expected, config):
    if expected is None:
        # "person" is every member's first name: ambiguous, so never guessed.
        with pytest.raises(clickup_workspace.IdentityResolutionError, match="several"):
            clickup_workspace.resolve_assignee_id(person, config)
    else:
        assert clickup_workspace.resolve_assignee_id(person, config) == expected


@pytest.mark.parametrize("person", ["departed-person", "Departed Person"])
def test_departed_people_fail_resolution(person, config):
    with pytest.raises(clickup_workspace.IdentityResolutionError, match="no longer"):
        clickup_workspace.resolve_assignee_id(person, config)


@pytest.mark.parametrize("person", ["Bob", "99999999", "person-z", "Person A Smith"])
def test_unknown_people_are_never_guessed(person, config):
    with pytest.raises(clickup_workspace.IdentityResolutionError):
        clickup_workspace.resolve_assignee_id(person, config)


def test_non_assignable_agent_is_never_an_assignee(config):
    with pytest.raises(clickup_workspace.IdentityResolutionError, match="never"):
        clickup_workspace.resolve_assignee_id("10000099", config)
    fake = FakeClickUp()
    result, _ = run([create(assignee="agent-service")], fake, config)
    assert result["ok"] is False
    assert fake.calls == []


def test_departed_assignee_refuses_the_whole_create(config):
    fake = FakeClickUp()
    result, _ = run([create(assignee="departed-person")], fake, config)
    assert result["ok"] is False
    assert fake.calls == []


def test_folder_owners_point_at_assignable_members(config):
    ids = {m["clickup_id"]: m for m in config["members"]}
    assert set(config["folder_owners"]) == set(config["product_folders"]) - {"Projects"}
    assert all(ids[owner]["assignable"] for owner in config["folder_owners"].values())


def test_public_example_carries_only_placeholder_people(config):
    for member in config["members"]:
        assert member["clickup_id"].startswith("100000"), member
        assert member["name"].startswith(("Person ", "Agent Service")), member


# The private config is mandatory for the writer


def test_writer_refuses_without_config_env(monkeypatch, capsys):
    monkeypatch.delenv(clickup_workspace.CONFIG_ENV)
    with pytest.raises(clickup_workspace.WorkspaceConfigError, match=clickup_workspace.CONFIG_ENV):
        clickup_workspace.load_workspace()
    with pytest.raises(clickup_workspace.WorkspaceConfigError):
        clickup_write.execute_batch([create()], "token", request_fn=FakeClickUp())
    monkeypatch.setattr(sys, "argv", [
        "clickup_write.py", "create-task", "--name", "X", "--description", DESCRIPTION,
    ])
    assert clickup_write.main() == 2
    assert clickup_workspace.CONFIG_ENV in json.loads(capsys.readouterr().out)["error"]["message"]


def test_writer_refuses_when_config_file_is_missing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(clickup_workspace.CONFIG_ENV, str(tmp_path / "missing.json"))
    monkeypatch.setattr(sys, "argv", [
        "clickup_write.py", "create-task", "--name", "X", "--description", DESCRIPTION,
    ])
    assert clickup_write.main() == 2
    assert "cannot read" in json.loads(capsys.readouterr().out)["error"]["message"]


# CLI dry run applies the same refusals


def test_cli_dry_run_refuses_archived_list(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "clickup_write.py", "create-task", "--list-id", "901327546010", "--name", "X",
        "--description", DESCRIPTION,
    ])
    assert clickup_write.main() == 2
    assert "archived" in json.loads(capsys.readouterr().out)["error"]["message"]


def test_cli_has_no_owner_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "clickup_write.py", "create-task", "--name", "X", "--owner", "person-a",
    ])
    with pytest.raises(SystemExit):
        clickup_write.main()


def test_cli_dry_run_reports_dropped_tags(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "clickup_write.py", "create-task", "--name", "X", "--description", DESCRIPTION,
        "--tag", "safety", "--tag", "R-7", "--assignee", "person-b",
    ])
    assert clickup_write.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["planned_request"]["path"] == "/list/{}/task".format(INBOX)
    assert out["planned_request"]["payload"]["tags"] == ["safety"]
    assert out["planned_request"]["payload"]["assignees"] == [10000002]
    assert out["dropped_tags"] == ["R-7"]
