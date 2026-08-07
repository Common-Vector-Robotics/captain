#!/usr/bin/env python3
"""All Sentry contact for the Captain workspace lives in this module.

Design rules (spec 2026-07-27-captain-sentry-integration-design.md):
- sentry_sdk is imported lazily and only here.
- Missing package, missing DSN, or CAPTAIN_SENTRY_DISABLED=1 -> silent no-op.
- Telemetry never changes caller behavior and never raises.
- Secret values are scrubbed from every outgoing event.
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

# Files that may hold secret values which never pass through os.environ (e.g.
# scripts/clickup_credentials.py reads CLICKUP_API_KEY straight out of
# .secrets/clickup.env into a local dict, not into the environment). These are
# read fresh on every scrub_event call so a secret loaded after init_telemetry
# is still redacted -- see fix for the two scrubber-bypass leak paths.
_SECRET_ENV_FILES = (
    ROOT / ".secrets" / "clickup.env",
    ROOT / ".secrets" / "sentry.env",
)

_STATE = {"active": False, "component": None, "scrub_values": ()}


class MissingSentryCredentials(RuntimeError):
    pass


def _read_known_values(path):
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
            line = line[len("export "):].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in KNOWN_KEYS:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            values[key] = value
    return values


def load_sentry_env(required_keys=(), environ=None, env_path=None):
    environ = os.environ if environ is None else environ
    keys = tuple(required_keys) or tuple(KNOWN_KEYS)
    file_values = {}
    if any(not environ.get(key) for key in keys):
        file_values = _read_known_values(env_path or DEFAULT_ENV_PATH)
    resolved = {}
    for key in keys:
        value = environ.get(key) or file_values.get(key)
        if value:
            resolved[key] = value
    missing = [key for key in tuple(required_keys) if not resolved.get(key)]
    if missing:
        raise MissingSentryCredentials("Missing " + " or ".join(missing))
    return resolved


def _collect_secret_values(environ):
    values = []
    for name, value in environ.items():
        if not isinstance(value, str) or len(value) < _MIN_SECRET_LENGTH:
            continue
        upper = name.upper()
        if any(marker in upper for marker in _SECRET_NAME_MARKERS):
            values.append(value)
    return tuple(values)


def _read_all_env_file_values(path):
    """Like _read_known_values, but returns every KEY=VALUE pair rather than
    only the sentry-specific KNOWN_KEYS -- used to catch secrets (e.g. the
    ClickUp API key) that a script reads directly out of a .secrets/*.env
    file without ever putting them into os.environ.

    A file that simply does not exist is the everyday case (most hosts have
    no .secrets/clickup.env at all) and must NOT be treated as an error --
    it yields no needles from this source and callers proceed normally. Any
    other failure (permission error, EIO, a directory where a file was
    expected, undecodable bytes) means we genuinely could not read a file
    that IS there, so it must propagate to the caller rather than being
    swallowed here -- see _current_scrub_values / scrub_event, which turn an
    incomplete redaction set into a fail-closed dropped event rather than
    shipping a partially-scrubbed one."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    lines = text.splitlines()
    values = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            values[key] = value
    return values


def _secret_values_from_files():
    """Merge needles from every configured .secrets/*.env file. A given file
    being absent is normal (see _read_all_env_file_values) and simply
    contributes nothing; any other failure reading a file that IS present is
    a genuine "cannot assemble the redaction set" condition and is allowed to
    propagate -- it must NOT be swallowed here, or a transient read error on
    one file would silently drop that file's needles while the event still
    ships, scrubbed against an incomplete set. The caller (_current_scrub_values
    / scrub_event) is responsible for turning that into a fail-closed dropped
    event rather than a partially-scrubbed one."""
    merged = {}
    for path in _SECRET_ENV_FILES:
        merged.update(_read_all_env_file_values(path))
    return merged


def _current_scrub_values():
    """Compute the redaction set at send time rather than trusting only the
    init-time snapshot in _STATE["scrub_values"]. Covers: (i) anything
    already seeded into _STATE (dsn + the init-time os.environ snapshot,
    also the test seam _set_scrub_values_for_tests), (ii) secret-looking
    values in the *current* os.environ (picks up secrets loaded after
    init_telemetry ran, e.g. session_report.load_default_env), and (iii)
    secret-looking values read fresh from .secrets/*.env files, which
    catches values that never enter os.environ at all.

    Deliberately does NOT catch exceptions from either collection step: if
    the os.environ scan or a present-but-unreadable secrets file raises, we
    cannot know the redaction set is complete, and shipping an event scrubbed
    against a partial set could leak a credential (e.g. CLICKUP_API_KEY,
    which lives in no other source on some code paths). The exception must
    propagate to scrub_event's outer handler, which fails CLOSED (returns
    None, discarding the event) rather than failing open. A merely-absent
    secrets file is not an error -- see _read_all_env_file_values -- so the
    everyday case of no .secrets/ directory still yields a normal, successful
    scrub."""
    values = set(v for v in _STATE["scrub_values"] if v)
    values.update(_collect_secret_values(os.environ))
    values.update(_collect_secret_values(_secret_values_from_files()))
    return tuple(values)


def _set_scrub_values_for_tests(values):
    _STATE["scrub_values"] = tuple(values)


def _scrub_text(text, values):
    for value in values:
        if value and value in text:
            text = text.replace(value, "[redacted]")
    return text


def _scrub_obj(obj, values):
    if isinstance(obj, str):
        return _scrub_text(obj, values)
    if isinstance(obj, dict):
        return {key: _scrub_obj(value, values) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub_obj(item, values) for item in obj]
    return obj


def scrub_event(event, hint):
    try:
        values = _current_scrub_values()
        return _scrub_obj(event, values)
    except Exception:
        # If scrubbing fails partway through, we cannot know whether the
        # returned structure still contains an unredacted secret. Dropping
        # the event (None tells Sentry's before_send contract to discard it)
        # is the only safe direction: losing a diagnostic event is
        # acceptable, leaking a secret is not.
        return None


def _import_sdk():
    import sentry_sdk

    return sentry_sdk


def _git_release():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return "captain-workspace@" + out.stdout.strip()
    except Exception:
        pass
    return None


def is_active():
    return bool(_STATE["active"])


def _reset_for_tests():
    if _STATE["active"]:
        try:
            _import_sdk().init()  # re-init with no DSN disables the client
        except Exception:
            pass
    _STATE["active"] = False
    _STATE["component"] = None
    _STATE["scrub_values"] = ()


def init_telemetry(component, environ=None, env_path=None, _sdk_options=None):
    environ = os.environ if environ is None else environ
    _STATE["active"] = False
    _STATE["component"] = component
    disabled_flag = environ.get("CAPTAIN_SENTRY_DISABLED", "")
    if not isinstance(disabled_flag, str):
        try:
            disabled_flag = str(disabled_flag)
        except Exception:
            disabled_flag = ""
    if disabled_flag.strip() == "1":
        return False
    try:
        resolved = load_sentry_env((), environ=environ, env_path=env_path)
    except Exception:
        return False
    dsn = resolved.get("SENTRY_DSN")
    if not dsn:
        return False
    try:
        sdk = _import_sdk()
    except Exception:
        return False
    _STATE["scrub_values"] = _collect_secret_values(environ) + (dsn,)
    try:
        from sentry_sdk.integrations.argv import ArgvIntegration
        disabled_integrations = [ArgvIntegration()]
    except Exception:
        disabled_integrations = []
    options = {
        "dsn": dsn,
        "environment": resolved.get("SENTRY_ENVIRONMENT") or DEFAULT_ENVIRONMENT,
        "traces_sample_rate": 0,
        "send_default_pii": False,
        "include_local_variables": False,
        "shutdown_timeout": 2,
        "before_send": scrub_event,
        # sentry_sdk 2.66.1: ArgvIntegration is a default integration that adds
        # extra["sys.argv"] -- the raw, unsanitized command line -- to every
        # captured event. That duplicated (and used to bypass) our own argv
        # handling in capture_exception. There is no per-event way to strip an
        # already-collected extra key that is safe against every integration
        # version, so the integration itself is disabled at init time; verified
        # by hand (see tests/test_captain_telemetry.py) that with this option
        # set, event["extra"] never contains a "sys.argv" key at all.
        "disabled_integrations": disabled_integrations,
    }
    release = _git_release()
    if release:
        options["release"] = release
    if _sdk_options:
        options.update(_sdk_options)
    try:
        sdk.init(**options)
        sdk.set_tag("component", component)
    except Exception:
        return False
    _STATE["active"] = True
    return True


def capture_message(message, level="error", fingerprint=None, extra=None):
    if not _STATE["active"]:
        return False
    try:
        sdk = _import_sdk()
        with sdk.new_scope() as scope:
            if fingerprint:
                scope.fingerprint = list(fingerprint)
            if extra:
                for key, value in extra.items():
                    scope.set_extra(key, value)
            sdk.capture_message(message, level=level)
        sdk.flush(timeout=2)
        return True
    except Exception:
        return False


_ARGV_INLINE_SEPARATORS = ("=", ":")


def summarize_argv(argv):
    """Content-free, structural summary of a command line for Sentry.

    This is the allowlist replacement for the old secret-name-denylist
    approach (`sanitize_argv`, removed): that function only redacted a flag's
    *value* when the flag's *name* matched token/key/secret/dsn, so it did
    nothing for `--text`, `--name`, `--description`, `--owner`, `--source`,
    `--action-note`, `--items`, etc. -- exactly the flags the daily-loop
    scripts use to carry Slack message text, ClickUp comment bodies, incident
    titles, and employee names/emails on the command line. Denylisting secret
    *names* can never be complete against arbitrary free-form content, so
    instead: carry no argument *values* at all, ever. What's useful for
    debugging without being able to carry content is: the script name, which
    subcommand (if any) was invoked, and which flags were passed (their
    values are never included, inline "--flag=value"/"--flag:value" forms
    included -- only the name before the separator is kept). Any other
    positional argument (i.e. not the script and not the one subcommand
    token) is dropped entirely rather than included as data, since it could
    just as easily be a free-text value as a subcommand.

    The subcommand is only ever read from argv[1] (the token immediately
    after the script name). A later non-flag token is never promoted to
    "subcommand", because without each script's argparse schema there is no
    reliable way to tell "a bare positional subcommand" apart from "the
    value of the preceding --flag" (e.g. `--morning path.json`), and
    guessing wrong would put a flag's value into the summary. This means a
    script that takes a leading option before its subcommand (e.g.
    `clickup_write.py --execute create-task ...`) reports no subcommand;
    that completeness loss is the price of the summary never being able to
    carry a value.
    """
    argv = [str(a) for a in argv]
    script = Path(argv[0]).name if argv else None
    rest = argv[1:]
    subcommand = rest[0] if rest and not rest[0].startswith("-") else None
    flags = []
    for arg in rest:
        if arg.startswith("-") and arg != "-":
            inline_sep = next(
                (sep for sep in _ARGV_INLINE_SEPARATORS if sep in arg), None
            )
            name = arg.partition(inline_sep)[0] if inline_sep else arg
            flags.append(name)
    return {"script": script, "subcommand": subcommand, "flags": flags}


def capture_exception(exc, component=None):
    if not _STATE["active"]:
        return False
    try:
        sdk = _import_sdk()
        with sdk.new_scope() as scope:
            if component:
                scope.set_tag("component", component)
            scope.set_extra("argv", summarize_argv(sys.argv))
            sdk.capture_exception(exc)
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
    if not _STATE["active"]:
        return False
    try:
        sdk = _import_sdk()
        from sentry_sdk.crons import capture_checkin as _sdk_checkin

        _sdk_checkin(
            monitor_slug=monitor_slug,
            status=status,
            monitor_config=monitor_config,
        )
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
        return bool(sys.stdin.isatty())
    except BaseException:
        return False


class guard(ContextDecorator):
    def __init__(self, component, environ=None, env_path=None, _sdk_options=None):
        self.component = component
        self._environ = environ
        self._env_path = env_path
        self._sdk_options = _sdk_options

    def __enter__(self):
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
        # A SystemExit chained from an underlying error (`raise SystemExit(...)
        # from err`) is a CLI turning a real failure into a clean one-line
        # message for a human. The message is for the human; the cause is
        # infrastructure and still belongs in Sentry. Deliberately `__cause__`
        # (explicit `raise ... from`) and not `__context__`, so only
        # intentional chaining alerts -- an incidental "during handling of the
        # above exception" chain must not produce a false positive.
        #
        # Gated on the run being unattended, because every chained site
        # raises from an OSError against a path the caller supplied (--db,
        # --path, --clickup, --morning). Under cron those are the built-in
        # defaults, so an OSError means a full disk or a corrupt file with
        # nobody watching -- precisely what the alert exists for. Run by hand
        # the same OSError is usually a typo'd path, and the operator is
        # already reading the clean one-line message on their own terminal;
        # paging someone over a typo is what clickup_write.py,
        # captain_activity.py and daily_activity_digest.py each take pains to
        # prevent, and this clause must not reintroduce it by the back door.
        # A tty is the discriminator: either a human is there to see the
        # message, or nobody is. When tty-ness cannot be determined we treat
        # the run as unattended and alert (see _is_interactive).
        if (
            isinstance(exc, SystemExit)
            and isinstance(exc.__cause__, OSError)
            and not _is_interactive()
        ):
            capture_exception(exc.__cause__, component=self.component)
            return False
        if exc is None or isinstance(exc, (SystemExit, KeyboardInterrupt)):
            return False
        capture_exception(exc, component=self.component)
        return False  # always re-raise


def _self_test():
    active = init_telemetry("telemetry-self-test")
    if not active:
        print(json.dumps({"ok": False, "error": "telemetry inactive "
                          "(missing sentry-sdk, DSN, or disabled)"}))
        return 1
    sent = capture_message("captain-telemetry self-test", level="error")
    print(json.dumps({"ok": bool(sent), "sent": bool(sent)}))
    return 0 if sent else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        raise SystemExit(_self_test())
    print(json.dumps({"ok": False, "error": "usage: captain_telemetry.py --self-test"}))
    raise SystemExit(2)
