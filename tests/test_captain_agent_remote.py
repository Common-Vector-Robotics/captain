"""Exercise durable, privacy-limited remote Captain client state."""

import json
import sqlite3
import stat
import sys
import threading
from dataclasses import FrozenInstanceError
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
