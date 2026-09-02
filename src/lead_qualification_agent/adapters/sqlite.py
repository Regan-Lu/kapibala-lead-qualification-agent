"""SQLite persistence and atomic outbound reservations.

The store opens a fresh SQLite connection for every operation.  That keeps
the implementation safe for separate worker processes and makes the locking
boundary explicit in tests.  A file-backed database is required for that
multi-process guarantee; an in-memory database is intentionally not accepted.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import sqlite3
import time

from lead_qualification_agent.domain import (
    Action,
    ConversationState,
    ConversationStatus,
    StateTransition,
    TransitionEvent,
)


class EventOutcome(StrEnum):
    """Outcomes persisted in the action event log."""

    OUTBOUND_RESERVED = "outbound_reserved"
    SENT = "sent"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    SCHEDULED = "scheduled"
    ESCALATED = "escalated"
    CLOSED = "closed"
    SILENT = "silent"
    STALE = "stale"
    REJECTED = "rejected"
    REACTIVATED = "reactivated"


@dataclass(frozen=True, slots=True)
class StoredSession:
    customer_id: str
    state: ConversationState
    last_sent_at: float | None
    version: int


@dataclass(frozen=True, slots=True)
class StoredEvent:
    event_id: int
    customer_id: str
    action: Action | None
    outcome: EventOutcome
    occurred_at: float
    detail: str


@dataclass(frozen=True, slots=True)
class StorageActionResult:
    outcome: EventOutcome
    event_id: int | None
    session: StoredSession
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DemoResetResult:
    sessions_deleted: int
    events_deleted: int


_STATUS_SQL = ", ".join(f"'{status.value}'" for status in ConversationStatus)
_ACTION_SQL = ", ".join(f"'{action.value}'" for action in Action)
_OUTCOME_SQL = ", ".join(f"'{outcome.value}'" for outcome in EventOutcome)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sessions (
    customer_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ({_STATUS_SQL})),
    issue_streak INTEGER NOT NULL CHECK (issue_streak BETWEEN 0 AND 2),
    version INTEGER NOT NULL CHECK (version >= 0),
    last_sent_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS action_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL REFERENCES sessions(customer_id),
    action TEXT CHECK (action IS NULL OR action IN ({_ACTION_SQL})),
    outcome TEXT NOT NULL CHECK (outcome IN ({_OUTCOME_SQL})),
    occurred_at REAL NOT NULL,
    detail TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_action_events_customer_time
    ON action_events(customer_id, occurred_at DESC);
"""


class SQLiteSessionStore:
    """Persist session state and action events in a shared SQLite file."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 5.0,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if str(path) == ":memory:":
            raise ValueError("use a file-backed SQLite database for worker sharing")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")

        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = float(timeout)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _customer_id(customer_id: str) -> str:
        normalized = customer_id.strip()
        if not normalized:
            raise ValueError("customer_id must not be empty")
        return normalized

    @staticmethod
    def _now(now: float | None) -> float:
        return time.time() if now is None else float(now)

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> StoredSession:
        version = int(row["version"])
        state = ConversationState(
            status=ConversationStatus(row["status"]),
            issue_streak=int(row["issue_streak"]),
            revision=version,
        )
        last_sent_at = row["last_sent_at"]
        return StoredSession(
            customer_id=str(row["customer_id"]),
            state=state,
            last_sent_at=(None if last_sent_at is None else float(last_sent_at)),
            version=version,
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredEvent:
        action_value = row["action"]
        return StoredEvent(
            event_id=int(row["event_id"]),
            customer_id=str(row["customer_id"]),
            action=(None if action_value is None else Action(action_value)),
            outcome=EventOutcome(row["outcome"]),
            occurred_at=float(row["occurred_at"]),
            detail=str(row["detail"]),
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        customer_id: str,
        action: Action | None,
        outcome: EventOutcome,
        occurred_at: float,
        detail: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO action_events
                (customer_id, action, outcome, occurred_at, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                None if action is None else action.value,
                outcome.value,
                occurred_at,
                detail,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _insert_session_if_missing(
        connection: sqlite3.Connection,
        *,
        customer_id: str,
        initial_state: ConversationState,
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO sessions
                (customer_id, status, issue_streak, version, last_sent_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (
                customer_id,
                initial_state.status.value,
                initial_state.issue_streak,
                initial_state.revision,
                now,
            ),
        )

    @staticmethod
    def _fetch_session(
        connection: sqlite3.Connection,
        customer_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM sessions WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("session disappeared during a write transaction")
        return row

    @staticmethod
    def _matches_state(row: sqlite3.Row, expected: ConversationState) -> bool:
        return (
            row["status"] == expected.status.value
            and int(row["issue_streak"]) == expected.issue_streak
            and int(row["version"]) == expected.revision
        )

    @staticmethod
    def _update_state(
        connection: sqlite3.Connection,
        *,
        customer_id: str,
        next_state: ConversationState,
        now: float,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET status = ?, issue_streak = ?, version = ?, updated_at = ?
            WHERE customer_id = ?
            """,
            (
                next_state.status.value,
                next_state.issue_streak,
                next_state.revision,
                now,
                customer_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("expected exactly one session state update")

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
        finally:
            connection.close()

    def ensure_session(
        self,
        customer_id: str,
        *,
        initial_state: ConversationState | None = None,
        now: float | None = None,
    ) -> StoredSession:
        customer_id = self._customer_id(customer_id)
        initial_state = initial_state or ConversationState()
        timestamp = self._now(now)
        with self._write_transaction() as connection:
            self._insert_session_if_missing(
                connection,
                customer_id=customer_id,
                initial_state=initial_state,
                now=timestamp,
            )
            return self._row_to_session(self._fetch_session(connection, customer_id))

    def get_session(self, customer_id: str) -> StoredSession | None:
        customer_id = self._customer_id(customer_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM sessions WHERE customer_id = ?",
                (customer_id,),
            ).fetchone()
            return None if row is None else self._row_to_session(row)
        finally:
            connection.close()

    def list_events(
        self,
        customer_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        customer_id = self._customer_id(customer_id)
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        if limit is None:
            query = (
                "SELECT * FROM action_events WHERE customer_id = ? "
                "ORDER BY event_id ASC"
            )
            params: tuple[object, ...] = (customer_id,)
        else:
            query = (
                "SELECT * FROM ("
                "SELECT * FROM action_events WHERE customer_id = ? "
                "ORDER BY event_id DESC LIMIT ?"
                ") ORDER BY event_id ASC"
            )
            params = (customer_id, limit)
        connection = self._connect()
        try:
            rows = connection.execute(query, params).fetchall()
            return tuple(self._row_to_event(row) for row in rows)
        finally:
            connection.close()

    def get_snapshot(
        self,
        customer_id: str,
        *,
        event_limit: int = 50,
    ) -> tuple[StoredSession, tuple[StoredEvent, ...]] | None:
        """Read one session and its newest events from one SQLite snapshot."""

        customer_id = self._customer_id(customer_id)
        if event_limit <= 0:
            raise ValueError("event_limit must be positive")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM sessions WHERE customer_id = ?",
                (customer_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            event_rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM action_events
                    WHERE customer_id = ?
                    ORDER BY event_id DESC
                    LIMIT ?
                )
                ORDER BY event_id ASC
                """,
                (customer_id, event_limit),
            ).fetchall()
            session = self._row_to_session(row)
            events = tuple(self._row_to_event(item) for item in event_rows)
            connection.commit()
            return session, events
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reset_demo(self) -> DemoResetResult:
        """Delete local demo sessions and events in one write transaction."""

        with self._write_transaction() as connection:
            events_deleted = int(
                connection.execute("SELECT COUNT(*) FROM action_events").fetchone()[0]
            )
            sessions_deleted = int(
                connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            )
            connection.execute("DELETE FROM action_events")
            connection.execute("DELETE FROM sessions")
        return DemoResetResult(
            sessions_deleted=sessions_deleted,
            events_deleted=events_deleted,
        )

    def record_event(
        self,
        customer_id: str,
        *,
        action: Action | None,
        outcome: EventOutcome,
        detail: str,
        now: float | None = None,
    ) -> StorageActionResult:
        customer_id = self._customer_id(customer_id)
        timestamp = self._now(now)
        with self._write_transaction() as connection:
            self._insert_session_if_missing(
                connection,
                customer_id=customer_id,
                initial_state=ConversationState(),
                now=timestamp,
            )
            event_id = self._insert_event(
                connection,
                customer_id=customer_id,
                action=action,
                outcome=outcome,
                occurred_at=timestamp,
                detail=detail,
            )
            session = self._row_to_session(self._fetch_session(connection, customer_id))
            return StorageActionResult(outcome, event_id, session, detail)

    def apply_silent_transition(
        self,
        customer_id: str,
        transition: StateTransition,
        *,
        now: float | None = None,
    ) -> StorageActionResult:
        """Persist a no-action transition, including explicit reactivation."""

        if transition.effective_action is not None:
            raise ValueError("a silent transition cannot contain an action")
        customer_id = self._customer_id(customer_id)
        timestamp = self._now(now)
        with self._write_transaction() as connection:
            self._insert_session_if_missing(
                connection,
                customer_id=customer_id,
                initial_state=transition.previous_state,
                now=timestamp,
            )
            row = self._fetch_session(connection, customer_id)
            if not self._matches_state(row, transition.previous_state):
                event_id = self._insert_event(
                    connection,
                    customer_id=customer_id,
                    action=None,
                    outcome=EventOutcome.STALE,
                    occurred_at=timestamp,
                    detail="state_transition_did_not_match_persisted_session",
                )
                return StorageActionResult(
                    EventOutcome.STALE,
                    event_id,
                    self._row_to_session(row),
                    "state_transition_did_not_match_persisted_session",
                )

            if transition.next_state != transition.previous_state:
                self._update_state(
                    connection,
                    customer_id=customer_id,
                    next_state=transition.next_state,
                    now=timestamp,
                )
            outcome = (
                EventOutcome.REACTIVATED
                if transition.event is TransitionEvent.HUMAN_REACTIVATED
                else EventOutcome.SILENT
            )
            event_id = self._insert_event(
                connection,
                customer_id=customer_id,
                action=None,
                outcome=outcome,
                occurred_at=timestamp,
                detail=transition.reason,
            )
            session = self._row_to_session(self._fetch_session(connection, customer_id))
            return StorageActionResult(outcome, event_id, session, transition.reason)

    def apply_non_reply_transition(
        self,
        customer_id: str,
        transition: StateTransition,
        *,
        outcome: EventOutcome,
        now: float | None = None,
    ) -> StorageActionResult:
        """Apply schedule/escalate/close after a state-and-action check."""

        action = transition.effective_action
        if action not in {
            Action.SCHEDULE_FOLLOWUP,
            Action.ESCALATE_TO_HUMAN,
            Action.MARK_NOT_INTERESTED,
        }:
            raise ValueError("non-reply transition has an unsupported action")
        customer_id = self._customer_id(customer_id)
        timestamp = self._now(now)
        with self._write_transaction() as connection:
            self._insert_session_if_missing(
                connection,
                customer_id=customer_id,
                initial_state=transition.previous_state,
                now=timestamp,
            )
            row = self._fetch_session(connection, customer_id)
            if not self._matches_state(row, transition.previous_state):
                event_id = self._insert_event(
                    connection,
                    customer_id=customer_id,
                    action=action,
                    outcome=EventOutcome.STALE,
                    occurred_at=timestamp,
                    detail="state_transition_did_not_match_persisted_session",
                )
                return StorageActionResult(
                    EventOutcome.STALE,
                    event_id,
                    self._row_to_session(row),
                    "state_transition_did_not_match_persisted_session",
                )

            self._update_state(
                connection,
                customer_id=customer_id,
                next_state=transition.next_state,
                now=timestamp,
            )
            event_id = self._insert_event(
                connection,
                customer_id=customer_id,
                action=action,
                outcome=outcome,
                occurred_at=timestamp,
                detail=transition.reason,
            )
            session = self._row_to_session(self._fetch_session(connection, customer_id))
            return StorageActionResult(outcome, event_id, session, transition.reason)

    def prepare_reply(
        self,
        customer_id: str,
        transition: StateTransition,
        *,
        now: float,
        window_seconds: float,
    ) -> StorageActionResult:
        """Atomically apply the transition and reserve one reply slot.

        The reservation updates ``last_sent_at`` before an external sender is
        called.  A crashed sender therefore sacrifices one window rather than
        allowing a retry race to produce duplicate customer messages.
        """

        if transition.effective_action is not Action.REPLY:
            raise ValueError("prepare_reply requires a reply action")
        if transition.previous_state.status is not ConversationStatus.ACTIVE:
            raise ValueError("reply transitions must start from an active state")
        if transition.next_state.status is not ConversationStatus.ACTIVE:
            raise ValueError("reply transitions must remain active")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        customer_id = self._customer_id(customer_id)
        timestamp = float(now)
        with self._write_transaction() as connection:
            self._insert_session_if_missing(
                connection,
                customer_id=customer_id,
                initial_state=transition.previous_state,
                now=timestamp,
            )
            row = self._fetch_session(connection, customer_id)
            if not self._matches_state(row, transition.previous_state):
                event_id = self._insert_event(
                    connection,
                    customer_id=customer_id,
                    action=Action.REPLY,
                    outcome=EventOutcome.STALE,
                    occurred_at=timestamp,
                    detail="state_transition_did_not_match_persisted_session",
                )
                return StorageActionResult(
                    EventOutcome.STALE,
                    event_id,
                    self._row_to_session(row),
                    "state_transition_did_not_match_persisted_session",
                )

            self._update_state(
                connection,
                customer_id=customer_id,
                next_state=transition.next_state,
                now=timestamp,
            )
            reservation = connection.execute(
                """
                UPDATE sessions
                SET last_sent_at = ?, updated_at = ?
                WHERE customer_id = ?
                  AND (
                      last_sent_at IS NULL
                      OR ? - last_sent_at >= ?
                  )
                """,
                (
                    timestamp,
                    timestamp,
                    customer_id,
                    timestamp,
                    float(window_seconds),
                ),
            )
            if reservation.rowcount != 1:
                event_id = self._insert_event(
                    connection,
                    customer_id=customer_id,
                    action=Action.REPLY,
                    outcome=EventOutcome.RATE_LIMITED,
                    occurred_at=timestamp,
                    detail="rolling_window_has_an_existing_reply",
                )
                session = self._row_to_session(self._fetch_session(connection, customer_id))
                return StorageActionResult(
                    EventOutcome.RATE_LIMITED,
                    event_id,
                    session,
                    "rolling_window_has_an_existing_reply",
                )

            event_id = self._insert_event(
                connection,
                customer_id=customer_id,
                action=Action.REPLY,
                outcome=EventOutcome.OUTBOUND_RESERVED,
                occurred_at=timestamp,
                detail="reply_slot_reserved_before_external_send",
            )
            session = self._row_to_session(self._fetch_session(connection, customer_id))
            return StorageActionResult(
                EventOutcome.OUTBOUND_RESERVED,
                event_id,
                session,
                "reply_slot_reserved_before_external_send",
            )

    def finalize_outbound(
        self,
        event_id: int,
        *,
        sent: bool,
        now: float | None = None,
        detail: str | None = None,
    ) -> bool:
        """Turn one reservation into a terminal sent/failed event exactly once."""

        if event_id <= 0:
            raise ValueError("event_id must be positive")
        timestamp = self._now(now)
        outcome = EventOutcome.SENT if sent else EventOutcome.FAILED
        event_detail = detail or (
            "outbound_sender_completed" if sent else "outbound_sender_failed"
        )
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE action_events
                SET outcome = ?, occurred_at = ?, detail = ?
                WHERE event_id = ? AND outcome = ?
                """,
                (
                    outcome.value,
                    timestamp,
                    event_detail,
                    event_id,
                    EventOutcome.OUTBOUND_RESERVED.value,
                ),
            )
            return cursor.rowcount == 1
