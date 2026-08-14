import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from configure_timezone import (
    TimezoneConfigurationError,
    configure_timezone,
)


EXPECTED_JOB_IDS = {
    "morning-cycle",
    "blocker-chase",
    "meeting-transcript-reconciliation",
    "bench-truth-watch",
    "eod-wrap",
    "action-summary-reporting",
}


def _manifest_copy(tmp_path):
    path = tmp_path / "CLAW.md"
    path.write_bytes((ROOT / "CLAW.md").read_bytes())
    path.chmod(0o644)
    return path


def test_configure_timezone_updates_all_six_jobs_and_preserves_mode(tmp_path):
    manifest = _manifest_copy(tmp_path)

    result = configure_timezone(manifest, "Europe/London")

    text = manifest.read_text(encoding="utf-8")
    assert result.timezone == "Europe/London"
    assert set(result.job_ids) == EXPECTED_JOB_IDS
    assert result.changed is True
    assert text.count("      timezone: Europe/London\n") == 6
    assert "America/Detroit" not in text
    assert manifest.stat().st_mode & 0o777 == 0o644


def test_configure_timezone_check_requires_exact_existing_value(tmp_path):
    manifest = _manifest_copy(tmp_path)
    before = manifest.read_bytes()

    result = configure_timezone(manifest, "America/Detroit", check=True)

    assert result.changed is False
    assert manifest.read_bytes() == before

    with pytest.raises(
        TimezoneConfigurationError,
        match="does not match requested timezone Europe/London",
    ):
        configure_timezone(manifest, "Europe/London", check=True)
    assert manifest.read_bytes() == before


def test_configure_timezone_rejects_invalid_iana_zone_without_writing(tmp_path):
    manifest = _manifest_copy(tmp_path)
    before = manifest.read_bytes()

    with pytest.raises(TimezoneConfigurationError, match="valid IANA timezone"):
        configure_timezone(manifest, "Mars/Olympus_Mons")

    assert manifest.read_bytes() == before


def test_configure_timezone_rejects_missing_or_duplicate_jobs(tmp_path):
    manifest = _manifest_copy(tmp_path)
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace("  - id: morning-cycle\n", "  - id: blocker-chase\n", 1),
        encoding="utf-8",
    )
    before = manifest.read_bytes()

    with pytest.raises(TimezoneConfigurationError, match="exactly six Captain cron jobs"):
        configure_timezone(manifest, "UTC")

    assert manifest.read_bytes() == before


def test_configure_timezone_rejects_symlink_manifest(tmp_path):
    target = _manifest_copy(tmp_path)
    link = tmp_path / "linked-CLAW.md"
    link.symlink_to(target)

    with pytest.raises(TimezoneConfigurationError, match="regular non-symlink file"):
        configure_timezone(link, "UTC")


def test_configure_timezone_replace_failure_preserves_original(tmp_path):
    manifest = _manifest_copy(tmp_path)
    before = manifest.read_bytes()

    def fail_replace(source, destination):
        raise OSError("injected replace failure")

    with pytest.raises(OSError, match="injected replace failure"):
        configure_timezone(
            manifest,
            "Europe/London",
            replace_fn=fail_replace,
        )

    assert manifest.read_bytes() == before
    assert list(tmp_path.glob(".CLAW.md.*.tmp")) == []


def test_configure_timezone_cli_reports_changed_jobs(tmp_path, capsys):
    manifest = _manifest_copy(tmp_path)

    from configure_timezone import main

    assert main(["--manifest", str(manifest), "--timezone", "UTC"]) == 0
    output = capsys.readouterr().out
    assert "Configured 6 Captain cron jobs" in output
    assert "UTC" in output
    for job_id in EXPECTED_JOB_IDS:
        assert job_id in output
