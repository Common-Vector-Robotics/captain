from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _prompt(name: str) -> str:
    return (ROOT / "cron-prompts" / name).read_text(encoding="utf-8")


def test_morning_prompt_retains_operational_contract():
    prompt = _prompt("daily-morning-cycle.md")

    for required in (
        "data/daily-morning-cycle-state.json",
        "scripts/fetch_clickup_tasks.py",
        "scripts/daily_context.py",
        "scripts/personal_top2.py rank",
        "scripts/personal_top2.py set",
        "scripts/daily_cycle.py set-top3",
        "channel_enumeration_unavailable: true",
        "personal_texts_sent[slack_user_id]",
        '"posted_brief"',
    ):
        assert required in prompt


def test_blocker_prompt_retains_same_cycle_contract():
    prompt = _prompt("daily-blocker-chase.md")

    for required in (
        "no open blocker\nends the day unowned",
        "data/daily-blocker-chase-state.json",
        "scripts/blocker_ledger.py list",
        "scripts/fetch_clickup_tasks.py",
        "scripts/clickup_write.py --execute update-task",
        "create-task --list-id <most relevant list>",
        "pings_sent[owner_slack_id]",
        "Same-cycle check",
        "needs_task_match",
    ):
        assert required in prompt


def test_bench_prompt_retains_reconciliation_and_safety_contract():
    prompt = _prompt("daily-bench-truth-watch.md")

    for required in (
        "data/daily-bench-truth-state.json",
        "The bench wins",
        "reply_status: pending|reconciled",
        "scripts/fetch_clickup_tasks.py",
        "channel_enumeration_unavailable: true",
        "INCIDENT: <summary>",
        '"posted_digest"',
    ):
        assert required in prompt


def test_eod_prompt_retains_wrap_and_tomorrow_plan_contract():
    prompt = _prompt("daily-eod-wrap.md")

    for required in (
        "data/daily-eod-wrap-state.json",
        "scripts/fetch_clickup_tasks.py",
        "scripts/critical_paths.py write-state",
        "scripts/daily_wrap.py",
        "Milestone diamond",
        "scripts/daily_cycle.py set-tomorrow",
        "scripts/daily_cycle.py stamp",
        "Tomorrow's top 3",
    ):
        assert required in prompt


def test_daily_prompts_use_runtime_configuration_not_private_deployment_literals():
    prompts = "\n".join(
        _prompt(name)
        for name in (
            "daily-morning-cycle.md",
            "daily-blocker-chase.md",
            "daily-bench-truth-watch.md",
            "daily-eod-wrap.md",
        )
    )

    for forbidden in (
        "/Users/owen",
        "#captains-quarters",
        "#dry-dock",
        "U0B4G00QXT8",
        "U043AKSJC85",
        "U09MVE90E4C",
        "AgentOwen",
    ):
        assert forbidden not in prompts

    for configured_field in (
        "program_channel",
        "shadow_recipient",
        "admin_recipients",
        "excluded_user_ids",
        "slack_account",
    ):
        assert configured_field in prompts
