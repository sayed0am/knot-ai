"""Event grammar shape and serialization round-trip for the core loop."""

from __future__ import annotations

from pydantic import TypeAdapter

from knot.core.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    PendingInputRequest,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from knot.core.tools import AgentToolResult
from knot.providers.events import AssistantDoneEvent
from knot.providers.messages import AssistantMessage, TextContent, ToolCall, UserMessage

_ADAPTER: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)


def test_plain_text_turn_event_sequence_shape() -> None:
    """The event grammar for a plain text turn matches the fixed order."""
    user = UserMessage(content="hi")
    assistant = AssistantMessage(content=[TextContent(text="hello")], stop_reason="stop")

    events: list[AgentEvent] = [
        AgentStartEvent(),
        TurnStartEvent(),
        MessageStartEvent(message=user),
        MessageEndEvent(message=user),
        MessageStartEvent(message=assistant),
        MessageEndEvent(message=assistant),
        TurnEndEvent(message=assistant),
        AgentEndEvent(outcome="completed", messages=[user, assistant]),
    ]

    assert [event.type for event in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"
    assert end.pending_requests == []


def test_message_update_event_carries_assistant_message_event() -> None:
    partial = AssistantMessage(content=[TextContent(text="he")])
    done = AssistantDoneEvent(reason="stop", message=partial)
    event = MessageUpdateEvent(message=partial, assistant_message_event=done)

    dumped = event.model_dump(by_alias=True)
    assert dumped["assistantMessageEvent"]["type"] == "done"


def test_tool_execution_events_round_trip() -> None:
    start = ToolExecutionStartEvent(tool_call_id="c1", tool_name="get", args={"id": 1})
    update = ToolExecutionUpdateEvent(
        tool_call_id="c1",
        tool_name="get",
        args={"id": 1},
        partial_result=AgentToolResult(content=[TextContent(text="partial")]),
    )
    end = ToolExecutionEndEvent(
        tool_call_id="c1",
        tool_name="get",
        result=AgentToolResult(content=[TextContent(text="done")]),
        is_error=False,
    )

    for event in (start, update, end):
        restored = _ADAPTER.validate_json(event.model_dump_json(by_alias=True))
        assert restored == event


def test_agent_end_event_with_pending_requests_round_trips() -> None:
    call = ToolCall(id="call_1", name="delete_invoice", arguments={"id": "inv_1"})
    pending = PendingInputRequest(
        id="req_abc123",
        kind="tool_approval",
        tool_call_id=call.id,
        tool_name=call.name,
        payload={"args": {"id": "inv_1"}},
    )
    end = AgentEndEvent(outcome="waiting_input", messages=[], pending_requests=[pending])

    payload = end.model_dump_json(by_alias=True)
    restored = _ADAPTER.validate_json(payload)

    assert isinstance(restored, AgentEndEvent)
    assert restored.outcome == "waiting_input"
    assert len(restored.pending_requests) == 1
    restored_request = restored.pending_requests[0]
    assert restored_request.id == "req_abc123"
    assert restored_request.kind == "tool_approval"
    assert restored_request.tool_call_id == "call_1"
    assert restored_request.payload == {"args": {"id": "inv_1"}}
    assert restored_request.ttl_seconds is None


def test_pending_input_request_default_ttl_is_none() -> None:
    request = PendingInputRequest(
        id="req_x", kind="question", tool_call_id="c1", tool_name="ask", payload={}
    )
    assert request.ttl_seconds is None
    assert request.created_at > 0
