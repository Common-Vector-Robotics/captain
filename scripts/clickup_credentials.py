import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = ROOT / ".secrets" / "clickup.env"
KNOWN_KEYS = frozenset(("CLICKUP_API_KEY", "CLICKUP_TEAM_ID"))


class MissingClickUpCredentials(RuntimeError):
    """Explain that Captain cannot connect because required ClickUp details are missing."""

    pass


def _read_known_values(path):
    """Read only the recognized ClickUp settings from a local settings file."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in KNOWN_KEYS:
            continue
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


def load_clickup_credentials(required_keys, environ=None, env_path=None):
    """Find the required ClickUp settings in the environment or local settings file."""
    environ = os.environ if environ is None else environ
    required_keys = tuple(required_keys)
    file_values = {}
    if any(not environ.get(key) for key in required_keys):
        file_values = _read_known_values(env_path or DEFAULT_ENV_PATH)
    resolved = {
        key: environ.get(key) or file_values.get(key) for key in required_keys
    }
    missing = [key for key, value in resolved.items() if not value]
    if missing:
        raise MissingClickUpCredentials("Missing " + " or ".join(missing))
    return resolved
