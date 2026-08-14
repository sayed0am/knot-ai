"""Deriving live session state from durable entries.

Session state is never stored as a mutable column; it is always computed
fresh from the entry tail. That is a deliberate simplification: crash
recovery, resuming a parked (HITL) session, and — later — recovering a
parked child session all read the exact same derived facts through the
exact same function, ``derive_state``, with no per-scenario special-casing.

Derivation rule: an ``"input_requested"`` entry is pending unless some
later ``"input_resolved"`` entry (by ``request_id``) resolves it. Status is
``"waiting"`` iff at least one request is pending, else ``"idle"``.
``"running"`` is not a derivable status at all — it only describes a live,
in-process ``AgentHarness`` mid-run; a process that just restarted is, by
definition, not running, so a restarted/rehydrated session is always either
``"idle"`` or ``"waiting"``.

Dangling tool calls (an ``AssistantMessage`` whose tool call has no
matching ``ToolResultMessage`` yet) are intentionally left unrepaired here.
That repair already happens once, deterministically, when
``AgentHarness`` starts its next run (see ``AgentHarness._run``), and
duplicating it here would just be a second place for the two to drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from knot.core.events import PendingInputRequest
from knot.core.harness import AgentHarness, AgentHarnessConfig
from knot.providers.messages import AgentMessage

from .entries import (
    ENTRY_TYPE_INPUT_REQUESTED,
    ENTRY_TYPE_INPUT_RESOLVED,
    ENTRY_TYPE_MESSAGE,
    entry_to_message,
    entry_to_request,
    entry_to_resolution,
)
from .store import Entry, SessionStore

SessionStatus = Literal["idle", "waiting"]


@dataclass(frozen=True, slots=True)
class DerivedState:
    """The live-equivalent state computed from a session's entry log."""

    status: SessionStatus
    pending_requests: tuple[PendingInputRequest, ...]
    messages: tuple[AgentMessage, ...]


def derive_state(entries: Sequence[Entry]) -> DerivedState:
    """Compute ``DerivedState`` from a session's entries, in log order."""
    messages: list[AgentMessage] = []
    requested: dict[str, PendingInputRequest] = {}
    resolved_request_ids: set[str] = set()

    for entry in entries:
        if entry.type == ENTRY_TYPE_MESSAGE:
            messages.append(entry_to_message(entry))
        elif entry.type == ENTRY_TYPE_INPUT_REQUESTED:
            request = entry_to_request(entry)
            requested[request.id] = request
        elif entry.type == ENTRY_TYPE_INPUT_RESOLVED:
            resolution = entry_to_resolution(entry)
            resolved_request_ids.add(resolution.request_id)
        # Unknown entry types are ignored rather than raising: durable
        # history should stay readable by older code as new entry types
        # are introduced.

    pending = tuple(
        request
        for request_id, request in requested.items()
        if request_id not in resolved_request_ids
    )
    status: SessionStatus = "waiting" if pending else "idle"
    return DerivedState(status=status, pending_requests=pending, messages=tuple(messages))


def rehydrate(store: SessionStore, session_id: str) -> DerivedState:
    """Read a session's entries and derive its current state.

    This is the one code path crash recovery, HITL resume, and (later)
    child-park recovery all share.
    """
    return derive_state(store.entries(session_id))


def harness_from_session(
    store: SessionStore, session_id: str, config: AgentHarnessConfig
) -> AgentHarness:
    """Build an ``AgentHarness`` whose state matches a session's durable log.

    Rehydrated messages seed the harness's history and rehydrated pending
    requests are seeded via ``AgentHarness.seed_pending_requests`` so
    ``harness.state`` reports ``"waiting"``/``"idle"`` exactly as
    ``rehydrate`` derived it, without running anything.
    """
    derived = rehydrate(store, session_id)
    harness = AgentHarness(config, messages=derived.messages)
    harness.seed_pending_requests(derived.pending_requests)
    return harness


__all__ = [
    "DerivedState",
    "SessionStatus",
    "derive_state",
    "harness_from_session",
    "rehydrate",
]
