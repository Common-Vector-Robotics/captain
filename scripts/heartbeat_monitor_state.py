#!/usr/bin/env python3
"""Atomically record the result of one enabled Captain heartbeat sweep."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = ROOT / "data" / "heartbeat-monitor-state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_private_parent(parent: Path) -> None:
    if os.path.lexists(parent):
        info = os.lstat(parent)
        if stat.S_ISLNK(info.st_mode):
            raise OSError(f"Heartbeat-state parent must not be a symlink: {parent}")
    else:
        parent.mkdir(parents=True, mode=0o700)
        info = os.lstat(parent)

    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or mode & 0o022
    ):
        raise OSError(
            f"Heartbeat-state parent is not an owner-controlled directory: {parent}"
        )
    if mode != 0o700:
        os.chmod(parent, 0o700)


def _load_existing_state(state_path: Path) -> dict:
    if not os.path.lexists(state_path):
        return {}

    info = os.lstat(state_path)
    if stat.S_ISLNK(info.st_mode):
        raise OSError(f"Heartbeat state must not be a symlink: {state_path}")
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise OSError(f"Heartbeat state is not an owner-controlled file: {state_path}")

    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read existing heartbeat monitor state: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("Cannot read existing heartbeat monitor state: expected JSON object")
    return value


def _atomic_write_json(state: dict, state_path: Path) -> None:
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.",
        suffix=".tmp",
        dir=state_path.parent,
    )
    staged = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        state_file = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with state_file:
            state_file.write(payload)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(staged, state_path)
    finally:
        if fd >= 0:
            os.close(fd)
        staged.unlink(missing_ok=True)


def record_sweep(
    *,
    enumeration_unavailable: bool,
    channels_scanned: int,
    state_path: Path = DEFAULT_STATE_PATH,
    now_fn=_now_iso,
) -> dict:
    """Preserve prior fields and atomically record one current sweep."""

    if not isinstance(enumeration_unavailable, bool):
        raise ValueError("enumeration_unavailable must be a boolean")
    if (
        isinstance(channels_scanned, bool)
        or not isinstance(channels_scanned, int)
        or channels_scanned < 0
    ):
        raise ValueError("channels_scanned must be a non-negative integer")

    state_path = Path(state_path)
    _ensure_private_parent(state_path.parent)
    state = _load_existing_state(state_path)
    last_run_at = now_fn()
    if not isinstance(last_run_at, str) or not last_run_at:
        raise ValueError("now_fn must return a non-empty timestamp string")
    state.update(
        {
            "last_run_at": last_run_at,
            "channel_enumeration_unavailable": enumeration_unavailable,
            "channels_scanned": channels_scanned,
        }
    )
    _atomic_write_json(state, state_path)
    return state


def main(argv=None, *, now_fn=_now_iso) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument(
        "--channel-enumeration-unavailable",
        required=True,
        choices=("true", "false"),
    )
    parser.add_argument("--channels-scanned", required=True, type=int)
    args = parser.parse_args(argv)

    state = record_sweep(
        enumeration_unavailable=args.channel_enumeration_unavailable == "true",
        channels_scanned=args.channels_scanned,
        state_path=args.state_path,
        now_fn=now_fn,
    )
    current = {
        "last_run_at": state["last_run_at"],
        "channel_enumeration_unavailable": state["channel_enumeration_unavailable"],
        "channels_scanned": state["channels_scanned"],
    }
    print(json.dumps(current, sort_keys=True))


if __name__ == "__main__":
    main()
