"""Slack user id -> display name resolution shared by scripts/
daily_activity_digest.py and scripts/refresh_slack_user_cache.py.

**NEVER FABRICATE A NAME.** A plausible-but-wrong name attached to a message
about someone's work is worse than a bare id: it would let a reader approve
or act on a message aimed at the wrong person. Every step below either
returns a name it actually knows, or falls through to the next one; the
final fallback is always the bare id, unchanged from today's behavior -- see
docs/daily-loop.md's Slack name-rendering convention for the full spec.

This module has no `message`/OpenClaw tool layer available to it (that
capability is LLM-agent-only -- see cron-prompts/*.md's
`message(action=member-info)` usage). It is a command cron's library, so it
can only ever resolve names that are already known offline:

Precedence (each step falls through to the next on failure):
  1. `admin_recipients` in data/captain-channels.json (name -> id; inverted
     here) -- free, offline, zero-cost, and already the source of truth for
     Gavin/Arnold/Raj.
  2. data/slack-user-cache.json (id -> display/real name), populated
     out-of-band by scripts/refresh_slack_user_cache.py. A missing or
     malformed cache file is a normal no-op here, not an error -- it just
     means step 3 applies.
  3. The bare id.

Never resolves or renders an email address: this module's only output is a
display/real *name* (or the bare id) -- see refresh_slack_user_cache.py's
module docstring for why an email must never end up in this cache in the
first place.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Slack user ids in this workspace are `U` + 9-11 uppercase-alnum chars --
# every admin_recipients/eng_leads/excluded_user_ids id in
# data/captain-channels.json matches this shape (e.g. `U0B4G00QXT8`,
# `U0988KDFL4A`). Deliberately over-inclusive rather than pinned to exactly
# 10 chars, in case a future workspace id differs slightly in length; a
# token that merely looks like an id but isn't a real Slack user just fails
# to resolve to a name and renders as itself, unchanged.
# Requires at least one digit. A letters-only match harvests ordinary
# uppercase words out of prose -- the first live cache refresh tried to
# resolve "UNIVERSITY" as a Slack id and got user_not_found. Real Slack
# user ids are mixed alphanumeric, so requiring a digit removes that
# whole class of false positive.
USER_ID_RE = re.compile(r"\bU(?=[A-Z0-9]*\d)[A-Z0-9]{9,11}\b")


def invert_admin_recipients(channels_cfg):
    """`admin_recipients` (name -> id) inverted to id -> name. A missing or
    malformed `admin_recipients` degrades to an empty map, never raises."""
    admins = channels_cfg.get("admin_recipients") if isinstance(channels_cfg, dict) else None
    if not isinstance(admins, dict):
        return {}
    inverted = {}
    for name, uid in admins.items():
        if isinstance(name, str) and name and isinstance(uid, str) and uid:
            inverted[uid] = name
    return inverted


def load_user_cache(cache_path):
    """Read the id -> display-name cache. A missing file, unreadable JSON,
    non-dict JSON, or any individual entry that isn't a plain string -> string
    pair is skipped rather than raised -- "a missing or malformed cache is a
    normal no-op, not an error" is the explicit spec here, since a stale or
    broken cache must degrade safely to bare ids, never break the caller."""
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and k and isinstance(v, str) and v.strip()
    }


class SlackNameResolver:
    """Resolves a bare Slack user id to `Name (Uxxxxxxxx)`. Falls back to
    the bare id, unchanged, when no name is known from any source -- NEVER
    invents one. See module docstring for the precedence order."""

    def __init__(self, channels_cfg=None, cache_path=None, root=None):
        root = root or ROOT
        self._admin_by_id = invert_admin_recipients(channels_cfg or {})
        self._cache = load_user_cache(cache_path or (root / "data" / "slack-user-cache.json"))

    def name_for(self, user_id):
        """Return a known name for `user_id`, or None if unresolvable from
        any source (admin_recipients, then cache) -- never a guess."""
        if not isinstance(user_id, str) or not user_id:
            return None
        return self._admin_by_id.get(user_id) or self._cache.get(user_id)

    def render(self, user_id):
        """Return `Name (Uxxxxxxxx)` when resolvable, else the bare id
        unchanged -- the safe, no-fabrication fallback that matches today's
        behavior for anyone this resolver cannot name."""
        name = self.name_for(user_id)
        if name:
            return "%s (%s)" % (name, user_id)
        return user_id

    def scrub_text(self, text):
        """Replace every Slack-user-id-shaped token in free text with its
        resolved `Name (Uxxxxxxxx)` form. An id with no known name passes
        through completely unchanged -- this is a pure enhancement over
        today's bare-id rendering, never a behavior regression, and it never
        touches anything that isn't already shaped exactly like a Slack user
        id (so ordinary prose is untouched)."""
        if not text:
            return text
        return USER_ID_RE.sub(lambda m: self.render(m.group(0)), text)
