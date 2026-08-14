#!/usr/bin/env python3
"""Render a host-specific launchd plist for Captain's Sentry bridge.

``launchd`` is the service manager built into macOS. It reads configuration
files called property lists (``.plist`` files). This script creates that file
using paths supplied by the person installing Captain, so the repository does
not need to contain paths that only work on one computer.
"""

import argparse
import os
import plistlib
import sys
from pathlib import Path


def build_launchd_config(workspace: Path, python_executable: Path, path_env: str) -> dict:
    """Build a launchd plist configuration using the caller's host paths."""

    # Convert shortcuts such as ``~`` and relative paths into complete paths.
    # launchd does not run inside an interactive shell, so explicit paths are
    # more reliable than depending on the user's current directory or aliases.
    workspace = workspace.expanduser().resolve()
    python_executable = python_executable.expanduser().resolve()

    return {
        # ``Label`` is the unique name launchd uses to identify this service.
        "Label": "ai.openclaw.captain-sentry-bridge",

        # Run the bridge with the exact Python executable selected by the user.
        "ProgramArguments": [
            str(python_executable),
            "scripts/openclaw_cron_sentry_bridge.py",
        ],

        # Relative script and data paths are resolved from Captain's workspace.
        "WorkingDirectory": str(workspace),

        # Files created by the bridge should only be accessible by their owner.
        "Umask": 0o077,

        # Run once when loaded, then repeat every 600 seconds (10 minutes).
        "RunAtLoad": True,
        "StartInterval": 600,

        # launchd starts with a small environment, so provide an explicit PATH.
        "EnvironmentVariables": {"PATH": path_env},

        # Keep normal output and errors in separate workspace log files.
        "StandardOutPath": str(workspace / "logs" / "sentry-bridge.out.log"),
        "StandardErrorPath": str(workspace / "logs" / "sentry-bridge.err.log"),
    }


def write_plist(output: Path, config: dict) -> None:
    """Write a parseable, owner-private plist atomically."""

    # Normalize the destination and create its parent directory when necessary.
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary file first. This prevents launchd from seeing a
    # half-written plist if the process is interrupted during serialization.
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(plistlib.dumps(config, sort_keys=True))

    # Restrict the file before publishing it, then atomically replace the final
    # destination. ``0o600`` means read/write for the owner and no access for
    # group members or other users.
    os.chmod(temporary, 0o600)
    temporary.replace(output)


def parse_args() -> argparse.Namespace:
    """Describe and parse the command-line options accepted by this script."""

    parser = argparse.ArgumentParser(
        description="Render a host-specific launchd plist for the Captain Sentry bridge."
    )

    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Captain workspace path",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Plist output path",
    )
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
    """Render the requested configuration and save it to disk."""

    # Keep command-line parsing, configuration building, and file writing as
    # separate steps so each piece is easy to understand and test independently.
    args = parse_args()
    config = build_launchd_config(args.workspace, args.python, args.path)
    write_plist(args.output, config)


if __name__ == "__main__":
    main()
