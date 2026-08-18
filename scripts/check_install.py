#!/usr/bin/env python3
"""Check a Captain installation without changing OpenClaw or external services.

Run this script from Captain's installed workspace. It checks local settings,
OpenClaw, Slack routing, Google read access, and ClickUp read access. Every
external command used here is read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_OPENCLAW_VERSION = "2026.7.2-beta.5"
EXPECTED_JOB_NAMES = {
    "Captain daily morning cycle",
    "Captain daily blocker chase",
    "Captain meeting transcript reconciliation",
    "Captain daily bench truth and channel watch",
    "Captain daily EOD wrap",
    "Action summary reporting",
}
EXPECTED_GOOGLE_SCOPES = {
    "email",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
}
EXAMPLE_VALUES = {
    "C0123456789",
    "U0123456789",
    "Firstname Lastname",
    "captain@example.com",
    "YOUR_",
}


class InstallationCheckError(RuntimeError):
    """Explain one setup problem in language an operator can act on."""


def _read_object(path: Path) -> dict:
    """Read one required JSON object and give a short error when it is invalid."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallationCheckError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise InstallationCheckError(f"{path} must contain one JSON object")
    return value


def _strings(value: object):
    """Yield every string inside a JSON-shaped value."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _require_text(config: dict, key: str, path: Path) -> str:
    """Return a required non-empty string setting."""

    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InstallationCheckError(f"{path} is missing {key}")
    return value


def _check_local_settings(root: Path, expected_mode: str) -> tuple[dict, dict, str]:
    """Validate private local files before making any network request."""

    channels_path = root / "data" / "captain-channels.json"
    meeting_path = root / "data" / "meeting-ingestion.json"
    channels = _read_object(channels_path)
    meeting = _read_object(meeting_path)

    for value in (*_strings(channels), *_strings(meeting)):
        if any(example in value for example in EXAMPLE_VALUES) or "replace-with" in value:
            raise InstallationCheckError(
                f"replace the example value {value!r} in Captain's local settings"
            )

    if _require_text(channels, "slack_account", channels_path) != "captain":
        raise InstallationCheckError("slack_account must be exactly 'captain'")
    _require_text(channels, "shadow_recipient", channels_path)
    _require_text(channels, "activity_digest_channel", channels_path)
    if not isinstance(channels.get("mode_toggle_users"), dict) or not channels[
        "mode_toggle_users"
    ]:
        raise InstallationCheckError(f"{channels_path} needs mode_toggle_users")
    program_channel = channels.get("program_channel")
    if not isinstance(program_channel, (str, dict)) or not program_channel:
        raise InstallationCheckError(f"{channels_path} needs program_channel")

    google_cli = _require_text(meeting, "google_cli", meeting_path)
    google_account = _require_text(meeting, "google_account", meeting_path)
    _require_text(meeting, "sender", meeting_path)
    for key in ("subject_prefixes", "meeting_title_patterns"):
        values = meeting.get(key)
        if not isinstance(values, list) or not values or not all(
            isinstance(item, str) and item.strip() for item in values
        ):
            raise InstallationCheckError(f"{meeting_path} needs non-empty {key}")

    modes_path = root / "data" / "captain-modes.json"
    if modes_path.exists():
        modes = _read_object(modes_path)
        daily_loop = modes.get("DailyLoop")
        mode = daily_loop.get("audience") if isinstance(daily_loop, dict) else None
        mode = mode or "off"
    else:
        mode = "off"
    if mode != expected_mode:
        raise InstallationCheckError(
            f"DailyLoop is {mode!r}; expected {expected_mode!r}"
        )
    return meeting, channels, google_cli


def _run(
    command: list[str],
    *,
    label: str,
    run: Callable[..., subprocess.CompletedProcess],
    env: dict[str, str] | None = None,
) -> str:
    """Run one read-only command and turn failures into a short setup message."""

    try:
        result = run(
            command,
            check=True,
            capture_output=True,
            text=True,
            **({"env": env} if env is not None else {}),
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise InstallationCheckError(f"{label} failed: {detail.strip()}") from error
    return result.stdout


def _json_output(output: str, label: str) -> object:
    """Decode JSON returned by a verified read-only command."""

    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise InstallationCheckError(f"{label} returned invalid JSON") from error


def _check_openclaw(
    root: Path,
    expected_heartbeat: str,
    run: Callable[..., subprocess.CompletedProcess],
) -> list[str]:
    """Check OpenClaw, the installed Claw, schedules, binding, and policy."""

    version = _run(["openclaw", "--version"], label="OpenClaw version", run=run)
    if f"OpenClaw {SUPPORTED_OPENCLAW_VERSION}" not in version:
        raise InstallationCheckError(
            f"Captain supports OpenClaw {SUPPORTED_OPENCLAW_VERSION}; got {version.strip()}"
        )

    gateway = _json_output(
        _run(
            ["openclaw", "gateway", "status", "--json"],
            label="OpenClaw Gateway check",
            run=run,
        ),
        "OpenClaw Gateway check",
    )
    if not isinstance(gateway, dict) or gateway.get("rpc", {}).get("ok") is not True:
        raise InstallationCheckError("OpenClaw Gateway is not reachable")

    environment = dict(os.environ)
    environment["OPENCLAW_EXPERIMENTAL_CLAWS"] = "1"
    status = _json_output(
        _run(
            ["openclaw", "claws", "status", "captain", "--json"],
            label="Captain Claw status",
            run=run,
            env=environment,
        ),
        "Captain Claw status",
    )
    summary = status.get("summary", {}) if isinstance(status, dict) else {}
    records = status.get("records", []) if isinstance(status, dict) else []
    complete = any(
        isinstance(record, dict)
        and isinstance(record.get("install"), dict)
        and record["install"].get("status") == "complete"
        for record in records
    )
    if not complete or any(
        summary.get(key, 0) != expected
        for key, expected in {
            "claws": 1,
            "partial": 0,
            "driftedFiles": 0,
            "cronRefs": 6,
            "unresolvedCronRefs": 0,
        }.items()
    ):
        raise InstallationCheckError("Captain Claw installation is incomplete or drifted")

    cron = _json_output(
        _run(
            [
                "openclaw", "cron", "list", "--agent", "captain", "--all", "--json",
            ],
            label="Captain schedule check",
            run=run,
        ),
        "Captain schedule check",
    )
    jobs = cron.get("jobs", []) if isinstance(cron, dict) else []
    captain_jobs = [job for job in jobs if job.get("agentId") == "captain"]
    names = {job.get("name") for job in captain_jobs}
    if names != EXPECTED_JOB_NAMES | {"heartbeat-captain"}:
        raise InstallationCheckError("Captain must have six jobs and one heartbeat")
    scheduled = [job for job in captain_jobs if job.get("schedule", {}).get("kind") == "cron"]
    timezones = {job.get("schedule", {}).get("tz") for job in scheduled}
    if len(scheduled) != 6 or len(timezones) != 1 or None in timezones:
        raise InstallationCheckError("Captain's six jobs must use one explicit timezone")

    bindings = _json_output(
        _run(
            ["openclaw", "agents", "bindings", "--agent", "captain", "--json"],
            label="Captain Slack binding check",
            run=run,
        ),
        "Captain Slack binding check",
    )
    if isinstance(bindings, dict):
        bindings = bindings.get("bindings", [])
    has_captain_binding = isinstance(bindings, list) and any(
        isinstance(binding, dict)
        and binding.get("agentId") == "captain"
        and isinstance(binding.get("match"), dict)
        and binding["match"].get("channel") == "slack"
        and binding["match"].get("accountId") == "captain"
        for binding in bindings
    )
    if not has_captain_binding:
        raise InstallationCheckError("Captain is missing the slack:captain agent binding")

    channel_status = _json_output(
        _run(
            ["openclaw", "channels", "status", "--probe", "--json"],
            label="Captain Slack connection check",
            run=run,
        ),
        "Captain Slack connection check",
    )
    channel_accounts = (
        channel_status.get("channelAccounts", {}).get("slack", [])
        if isinstance(channel_status, dict)
        else []
    )
    captain_accounts = [
        account
        for account in channel_accounts
        if isinstance(account, dict) and account.get("accountId") == "captain"
    ]
    if (
        len(captain_accounts) != 1
        or captain_accounts[0].get("configured") is not True
        or captain_accounts[0].get("running") is not True
        or captain_accounts[0].get("probe", {}).get("ok") is not True
    ):
        raise InstallationCheckError("Captain Slack account is not configured and healthy")
    prompt = _json_output(
        _run(
            [
                "openclaw", "config", "get",
                "agents.entries.captain.heartbeat.prompt", "--json",
            ],
            label="Captain heartbeat policy check",
            run=run,
        ),
        "Captain heartbeat policy check",
    )
    expected_prompt = (root / "HEARTBEAT.md").read_text(encoding="utf-8")
    if prompt != expected_prompt:
        raise InstallationCheckError("Captain heartbeat policy does not match HEARTBEAT.md")
    cadence = _json_output(
        _run(
            [
                "openclaw", "config", "get",
                "agents.entries.captain.heartbeat.every", "--json",
            ],
            label="Captain heartbeat cadence check",
            run=run,
        ),
        "Captain heartbeat cadence check",
    )
    if cadence != expected_heartbeat:
        raise InstallationCheckError(
            f"Captain heartbeat is {cadence!r}; expected {expected_heartbeat!r}"
        )
    return [
        "OpenClaw and Gateway",
        "Captain Claw and schedules",
        "Captain Slack connection and binding",
        "Captain heartbeat policy",
    ]


def _google_records(value: object) -> list[dict]:
    """Normalize the simple gog account-list shapes used by supported versions."""

    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("accounts", "records"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
        if "account" in value:
            return [value]
    return []


def _check_connections(
    root: Path,
    meeting: dict,
    google_cli: str,
    run: Callable[..., subprocess.CompletedProcess],
) -> list[str]:
    """Make one read-only Google check and one read-only ClickUp fetch."""

    account = meeting["google_account"]
    auth = _json_output(
        _run(
            [
                google_cli, "auth", "list", "--check",
                "--account", account, "--no-input", "--json",
            ],
            label="Captain Google account check",
            run=run,
        ),
        "Captain Google account check",
    )
    matches = [
        record
        for record in _google_records(auth)
        if (record.get("account") or record.get("email")) == account
    ]
    if len(matches) != 1 or matches[0].get("valid") is not True:
        raise InstallationCheckError(f"Google account {account} is not valid")
    if set(matches[0].get("scopes", [])) != EXPECTED_GOOGLE_SCOPES:
        raise InstallationCheckError(f"Google account {account} has the wrong scopes")

    with tempfile.TemporaryDirectory(prefix="captain-install-check-") as directory:
        output = _run(
            [
                sys.executable,
                str(root / "scripts" / "fetch_clickup_tasks.py"),
                "--out",
                str(Path(directory) / "clickup.json"),
            ],
            label="Captain ClickUp read check",
            run=run,
        )
    result = _json_output(output, "Captain ClickUp read check")
    if not isinstance(result, dict) or not isinstance(result.get("tasks"), int):
        raise InstallationCheckError("Captain ClickUp read check returned no task count")
    return ["Google read access", "ClickUp read access"]


def run_checks(
    *,
    root: Path = ROOT,
    expected_mode: str = "off",
    expected_heartbeat: str = "0m",
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str]:
    """Run every installation check and return beginner-friendly pass messages."""

    if expected_mode not in {"off", "shadow", "live"}:
        raise InstallationCheckError("expected mode must be off, shadow, or live")
    if expected_heartbeat not in {"0m", "60m"}:
        raise InstallationCheckError("expected heartbeat must be 0m or 60m")

    meeting, _channels, google_cli = _check_local_settings(root, expected_mode)
    checks = ["Local Captain settings and mode"]
    checks.extend(_check_openclaw(root, expected_heartbeat, run))
    checks.extend(_check_connections(root, meeting, google_cli, run))
    checks.append(
        "Captain is ready for shadow mode."
        if expected_mode == "off"
        else f"Captain {expected_mode} installation verified."
    )
    return checks


def main() -> None:
    """Parse simple expectations, run checks, and print one result per line."""

    parser = argparse.ArgumentParser(
        description="Verify a Captain installation without changing external services"
    )
    parser.add_argument(
        "--expect-mode",
        choices=("off", "shadow", "live"),
        default="off",
    )
    parser.add_argument(
        "--expect-heartbeat",
        choices=("0m", "60m"),
        default="0m",
    )
    args = parser.parse_args()
    try:
        checks = run_checks(
            expected_mode=args.expect_mode,
            expected_heartbeat=args.expect_heartbeat,
        )
    except InstallationCheckError as error:
        raise SystemExit(f"Installation check failed: {error}") from error
    for check in checks:
        print(f"[PASS] {check}")


if __name__ == "__main__":
    main()
