"""Durable single-file SQLite session store.

One embedded SQLite database holds every session: an append-only ``entries``
log per session (the source of truth for durable state — see
``knot.core.session.state``) and a ``sessions`` table for identity and
parent/child linkage (used for the fleet-wide queries in
``knot.core.session.queries``).

Design choices, made deliberately for the v0 single-process asyncio runtime:

- One ``sqlite3.Connection`` per store instance, opened with
  ``check_same_thread=False`` and guarded by a single ``threading.Lock`` for
  every statement (read or write). This keeps the store simple and correct
  without pretending to support multi-process writers; a later work package
  can revisit pooling/concurrency if the runtime grows beyond one process.
- Appends are short, single-row, synchronous writes committed immediately.
  For the volumes a single-process agent runtime produces, blocking a
  worker thread for a few milliseconds per event is an acceptable and much
  simpler alternative to an async SQLite driver or a write-behind queue.
- Session ``status`` is never stored as a mutable column: it is always
  computed from the entry tail (see ``knot.core.session.state``), so crash
  recovery, HITL rehydration, and (later) child-park rehydration all read
  the same durable facts through the same code path.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knot.providers.messages import current_timestamp_ms
from knot.providers.types import JSONValue

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    parent_session_id TEXT REFERENCES sessions(session_id),
    parent_tool_call_id TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(type);
CREATE INDEX IF NOT EXISTS idx_sessions_agent_id ON sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent_session_id ON sessions(parent_session_id);
"""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One row of the ``sessions`` table: identity and parent linkage."""

    session_id: str
    agent_id: str
    parent_session_id: str | None
    parent_tool_call_id: str | None
    created_at: int


@dataclass(frozen=True, slots=True)
class Entry:
    """One row of the append-only ``entries`` log for a session."""

    session_id: str
    seq: int
    type: str
    payload: dict[str, JSONValue]
    created_at: int


class WriterAlreadyClaimedError(RuntimeError):
    """Raised when a session already has a live writer claim.

    Each session must have exactly one live writer (see
    ``SessionStore.claim_writer``); this guards the store-level invariant
    that ``knot.core.session.persistence.PersistenceSubscriber`` relies on.
    """


class WriterClaim:
    """A held claim that one caller is the sole writer for a session.

    Returned by ``SessionStore.claim_writer``. Usable as a context manager
    or released explicitly with ``release()``; releasing twice is a no-op.
    """

    def __init__(self, store: SessionStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self._released = False

    @property
    def session_id(self) -> str:
        return self._session_id

    def release(self) -> None:
        if self._released:
            return
        self._store._release_writer(self._session_id)
        self._released = True

    def __enter__(self) -> WriterClaim:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class SessionStore:
    """Synchronous facade over one embedded SQLite session database."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._claimed_writers: set[str] = set()
        self._bootstrap()

    def _bootstrap(self) -> None:
        with self._lock:
            if self._path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- identity -----------------------------------------------------

    def create_session(
        self,
        agent_id: str,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> SessionRecord:
        session_id = session_id or new_session_id()
        created_at = current_timestamp_ms()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions "
                "(session_id, agent_id, parent_session_id, parent_tool_call_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, agent_id, parent_session_id, parent_tool_call_id, created_at),
            )
            self._conn.commit()
        return SessionRecord(
            session_id=session_id,
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            parent_tool_call_id=parent_tool_call_id,
            created_at=created_at,
        )

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return _row_to_session(row) if row is not None else None

    # -- entries --------------------------------------------------------

    def append_entry(self, session_id: str, type: str, payload: dict[str, JSONValue]) -> Entry:
        """Append one entry, assigning ``seq`` atomically under the lock.

        ``seq`` is computed as ``max(existing seq) + 1`` (0 for the first
        entry) and the insert happens in the same critical section, so
        interleaved appends to different sessions from different threads
        never race on a shared sequence.
        """
        payload_json = json.dumps(payload)
        created_at = current_timestamp_ms()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM entries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = row[0]
            self._conn.execute(
                "INSERT INTO entries (session_id, seq, type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, seq, type, payload_json, created_at),
            )
            self._conn.commit()
        return Entry(
            session_id=session_id, seq=seq, type=type, payload=payload, created_at=created_at
        )

    def entries(self, session_id: str) -> list[Entry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM entries WHERE session_id = ? ORDER BY seq ASC", (session_id,)
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    # -- single-writer enforcement ---------------------------------------

    def claim_writer(self, session_id: str) -> WriterClaim:
        """Claim exclusive write ownership of ``session_id``.

        Raises ``WriterAlreadyClaimedError`` if another live claim exists
        for the same session. Release the returned ``WriterClaim`` (or use
        it as a context manager) to free the claim.
        """
        with self._lock:
            if session_id in self._claimed_writers:
                raise WriterAlreadyClaimedError(
                    f"session {session_id!r} already has a live writer claim"
                )
            self._claimed_writers.add(session_id)
        return WriterClaim(self, session_id)

    def _release_writer(self, session_id: str) -> None:
        with self._lock:
            self._claimed_writers.discard(session_id)

    # -- read-only escape hatch for cross-session queries -----------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Run a read-only SQL statement under the store's lock.

        Exposed for ``knot.core.session.queries``, whose cross-session
        queries (fleet inbox, per-agent listing, child chains) are each
        expressed as a single SQL statement rather than a Python loop.
        """
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex}"


def session_from_row(row: sqlite3.Row) -> SessionRecord:
    """Build a ``SessionRecord`` from a raw ``sessions`` row.

    Exposed (not underscored) because ``knot.core.session.queries`` reuses
    it to turn rows from its own hand-written SQL into the same record type.
    """
    return _row_to_session(row)


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        parent_session_id=row["parent_session_id"],
        parent_tool_call_id=row["parent_tool_call_id"],
        created_at=row["created_at"],
    )


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        session_id=row["session_id"],
        seq=row["seq"],
        type=row["type"],
        payload=json.loads(row["payload_json"]),
        created_at=row["created_at"],
    )


__all__ = [
    "Entry",
    "SessionRecord",
    "SessionStore",
    "WriterAlreadyClaimedError",
    "WriterClaim",
    "new_session_id",
    "session_from_row",
]
