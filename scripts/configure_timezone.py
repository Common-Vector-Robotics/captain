#!/usr/bin/env python3
"""Configure the explicit IANA timezone on Captain's six Claw cron jobs.

OpenClaw beta.5 requires each cron job in ``CLAW.md`` to contain a literal
timezone. This script gives an operator one safe command for changing all six
Captain jobs together instead of editing the YAML by hand.

The script deliberately validates the whole cron section before writing. It
then publishes the new manifest with an atomic same-directory replacement, so
an invalid manifest or interrupted write cannot leave a partially edited file.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Keep this list in the same order as the jobs in CLAW.md. Requiring the exact
# set prevents a future job from silently retaining an old timezone.
EXPECTED_JOB_IDS = (
    "morning-cycle",
    "blocker-chase",
    "meeting-transcript-reconciliation",
    "bench-truth-watch",
    "eod-wrap",
    "action-summary-reporting",
)

# Resolve from this script rather than the caller's working directory. This
# lets users run the command from anywhere inside or outside the repository.
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "CLAW.md"


class TimezoneConfigurationError(ValueError):
    """Raised when the manifest or requested timezone is unsafe or invalid."""


@dataclass(frozen=True)
class ConfigurationResult:
    """Describe what the configurator verified or changed.

    Attributes:
        timezone: The validated IANA timezone requested by the operator.
        job_ids: The six cron job IDs found in the manifest, in file order.
        changed: ``True`` when new manifest bytes were written.
    """

    timezone: str
    job_ids: tuple[str, ...]
    changed: bool


def _validate_timezone(timezone: str) -> str:
    """Return ``timezone`` after validating it with Python's IANA database.

    ``zoneinfo`` rejects misspellings such as ``America/Detriot`` while still
    accepting standard values such as ``UTC`` and ``Europe/London``.

    Raises:
        TimezoneConfigurationError: If the value is empty, padded with
            whitespace, or absent from the installed IANA timezone database.
    """

    if not timezone or timezone != timezone.strip():
        raise TimezoneConfigurationError(
            "timezone must be a non-empty valid IANA timezone without surrounding whitespace"
        )

    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise TimezoneConfigurationError(
            f"{timezone!r} is not a valid IANA timezone"
        ) from exc
    return timezone


def _read_manifest(path: Path) -> tuple[bytes, os.stat_result]:
    """Read a regular manifest without following a symbolic link.

    The returned ``stat`` data is later used to preserve the file's permission
    bits when the replacement file is created.

    Raises:
        TimezoneConfigurationError: If the path is missing, a symlink, or not a
            regular file.
    """

    # lstat() examines the path itself. A normal stat() call would follow a
    # symlink and could cause the script to replace an unintended target.
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise TimezoneConfigurationError(f"manifest does not exist: {path}") from exc

    if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        raise TimezoneConfigurationError(
            f"manifest must be a regular non-symlink file: {path}"
        )

    return path.read_bytes(), file_stat


def _configured_manifest(
    text: str,
    timezone: str,
    *,
    check: bool,
) -> tuple[str, tuple[str, ...]]:
    """Build updated manifest text, or verify existing timezone values.

    Only the YAML ``cronJobs`` section is inspected. The Markdown body after
    the closing ``---`` delimiter is deliberately ignored so examples in the
    documentation cannot be mistaken for real scheduled jobs.

    Returns:
        A pair containing the configured text and the discovered job IDs.

    Raises:
        TimezoneConfigurationError: If the cron section is missing, has the
            wrong jobs, has malformed schedules, or fails ``--check``.
    """

    # CLAW.md starts with YAML front matter and then switches to Markdown. Find
    # the cron section and its closing front-matter delimiter before parsing.
    cron_start = text.find("\ncronJobs:\n")
    cron_end = text.find("\n---\n", cron_start + 1)
    if cron_start < 0 or cron_end < 0:
        raise TimezoneConfigurationError("CLAW.md must contain one bounded cronJobs section")

    section_start = cron_start + 1
    section = text[section_start:cron_end]

    # Locate each job block by its top-level list item. We validate the exact
    # IDs before calculating any replacement positions.
    job_matches = list(re.finditer(r"(?m)^  - id: ([a-z0-9-]+)\n", section))
    job_ids = tuple(match.group(1) for match in job_matches)
    if len(job_ids) != 6 or set(job_ids) != set(EXPECTED_JOB_IDS):
        raise TimezoneConfigurationError(
            "CLAW.md must declare exactly six Captain cron jobs: "
            + ", ".join(EXPECTED_JOB_IDS)
        )

    replacements: list[tuple[int, int, str]] = []
    for index, job_match in enumerate(job_matches):
        block_start = job_match.start()
        block_end = (
            job_matches[index + 1].start()
            if index + 1 < len(job_matches)
            else len(section)
        )
        block = section[block_start:block_end]

        # The indentation and ordering here match OpenClaw's strict Claw schema:
        # schedule, cron expression, then one explicit timezone.
        timezone_matches = list(
            re.finditer(
                r'(?m)^    schedule:\n      cron: "[^"]+"\n'
                r"      timezone: ([^\n]+)$",
                block,
            )
        )
        if len(timezone_matches) != 1:
            raise TimezoneConfigurationError(
                f"cron job {job_match.group(1)!r} must have exactly one cron schedule and timezone"
            )

        timezone_match = timezone_matches[0]
        current = timezone_match.group(1)
        if check and current != timezone:
            raise TimezoneConfigurationError(
                f"cron job {job_match.group(1)!r} timezone {current!r} "
                f"does not match requested timezone {timezone}"
            )

        # Store absolute character offsets into the complete file. No text is
        # changed until every job has passed validation.
        value_start = section_start + block_start + timezone_match.start(1)
        value_end = section_start + block_start + timezone_match.end(1)
        replacements.append((value_start, value_end, timezone))

    if check:
        return text, job_ids

    # Apply edits from the end of the file toward the beginning. Earlier
    # offsets therefore remain valid even when timezone lengths differ.
    configured = text
    for start, end, value in reversed(replacements):
        configured = configured[:start] + value + configured[end:]

    return configured, job_ids


def _atomic_write(
    path: Path,
    content: bytes,
    file_stat: os.stat_result,
    *,
    replace_fn: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None],
) -> None:
    """Publish ``content`` without exposing a partially written manifest.

    The temporary file lives beside ``CLAW.md`` so ``os.replace`` stays on the
    same filesystem and is atomic. The original file remains untouched until
    the temporary file is fully written and flushed.

    Args:
        path: Final manifest path.
        content: Complete UTF-8 manifest bytes to publish.
        file_stat: Metadata from the original manifest.
        replace_fn: Replacement function, injectable for failure-path tests.
    """

    # A randomized name avoids collisions between simultaneous setup attempts.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)

    try:
        # Preserve the original mode rather than inheriting the caller's umask.
        os.fchmod(descriptor, stat.S_IMODE(file_stat.st_mode))
        with os.fdopen(descriptor, "wb") as handle:
            # fdopen() owns the descriptor from this point onward.
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        # This is the only operation that changes the visible CLAW.md path.
        replace_fn(temporary, path)
    except BaseException:
        # Cleanup also runs for Ctrl-C and SystemExit, not just ordinary errors.
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def configure_timezone(
    manifest: Path | str,
    timezone: str,
    *,
    check: bool = False,
    replace_fn: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
) -> ConfigurationResult:
    """Configure or verify every Captain cron job's explicit IANA timezone.

    Args:
        manifest: Path to the ``CLAW.md`` file to inspect.
        timezone: Explicit IANA timezone requested by the operator.
        check: When true, verify existing values without writing.
        replace_fn: Atomic replacement function, injectable for tests.

    Returns:
        A summary of the validated timezone, job IDs, and whether bytes changed.

    Raises:
        TimezoneConfigurationError: If the timezone or manifest is invalid.
        OSError: If writing or atomically publishing the replacement fails.
    """

    path = Path(manifest)
    validated_timezone = _validate_timezone(timezone)
    original_bytes, file_stat = _read_manifest(path)

    try:
        original_text = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TimezoneConfigurationError("CLAW.md must be valid UTF-8") from exc

    configured_text, job_ids = _configured_manifest(
        original_text,
        validated_timezone,
        check=check,
    )
    configured_bytes = configured_text.encode("utf-8")
    changed = configured_bytes != original_bytes

    # --check returns the original text, so this write branch is unreachable in
    # verification mode. Re-running configuration with the same timezone is
    # also a no-op and does not churn the file's metadata.
    if changed:
        _atomic_write(
            path,
            configured_bytes,
            file_stat,
            replace_fn=replace_fn,
        )

    return ConfigurationResult(validated_timezone, job_ids, changed)


def _parser() -> argparse.ArgumentParser:
    """Create the command-line parser separately for easy testing and reuse."""

    parser = argparse.ArgumentParser(
        description="Set the explicit IANA timezone on Captain's six Claw cron jobs.",
    )
    parser.add_argument(
        "--timezone",
        required=True,
        help="IANA timezone, for example Europe/London",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="CLAW.md path (defaults to the package manifest next to this script)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify all six declarations without changing the manifest",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and translate validation failures into exit status 2."""

    args = _parser().parse_args(argv)
    try:
        result = configure_timezone(
            args.manifest,
            args.timezone,
            check=args.check,
        )
    except TimezoneConfigurationError as exc:
        print(f"Timezone configuration failed: {exc}", file=sys.stderr)
        return 2

    # Include every job ID so an operator can confirm the complete scope without
    # opening CLAW.md after a successful command.
    verb = "Verified" if args.check else "Configured"
    print(
        f"{verb} {len(result.job_ids)} Captain cron jobs in {args.manifest} "
        f"for {result.timezone}: {', '.join(result.job_ids)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
