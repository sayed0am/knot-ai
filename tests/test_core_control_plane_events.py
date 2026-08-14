"""Control-plane events: ``emit_loop_event`` and the loop's merge channel.

Covers the mechanism ``knot.authoring.runtime`` relies on to fold
``SubagentCalledEvent``/``SubagentCompletedEvent`` onto a parent session's
own event stream (see its module docstring), tested here at the core level
with a hand-built tool executor so the mechanism itself is verified
independently of delegation.
"""

from __future__ import annotations

from knot.core.events import (
    AgentEndEvent,
    MessageEndEvent,
    SubagentCalledEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from knot.core.loop import emit_loop_event, run_agent_loop
from knot.core.session.entries import ENTRY_TYPE_MESSAGE
from knot.core.session.persistence import PersistenceSubscriber
from knot.core.session.store import SessionStore
from knot.core.tools import AgentTool, AgentToolResult
from knot.providers.fake import FakeProvider, reply
from knot.providers.messages import ToolCall, ToolResultMessage, UserMessage


def _control_emitting_tool(name: str) -> AgentTool:
    """A tool whose executor emits a custom control event mid-execution."""

    async def run(tool_call_id, arguments, signal=None, on_update=None):
        emit_loop_event(
            SubagentCalledEvent(
                tool_call_id=tool_call_id, subagent_id="sub", child_session_id="sess_child"
            )
        )
        return AgentToolResult(content=f"{name}-result")

    return AgentTool(name=name, description="", parameters={}, execute_fn=run)


async def test_emit_loop_event_is_a_noop_outside_a_running_tool_phase() -> None:
    event = SubagentCalledEvent(tool_call_id="x", subagent_id="s", child_session_id="c")
    assert emit_loop_event(event) is False


async def test_custom_control_event_appears_between_tool_execution_start_and_end() -> None:
    tool = _control_emitting_tool("delegate")
    call = ToolCall(id="call_1", name="delegate", arguments={})
    provider = FakeProvider([reply(tool_calls=[call]), reply("done")])

    events = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=[],
            tools=[tool],
            prompts=[UserMessage(content="go")],
        )
    ]

    types = [e.type for e in events]
    start_index = types.index("tool_execution_start")
    end_index = types.index("tool_execution_end")
    assert start_index < types.index("subagent_called") < end_index

    control_events = [e for e in events if isinstance(e, SubagentCalledEvent)]
    assert len(control_events) == 1
    assert control_events[0].tool_call_id == "call_1"
    assert control_events[0].child_session_id == "sess_child"

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"


async def test_control_event_does_not_count_toward_concurrent_tool_completion() -> None:
    """The concurrent tool phase's result-counting keys on ``ToolResultMessage``
    ``MessageEndEvent``s; a control event slipped onto the same queue must
    not be mistaken for one of those, nor prevent the other, ordinary
    concurrent call from being recorded."""

    async def normal(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content="ok")

    other_tool = AgentTool(name="other", description="", parameters={}, execute_fn=normal)
    tools = [_control_emitting_tool("delegate"), other_tool]
    calls = [
        ToolCall(id="call_delegate", name="delegate", arguments={}),
        ToolCall(id="call_other", name="other", arguments={}),
    ]
    provider = FakeProvider([reply(tool_calls=calls), reply("done")])

    events = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=[],
            tools=tools,
            prompts=[UserMessage(content="go")],
        )
    ]

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"

    result_messages = [
        e.message
        for e in events
        if isinstance(e, MessageEndEvent) and isinstance(e.message, ToolResultMessage)
    ]
    assert {m.tool_call_id for m in result_messages} == {"call_delegate", "call_other"}

    tool_ends = [e for e in events if isinstance(e, ToolExecutionEndEvent)]
    assert {e.tool_call_id for e in tool_ends} == {"call_delegate", "call_other"}

    starts = [e for e in events if isinstance(e, ToolExecutionStartEvent)]
    assert {e.tool_call_id for e in starts} == {"call_delegate", "call_other"}


async def test_persistence_subscriber_ignores_control_events(tmp_path) -> None:
    """A ``SubagentCalledEvent``/``SubagentCompletedEvent`` never becomes a
    durable entry: ``PersistenceSubscriber`` only matches
    ``MessageEndEvent``/``AgentEndEvent``."""
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("agent_a")
    subscriber = PersistenceSubscriber(store, session.session_id)

    subscriber(
        SubagentCalledEvent(tool_call_id="call_1", subagent_id="sub", child_session_id="sess_child")
    )
    from knot.core.events import SubagentCompletedEvent

    subscriber(
        SubagentCompletedEvent(
            tool_call_id="call_1",
            subagent_id="sub",
            child_session_id="sess_child",
            outcome="completed",
        )
    )

    assert store.entries(session.session_id) == []
    subscriber.release()

    # Sanity: a real MessageEndEvent still records, proving the subscriber
    # is live and simply filtering these two event types out.
    subscriber2 = PersistenceSubscriber(store, session.session_id)
    subscriber2(MessageEndEvent(message=UserMessage(content="hi")))
    subscriber2.release()
    entries = store.entries(session.session_id)
    assert len(entries) == 1
    assert entries[0].type == ENTRY_TYPE_MESSAGE
    store.close()
