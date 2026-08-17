"""Install the Captain skill and MCP server into OpenCode."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

MAX_CONFIG_BYTES = 1_000_000
OPEN_CODE_TIMEOUT_SECONDS = 15


class InstallError(RuntimeError):
    """Report an actionable OpenCode installation failure."""


class InstallConflict(InstallError):
    """Protect a user-owned skill or MCP definition from replacement."""


def config_root(environment: Mapping[str, str]) -> Path:
    """Return OpenCode's user configuration directory."""

    xdg_root = environment.get("XDG_CONFIG_HOME")
    if xdg_root:
        return Path(xdg_root).expanduser() / "opencode"
    return Path.home() / ".config" / "opencode"


def expected_server(launcher: Path) -> dict[str, Any]:
    """Return the effective MCP definition owned by this installer."""

    return {"type": "local", "command": [str(launcher)]}


def _run_opencode(
    command: str,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    *,
    failure_message: str,
    timeout_message: str,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded OpenCode command without a shell."""

    try:
        completed = subprocess.run(
            [command, *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=dict(environment),
            timeout=OPEN_CODE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise InstallError(f"OpenCode executable not found: {command}") from error
    except OSError as error:
        raise InstallError(f"Could not start OpenCode: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise InstallError(timeout_message) from error

    if completed.returncode != 0:
        detail = completed.stderr.strip()[:500]
        suffix = f" {detail}" if detail else ""
        raise InstallError(f"{failure_message}.{suffix}")
    return completed


def load_resolved_config(
    command: str,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Read OpenCode's merged configuration through its public CLI."""

    completed = _run_opencode(
        command,
        ["debug", "config", "--pure"],
        environment,
        failure_message="OpenCode could not read its configuration",
        timeout_message="OpenCode configuration inspection timed out.",
    )
    if len(completed.stdout.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise InstallError("OpenCode returned more than 1 MiB of configuration.")

    try:
        config = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise InstallError("OpenCode returned invalid configuration JSON.") from error
    if not isinstance(config, dict):
        raise InstallError("OpenCode returned a non-object configuration.")
    return config


def _configured_server(config: Mapping[str, Any]) -> Any:
    """Return the effective `captain` MCP definition, when present."""

    mcp = config.get("mcp", {})
    if not isinstance(mcp, Mapping):
        raise InstallError("OpenCode's resolved `mcp` configuration is not an object.")
    return mcp.get("captain")


def _server_matches(server: Any, launcher: Path) -> bool:
    """Accept the definition OpenCode writes, with an optional enabled flag."""

    return (
        isinstance(server, Mapping)
        and server.get("type") == "local"
        and server.get("command") == [str(launcher)]
        and server.get("enabled", True) is not False
    )


def _install_skill(source: Path, destination: Path) -> None:
    """Atomically copy the shared skill into OpenCode's global catalog."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        # A same-directory temporary file keeps the final replacement atomic.
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=".captain-skill-",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary_name).chmod(0o644)

        # Recheck after writing so a concurrent user-created skill is preserved.
        if destination.exists():
            raise InstallConflict(f"Captain skill already exists at {destination}.")
        Path(temporary_name).replace(destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _add_mcp_server(
    command: str,
    launcher: Path,
    environment: Mapping[str, str],
) -> None:
    """Ask OpenCode to own the global MCP configuration write."""

    _run_opencode(
        command,
        ["mcp", "add", "captain", "--", str(launcher)],
        environment,
        failure_message="OpenCode could not add the Captain MCP server",
        timeout_message="OpenCode MCP installation timed out.",
    )


def install_opencode(
    command: str,
    *,
    dry_run: bool,
    environment: Mapping[str, str],
    plugin_root: Path,
) -> list[str]:
    """Install Captain after preflighting every user-owned destination."""

    plugin_root = plugin_root.resolve()
    source_skill = plugin_root / "skills" / "captain" / "SKILL.md"
    launcher = plugin_root / "bin" / "captain-agent-mcp"
    destination = config_root(environment) / "skills" / "captain" / "SKILL.md"

    if not source_skill.is_file():
        raise InstallError(f"Bundled Captain skill is missing: {source_skill}")
    if not launcher.is_file():
        raise InstallError(f"Bundled Captain MCP launcher is missing: {launcher}")

    skill_exists = destination.exists()
    if skill_exists and destination.read_bytes() != source_skill.read_bytes():
        raise InstallConflict(f"Captain skill already exists at {destination}.")

    config = load_resolved_config(command, environment)
    configured_server = _configured_server(config)
    server_exists = configured_server is not None
    if server_exists and not _server_matches(configured_server, launcher):
        raise InstallConflict(
            "OpenCode MCP server already exists with a different Captain definition."
        )

    if dry_run:
        messages = []
        if skill_exists:
            messages.append("OpenCode /captain is already installed.")
        else:
            messages.append(f"Would install OpenCode skill at {destination}.")
        if server_exists:
            messages.append("OpenCode MCP server captain is already configured.")
        else:
            messages.append(
                f"Would add OpenCode MCP server captain using {launcher}."
            )
        return messages

    installed_skill = False
    if not skill_exists:
        _install_skill(source_skill, destination)
        installed_skill = True

    try:
        if not server_exists:
            _add_mcp_server(command, launcher, environment)
    except InstallError:
        # Roll back only the skill this invocation created.
        if (
            installed_skill
            and destination.is_file()
            and destination.read_bytes() == source_skill.read_bytes()
        ):
            destination.unlink()
        raise

    return [
        (
            "Installed OpenCode /captain."
            if installed_skill
            else "OpenCode /captain is already installed."
        ),
        (
            "Added OpenCode MCP server captain."
            if not server_exists
            else "OpenCode MCP server captain is already configured."
        ),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Parse installer options and print a concise result."""

    parser = argparse.ArgumentParser(
        description="Install Captain for the current OpenCode user."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the intended changes without writing them",
    )
    parser.add_argument(
        "--opencode-command",
        default="opencode",
        help="OpenCode executable name or path (default: opencode)",
    )
    arguments = parser.parse_args(argv)
    plugin_root = Path(__file__).resolve().parents[1]

    try:
        messages = install_opencode(
            arguments.opencode_command,
            dry_run=arguments.dry_run,
            environment=os.environ,
            plugin_root=plugin_root,
        )
    except InstallError as error:
        print(f"Captain OpenCode installation failed: {error}", file=sys.stderr)
        return 2

    for message in messages:
        print(message)
    return 0
