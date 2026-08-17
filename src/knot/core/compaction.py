"""The compaction engine: boundary selection and summarization.

Pure pieces only — no store access, no harness/runtime wiring, mirroring
the same "pure core, policy in the runtime" split ``knot.core.loop`` /
``knot.authoring.runtime`` already draw for everything else. The durable
append, the harness's ``pre_turn_hook`` binding, the proactive
threshold check, and the reactive overflow-retry driver all live in
``knot.authoring.runtime`` (design D3); this module only answers two pure
questions — "where should the cut go?" and "what does the summary say?" —
plus the settings shape both callers configure it with.

Token costs everywhere in this module are *estimates* (see
``_estimate_message_tokens``): cheap, conservative, and only ever used to
pick *where* to cut or to decide whether a summary shrank its span. They
are never treated as an accurate token count — the provider's own reported
usage (``knot.providers.messages.Usage``, consumed by ``context_pressure``)
is the only number this system trusts for "how close are we to the limit".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from knot.providers.events import AssistantDoneEvent, AssistantErrorEvent
from knot.providers.messages import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from knot.providers.provider import CancellationToken, ModelProvider, ToolSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompactionSettings:
    """One session's resolved compaction configuration.

    Mirrors ``knot.authoring.config.CompactionConfig`` field for field —
    that's the author-facing wire schema (validated, ``agent.yaml``-shaped);
    this is the plain dataclass the pure engine and the runtime's wiring
    actually pass around, so this module never needs to import
    ``knot.authoring`` (which would invert the package layering).
    """

    enabled: bool = True
    #: Proactive trigger: compact when the latest reported context usage is
    #: at or above this fraction of the model's context window.
    threshold_ratio: float = 0.8
    #: Fraction of the context window kept verbatim as the retained tail
    #: (estimated conservatively — see ``select_boundary``).
    retain_budget: float = 0.16
    #: ``None`` (the default) reuses the session's own model for the
    #: summarization call.
    summarization_model: str | None = None
    max_overflow_retries: int = 1
    summarization_max_tokens: int = 8192


class CompactionRejected(Exception):
    """A compaction attempt failed and must leave the conversation untouched.

    Raised by ``summarize`` (empty/tool-call/error response) and by
    callers (``knot.authoring.runtime``) that additionally reject a summary
    which failed to shrink its span (see ``validate_shrink``). Carries
    ``reason`` as a plain string so callers can log it without parsing
    ``str(exc)``.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def context_pressure(messages: Sequence[AgentMessage]) -> int | None:
    """The provider's own statement of the current context cost, or ``None``.

    ``input + cache_read + cache_write`` of the *last* ``AssistantMessage``
    that reported nonzero usage — walking from the tail so a later message
    with usage always wins over an earlier one, and skipping any message
    with no usage at all (a scripted test reply, a message built before a
    real request ever completed) rather than treating zero as "the context
    is empty". ``None`` means no such message exists yet, which the caller
    (the proactive pre-turn hook) must treat as "pressure unknown, don't
    trigger" — exactly the same as an unknown context window (design D2:
    "no capacity ⇒ proactive trigger disabled").
    """
    for message in reversed(messages):
        if not isinstance(message, AssistantMessage):
            continue
        usage = message.usage
        total = usage.input + usage.cache_read + usage.cache_write
        if total > 0:
            return total
    return None


#: Small fixed overhead per message added to the length-based estimate,
#: standing in for role/structure tokens a plain character count misses.
#: Deliberately not tuned against a real tokenizer — see the module
#: docstring: this only ever decides *where* to cut, never *whether* the
#: outcome is acceptable.
_ESTIMATE_OVERHEAD_TOKENS = 8


def _message_estimate_text(message: AgentMessage) -> str:
    if isinstance(message, AssistantMessage):
        parts = [message.text, message.thinking_text]
        for call in message.tool_calls:
            parts.append(call.name)
            parts.append(json.dumps(call.arguments, default=str))
        return "".join(parts)
    return message.text


def _estimate_message_tokens(message: AgentMessage) -> int:
    """Conservative per-message token estimate.

    Prefers the provider's own reported output-token count when this
    message actually carries one (an ``AssistantMessage`` with nonzero
    ``usage.output`` — a real, completed response, not a scripted or
    zero-usage one); otherwise falls back to a plain ``len(text) // 4``
    character estimate plus ``_ESTIMATE_OVERHEAD_TOKENS``.
    """
    if isinstance(message, AssistantMessage) and message.usage.output > 0:
        return message.usage.output
    return len(_message_estimate_text(message)) // 4 + _ESTIMATE_OVERHEAD_TOKENS


def _estimate_span_tokens(messages: Sequence[AgentMessage]) -> int:
    return sum(_estimate_message_tokens(message) for message in messages)


def _atomic_units(pairs: Sequence[tuple[int, AgentMessage]]) -> list[tuple[int, int]]:
    """Group ``pairs`` into contiguous ``[start, end)`` index spans that a
    compaction boundary must never fall inside.

    A lone message is its own one-element unit. An ``AssistantMessage``
    with tool calls is grouped with every ``ToolResultMessage`` answering
    one of those calls that immediately follows it — the exact "tool call
    plus its tool results" pairing the spec requires stay whole (history
    reaching this function has already been through
    ``knot.core.tool_history.repair_tool_history`` at some point upstream,
    so those results are always contiguous).
    """
    units: list[tuple[int, int]] = []
    index = 0
    total = len(pairs)
    while index < total:
        message = pairs[index][1]
        if isinstance(message, AssistantMessage) and message.tool_calls:
            call_ids = {call.id for call in message.tool_calls}
            end = index + 1
            while (
                end < total
                and isinstance(pairs[end][1], ToolResultMessage)
                and pairs[end][1].tool_call_id in call_ids
            ):
                end += 1
            units.append((index, end))
            index = end
        else:
            units.append((index, index + 1))
            index += 1
    return units


def _snap_to_unit_start(units: Sequence[tuple[int, int]], index: int) -> int:
    for start, end in units:
        if start <= index < end:
            return start
    return index


def select_boundary(pairs: Sequence[tuple[int, AgentMessage]], retain_tokens: float) -> int | None:
    """Pick the durable ``seq`` of the last message a compaction should
    cover, or ``None`` if there is nothing worth compacting.

    ``pairs`` is the seq-aligned view of a session's currently-derived
    history — ``(entry_seq, message)`` in log order, exactly
    ``zip(DerivedState.message_seqs, DerivedState.messages)`` (see
    ``knot.core.session.state.DerivedState``). ``retain_tokens`` is the
    estimated token budget the retained tail should stay under (typically
    ``retain_budget * context_window``, computed by the caller).

    The returned boundary is **seq-upward-closed**: the compacted span is
    exactly ``{message : seq <= boundary}``, matching how both
    ``derive_state``'s compaction fold and the runtime's split apply it.
    After a prior compaction the seqs in ``pairs`` are non-monotonic (the
    prior summary sits at position 0 under its compaction entry's high seq,
    ahead of retained messages with lower seqs), so the boundary is the
    *max* seq of the compacted prefix, not the seq of its last element —
    otherwise the prior summary would leak into the retained tail and stale
    summaries would accumulate on every subsequent compaction.

    Algorithm: walk from the tail accumulating ``_estimate_message_tokens``
    until the budget would be exceeded (always retaining at least the very
    last message, whatever the budget, so a pathologically small budget
    never produces an empty retained tail); widen the resulting cut so it
    never splits an assistant message from its tool results (see
    ``_atomic_units``) and never lands after the most recent user message
    (the spec's "latest user turn kept intact"); finally reject a boundary
    that would compact fewer than two messages — not enough to be worth a
    summarization round trip.

    Two consequences of the upward closure, both accepted as the minimal
    valid behavior:

    - The retain budget is approximate right after a prior compaction:
      retained messages whose seqs fall below the prior summary's seq are
      pulled into the compacted span even when the budget walk kept them.
    - The latest-user-turn guard is best-effort: when the most recent user
      message *predates* the prior compaction, every nonempty prefix
      contains the prior summary's higher seq, so no upward-closed boundary
      can compact anything while retaining that user message. Compaction
      proceeds (the stale user turn is summarized) rather than being
      skipped, which would otherwise disable compaction for the session's
      lifetime and defeat reactive overflow recovery.
    """
    total = len(pairs)
    if total < 2:
        return None

    tail_start = total
    accumulated = 0
    for index in range(total - 1, -1, -1):
        cost = _estimate_message_tokens(pairs[index][1])
        if accumulated + cost > retain_tokens and tail_start < total:
            break
        accumulated += cost
        tail_start = index

    units = _atomic_units(pairs)
    tail_start = _snap_to_unit_start(units, tail_start)

    def closed_boundary(prefix_len: int) -> int:
        return max(seq for seq, _ in pairs[:prefix_len])

    last_user = next(
        (
            (i, pairs[i][0])
            for i in range(total - 1, -1, -1)
            if isinstance(pairs[i][1], UserMessage)
        ),
        None,
    )
    if last_user is not None and tail_start >= 1:
        last_user_index, last_user_seq = last_user
        if closed_boundary(tail_start) >= last_user_seq:
            pulled = _snap_to_unit_start(units, last_user_index)
            if pulled == 0 or closed_boundary(pulled) < last_user_seq:
                tail_start = pulled
            # else: guard unsatisfiable — a prior summary (tracked under a
            # higher seq) sits in every nonempty prefix; compact anyway.

    if tail_start < 2:
        return None

    return closed_boundary(tail_start)


def validate_shrink(replaced: Sequence[AgentMessage], summary: AgentMessage) -> bool:
    """True iff ``summary`` is estimated smaller than the span it replaces.

    The spec's shrink check compares "summary plus retained tail" against
    "the history it replaces" — the retained tail is identical on both
    sides of that comparison and cancels out, so this only needs to compare
    the summary against the compacted span itself, with the same estimator
    ``select_boundary`` used to choose the span in the first place.
    """
    return _estimate_message_tokens(summary) < _estimate_span_tokens(replaced)


_SUMMARY_INSTRUCTION = (
    "Summarize the conversation above so it can be dropped from context and "
    "replaced by this summary. Cover: the current state of the task, "
    "decisions already made, open threads or questions still unresolved, "
    "and concrete facts (names, ids, values, file paths) worth keeping. Be "
    "concise but do not omit anything a continuation of this conversation "
    "would need. Respond with plain text only — do not call any tool."
)


async def summarize(
    *,
    provider: ModelProvider,
    model: str,
    system: str,
    tools: Sequence[ToolSpec],
    messages_to_compact: Sequence[AgentMessage],
    max_tokens: int,
    session_id: str | None = None,
    signal: CancellationToken | None = None,
) -> UserMessage:
    """Summarize ``messages_to_compact`` in one provider call.

    Replays the session's own ``system`` prompt, ``tools``, and the
    messages being compacted verbatim (any prior compaction's summary is
    already the first element of ``messages_to_compact`` when there is
    one — compaction composes for free, see
    ``knot.core.session.state``'s "Compaction folding" note), with the
    summarize instruction appended as one final ``UserMessage`` — this
    exact prefix match is what lets the provider serve the request from its
    warm prompt cache (design D5).

    Raises ``CompactionRejected`` for every failure mode the spec calls
    out: a provider error, an empty response, or a response containing
    tool calls (this call must be text-only). On success, returns the
    summary wrapped as a ``UserMessage`` in ``<compacted-summary>`` tags —
    already in the shape ``knot.core.session.entries.Compaction`` stores it
    and every downstream surface (rehydration, export, the
    model-visible-logged invariant) already knows how to handle.

    ``max_tokens`` is accepted for the config surface
    (``CompactionSettings.summarization_max_tokens``) but not currently
    forwarded to the provider call: ``knot.providers.provider.ModelProvider
    .stream_response`` has no per-call token-limit parameter today — every
    adapter bakes its max-tokens ceiling in at construction time instead.
    Accepted here (rather than dropped from the signature) so this
    function's contract already has the parameter a future per-call
    override would need, and so a caller reading this signature sees the
    setting is honored where the provider layer allows it.
    """
    del max_tokens  # see docstring: no per-call override exists yet.
    request_messages: list[AgentMessage] = [
        *messages_to_compact,
        UserMessage(content=_SUMMARY_INSTRUCTION),
    ]

    assistant: AssistantMessage | None = None
    async for event in provider.stream_response(
        model=model,
        system=system,
        messages=request_messages,
        tools=tools,
        signal=signal,
        session_id=session_id,
    ):
        if isinstance(event, AssistantDoneEvent):
            assistant = event.message
        elif isinstance(event, AssistantErrorEvent):
            raise CompactionRejected(
                f"summarization request failed: {event.error.error_message or 'unknown error'}"
            )

    if assistant is None:
        raise CompactionRejected("summarization request produced no response")
    if assistant.stop_reason in {"error", "aborted"}:
        raise CompactionRejected(f"summarization response stopped with {assistant.stop_reason!r}")
    if assistant.tool_calls:
        raise CompactionRejected("summarization response contained tool calls")
    text = assistant.text.strip()
    if not text:
        raise CompactionRejected("summarization response was empty")

    return UserMessage(content=f"<compacted-summary>\n{text}\n</compacted-summary>")


__all__ = [
    "CompactionRejected",
    "CompactionSettings",
    "context_pressure",
    "select_boundary",
    "summarize",
    "validate_shrink",
]
