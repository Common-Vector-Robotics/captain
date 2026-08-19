#!/usr/bin/env python3
"""Install and verify Captain's managed heartbeat policy in OpenClaw.

Captain keeps its heartbeat instructions in ``HEARTBEAT.md``. OpenClaw's
lightweight heartbeat mode needs a copy of those instructions in its local
configuration. This script copies them safely and then proves that OpenClaw
stored the exact same text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Callable


# This is the OpenClaw setting where Captain's heartbeat instructions belong.
# Change this only if OpenClaw changes the setting name in a future release.
CONFIG_PATH = "agents.entries.captain.heartbeat.prompt"
CADENCE_CONFIG_PATH = "agents.entries.captain.heartbeat.every"
DISABLED_CADENCE = "0m"
ENABLED_CADENCE = "60m"

# The script lives in ``scripts/``, so its parent directory is Captain's root
# folder. Building the path this way makes the command work from any directory.
POLICY_PATH = Path(__file__).resolve().parents[1] / "HEARTBEAT.md"


def install_policy(
    policy_path: Path = POLICY_PATH,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    *,
    enable: bool = False,
) -> str:
    """Install the policy and optionally enable its hourly schedule safely."""

    # Read bytes first so the later comparison can detect even subtle changes,
    # such as a missing final newline. ``strict=True`` also gives a clear error
    # if the policy file is missing.
    policy_bytes = policy_path.resolve(strict=True).read_bytes()

    # OpenClaw expects text. Invalid UTF-8 stops here instead of installing a
    # damaged or partially decoded policy.
    policy = policy_bytes.decode("utf-8")

    # JSON encoding turns the entire policy into one safe command argument.
    # No shell is used, so quotes or symbols in HEARTBEAT.md are not executed.
    encoded_policy = json.dumps(policy, ensure_ascii=False)

    # Keeping the command as a list means Python passes each item directly to
    # OpenClaw. The final flag tells OpenClaw that encoded_policy is JSON.
    set_command = [
        "openclaw",
        "config",
        "set",
        CONFIG_PATH,
        encoded_policy,
        "--strict-json",
    ]

    # Always preview the change first. ``check=True`` stops immediately if the
    # preview fails, so the real setting is never attempted after a bad dry run.
    run([*set_command, "--dry-run"], check=True)

    # The preview succeeded, so apply the exact same command without --dry-run.
    run(set_command, check=True)

    # Read the saved value back from OpenClaw. Capturing stdout lets us verify
    # what was actually stored instead of assuming the write worked correctly.
    result = run(
        ["openclaw", "config", "get", CONFIG_PATH, "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    # ``config get --json`` should return one JSON string. Reject malformed JSON
    # and other JSON types, such as an object or list, before comparing values.
    try:
        actual = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit("OpenClaw returned an invalid heartbeat prompt") from error
    if not isinstance(actual, str):
        raise SystemExit("Captain heartbeat prompt read-back is not a string")

    # Compare the stored value with the original file byte for byte. Any change
    # is unsafe because it could alter Captain's heartbeat behavior.
    actual_bytes = actual.encode("utf-8")
    if actual_bytes != policy_bytes:
        raise SystemExit("Captain heartbeat prompt does not exactly match HEARTBEAT.md")

    # The hash is a short, repeatable identifier for the verified policy. It is
    # useful when checking logs or confirming that two installations match.
    expected_hash = hashlib.sha256(policy_bytes).hexdigest()
    actual_hash = hashlib.sha256(actual_bytes).hexdigest()
    if actual_hash != expected_hash:
        raise SystemExit("Captain heartbeat prompt SHA-256 verification failed")

    if enable:
        enable_heartbeat(run=run)
    return actual_hash


def _cadence_command(value: str) -> list[str]:
    """Build one direct OpenClaw command for Captain's heartbeat cadence."""

    return [
        "openclaw",
        "config",
        "set",
        CADENCE_CONFIG_PATH,
        json.dumps(value),
        "--strict-json",
    ]


def _read_cadence(
    run: Callable[..., subprocess.CompletedProcess],
) -> str:
    """Read and validate the stored heartbeat cadence as one JSON string."""

    result = run(
        ["openclaw", "config", "get", CADENCE_CONFIG_PATH, "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("OpenClaw returned an invalid heartbeat cadence") from error
    if not isinstance(value, str):
        raise ValueError("Captain heartbeat cadence read-back is not a string")
    return value


def _restore_disabled_cadence(
    run: Callable[..., subprocess.CompletedProcess],
) -> None:
    """Return the heartbeat to its packaged disabled state after a bad check."""

    command = _cadence_command(DISABLED_CADENCE)
    run([*command, "--dry-run"], check=True)
    run(command, check=True)
    if _read_cadence(run) != DISABLED_CADENCE:
        raise SystemExit("Captain heartbeat cadence rollback could not be verified")


def enable_heartbeat(
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    """Enable the hourly heartbeat only after applying and verifying its policy."""

    command = _cadence_command(ENABLED_CADENCE)
    run([*command, "--dry-run"], check=True)
    run(command, check=True)
    try:
        actual = _read_cadence(run)
    except (ValueError, subprocess.CalledProcessError) as error:
        _restore_disabled_cadence(run)
        raise SystemExit("Captain heartbeat cadence verification failed") from error
    if actual != ENABLED_CADENCE:
        _restore_disabled_cadence(run)
        raise SystemExit("Captain heartbeat cadence verification failed")


def main() -> None:
    """Install the packaged policy and print a beginner-friendly confirmation."""

    parser = argparse.ArgumentParser(
        description="Install and verify Captain's OpenClaw heartbeat safety policy"
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="enable the hourly heartbeat after the policy is verified",
    )
    args = parser.parse_args()

    # Reaching these messages means the preview, write, read-back, byte check,
    # and hash check all succeeded. Any earlier problem exits with an error.
    verified_hash = install_policy(enable=args.enable)
    print("Captain heartbeat policy installed and verified.")
    print(f"SHA-256: {verified_hash}")
    if args.enable:
        print("Captain heartbeat enabled: every 60m.")
    else:
        print("Captain heartbeat remains disabled until setup is complete.")


# Run main() only when a person executes this file. Importing the module in a
# test or another script does not change OpenClaw configuration automatically.
if __name__ == "__main__":
    main()
