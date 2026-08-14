"""AgentHarness: queues, cancellation, subscribers, and derived state."""

from __future__ import annotations

import asyncio

import pytest

from knot.core.decisions import Allow, RequireApproval
from knot.core.events import AgentEndEvent, MessageEndEvent
from knot.core.harness import AgentHarness, AgentHarnessConfig
from knot.core.tools import AgentTool, AgentToolResult
from knot.providers.fake import FakeProvider, reply
from knot.providers.messages import ToolCall, ToolResultMessage


def _make_harness(provider: FakeProvider, **overrides) -> AgentHarness:
    config = AgentHarnessConfig(provider=provider, model="m", system="s", **overrides)
    return AgentHarness(config)


async def test_single_run_enforcement_raises() -> None:
    harness = _make_harness(FakeProvider([reply("hi")]))

    gen = harness.prompt("go")
    assert harness.state == "running"

    with pytest.raises(RuntimeError, match="steer\\(\\) or follow_up\\(\\)"):
        harness.prompt("again")

    # Drain the first run cleanly.
    events = [event async for event in gen]
    assert events[-1].outcome == "completed"
    assert harness.state == "idle"


async def test_subscribers_sync_and_async_receive_every_event_and_unsubscribe() -> None:
    harness = _make_harness(FakeProvider([reply("hi"), reply("hi again")]))
    sync_events = []
    async_events = []

    def sync_listener(event) -> None:
        sync_events.append(event)

    async def async_listener(event) -> None:
        async_events.append(event)

    unsubscribe_sync = harness.subscribe(sync_listener)
    harness.subscribe(async_listener)

    first_run = [event async for event in harness.prompt("go")]
    assert len(sync_events) == len(first_run)
    assert len(async_events) == len(first_run)

    unsubscribe_sync()
    second_run = [event async for event in harness.prompt("go again")]
    assert len(sync_events) == len(first_run)  # unchanged after unsubscribe
    assert len(async_events) == len(first_run) + len(second_run)


async def test_state_transitions_idle_running_idle() -> None:
    harness = _make_harness(FakeProvider([reply("hi")]))
    assert harness.state == "idle"

    gen = harness.prompt("go")
    assert harness.state == "running"

    events = [event async for event in gen]
    assert events[-1].outcome == "completed"
    assert harness.state == "idle"


async def test_state_transitions_idle_running_waiting_mixed_park() -> None:
    """The spec scenario: two calls execute, one is gated for approval."""

    async def hook(call: ToolCall, tool: AgentTool | None):
        if call.name == "sensitive_op":
            return RequireApproval()
        return Allow()

    async def echo(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content="ok")

    tools = [
        AgentTool(name="read_a", description="", parameters={}, execute_fn=echo),
        AgentTool(name="sensitive_op", description="", parameters={}, execute_fn=echo),
    ]
    calls = [
        ToolCall(id="call_read_a", name="read_a", arguments={}),
        ToolCall(id="call_sensitive", name="sensitive_op", arguments={}),
    ]
    provider = FakeProvider([reply(tool_calls=calls)])
    harness = _make_harness(provider, tools=tools, tool_decision_hook=hook)

    assert harness.state == "idle"
    events = [event async for event in harness.prompt("go")]

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "waiting_input"
    assert len(end.pending_requests) == 1

    assert harness.state == "waiting"
    assert len(harness.pending_requests) == 1

    result_messages = [
        e.message
        for e in events
        if isinstance(e, MessageEndEvent) and isinstance(e.message, ToolResultMessage)
    ]
    assert {m.tool_call_id for m in result_messages} == {"call_read_a"}

    resolved = harness.resolve_pending(harness.pending_requests[0].id)
    assert resolved is not None
    assert resolved.tool_call_id == "call_sensitive"
    assert harness.state == "idle"
    assert harness.pending_requests == ()


async def test_resolve_pending_unknown_id_returns_none() -> None:
    harness = _make_harness(FakeProvider([reply("hi")]))
    assert harness.resolve_pending("nope") is None


async def test_queued_messages_and_clear_queues() -> None:
    harness = _make_harness(FakeProvider([reply("hi")]))
    harness.steer("check this too")
    harness.follow_up("and this after")

    queued = harness.queued_messages
    assert queued.count == 2
    assert harness.has_queued_messages() is True

    snapshot = harness.clear_queues()
    assert snapshot.count == 2
    assert harness.has_queued_messages() is False


async def test_cancel_mid_tool_appends_interrupted_repairs_for_subscribers() -> None:
    tool_call_obj = ToolCall(id="call_slow", name="slow", arguments={})
    provider = FakeProvider([reply(tool_calls=[tool_call_obj])])

    async def slow(tool_call_id, arguments, signal=None, on_update=None):
        await asyncio.sleep(10)
        return AgentToolResult(content="unreachable")

    tools = [AgentTool(name="slow", description="", parameters={}, execute_fn=slow)]
    harness = _make_harness(provider, tools=tools)

    pushed_events = []
    harness.subscribe(lambda event: pushed_events.append(event))

    consumed = []

    async def drive() -> None:
        async for event in harness.prompt("go"):
            consumed.append(event)

    task = asyncio.create_task(drive())
    await asyncio.sleep(0.03)
    harness.cancel()
    await task

    assert consumed[-1].outcome == "aborted"
    # The synthetic interrupted-tool-result repair was pushed to subscribers.
    interrupted = [
        e.message
        for e in pushed_events
        if isinstance(e, MessageEndEvent)
        and isinstance(e.message, ToolResultMessage)
        and e.message.tool_call_id == "call_slow"
    ]
    assert len(interrupted) == 1
    assert interrupted[0].is_error is True

    # Durable history is rehydratable: every tool call now has a result.
    result_ids = {m.tool_call_id for m in harness.messages if isinstance(m, ToolResultMessage)}
    assert "call_slow" in result_ids
    assert harness.state == "idle"


async def test_prompt_repairs_dangling_calls_from_prior_session() -> None:
    """A message list loaded from storage with a dangling tool call is
    repaired before the run starts, and the repair is delivered as an event.
    """
    dangling_call = ToolCall(id="call_x", name="whatever", arguments={})
    from knot.providers.messages import AssistantMessage

    prior_messages = [AssistantMessage(content=[dangling_call], stop_reason="toolUse")]
    provider = FakeProvider([reply("continuing")])
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, model="m", system="s"), messages=prior_messages
    )

    events = [event async for event in harness.prompt("go on")]

    repaired = [
        e.message
        for e in events
        if isinstance(e, MessageEndEvent)
        and isinstance(e.message, ToolResultMessage)
        and e.message.tool_call_id == "call_x"
    ]
    assert len(repaired) == 1
    assert repaired[0].is_error is True
