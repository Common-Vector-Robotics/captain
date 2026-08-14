#!/usr/bin/env python3
"""Install and verify Captain's managed heartbeat policy in OpenClaw."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Callable


CONFIG_PATH = "agents.entries.captain.heartbeat.prompt"
POLICY_PATH = Path(__file__).resolve().parents[1] / "HEARTBEAT.md"


def install_policy(
    policy_path: Path = POLICY_PATH,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Install the policy fail-closed and return its verified SHA-256."""

    policy_bytes = policy_path.resolve(strict=True).read_bytes()
    policy = policy_bytes.decode("utf-8")
    encoded_policy = json.dumps(policy, ensure_ascii=False)
    set_command = [
        "openclaw",
        "config",
        "set",
        CONFIG_PATH,
        encoded_policy,
        "--strict-json",
    ]

    run([*set_command, "--dry-run"], check=True)
    run(set_command, check=True)
    result = run(
        ["openclaw", "config", "get", CONFIG_PATH, "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        actual = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit("OpenClaw returned an invalid heartbeat prompt") from error
    if not isinstance(actual, str):
        raise SystemExit("Captain heartbeat prompt read-back is not a string")

    actual_bytes = actual.encode("utf-8")
    if actual_bytes != policy_bytes:
        raise SystemExit("Captain heartbeat prompt does not exactly match HEARTBEAT.md")

    expected_hash = hashlib.sha256(policy_bytes).hexdigest()
    actual_hash = hashlib.sha256(actual_bytes).hexdigest()
    if actual_hash != expected_hash:
        raise SystemExit("Captain heartbeat prompt SHA-256 verification failed")
    return actual_hash


def main() -> None:
    """Install the packaged policy and print a beginner-friendly confirmation."""

    verified_hash = install_policy()
    print("Captain heartbeat policy installed and verified.")
    print(f"SHA-256: {verified_hash}")


if __name__ == "__main__":
    main()
