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

Compaction folding: a ``"compaction"`` entry does not add a message of its
own to the accumulated list. Instead it drops every accumulated message
whose *originating* entry ``seq`` is ``<= covers_through_seq`` and prepends
its ``summary_message`` in their place, tracked internally under the
compaction entry's own ``seq``. Tracking the summary under its own entry's
seq (rather than, say, the seq of the span it replaces) is what makes
repeated compactions compose for free: a later compaction's
``covers_through_seq`` is simply a higher number, and since it is
necessarily >= the earlier compaction entry's own seq, folding it drops the
prior summary right along with the rest of the span it now also covers —
no special-casing needed to detect "this accumulated message is itself a
prior summary".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from knot.core.events import PendingInputRequest
from knot.core.harness import AgentHarness, AgentHarnessConfig
from knot.providers.messages import AgentMessage

from .entries import (
    ENTRY_TYPE_COMPACTION,
    ENTRY_TYPE_INPUT_REQUESTED,
    ENTRY_TYPE_INPUT_RESOLVED,
    ENTRY_TYPE_MESSAGE,
    entry_to_compaction,
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
    #: The originating entry ``seq`` for each element of ``messages``, in
    #: the same order — ``message_seqs[i]`` is the durable coordinate
    #: ``messages[i]`` was folded from (see the module docstring's
    #: "Compaction folding" note: a summary message is tracked under its own
    #: compaction entry's seq, not the seq of the span it replaced). Context
    #: compaction (``knot.core.compaction``) needs this seq-aligned view to
    #: select a boundary in terms of the store's own stable coordinate
    #: rather than a message index that shifts as later compactions land.
    #: Defaults to ``()`` only for backward-compatible direct construction;
    #: ``derive_state`` always fills it in aligned 1:1 with ``messages``.
    message_seqs: tuple[int, ...] = ()


def derive_state(entries: Sequence[Entry]) -> DerivedState:
    """Compute ``DerivedState`` from a session's entries, in log order."""
    # Each accumulated message is tracked with the seq of the entry it
    # originated from, so a later "compaction" entry can drop the covered
    # span by seq (see the module docstring's "Compaction folding" note).
    # This bookkeeping is purely internal — DerivedState.messages stays a
    # plain message tuple.
    messages: list[tuple[int, AgentMessage]] = []
    requested: dict[str, PendingInputRequest] = {}
    resolved_request_ids: set[str] = set()

    for entry in entries:
        if entry.type == ENTRY_TYPE_MESSAGE:
            messages.append((entry.seq, entry_to_message(entry)))
        elif entry.type == ENTRY_TYPE_INPUT_REQUESTED:
            request = entry_to_request(entry)
            requested[request.id] = request
        elif entry.type == ENTRY_TYPE_INPUT_RESOLVED:
            resolution = entry_to_resolution(entry)
            resolved_request_ids.add(resolution.request_id)
        elif entry.type == ENTRY_TYPE_COMPACTION:
            compaction = entry_to_compaction(entry)
            messages = [
                (seq, message) for seq, message in messages if seq > compaction.covers_through_seq
            ]
            messages.insert(0, (entry.seq, compaction.summary_message))
        # Unknown entry types are ignored rather than raising: durable
        # history should stay readable by older code as new entry types
        # are introduced.

    pending = tuple(
        request
        for request_id, request in requested.items()
        if request_id not in resolved_request_ids
    )
    status: SessionStatus = "waiting" if pending else "idle"
    return DerivedState(
        status=status,
        pending_requests=pending,
        messages=tuple(message for _, message in messages),
        message_seqs=tuple(seq for seq, _ in messages),
    )


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
