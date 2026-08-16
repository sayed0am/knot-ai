"""Scenario tests for the pure provider/tool agent loop."""

from __future__ import annotations

import asyncio

from knot.core.decisions import Deny, RequireApproval
from knot.core.events import (
    AgentEndEvent,
    AgentEvent,
    MessageEndEvent,
    PendingInputRequest,
    ToolExecutionEndEvent,
)
from knot.core.invariant import DivergenceReport, HistoryDivergenceError
from knot.core.loop import run_agent_loop
from knot.core.repeat_guard import ADVISORY_TAG_OPEN, RepeatGuardSettings
from knot.core.tool_history import repair_tool_history
from knot.core.tools import AgentTool, AgentToolResult, ToolParkedError
from knot.providers.fake import FakeProvider, error, reply, tool_call
from knot.providers.messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from knot.providers.provider import SimpleCancellationToken


def _echo_tool(name: str) -> AgentTool:
    async def run(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content=f"{name}-result")

    return AgentTool(name=name, description="", parameters={}, execute_fn=run)


async def _collect(**kwargs) -> list[AgentEvent]:
    return [event async for event in run_agent_loop(**kwargs)]


async def test_plain_text_turn_full_event_sequence() -> None:
    provider = FakeProvider([reply("hello there")])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[],
        prompts=[UserMessage(content="hi")],
    )

    top_level_types = [event.type for event in events]
    assert top_level_types[0] == "agent_start"
    assert top_level_types[1] == "turn_start"
    assert "message_update" in top_level_types  # streamed text deltas
    assert top_level_types[-2] == "turn_end"
    assert top_level_types[-1] == "agent_end"

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"
    assert end.pending_requests == []
    assert any(
        isinstance(e, MessageEndEvent)
        and isinstance(e.message, AssistantMessage)
        and e.message.text == "hello there"
        for e in events
    )


async def test_tool_call_then_second_provider_call_completes() -> None:
    provider = FakeProvider([tool_call("get_invoice", {"id": "inv_1"}), reply("all done")])
    tool = _echo_tool("get_invoice")

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"
    assert len(provider.calls) == 2

    tool_result_messages = [
        e.message
        for e in events
        if isinstance(e, MessageEndEvent) and isinstance(e.message, ToolResultMessage)
    ]
    assert len(tool_result_messages) == 1
    assert tool_result_messages[0].text == "get_invoice-result"

    # The second provider call must see the tool result in its context.
    second_call_messages = provider.calls[1][2]
    assert any(isinstance(m, ToolResultMessage) for m in second_call_messages)


async def test_three_tool_calls_execute_concurrently() -> None:
    ready = {name: asyncio.Event() for name in ("a", "b", "c")}

    def make_tool(name: str) -> AgentTool:
        async def run(tool_call_id, arguments, signal=None, on_update=None):
            ready[name].set()
            # Only completes once every call has started: proves overlap,
            # since a sequential executor would deadlock here.
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in ready.values())), timeout=2.0
            )
            return AgentToolResult(content=name)

        return AgentTool(name=name, description="", parameters={}, execute_fn=run)

    tools = [make_tool(name) for name in ("a", "b", "c")]
    calls = [ToolCall(id=f"call_{name}", name=name, arguments={}) for name in ("a", "b", "c")]
    provider = FakeProvider([reply(tool_calls=calls), reply("done")])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=tools,
        prompts=[UserMessage(content="go")],
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"

    ends = [e for e in events if isinstance(e, ToolExecutionEndEvent)]
    assert {e.tool_call_id for e in ends} == {"call_a", "call_b", "call_c"}
    assert all(not e.is_error for e in ends)


async def test_mixed_park_two_execute_one_waits_for_approval() -> None:
    async def hook(call: ToolCall, tool: AgentTool | None):
        if call.name == "sensitive_op":
            return RequireApproval()
        from knot.core.decisions import Allow

        return Allow()

    tools = [_echo_tool("read_a"), _echo_tool("read_b"), _echo_tool("sensitive_op")]
    calls = [
        ToolCall(id="call_read_a", name="read_a", arguments={}),
        ToolCall(id="call_sensitive", name="sensitive_op", arguments={"amount": 100}),
        ToolCall(id="call_read_b", name="read_b", arguments={}),
    ]
    provider = FakeProvider([reply(tool_calls=calls)])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=tools,
        prompts=[UserMessage(content="go")],
        tool_decision_hook=hook,
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "waiting_input"
    assert len(end.pending_requests) == 1
    pending = end.pending_requests[0]
    assert pending.kind == "tool_approval"
    assert pending.tool_call_id == "call_sensitive"

    result_messages = [
        e.message
        for e in events
        if isinstance(e, MessageEndEvent) and isinstance(e.message, ToolResultMessage)
    ]
    assert {m.tool_call_id for m in result_messages} == {"call_read_a", "call_read_b"}
    assert len(provider.calls) == 1  # no second turn started while parked


async def test_execute_less_tool_parks_as_question() -> None:
    tool = AgentTool(name="ask_human", description="", parameters={}, execute_fn=None)
    call = ToolCall(id="call_ask", name="ask_human", arguments={"question": "proceed?"})
    provider = FakeProvider([reply(tool_calls=[call])])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "waiting_input"
    assert len(end.pending_requests) == 1
    pending = end.pending_requests[0]
    assert pending.kind == "question"
    assert pending.tool_call_id == "call_ask"
    assert pending.payload == {"args": {"question": "proceed?"}}


async def test_tool_that_raises_tool_parked_error_parks_like_a_gated_call() -> None:
    """A hand-built tool whose executor raises ``ToolParkedError`` mid
    execution joins the same park path as a gated or execute-less call: no
    result is synthesized, and the run ends ``waiting_input`` carrying
    exactly the ``PendingInputRequest`` the tool raised.
    """

    async def run(tool_call_id, arguments, signal=None, on_update=None):
        raise ToolParkedError(
            PendingInputRequest(
                id="req_child_1",
                kind="child_session",
                tool_call_id=tool_call_id,
                tool_name="researcher",
                payload={"childSessionId": "sess_child_1"},
            )
        )

    tool = AgentTool(name="researcher", description="", parameters={}, execute_fn=run)
    call = ToolCall(id="call_1", name="researcher", arguments={"message": "go"})
    provider = FakeProvider([reply(tool_calls=[call])])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "waiting_input"
    assert len(end.pending_requests) == 1
    request = end.pending_requests[0]
    assert request.kind == "child_session"
    assert request.id == "req_child_1"
    assert request.tool_call_id == "call_1"
    assert request.payload == {"childSessionId": "sess_child_1"}

    # No tool result was synthesized for the parked call.
    result_messages = [
        e.message
        for e in events
        if isinstance(e, MessageEndEvent) and isinstance(e.message, ToolResultMessage)
    ]
    assert result_messages == []


async def test_tool_parked_error_mixed_with_a_normal_concurrent_call() -> None:
    """A ``ToolParkedError`` from one concurrently-running call doesn't stop
    its sibling calls from completing normally in the same turn."""

    async def parking(tool_call_id, arguments, signal=None, on_update=None):
        raise ToolParkedError(
            PendingInputRequest(
                id="req_1", kind="child_session", tool_call_id=tool_call_id, tool_name="sub"
            )
        )

    async def normal(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content="ok")

    tools = [
        AgentTool(name="sub", description="", parameters={}, execute_fn=parking),
        AgentTool(name="other", description="", parameters={}, execute_fn=normal),
    ]
    calls = [
        ToolCall(id="call_sub", name="sub", arguments={}),
        ToolCall(id="call_other", name="other", arguments={}),
    ]
    provider = FakeProvider([reply(tool_calls=calls)])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=tools,
        prompts=[UserMessage(content="go")],
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "waiting_input"
    assert len(end.pending_requests) == 1
    assert end.pending_requests[0].tool_call_id == "call_sub"

    result_messages = [
        e.message
        for e in events
        if isinstance(e, MessageEndEvent) and isinstance(e.message, ToolResultMessage)
    ]
    assert {m.tool_call_id for m in result_messages} == {"call_other"}


async def test_denied_tool_call_produces_error_result_and_turn_continues() -> None:
    async def hook(call: ToolCall, tool: AgentTool | None):
        return Deny("cross-tenant access")

    tool = _echo_tool("delete_invoice")
    provider = FakeProvider([tool_call("delete_invoice", {}), reply("noted")])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        tool_decision_hook=hook,
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"

    result_messages = [
        e.message
        for e in events
        if isinstance(e, MessageEndEvent) and isinstance(e.message, ToolResultMessage)
    ]
    assert len(result_messages) == 1
    assert result_messages[0].is_error is True
    assert "cross-tenant access" in result_messages[0].text

    # The model must see the denial reason on the next turn.
    second_call_messages = provider.calls[1][2]
    denial_seen = [
        m
        for m in second_call_messages
        if isinstance(m, ToolResultMessage) and "cross-tenant access" in m.text
    ]
    assert len(denial_seen) == 1
    assert denial_seen[0].is_error is True


async def test_steering_message_drained_between_turns() -> None:
    provider = FakeProvider([tool_call("get_invoice", {}), reply("done")])
    tool = _echo_tool("get_invoice")
    steer_messages = [UserMessage(content="also check the total")]

    def get_steering():
        if steer_messages:
            return (steer_messages.pop(0),)
        return ()

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        get_steering_messages=get_steering,
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"

    second_call_messages = provider.calls[1][2]
    assert any(
        isinstance(m, UserMessage) and m.text == "also check the total"
        for m in second_call_messages
    )


async def test_follow_up_triggers_new_turn_cycle_after_tools_finish() -> None:
    provider = FakeProvider([reply("first done"), reply("second done")])
    follow_ups = [UserMessage(content="one more thing")]

    def get_follow_up():
        if follow_ups:
            return (follow_ups.pop(0),)
        return ()

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[],
        prompts=[UserMessage(content="go")],
        get_follow_up_messages=get_follow_up,
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"
    assert len(provider.calls) == 2
    second_call_messages = provider.calls[1][2]
    assert any(
        isinstance(m, UserMessage) and m.text == "one more thing" for m in second_call_messages
    )


async def test_max_turns_produces_error_message_and_ends() -> None:
    provider = FakeProvider([tool_call("get_invoice", {})])
    tool = _echo_tool("get_invoice")

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        max_turns=1,
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "error"
    assert any(
        isinstance(e, MessageEndEvent)
        and isinstance(e.message, AssistantMessage)
        and e.message.stop_reason == "error"
        and "max_turns" in (e.message.error_message or "")
        for e in events
    )


async def test_max_turns_below_one_ends_immediately_with_error() -> None:
    provider = FakeProvider([])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[],
        prompts=[UserMessage(content="go")],
        max_turns=0,
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "error"
    assert len(provider.calls) == 0


async def test_provider_error_ends_run_with_error_outcome() -> None:
    provider = FakeProvider([error("provider exploded")])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[],
        prompts=[UserMessage(content="go")],
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "error"


async def test_scripted_error_type_survives_a_full_run_agent_loop_drain() -> None:
    """The fake provider's ``error(..., error_type=...)`` scripting helper
    must yield an error AssistantMessage carrying that classification all
    the way through a real ``run_agent_loop`` drain, not just through the
    provider adapter layer in isolation."""
    provider = FakeProvider([error("too much history", error_type="context_overflow")])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[],
        prompts=[UserMessage(content="go")],
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "error"
    error_message = end.messages[-1]
    assert isinstance(error_message, AssistantMessage)
    assert error_message.error_type == "context_overflow"


async def test_provider_aborted_ends_run_with_aborted_outcome() -> None:
    provider = FakeProvider([error("stopped", reason="aborted")])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[],
        prompts=[UserMessage(content="go")],
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "aborted"


async def test_pre_request_hook_fires_once_per_provider_request() -> None:
    provider = FakeProvider([tool_call("get_invoice", {}), reply("done")])
    tool = _echo_tool("get_invoice")
    seen: list[list] = []

    async def hook(messages) -> None:
        seen.append(list(messages))

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        pre_request_hook=hook,
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"
    # One call to the hook per provider request (two turns -> two requests).
    assert len(seen) == 2 == len(provider.calls)
    # The hook sees the exact message list the request is built from: the
    # second call's snapshot must already include the first turn's results.
    assert any(isinstance(m, ToolResultMessage) for m in seen[1])


async def test_pre_request_hook_none_leaves_run_unchanged() -> None:
    provider = FakeProvider([reply("hello there")])

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[],
        prompts=[UserMessage(content="hi")],
        pre_request_hook=None,
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"


async def test_pre_request_hook_strict_divergence_ends_run_with_error_and_no_provider_call() -> (
    None
):
    provider = FakeProvider([reply("hello there")])
    report = DivergenceReport(
        index=0, entry_seq=None, kind="memory_extra", summary="synthetic test divergence"
    )

    async def hook(messages) -> None:
        raise HistoryDivergenceError(report)

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[],
        prompts=[UserMessage(content="hi")],
        pre_request_hook=hook,
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "error"
    assert len(provider.calls) == 0
    assert any(
        isinstance(e, MessageEndEvent)
        and isinstance(e.message, AssistantMessage)
        and e.message.stop_reason == "error"
        and "synthetic test divergence" in (e.message.error_message or "")
        for e in events
    )


async def test_pre_request_hook_strict_divergence_mid_run_stops_further_provider_calls() -> None:
    """Divergence detected before the *second* request must stop the run
    with zero further provider calls after that point -- the first call
    (before divergence was introduced) is allowed to have already happened.
    """
    provider = FakeProvider([tool_call("get_invoice", {}), reply("unreachable")])
    tool = _echo_tool("get_invoice")
    calls_before_hook_raises = 0

    async def hook(messages) -> None:
        nonlocal calls_before_hook_raises
        calls_before_hook_raises += 1
        if calls_before_hook_raises == 2:
            raise HistoryDivergenceError(
                DivergenceReport(
                    index=0, entry_seq=None, kind="memory_extra", summary="diverged on turn 2"
                )
            )

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        pre_request_hook=hook,
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "error"
    assert len(provider.calls) == 1  # the second provider call never happened
    assert calls_before_hook_raises == 2


async def test_cancel_mid_tool_ends_aborted_and_history_is_rehydratable() -> None:
    signal = SimpleCancellationToken()

    async def slow(tool_call_id, arguments, signal=None, on_update=None):
        await asyncio.sleep(10)
        return AgentToolResult(content="unreachable")

    async def fast(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content="fast done")

    tools = [
        AgentTool(name="slow", description="", parameters={}, execute_fn=slow),
        AgentTool(name="fast", description="", parameters={}, execute_fn=fast),
    ]
    calls = [
        ToolCall(id="call_slow", name="slow", arguments={}),
        ToolCall(id="call_fast", name="fast", arguments={}),
    ]
    provider = FakeProvider([reply(tool_calls=calls)])
    messages: list = []

    async def cancel_soon() -> None:
        await asyncio.sleep(0.05)
        signal.cancel()

    cancel_task = asyncio.create_task(cancel_soon())
    events = []
    async for event in run_agent_loop(
        provider=provider,
        model="m",
        system="s",
        messages=messages,
        tools=tools,
        prompts=[UserMessage(content="go")],
        signal=signal,
    ):
        events.append(event)
    await cancel_task

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "aborted"

    # The slow call is dangling: no result was appended to durable history.
    report = repair_tool_history(messages)
    assert report.synthesized_results == 1
    assert report.changed is True
    result_ids = {m.tool_call_id for m in report.messages if isinstance(m, ToolResultMessage)}
    assert result_ids == {"call_slow", "call_fast"}


# ---------------------------------------------------------------------------
# Repeat-tool-call guard (loop integration; the pure module itself is
# covered by tests/test_core_repeat_guard.py).
# ---------------------------------------------------------------------------


def _advisory_messages(events: list[AgentEvent]) -> list[UserMessage]:
    return [
        e.message
        for e in events
        if isinstance(e, MessageEndEvent)
        and isinstance(e.message, UserMessage)
        and ADVISORY_TAG_OPEN in e.message.text
    ]


async def test_repeat_guard_fires_advisory_delivered_at_next_turn_start() -> None:
    provider = FakeProvider(
        [
            tool_call("search", {"q": "cats"}),
            tool_call("search", {"q": "cats"}),
            tool_call("search", {"q": "cats"}),
            reply("done"),
        ]
    )
    tool = _echo_tool("search")

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        repeat_guard=RepeatGuardSettings(thresholds=(3,)),
    )

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"

    advisories = _advisory_messages(events)
    assert len(advisories) == 1

    # Delivered as an ordinary MessageStart/End pair — the same path
    # steering messages use — and appears in the *next* provider request
    # (the 4th call, index 3), not the one that crossed the threshold.
    fourth_call_messages = provider.calls[3][2]
    assert any(
        isinstance(m, UserMessage) and ADVISORY_TAG_OPEN in m.text for m in fourth_call_messages
    )
    third_call_messages = provider.calls[2][2]
    assert not any(
        isinstance(m, UserMessage) and ADVISORY_TAG_OPEN in m.text for m in third_call_messages
    )

    # Advisory-only proof: the threshold-crossing call still executed
    # normally, with an unchanged result.
    tool_result_messages = [
        e.message
        for e in events
        if isinstance(e, MessageEndEvent) and isinstance(e.message, ToolResultMessage)
    ]
    assert len(tool_result_messages) == 3
    assert all(m.text == "search-result" and not m.is_error for m in tool_result_messages)


async def test_repeat_guard_denied_calls_still_count() -> None:
    async def deny_everything(call: ToolCall, tool: AgentTool | None):
        return Deny("not allowed")

    provider = FakeProvider(
        [
            tool_call("search", {"q": "cats"}),
            tool_call("search", {"q": "cats"}),
            tool_call("search", {"q": "cats"}),
            reply("done"),
        ]
    )
    tool = _echo_tool("search")

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        tool_decision_hook=deny_everything,
        repeat_guard=RepeatGuardSettings(thresholds=(3,)),
    )

    assert len(_advisory_messages(events)) == 1


async def test_repeat_guard_excluded_tool_is_transparent_to_the_chain() -> None:
    """An identical tracked call separated by an excluded (but still
    executable) tool call keeps counting across the interleaving."""
    provider = FakeProvider(
        [
            tool_call("search", {"q": "cats"}),
            tool_call("log_note", {"note": "checked"}),
            tool_call("search", {"q": "cats"}),
            reply("done"),
        ]
    )
    tools = [_echo_tool("search"), _echo_tool("log_note")]

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=tools,
        prompts=[UserMessage(content="go")],
        repeat_guard=RepeatGuardSettings(thresholds=(2,), exclude=("log_note",)),
    )

    assert len(_advisory_messages(events)) == 1


async def test_repeat_guard_steering_resets_chain() -> None:
    provider = FakeProvider(
        [
            tool_call("search", {"q": "cats"}),
            tool_call("search", {"q": "cats"}),
            tool_call("search", {"q": "cats"}),
            tool_call("search", {"q": "cats"}),
            reply("done"),
        ]
    )
    tool = _echo_tool("search")
    steer_messages = [UserMessage(content="also check dogs")]

    def get_steering():
        # Delivered once, right after the second tool call's turn ends —
        # before it, the chain would be at count 2; a third identical call
        # without the reset would cross threshold 3.
        if len(provider.calls) == 2 and steer_messages:
            return (steer_messages.pop(0),)
        return ()

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        get_steering_messages=get_steering,
        repeat_guard=RepeatGuardSettings(thresholds=(3,)),
    )

    assert _advisory_messages(events) == []


async def test_repeat_guard_first_vs_later_advisory_content() -> None:
    provider = FakeProvider(
        [
            tool_call("search", {"q": "cats"}),
            tool_call("search", {"q": "cats"}),
            tool_call("search", {"q": "cats"}),
            tool_call("search", {"q": "cats"}),
            reply("done"),
        ]
    )
    tool = _echo_tool("search")

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        repeat_guard=RepeatGuardSettings(thresholds=(2, 4)),
    )

    advisories = _advisory_messages(events)
    assert len(advisories) == 2

    first, later = advisories
    assert "search" not in first.text
    assert "search" in later.text
    assert "4" in later.text
    assert '"q":"cats"' in later.text


async def test_repeat_guard_once_per_threshold_per_run() -> None:
    """Even across many more repeats than the configured thresholds, each
    threshold fires exactly once for the whole run."""
    calls = [tool_call("search", {"q": "cats"}) for _ in range(6)]
    provider = FakeProvider([*calls, reply("done")])
    tool = _echo_tool("search")

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        repeat_guard=RepeatGuardSettings(thresholds=(2, 4)),
    )

    assert len(_advisory_messages(events)) == 2


async def test_repeat_guard_disabled_never_fires() -> None:
    calls = [tool_call("search", {"q": "cats"}) for _ in range(5)]
    provider = FakeProvider([*calls, reply("done")])
    tool = _echo_tool("search")

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
        repeat_guard=RepeatGuardSettings(enabled=False, thresholds=(2, 3)),
    )

    assert _advisory_messages(events) == []


async def test_repeat_guard_default_none_never_fires() -> None:
    calls = [tool_call("search", {"q": "cats"}) for _ in range(5)]
    provider = FakeProvider([*calls, reply("done")])
    tool = _echo_tool("search")

    events = await _collect(
        provider=provider,
        model="m",
        system="s",
        messages=[],
        tools=[tool],
        prompts=[UserMessage(content="go")],
    )

    assert _advisory_messages(events) == []
