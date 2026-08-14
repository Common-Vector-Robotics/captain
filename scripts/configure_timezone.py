#!/usr/bin/env python3
"""Configure the explicit IANA timezone on Captain's six Claw cron jobs."""

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


EXPECTED_JOB_IDS = (
    "morning-cycle",
    "blocker-chase",
    "meeting-transcript-reconciliation",
    "bench-truth-watch",
    "eod-wrap",
    "action-summary-reporting",
)
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "CLAW.md"


class TimezoneConfigurationError(ValueError):
    """Raised when the manifest or requested timezone is unsafe or invalid."""


@dataclass(frozen=True)
class ConfigurationResult:
    timezone: str
    job_ids: tuple[str, ...]
    changed: bool


def _validate_timezone(timezone: str) -> str:
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
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise TimezoneConfigurationError(f"manifest does not exist: {path}") from exc
    if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        raise TimezoneConfigurationError(
            f"manifest must be a regular non-symlink file: {path}"
        )
    return path.read_bytes(), file_stat


def _configured_manifest(text: str, timezone: str, *, check: bool) -> tuple[str, tuple[str, ...]]:
    cron_start = text.find("\ncronJobs:\n")
    cron_end = text.find("\n---\n", cron_start + 1)
    if cron_start < 0 or cron_end < 0:
        raise TimezoneConfigurationError("CLAW.md must contain one bounded cronJobs section")

    section_start = cron_start + 1
    section = text[section_start:cron_end]
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
        block_end = job_matches[index + 1].start() if index + 1 < len(job_matches) else len(section)
        block = section[block_start:block_end]
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
        value_start = section_start + block_start + timezone_match.start(1)
        value_end = section_start + block_start + timezone_match.end(1)
        replacements.append((value_start, value_end, timezone))

    if check:
        return text, job_ids

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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IMODE(file_stat.st_mode))
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace_fn(temporary, path)
    except BaseException:
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
    """Configure or verify every Captain cron job's explicit IANA timezone."""

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
    if changed:
        _atomic_write(
            path,
            configured_bytes,
            file_stat,
            replace_fn=replace_fn,
        )
    return ConfigurationResult(validated_timezone, job_ids, changed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Set the explicit IANA timezone on Captain's six Claw cron jobs.",
    )
    parser.add_argument("--timezone", required=True, help="IANA timezone, for example Europe/London")
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

    verb = "Verified" if args.check else "Configured"
    print(
        f"{verb} {len(result.job_ids)} Captain cron jobs in {args.manifest} "
        f"for {result.timezone}: {', '.join(result.job_ids)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
