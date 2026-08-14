#!/usr/bin/env python3
"""Render a host-specific launchd plist for Captain's Sentry bridge."""

import argparse
import os
import plistlib
import sys
from pathlib import Path


def build_launchd_config(workspace: Path, python_executable: Path, path_env: str) -> dict:
    """Build a launchd plist configuration using the caller's host paths."""
    workspace = workspace.expanduser().resolve()
    python_executable = python_executable.expanduser().resolve()
    return {
        "Label": "ai.openclaw.captain-sentry-bridge",
        "ProgramArguments": [str(python_executable), "scripts/openclaw_cron_sentry_bridge.py"],
        "WorkingDirectory": str(workspace),
        "Umask": 0o077,
        "RunAtLoad": True,
        "StartInterval": 600,
        "EnvironmentVariables": {"PATH": path_env},
        "StandardOutPath": str(workspace / "logs" / "sentry-bridge.out.log"),
        "StandardErrorPath": str(workspace / "logs" / "sentry-bridge.err.log"),
    }


def write_plist(output: Path, config: dict) -> None:
    """Write a parseable, owner-private plist atomically."""
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(plistlib.dumps(config, sort_keys=True))
    os.chmod(temporary, 0o600)
    temporary.replace(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a host-specific launchd plist for the Captain Sentry bridge."
    )
    parser.add_argument("--workspace", type=Path, required=True, help="Captain workspace path")
    parser.add_argument("--output", type=Path, required=True, help="Plist output path")
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Python executable for the bridge (default: this Python)",
    )
    parser.add_argument(
        "--path",
        default="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        help="PATH passed to launchd",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = build_launchd_config(args.workspace, args.python, args.path)
    write_plist(args.output, config)


if __name__ == "__main__":
    main()
