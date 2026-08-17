import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def is_gitignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        ".secrets/clickup.env",
        ".secrets/sentry.env",
        ".env",
        ".env.local",
        "data/audit-log.jsonl",
        "data/approval-queue.jsonl",
        "data/captain-channels.json",
        "data/captain-modes.json",
        "data/clickup-tasks.json",
        "data/critical-path-overrides.json",
        "data/critical-paths.json",
        "data/meeting-ingestion.json",
        "data/meeting-transcript-clickup-reconciliation-state.json",
        "data/sentry-bridge-state.json",
        "data/captain.sqlite",
        "data/captain.sqlite-journal",
        "data/captain.sqlite-wal",
        "data/captain.sqlite-shm",
        "agent-plugin/.venv/bin/python",
        "agent-plugin/local.sqlite3",
        "logs/captain.log",
        "memory/daily/2026-08-13.md",
        "reports/daily-activity.md",
    ],
)
def test_sensitive_and_runtime_private_paths_are_gitignored(path):
    assert is_gitignored(path), f"runtime-private path is not ignored: {path}"


@pytest.mark.parametrize(
    "path",
    [
        "data/captain-channels.example.json",
        "data/captain-modes.example.json",
        "data/critical-path-overrides.example.json",
        "data/meeting-ingestion.example.json",
        "profiles/openclaw.yml",
        "CLAW.md",
        "README.md",
        "tests/test_public_package_contract.py",
    ],
)
def test_shipped_product_artifacts_are_not_gitignored(path):
    assert not is_gitignored(path), f"shipped artifact is unexpectedly ignored: {path}"
