#!/usr/bin/env python3
"""Render host-specific service files for Captain's Sentry bridge.

On macOS this script renders a launchd plist; on Linux it renders a systemd
user service and timer.
"""

import argparse
import os
import plistlib
import sys
from pathlib import Path


SERVICE_NAME = "ai.openclaw.captain-sentry-bridge"


def build_launchd_config(workspace: Path, python_executable: Path, path_env: str) -> dict:
    """Build a launchd plist configuration using the caller's host paths."""

    # Convert shortcuts such as ``~`` and relative paths into complete paths.
    # launchd does not run inside an interactive shell, so explicit paths are
    # more reliable than depending on the user's current directory or aliases.
    workspace = workspace.expanduser().resolve()
    python_executable = python_executable.expanduser().resolve()

    return {
        # ``Label`` is the unique name launchd uses to identify this service.
        "Label": SERVICE_NAME,

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


def _systemd_quote(value: str) -> str:
    """Quote one systemd directive value without invoking a shell."""

    if "\n" in value or "\0" in value:
        raise ValueError("systemd values must not contain newlines or NUL bytes")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{value}"'


def build_systemd_units(
    workspace: Path, python_executable: Path, path_env: str
) -> dict[str, str]:
    """Build a systemd user service and timer using caller-supplied paths."""

    workspace = workspace.expanduser().resolve()
    python_executable = python_executable.expanduser().resolve()
    bridge = workspace / "scripts" / "openclaw_cron_sentry_bridge.py"

    service = f"""[Unit]
Description=Captain Sentry bridge

[Service]
Type=oneshot
WorkingDirectory={_systemd_quote(str(workspace))}
UMask=0077
Environment={_systemd_quote(f"PATH={path_env}")}
ExecStart={_systemd_quote(str(python_executable))} {_systemd_quote(str(bridge))}
StandardOutput=journal
StandardError=journal
"""
    timer = f"""[Unit]
Description=Run the Captain Sentry bridge every 10 minutes

[Timer]
OnBootSec=0
OnUnitInactiveSec=10min
Unit={SERVICE_NAME}.service

[Install]
WantedBy=timers.target
"""
    return {
        f"{SERVICE_NAME}.service": service,
        f"{SERVICE_NAME}.timer": timer,
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


def write_systemd_units(output_directory: Path, units: dict[str, str]) -> None:
    """Write owner-private systemd user units atomically."""

    output_directory = output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    for name, content in units.items():
        output = output_directory / name
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(output)


def detect_platform() -> str:
    """Return the supported service manager for the current operating system."""

    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise OSError(f"unsupported platform: {sys.platform}")


def parse_args() -> argparse.Namespace:
    """Describe and parse the command-line options accepted by this script."""

    parser = argparse.ArgumentParser(
        description="Render Captain Sentry bridge service files for macOS or Linux."
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
        help="Plist path on macOS; systemd user-unit directory on Linux",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Python executable for the bridge (default: this Python)",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="PATH passed to the service manager",
    )
    parser.add_argument(
        "--platform",
        choices=("auto", "macos", "linux"),
        default="auto",
        help="Target platform (default: detect this host)",
    )

    return parser.parse_args()


def main() -> None:
    """Render the requested configuration and save it to disk."""

    # Keep command-line parsing, configuration building, and file writing as
    # separate steps so each piece is easy to understand and test independently.
    args = parse_args()
    platform = detect_platform() if args.platform == "auto" else args.platform
    path_env = args.path or (
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        if platform == "macos"
        else "/usr/local/bin:/usr/bin:/bin"
    )
    if platform == "macos":
        write_plist(
            args.output,
            build_launchd_config(args.workspace, args.python, path_env),
        )
    else:
        write_systemd_units(
            args.output,
            build_systemd_units(args.workspace, args.python, path_env),
        )


if __name__ == "__main__":
    main()
