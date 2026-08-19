"""Keep the minimum durable state needed to retry remote Captain turns safely."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


MAX_REPORT_ID_CHARACTERS = 128
MAX_TURN_ID_CHARACTERS = 128
MAX_DIGEST_CHARACTERS = 256
MAX_PENDING_QUESTIONS = 20
MAX_QUESTION_CHARACTERS = 1_000
SQLITE_BUSY_TIMEOUT_SECONDS = 1.0


class RemoteStateConflict(Exception):
    """Raised when a retry no longer matches the current remote state."""


@dataclass(frozen=True)
class PendingCaptainQuestions:
    """The exact Captain questions that a later user reply must answer."""

    report_id: str
    parent_turn_id: str
    questions: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject invalid context and make question strings immutable at the boundary."""

        object.__setattr__(
            self,
            "report_id",
            _require_text(self.report_id, "report_id", MAX_REPORT_ID_CHARACTERS),
        )
        object.__setattr__(
            self,
            "parent_turn_id",
            _require_text(
                self.parent_turn_id, "parent_turn_id", MAX_TURN_ID_CHARACTERS
            ),
        )
        object.__setattr__(self, "questions", _validated_questions(self.questions))


def remote_state_path(env: Mapping[str, str]) -> Path:
    """Choose the privacy-limited SQLite path for remote client state."""

    override = str(env.get("CAPTAIN_REMOTE_STATE_PATH", "")).strip()
    if override:
        return Path(override).expanduser()

    xdg_state_home = str(env.get("XDG_STATE_HOME", "")).strip()
    state_directory = (
        Path(xdg_state_home).expanduser()
        if xdg_state_home
        else Path.home() / ".local" / "state"
    )
    return state_directory / "captain-agent" / "remote.sqlite3"


def _current_utc_time() -> str:
    """Return a compact, sortable timestamp for SQLite rows."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_text(value: object, name: str, limit: int) -> str:
    """Return one bounded nonblank text value without silently coercing it."""

    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a nonblank string up to {limit} characters")
    return value


def _validated_questions(questions: Sequence[str]) -> tuple[str, ...]:
    """Return the exact valid question strings that continuation needs."""

    if not isinstance(questions, (list, tuple)):
        raise ValueError("questions must be a list or tuple of strings")
    if not questions or len(questions) > MAX_PENDING_QUESTIONS:
        raise ValueError(f"questions must contain 1 to {MAX_PENDING_QUESTIONS} items")

    return tuple(
        _require_text(question, "question", MAX_QUESTION_CHARACTERS)
        for question in questions
    )


class RemoteClientState:
    """Store turn IDs and the one pending Captain question context per report."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._initialize_store()

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived connection that waits only briefly for another writer."""

        return sqlite3.connect(
            self.path,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
        )

    def _initialize_store(self) -> None:
        """Create the owner-only state directory, database, and small schema."""

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS remote_turns(
                        report_id TEXT NOT NULL,
                        turn_kind TEXT NOT NULL,
                        parent_turn_id TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(report_id, turn_kind, parent_turn_id, payload_digest)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS one_initial_remote_turn_per_report
                    ON remote_turns(report_id)
                    WHERE turn_kind = 'report' AND parent_turn_id = ''
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_questions(
                        report_id TEXT PRIMARY KEY,
                        parent_turn_id TEXT NOT NULL,
                        questions_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        self.path.chmod(0o600)

    def _pending_in_transaction(
        self, connection: sqlite3.Connection, report_id: str
    ) -> PendingCaptainQuestions | None:
        """Read one pending row, treating malformed local data as unusable state."""

        row = connection.execute(
            """
            SELECT parent_turn_id, questions_json
            FROM pending_questions
            WHERE report_id = ?
            """,
            (report_id,),
        ).fetchone()
        if row is None:
            return None

        parent_turn_id, questions_json = row
        try:
            questions = _validated_questions(json.loads(questions_json))
            parent_turn_id = _require_text(
                parent_turn_id, "parent_turn_id", MAX_TURN_ID_CHARACTERS
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return PendingCaptainQuestions(report_id, parent_turn_id, questions)

    def _get_or_create_turn(
        self,
        report_id: str,
        turn_kind: str,
        parent_turn_id: str,
        payload_digest: str,
    ) -> str:
        """Atomically return the winner for one report or reply retry key."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if turn_kind == "report":
                    prior_reports = connection.execute(
                        """
                        SELECT payload_digest, turn_id
                        FROM remote_turns
                        WHERE report_id = ? AND turn_kind = 'report'
                            AND parent_turn_id = ''
                        """,
                        (report_id,),
                    ).fetchall()
                    if prior_reports:
                        if prior_reports[0][0] != payload_digest:
                            raise RemoteStateConflict(
                                "report_id already has a different initial payload"
                            )
                        connection.commit()
                        return prior_reports[0][1]
                else:
                    pending = self._pending_in_transaction(connection, report_id)
                    if pending is None or pending.parent_turn_id != parent_turn_id:
                        raise RemoteStateConflict(
                            "reply does not match the current pending Captain question"
                        )

                generated_turn_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO remote_turns(
                        report_id, turn_kind, parent_turn_id, payload_digest,
                        turn_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        report_id,
                        turn_kind,
                        parent_turn_id,
                        payload_digest,
                        generated_turn_id,
                        _current_utc_time(),
                    ),
                )
                winner = connection.execute(
                    """
                    SELECT turn_id
                    FROM remote_turns
                    WHERE report_id = ? AND turn_kind = ? AND parent_turn_id = ?
                        AND payload_digest = ?
                    """,
                    (report_id, turn_kind, parent_turn_id, payload_digest),
                ).fetchone()
                if winner is None:
                    raise RemoteStateConflict("remote turn could not be reserved")
                connection.commit()
                return winner[0]
            except Exception:
                connection.rollback()
                raise

    def get_or_create_report_turn(self, report_id: str, payload_digest: str) -> str:
        """Return the one stable remote turn ID for an initial report payload."""

        report_id = _require_text(report_id, "report_id", MAX_REPORT_ID_CHARACTERS)
        payload_digest = _require_text(
            payload_digest, "payload_digest", MAX_DIGEST_CHARACTERS
        )
        return self._get_or_create_turn(report_id, "report", "", payload_digest)

    def get_or_create_reply_turn(
        self, report_id: str, parent_turn_id: str, payload_digest: str
    ) -> str:
        """Return a stable reply turn only for the current pending Captain question."""

        report_id = _require_text(report_id, "report_id", MAX_REPORT_ID_CHARACTERS)
        parent_turn_id = _require_text(
            parent_turn_id, "parent_turn_id", MAX_TURN_ID_CHARACTERS
        )
        payload_digest = _require_text(
            payload_digest, "payload_digest", MAX_DIGEST_CHARACTERS
        )
        return self._get_or_create_turn(
            report_id, "reply", parent_turn_id, payload_digest
        )

    def get_pending(self, report_id: str) -> PendingCaptainQuestions | None:
        """Return the current unanswered Captain questions for one report, if valid."""

        report_id = _require_text(report_id, "report_id", MAX_REPORT_ID_CHARACTERS)
        with closing(self._connect()) as connection:
            return self._pending_in_transaction(connection, report_id)

    def replace_pending(
        self, report_id: str, parent_turn_id: str, questions: Sequence[str]
    ) -> None:
        """Atomically replace one report's unanswered Captain question context."""

        report_id = _require_text(report_id, "report_id", MAX_REPORT_ID_CHARACTERS)
        parent_turn_id = _require_text(
            parent_turn_id, "parent_turn_id", MAX_TURN_ID_CHARACTERS
        )
        validated_questions = _validated_questions(questions)
        questions_json = json.dumps(list(validated_questions), ensure_ascii=False)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO pending_questions(
                        report_id, parent_turn_id, questions_json, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(report_id) DO UPDATE SET
                        parent_turn_id = excluded.parent_turn_id,
                        questions_json = excluded.questions_json,
                        updated_at = excluded.updated_at
                    """,
                    (report_id, parent_turn_id, questions_json, _current_utc_time()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def clear_pending(self, report_id: str) -> None:
        """Remove current question context for one report; repeated clearing is safe."""

        report_id = _require_text(report_id, "report_id", MAX_REPORT_ID_CHARACTERS)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM pending_questions WHERE report_id = ?", (report_id,)
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
