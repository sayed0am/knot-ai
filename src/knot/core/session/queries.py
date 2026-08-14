"""Cross-session queries, each expressed as a single SQL statement.

Every function here answers a question that spans many sessions (a fleet
inbox, an agent's session history, a parent/child chain). Each is one SQL
statement (a recursive CTE where the shape is a tree) executed through
``SessionStore.query`` — never a Python loop issuing one query per session —
so the cost stays a single round trip to SQLite regardless of fleet size.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from knot.core.events import PendingInputRequest

from .store import SessionRecord, SessionStore, session_from_row

# Recursive CTE walking every session's ancestor chain (self at depth 0, up
# through parent_session_id to the root). Reused by pending_requests_fleet to
# attribute each request to its root session and to the hop path between.
_ANCESTRY_CTE = """
WITH RECURSIVE path(origin_session_id, session_id, agent_id, parent_session_id, depth) AS (
    SELECT session_id, session_id, agent_id, parent_session_id, 0
    FROM sessions
    UNION ALL
    SELECT p.origin_session_id, s.session_id, s.agent_id, s.parent_session_id, p.depth + 1
    FROM sessions s
    JOIN path p ON s.session_id = p.parent_session_id
)
"""


@dataclass(frozen=True, slots=True)
class SessionHop:
    """One session on the path from a request's session up to its root."""

    session_id: str
    agent_id: str


@dataclass(frozen=True, slots=True)
class PendingRequestRow:
    """One unresolved request in the fleet-wide approvals inbox.

    ``path`` runs root-first down to (and including) ``session_id`` — the
    session that actually raised the request — so a child session's
    request is attributable to the top-level session a human recognizes.
    """

    request: PendingInputRequest
    session_id: str
    agent_id: str
    created_at: int
    root_session_id: str
    path: tuple[SessionHop, ...]


def pending_requests_fleet(
    store: SessionStore, *, kinds: tuple[str, ...] | None = None
) -> list[PendingRequestRow]:
    """Every unresolved ``input_requested`` entry across all sessions.

    A request is unresolved when no ``input_resolved`` entry in the same
    session references its ``request_id``. Ordered oldest-first.

    ``kinds`` restricts the result to requests whose ``PendingInputRequest.kind``
    is in the given set; ``None`` (the default) returns every kind. An
    approvals-inbox caller passes ``("tool_approval", "question")`` to keep
    a parent's own ``"child_session"`` park signal — an internal linkage
    record, not something a human ever acts on directly — out of the list;
    a child session's own ``tool_approval``/``question`` requests still
    appear, with root attribution, since they belong to a different session
    than the parent's park.
    """
    kind_filter_sql = ""
    params: list[str] = []
    if kinds is not None:
        placeholders = ", ".join("?" for _ in kinds)
        kind_filter_sql = f"AND json_extract(payload_json, '$.kind') IN ({placeholders})"
        params = list(kinds)

    sql = f"""
    {_ANCESTRY_CTE},
    resolved AS (
        SELECT session_id, json_extract(payload_json, '$.requestId') AS request_id
        FROM entries
        WHERE type = 'input_resolved'
    ),
    requested AS (
        SELECT session_id, seq, payload_json, created_at,
               json_extract(payload_json, '$.id') AS request_id
        FROM entries
        WHERE type = 'input_requested'
        {kind_filter_sql}
    ),
    unresolved AS (
        SELECT r.session_id, r.seq, r.payload_json, r.created_at
        FROM requested r
        LEFT JOIN resolved x
          ON x.session_id = r.session_id AND x.request_id = r.request_id
        WHERE x.request_id IS NULL
    ),
    hops AS (
        SELECT origin_session_id,
               json_group_array(json_object('sessionId', session_id, 'agentId', agent_id))
                   AS hops_json
        FROM (
            SELECT origin_session_id, session_id, agent_id
            FROM path
            ORDER BY origin_session_id, depth DESC
        )
        GROUP BY origin_session_id
    ),
    roots AS (
        SELECT origin_session_id, session_id AS root_session_id
        FROM path
        WHERE parent_session_id IS NULL
    )
    SELECT u.session_id, u.payload_json, u.created_at,
           s.agent_id, roots.root_session_id, hops.hops_json
    FROM unresolved u
    JOIN sessions s ON s.session_id = u.session_id
    JOIN roots ON roots.origin_session_id = u.session_id
    JOIN hops ON hops.origin_session_id = u.session_id
    ORDER BY u.created_at ASC, u.session_id ASC, u.seq ASC
    """
    rows = store.query(sql, params)
    result: list[PendingRequestRow] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        hops = tuple(
            SessionHop(session_id=hop["sessionId"], agent_id=hop["agentId"])
            for hop in json.loads(row["hops_json"])
        )
        result.append(
            PendingRequestRow(
                request=PendingInputRequest.model_validate(payload),
                session_id=row["session_id"],
                agent_id=row["agent_id"],
                created_at=row["created_at"],
                root_session_id=row["root_session_id"],
                path=hops,
            )
        )
    return result


def sessions_for_agent(store: SessionStore, agent_id: str) -> list[SessionRecord]:
    """Every session for ``agent_id``, newest-first."""
    sql = "SELECT * FROM sessions WHERE agent_id = ? ORDER BY created_at DESC, session_id DESC"
    rows = store.query(sql, (agent_id,))
    return [session_from_row(row) for row in rows]


@dataclass(frozen=True, slots=True)
class ChildChainRow:
    """One descendant of a session, with its distance from that session."""

    session: SessionRecord
    depth: int


def child_chain(store: SessionStore, session_id: str) -> list[ChildChainRow]:
    """Every descendant of ``session_id`` (children, grandchildren, ...).

    A recursive CTE walking down ``parent_session_id``, ordered breadth
    first (shallowest descendants before deeper ones); each row carries its
    own ``parent_session_id``/``parent_tool_call_id`` so a caller can
    reconstruct the tree.
    """
    sql = """
    WITH RECURSIVE descendants(
        session_id, agent_id, parent_session_id, parent_tool_call_id, created_at, depth
    ) AS (
        SELECT session_id, agent_id, parent_session_id, parent_tool_call_id, created_at, 0
        FROM sessions
        WHERE session_id = ?
        UNION ALL
        SELECT s.session_id, s.agent_id, s.parent_session_id, s.parent_tool_call_id,
               s.created_at, d.depth + 1
        FROM sessions s
        JOIN descendants d ON s.parent_session_id = d.session_id
    )
    SELECT * FROM descendants WHERE depth > 0 ORDER BY depth ASC, created_at ASC, session_id ASC
    """
    rows = store.query(sql, (session_id,))
    return [
        ChildChainRow(session=session_from_row(row), depth=row["depth"]) for row in rows
    ]


def scan_payloads(store: SessionStore, needle: str) -> list[tuple[str, int, str]]:
    """Return ``(session_id, seq, type)`` for every entry whose payload contains ``needle``.

    A no-secrets boundary check: durable session payloads should never
    contain runtime secret material (see ``tests/test_session_secrets.py``).
    """
    sql = "SELECT session_id, seq, type FROM entries WHERE payload_json LIKE ?"
    rows = store.query(sql, (f"%{needle}%",))
    return [(row["session_id"], row["seq"], row["type"]) for row in rows]


__all__ = [
    "ChildChainRow",
    "PendingRequestRow",
    "SessionHop",
    "child_chain",
    "pending_requests_fleet",
    "scan_payloads",
    "sessions_for_agent",
]
