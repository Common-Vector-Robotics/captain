import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PATTERNS = (
    r"/Users/[^/\s]+/",
    r"\b[\w.+-]+@intermode\.io\b",
    r"\bU[A-Z0-9]{8,}\b",
)


def _cron_block(cron_id):
    """Find one scheduled-job section in the public package description."""
    manifest = (ROOT / "CLAW.md").read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^  - id: {re.escape(cron_id)}\n(?P<body>.*?)(?=^  - id:|^---$)",
        manifest,
    )
    assert match is not None, f"missing Claw cron {cron_id}"
    return match.group("body")


def test_ingestion_dependencies_are_packaged():
    """Confirm the meeting settings and instructions ship with the package."""
    manifest = (ROOT / "CLAW.md").read_text(encoding="utf-8")
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    for path in (
        "data/meeting-ingestion.example.json",
        "cron-prompts/meeting-transcript-clickup-reconciliation.md",
    ):
        assert f"    - source: {path}\n      path: {path}\n" in manifest
        assert path in package["files"]


def test_ingestion_cron_contract():
    """Confirm the packaged meeting job keeps its expected schedule and safeguards."""
    block = _cron_block("meeting-transcript-reconciliation")
    assert "    name: Captain meeting transcript reconciliation\n" in block
    assert '      cron: "0 14 * * 1-5"\n' in block
    assert "      timezone: America/Detroit\n" in block
    assert "    session: isolated\n" in block
    assert "      mode: none\n" in block
    assert "meeting-transcript-clickup-reconciliation.md" in block
    assert "Final response must be NO_REPLY." in block


def test_example_config_is_safe_and_complete():
    """Confirm the example settings are complete and use placeholder information."""
    config = json.loads(
        (ROOT / "data" / "meeting-ingestion.example.json").read_text(
            encoding="utf-8"
        )
    )
    assert config == {
        "google_cli": "gog",
        "google_account": "captain@example.com",
        "sender": "gemini-notes@google.com",
        "subject_prefixes": ["Notes:"],
        "meeting_title_patterns": [
            "Daily Standup",
            "Weekly Standup",
            "EOW Standup",
        ],
        "lookback_days": 10,
        "local_summary_directory": None,
    }


def test_prompt_keeps_the_runtime_safety_contract():
    """Confirm the meeting instructions retain every required safety rule."""
    prompt = (
        ROOT / "cron-prompts" / "meeting-transcript-clickup-reconciliation.md"
    ).read_text(encoding="utf-8")
    required = (
        "data/meeting-ingestion.json",
        "data/captain-channels.json",
        "data/captain-modes.json",
        "Transcript first",
        "Notes",
        "partial",
        "scripts/clickup_write.py",
        "audit-log.jsonl",
        "shadow_recipient",
        "program_channel",
        "slack_account",
        "excluded_user_ids",
        "NO_REPLY",
        "30",
    )
    for value in required:
        assert value in prompt


def test_prompt_normalizes_object_and_string_program_channels():
    """Catch regressions that stringify routing objects or retarget live sends."""
    prompt = (
        ROOT / "cron-prompts" / "meeting-transcript-clickup-reconciliation.md"
    ).read_text(encoding="utf-8")
    expected_examples = (
        (
            '{"name":"captains-quarters","id":"C0123456789"}',
            "#captains-quarters",
            "C0123456789",
        ),
        ('"captains-quarters"', "#captains-quarters", "captains-quarters"),
    )

    for configured_value, display_label, live_target in expected_examples:
        row = f"| `{configured_value}` | `{display_label}` | `{live_target}` |"
        assert row in prompt
    assert "Never render the object itself" in prompt
    assert "account=slack_account" in prompt


def test_prompt_uses_exact_noninteractive_docs_cat_contract():
    """Catch ambiguous Google Docs export guidance that writes to a fake stdout path."""
    prompt = (
        ROOT / "cron-prompts" / "meeting-transcript-clickup-reconciliation.md"
    ).read_text(encoding="utf-8")

    assert (
        "<google_cli> docs cat <docId> --account <google_account> --no-input"
        in prompt
    )


def test_prompt_uses_configured_account_for_raw_gmail_fallback():
    """Catch a raw-message fallback that bypasses the configured CLI or account."""
    prompt = (
        ROOT / "cron-prompts" / "meeting-transcript-clickup-reconciliation.md"
    ).read_text(encoding="utf-8")

    assert (
        "<google_cli> gmail get <message-id> --format raw --json "
        "--account <google_account> --no-input"
        in prompt
    )
    assert re.search(r"(?<!<)\bgog gmail get\b", prompt) is None


def test_prompt_fails_closed_instead_of_switching_google_access_paths():
    """Catch fallback to a browser, alternate account, or broader authorization."""
    prompt = (
        ROOT / "cron-prompts" / "meeting-transcript-clickup-reconciliation.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())

    assert "browser access" not in prompt
    assert (
        "Never use a browser, another executable, another account, or a "
        "broader-scope token as a fallback."
        in normalized
    )
    assert (
        "If the configured executable and account cannot read both document "
        "tabs with least-privilege read-only authorization, record a "
        "source-access blocker and fail closed."
        in normalized
    )


def test_prompt_uses_regular_file_for_non_docs_drive_downloads():
    """Reject special stdout paths and extension-dependent Drive downloads."""
    prompt = (
        ROOT / "cron-prompts" / "meeting-transcript-clickup-reconciliation.md"
    ).read_text(encoding="utf-8")

    assert (
        "<google_cli> drive download <fileId> --out "
        "<temporary-file-with-extension> --account <google_account> --no-input"
        in prompt
    )
    assert "regular file inside an owner-only temporary directory" in prompt
    assert "Do not use `--format`" in prompt
    assert re.search(r"--out\s+`?/dev/stdout(?:\.txt)?`?", prompt) is None
    assert "/dev/stdout.txt" not in prompt


def test_ingestion_artifacts_exclude_private_deployment_data():
    """Confirm public meeting files contain no private paths, emails, or Slack IDs."""
    paths = (
        ROOT / "data" / "meeting-ingestion.example.json",
        ROOT / "cron-prompts" / "meeting-transcript-clickup-reconciliation.md",
    )
    combined = "".join(path.read_text(encoding="utf-8") for path in paths)
    for pattern in PRIVATE_PATTERNS:
        assert re.search(pattern, combined) is None
