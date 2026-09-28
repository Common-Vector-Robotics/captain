"""Pin direct filing into subsystem folder lists to the certainty rule.

A new task goes straight into a subsystem folder's list only when both the
product (space) and the folder are certain from the evidence text; anything
less goes to the Inbox. Tests use the public placeholder example config.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import clickup_workspace  # noqa: E402
import clickup_write  # noqa: E402

INBOX = "1400460000001206"
GL1_POWER = "1400460000001193"
GL1_HARNESS = "1400460000001197"
GR_SAFETY = "1400460000001196"
POWER_OWNER = 10000002  # Person B in the example config
SAFETY_OWNER = 10000004  # Person D
DONE = "Done when: the fault no longer reproduces on the bench"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (clickup_workspace.INBOX_ENV, clickup_workspace.ARCHIVED_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(clickup_workspace.CONFIG_ENV, str(clickup_workspace.EXAMPLE_CONFIG_PATH))


@pytest.fixture
def config():
    return clickup_workspace.load_workspace()


class FakeClickUp:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, token, payload=None):
        self.calls.append((method, path, payload))
        if method == "POST" and path.endswith("/task"):
            return {"id": "new1", "url": "https://app.clickup.com/t/new1"}
        return {"statuses": []}


def run(operations, config):
    fake, audits = FakeClickUp(), []
    result = clickup_write.execute_batch(
        operations, "token", request_fn=fake,
        audit_fn=lambda event, **kw: audits.append((event, kw)), config=config,
    )
    return result, fake, audits


def create(**extra):
    op = {"operation_id": "c1", "command": "create-task", "name": "Routed task",
          "description": DONE + "\nProposed folder: GL-1/Power"}
    op.update(extra)
    return op


# The certainty rule


@pytest.mark.parametrize("text,assignee,expected", [
    ("check the DCDC grounding on GL-1", POWER_OWNER, (GL1_POWER, "keyword-owner")),
    ("redesign the side panel", None, (INBOX, "inbox")),
    ("redesign the side panel", POWER_OWNER, (INBOX, "inbox")),
    ("e-stop reliability on Ghostrunner", SAFETY_OWNER, (GR_SAFETY, "keyword-owner")),
    ("e-stop safety reliability on Ghostrunner", None, (GR_SAFETY, "named-folder")),
    ("GL-1 harness and battery rework", None, (INBOX, "inbox")),
    ("HMI scaling on Ghostrunner", None, (INBOX, "inbox")),
    ("HMI scaling on GL1", None, ("1400460000001207", "named-folder")),
    ("AFS harness continuity", None, (GL1_HARNESS, "named-folder")),
    ("Newlab sensors and compute mount", None, ("1400460000001194", "named-folder")),
    ("ghost runner Sensors & Compute", None, ("1400460000001194", "named-folder")),
    ("GL-1 and Ghostrunner power budget", None, (INBOX, "inbox")),
])
def test_route_destination(text, assignee, expected, config):
    list_id, rule, reason = clickup_workspace.route_destination(text, assignee, config)
    assert (list_id, rule) == expected, reason
    assert reason


def test_keyword_hit_without_the_folder_owner_goes_to_the_inbox(config):
    for assignee in (None, 10000001):
        list_id, rule, reason = clickup_workspace.route_destination(
            "check the DCDC grounding on GL-1", assignee, config)
        assert (list_id, rule) == (INBOX, "inbox")
        assert "owner" in reason


def test_keywords_matching_two_folders_go_to_the_inbox(config):
    list_id, rule, reason = clickup_workspace.route_destination(
        "DCDC connector swap on GL-1", POWER_OWNER, config)
    assert (list_id, rule) == (INBOX, "inbox")
    assert "several" in reason


def test_example_config_maps_all_21_subsystem_lists(config):
    direct = clickup_workspace.direct_lists(config)
    assert len(direct) == 21
    assert INBOX not in direct
    assert all(folder != "Projects" for _, folder in direct.values())
    assert set(config["folder_keywords"]) == set(config["folder_owners"])


# The writer


def test_routed_create_files_directly_and_records_the_rule(config):
    result, fake, audits = run([create(assignee="person-b",
                                       route_from="check the DCDC grounding on GL-1")], config)
    assert result["ok"] is True
    (_, path, payload), = [call for call in fake.calls if call[0] == "POST"]
    assert path == "/list/{}/task".format(GL1_POWER)
    lines = payload["description"].splitlines()
    assert lines[0] == DONE
    assert lines[1] == "Filed directly: GL-1/Power (keyword-owner)"
    assert result["succeeded"][0]["route_rule"] == "keyword-owner"
    event, record = audits[0]
    assert event == "clickup_task_create"
    assert record["route_rule"] == "keyword-owner"
    assert "Power" in record["route_reason"]


def test_routed_create_that_is_not_certain_stays_in_the_inbox(config):
    result, fake, audits = run([create(
        route_from="redesign the side panel",
        description=DONE + "\nFiled directly: GL-1/Structures (named-folder)",
    )], config)
    assert result["ok"] is True
    (_, path, payload), = [call for call in fake.calls if call[0] == "POST"]
    assert path == "/list/{}/task".format(INBOX)
    assert "Filed directly:" not in payload["description"]
    assert "Proposed folder: GL-1/Structures or Ghostrunner/Structures" in payload["description"]
    assert audits[0][1]["route_rule"] == "inbox"


def test_routed_inbox_create_keeps_an_existing_proposed_folder(config):
    result, fake, _ = run([create(route_from="GL-1 harness and battery rework",
                                  description=DONE + "\nProposed folder: GL-1/Harness")], config)
    assert result["ok"] is True
    payload = [call for call in fake.calls if call[0] == "POST"][0][2]
    assert payload["description"] == DONE + "\nProposed folder: GL-1/Harness"


def test_route_and_list_id_must_agree(config):
    result, fake, _ = run([create(route_from="redesign the side panel", list_id=GL1_POWER,
                                  description=DONE + "\nFiled directly: GL-1/Power (x)")], config)
    assert result["ok"] is False
    assert fake.calls == []


def test_direct_filing_without_filed_directly_line_is_refused(config):
    result, fake, _ = run([create(list_id=GL1_POWER)], config)
    assert result["ok"] is False
    assert "Filed directly" in result["failed"][0]["error"]["message"]
    assert fake.calls == []


def test_direct_filing_line_must_name_the_target_list(config):
    result, fake, _ = run([create(
        list_id=GL1_POWER, description=DONE + "\nFiled directly: GL-1/Harness (named-folder)",
    )], config)
    assert result["ok"] is False
    assert fake.calls == []


def test_direct_filing_with_filed_directly_line_is_allowed(config):
    result, fake, audits = run([create(
        list_id=GL1_POWER, description=DONE + "\nFiled directly: GL-1/Power (named-folder)",
    )], config)
    assert result["ok"] is True
    assert fake.calls[0][1] == "/list/{}/task".format(GL1_POWER)
    assert audits[0][1]["route_rule"] == "explicit-list"


def test_inbox_task_must_not_claim_a_direct_filing(config):
    result, _, _ = run([create(
        description=DONE + "\nProposed folder: GL-1/Power\nFiled directly: GL-1/Power (x)",
    )], config)
    assert result["ok"] is False


def test_direct_filing_to_a_projects_list_is_refused(config):
    config = copy.deepcopy(config)
    config["lists"]["GL-1"]["Projects"] = "1400460000009991"
    config["lists"]["OPS"] = {"Ops": "1400460000009992"}
    for list_id, folder in (("1400460000009991", "GL-1/Projects"),
                            ("1400460000009992", "OPS/Ops")):
        result, fake, _ = run([create(
            list_id=list_id, description=DONE + "\nFiled directly: {} (named-folder)".format(folder),
        )], config)
        assert result["ok"] is False
        assert "Inbox" in result["failed"][0]["error"]["message"]
        assert fake.calls == []


def test_archived_subsystem_list_is_never_a_destination(monkeypatch, config):
    monkeypatch.setenv(clickup_workspace.ARCHIVED_ENV, GL1_POWER)
    result, fake, _ = run([create(list_id=GL1_POWER,
                                  description=DONE + "\nFiled directly: GL-1/Power (x)")], config)
    assert "archived" in result["failed"][0]["error"]["message"]
    assert fake.calls == []


def test_cli_dry_run_reports_the_route(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "clickup_write.py", "create-task", "--name", "Fix e-stop", "--assignee", "person-d",
        "--route-from", "e-stop reliability on Ghostrunner",
        "--description", DONE + "\nProposed folder: Ghostrunner/Safety",
    ])
    assert clickup_write.main() == 0
    out = json.loads(capsys.readouterr().out)
    planned = out["planned_request"]
    assert planned["path"] == "/list/{}/task".format(GR_SAFETY)
    assert out["route_rule"] == "keyword-owner"
    assert "Safety" in out["route_reason"]
    assert "Filed directly: Ghostrunner/Safety (keyword-owner)" in planned["payload"]["description"]


def test_cli_dry_run_reports_the_inbox_rule(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "clickup_write.py", "create-task", "--name", "HMI", "--route-from",
        "HMI scaling on Ghostrunner", "--description", DONE + "\nProposed folder: GL-1/HMI",
    ])
    assert clickup_write.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["planned_request"]["path"] == "/list/{}/task".format(INBOX)
    assert out["route_rule"] == "inbox"
    assert "GL-1" in out["route_reason"]


# A routed Inbox create is refused only for a missing Done-when line


@pytest.mark.parametrize("text,assignee,why", [
    ("HMI scaling on Ghostrunner", "person-a", "HMI exists only in GL-1"),
    ("look into the odd noise on GL-1", "person-b", "no folder keyword matches"),
    ("look into the odd noise", None, "names no product"),
])
def test_routed_inbox_create_without_a_proposal_is_unresolved_not_refused(text, assignee, why,
                                                                          config):
    result, fake, audits = run([create(route_from=text, assignee=assignee,
                                       description="Done when: x")], config)
    assert result["ok"] is True, result
    (_, path, payload), = [call for call in fake.calls if call[0] == "POST"]
    assert path == "/list/{}/task".format(INBOX)
    first, second = payload["description"].splitlines()[:2]
    assert first == "Done when: x"
    assert second.startswith("Proposed folder: unresolved (")
    assert why in second and second.endswith("owner files at triage)")
    assert result["succeeded"][0]["route_rule"] == "inbox"
    assert audits[0][1]["route_rule"] == "inbox"
    assert why in audits[0][1]["route_reason"]


def test_routed_create_without_done_when_is_refused_and_reports_the_route(config):
    result, fake, audits = run([create(route_from="HMI scaling on Ghostrunner",
                                       description="Proposed folder: GL-1/HMI")], config)
    assert result["ok"] is False
    failed, = result["failed"]
    assert "Done when" in failed["error"]["message"]
    assert failed["route_rule"] == "inbox"
    assert "HMI" in failed["route_reason"]
    assert fake.calls == [] and audits == []


def test_cli_dry_run_unresolved_inbox_route_is_at_the_top_level(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "clickup_write.py", "create-task", "--name", "HMI", "--assignee", "person-a",
        "--route-from", "HMI scaling on Ghostrunner", "--description", "Done when: x",
    ])
    assert clickup_write.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["route_rule"] == "inbox"
    assert "HMI exists only in GL-1" in out["route_reason"]
    assert out["planned_request"]["path"] == "/list/{}/task".format(INBOX)
    assert "Proposed folder: unresolved (" in out["planned_request"]["payload"]["description"]


def test_cli_dry_run_refusal_reports_the_route(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "clickup_write.py", "create-task", "--name", "X", "--route-from",
        "HMI scaling on Ghostrunner", "--description", "no done-when line",
    ])
    assert clickup_write.main() == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["route_rule"] == "inbox"


def test_batch_dry_run_items_carry_the_route(monkeypatch, tmp_path, capsys):
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        create(operation_id="a", route_from="check the DCDC grounding on GL-1",
               assignee="person-b"),
        create(operation_id="b", route_from="HMI scaling on Ghostrunner", description="nope"),
    ]))
    monkeypatch.setattr(sys, "argv", ["clickup_write.py", "batch", "--operations-file", str(ops)])
    assert clickup_write.main() == 0
    first, second = json.loads(capsys.readouterr().out)["operations"]
    assert first["route_rule"] == "keyword-owner"
    assert second["ok"] is False and second["route_rule"] == "inbox"
