"""Load ClickUp credentials without making callers parse settings files.

Captain accepts recognized ClickUp settings from either the process
environment or ``.secrets/clickup.env``. Environment variables take
precedence, unrelated settings are ignored, and callers receive a clear error
when any explicitly required value is unavailable.
"""

# Shared imports
import os
from pathlib import Path

# Shared paths and recognized keys
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = ROOT / ".secrets" / "clickup.env" 
KNOWN_KEYS = frozenset(("CLICKUP_API_KEY", "CLICKUP_TEAM_ID")) # Read-only key names


# Configuration errors

class MissingClickUpCredentials(RuntimeError):
    """Explain that Captain cannot connect without a required setting."""

    pass


# Settings-file parsing

def _read_known_values(path):
    """Read recognized ClickUp settings from a local settings file.

    Example input: ``export CLICKUP_TEAM_ID="12345"``
    Example output: ``{"CLICKUP_TEAM_ID": "12345"}``
    """
    # A missing or unreadable optional settings file contributes no values.
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values = {}

    # Parse each non-empty, non-comment line as a possible KEY=VALUE pair.
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Accept shell-style lines such as ``export CLICKUP_API_KEY=...``.
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        # Ignore malformed lines and settings this module does not recognize.
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in KNOWN_KEYS:
            continue

        # Normalize whitespace and one matching pair of surrounding quotes.
        value = raw_value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]

        if value:
            values[key] = value

    return values


# Public credential loader

def load_clickup_credentials(required_keys, environ=None, env_path=None):
    """Resolve required ClickUp settings from the environment or a file.

    Process environment values take precedence over values in ``env_path``.
    Missing keys raise ``MissingClickUpCredentials`` rather than returning a
    partial configuration.

    Example input: ``required_keys=("CLICKUP_TEAM_ID",)``
    Example output: ``{"CLICKUP_TEAM_ID": "12345"}``
    """
    # Use the real process environment unless a caller supplies a test mapping.
    environ = os.environ if environ is None else environ
    required_keys = tuple(required_keys)

    # Read the optional file only when the environment cannot satisfy every key.
    file_values = {}
    if any(not environ.get(key) for key in required_keys):
        file_values = _read_known_values(env_path or DEFAULT_ENV_PATH)

    # Resolve each setting with the environment as the authoritative source.
    resolved = {
        key: environ.get(key) or file_values.get(key) for key in required_keys
    }

    # Refuse to return an incomplete credential set.
    missing = [key for key, value in resolved.items() if not value]
    if missing:
        raise MissingClickUpCredentials("Missing " + " or ".join(missing))

    return resolved
