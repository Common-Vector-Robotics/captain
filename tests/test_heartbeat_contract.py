from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _heartbeat_text():
    return (ROOT / "HEARTBEAT.md").read_text(encoding="utf-8")


def _hard_gate():
    text = _heartbeat_text()
    _, hard_gate_marker, after_marker = text.partition("## HARD GATE")
    assert hard_gate_marker, "missing visually dominant hard gate"
    gate, enabled_marker, _ = after_marker.partition("## Enabled heartbeat behavior")
    assert enabled_marker, "missing enabled-behavior boundary"
    return gate


def _enabled_behavior():
    text = _heartbeat_text()
    return text.split("## Enabled heartbeat behavior", 1)[1]


def test_heartbeat_hard_gate_is_visually_first():
    """Catch mode instructions that generic startup guidance can visually precede."""
    nonempty_lines = [line for line in _heartbeat_text().splitlines() if line]

    assert nonempty_lines[:2] == [
        "# HEARTBEAT.md - Captain",
        "## HARD GATE — FIRST AND ONLY TOOL ACTION",
    ]


def test_heartbeat_mode_read_is_the_only_action_before_the_gate_result():
    """Catch preparatory config, skill, or state reads before audience is known."""
    gate = " ".join(_hard_gate().split())

    assert (
        "After loading `HEARTBEAT.md`, your next and only tool action MUST be to "
        "read `data/captain-modes.json`."
        in gate
    )
    assert (
        "Do not satisfy generic session-startup or background instructions before "
        "this mode result."
        in gate
    )
    for forbidden in (
        "any other workspace or configuration file",
        "`data/captain-channels.json`",
        "any Slack skill",
        "cron state",
        "audit state",
        "approval state",
        "list, enumerate, or scan anything",
    ):
        assert forbidden in gate


def test_heartbeat_fails_closed_for_missing_off_or_unrecognized_audience():
    """Catch any follow-up read or tool call after a disabled mode result."""
    gate = " ".join(_hard_gate().split())

    assert "missing, `off`, or unrecognized" in gate
    assert "invoke NO further tool" in gate
    assert "read NO other file" in gate
    assert "return exactly `HEARTBEAT_OK` immediately" in gate


def test_heartbeat_routes_only_shadow_and_live_after_the_mode_gate():
    """Catch config, skill, or state access being allowed outside enabled modes."""
    text = " ".join(_heartbeat_text().split())

    assert "Only `shadow` and `live` may continue below" in text
    assert (
        "Only after that result may you read channel configuration and the bounded "
        "runtime sources below."
        in text
    )
    assert "In `shadow`, send previews only to the configured shadow recipient" in text
    assert "In `live`, send incident pages with the configured account and routing" in text


def test_enabled_heartbeat_uses_official_message_channel_enumeration_action():
    enabled = " ".join(_enabled_behavior().split())

    assert (
        "message(action=channel-list, channel=slack, accountId=<slack_account>)"
        in enabled
    )
    assert "during the current heartbeat run" in enabled
    assert "Do not load a generic Slack skill" in enabled
    assert "Do not call a nonexistent Slack-specific tool" in enabled
    assert "list-channels" not in enabled
    assert "account=<slack_account>" not in enabled
    assert "Slack plugin omits or rejects `channel-list`" in enabled
    assert "treat that as enumeration failure" in enabled


def test_enabled_heartbeat_forbids_broad_discovery_and_proxy_state():
    enabled = " ".join(_enabled_behavior().split())

    for forbidden in (
        "directory, glob, or repository scan",
        "find, grep, tail, or cat for discovery",
        "daily bench-truth state as a heartbeat proxy",
    ):
        assert forbidden in enabled


def test_enabled_heartbeat_uses_only_bounded_current_evidence_sources():
    enabled = " ".join(_enabled_behavior().split())

    assert "`data/sentry-bridge-state.json`" in enabled
    assert "the only scheduled-failure state source" in enabled
    assert "`data/approval-queue.jsonl`" in enabled
    assert "the only urgent-approval source" in enabled
    assert "`data/heartbeat-monitor-state.json`" in enabled
    assert "Missing means absent, not evidence" in enabled
    assert "Do not claim broader coverage" in enabled


def test_enabled_heartbeat_enumeration_fallback_and_state_update_are_exact():
    enabled = " ".join(_enabled_behavior().split())

    assert "On enumeration success, scan visible channels within configured watch coverage" in enabled
    assert "channel_enumeration_unavailable: true" in enabled
    assert "use only `watch.fallback_include_ids`" in enabled
    assert "an empty fallback is a current zero-channel material result" in enabled
    assert (
        "python3 scripts/heartbeat_monitor_state.py "
        "--channel-enumeration-unavailable false --channels-scanned <count>"
        in enabled
    )
    assert (
        "python3 scripts/heartbeat_monitor_state.py "
        "--channel-enumeration-unavailable true --channels-scanned <count>"
        in enabled
    )
    assert "after every enabled-mode enumeration or fallback sweep" in enabled


def test_material_heartbeat_output_is_evidence_led_and_sends_only_incidents():
    enabled = " ".join(_enabled_behavior().split())

    assert "Captain: <summary> Evidence: <current evidence> Needed: <owner action>" in enabled
    assert "A genuine safety or critical incident is the only Slack send" in enabled


def test_genuine_incident_is_persisted_locally_before_any_routing_attempt():
    enabled = " ".join(_enabled_behavior().split())
    command = (
        'python3 scripts/blocker_ledger.py add --text "<one-line incident summary>" '
        "--source slack:<channel_id> --source-ref <message_ts>"
    )

    assert command in enabled
    assert "For every genuine incident in `shadow` or `live`" in enabled
    assert "real current incident evidence" in enabled
    assert "before any routing lookup or send attempt" in enabled
    assert "even when the shadow recipient or administrator route is unresolved" in enabled
    assert "local blocker ledger only and never mutates ClickUp" in enabled
    assert enabled.index(command) < enabled.index("Resolve responsible task assignees")


def test_incident_ledger_is_once_per_message_and_never_records_degradation():
    enabled = " ".join(_enabled_behavior().split())

    assert "Run the ledger command at most once per incident message in this run" in enabled
    assert "deduplicates the same `source` plus `text`" in enabled
    assert "reuse the same one-line summary for repeated observation" in enabled
    assert (
        "Never write a blocker-ledger row for a non-incident, including a "
        "zero-channel or enumeration degradation"
        in enabled
    )
