"""Exercise durable state, transport, and dispatch for remote Captain turns."""

import hashlib
import json
import socket
import sqlite3
import stat
import sys
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-plugin"))

from captain_agent.client_state import (
    PendingCaptainQuestions,
    RemoteClientState,
    RemoteStateConflict,
    remote_state_path,
)
from captain_agent.dispatch import handle_captain_turn
from captain_agent.remote import (
    MAX_REMOTE_BODY_BYTES,
    RemoteCaptainClient,
    RemoteConfig,
    RemoteConfigurationError,
    read_remote_config,
)
from captain_agent.reporting import CaptainReportResult


VALID_REPORT = {
    "project": "Captain",
    "summary": ["Added the remote Captain adapter."],
    "changed_files": ["agent-plugin/captain_agent/remote.py"],
    "verification": [{"command": "pytest", "result": "pass"}],
    "decisions": [],
    "blockers": [],
    "risks": [],
    "next_steps": [],
}

TERMINAL_RESULT = {
    "report_id": "report-1",
    "status": "updated",
    "clickup_updates": [],
    "captain_feedback": "Updated the task.",
    "questions": [],
    "warnings": [],
}


class FakeClock:
    """Advance deterministic poll time whenever the client sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@contextmanager
def scripted_server(responses):
    """Serve scripted HTTP responses and record the real wire request boundary."""

    script = list(responses)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self._handle()

        def do_GET(self):
            self._handle()

        def _handle(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": self.headers,
                    "body": body,
                }
            )
            response = script.pop(0)
            if response.get("disconnect"):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return

            body_value = response.get("body", {})
            if isinstance(body_value, (dict, list)):
                response_body = json.dumps(body_value).encode("utf-8")
            elif isinstance(body_value, str):
                response_body = body_value.encode("utf-8")
            else:
                response_body = body_value
            self.send_response(response.get("status", 200))
            for name, value in response.get("headers", {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, script
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def remote_client(base_url, clock=None, token="member-token"):
    clock = clock or FakeClock()
    return RemoteCaptainClient(
        RemoteConfig(base_url, token),
        clock=clock.monotonic,
        wall_clock=clock.time,
        sleep=clock.sleep,
    )


def envelope(turn_id, turn_status="succeeded", **changes):
    value = {
        "report_id": "report-1",
        "turn_id": turn_id,
        "turn_status": turn_status,
    }
    if turn_status == "succeeded":
        value["result"] = dict(TERMINAL_RESULT)
    value.update(changes)
    return value


def assert_uuid(value):
    """Assert that a turn ID is a generated UUID rather than caller input."""

    assert UUID(value).version == 4


def test_report_turn_id_is_stable_for_the_same_payload(tmp_path):
    """A retry must reuse its initial report turn instead of creating a duplicate."""

    state = RemoteClientState(tmp_path / "remote.sqlite3")

    first = state.get_or_create_report_turn("report-1", "digest-1")
    second = state.get_or_create_report_turn("report-1", "digest-1")

    assert first == second
    assert_uuid(first)


def test_changed_initial_payload_is_a_conflict(tmp_path):
    """A report ID must never point to two differently digested initial turns."""

    state = RemoteClientState(tmp_path / "remote.sqlite3")
    state.get_or_create_report_turn("report-1", "digest-1")

    with pytest.raises(RemoteStateConflict):
        state.get_or_create_report_turn("report-1", "digest-2")


def test_report_turns_are_independent_for_each_report_id(tmp_path):
    """Different reports need independent retry keys even for equal payloads."""

    state = RemoteClientState(tmp_path / "remote.sqlite3")

    first = state.get_or_create_report_turn("report-1", "digest-1")
    second = state.get_or_create_report_turn("report-2", "digest-1")

    assert first != second


def test_report_turn_is_stable_after_reopening_the_state_store(tmp_path):
    """A process restart must keep the same retry-safe initial report turn."""

    path = tmp_path / "remote.sqlite3"
    first = RemoteClientState(path).get_or_create_report_turn("report-1", "digest-1")

    second = RemoteClientState(path).get_or_create_report_turn("report-1", "digest-1")

    assert second == first


def test_reply_turn_is_stable_for_parent_and_exact_user_text(tmp_path):
    """Retrying one reply must reuse its turn while the same question is pending."""

    state = RemoteClientState(tmp_path / "remote.sqlite3")
    state.replace_pending("report-1", "parent-turn", ["Ship Friday?"])

    first = state.get_or_create_reply_turn("report-1", "parent-turn", "reply-digest")
    second = state.get_or_create_reply_turn("report-1", "parent-turn", "reply-digest")

    assert first == second
    assert_uuid(first)


def test_changed_reply_text_digest_gets_a_new_turn_without_replacing_pending(tmp_path):
    """A corrected reply is distinct but cannot alter the Captain question it answers."""

    state = RemoteClientState(tmp_path / "remote.sqlite3")
    state.replace_pending("report-1", "parent-turn", ["Ship Friday?"])

    first = state.get_or_create_reply_turn("report-1", "parent-turn", "digest-yes")
    second = state.get_or_create_reply_turn("report-1", "parent-turn", "digest-no")

    assert first != second
    assert state.get_pending("report-1") == PendingCaptainQuestions(
        "report-1", "parent-turn", ("Ship Friday?",)
    )


def test_reply_for_stale_parent_fails_without_creating_a_turn(tmp_path):
    """A prior Captain question cannot accept a reply after a newer question replaces it."""

    path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(path)
    state.replace_pending("report-1", "old-parent", ["Old question?"])
    state.replace_pending("report-1", "new-parent", ["New question?"])

    with pytest.raises(RemoteStateConflict):
        state.get_or_create_reply_turn("report-1", "old-parent", "reply-digest")

    with sqlite3.connect(path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM remote_turns WHERE turn_kind = 'reply'"
        ).fetchone()[0]
    assert count == 0


def test_pending_replacement_is_durable_and_questions_are_immutable(tmp_path):
    """The current question context survives reopen and exposes no mutable list."""

    path = tmp_path / "remote.sqlite3"
    RemoteClientState(path).replace_pending(
        "report-1", "parent-1", ("First?", "Second?")
    )
    state = RemoteClientState(path)

    pending = state.get_pending("report-1")

    assert pending == PendingCaptainQuestions(
        "report-1", "parent-1", ("First?", "Second?"),
    )
    assert isinstance(pending.questions, tuple)
    with pytest.raises(AttributeError):
        pending.questions.append("Third?")
    with pytest.raises(FrozenInstanceError):
        pending.parent_turn_id = "another-parent"


def test_pending_question_value_validates_and_freezes_direct_construction():
    """The public pending value cannot be forged with empty or mutable questions."""

    pending = PendingCaptainQuestions("report-1", "parent-turn", ["Exact question?"])

    assert pending.questions == ("Exact question?",)
    with pytest.raises(ValueError):
        PendingCaptainQuestions("report-1", "parent-turn", ())


def test_pending_replacement_atomically_changes_parent_and_questions(tmp_path):
    """A new Captain question context replaces both fields as one current state."""

    state = RemoteClientState(tmp_path / "remote.sqlite3")
    state.replace_pending("report-1", "parent-1", ["First?"])
    state.replace_pending("report-1", "parent-2", ["Second?"])

    assert state.get_pending("report-1") == PendingCaptainQuestions(
        "report-1", "parent-2", ("Second?",)
    )


def test_clearing_pending_is_report_scoped_and_idempotent(tmp_path):
    """Finishing one report cannot erase another report's unanswered question."""

    state = RemoteClientState(tmp_path / "remote.sqlite3")
    state.replace_pending("report-1", "parent-1", ["Question one?"])
    state.replace_pending("report-2", "parent-2", ["Question two?"])

    state.clear_pending("report-1")
    state.clear_pending("report-1")

    assert state.get_pending("report-1") is None
    assert state.get_pending("report-2") == PendingCaptainQuestions(
        "report-2", "parent-2", ("Question two?",)
    )


@pytest.mark.parametrize(
    "questions",
    [
        [],
        (),
        [""],
        ["   "],
        ["ok", 3],
        ["x" * 1_001],
        [f"question-{index}" for index in range(21)],
        "not-a-question-list",
    ],
)
def test_pending_requires_a_nonempty_bounded_sequence_of_strings(tmp_path, questions):
    """Invalid question context must not be persisted for a later continuation."""

    state = RemoteClientState(tmp_path / "remote.sqlite3")

    with pytest.raises(ValueError):
        state.replace_pending("report-1", "parent-turn", questions)

    assert state.get_pending("report-1") is None


def test_get_pending_ignores_malformed_persisted_json(tmp_path):
    """A corrupt local question row must fail closed rather than crash or guess context."""

    path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(path)
    state.replace_pending("report-1", "parent-turn", ["Question?"])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE pending_questions SET questions_json = ? WHERE report_id = ?",
            ("{not-json", "report-1"),
        )

    assert state.get_pending("report-1") is None
    with pytest.raises(RemoteStateConflict):
        state.get_or_create_reply_turn("report-1", "parent-turn", "reply-digest")


def test_concurrent_report_insert_returns_one_winning_turn(tmp_path):
    """Separate clients racing the first retry key must converge on one UUID."""

    path = tmp_path / "remote.sqlite3"
    barrier = threading.Barrier(8)
    results = []
    errors = []
    lock = threading.Lock()

    def create_turn():
        try:
            barrier.wait()
            turn_id = RemoteClientState(path).get_or_create_report_turn(
                "report-1", "digest-1"
            )
            with lock:
                results.append(turn_id)
        except Exception as error:  # pragma: no cover - asserted below
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=create_turn) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(set(results)) == 1
    with sqlite3.connect(path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM remote_turns WHERE report_id = ?",
            ("report-1",),
        ).fetchone()[0]
    assert count == 1


def test_remote_state_path_honors_exact_override_then_xdg_default(monkeypatch, tmp_path):
    """Remote state follows the report-store path convention without sharing its file."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert remote_state_path({"CAPTAIN_REMOTE_STATE_PATH": "  /tmp/exact.sqlite3  "}) == Path(
        "/tmp/exact.sqlite3"
    )
    assert remote_state_path({"XDG_STATE_HOME": "~/state-home"}) == (
        tmp_path / "home" / "state-home" / "captain-agent" / "remote.sqlite3"
    )
    assert remote_state_path({}) == (
        tmp_path / "home" / ".local" / "state" / "captain-agent" / "remote.sqlite3"
    )


def test_state_directory_and_database_are_owner_only(tmp_path):
    """Saved remote context must not be readable by other local accounts."""

    path = tmp_path / "private" / "remote.sqlite3"
    RemoteClientState(path).get_or_create_report_turn("report-1", "digest-1")

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_state_initialization_preserves_an_existing_custom_parent_mode(tmp_path):
    """An explicit state path must not change a shared parent's access mode."""

    path = tmp_path / "private" / "remote.sqlite3"
    path.parent.mkdir(mode=0o755, parents=True)
    path.touch(mode=0o644)
    path.parent.chmod(0o755)
    path.chmod(0o644)

    RemoteClientState(path, env={"CAPTAIN_REMOTE_STATE_PATH": str(path)})

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_relative_state_path_preserves_the_current_directory_mode(monkeypatch, tmp_path):
    """A relative override must not re-mode the working directory or its shared parent."""

    shared_parent = tmp_path / "shared"
    shared_parent.mkdir(mode=0o755)
    shared_parent.chmod(0o755)
    monkeypatch.chdir(shared_parent)

    state = RemoteClientState(Path("remote.sqlite3"))
    state.get_or_create_report_turn("report-1", "digest-1")

    assert stat.S_IMODE(shared_parent.stat().st_mode) == 0o755
    assert stat.S_IMODE((shared_parent / "remote.sqlite3").stat().st_mode) == 0o600


def test_default_dedicated_parent_is_remoded_when_reopened(monkeypatch, tmp_path):
    """The normal Captain state directory remains owner-only across process reopen."""

    state_home = tmp_path / "state-home"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.delenv("CAPTAIN_REMOTE_STATE_PATH", raising=False)
    path = remote_state_path({"XDG_STATE_HOME": str(state_home)})
    path.parent.mkdir(mode=0o755, parents=True)
    path.parent.chmod(0o755)
    path.touch(mode=0o644)
    path.chmod(0o644)

    RemoteClientState(path)
    RemoteClientState(path)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_symlinked_state_parent_is_rejected_without_touching_its_target(tmp_path):
    """A database under a symlinked parent cannot redirect local state elsewhere."""

    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    parent_link = tmp_path / "state-link"
    parent_link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        RemoteClientState(parent_link / "remote.sqlite3")

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_symlinked_state_database_is_rejected_without_touching_its_target(tmp_path):
    """A database symlink cannot redirect the store or re-mode another file."""

    parent = tmp_path / "state"
    parent.mkdir()
    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o644)
    target.chmod(0o644)
    database_link = parent / "remote.sqlite3"
    database_link.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        RemoteClientState(database_link)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.parametrize("bad_turn_id", ["not-a-uuid", sqlite3.Binary(b"not-a-uuid")])
def test_corrupt_initial_turn_id_fails_closed(tmp_path, bad_turn_id):
    """A persisted initial row cannot return malformed or non-text turn IDs."""

    path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(path)
    state.get_or_create_report_turn("report-1", "digest-1")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE remote_turns SET turn_id = ? WHERE report_id = ?",
            (bad_turn_id, "report-1"),
        )

    with pytest.raises(RemoteStateConflict):
        state.get_or_create_report_turn("report-1", "digest-1")


@pytest.mark.parametrize("turn_kind", ["report", "reply"])
def test_blob_report_id_candidate_fails_closed_without_a_second_turn(tmp_path, turn_kind):
    """A non-text report ID must not hide a corrupt initial or reply retry row."""

    path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(path)
    if turn_kind == "report":
        state.get_or_create_report_turn("report-1", "digest-1")
    else:
        state.replace_pending("report-1", "parent-turn", ["Question?"])
        state.get_or_create_reply_turn("report-1", "parent-turn", "reply-digest")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE remote_turns SET report_id = ? WHERE turn_kind = ?",
            (sqlite3.Binary(b"report-1"), turn_kind),
        )

    with pytest.raises(RemoteStateConflict):
        if turn_kind == "report":
            state.get_or_create_report_turn("report-1", "digest-1")
        else:
            state.get_or_create_reply_turn("report-1", "parent-turn", "reply-digest")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM remote_turns WHERE turn_kind = ?", (turn_kind,)
        ).fetchone()[0] == 1


def test_corrupt_initial_parent_fails_closed_without_a_second_turn(tmp_path):
    """An initial report row with a parent is corrupt, not a new report reservation."""

    path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(path)
    state.get_or_create_report_turn("report-1", "digest-1")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE remote_turns SET parent_turn_id = ? WHERE report_id = ?",
            ("unexpected-parent", "report-1"),
        )

    with pytest.raises(RemoteStateConflict):
        state.get_or_create_report_turn("report-1", "digest-1")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM remote_turns WHERE report_id = ?", ("report-1",)
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "column,bad_value",
    [
        ("turn_id", "not-a-uuid"),
        ("turn_id", sqlite3.Binary(b"not-a-uuid")),
        ("payload_digest", sqlite3.Binary(b"reply-digest")),
        ("parent_turn_id", sqlite3.Binary(b"parent-turn")),
    ],
)
def test_corrupt_reply_row_fails_closed_instead_of_creating_around_it(
    tmp_path, column, bad_value
):
    """A malformed reply row cannot be bypassed by inserting a second retry turn."""

    path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(path)
    state.replace_pending("report-1", "parent-turn", ["Question?"])
    state.get_or_create_reply_turn("report-1", "parent-turn", "reply-digest")
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"UPDATE remote_turns SET {column} = ? WHERE turn_kind = 'reply'",
            (bad_value,),
        )

    with pytest.raises(RemoteStateConflict):
        state.get_or_create_reply_turn("report-1", "parent-turn", "reply-digest")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM remote_turns WHERE turn_kind = 'reply'"
        ).fetchone()[0] == 1


def test_database_contains_only_allowed_remote_state_fields(tmp_path):
    """The durable database must exclude credentials, URLs, report bodies, and reply text."""

    path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(path)
    state.get_or_create_report_turn("report-1", "report-digest")
    state.replace_pending("report-1", "parent-turn", ["What should ship?"])
    state.get_or_create_reply_turn("report-1", "parent-turn", "reply-digest")

    with sqlite3.connect(path) as connection:
        schema = "\n".join(
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ).lower()
        stored_text = "\n".join(
            row[0]
            for row in connection.execute(
                "SELECT turn_id FROM remote_turns UNION ALL SELECT questions_json FROM pending_questions"
            )
        )

    for forbidden in ("token", "url", "body", "metadata", "reply", "email", "identity"):
        assert forbidden not in schema
    for forbidden in ("member-token", "https://captain.example", "report body", "the user reply"):
        assert forbidden not in stored_text
    assert json.dumps(["What should ship?"]) in stored_text


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, None),
        ({"CAPTAIN_REMOTE_URL": "  ", "CAPTAIN_MEMBER_TOKEN": "\t"}, None),
    ],
)
def test_remote_config_requires_neither_or_both_nonblank_values(env, expected):
    """Blank remote variables must preserve the established local selection."""

    assert read_remote_config(env) is expected


@pytest.mark.parametrize(
    "env",
    [
        {"CAPTAIN_REMOTE_URL": "https://captain.example"},
        {"CAPTAIN_MEMBER_TOKEN": "member-token"},
        {"CAPTAIN_REMOTE_URL": "https://captain.example", "CAPTAIN_MEMBER_TOKEN": " "},
    ],
)
def test_partial_remote_config_is_rejected(env):
    """One remote setting must not silently fall back to local execution."""

    with pytest.raises(RemoteConfigurationError):
        read_remote_config(env)


@pytest.mark.parametrize(
    "url",
    [
        "http://captain.example",
        "https://captain.example/path",
        "https://captain.example//",
        "https://captain.example?member=alice",
        "https://captain.example#fragment",
        "https://alice:secret@captain.example",
        "https://captain.example:",
        "ftp://captain.example",
        "https://captain.example:invalid",
    ],
)
def test_remote_config_rejects_non_origin_or_insecure_urls(url):
    """A configured base can name only one safe HTTPS origin."""

    with pytest.raises(RemoteConfigurationError) as raised:
        read_remote_config(
            {"CAPTAIN_REMOTE_URL": url, "CAPTAIN_MEMBER_TOKEN": "member-token"}
        )
    assert "member-token" not in str(raised.value)
    assert "alice" not in str(raised.value)
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8787",
        "http://[::1]:8787/",
        "http://localhost:8787",
        "https://captain.example/",
    ],
)
def test_remote_config_accepts_https_and_exact_http_loopback(url):
    """Development HTTP is limited to explicit loopback host names."""

    config = read_remote_config(
        {"CAPTAIN_REMOTE_URL": url, "CAPTAIN_MEMBER_TOKEN": "member-token"}
    )
    assert config.base_url == url.rstrip("/")
    assert "member-token" not in repr(config)


@pytest.mark.parametrize("token", ["line\nbreak", "member-☃"])
def test_remote_config_rejects_header_unsafe_tokens_without_reflection(token):
    """Invalid credentials must fail before urllib constructs a header."""

    with pytest.raises(RemoteConfigurationError) as raised:
        read_remote_config(
            {"CAPTAIN_REMOTE_URL": "https://captain.example", "CAPTAIN_MEMBER_TOKEN": token}
        )
    assert token not in str(raised.value)


def test_submit_uses_exact_path_body_and_one_private_authorization_header():
    """The wire request must carry the token once in a header, never in its URL."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    payload = {
        "turn_id": turn_id,
        "kind": "report",
        "report": VALID_REPORT,
        "metadata": {"client": "pytest"},
    }
    with scripted_server([{"body": envelope(turn_id)}]) as (url, requests, script):
        result = remote_client(url).submit_and_poll("report-1", turn_id, payload)

    assert result.status == "updated"
    assert script == []
    assert len(requests) == 1
    request = requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/captain/v1/reports/report-1/turns"
    assert request["headers"].get_all("Authorization") == ["Bearer member-token"]
    assert request["headers"]["Content-Type"] == "application/json"
    assert json.loads(request["body"]) == payload
    assert b"member-token" not in request["body"]
    assert "member-token" not in request["path"]


@pytest.mark.parametrize("report_id", ["report/1", "", "x" * 129])
def test_invalid_report_id_is_rejected_before_network(report_id):
    """Untrusted report IDs must never become URL path segments."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    with scripted_server([]) as (url, requests, _script):
        result = remote_client(url).submit_and_poll(
            report_id,
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )
    assert result.status == "failed"
    assert requests == []


@pytest.mark.parametrize(
    "turn_id",
    [
        "not-a-uuid",
        "b73db2fe-ec74-1f44-a74c-fbe44eb11e46",
        "B73DB2FE-EC74-4F44-A74C-FBE44EB11E46",
    ],
)
def test_invalid_or_noncanonical_uuid4_turn_is_rejected_before_network(turn_id):
    """Only canonical UUIDv4 values may become remote paths."""

    with scripted_server([]) as (url, requests, _script):
        result = remote_client(url).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )
    assert result.status == "failed"
    assert requests == []


def test_oversized_request_is_rejected_before_network():
    """The serialized body limit must be enforced before a socket is opened."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    payload = {"turn_id": turn_id, "kind": "reply", "reply": "x" * MAX_REMOTE_BODY_BYTES}
    with scripted_server([]) as (url, requests, _script):
        result = remote_client(url).submit_and_poll("report-1", turn_id, payload)
    assert result.status == "failed"
    assert requests == []


def test_submit_disconnect_is_unknown_and_never_reposted():
    """A lost submit response is ambiguous because the server may have queued it."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    with scripted_server([{"disconnect": True}]) as (url, requests, script):
        result = remote_client(url, token="top-secret-token").submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )

    assert result.status == "unknown_outcome"
    assert len(requests) == 1
    assert script == []
    serialized = result.model_dump_json()
    assert "top-secret-token" not in serialized
    assert url not in serialized


def test_redirect_is_rejected_without_forwarding_credentials():
    """A redirect must not forward the member credential to another request."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    with scripted_server(
        [{"status": 307, "headers": {"Location": "http://127.0.0.1:1/stolen"}}]
    ) as (url, requests, _script):
        result = remote_client(url).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )
    assert result.status == "unknown_outcome"
    assert len(requests) == 1


def test_poll_uses_initial_delay_bounded_backoff_and_retry_after():
    """Polling must remain patient under queued work and server rate limits."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    clock = FakeClock()
    responses = [
        {"status": 202, "body": envelope(turn_id, "queued")},
        {"status": 429, "headers": {"Retry-After": "3"}},
        {"body": envelope(turn_id, "started")},
        {"body": envelope(turn_id, "queued")},
        {"body": envelope(turn_id, "started")},
        {"body": envelope(turn_id)},
    ]
    with scripted_server(responses) as (url, requests, script):
        result = remote_client(url, clock).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )

    assert result.status == "updated"
    assert script == []
    assert clock.sleeps == [2, 3, 4, 8, 10]
    assert [request["method"] for request in requests] == ["POST", "GET", "GET", "GET", "GET", "GET"]
    expected_poll_path = f"/captain/v1/reports/report-1/turns/{turn_id}"
    assert {request["path"] for request in requests[1:]} == {expected_poll_path}


@pytest.mark.parametrize("retry_after", ["garbage", "-1", "1.5", "9" * 5_000])
def test_malformed_or_negative_retry_after_fails_safely(retry_after):
    """Invalid rate-limit timing must not create a tight poll loop."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    clock = FakeClock()
    with scripted_server(
        [
            {"status": 202, "body": envelope(turn_id, "queued")},
            {"status": 429, "headers": {"Retry-After": retry_after}},
        ]
    ) as (url, requests, _script):
        result = remote_client(url, clock).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )
    assert result.status == "unknown_outcome"
    assert len(requests) == 2
    assert clock.sleeps == [2]


def test_retry_after_that_crosses_deadline_returns_queued_without_extra_poll():
    """An excessive valid delay must stop at the deadline instead of oversleeping."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    clock = FakeClock()
    with scripted_server(
        [
            {"status": 202, "body": envelope(turn_id, "queued")},
            {"status": 429, "headers": {"Retry-After": "400"}},
        ]
    ) as (url, requests, _script):
        result = remote_client(url, clock).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )
    assert result.status == "queued"
    assert len(requests) == 2
    assert clock.sleeps == [2]


def test_poll_deadline_returns_queued_without_resubmitting():
    """A client deadline preserves the durable turn for same-ID re-entry."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    clock = FakeClock()
    responses = [{"status": 202, "body": envelope(turn_id, "queued")}] + [
        {"body": envelope(turn_id, "started")} for _ in range(34)
    ]
    with scripted_server(responses) as (url, requests, _script):
        result = remote_client(url, clock).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )
    assert result.status == "queued"
    assert [request["method"] for request in requests].count("POST") == 1
    assert clock.now <= 330
    assert max(clock.sleeps) == 10


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "needs_configuration"), (409, "failed"), (413, "failed"), (400, "failed"), (500, "unknown_outcome")],
)
def test_submit_http_statuses_map_without_reflecting_remote_body(status, expected):
    """Public errors distinguish safe rejection from ambiguous server receipt."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    with scripted_server([{"status": status, "body": "remote-secret-body"}]) as (url, _requests, _script):
        result = remote_client(url).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )
    assert result.status == expected
    assert "remote-secret-body" not in result.model_dump_json()


@pytest.mark.parametrize(
    "bad_envelope",
    [
        {"report_id": "report-2", "turn_id": "b73db2fe-ec74-4f44-a74c-fbe44eb11e46", "turn_status": "queued"},
        {"report_id": "report-1", "turn_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "turn_status": "queued"},
        {"report_id": "report-1", "turn_id": "b73db2fe-ec74-4f44-a74c-fbe44eb11e46", "turn_status": "invented"},
        {"report_id": "report-1", "turn_id": "b73db2fe-ec74-4f44-a74c-fbe44eb11e46", "turn_status": []},
        {"report_id": "report-1", "turn_id": "b73db2fe-ec74-4f44-a74c-fbe44eb11e46", "turn_status": "succeeded", "result": {**TERMINAL_RESULT, "status": []}},
        {"report_id": "report-1", "turn_id": "b73db2fe-ec74-4f44-a74c-fbe44eb11e46", "result": TERMINAL_RESULT},
        {"report_id": "report-1", "turn_id": "b73db2fe-ec74-4f44-a74c-fbe44eb11e46", "turn_status": "failed", "result": TERMINAL_RESULT},
        ["not", "an", "object"],
    ],
)
def test_mismatched_or_malformed_envelope_is_unknown_without_polling(bad_envelope):
    """The client must never follow identifiers supplied by an invalid envelope."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    with scripted_server([{"status": 202, "body": bad_envelope}]) as (url, requests, _script):
        result = remote_client(url).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )
    assert result.status == "unknown_outcome"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response_body",
    [
        b"{not-json",
        b"x" * (MAX_REMOTE_BODY_BYTES + 1),
        (
            b'{"report_id":"report-2","report_id":"report-1",'
            b'"turn_id":"b73db2fe-ec74-4f44-a74c-fbe44eb11e46",'
            b'"turn_status":"succeeded","result":'
            + json.dumps(TERMINAL_RESULT).encode("utf-8")
            + b"}"
        ),
    ],
)
def test_malformed_or_oversized_response_is_unknown_and_not_reflected(response_body):
    """Remote response parsing must stop at a small fixed memory boundary."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    with scripted_server([{"body": response_body}]) as (url, _requests, _script):
        result = remote_client(url).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )
    assert result.status == "unknown_outcome"
    assert "not-json" not in result.model_dump_json()


def test_local_mode_delegates_to_existing_handler_with_exact_arguments(monkeypatch):
    """No remote variables must preserve the current local call boundary unchanged."""

    sentinel = CaptainReportResult(
        report_id="report-1",
        status="updated",
        captain_feedback="local",
    )
    calls = []

    def local(report_id, report, metadata, *, env):
        calls.append((report_id, report, metadata, env))
        return sentinel

    monkeypatch.setattr("captain_agent.dispatch.handle_session_report", local)
    environment = {"CAPTAIN_REMOTE_URL": " ", "CAPTAIN_MEMBER_TOKEN": ""}
    result = handle_captain_turn("report-1", VALID_REPORT, {"client": "pytest"}, env=environment)
    assert result is sentinel
    assert calls == [("report-1", VALID_REPORT, {"client": "pytest"}, environment)]


def test_partial_remote_config_never_calls_local_or_network(monkeypatch):
    """A half-configured client must fail closed before choosing either transport."""

    monkeypatch.setattr(
        "captain_agent.dispatch.handle_session_report",
        lambda *_args, **_kwargs: pytest.fail("local transport was selected"),
    )
    result = handle_captain_turn(
        "report-1",
        VALID_REPORT,
        {},
        env={"CAPTAIN_REMOTE_URL": "https://captain.example"},
    )
    assert result.status == "needs_configuration"


def test_invalid_remote_report_does_not_create_continuation_state(tmp_path):
    """Report validation must finish before any durable or network side effect."""

    state_path = tmp_path / "remote.sqlite3"
    result = handle_captain_turn(
        "report-1",
        {"summary": []},
        {},
        env={
            "CAPTAIN_REMOTE_URL": "http://127.0.0.1:1",
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        },
    )
    assert result.status == "needs_clarification"
    assert not state_path.exists()


def test_remote_report_uses_stable_turn_after_lost_submit_response(tmp_path):
    """Re-entry after ambiguity must POST the durable idempotency key again."""

    state_path = tmp_path / "remote.sqlite3"
    with scripted_server([{"disconnect": True}]) as (url, first_requests, _script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        first = handle_captain_turn("report-1", VALID_REPORT, {"client": "pytest"}, env=env)
    first_turn = json.loads(first_requests[0]["body"])["turn_id"]
    assert first.status == "unknown_outcome"

    with scripted_server([{"body": envelope(first_turn)}]) as (url, second_requests, _script):
        env["CAPTAIN_REMOTE_URL"] = url
        second = handle_captain_turn("report-1", VALID_REPORT, {"client": "pytest"}, env=env)
    assert second.status == "updated"
    assert json.loads(second_requests[0]["body"])["turn_id"] == first_turn


def test_remote_report_updates_pending_only_from_terminal_response(tmp_path):
    """A terminal Captain question becomes the exact parent for a later reply."""

    state_path = tmp_path / "remote.sqlite3"
    turn_id = None
    question_result = {**TERMINAL_RESULT, "status": "needs_clarification", "questions": ["Ship Friday?"]}
    with scripted_server([]) as (url, requests, script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        state = RemoteClientState(state_path, env=env)
        digest = hashlib.sha256(
            json.dumps(
                {"metadata": {"client": "pytest"}, "report": VALID_REPORT},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        turn_id = state.get_or_create_report_turn("report-1", digest)
        script.append({"body": envelope(turn_id, result=question_result)})
        result = handle_captain_turn("report-1", VALID_REPORT, {"client": "pytest"}, env=env)
    assert result.questions == ["Ship Friday?"]
    assert RemoteClientState(state_path, env=env).get_pending("report-1").parent_turn_id == turn_id


def test_remote_reply_preserves_exact_text_and_replaces_pending(tmp_path):
    """A reply body contains only the exact user text and its stable generated turn."""

    state_path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(state_path)
    state.replace_pending("report-1", "parent-turn", ["Ship Friday?"])
    reply = "  Yes, Friday is correct.\n"
    replacement = {**TERMINAL_RESULT, "status": "needs_clarification", "questions": ["What time Friday?"]}

    with scripted_server([]) as (url, requests, script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        reply_digest = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        reply_turn = state.get_or_create_reply_turn("report-1", "parent-turn", reply_digest)
        script.append({"body": envelope(reply_turn, result=replacement)})
        result = handle_captain_turn("report-1", reply=reply, env=env)

    assert result.questions == ["What time Friday?"]
    assert json.loads(requests[0]["body"]) == {
        "turn_id": reply_turn,
        "kind": "reply",
        "reply": reply,
    }
    pending = RemoteClientState(state_path, env=env).get_pending("report-1")
    assert pending.parent_turn_id == reply_turn
    assert pending.questions == ("What time Friday?",)


def test_remote_terminal_reply_without_questions_clears_pending(tmp_path):
    """A completed continuation removes only its now-answered question context."""

    state_path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(state_path)
    state.replace_pending("report-1", "parent-turn", ["Ship Friday?"])
    reply = "Yes"
    reply_digest = hashlib.sha256(reply.encode("utf-8")).hexdigest()
    reply_turn = state.get_or_create_reply_turn("report-1", "parent-turn", reply_digest)
    with scripted_server([{"body": envelope(reply_turn)}]) as (url, _requests, _script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        result = handle_captain_turn("report-1", reply=reply, env=env)
    assert result.status == "updated"
    assert RemoteClientState(state_path, env=env).get_pending("report-1") is None


def test_queued_reply_keeps_pending_context(tmp_path):
    """A client poll deadline cannot erase the question needed for re-entry."""

    state_path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(state_path)
    state.replace_pending("report-1", "parent-turn", ["Ship Friday?"])
    env = {
        "CAPTAIN_REMOTE_URL": "http://127.0.0.1:1",
        "CAPTAIN_MEMBER_TOKEN": "member-token",
        "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
    }
    monkeypatch_result = CaptainReportResult(
        report_id="report-1", status="queued", captain_feedback="queued"
    )
    original = RemoteCaptainClient.submit_and_poll
    try:
        RemoteCaptainClient.submit_and_poll = lambda *_args, **_kwargs: monkeypatch_result
        result = handle_captain_turn("report-1", reply="Yes", env=env)
    finally:
        RemoteCaptainClient.submit_and_poll = original
    assert result.status == "queued"
    assert RemoteClientState(state_path, env=env).get_pending("report-1").questions == ("Ship Friday?",)


def test_terminal_envelope_with_queued_public_result_keeps_pending(tmp_path):
    """A public queued result is not completion evidence for continuation state."""

    state_path = tmp_path / "remote.sqlite3"
    state = RemoteClientState(state_path)
    state.replace_pending("report-1", "parent-turn", ["Ship Friday?"])
    reply = "Yes"
    reply_digest = hashlib.sha256(reply.encode("utf-8")).hexdigest()
    reply_turn = state.get_or_create_reply_turn("report-1", "parent-turn", reply_digest)
    queued_result = {**TERMINAL_RESULT, "status": "queued"}
    with scripted_server([{"body": envelope(reply_turn, result=queued_result)}]) as (url, _requests, _script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        result = handle_captain_turn("report-1", reply=reply, env=env)
    assert result.status == "queued"
    assert RemoteClientState(state_path, env=env).get_pending("report-1").questions == ("Ship Friday?",)


def test_reply_without_pending_and_mixed_report_reply_fail_before_io(tmp_path):
    """Continuation dispatch must have exactly one valid input and a current parent."""

    with scripted_server([]) as (url, requests, _script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(tmp_path / "remote.sqlite3"),
        }
        missing = handle_captain_turn("report-1", reply="Yes", env=env)
        mixed = handle_captain_turn("report-1", VALID_REPORT, {}, "Yes", env=env)
    assert missing.status == "needs_clarification"
    assert mixed.status == "failed"
    assert requests == []


def test_local_reply_requires_remote_mode_without_calling_local(monkeypatch):
    """Local report replay remains report-only until a remote continuation exists."""

    monkeypatch.setattr(
        "captain_agent.dispatch.handle_session_report",
        lambda *_args, **_kwargs: pytest.fail("local report handler was called"),
    )
    result = handle_captain_turn("report-1", reply="Yes", env={})
    assert result.status == "needs_configuration"
