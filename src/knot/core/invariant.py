"""The model-visible-means-logged invariant: canonical projection and check.

Knot's durability story rests on one implicit contract: everything the
model sees must be reconstructable from the session's durable entry log,
because park/resume, crash repair, ``once`` approvals, and restart recovery
all rehydrate from that log. A code path that appends a message to the
in-memory harness without persisting it (or persists something rehydration
reconstructs differently) causes *silent replay divergence* — the resumed
session sees different history than the live one did, and nothing notices
until much later, far from the cause.

This module makes that contract explicit and checkable:

- ``canonical_history`` projects a message list the same way a live
  provider request is built (``knot.core.tool_history.provider_context``),
  then strips fields the provider never sees, so a difference confined to
  those fields is never mistaken for divergence.
- ``diff_histories`` compares two canonical projections and reports the
  first point they disagree, with enough detail to debug it.
- ``build_invariant_hook`` wires the check to a session's durable store as a
  ``pre_request_hook`` (see ``knot.core.loop.run_agent_loop``): before every
  provider request, it rehydrates the log side via ``derive_state`` and
  compares it against the in-memory side the request is about to be built
  from. ``strict`` raises ``HistoryDivergenceError`` (which the loop routes
  into its existing error-outcome path, before any provider call is made);
  ``warn`` logs the report and lets the run continue; ``off`` installs no
  hook at all.

Import-cycle note: ``knot.core.session`` (needed by the checker for
``derive_state``/``SessionStore``) imports ``knot.core.harness``, which
imports ``knot.core.loop`` — and ``loop`` imports ``HistoryDivergenceError``
from *this* module to route a raised divergence into its error path. A
module-level import of ``knot.core.session`` here would therefore close a
load-time cycle back through ``loop``. ``build_invariant_hook`` sidesteps it
with a deferred (call-time, not import-time) import — by the time anything
actually calls it, every module above is already fully loaded.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from knot.core.tool_history import provider_context
from knot.providers.messages import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    ToolResultMessage,
    UserMessage,
)
from knot.providers.types import JSONValue

if TYPE_CHECKING:  # pragma: no cover - type-only, see module docstring
    from knot.core.session.store import Entry, SessionStore

logger = logging.getLogger(__name__)

# Duplicates knot.core.session.entries.ENTRY_TYPE_MESSAGE as a literal
# rather than importing it, for the same reason described in the module
# docstring: importing anything under knot.core.session at module scope
# here would close a load-time cycle back through knot.core.loop.
_ENTRY_TYPE_MESSAGE = "message"

# Fields that never reach a provider payload: local bookkeeping, telemetry,
# and wall-clock timestamps the provider adapter never puts on the wire. A
# difference confined to these fields can never cause the model to see
# something different, so the invariant check must never treat it as
# divergence. Every message type drops `timestamp`; `AssistantMessage` and
# `ToolResultMessage` additionally drop fields specific to them.
PROVIDER_INVISIBLE_FIELDS: Mapping[type[AgentMessage], frozenset[str]] = {
    UserMessage: frozenset({"timestamp"}),
    AssistantMessage: frozenset({"timestamp", "usage", "diagnostics", "response_id"}),
    ToolResultMessage: frozenset({"timestamp", "details"}),
    CustomMessage: frozenset({"timestamp"}),
}


def canonical_history(messages: Sequence[AgentMessage]) -> list[dict[str, JSONValue]]:
    """Project ``messages`` to the form the invariant check compares.

    Applies ``provider_context`` — the same normalization a live request is
    built from — and then dumps each resulting message to plain JSON with
    provider-invisible fields removed (see ``PROVIDER_INVISIBLE_FIELDS``),
    so a difference confined to those fields is never reported as
    divergence.
    """
    return [_canonical_message(message) for message in provider_context(messages)]


def _canonical_message(message: AgentMessage) -> dict[str, JSONValue]:
    exclude = PROVIDER_INVISIBLE_FIELDS.get(type(message), frozenset({"timestamp"}))
    return message.model_dump(mode="json", by_alias=True, exclude=exclude)


DivergenceKind = Literal["memory_extra", "log_extra", "field_mismatch"]


@dataclass(frozen=True, slots=True)
class DivergenceReport:
    """Where and how two canonical message histories first diverge.

    ``index`` is the position in the canonical (provider-context-projected)
    message list. ``entry_seq`` is the durable entry seq at that position
    when it is derivable — ``provider_context``'s own filtering/repair can
    shift canonical indices relative to the raw entry log, so this is
    reported best-effort ("where derivable", per the spec), never
    guaranteed. ``kind`` names which side has the extra or differing
    message; ``field_diff`` maps field name to ``(log_value, memory_value)``
    for the first differing message (empty for an extra-message divergence,
    since there is no counterpart to diff fields against).
    """

    index: int
    entry_seq: int | None
    kind: DivergenceKind
    summary: str
    field_diff: Mapping[str, tuple[JSONValue, JSONValue]] = field(default_factory=dict)


class HistoryDivergenceError(RuntimeError):
    """Raised in strict mode when the invariant check finds a divergence.

    Carries the structured ``DivergenceReport`` so callers (the loop's
    error-outcome path, tests, operators reading logs) can inspect exactly
    what diverged instead of parsing a message string.
    """

    def __init__(self, report: DivergenceReport) -> None:
        super().__init__(f"model-visible-logged invariant violated: {report.summary}")
        self.report = report


InvariantMode = Literal["strict", "warn", "off"]


def diff_histories(
    memory_messages: Sequence[AgentMessage],
    log_messages: Sequence[AgentMessage],
    log_entries: Sequence[Entry] = (),
) -> DivergenceReport | None:
    """Return the first divergence between two histories' canonical forms.

    ``None`` means the histories project to the same canonical form — the
    check passes silently. ``log_entries`` is optional and used only to
    resolve ``DivergenceReport.entry_seq``; pass ``()`` when it isn't
    available (e.g. a projection-only comparison in a test).
    """
    memory_canonical = canonical_history(memory_messages)
    log_canonical = canonical_history(log_messages)
    seq_by_index = _message_entry_seqs(log_entries)

    common_length = min(len(memory_canonical), len(log_canonical))
    for index in range(common_length):
        memory_message = memory_canonical[index]
        log_message = log_canonical[index]
        if memory_message == log_message:
            continue
        keys = set(memory_message) | set(log_message)
        field_diff = {
            key: (log_message.get(key), memory_message.get(key))
            for key in keys
            if memory_message.get(key) != log_message.get(key)
        }
        return DivergenceReport(
            index=index,
            entry_seq=seq_by_index.get(index),
            kind="field_mismatch",
            summary=f"message at index {index} differs between the log and in-memory history",
            field_diff=field_diff,
        )

    if len(memory_canonical) == len(log_canonical):
        return None

    index = common_length
    kind: DivergenceKind = (
        "memory_extra" if len(memory_canonical) > len(log_canonical) else "log_extra"
    )
    side = "in-memory" if kind == "memory_extra" else "log"
    return DivergenceReport(
        index=index,
        entry_seq=seq_by_index.get(index),
        kind=kind,
        summary=f"{side} history has an extra message at index {index}",
    )


def _message_entry_seqs(entries: Sequence[Entry]) -> dict[int, int]:
    """Best-effort canonical-index -> durable entry seq map.

    The Nth ``"message"`` entry corresponds to the Nth message
    ``derive_state`` reconstructs from the log. See ``DivergenceReport`` for
    why this mapping is best-effort rather than exact.
    """
    return {
        index: entry.seq
        for index, entry in enumerate(e for e in entries if e.type == _ENTRY_TYPE_MESSAGE)
    }


def build_invariant_hook(
    store: SessionStore, session_id: str, mode: InvariantMode = "strict"
) -> Callable[[Sequence[AgentMessage]], Awaitable[None]] | None:
    """Build a ``pre_request_hook`` (see ``run_agent_loop``) for one session.

    ``mode="off"`` returns ``None`` outright — the loop treats a ``None``
    hook as a plain no-op, so this costs nothing beyond the one attribute
    check, matching the "cheap when disabled" goal. ``strict`` and ``warn``
    both rehydrate the session's durable log via ``derive_state`` and
    compare it against the in-memory messages the hook is called with;
    ``strict`` raises ``HistoryDivergenceError`` on a divergence, ``warn``
    logs it and returns.
    """
    if mode == "off":
        return None

    # Deferred on purpose: see the module docstring's import-cycle note.
    from knot.core.session.state import derive_state

    async def hook(messages: Sequence[AgentMessage]) -> None:
        entries = store.entries(session_id)
        log_messages = derive_state(entries).messages
        report = diff_histories(messages, log_messages, entries)
        if report is None:
            return
        if mode == "strict":
            raise HistoryDivergenceError(report)
        logger.warning("model-visible-logged invariant diverged: %s", report)

    return hook


__all__ = [
    "PROVIDER_INVISIBLE_FIELDS",
    "DivergenceKind",
    "DivergenceReport",
    "HistoryDivergenceError",
    "InvariantMode",
    "build_invariant_hook",
    "canonical_history",
    "diff_histories",
]
