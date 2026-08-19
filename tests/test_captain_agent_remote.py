"""Exercise durable state, transport, and dispatch for remote Captain turns."""

import hashlib
import io
import json
import socket
import sqlite3
import stat
import sys
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-plugin"))

from captain_agent.client_state import (
    PendingCaptainQuestions,
    RemoteClientState as _RemoteClientState,
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
    remote_profile_id,
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

TEST_PROFILE_ID = "a" * 64
MEMBER_ONE_LOOKUP = "abcdefghijklmnop"
MEMBER_TWO_LOOKUP = "qrstuvwxyzABCDEF"
MEMBER_ONE_TOKEN = f"cap_v1_{MEMBER_ONE_LOOKUP}.{'A' * 43}"
MEMBER_ONE_ROTATED_TOKEN = f"cap_v1_{MEMBER_ONE_LOOKUP}.{'B' * 43}"
MEMBER_TWO_TOKEN = f"cap_v1_{MEMBER_TWO_LOOKUP}.{'A' * 43}"


def RemoteClientState(path, *, env=None, profile_id=TEST_PROFILE_ID):
    """Open test state under one explicit non-secret remote profile."""

    return _RemoteClientState(path, profile_id=profile_id, env=env)


def remote_state_for_env(path, env):
    """Open state under the same derived profile that dispatch will use."""

    config = read_remote_config(env)
    assert config is not None
    return RemoteClientState(path, profile_id=remote_profile_id(config), env=env)


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


class AdvancingOpener:
    """Record socket timeouts while simulating bounded in-flight time."""

    def __init__(self, clock, responses):
        self.clock = clock
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        advance, status, body, headers = self.responses.pop(0)
        self.requests.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "body": request.data,
                "timeout": timeout,
            }
        )
        self.clock.now += advance
        message = Message()
        for name, value in headers.items():
            message.add_header(name, value)
        if status >= 300:
            raise HTTPError(
                request.full_url,
                status,
                "scripted error",
                message,
                io.BytesIO(b""),
            )

        raw = json.dumps(body).encode("utf-8")
        message["Content-Length"] = str(len(raw))

        class Response:
            def __init__(self):
                self.headers = message

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit):
                return raw[:limit]

        return Response()


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

        def do_CONNECT(self):
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
            if callable(response):
                response = response(requests[-1])
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


def test_remote_profile_id_is_one_way_stable_and_configuration_scoped():
    """Routine rotation preserves one member profile while origin/member changes isolate."""

    first = RemoteConfig("https://captain.example/", MEMBER_ONE_TOKEN)
    equivalent = RemoteConfig("https://captain.example", MEMBER_ONE_ROTATED_TOKEN)
    another_origin = RemoteConfig("https://other.example", MEMBER_ONE_TOKEN)
    another_member = RemoteConfig("https://captain.example", MEMBER_TWO_TOKEN)

    profile_id = remote_profile_id(first)

    assert profile_id == remote_profile_id(equivalent)
    assert profile_id != remote_profile_id(another_origin)
    assert profile_id != remote_profile_id(another_member)
    assert len(profile_id) == 64
    assert set(profile_id) <= set("0123456789abcdef")
    assert "captain.example" not in profile_id
    assert MEMBER_ONE_LOOKUP not in profile_id
    assert MEMBER_ONE_TOKEN not in profile_id


def test_turn_and_pending_context_continue_across_member_token_rotation(tmp_path):
    """A replacement secret for the same member must reopen the same durable profile."""

    path = tmp_path / "remote.sqlite3"
    first_config = RemoteConfig("https://captain.example", MEMBER_ONE_TOKEN)
    rotated_config = RemoteConfig(
        "https://captain.example",
        MEMBER_ONE_ROTATED_TOKEN,
    )
    first = RemoteClientState(path, profile_id=remote_profile_id(first_config))
    turn_id = first.get_or_create_report_turn("report-1", "digest-1")
    first.replace_pending("report-1", turn_id, ["Ship Friday?"])

    rotated = RemoteClientState(path, profile_id=remote_profile_id(rotated_config))

    assert rotated.get_or_create_report_turn("report-1", "digest-1") == turn_id
    assert rotated.get_pending("report-1") == PendingCaptainQuestions(
        "report-1",
        turn_id,
        ("Ship Friday?",),
    )


def test_remote_state_requires_an_explicit_profile_namespace(tmp_path):
    """No caller may silently reopen durable state as an unscoped client."""

    with pytest.raises(TypeError, match="profile_id"):
        _RemoteClientState(tmp_path / "remote.sqlite3")


def test_turns_and_pending_questions_are_namespaced_by_remote_profile(tmp_path):
    """Equal report IDs on different remotes must never replay or answer each other."""

    path = tmp_path / "remote.sqlite3"
    first = RemoteClientState(path, profile_id="a" * 64)
    second = RemoteClientState(path, profile_id="b" * 64)

    first_turn = first.get_or_create_report_turn("shared-report", "same-digest")
    second_turn = second.get_or_create_report_turn("shared-report", "same-digest")
    first.replace_pending("shared-report", first_turn, ["First Captain?"])
    second.replace_pending("shared-report", second_turn, ["Second Captain?"])

    assert first_turn != second_turn
    assert first.get_or_create_report_turn("shared-report", "same-digest") == first_turn
    assert second.get_or_create_report_turn("shared-report", "same-digest") == second_turn
    assert first.get_pending("shared-report").questions == ("First Captain?",)
    assert second.get_pending("shared-report").questions == ("Second Captain?",)

    first.clear_pending("shared-report")
    assert first.get_pending("shared-report") is None
    assert second.get_pending("shared-report").questions == ("Second Captain?",)


def test_existing_unscoped_state_is_migrated_but_never_claimed_by_a_profile(tmp_path):
    """A v1 database upgrade must quarantine legacy rows instead of guessing ownership."""

    path = tmp_path / "remote.sqlite3"
    legacy_turn = "00000000-0000-4000-8000-000000000001"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE remote_turns(
                report_id TEXT NOT NULL,
                turn_kind TEXT NOT NULL,
                parent_turn_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(report_id, turn_kind, parent_turn_id, payload_digest)
            );
            CREATE UNIQUE INDEX one_initial_remote_turn_per_report
                ON remote_turns(report_id)
                WHERE turn_kind = 'report' AND parent_turn_id = '';
            CREATE TABLE pending_questions(
                report_id TEXT PRIMARY KEY,
                parent_turn_id TEXT NOT NULL,
                questions_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO remote_turns VALUES (?, 'report', '', ?, ?, ?)",
            ("shared-report", "legacy-digest", legacy_turn, "2026-08-18T12:00:00Z"),
        )
        connection.execute(
            "INSERT INTO pending_questions VALUES (?, ?, ?, ?)",
            (
                "shared-report",
                legacy_turn,
                '["Legacy question?"]',
                "2026-08-18T12:00:01Z",
            ),
        )

    scoped = RemoteClientState(path, profile_id="c" * 64)

    assert scoped.get_pending("shared-report") is None
    scoped_turn = scoped.get_or_create_report_turn("shared-report", "current-digest")
    assert scoped_turn != legacy_turn
    assert scoped.get_or_create_report_turn("shared-report", "current-digest") == scoped_turn

    with sqlite3.connect(path) as connection:
        turn_rows = connection.execute(
            "SELECT profile_id, turn_id FROM remote_turns ORDER BY created_at"
        ).fetchall()
        pending_rows = connection.execute(
            "SELECT profile_id FROM pending_questions"
        ).fetchall()
    assert turn_rows == [("0" * 64, legacy_turn), ("c" * 64, scoped_turn)]
    assert pending_rows == [("0" * 64,)]


def test_profile_scoped_database_contains_no_raw_origin_or_credential(tmp_path):
    """Durable retry state may retain only the one-way remote profile identifier."""

    path = tmp_path / "remote.sqlite3"
    config = RemoteConfig("https://captain.example", MEMBER_ONE_TOKEN)
    profile_id = remote_profile_id(config)
    state = RemoteClientState(path, profile_id=profile_id)
    state.get_or_create_report_turn("report-1", "digest-1")

    persisted = path.read_bytes()
    assert profile_id.encode() in persisted
    assert b"captain.example" not in persisted
    assert MEMBER_ONE_LOOKUP.encode() not in persisted
    assert MEMBER_ONE_TOKEN.encode() not in persisted


def test_remote_state_path_honors_exact_override_then_xdg_default(monkeypatch, tmp_path):
    """Remote state follows the report-store path convention without sharing its file."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    exact = str(tmp_path / " remote state.sqlite3 ")
    assert remote_state_path({"CAPTAIN_REMOTE_STATE_PATH": exact}) == Path(exact)
    assert remote_state_path({"CAPTAIN_REMOTE_STATE_PATH": "   "}) == (
        tmp_path / "home" / ".local" / "state" / "captain-agent" / "remote.sqlite3"
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


def test_remote_client_ignores_ambient_http_and_https_proxies(monkeypatch):
    """Origin credentials must go directly to the configured target, never a proxy."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"

    def terminal_for_request(request):
        submitted_turn = json.loads(request["body"])["turn_id"]
        return {"body": envelope(submitted_turn)}

    with scripted_server([terminal_for_request]) as (target_url, target_requests, _):
        with scripted_server(
            [terminal_for_request, {"status": 502, "body": "proxy"}]
        ) as (proxy_url, proxy_requests, _proxy_script):
            for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
                monkeypatch.setenv(name, proxy_url)
            monkeypatch.setenv("no_proxy", "")
            monkeypatch.setenv("NO_PROXY", "")

            result = remote_client(target_url).submit_and_poll(
                "report-1",
                turn_id,
                {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
            )
            https_result = remote_client(
                target_url.replace("http://", "https://", 1)
            ).submit_and_poll(
                "report-1",
                turn_id,
                {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
            )

    assert result.status == "updated"
    assert https_result.status == "unknown_outcome"
    assert len(target_requests) == 1
    assert target_requests[0]["headers"].get_all("Authorization") == [
        "Bearer member-token"
    ]
    assert proxy_requests == []


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


@pytest.mark.parametrize("status", [409, 413])
def test_poll_conflict_and_size_rejection_are_definitive_failed(status):
    """A definitive poll rejection must not be upgraded to an ambiguous outcome."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    clock = FakeClock()
    with scripted_server(
        [
            {"status": 202, "body": envelope(turn_id, "queued")},
            {"status": status, "body": "remote-secret-body"},
        ]
    ) as (url, requests, _script):
        client = remote_client(url, clock)
        result = client.submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )

    assert result.status == "failed"
    assert "remote-secret-body" not in result.model_dump_json()
    assert [request["method"] for request in requests] == ["POST", "GET"]
    assert client.terminal_response is False


def test_in_flight_poll_timeout_uses_only_remaining_deadline_budget():
    """A poll socket timeout must shrink so no new request crosses the deadline."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    clock = FakeClock()
    client = remote_client("http://127.0.0.1:1", clock)
    opener = AdvancingOpener(
        clock,
        [
            (320, 202, envelope(turn_id, "queued"), {}),
            (8, 200, envelope(turn_id, "started"), {}),
        ],
    )
    client._opener = opener

    result = client.submit_and_poll(
        "report-1",
        turn_id,
        {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
    )

    assert result.status == "queued"
    assert [request["timeout"] for request in opener.requests] == [15, 8]
    assert clock.now == 330
    assert clock.sleeps == [2]


def test_submit_429_waits_then_reposts_the_identical_stable_turn():
    """An explicit not-accepted response may retry only the same idempotent POST."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    clock = FakeClock()
    with scripted_server(
        [
            {"status": 429, "headers": {"Retry-After": "3"}},
            {"body": envelope(turn_id)},
        ]
    ) as (url, requests, script):
        result = remote_client(url, clock).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Exact reply"},
        )

    assert result.status == "updated"
    assert script == []
    assert clock.sleeps == [3]
    assert [request["method"] for request in requests] == ["POST", "POST"]
    assert requests[0]["path"] == requests[1]["path"]
    assert requests[0]["body"] == requests[1]["body"]


def test_submit_429_deadline_expires_failed_without_sleep_or_repost():
    """A turn never accepted before its deadline is a definitive busy failure."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    clock = FakeClock()
    with scripted_server(
        [{"status": 429, "headers": {"Retry-After": "330"}}]
    ) as (url, requests, _script):
        result = remote_client(url, clock).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )

    assert result.status == "failed"
    assert len(requests) == 1
    assert clock.sleeps == []
    assert clock.now == 0


def test_submit_429_with_malformed_retry_after_fails_without_repost():
    """Malformed retry timing after explicit rejection must fail closed."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    clock = FakeClock()
    with scripted_server(
        [{"status": 429, "headers": {"Retry-After": "invalid"}}]
    ) as (url, requests, _script):
        result = remote_client(url, clock).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )

    assert result.status == "failed"
    assert len(requests) == 1
    assert clock.sleeps == []


def test_submit_429_then_disconnect_never_retries_the_ambiguous_post():
    """Only explicit rejection is retryable; a lost retry response remains ambiguous."""

    turn_id = "b73db2fe-ec74-4f44-a74c-fbe44eb11e46"
    clock = FakeClock()
    with scripted_server(
        [
            {"status": 429, "headers": {"Retry-After": "1"}},
            {"disconnect": True},
            {"body": envelope(turn_id)},
        ]
    ) as (url, requests, script):
        result = remote_client(url, clock).submit_and_poll(
            "report-1",
            turn_id,
            {"turn_id": turn_id, "kind": "reply", "reply": "Yes"},
        )

    assert result.status == "unknown_outcome"
    assert len(requests) == 2
    assert len(script) == 1
    assert clock.sleeps == [1]


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


def test_partially_migrated_remote_state_fails_closed_without_network(tmp_path):
    """A partial profile migration must return fixed configuration feedback."""

    state_path = tmp_path / "remote.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE remote_turns(
                profile_id TEXT NOT NULL,
                report_id TEXT NOT NULL,
                turn_kind TEXT NOT NULL,
                parent_turn_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    with scripted_server([]) as (url, requests, _script):
        result = handle_captain_turn(
            "report-1",
            VALID_REPORT,
            {},
            env={
                "CAPTAIN_REMOTE_URL": url,
                "CAPTAIN_MEMBER_TOKEN": "member-token",
                "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
            },
        )

    assert result.status == "needs_configuration"
    assert result.captain_feedback == (
        "Captain remote continuation state could not be opened."
    )
    assert requests == []


def test_oversized_remote_report_does_not_reserve_state_and_smaller_retry_proceeds(
    tmp_path,
):
    """The exact wire-size check must precede stable turn reservation."""

    state_path = tmp_path / "remote.sqlite3"

    def terminal_for_request(request):
        turn_id = json.loads(request["body"])["turn_id"]
        return {"body": envelope(turn_id)}

    with scripted_server([terminal_for_request]) as (url, requests, _script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        oversized = handle_captain_turn(
            "report-1",
            {"summary": ["x" * 300_000]},
            {},
            env=env,
        )
        assert oversized.status == "failed"
        assert not state_path.exists()
        assert requests == []

        corrected = handle_captain_turn(
            "report-1",
            {"summary": ["Corrected smaller report."]},
            {},
            env=env,
        )

    assert corrected.status == "updated"
    assert len(requests) == 1


def test_oversized_remote_reply_preserves_pending_without_reserving_turn(tmp_path):
    """An unsendable reply must not consume or conflict with pending context."""

    state_path = tmp_path / "remote.sqlite3"
    with scripted_server([]) as (url, requests, _script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        remote_state_for_env(state_path, env).replace_pending(
            "report-1", "parent-turn", ["Ship Friday?"]
        )
        result = handle_captain_turn(
            "report-1",
            reply="x" * 300_000,
            env=env,
        )

    assert result.status == "failed"
    assert requests == []
    assert remote_state_for_env(state_path, env).get_pending("report-1").questions == (
        "Ship Friday?",
    )
    with sqlite3.connect(state_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM remote_turns WHERE turn_kind = 'reply'"
        ).fetchone()[0] == 0


def test_submit_429_deadline_failure_preserves_pending_reply(tmp_path, monkeypatch):
    """A never-accepted reply must leave its current parent available for retry."""

    state_path = tmp_path / "remote.sqlite3"
    clock = FakeClock()
    original_init = RemoteCaptainClient.__init__

    def initialize(client, config):
        original_init(
            client,
            config,
            clock=clock.monotonic,
            wall_clock=clock.time,
            sleep=clock.sleep,
        )

    monkeypatch.setattr(RemoteCaptainClient, "__init__", initialize)
    with scripted_server(
        [{"status": 429, "headers": {"Retry-After": "330"}}]
    ) as (url, requests, _script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        remote_state_for_env(state_path, env).replace_pending(
            "report-1", "parent-turn", ["Ship Friday?"]
        )
        result = handle_captain_turn("report-1", reply="Yes", env=env)

    assert result.status == "failed"
    assert len(requests) == 1
    assert remote_state_for_env(state_path, env).get_pending("report-1").questions == (
        "Ship Friday?",
    )


def test_remote_report_uses_stable_turn_after_lost_submit_response(tmp_path):
    """Re-entry after ambiguity must POST the durable idempotency key again."""

    state_path = tmp_path / "remote.sqlite3"
    with scripted_server([{"disconnect": True}, None]) as (url, requests, script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        first = handle_captain_turn("report-1", VALID_REPORT, {"client": "pytest"}, env=env)
        first_turn = json.loads(requests[0]["body"])["turn_id"]
        assert first.status == "unknown_outcome"
        script[0] = {"body": envelope(first_turn)}
        second = handle_captain_turn("report-1", VALID_REPORT, {"client": "pytest"}, env=env)
    assert second.status == "updated"
    assert json.loads(requests[1]["body"])["turn_id"] == first_turn


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
        state = remote_state_for_env(state_path, env)
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
    assert remote_state_for_env(state_path, env).get_pending("report-1").parent_turn_id == turn_id


def test_remote_reply_preserves_exact_text_and_replaces_pending(tmp_path):
    """A reply body contains only the exact user text and its stable generated turn."""

    state_path = tmp_path / "remote.sqlite3"
    reply = "  Yes, Friday is correct.\n"
    replacement = {**TERMINAL_RESULT, "status": "needs_clarification", "questions": ["What time Friday?"]}

    with scripted_server([]) as (url, requests, script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        state = remote_state_for_env(state_path, env)
        state.replace_pending("report-1", "parent-turn", ["Ship Friday?"])
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
    pending = remote_state_for_env(state_path, env).get_pending("report-1")
    assert pending.parent_turn_id == reply_turn
    assert pending.questions == ("What time Friday?",)


def test_remote_terminal_reply_without_questions_clears_pending(tmp_path):
    """A completed continuation removes only its now-answered question context."""

    state_path = tmp_path / "remote.sqlite3"
    reply = "Yes"
    with scripted_server([]) as (url, _requests, script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        state = remote_state_for_env(state_path, env)
        state.replace_pending("report-1", "parent-turn", ["Ship Friday?"])
        reply_digest = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        reply_turn = state.get_or_create_reply_turn("report-1", "parent-turn", reply_digest)
        script.append({"body": envelope(reply_turn)})
        result = handle_captain_turn("report-1", reply=reply, env=env)
    assert result.status == "updated"
    assert remote_state_for_env(state_path, env).get_pending("report-1") is None


def test_queued_reply_keeps_pending_context(tmp_path):
    """A client poll deadline cannot erase the question needed for re-entry."""

    state_path = tmp_path / "remote.sqlite3"
    env = {
        "CAPTAIN_REMOTE_URL": "http://127.0.0.1:1",
        "CAPTAIN_MEMBER_TOKEN": "member-token",
        "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
    }
    state = remote_state_for_env(state_path, env)
    state.replace_pending("report-1", "parent-turn", ["Ship Friday?"])
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
    assert remote_state_for_env(state_path, env).get_pending("report-1").questions == ("Ship Friday?",)


def test_terminal_envelope_with_queued_public_result_keeps_pending(tmp_path):
    """A public queued result is not completion evidence for continuation state."""

    state_path = tmp_path / "remote.sqlite3"
    reply = "Yes"
    queued_result = {**TERMINAL_RESULT, "status": "queued"}
    with scripted_server([]) as (url, _requests, script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        state = remote_state_for_env(state_path, env)
        state.replace_pending("report-1", "parent-turn", ["Ship Friday?"])
        reply_digest = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        reply_turn = state.get_or_create_reply_turn("report-1", "parent-turn", reply_digest)
        script.append({"body": envelope(reply_turn, result=queued_result)})
        result = handle_captain_turn("report-1", reply=reply, env=env)
    assert result.status == "queued"
    assert remote_state_for_env(state_path, env).get_pending("report-1").questions == ("Ship Friday?",)


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


def test_switching_remote_credential_cannot_replay_or_answer_same_report(tmp_path):
    """A token change must create an independent turn and hide the prior pending reply."""

    state_path = tmp_path / "remote.sqlite3"
    with scripted_server([]) as (url, requests, script):
        first_env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token-one",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        second_env = {**first_env, "CAPTAIN_MEMBER_TOKEN": "member-token-two"}

        def terminal_with_question(request):
            turn_id = json.loads(request["body"])["turn_id"]
            return {
                "body": envelope(
                    turn_id,
                    result={
                        **TERMINAL_RESULT,
                        "status": "needs_clarification",
                        "questions": ["First profile question?"],
                    },
                )
            }

        def terminal_without_question(request):
            turn_id = json.loads(request["body"])["turn_id"]
            return {"body": envelope(turn_id)}

        script.extend([terminal_with_question, terminal_without_question])
        first = handle_captain_turn("report-1", VALID_REPORT, {}, env=first_env)
        second = handle_captain_turn("report-1", VALID_REPORT, {}, env=second_env)
        cross_profile_reply = handle_captain_turn(
            "report-1", reply="Yes", env=second_env
        )

    first_turn = json.loads(requests[0]["body"])["turn_id"]
    second_turn = json.loads(requests[1]["body"])["turn_id"]
    assert first.questions == ["First profile question?"]
    assert second.status == "updated"
    assert first_turn != second_turn
    assert cross_profile_reply.status == "needs_clarification"
    assert len(requests) == 2
    assert remote_state_for_env(state_path, first_env).get_pending("report-1") is not None
    assert remote_state_for_env(state_path, second_env).get_pending("report-1") is None


def test_local_cancellation_clears_only_the_applicable_profile_without_http(tmp_path):
    """A refusal clears local pending state without sending the refusal as a reply."""

    state_path = tmp_path / "remote.sqlite3"
    with scripted_server([]) as (url, requests, _script):
        first_env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token-one",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        second_env = {**first_env, "CAPTAIN_MEMBER_TOKEN": "member-token-two"}
        remote_state_for_env(state_path, first_env).replace_pending(
            "report-1", "first-parent", ["First question?"]
        )
        remote_state_for_env(state_path, second_env).replace_pending(
            "report-1", "second-parent", ["Second question?"]
        )

        result = handle_captain_turn(
            "report-1",
            env=first_env,
            cancel_pending=True,
        )

    assert result.status == "needs_clarification"
    assert "cleared locally" in result.captain_feedback
    assert requests == []
    assert remote_state_for_env(state_path, first_env).get_pending("report-1") is None
    assert remote_state_for_env(state_path, second_env).get_pending("report-1") is not None
    assert b"refusal" not in state_path.read_bytes()


def test_cancellation_rejects_mixed_content_without_clearing_or_sending(tmp_path):
    """Cancellation is a distinct local action, never a report or reply modifier."""

    state_path = tmp_path / "remote.sqlite3"
    with scripted_server([]) as (url, requests, _script):
        env = {
            "CAPTAIN_REMOTE_URL": url,
            "CAPTAIN_MEMBER_TOKEN": "member-token",
            "CAPTAIN_REMOTE_STATE_PATH": str(state_path),
        }
        remote_state_for_env(state_path, env).replace_pending(
            "report-1", "parent-turn", ["Question?"]
        )
        mixed = handle_captain_turn(
            "report-1",
            reply="Do not send that",
            env=env,
            cancel_pending=True,
        )

    assert mixed.status == "failed"
    assert requests == []
    assert remote_state_for_env(state_path, env).get_pending("report-1") is not None


def test_local_reply_requires_remote_mode_without_calling_local(monkeypatch):
    """Local report replay remains report-only until a remote continuation exists."""

    monkeypatch.setattr(
        "captain_agent.dispatch.handle_session_report",
        lambda *_args, **_kwargs: pytest.fail("local report handler was called"),
    )
    result = handle_captain_turn("report-1", reply="Yes", env={})
    assert result.status == "needs_configuration"


def test_local_cancellation_requires_complete_remote_profile(monkeypatch):
    """Without a remote profile there is no safe local namespace to clear."""

    monkeypatch.setattr(
        "captain_agent.dispatch.handle_session_report",
        lambda *_args, **_kwargs: pytest.fail("local report handler was called"),
    )
    result = handle_captain_turn("report-1", cancel_pending=True, env={})
    assert result.status == "needs_configuration"
