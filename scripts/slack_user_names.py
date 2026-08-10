"""Resolve Slack user IDs to trustworthy offline display names.

Never fabricate a name. A plausible but incorrect name could direct action at
the wrong person, so an unresolved user always remains a bare Slack ID.

Resolution order:

1. Invert ``admin_recipients`` from Captain's channel configuration.
2. Check ``data/slack-user-cache.json`` when available and valid.
3. Return the original user ID unchanged.

This command-side helper cannot call OpenClaw's agent-only ``member-info``
tool. It also never resolves or renders email addresses; the resolution order
above is the complete policy enforced by this module.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Match workspace-style Slack IDs: ``U`` plus 9-11 uppercase letters or digits.
# At least one digit is required so ordinary uppercase words are not mistaken
# for users. Slightly varied lengths remain allowed for future Slack formats;
# an unknown match safely renders as itself.
USER_ID_RE = re.compile(r"\bU(?=[A-Z0-9]*\d)[A-Z0-9]{9,11}\b")


def invert_admin_recipients(channels_cfg):
    """Invert ``admin_recipients`` from name-to-ID into ID-to-name form.

    Missing or malformed configuration safely produces an empty mapping.

    Example input: {"admin_recipients": {"Alex": "U0123456789"}}
    Example output: {"U0123456789": "Alex"}
    """
    # Only dictionary configuration can contain the expected mapping.
    admins = (
        channels_cfg.get("admin_recipients")
        if isinstance(channels_cfg, dict)
        else None
    )
    if not isinstance(admins, dict):
        return {}

    # Keep only non-empty string pairs; malformed entries cannot be trusted.
    inverted = {}
    for name, user_id in admins.items():
        if (
            isinstance(name, str)
            and name
            and isinstance(user_id, str)
            and user_id
        ):
            inverted[user_id] = name

    return inverted


def load_user_cache(cache_path):
    """Read a validated Slack-ID-to-display-name cache.

    Missing files, unreadable JSON, non-dictionary data, and malformed entries
    are ignored. A stale cache must degrade to bare IDs, never break a caller.
    """
    # The cache is optional supporting data, so read and parse failures are safe.
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}

    # Reject anything except a non-empty string ID and non-blank string name.
    return {
        key: value
        for key, value in data.items()
        if isinstance(key, str)
        and key
        and isinstance(value, str)
        and value.strip()
    }


class SlackNameResolver:
    """Render Slack IDs with known names while preserving unknown IDs."""

    def __init__(self, channels_cfg=None, cache_path=None, root=None):
        """Load known names from Captain's channel settings and local name cache."""
        # ``root`` and ``cache_path`` are seams for isolated callers and tests.
        root = root or ROOT
        self._admin_by_id = invert_admin_recipients(channels_cfg or {})
        self._cache = load_user_cache(
            cache_path or (root / "data" / "slack-user-cache.json")
        )

    def name_for(self, user_id):
        """Return a known name for a Slack ID, or ``None`` without guessing."""
        if not isinstance(user_id, str) or not user_id:
            return None

        # Administrator configuration has precedence over the generated cache.
        return self._admin_by_id.get(user_id) or self._cache.get(user_id)

    def render(self, user_id):
        """Return ``Name (Uxxxxxxxx)`` when known, otherwise the bare ID."""
        name = self.name_for(user_id)
        if name:
            return "%s (%s)" % (name, user_id)

        return user_id

    def scrub_text(self, text):
        """Render each Slack-ID-shaped token found in free text.

        Unknown IDs and text that does not match ``USER_ID_RE`` remain exactly
        unchanged.
        """
        if not text:
            return text

        return USER_ID_RE.sub(
            lambda match: self.render(match.group(0)),
            text,
        )
