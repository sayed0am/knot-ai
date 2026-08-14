"""Tests for the Anthropic Messages API adapter, driven by synthetic SSE."""

from __future__ import annotations

from collections.abc import AsyncIterator
from json import loads

import httpx
import pytest

from knot.providers.anthropic import AnthropicProvider
from knot.providers.events import AssistantDoneEvent, AssistantErrorEvent
from knot.providers.messages import UserMessage
from knot.providers.provider import CancellationToken


async def _collect(stream: AsyncIterator[object]) -> list[object]:
    return [event async for event in stream]


class _CountingCancellationToken:
    """Cancels once ``is_cancelled`` has been checked more than ``cancel_after`` times."""

    def __init__(self, cancel_after: int) -> None:
        self._count = 0
        self._cancel_after = cancel_after

    def is_cancelled(self) -> bool:
        self._count += 1
        return self._count > self._cancel_after


def _provider(handler, **overrides) -> tuple[AnthropicProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(
        api_key="test-key",
        base_url="https://api.anthropic.test/v1",
        client=client,
        **overrides,
    )
    return provider, client


async def test_streams_text_deltas_and_sends_expected_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=(
                'data: {"type":"message_start","message":{"content":[]}}\n\n'
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"Hel"}}\n\n'
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"lo"}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider, client = _provider(handler)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="claude-test",
                system="You are helpful.",
                messages=[UserMessage(content="Say hello")],
                tools=[],
            )
        )

    assert [event.type for event in events] == [
        "start",
        "text_start",
        "text_delta",
        "text_delta",
        "text_end",
        "done",
    ]
    done = events[-1]
    assert isinstance(done, AssistantDoneEvent)
    assert done.message.text == "Hello"
    assert done.reason == "stop"

    request = requests[0]
    assert request.url == "https://api.anthropic.test/v1/messages"
    assert request.headers["x-api-key"] == "test-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    payload = loads(request.content)
    assert payload["model"] == "claude-test"
    assert payload["stream"] is True
    assert payload["system"] == "You are helpful."
    assert payload["messages"] == [{"role": "user", "content": "Say hello"}]


async def test_streams_tool_use_with_streamed_input_json_delta() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"type":"content_block_start","index":0,'
                '"content_block":{"type":"tool_use","id":"call_1","name":"get_weather"}}\n\n'
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":"}}\n\n'
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"input_json_delta","partial_json":"\\"nyc\\"}"}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider, client = _provider(handler)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="claude-test", system="s", messages=[UserMessage(content="hi")], tools=[]
            )
        )

    assert [event.type for event in events] == ["start", "toolcall_start", "toolcall_end", "done"]
    done = events[-1]
    assert isinstance(done, AssistantDoneEvent)
    assert done.reason == "toolUse"
    call = done.message.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "get_weather"
    assert call.arguments == {"city": "nyc"}


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("end_turn", "stop"), ("max_tokens", "length")],
)
async def test_stop_reason_mapping(stop_reason: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"ok"}}\n\n'
                f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop_reason}"}}}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider, client = _provider(handler)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="claude-test", system="s", messages=[UserMessage(content="hi")], tools=[]
            )
        )

    done = events[-1]
    assert isinstance(done, AssistantDoneEvent)
    assert done.reason == expected


async def test_non_transient_http_error_surfaces_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            401,
            json={"error": {"message": "invalid x-api-key"}},
            headers={"content-type": "application/json"},
        )

    provider, client = _provider(handler, max_retries=2, max_retry_delay_seconds=0)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="claude-test", system="s", messages=[UserMessage(content="hi")], tools=[]
            )
        )

    assert len(requests) == 1
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].reason == "error"
    assert "invalid x-api-key" in events[-1].error.error_message


async def test_transient_status_is_retried_then_succeeds() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(529, text="overloaded")
        return httpx.Response(
            200,
            text=(
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"ok"}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider, client = _provider(handler, max_retries=1, max_retry_delay_seconds=0)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="claude-test", system="s", messages=[UserMessage(content="hi")], tools=[]
            )
        )

    assert len(requests) == 2
    assert isinstance(events[-1], AssistantDoneEvent)
    assert events[-1].message.text == "ok"


async def test_permanent_error_does_not_retry_storm() -> None:
    """A non-transient status must fail after exactly one attempt."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider, client = _provider(handler, max_retries=5, max_retry_delay_seconds=0)
    async with client:
        await _collect(
            provider.stream_response(
                model="claude-test", system="s", messages=[UserMessage(content="hi")], tools=[]
            )
        )

    assert len(requests) == 1


async def test_cancellation_mid_stream_yields_aborted_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"partial"}}\n\n'
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":" more"}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider, client = _provider(handler)
    signal: CancellationToken = _CountingCancellationToken(cancel_after=2)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="claude-test",
                system="s",
                messages=[UserMessage(content="hi")],
                tools=[],
                signal=signal,
            )
        )

    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].reason == "aborted"


async def test_api_key_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["x-api-key"] = request.headers["x-api-key"]
        return httpx.Response(
            200,
            text='data: {"type":"message_stop"}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(client=client)
    async with client:
        await _collect(
            provider.stream_response(
                model="claude-test", system="s", messages=[UserMessage(content="hi")], tools=[]
            )
        )
    assert captured["x-api-key"] == "env-key"


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(api_key=None, base_url="https://x.test")
