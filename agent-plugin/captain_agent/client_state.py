"""Keep the minimum durable state to retry remote Captain turns safely."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
from uuid import UUID, uuid4


MAX_REPORT_ID_CHARACTERS = 128
MAX_TURN_ID_CHARACTERS = 128
MAX_DIGEST_CHARACTERS = 256
MAX_PENDING_QUESTIONS = 20
MAX_QUESTION_CHARACTERS = 1_000
MAX_TIMESTAMP_CHARACTERS = 128
SQLITE_BUSY_TIMEOUT_SECONDS = 1.0
LEGACY_UNSCOPED_PROFILE_ID = "0" * 64
PROFILE_ID_CHARACTERS = frozenset("0123456789abcdef")


class RemoteStateConflict(Exception):
    """Raised when a retry no longer matches the current remote state."""


@dataclass(frozen=True)
class PendingCaptainQuestions:
    """The exact Captain questions that a later user reply must answer.

    Attributes:
        report_id: Caller-chosen identifier of the originating report.
        parent_turn_id: Remote turn ID that produced these questions.
        questions: Validated question strings awaiting a user reply.
    """

    report_id: str
    parent_turn_id: str
    questions: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate fields and freeze question strings at the boundary."""

        object.__setattr__(
            self,
            "report_id",
            _require_text(
                self.report_id, "report_id", MAX_REPORT_ID_CHARACTERS
            ),
        )
        object.__setattr__(
            self,
            "parent_turn_id",
            _require_text(
                self.parent_turn_id, "parent_turn_id", MAX_TURN_ID_CHARACTERS
            ),
        )
        object.__setattr__(
            self, "questions", _validated_questions(self.questions)
        )


def remote_state_path(env: Mapping[str, str]) -> Path:
    """Choose the privacy-limited SQLite path for remote client state.

    Args:
        env: Environment mapping consulted for path overrides.

    Returns:
        The SQLite database path for remote client state.
    """

    override = str(env.get("CAPTAIN_REMOTE_STATE_PATH", ""))
    if override.strip():
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
        raise ValueError(
            f"{name} must be a nonblank string up to {limit} characters"
        )
    return value


def _require_profile_id(value: object) -> str:
    """Accept only a non-legacy lowercase SHA-256 profile identifier."""

    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in PROFILE_ID_CHARACTERS for character in value)
        or value == LEGACY_UNSCOPED_PROFILE_ID
    ):
        raise ValueError("profile_id must be a non-legacy SHA-256 identifier")
    return value


def _validated_questions(questions: Sequence[str]) -> tuple[str, ...]:
    """Return the exact valid question strings that continuation needs."""

    if not isinstance(questions, (list, tuple)):
        raise ValueError("questions must be a list or tuple of strings")
    if not questions or len(questions) > MAX_PENDING_QUESTIONS:
        raise ValueError(
            f"questions must contain 1 to {MAX_PENDING_QUESTIONS} items"
        )

    return tuple(
        _require_text(question, "question", MAX_QUESTION_CHARACTERS)
        for question in questions
    )


class RemoteClientState:
    """Store turn IDs and one pending Captain question context per report.

    Attributes:
        path: Location of the SQLite database backing this store.
        profile_id: SHA-256 namespace that scopes every stored row.
    """

    def __init__(
        self,
        path: Path,
        *,
        profile_id: str,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Open or create the scoped store at the given path.

        Args:
            path: SQLite database location for this store.
            profile_id: Non-legacy lowercase SHA-256 namespace.
            env: Environment mapping used for path policy checks;
                defaults to ``os.environ``.
        """

        self.path = Path(path)
        self.profile_id = _require_profile_id(profile_id)
        self._env = os.environ if env is None else env
        self._initialize_store()

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived connection that waits briefly for a writer."""

        self._assert_safe_store_path()
        return sqlite3.connect(
            self.path,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
        )

    def _initialize_store(self) -> None:
        """Create the owner-only state directory, database, and small schema."""

        self._prepare_store_path()

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_unscoped_schema(connection)
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS remote_turns(
                        profile_id TEXT NOT NULL,
                        report_id TEXT NOT NULL,
                        turn_kind TEXT NOT NULL,
                        parent_turn_id TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(
                            profile_id, report_id, turn_kind,
                            parent_turn_id, payload_digest
                        )
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS one_initial_remote_turn_per_report
                    ON remote_turns(profile_id, report_id)
                    WHERE turn_kind = 'report' AND parent_turn_id = ''
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_questions(
                        profile_id TEXT NOT NULL,
                        report_id TEXT NOT NULL,
                        parent_turn_id TEXT NOT NULL,
                        questions_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(profile_id, report_id)
                    )
                    """
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _migrate_unscoped_schema(self, connection: sqlite3.Connection) -> None:
        """Quarantine v1 rows under an unreachable legacy namespace."""

        turn_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(remote_turns)"
            )
        }
        pending_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(pending_questions)"
            )
        }
        if not turn_columns and not pending_columns:
            return
        turn_is_scoped = "profile_id" in turn_columns
        pending_is_scoped = "profile_id" in pending_columns
        if turn_is_scoped and pending_is_scoped:
            return
        if (
            turn_is_scoped != pending_is_scoped
            or not turn_columns
            or not pending_columns
        ):
            raise RemoteStateConflict("remote state schema is partially scoped")

        connection.execute(
            "DROP INDEX IF EXISTS one_initial_remote_turn_per_report"
        )
        connection.execute(
            "ALTER TABLE remote_turns RENAME TO remote_turns_unscoped_v1"
        )
        connection.execute(
            "ALTER TABLE pending_questions"
            " RENAME TO pending_questions_unscoped_v1"
        )
        connection.execute(
            """
            CREATE TABLE remote_turns(
                profile_id TEXT NOT NULL,
                report_id TEXT NOT NULL,
                turn_kind TEXT NOT NULL,
                parent_turn_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(
                    profile_id, report_id, turn_kind,
                    parent_turn_id, payload_digest
                )
            )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX one_initial_remote_turn_per_report
            ON remote_turns(profile_id, report_id)
            WHERE turn_kind = 'report' AND parent_turn_id = ''
            """
        )
        connection.execute(
            """
            CREATE TABLE pending_questions(
                profile_id TEXT NOT NULL,
                report_id TEXT NOT NULL,
                parent_turn_id TEXT NOT NULL,
                questions_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(profile_id, report_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO remote_turns(
                profile_id, report_id, turn_kind, parent_turn_id,
                payload_digest, turn_id, created_at
            )
            SELECT ?, report_id, turn_kind, parent_turn_id,
                payload_digest, turn_id, created_at
            FROM remote_turns_unscoped_v1
            """,
            (LEGACY_UNSCOPED_PROFILE_ID,),
        )
        connection.execute(
            """
            INSERT INTO pending_questions(
                profile_id, report_id, parent_turn_id, questions_json, updated_at
            )
            SELECT ?, report_id, parent_turn_id, questions_json, updated_at
            FROM pending_questions_unscoped_v1
            """,
            (LEGACY_UNSCOPED_PROFILE_ID,),
        )
        connection.execute("DROP TABLE remote_turns_unscoped_v1")
        connection.execute("DROP TABLE pending_questions_unscoped_v1")

    def _uses_normal_state_parent(self) -> bool:
        """Return whether this path is the current default state location."""

        override = str(self._env.get("CAPTAIN_REMOTE_STATE_PATH", "")).strip()
        return not override and self.path == remote_state_path(self._env)

    def _prepare_store_path(self) -> None:
        """Create missing Captain-owned paths and secure the database file."""

        try:
            parent_info = os.lstat(self.path.parent)
            parent_existed = True
        except FileNotFoundError:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
            parent_info = os.lstat(self.path.parent)
            parent_existed = False

        if stat.S_ISLNK(parent_info.st_mode):
            raise ValueError("remote state parent must not be a symlink")
        if not stat.S_ISDIR(parent_info.st_mode):
            raise ValueError("remote state parent must be a directory")
        if not parent_existed or self._uses_normal_state_parent():
            os.chmod(self.path.parent, 0o700)

        try:
            database_info = os.lstat(self.path)
        except FileNotFoundError:
            self._create_private_database_file()
        else:
            if stat.S_ISLNK(database_info.st_mode):
                raise ValueError("remote state database must not be a symlink")
            self._secure_existing_database_file()

    def _no_follow_flags(self) -> int:
        """Use the strongest no-follow flag available on this platform."""

        return getattr(os, "O_NOFOLLOW_ANY", getattr(os, "O_NOFOLLOW", 0))

    def _create_private_database_file(self) -> None:
        """Create the SQLite file privately before SQLite opens it."""

        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | self._no_follow_flags()
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError:
            self._secure_existing_database_file()
            return
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("remote state database must be a regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _secure_existing_database_file(self) -> None:
        """Re-mode an existing regular database without following links."""

        database_info = os.lstat(self.path)
        if stat.S_ISLNK(database_info.st_mode):
            raise ValueError("remote state database must not be a symlink")
        flags = os.O_RDWR | self._no_follow_flags()
        descriptor = os.open(self.path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("remote state database must be a regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _assert_safe_store_path(self) -> None:
        """Reject a symlink or non-regular database before SQLite opens it."""

        parent_info = os.lstat(self.path.parent)
        database_info = os.lstat(self.path)
        if (
            stat.S_ISLNK(parent_info.st_mode)
            or stat.S_ISLNK(database_info.st_mode)
        ):
            raise ValueError("remote state path must not contain a symlink")
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or not stat.S_ISREG(database_info.st_mode)
        ):
            raise ValueError(
                "remote state path must use a directory and regular "
                "database"
            )

    def _pending_in_transaction(
        self, connection: sqlite3.Connection, report_id: str
    ) -> PendingCaptainQuestions | None:
        """Read one pending row, treating malformed data as unusable."""

        row = connection.execute(
            """
            SELECT parent_turn_id, questions_json
            FROM pending_questions
            WHERE profile_id = ? AND report_id = ?
            """,
            (self.profile_id, report_id),
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

    def _turn_rows_in_transaction(
        self, connection: sqlite3.Connection, report_id: str
    ) -> list[tuple[str, str, str, str, str, str, str]]:
        """Read only structurally valid persisted turns for one report ID."""

        rows = connection.execute(
            """
            SELECT profile_id, report_id, turn_kind, parent_turn_id, payload_digest,
                turn_id, created_at
            FROM remote_turns
            WHERE (
                profile_id = ?
                OR (typeof(profile_id) != 'text' AND CAST(profile_id AS TEXT) = ?)
            ) AND (
                report_id = ?
                OR (typeof(report_id) != 'text' AND CAST(report_id AS TEXT) = ?)
            )
            """,
            (self.profile_id, self.profile_id, report_id, report_id),
        ).fetchall()
        validated_rows = []
        for row in rows:
            if len(row) != 7:
                raise RemoteStateConflict(
                    "remote turn row has an invalid shape"
                )
            (
                stored_profile_id,
                stored_report_id,
                turn_kind,
                parent_turn_id,
                digest,
                turn_id,
                created_at,
            ) = row
            try:
                _require_profile_id(stored_profile_id)
                _require_text(
                    stored_report_id, "report_id", MAX_REPORT_ID_CHARACTERS
                )
                _require_text(turn_kind, "turn_kind", MAX_TURN_ID_CHARACTERS)
                _require_text(digest, "payload_digest", MAX_DIGEST_CHARACTERS)
                _require_text(
                    created_at, "created_at", MAX_TIMESTAMP_CHARACTERS
                )
                if (
                    stored_profile_id != self.profile_id
                    or stored_report_id != report_id
                    or turn_kind not in {"report", "reply"}
                ):
                    raise ValueError(
                        "remote turn row has an unexpected report or kind"
                    )
                if turn_kind == "report":
                    if parent_turn_id != "":
                        raise ValueError("initial remote turn has a parent")
                else:
                    _require_text(
                        parent_turn_id, "parent_turn_id", MAX_TURN_ID_CHARACTERS
                    )
                parsed_turn_id = UUID(turn_id)
                if (
                    parsed_turn_id.version != 4
                    or str(parsed_turn_id) != turn_id
                ):
                    raise ValueError("remote turn ID is not a canonical UUIDv4")
            except (TypeError, ValueError, AttributeError):
                raise RemoteStateConflict(
                    "remote turn row is malformed"
                ) from None
            validated_rows.append(row)
        return validated_rows

    @staticmethod
    def _matching_turn(
        rows: Sequence[tuple[str, str, str, str, str, str, str]],
        turn_kind: str,
        parent_turn_id: str,
        payload_digest: str,
    ) -> str | None:
        """Return the only row matching one exact retry relationship."""

        matches = [
            row
            for row in rows
            if row[2] == turn_kind
            and row[3] == parent_turn_id
            and row[4] == payload_digest
        ]
        if len(matches) > 1:
            raise RemoteStateConflict(
                "remote turn state contains duplicate retry rows"
            )
        return matches[0][5] if matches else None

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
                rows = self._turn_rows_in_transaction(connection, report_id)
                if turn_kind == "report":
                    prior_reports = [row for row in rows if row[2] == "report"]
                    if prior_reports:
                        if (
                            len(prior_reports) != 1
                            or prior_reports[0][4] != payload_digest
                        ):
                            raise RemoteStateConflict(
                                "report_id already has a different "
                                "initial payload"
                            )
                        connection.commit()
                        return prior_reports[0][5]
                else:
                    pending = self._pending_in_transaction(
                        connection, report_id
                    )
                    if (
                        pending is None
                        or pending.parent_turn_id != parent_turn_id
                    ):
                        raise RemoteStateConflict(
                            "reply does not match the current pending "
                            "Captain question"
                        )
                    winner = self._matching_turn(
                        rows, turn_kind, parent_turn_id, payload_digest
                    )
                    if winner is not None:
                        connection.commit()
                        return winner

                generated_turn_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO remote_turns(
                        profile_id, report_id, turn_kind, parent_turn_id, payload_digest,
                        turn_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        self.profile_id,
                        report_id,
                        turn_kind,
                        parent_turn_id,
                        payload_digest,
                        generated_turn_id,
                        _current_utc_time(),
                    ),
                )
                winner = self._matching_turn(
                    self._turn_rows_in_transaction(connection, report_id),
                    turn_kind,
                    parent_turn_id,
                    payload_digest,
                )
                if winner is None:
                    raise RemoteStateConflict(
                        "remote turn could not be reserved"
                    )
                connection.commit()
                return winner
            except Exception:
                connection.rollback()
                raise

    def get_or_create_report_turn(
        self, report_id: str, payload_digest: str
    ) -> str:
        """Return the one stable remote turn ID for an initial report.

        Args:
            report_id: Caller-chosen report identifier.
            payload_digest: SHA-256 digest of the canonical payload.

        Returns:
            The stable UUIDv4 turn ID reserved for this payload.

        Raises:
            ValueError: If an argument is blank or too long.
            RemoteStateConflict: If the report ID was already used with a
                different payload.
        """

        report_id = _require_text(
            report_id, "report_id", MAX_REPORT_ID_CHARACTERS
        )
        payload_digest = _require_text(
            payload_digest, "payload_digest", MAX_DIGEST_CHARACTERS
        )
        return self._get_or_create_turn(report_id, "report", "", payload_digest)

    def get_or_create_reply_turn(
        self, report_id: str, parent_turn_id: str, payload_digest: str
    ) -> str:
        """Return a stable reply turn for the pending Captain question.

        Args:
            report_id: Caller-chosen report identifier.
            parent_turn_id: Turn ID of the question being answered.
            payload_digest: SHA-256 digest of the reply text.

        Returns:
            The stable UUIDv4 turn ID reserved for this reply.

        Raises:
            ValueError: If an argument is blank or too long.
            RemoteStateConflict: If the reply does not match the current
                pending question context.
        """

        report_id = _require_text(
            report_id, "report_id", MAX_REPORT_ID_CHARACTERS
        )
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
        """Return the current unanswered Captain questions, if valid.

        Args:
            report_id: Caller-chosen report identifier.

        Returns:
            The pending question context, or None when no valid pending
            row exists for the report.
        """

        report_id = _require_text(
            report_id, "report_id", MAX_REPORT_ID_CHARACTERS
        )
        with closing(self._connect()) as connection:
            return self._pending_in_transaction(connection, report_id)

    def replace_pending(
        self, report_id: str, parent_turn_id: str, questions: Sequence[str]
    ) -> None:
        """Atomically replace one report's pending question context.

        Args:
            report_id: Caller-chosen report identifier.
            parent_turn_id: Turn ID that produced the questions.
            questions: Nonempty sequence of question strings.

        Raises:
            ValueError: If an argument is blank, too long, or malformed.
        """

        report_id = _require_text(
            report_id, "report_id", MAX_REPORT_ID_CHARACTERS
        )
        parent_turn_id = _require_text(
            parent_turn_id, "parent_turn_id", MAX_TURN_ID_CHARACTERS
        )
        validated_questions = _validated_questions(questions)
        questions_json = json.dumps(
            list(validated_questions), ensure_ascii=False
        )

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO pending_questions(
                        profile_id, report_id, parent_turn_id, questions_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id, report_id) DO UPDATE SET
                        parent_turn_id = excluded.parent_turn_id,
                        questions_json = excluded.questions_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        self.profile_id,
                        report_id,
                        parent_turn_id,
                        questions_json,
                        _current_utc_time(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def clear_pending(self, report_id: str) -> None:
        """Remove one report's question context; repeat clears are safe.

        Args:
            report_id: Caller-chosen report identifier.
        """

        report_id = _require_text(
            report_id, "report_id", MAX_REPORT_ID_CHARACTERS
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM pending_questions"
                    " WHERE profile_id = ? AND report_id = ?",
                    (self.profile_id, report_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
