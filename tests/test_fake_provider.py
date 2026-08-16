"""Full event-sequence assertions for the scripted fake provider."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from knot.providers.events import AssistantDoneEvent, AssistantErrorEvent
from knot.providers.fake import FakeProvider, error, reply, tool_call
from knot.providers.messages import UserMessage
from knot.providers.provider import SimpleCancellationToken


async def _collect(stream: AsyncIterator[object]) -> list[object]:
    return [event async for event in stream]


async def test_reply_emits_realistic_text_sequence() -> None:
    provider = FakeProvider([reply("hello there")])

    events = await _collect(
        provider.stream_response(
            model="fake-model",
            system="system prompt",
            messages=[UserMessage(content="hi")],
            tools=[],
        )
    )

    assert [event.type for event in events] == [
        "start",
        "text_start",
        "text_delta",
        "text_end",
        "done",
    ]
    assert isinstance(events[-1], AssistantDoneEvent)
    assert events[-1].reason == "stop"
    assert events[-1].message.text == "hello there"

    assert provider.calls[0][0] == "fake-model"
    assert provider.calls[0][1] == "system prompt"
    assert provider.session_ids[0] is None


async def test_tool_call_emits_toolcall_sequence_with_toolUse_reason() -> None:
    provider = FakeProvider([tool_call("get_invoice", {"id": "inv_1"})])

    events = await _collect(
        provider.stream_response(
            model="m", system="s", messages=[UserMessage(content="hi")], tools=[]
        )
    )

    assert [event.type for event in events] == ["start", "toolcall_start", "toolcall_end", "done"]
    done = events[-1]
    assert isinstance(done, AssistantDoneEvent)
    assert done.reason == "toolUse"
    assert done.message.tool_calls[0].name == "get_invoice"
    assert done.message.tool_calls[0].arguments == {"id": "inv_1"}


async def test_error_emits_start_then_error_event() -> None:
    provider = FakeProvider([error("boom")])

    events = await _collect(
        provider.stream_response(
            model="m", system="s", messages=[UserMessage(content="hi")], tools=[]
        )
    )

    assert [event.type for event in events] == ["start", "error"]
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].reason == "error"
    assert events[-1].error.error_message == "boom"


async def test_aborted_error_reason_is_configurable() -> None:
    provider = FakeProvider([error("cancelled", reason="aborted")])
    events = await _collect(
        provider.stream_response(
            model="m", system="s", messages=[UserMessage(content="hi")], tools=[]
        )
    )
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].reason == "aborted"


async def test_provider_replays_scripts_in_order_and_empty_after_exhaustion() -> None:
    provider = FakeProvider([reply("one"), reply("two")])

    first = await _collect(provider.stream_response(model="m", system="s", messages=[], tools=[]))
    second = await _collect(provider.stream_response(model="m", system="s", messages=[], tools=[]))
    third = await _collect(provider.stream_response(model="m", system="s", messages=[], tools=[]))

    assert first[-1].message.text == "one"
    assert second[-1].message.text == "two"
    assert third == []
    assert len(provider.calls) == 3


async def test_provider_stops_replay_when_cancelled() -> None:
    provider = FakeProvider([reply("hello")])
    signal = SimpleCancellationToken()
    signal.cancel()

    events = await _collect(
        provider.stream_response(model="m", system="s", messages=[], tools=[], signal=signal)
    )

    assert events == []


@pytest.mark.parametrize("session_id", [None, "session-123"])
async def test_provider_records_session_id(session_id: str | None) -> None:
    provider = FakeProvider([reply("hi")])
    await _collect(
        provider.stream_response(
            model="m", system="s", messages=[], tools=[], session_id=session_id
        )
    )
    assert provider.session_ids == [session_id]
