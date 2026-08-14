"""The harness-event subscriber that durably persists a session.

``PersistenceSubscriber`` is a plain callable suitable for
``AgentHarness.subscribe(...)``: it turns ``MessageEndEvent`` into
``"message"`` entries and, on a parked run (``AgentEndEvent`` with outcome
``waiting_input``), one ``"input_requested"`` entry per pending request. It
ignores every other event in the grammar — those are streaming/UI
concerns, not durable facts.

It claims exclusive write ownership of its session for its lifetime (see
``SessionStore.claim_writer``), so a second subscriber for the same session
cannot be constructed while the first is still live — the store-level
invariant that only one writer ever appends to a given session's log.
"""

from __future__ import annotations

from knot.core.events import AgentEndEvent, AgentEvent, MessageEndEvent

from .entries import ENTRY_TYPE_INPUT_REQUESTED, ENTRY_TYPE_MESSAGE
from .store import SessionStore, WriterClaim


class PersistenceSubscriber:
    """Callable session-persistence listener; the sole writer for one session."""

    def __init__(self, store: SessionStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self._claim: WriterClaim = store.claim_writer(session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    def __call__(self, event: AgentEvent) -> None:
        if isinstance(event, MessageEndEvent):
            self._store.append_entry(
                self._session_id,
                ENTRY_TYPE_MESSAGE,
                event.message.model_dump(by_alias=True),
            )
        elif isinstance(event, AgentEndEvent) and event.outcome == "waiting_input":
            for request in event.pending_requests:
                self._store.append_entry(
                    self._session_id,
                    ENTRY_TYPE_INPUT_REQUESTED,
                    request.model_dump(by_alias=True),
                )

    def release(self) -> None:
        """Release this subscriber's writer claim, freeing the session for another."""
        self._claim.release()

    def __enter__(self) -> PersistenceSubscriber:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


__all__ = ["PersistenceSubscriber"]
