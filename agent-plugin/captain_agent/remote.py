"""Send one idempotent Captain turn through the remote HTTPS boundary."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import UUID

from .reporting import (
    ALLOWED_STATUSES,
    REPORT_ID_PATTERN,
    CaptainReportResult,
    canonical_result,
)


MAX_REMOTE_BODY_BYTES = 262_144
REQUEST_TIMEOUT_SECONDS = 15
POLL_DEADLINE_SECONDS = 330
INITIAL_POLL_DELAY_SECONDS = 2
MAX_POLL_DELAY_SECONDS = 10
MAX_RESULT_ITEMS = 32
MAX_RESULT_STRING_CHARACTERS = 4_096
MAX_PENDING_QUESTIONS = 20
MAX_PENDING_QUESTION_CHARACTERS = 1_000
MEMBER_TOKEN_PATTERN = re.compile(
    r"^cap_v1_([A-Za-z0-9_-]{16})\.[A-Za-z0-9_-]{43}$"
)


class RemoteConfigurationError(ValueError):
    """Raised when remote mode is incomplete or unsafe."""


class _NetworkFailure(Exception):
    """Hide all raw network details from the public result."""


class _ResponseFailure(Exception):
    """Mark a response that cannot be trusted or parsed safely."""


class _DeadlineExpired(Exception):
    """Prevent a new socket request after the overall deadline."""


class _HttpStatus(Exception):
    """Carry only an HTTP status and response headers, never its body."""

    def __init__(self, status: int, headers: Any):
        super().__init__("remote HTTP status")
        self.status = status
        self.headers = headers


class _RejectRedirects(HTTPRedirectHandler):
    """Reject every redirect so urllib cannot forward the bearer credential."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _configuration_error() -> RemoteConfigurationError:
    """Return one fixed error that cannot expose a URL or credential."""

    return RemoteConfigurationError("Captain remote configuration is invalid.")


def _validated_origin(value: str) -> str:
    """Return one normalized bare origin or raise a fixed safe error."""

    if not isinstance(value, str):
        raise _configuration_error()
    candidate = value.strip()
    if (
        not candidate
        or not candidate.isascii()
        or any(character.isspace() for character in candidate)
        or "\\" in candidate
        or "%" in candidate
        or "?" in candidate
        or "#" in candidate
    ):
        raise _configuration_error()

    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise _configuration_error() from None

    scheme = parsed.scheme.lower()
    if (
        not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.netloc.endswith(":")
    ):
        raise _configuration_error()

    if scheme != "https":
        loopback = hostname.lower() in {"127.0.0.1", "::1", "localhost"}
        if scheme != "http" or not loopback:
            raise _configuration_error()

    return f"{scheme}://{parsed.netloc}"


def _validated_token(value: str) -> str:
    """Return a header-safe nonblank token without placing it in an error."""

    if not isinstance(value, str):
        raise _configuration_error()
    token = value.strip()
    if (
        not token
        or not token.isascii()
        or len(token) > 8_192
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
    ):
        raise _configuration_error()
    return token


@dataclass(frozen=True)
class RemoteConfig:
    """The complete remote origin and its private member credential."""

    base_url: str
    member_token: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validated_origin(self.base_url))
        object.__setattr__(self, "member_token", _validated_token(self.member_token))


def remote_profile_id(config: RemoteConfig) -> str:
    """Derive a stable non-secret namespace from one normalized remote credential."""

    member_token = MEMBER_TOKEN_PATTERN.fullmatch(config.member_token)
    credential_identity = (
        b"member-lookup\0" + member_token.group(1).encode("ascii")
        if member_token
        else b"credential-digest\0"
        + hashlib.sha256(config.member_token.encode("utf-8")).digest()
    )
    framed = (
        b"captain-remote-profile-v1\0"
        + config.base_url.encode("ascii")
        + b"\0"
        + credential_identity
    )
    return hashlib.sha256(framed).hexdigest()


def read_remote_config(env: Mapping[str, str]) -> RemoteConfig | None:
    """Select remote mode only when both required values are nonblank."""

    url = str(env.get("CAPTAIN_REMOTE_URL", "")).strip()
    token = str(env.get("CAPTAIN_MEMBER_TOKEN", "")).strip()
    if not url and not token:
        return None
    if not url or not token:
        raise _configuration_error()
    return RemoteConfig(url, token)


def _unknown_result(report_id: str) -> CaptainReportResult:
    return canonical_result(
        report_id,
        "unknown_outcome",
        captain_feedback="Captain's remote completion could not be proven.",
    )


def _failed_result(report_id: str) -> CaptainReportResult:
    return canonical_result(
        report_id,
        "failed",
        captain_feedback="Captain rejected the remote turn.",
    )


def _configuration_result(report_id: str) -> CaptainReportResult:
    return canonical_result(
        report_id,
        "needs_configuration",
        captain_feedback="Captain remote authentication or configuration must be updated.",
    )


def _queued_result(report_id: str) -> CaptainReportResult:
    return canonical_result(
        report_id,
        "queued",
        captain_feedback=(
            "Captain is still processing this turn. Retry with the same report ID."
        ),
    )


def _busy_result(report_id: str) -> CaptainReportResult:
    return canonical_result(
        report_id,
        "failed",
        captain_feedback=(
            "Captain did not accept the remote turn before the retry deadline. "
            "Retry later with the same report ID."
        ),
    )


def _valid_uuid4(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _bounded_string(value: Any, *, limit: int = MAX_RESULT_STRING_CHARACTERS) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _string_list(
    value: Any,
    *,
    max_items: int = MAX_RESULT_ITEMS,
    max_characters: int = MAX_RESULT_STRING_CHARACTERS,
    nonblank: bool = False,
) -> list[str] | None:
    if not isinstance(value, list) or len(value) > max_items:
        return None
    if not all(
        _bounded_string(item, limit=max_characters)
        and (not nonblank or bool(item.strip()))
        for item in value
    ):
        return None
    return list(value)


def _captain_result(report_id: str, value: Any) -> CaptainReportResult | None:
    """Read the strict, bounded result contract emitted by the remote service."""

    if not isinstance(value, dict) or set(value) != {
        "report_id",
        "status",
        "clickup_updates",
        "captain_feedback",
        "questions",
        "warnings",
    }:
        return None
    if (
        value["report_id"] != report_id
        or not isinstance(value["status"], str)
        or value["status"] not in ALLOWED_STATUSES
        or not _bounded_string(value["captain_feedback"])
    ):
        return None

    questions = _string_list(
        value["questions"],
        max_items=MAX_PENDING_QUESTIONS,
        max_characters=MAX_PENDING_QUESTION_CHARACTERS,
        nonblank=True,
    )
    warnings = _string_list(value["warnings"])
    updates = value["clickup_updates"]
    if questions is None or warnings is None or not isinstance(updates, list):
        return None
    if len(updates) > MAX_RESULT_ITEMS:
        return None

    copied_updates: list[dict[str, Any]] = []
    for update in updates:
        if (
            not isinstance(update, dict)
            or set(update) != {"action", "task_id"}
            or not _bounded_string(update["action"])
            or not _bounded_string(update["task_id"])
        ):
            return None
        copied_updates.append(dict(update))

    return CaptainReportResult(
        report_id=report_id,
        status=value["status"],
        clickup_updates=copied_updates,
        captain_feedback=value["captain_feedback"],
        questions=questions,
        warnings=warnings,
    )


def _validated_envelope(
    value: Any,
    report_id: str,
    turn_id: str,
) -> tuple[str, CaptainReportResult | None]:
    """Validate IDs and the complete bounded remote envelope."""

    if not isinstance(value, dict) or not set(value).issubset(
        {"report_id", "turn_id", "turn_status", "result", "error"}
    ):
        raise _ResponseFailure
    if not {"report_id", "turn_id", "turn_status"}.issubset(value):
        raise _ResponseFailure
    if value["report_id"] != report_id or value["turn_id"] != turn_id:
        raise _ResponseFailure

    turn_status = value["turn_status"]
    if not isinstance(turn_status, str) or turn_status not in {
        "queued",
        "started",
        "succeeded",
        "failed",
        "timed_out",
        "unknown_outcome",
    }:
        raise _ResponseFailure

    error = value.get("error")
    if error is not None and (
        not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or not _bounded_string(error["code"])
        or not _bounded_string(error["message"])
    ):
        raise _ResponseFailure

    if turn_status in {"queued", "started"}:
        if "result" in value or "error" in value:
            raise _ResponseFailure
        return turn_status, None

    result = None
    if "result" in value:
        result = _captain_result(report_id, value["result"])
        if result is None:
            raise _ResponseFailure
    if turn_status == "succeeded" and result is None:
        raise _ResponseFailure
    if turn_status == "failed" and result is not None and result.status != "failed":
        raise _ResponseFailure
    if (
        turn_status == "unknown_outcome"
        and result is not None
        and result.status != "unknown_outcome"
    ):
        raise _ResponseFailure
    if turn_status == "timed_out" and result is not None:
        raise _ResponseFailure
    return turn_status, result


def serialize_remote_payload(payload: Mapping[str, Any], turn_id: str) -> bytes:
    """Serialize the exact request once after checking its strict union shape."""

    if not isinstance(payload, Mapping) or payload.get("turn_id") != turn_id:
        raise ValueError
    kind = payload.get("kind")
    expected_keys = (
        {"turn_id", "kind", "report", "metadata"}
        if kind == "report"
        else {"turn_id", "kind", "reply"}
        if kind == "reply"
        else set()
    )
    if set(payload) != expected_keys:
        raise ValueError
    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ValueError from None
    if len(body) > MAX_REMOTE_BODY_BYTES:
        raise ValueError
    return body


class RemoteCaptainClient:
    """Submit one turn once, then poll its validated path to a bounded deadline."""

    def __init__(
        self,
        config: RemoteConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = RemoteConfig(config.base_url, config.member_token)
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        # Credentials are origin-only. Never inherit ambient proxy settings.
        self._opener = build_opener(ProxyHandler({}), _RejectRedirects())
        self.terminal_response = False

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        *,
        timeout: float,
    ) -> Any:
        request = Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._config.member_token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > MAX_REMOTE_BODY_BYTES:
                            raise _ResponseFailure
                    except ValueError:
                        raise _ResponseFailure from None
                raw = response.read(MAX_REMOTE_BODY_BYTES + 1)
                if len(raw) > MAX_REMOTE_BODY_BYTES:
                    raise _ResponseFailure
        except HTTPError as error:
            try:
                raise _HttpStatus(error.code, error.headers) from None
            finally:
                error.close()
        except _ResponseFailure:
            raise
        except (URLError, OSError, TimeoutError, HTTPException, ValueError):
            raise _NetworkFailure from None

        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON key")
                value[key] = item
            return value

        try:
            return json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=unique_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise _ResponseFailure from None

    def _request_before_deadline(
        self,
        method: str,
        url: str,
        deadline: float,
        body: bytes | None = None,
    ) -> Any:
        """Start one request only when positive overall time remains."""

        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _DeadlineExpired
        return self._request(
            method,
            url,
            body,
            timeout=min(float(REQUEST_TIMEOUT_SECONDS), remaining),
        )

    def _retry_after(self, headers: Any) -> float:
        values = headers.get_all("Retry-After") if headers is not None else None
        if not values or len(values) != 1:
            raise _ResponseFailure
        value = values[0].strip()
        if value.isdigit():
            if len(value) > 10:
                raise _ResponseFailure
            try:
                seconds = int(value)
            except ValueError:
                raise _ResponseFailure from None
        else:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    raise ValueError
                seconds = math.ceil(parsed.timestamp() - self._wall_clock())
            except (TypeError, ValueError, OverflowError):
                raise _ResponseFailure from None
        if seconds < 0:
            raise _ResponseFailure
        return float(max(seconds, 1))

    def _http_result(
        self,
        report_id: str,
        error: _HttpStatus,
        *,
        submitted: bool,
    ) -> CaptainReportResult:
        if error.status == 401:
            return _configuration_result(report_id)
        if error.status in {409, 413}:
            return _failed_result(report_id)
        if not submitted and 400 <= error.status < 500 and error.status != 429:
            return _failed_result(report_id)
        return _unknown_result(report_id)

    def submit_and_poll(
        self,
        report_id: str,
        turn_id: str,
        payload: Mapping[str, Any],
    ) -> CaptainReportResult:
        """Submit one stable turn, retrying only explicit pre-acceptance 429s."""

        self.terminal_response = False
        if (
            not isinstance(report_id, str)
            or REPORT_ID_PATTERN.fullmatch(report_id) is None
            or not _valid_uuid4(turn_id)
        ):
            return _failed_result(report_id if isinstance(report_id, str) else "invalid-report")
        try:
            body = serialize_remote_payload(payload, turn_id)
        except ValueError:
            return _failed_result(report_id)

        submit_path = f"/captain/v1/reports/{report_id}/turns"
        poll_path = f"{submit_path}/{turn_id}"
        deadline = self._clock() + POLL_DEADLINE_SECONDS
        submit_url = f"{self._config.base_url}{submit_path}"
        while True:
            try:
                response = self._request_before_deadline(
                    "POST",
                    submit_url,
                    deadline,
                    body,
                )
                break
            except _DeadlineExpired:
                return _busy_result(report_id)
            except _HttpStatus as error:
                if error.status != 429:
                    return self._http_result(report_id, error, submitted=False)
                try:
                    delay = self._retry_after(error.headers)
                except _ResponseFailure:
                    return _busy_result(report_id)
                if self._clock() + delay >= deadline:
                    return _busy_result(report_id)
                self._sleep(delay)
                if self._clock() >= deadline:
                    return _busy_result(report_id)
            except (_NetworkFailure, _ResponseFailure):
                return _unknown_result(report_id)

        try:
            turn_status, result = _validated_envelope(response, report_id, turn_id)
        except _ResponseFailure:
            return _unknown_result(report_id)
        if turn_status not in {"queued", "started"}:
            return self._terminal_result(report_id, turn_status, result)
        if self._clock() >= deadline:
            return _queued_result(report_id)

        normal_delay = float(INITIAL_POLL_DELAY_SECONDS)
        delay = normal_delay
        while True:
            if self._clock() + delay >= deadline:
                return _queued_result(report_id)
            self._sleep(delay)
            if self._clock() >= deadline:
                return _queued_result(report_id)
            try:
                response = self._request_before_deadline(
                    "GET",
                    f"{self._config.base_url}{poll_path}",
                    deadline,
                )
            except _DeadlineExpired:
                return _queued_result(report_id)
            except _HttpStatus as error:
                if error.status == 429:
                    try:
                        delay = self._retry_after(error.headers)
                    except _ResponseFailure:
                        return _unknown_result(report_id)
                    if self._clock() + delay >= deadline:
                        return _queued_result(report_id)
                    continue
                return self._http_result(report_id, error, submitted=True)
            except (_NetworkFailure, _ResponseFailure):
                return _unknown_result(report_id)

            try:
                turn_status, result = _validated_envelope(
                    response, report_id, turn_id
                )
            except _ResponseFailure:
                return _unknown_result(report_id)
            if turn_status not in {"queued", "started"}:
                return self._terminal_result(report_id, turn_status, result)
            if self._clock() >= deadline:
                return _queued_result(report_id)
            normal_delay = min(normal_delay * 2, float(MAX_POLL_DELAY_SECONDS))
            delay = normal_delay

    def _terminal_result(
        self,
        report_id: str,
        turn_status: str,
        result: CaptainReportResult | None,
    ) -> CaptainReportResult:
        """Map one validated terminal envelope to the existing public model."""

        self.terminal_response = True
        if result is not None:
            return result
        if turn_status == "failed":
            return _failed_result(report_id)
        return _unknown_result(report_id)
