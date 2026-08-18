import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts" / "check_install.py"
GOOGLE_SCOPES = {
    "email",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
}
JOB_NAMES = {
    "Captain daily morning cycle",
    "Captain daily blocker chase",
    "Captain meeting transcript reconciliation",
    "Captain daily bench truth and channel watch",
    "Captain daily EOD wrap",
    "Action summary reporting",
}


def load_checker():
    spec = importlib.util.spec_from_file_location("check_install", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "captain"
    (root / "scripts").mkdir(parents=True)
    (root / "HEARTBEAT.md").write_text("# safe heartbeat\n", encoding="utf-8")
    (root / "CLAW.md").write_text(
        "\n".join(
            f"  - id: job-{index}\n    schedule:\n      cron: \"0 {index} * * *\"\n"
            "      timezone: America/Detroit"
            for index in range(6)
        ),
        encoding="utf-8",
    )
    write_json(
        root / "data" / "captain-channels.json",
        {
            "admin_recipients": {"Operator": "U1111111111"},
            "mode_toggle_users": {"Operator": "U1111111111"},
            "shadow_recipient": "channel:C1111111111",
            "activity_digest_channel": "channel:C1111111111",
            "slack_account": "captain",
            "program_channel": {"name": "captains-quarters", "id": "C2222222222"},
        },
    )
    write_json(
        root / "data" / "meeting-ingestion.json",
        {
            "google_cli": "/private/captain-gog",
            "google_account": "captain@team.test",
            "sender": "gemini-notes@google.com",
            "subject_prefixes": ["Notes:"],
            "meeting_title_patterns": ["Standup"],
            "lookback_days": 10,
            "local_summary_directory": None,
        },
    )
    write_json(root / "data" / "captain-modes.json", {"DailyLoop": {"audience": "off"}})
    return root


def fake_runner(
    root: Path,
    *,
    include_binding: bool = True,
    captain_slack_ok: bool = True,
):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["openclaw", "--version"]:
            output = "OpenClaw 2026.7.2-beta.5 (ee929db)\n"
        elif command[:3] == ["openclaw", "gateway", "status"]:
            output = json.dumps({"rpc": {"ok": True}})
        elif command[:3] == ["openclaw", "claws", "status"]:
            output = json.dumps(
                {
                    "summary": {
                        "claws": 1,
                        "partial": 0,
                        "driftedFiles": 0,
                        "cronRefs": 6,
                        "unresolvedCronRefs": 0,
                    },
                    "records": [{"install": {"status": "complete"}}],
                }
            )
        elif command[:3] == ["openclaw", "cron", "list"]:
            jobs = [
                {
                    "agentId": "captain",
                    "name": name,
                    "schedule": {"kind": "cron", "tz": "America/Detroit"},
                }
                for name in sorted(JOB_NAMES)
            ]
            jobs.append(
                {
                    "agentId": "captain",
                    "name": "heartbeat-captain",
                    "schedule": {"kind": "every", "everyMs": 3_600_000},
                }
            )
            output = json.dumps({"jobs": jobs})
        elif command[:3] == ["openclaw", "agents", "bindings"]:
            output = json.dumps(
                [
                    {
                        "type": "route",
                        "agentId": "captain",
                        "match": {"channel": "slack", "accountId": "captain"},
                        "comment": "Captain account routing",
                    }
                ]
                if include_binding
                else []
            )
        elif command[:3] == ["openclaw", "channels", "status"]:
            output = json.dumps(
                {
                    "channelAccounts": {
                        "slack": [
                            {
                                "accountId": "captain",
                                "configured": True,
                                "running": captain_slack_ok,
                                "probe": {"ok": captain_slack_ok},
                            }
                        ]
                    }
                }
            )
        elif command[:3] == ["openclaw", "config", "get"]:
            if command[3].endswith("prompt"):
                output = json.dumps((root / "HEARTBEAT.md").read_text(encoding="utf-8"))
            else:
                output = json.dumps("0m")
        elif command[0] == "/private/captain-gog":
            output = json.dumps(
                {
                    "accounts": [
                        {
                            "email": "captain@team.test",
                            "valid": True,
                            "scopes": sorted(GOOGLE_SCOPES),
                        }
                    ]
                }
            )
        elif command[1].endswith("fetch_clickup_tasks.py"):
            output = json.dumps({"tasks": 5})
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    return run, calls


def test_checker_accepts_a_complete_off_installation(tmp_path):
    checker = load_checker()
    root = make_workspace(tmp_path)
    run, calls = fake_runner(root)

    checks = checker.run_checks(
        root=root,
        expected_mode="off",
        expected_heartbeat="0m",
        run=run,
    )

    assert checks[-1] == "Captain is ready for shadow mode."
    commands = [command for command, _kwargs in calls]
    assert ["openclaw", "agents", "bindings", "--agent", "captain", "--json"] in commands
    assert [
        "openclaw", "cron", "list", "--agent", "captain", "--all", "--json",
    ] in commands
    assert any(command[0] == "/private/captain-gog" for command in commands)
    assert any(command[1].endswith("fetch_clickup_tasks.py") for command in commands)


def test_checker_rejects_example_values_before_external_checks(tmp_path):
    checker = load_checker()
    root = make_workspace(tmp_path)
    channels = root / "data" / "captain-channels.json"
    value = json.loads(channels.read_text(encoding="utf-8"))
    value["shadow_recipient"] = "channel:C0123456789"
    write_json(channels, value)
    run, calls = fake_runner(root)

    with pytest.raises(checker.InstallationCheckError, match="example value"):
        checker.run_checks(root=root, expected_mode="off", expected_heartbeat="0m", run=run)

    assert calls == []


def test_checker_rejects_descriptive_documentation_placeholders(tmp_path):
    checker = load_checker()
    root = make_workspace(tmp_path)
    channels = root / "data" / "captain-channels.json"
    value = json.loads(channels.read_text(encoding="utf-8"))
    value["shadow_recipient"] = "channel:YOUR_SHADOW_CHANNEL_ID"
    write_json(channels, value)
    run, calls = fake_runner(root)

    with pytest.raises(checker.InstallationCheckError, match="example value"):
        checker.run_checks(root=root, expected_mode="off", expected_heartbeat="0m", run=run)

    assert calls == []


def test_checker_requires_the_captain_slack_binding(tmp_path):
    checker = load_checker()
    root = make_workspace(tmp_path)
    run, _calls = fake_runner(root, include_binding=False)

    with pytest.raises(checker.InstallationCheckError, match="slack:captain"):
        checker.run_checks(root=root, expected_mode="off", expected_heartbeat="0m", run=run)


def test_checker_does_not_require_source_manifest_in_installed_workspace(tmp_path):
    checker = load_checker()
    root = make_workspace(tmp_path)
    (root / "CLAW.md").unlink()
    run, _calls = fake_runner(root)

    checks = checker.run_checks(
        root=root,
        expected_mode="off",
        expected_heartbeat="0m",
        run=run,
    )

    assert checks[-1] == "Captain is ready for shadow mode."


def test_checker_requires_a_healthy_captain_slack_account(tmp_path):
    checker = load_checker()
    root = make_workspace(tmp_path)
    run, _calls = fake_runner(root, captain_slack_ok=False)

    with pytest.raises(checker.InstallationCheckError, match="Slack account"):
        checker.run_checks(root=root, expected_mode="off", expected_heartbeat="0m", run=run)
