"""Round-trip serialization tests for knot.providers.events."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from knot.providers.events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from knot.providers.messages import AssistantMessage, ToolCall

_EVENT_ADAPTER: TypeAdapter[AssistantMessageEvent] = TypeAdapter(AssistantMessageEvent)


def _all_events() -> list[AssistantMessageEvent]:
    partial = AssistantMessage(model="m")
    call = ToolCall(id="1", name="t", arguments={})
    done_message = AssistantMessage(content="hi", model="m", stop_reason="stop")
    error_message = AssistantMessage(model="m", stop_reason="error", error_message="boom")
    return [
        AssistantStartEvent(partial=partial),
        TextStartEvent(content_index=0, partial=partial),
        TextDeltaEvent(content_index=0, delta="hi", partial=partial),
        TextEndEvent(content_index=0, content="hi", partial=partial),
        ThinkingStartEvent(content_index=0, partial=partial),
        ThinkingDeltaEvent(content_index=0, delta="hm", partial=partial),
        ThinkingEndEvent(content_index=0, content="hm", partial=partial),
        ToolCallStartEvent(content_index=0, partial=partial),
        ToolCallDeltaEvent(content_index=0, delta="{}", partial=partial),
        ToolCallEndEvent(content_index=0, tool_call=call, partial=partial),
        AssistantDoneEvent(reason="stop", message=done_message),
        AssistantErrorEvent(reason="error", error=error_message),
    ]


def test_every_event_round_trips_through_camel_case_json() -> None:
    for event in _all_events():
        dumped = event.model_dump(mode="json", by_alias=True)
        restored = type(event).model_validate(dumped)
        assert restored == event


def test_assistant_message_event_union_discriminates_by_type() -> None:
    for event in _all_events():
        dumped = event.model_dump(mode="json", by_alias=True)
        restored = _EVENT_ADAPTER.validate_python(dumped)
        assert type(restored) is type(event)
        assert restored == event


def test_done_event_reason_is_restricted_to_terminal_reasons() -> None:
    message = AssistantMessage(content="hi", model="m")
    with pytest.raises(ValidationError):
        AssistantDoneEvent(reason="aborted", message=message)  # type: ignore[arg-type]


def test_error_event_reason_accepts_error_and_aborted() -> None:
    message = AssistantMessage(model="m", stop_reason="error")
    assert AssistantErrorEvent(reason="error", error=message).reason == "error"
    assert AssistantErrorEvent(reason="aborted", error=message).reason == "aborted"
