#!/usr/bin/env python3
"""Install and verify Captain's managed heartbeat policy in OpenClaw.

Captain keeps its heartbeat instructions in ``HEARTBEAT.md``. OpenClaw's
lightweight heartbeat mode needs a copy of those instructions in its local
configuration. This script copies them safely and then proves that OpenClaw
stored the exact same text.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Callable


# This is the OpenClaw setting where Captain's heartbeat instructions belong.
# Change this only if OpenClaw changes the setting name in a future release.
CONFIG_PATH = "agents.entries.captain.heartbeat.prompt"

# The script lives in ``scripts/``, so its parent directory is Captain's root
# folder. Building the path this way makes the command work from any directory.
POLICY_PATH = Path(__file__).resolve().parents[1] / "HEARTBEAT.md"


def install_policy(
    policy_path: Path = POLICY_PATH,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Install the policy fail-closed and return its verified SHA-256."""

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
    return actual_hash


def main() -> None:
    """Install the packaged policy and print a beginner-friendly confirmation."""

    # Reaching these messages means the preview, write, read-back, byte check,
    # and hash check all succeeded. Any earlier problem exits with an error.
    verified_hash = install_policy()
    print("Captain heartbeat policy installed and verified.")
    print(f"SHA-256: {verified_hash}")


# Run main() only when a person executes this file. Importing the module in a
# test or another script does not change OpenClaw configuration automatically.
if __name__ == "__main__":
    main()
