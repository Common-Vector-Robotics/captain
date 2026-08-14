"""
A tool for Captain to automatically resolve Slack user IDs into human-readable names.
"""

# Requirements
from __future__ import annotations

import json
import re
from pathlib import Path

# Captain repo root
ROOT = Path(__file__).resolve().parents[1]

# Regex to match Slack user IDs, which start with 'U' and are followed by 9 to 11 alphanumeric characters.
USER_ID_RE = re.compile(r"\bU(?=[A-Z0-9]*\d)[A-Z0-9]{9,11}\b")


# ------------ Helper Functions ------------

def invert_admin_recipients(channels_cfg):
    """Invert ``admin_recipients`` from name-to-ID into ID-to-name form.

    Missing or malformed configuration safely produces an empty mapping.

    Example input: {"admin_recipients": {"Name": "U0123456789"}}
    Example output: {"U0123456789": "Name"}
    """

    # Get admin recipients from channels configuration
    admins = (channels_cfg.get("admin_recipients") if isinstance(channels_cfg, dict) else None ) 

    # Fails soft if the configuration is missing or malformed.
    if not isinstance(admins, dict):
        return {}

    # Keep only non-empty string pairs; malformed entries cannot be trusted.
    inverted = {}

    # Iterate over the admin recipients and invert the mapping from name-to-ID to ID-to-name.
    for name, user_id in admins.items():
        if (
            isinstance(name, str) # Is name string?
            and name # Is name non-empty?
            and isinstance(user_id, str) # Is user_id string?
            and user_id # Is user_id non-empty?
        ):
            inverted[user_id] = name # Add to inverted

    return inverted


def load_user_cache(cache_path):
    """Read a validated Slack-ID-to-display-name cache.

    Missing files, unreadable JSON, non-dictionary data, and malformed entries
    are ignored. A stale cache must degrade to bare IDs, never break a caller.
    """

    # The cache is optional supporting data, so read and parse failures are safe.

    # Try loading cache.
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError): # Return empty mapping if missing
        return {}

    # Return empty mapping if the cache is malformed
    if not isinstance(data, dict):
        return {}

    # Return valid entries
    return {
        key: value
        for key, value in data.items()
        if isinstance(key, str)
        and key
        and isinstance(value, str)
        and value.strip()
    }


# ----------- SlackNameResolver Class ------------ 

class SlackNameResolver:
    """Render Slack IDs with known names while preserving unknown IDs."""

    def __init__(self, channels_cfg=None, cache_path=None, root=None):
        """Load known names from Captain's channel settings and local name cache."""

        # Default root path is the project root, but it can be overridden for testing.
        root = root or ROOT

        # Invert the admin recipients from the channels configuration to create a mapping of Slack user IDs to display names.
        self._admin_by_id = invert_admin_recipients(channels_cfg or {})

        # Load the Slack user cache from the specified cache path or the default location in the project root.
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
