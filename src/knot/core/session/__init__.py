"""Durable session store: append-only SQLite log, state derivation, queries.

Sessions persist to one embedded SQLite database. Each session's transcript
and park signals live in an append-only ``entries`` log; live state
(``idle``/``waiting``) is always computed from that log, never stored as a
mutable column, so crash recovery and human-in-the-loop resume share one
code path (``rehydrate``). See the individual modules for details:
``store`` (schema + persistence primitives), ``entries`` (entry payload
types), ``persistence`` (the harness subscriber that writes them),
``state`` (derivation + rehydration), ``queries`` (fleet-wide SQL), and
``export`` (JSONL export).
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from .entries import (
    ENTRY_TYPE_COMPACTION,
    ENTRY_TYPE_EXECUTION_STARTED,
    ENTRY_TYPE_INPUT_REQUESTED,
    ENTRY_TYPE_INPUT_RESOLVED,
    ENTRY_TYPE_MESSAGE,
    Compaction,
    ExecutionStarted,
    InputResolution,
    ResolutionDecision,
    entry_to_compaction,
    entry_to_execution_started,
    entry_to_message,
    entry_to_request,
    entry_to_resolution,
)
from .export import export_session_jsonl
from .persistence import PersistenceSubscriber
from .queries import (
    ChildChainRow,
    PendingRequestRow,
    SessionHop,
    child_chain,
    pending_requests_fleet,
    scan_payloads,
    sessions_for_agent,
)
from .state import DerivedState, SessionStatus, derive_state, harness_from_session, rehydrate
from .store import (
    Entry,
    SessionRecord,
    SessionStore,
    WriterAlreadyClaimedError,
    WriterClaim,
    new_session_id,
)

__all__ = [name for name in globals() if not name.startswith("_")]
