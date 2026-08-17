"""Unit tests for the pure compaction engine (``knot.core.compaction``).

Covers boundary selection (tail retention, tool-pair widening, the
fewer-than-two-messages no-op, and the latest-user-turn guard),
``context_pressure``, the summarization call's prefix-replay request shape
and outcome validation (text-only, non-empty, shrink check), all against
``FakeProvider`` — no store, no harness, matching the module's "pure pieces
only" docstring.
"""

from __future__ import annotations

import pytest

from knot.core.compaction import (
    CompactionRejected,
    context_pressure,
    select_boundary,
    summarize,
    validate_shrink,
)
from knot.providers.fake import FakeProvider, error, reply, tool_call
from knot.providers.messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)


def _pairs(*messages: object) -> list[tuple[int, object]]:
    """Seq-align a plain message list starting at seq 0, one per message —
    the shape ``select_boundary`` expects (``zip(message_seqs, messages)``).
    """
    return list(enumerate(messages))


# -- select_boundary -------------------------------------------------------


def test_select_boundary_none_when_fewer_than_two_messages() -> None:
    assert select_boundary(_pairs(UserMessage(content="only one")), retain_tokens=1000) is None
    assert select_boundary([], retain_tokens=1000) is None


def test_select_boundary_none_when_everything_fits_in_retain_budget() -> None:
    pairs = _pairs(UserMessage(content="a"), UserMessage(content="b"))
    # A huge retain budget keeps the entire (two-message) history as the
    # tail, leaving nothing worth compacting.
    assert select_boundary(pairs, retain_tokens=10_000) is None


def test_select_boundary_covers_oldest_span_keeps_recent_tail() -> None:
    messages = [UserMessage(content="x" * 400) for _ in range(6)]
    pairs = _pairs(*messages)
    # Each message is ~100+8 estimated tokens; a small budget should only
    # retain the very last one or two messages.
    boundary = select_boundary(pairs, retain_tokens=50)
    assert boundary is not None
    # The boundary seq must leave at least the final message uncompacted.
    assert boundary < pairs[-1][0]


def test_select_boundary_never_splits_assistant_from_its_tool_results() -> None:
    call = ToolCall(id="c1", name="do_thing", arguments={})
    assistant = AssistantMessage(content=[call], stop_reason="toolUse")
    result = ToolResultMessage(tool_call_id="c1", tool_name="do_thing", content=[])
    messages = [
        UserMessage(content="setup"),
        assistant,
        result,
        UserMessage(content="latest turn"),
    ]
    pairs = _pairs(*messages)
    # A retain budget that would otherwise land the cut between the
    # assistant's tool call and its result (index 2, i.e. seq 2) must widen
    # backward so the pair stays together on the retained side.
    boundary = select_boundary(pairs, retain_tokens=1)
    assert boundary is not None
    # Either the whole tool-call/result pair is retained (boundary < 1) or
    # compacted together (boundary >= 2) — never split (boundary == 1,
    # which would separate seq 1 from seq 2).
    assert boundary != 1


def test_select_boundary_never_lands_after_the_latest_user_message() -> None:
    messages = [
        UserMessage(content="old " * 200),
        AssistantMessage(content=[], stop_reason="stop"),
        UserMessage(content="the latest user turn"),
    ]
    pairs = _pairs(*messages)
    # Even a tiny retain budget must not cut past (or including) the latest
    # user message — it and everything after it stays on the retained side.
    boundary = select_boundary(pairs, retain_tokens=1)
    assert boundary is not None
    assert boundary < pairs[2][0]


def test_select_boundary_rejects_when_widening_leaves_fewer_than_two_compacted() -> None:
    # Only one message precedes the latest user turn's tool pair, so once
    # the boundary is widened to respect both guards, fewer than two
    # messages would be covered — not worth compacting.
    call = ToolCall(id="c1", name="t", arguments={})
    assistant = AssistantMessage(content=[call], stop_reason="toolUse")
    result = ToolResultMessage(tool_call_id="c1", tool_name="t", content=[])
    messages = [UserMessage(content="x"), assistant, result]
    pairs = _pairs(*messages)
    boundary = select_boundary(pairs, retain_tokens=1)
    assert boundary is None


def test_select_boundary_is_seq_closed_after_prior_compaction() -> None:
    # After a compaction, derive_state tracks the summary at list position 0
    # under the compaction entry's own (high) seq, ahead of retained
    # messages with lower seqs — the seq view is non-monotonic. The
    # boundary must be upward-closed (the max seq of the compacted prefix),
    # or the seq-filtered split would leave the prior summary in the
    # retained tail.
    summary = UserMessage(content="<compacted-summary>\nearlier\n</compacted-summary>")
    old = [UserMessage(content="x" * 400) for _ in range(4)]
    latest = UserMessage(content="latest turn")
    pairs = [(10, summary), (6, old[0]), (7, old[1]), (8, old[2]), (9, old[3]), (11, latest)]

    boundary = select_boundary(pairs, retain_tokens=150)
    assert boundary == 10  # the prior summary's seq, not a stale tail seq (8)

    # The seq-filtered compacted span (exactly how the runtime and
    # derive_state apply the boundary) is a contiguous positional prefix
    # that includes the prior summary.
    compacted = [message for seq, message in pairs if seq <= boundary]
    assert compacted == [message for _, message in pairs[: len(compacted)]]
    assert compacted[0] is summary


def test_select_boundary_retains_recent_user_turn_after_prior_compaction() -> None:
    summary = UserMessage(content="<compacted-summary>\nearlier\n</compacted-summary>")
    old = [UserMessage(content="x" * 400) for _ in range(3)]
    latest = UserMessage(content="latest turn")
    trailing = AssistantMessage(content=[], stop_reason="stop")
    pairs = [(10, summary), (6, old[0]), (7, old[1]), (8, old[2]), (11, latest), (12, trailing)]

    # A tiny budget wants to retain only the trailing assistant message,
    # but the latest user turn (seq 11, newer than the prior compaction)
    # must stay on the retained side of the seq-closed boundary.
    boundary = select_boundary(pairs, retain_tokens=1)
    assert boundary == 10
    assert boundary < 11


def test_select_boundary_compacts_when_stale_user_turn_unretainable() -> None:
    # When the most recent user message predates the prior compaction, its
    # seq sits below the prior summary's, so every upward-closed boundary
    # that compacts anything swallows it. Compaction must still proceed
    # (summarizing the stale user turn) rather than return None, which
    # would disable compaction for the session's lifetime.
    summary = UserMessage(content="<compacted-summary>\nearlier\n</compacted-summary>")
    old = [UserMessage(content="x" * 400) for _ in range(3)]
    stale_user = UserMessage(content="stale latest user")
    pairs = [
        (10, summary),
        (6, old[0]),
        (7, old[1]),
        (8, old[2]),
        (9, stale_user),
        (11, AssistantMessage(content=[], stop_reason="stop")),
        (12, AssistantMessage(content=[], stop_reason="stop")),
    ]

    boundary = select_boundary(pairs, retain_tokens=60)
    assert boundary == 10
    assert 9 <= boundary  # the stale user turn lands in the compacted span


# -- context_pressure -------------------------------------------------------


def test_context_pressure_none_with_no_usage_bearing_assistant_message() -> None:
    assert context_pressure([UserMessage(content="hi")]) is None
    zero_usage = AssistantMessage(content=[], stop_reason="stop")
    assert context_pressure([zero_usage]) is None


def test_context_pressure_uses_the_latest_usage_bearing_message() -> None:
    old = AssistantMessage(
        content=[], stop_reason="stop", usage=Usage(input=100, cache_read=0, cache_write=0)
    )
    new = AssistantMessage(
        content=[], stop_reason="stop", usage=Usage(input=50, cache_read=25, cache_write=25)
    )
    assert context_pressure([old, UserMessage(content="more"), new]) == 100


# -- summarize ---------------------------------------------------------------


async def test_summarize_replays_system_tools_and_span_with_instruction_appended() -> None:
    provider = FakeProvider([reply("state: done. decisions: none. threads: none. facts: none.")])
    to_compact = [UserMessage(content="one"), UserMessage(content="two")]

    summary = await summarize(
        provider=provider,
        model="m",
        system="the system prompt",
        tools=[],
        messages_to_compact=to_compact,
        max_tokens=1000,
        session_id="sess_1",
    )

    assert isinstance(summary, UserMessage)
    assert summary.text.startswith("<compacted-summary>")
    assert summary.text.endswith("</compacted-summary>")

    assert len(provider.calls) == 1
    model, system, messages, tools = provider.calls[0]
    assert model == "m"
    assert system == "the system prompt"
    assert tools == []
    # Prefix-replay shape: the exact span being compacted, verbatim, then
    # exactly one final UserMessage carrying the summarize instruction.
    assert messages[:2] == to_compact
    assert len(messages) == 3
    assert isinstance(messages[2], UserMessage)
    assert messages[2] is not to_compact[-1]


async def test_summarize_rejects_response_containing_tool_calls() -> None:
    provider = FakeProvider([tool_call("some_tool", {})])
    with pytest.raises(CompactionRejected):
        await summarize(
            provider=provider,
            model="m",
            system="sys",
            tools=[],
            messages_to_compact=[UserMessage(content="a")],
            max_tokens=100,
        )


async def test_summarize_rejects_empty_response() -> None:
    provider = FakeProvider([reply("")])
    with pytest.raises(CompactionRejected):
        await summarize(
            provider=provider,
            model="m",
            system="sys",
            tools=[],
            messages_to_compact=[UserMessage(content="a")],
            max_tokens=100,
        )


async def test_summarize_rejects_provider_error() -> None:
    provider = FakeProvider([error("boom")])
    with pytest.raises(CompactionRejected):
        await summarize(
            provider=provider,
            model="m",
            system="sys",
            tools=[],
            messages_to_compact=[UserMessage(content="a")],
            max_tokens=100,
        )


# -- validate_shrink ----------------------------------------------------------


def test_validate_shrink_accepts_a_smaller_summary() -> None:
    replaced = [UserMessage(content="x" * 4000)]
    summary = UserMessage(content="<compacted-summary>short</compacted-summary>")
    assert validate_shrink(replaced, summary) is True


def test_validate_shrink_rejects_a_non_shrinking_summary() -> None:
    replaced = [UserMessage(content="short")]
    summary = UserMessage(content="<compacted-summary>" + "y" * 4000 + "</compacted-summary>")
    assert validate_shrink(replaced, summary) is False
