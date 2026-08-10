#!/usr/bin/env python3
"""Safe, optional Sentry telemetry for Captain scripts.

This module is the only place in the Captain workspace that talks directly to
Sentry. Callers can initialize telemetry, capture errors or check-ins, and use
``guard`` to report unexpected failures without changing normal script
behavior. Missing credentials, a missing SDK, or a telemetry failure always
leave the calling script free to continue.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import ContextDecorator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = ROOT / ".secrets" / "sentry.env"
KNOWN_KEYS = frozenset(("SENTRY_DSN", "SENTRY_ENVIRONMENT"))
DEFAULT_ENVIRONMENT = "captain-host"
_SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "DSN")
_MIN_SECRET_LENGTH = 8

# Files that may hold secret values which never pass through ``os.environ``.
_SECRET_ENV_FILES = (
    ROOT / ".secrets" / "clickup.env",
    ROOT / ".secrets" / "sentry.env",
)

# Current-process state: whether telemetry is active, which script is using it,
# and which values must be removed from outgoing events.
_STATE = {"active": False, "component": None, "scrub_values": ()}


# Configuration helpers

class MissingSentryCredentials(RuntimeError):
    """Explain that error reporting cannot start without a required setting."""

    pass


def _read_known_values(path):
    """Read recognized error-reporting settings from a local settings file.

    Example input: SENTRY_DSN=https://example.com/sentrydsn
    Example output: {"SENTRY_DSN": "https://example.com/sentrydsn"}
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

        # Accept shell-style lines such as ``export SENTRY_DSN=...``.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        # Ignore malformed lines and settings this module does not recognize.
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in KNOWN_KEYS:
            continue

        # Normalize whitespace and one matching pair of surrounding quotes.
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if value:
            values[key] = value

    return values


def load_sentry_env(required_keys=(), environ=None, env_path=None):
    """Resolve Sentry settings, preferring environment variables over a file.

    ``required_keys`` controls which missing values raise
    ``MissingSentryCredentials``. With no required keys, the function returns
    whichever recognized settings are available.

    Example input: required_keys=("SENTRY_DSN",)
    Example output: {"SENTRY_DSN": "https://example.com/sentrydsn"}
    """

    # Use the real process environment unless a caller supplies a test mapping.
    environ = os.environ if environ is None else environ

    # Read all recognized keys by default, or only the explicitly requested set.
    keys = tuple(required_keys) or tuple(KNOWN_KEYS)
    file_values = {}

    # Touch the settings file only when the environment cannot answer fully.
    if any(not environ.get(key) for key in keys):
        file_values = _read_known_values(env_path or DEFAULT_ENV_PATH)

    # Environment variables take precedence over values from the file.
    resolved = {}
    for key in keys:
        value = environ.get(key) or file_values.get(key)
        if value:
            resolved[key] = value

    # Required settings fail together so the caller gets one useful error.
    missing = [key for key in tuple(required_keys) if not resolved.get(key)]
    if missing:
        raise MissingSentryCredentials("Missing " + " or ".join(missing))

    return resolved


# Secret-redaction helpers


def _collect_secret_values(environ):
    """Collect likely secret values from an environment-style mapping.

    A value is treated as secret when its key contains a marker such as
    ``TOKEN`` or ``PASSWORD`` and its value is a reasonably long string.

    Example input: {"CLICKUP_API_KEY": "example-secret", "MODE": "live"}
    Example output: ("example-secret",)
    """
    values = []

    # Keep only string values long enough to be meaningful redaction needles.
    for name, value in environ.items():
        if not isinstance(value, str) or len(value) < _MIN_SECRET_LENGTH:
            continue

        # Match secret markers case-insensitively against the setting name.
        upper = name.upper()
        if any(marker in upper for marker in _SECRET_NAME_MARKERS):
            values.append(value)

    return tuple(values)


def _read_all_env_file_values(path):
    """Read every setting from a local environment file.

    Unlike ``_read_known_values``, this helper keeps every valid key. That
    allows the scrubber to find secrets, such as a ClickUp API key, that a
    script reads directly from a file without adding to ``os.environ``.

    A missing file is normal and returns an empty mapping. Other read errors
    propagate so ``scrub_event`` can drop the event instead of sending it with
    an incomplete redaction set.

    Example input: CLICKUP_API_KEY='example-secret'
    Example output: {"CLICKUP_API_KEY": "example-secret"}
    """
    # Absence is expected on hosts that do not use this particular secret file.
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}

    lines = text.splitlines()
    values = {}

    # Parse each non-empty, non-comment line as a KEY=VALUE pair.
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Accept shell-style lines such as ``export CLICKUP_API_KEY=...``.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        # A key and equals sign are both required; the value may contain "=".
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue

        # Normalize whitespace and one matching pair of surrounding quotes.
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if value:
            values[key] = value

    return values


def _secret_values_from_files():
    """Merge settings from every configured secret file.

    Missing files contribute nothing. Any other read failure propagates so the
    caller can fail closed instead of trusting an incomplete secret list.
    """
    merged = {}

    # Later files replace duplicate keys from earlier files.
    for path in _SECRET_ENV_FILES:
        merged.update(_read_all_env_file_values(path))

    return merged


def _current_scrub_values():
    """Build the complete set of secret values immediately before sending.

    The set combines values saved during initialization, secrets currently in
    ``os.environ``, and secrets read fresh from configured files. Re-reading at
    send time catches credentials loaded after telemetry started.

    Collection errors deliberately propagate to ``scrub_event``. When the
    scrubber cannot prove its redaction set is complete, it drops the event.
    """
    # Start with the DSN and environment secrets captured during initialization.
    values = set(v for v in _STATE["scrub_values"] if v)

    # Add secrets loaded into the process after telemetry initialization.
    values.update(_collect_secret_values(os.environ))

    # Add secrets that scripts may read directly from local settings files.
    values.update(_collect_secret_values(_secret_values_from_files()))

    return tuple(values)


def _set_scrub_values_for_tests(values):
    """Replace the saved secret list through the test-only seam."""
    _STATE["scrub_values"] = tuple(values)


def _scrub_text(text, values):
    """Replace every known secret in a string with ``[redacted]``.

    Example input: text="token=example-secret", values=("example-secret",)
    Example output: "token=[redacted]"
    """
    # A string may contain more than one secret, so apply every replacement.
    for value in values:
        if value and value in text:
            text = text.replace(value, "[redacted]")

    return text


def _scrub_obj(obj, values):
    """Recursively remove known secrets from a nested event value.

    Strings are scrubbed directly, mappings and sequences are traversed, and
    values such as numbers or booleans pass through unchanged.
    """
    # Strings are the only values that can contain a secret substring.
    if isinstance(obj, str):
        return _scrub_text(obj, values)

    # Preserve mapping keys while recursively scrubbing their values.
    if isinstance(obj, dict):
        return {key: _scrub_obj(value, values) for key, value in obj.items()}

    # Sentry accepts lists here, so tuples are normalized to lists as before.
    if isinstance(obj, (list, tuple)):
        return [_scrub_obj(item, values) for item in obj]

    return obj


def scrub_event(event, hint):
    """Return a redacted Sentry event, or ``None`` when scrubbing fails.

    Sentry calls this function through its ``before_send`` hook. The ``hint``
    parameter is part of that callback interface even though Captain does not
    need it.
    """
    try:
        # Calculate the redaction set at send time, then scrub the whole event.
        values = _current_scrub_values()
        return _scrub_obj(event, values)
    except Exception:
        # If scrubbing fails partway through, we cannot know whether the
        # returned structure still contains an unredacted secret. Dropping
        # the event (None tells Sentry's before_send contract to discard it)
        # is the only safe direction: losing a diagnostic event is
        # acceptable, leaking a secret is not.
        return None


# Sentry lifecycle and capture helpers


def _import_sdk():
    """Load the Sentry SDK only when a telemetry operation needs it.

    Keeping this import local makes Sentry an optional dependency for every
    Captain script that imports this module.
    """
    import sentry_sdk

    return sentry_sdk


def _git_release():
    """Return a Sentry release name for the current Git commit, when available.

    Example output: "captain-workspace@a1b2c3d"
    """
    try:
        # Bound the Git lookup so telemetry can never stall a caller for long.
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=5,
        )

        if out.returncode == 0 and out.stdout.strip():
            return "captain-workspace@" + out.stdout.strip()
    except Exception:
        # Git metadata enriches reports but is never required to send them.
        pass

    return None


def is_active():
    """Return whether Sentry started successfully for this process."""
    return bool(_STATE["active"])


def _reset_for_tests():
    """Return error-reporting state to a clean, inactive state for tests."""
    # Reinitializing without a DSN disables an existing Sentry client.
    if _STATE["active"]:
        try:
            _import_sdk().init()
        except Exception:
            pass

    # Clear all local state even when disabling the SDK itself failed.
    _STATE["active"] = False
    _STATE["component"] = None
    _STATE["scrub_values"] = ()


def init_telemetry(component, environ=None, env_path=None, _sdk_options=None):
    """Start safe error reporting for one Captain script.

    The function returns ``True`` only when Sentry initializes completely.
    Disabled telemetry, absent credentials, a missing SDK, and initialization
    errors all return ``False`` without changing caller behavior.

    Example input: component="daily_cycle"
    Example output: True
    """
    # Use the real process environment unless a caller supplies a test mapping.
    environ = os.environ if environ is None else environ

    # Every initialization attempt begins in a known inactive state.
    _STATE["active"] = False
    _STATE["component"] = component

    # Normalize the opt-out flag defensively because test mappings may contain
    # non-string values even though a real process environment cannot.
    disabled_flag = environ.get("CAPTAIN_SENTRY_DISABLED", "")
    if not isinstance(disabled_flag, str):
        try:
            disabled_flag = str(disabled_flag)
        except Exception:
            disabled_flag = ""

    if disabled_flag.strip() == "1":
        return False

    # Resolve optional Sentry settings without making configuration fatal.
    try:
        resolved = load_sentry_env((), environ=environ, env_path=env_path)
    except Exception:
        return False

    dsn = resolved.get("SENTRY_DSN")
    if not dsn:
        return False

    # Import the optional SDK only after configuration says telemetry is usable.
    try:
        sdk = _import_sdk()
    except Exception:
        return False

    # Seed the scrubber with every current secret plus the DSN itself.
    _STATE["scrub_values"] = _collect_secret_values(environ) + (dsn,)

    # Disable Sentry's raw argv integration when the installed SDK provides it.
    try:
        from sentry_sdk.integrations.argv import ArgvIntegration

        disabled_integrations = [ArgvIntegration()]
    except Exception:
        disabled_integrations = []

    # Privacy-safe defaults apply to every Captain telemetry client.
    options = {
        "dsn": dsn,
        "environment": resolved.get("SENTRY_ENVIRONMENT") or DEFAULT_ENVIRONMENT,
        "traces_sample_rate": 0,
        "send_default_pii": False,
        "include_local_variables": False,
        "shutdown_timeout": 2,
        "before_send": scrub_event,
        # Sentry's default ArgvIntegration adds the raw command line to every
        # event. Disable it so only summarize_argv's content-free version ships.
        "disabled_integrations": disabled_integrations,
    }

    # Attach the current commit when Git metadata is available.
    release = _git_release()
    if release:
        options["release"] = release

    # Tests may override SDK options without changing production defaults.
    if _sdk_options:
        options.update(_sdk_options)

    # Initialization and tagging are one operation: either both work or the
    # module remains inactive.
    try:
        sdk.init(**options)
        sdk.set_tag("component", component)
    except Exception:
        return False

    _STATE["active"] = True
    return True


def capture_message(message, level="error", fingerprint=None, extra=None):
    """Send a plain diagnostic message when telemetry is active.

    ``fingerprint`` can group related events, while ``extra`` adds structured
    diagnostic context. The function returns whether the send path completed.
    """
    # Inactive telemetry is a deliberate no-op for callers.
    if not _STATE["active"]:
        return False

    try:
        sdk = _import_sdk()

        # A new scope keeps this message's metadata out of later events.
        with sdk.new_scope() as scope:
            if fingerprint:
                scope.fingerprint = list(fingerprint)

            if extra:
                for key, value in extra.items():
                    scope.set_extra(key, value)

            sdk.capture_message(message, level=level)

        # Give the short-lived CLI process a bounded chance to deliver the event.
        sdk.flush(timeout=2)
        return True
    except Exception:
        # Telemetry must never turn a diagnostic message into a script failure.
        return False


# Privacy-safe command-line metadata


_ARGV_INLINE_SEPARATORS = ("=", ":")


def summarize_argv(argv):
    """Content-free, structural summary of a command line for Sentry.

    Captain keeps the script name, a leading subcommand, and option names. It
    drops all option values and later positional arguments because those may
    contain Slack text, ClickUp content, names, email addresses, or secrets.
    Inline forms such as ``--owner=Alex`` keep only ``--owner``.

    Only ``argv[1]`` can become the subcommand. A later bare token may actually
    be an option value, and safely distinguishing the two would require each
    caller's argument schema.

    Example input: ["clickup_write.py", "create-task", "--owner=Alex"]
    Example output: {
        "script": "clickup_write.py",
        "subcommand": "create-task",
        "flags": ["--owner"],
    }
    """
    # Normalize Path objects and other argument-like values to strings.
    argv = [str(arg) for arg in argv]

    # Keep only the script filename, never its full local path.
    script = Path(argv[0]).name if argv else None
    rest = argv[1:]

    # Only the first post-script token can safely be treated as a subcommand.
    subcommand = rest[0] if rest and not rest[0].startswith("-") else None

    # Record option names while discarding every option and positional value.
    flags = []
    for arg in rest:
        if arg.startswith("-") and arg != "-":
            # Reduce --flag=value and --flag:value to the safe name --flag.
            inline_sep = next(
                (sep for sep in _ARGV_INLINE_SEPARATORS if sep in arg), None
            )
            name = arg.partition(inline_sep)[0] if inline_sep else arg
            flags.append(name)

    return {"script": script, "subcommand": subcommand, "flags": flags}


def capture_exception(exc, component=None):
    """Report an unexpected exception without exposing command-line values.

    The optional ``component`` overrides the scope tag for this event. The
    return value says whether the capture path completed, not whether Sentry's
    remote service ultimately accepted the event.
    """
    # Inactive telemetry is a deliberate no-op for callers.
    if not _STATE["active"]:
        return False

    try:
        sdk = _import_sdk()

        # A new scope keeps exception-specific tags and metadata isolated.
        with sdk.new_scope() as scope:
            if component:
                scope.set_tag("component", component)

            scope.set_extra("argv", summarize_argv(sys.argv))
            sdk.capture_exception(exc)

        # Give the short-lived CLI process a bounded chance to deliver the event.
        sdk.flush(timeout=2)
        return True
    except BaseException:
        # BaseException (not just Exception) is intentional: this helper is
        # invoked from guard.__exit__ while the caller's *original* exception
        # is already in flight. sdk.flush(timeout=2) can be interrupted by a
        # KeyboardInterrupt (or raise SystemExit); if that escaped here it
        # would propagate out of __exit__ and replace the caller's real
        # exception with an unrelated interrupt. The original exception is
        # the signal that matters, so it must win. Swallowing the interrupt
        # here does not strand the process in an un-interruptible state:
        # flush() is already bounded by its own 2-second timeout regardless
        # of the interrupt, this capture path is only ever a few statements
        # long, and the guard always re-raises the original exception
        # immediately afterward (see guard.__exit__), which continues
        # unwinding the stack right away.
        return False


def capture_checkin(monitor_slug, status, monitor_config=None):
    """Report the status of a scheduled process to Sentry Cron Monitoring.

    ``monitor_slug`` identifies the monitor, ``status`` describes this run,
    and ``monitor_config`` may define the expected schedule.
    """
    # Inactive telemetry is a deliberate no-op for callers.
    if not _STATE["active"]:
        return False

    try:
        sdk = _import_sdk()
        from sentry_sdk.crons import capture_checkin as _sdk_checkin

        # Forward the check-in using Sentry's cron-specific API.
        _sdk_checkin(
            monitor_slug=monitor_slug,
            status=status,
            monitor_config=monitor_config,
        )

        # Give the short-lived CLI process a bounded chance to deliver the event.
        sdk.flush(timeout=2)
        return True
    except BaseException:
        # See capture_exception above: this can run while an original
        # exception (possibly a KeyboardInterrupt/SystemExit) is unwinding
        # through a guarded call site, and must never let a second,
        # unrelated BaseException from the flush escape and displace it.
        return False


def _is_interactive():
    """True only when we can positively confirm a human is at a terminal.

    Deliberately fails toward "unattended": a closed stdin makes isatty()
    raise ValueError, a replaced stdin may not have isatty() at all, and
    under some launchers sys.stdin is None. In every one of those cases we
    cannot show a human anything, so the run is treated as unattended and
    the alert is allowed through -- for a fleet of crons, an extra event is
    cheaper than a silent failure.

    Catches BaseException for the same reason capture_exception does: this
    is called from guard.__exit__ while the caller's real exception is
    already unwinding, and anything escaping here would replace it.
    """
    try:
        # A TTY means a human is present to see the command's own error message.
        return bool(sys.stdin.isatty())
    except BaseException:
        # Unknown terminal state is treated as unattended so failures are visible.
        return False


# Automatic exception guard


class guard(ContextDecorator):
    """Initialize telemetry and report uncaught failures around a call or block.

    ``ContextDecorator`` allows both ``with guard("component")`` and
    ``@guard("component")`` usage. The original exception always continues to
    the caller after Captain attempts to report it.
    """

    def __init__(self, component, environ=None, env_path=None, _sdk_options=None):
        """Save the settings needed when the protected work begins."""
        self.component = component
        self._environ = environ
        self._env_path = env_path
        self._sdk_options = _sdk_options

    def __enter__(self):
        """Start error reporting before the protected work begins."""
        # Initialization is best-effort; protected work runs either way.
        try:
            init_telemetry(
                self.component,
                environ=self._environ,
                env_path=self._env_path,
                _sdk_options=self._sdk_options,
            )
        except Exception:
            pass

        return self

    def __exit__(self, exc_type, exc, tb):
        """Report unexpected failures, then allow the original failure to continue."""
        # A CLI may turn an OSError into a clean SystemExit message with
        # ``raise SystemExit(...) from err``. Report the underlying OSError for
        # unattended jobs, where no human can see that clean terminal message.
        # Explicit ``__cause__`` avoids alerting on incidental exception chains.
        if (
            isinstance(exc, SystemExit)
            and isinstance(exc.__cause__, OSError)
            and not _is_interactive()
        ):
            capture_exception(exc.__cause__, component=self.component)
            return False

        # Normal completion and deliberate user exits do not need reporting.
        if exc is None or isinstance(exc, (SystemExit, KeyboardInterrupt)):
            return False

        # Report all other uncaught failures under this guard's component.
        capture_exception(exc, component=self.component)

        # False tells the context-manager protocol to re-raise the exception.
        return False


# Command-line self-test


def _self_test():
    """Send a harmless test message and return a shell-friendly exit code."""
    # Initialization failure gives the operator likely configuration causes.
    active = init_telemetry("telemetry-self-test")
    if not active:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "telemetry inactive "
                        "(missing sentry-sdk, DSN, or disabled)"
                    ),
                }
            )
        )
        return 1

    # A successful initialization still needs a real capture-path check.
    sent = capture_message("captain-telemetry self-test", level="error")
    print(json.dumps({"ok": bool(sent), "sent": bool(sent)}))

    return 0 if sent else 1


if __name__ == "__main__":
    # This module exposes only the explicit self-test as a command-line action.
    if "--self-test" in sys.argv[1:]:
        raise SystemExit(_self_test())

    print(json.dumps({"ok": False, "error": "usage: captain_telemetry.py --self-test"}))
    raise SystemExit(2)
