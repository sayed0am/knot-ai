"""Tests for the OpenAI-compatible chat-completions adapter, via synthetic SSE."""

from __future__ import annotations

from collections.abc import AsyncIterator
from json import loads

import httpx
import pytest

from knot.providers.events import AssistantDoneEvent, AssistantErrorEvent
from knot.providers.messages import UserMessage
from knot.providers.openai_compatible import (
    OpenAICompatibleProvider,
    openai_provider,
    openrouter_provider,
)


async def _collect(stream: AsyncIterator[object]) -> list[object]:
    return [event async for event in stream]


def _provider(handler, **overrides) -> tuple[OpenAICompatibleProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://api.openai-compat.test/v1",
        api_key="test-key",
        client=client,
        **overrides,
    )
    return provider, client


async def test_streams_text_deltas_and_sends_expected_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = (
            'data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider, client = _provider(handler)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="gpt-test",
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
    assert done.message.usage.input == 10
    assert done.message.usage.output == 5
    assert done.message.usage.total_tokens == 15

    request = requests[0]
    assert request.url == "https://api.openai-compat.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    payload = loads(request.content)
    assert payload["model"] == "gpt-test"
    assert payload["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert payload["messages"][1] == {"role": "user", "content": "Say hello"}


async def test_streams_tool_calls_with_streamed_arguments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1",'
            '"type":"function","function":{"name":"get_weather","arguments":""}}]},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"{\\"city\\":"}}]},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"\\"nyc\\"}"}}]},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider, client = _provider(handler)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="gpt-test", system="s", messages=[UserMessage(content="hi")], tools=[]
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
    ("finish_reason", "expected"),
    [("stop", "stop"), ("length", "length"), ("tool_calls", "toolUse")],
)
async def test_finish_reason_mapping(finish_reason: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        tail = f'"finish_reason":"{finish_reason}"'
        body = (
            'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
            f'data: {{"choices":[{{"index":0,"delta":{{}},{tail}}}]}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider, client = _provider(handler)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="gpt-test", system="s", messages=[UserMessage(content="hi")], tools=[]
            )
        )

    done = events[-1]
    assert isinstance(done, AssistantDoneEvent)
    assert done.reason == expected


async def test_thinking_deltas_are_streamed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"index":0,"delta":{"reasoning_content":"pondering"},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"content":"answer"},'
            '"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider, client = _provider(handler)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="gpt-test", system="s", messages=[UserMessage(content="hi")], tools=[]
            )
        )

    assert "thinking_start" in [event.type for event in events]
    done = events[-1]
    assert isinstance(done, AssistantDoneEvent)
    assert done.message.thinking_text == "pondering"
    assert done.message.text == "answer"


async def test_non_transient_http_error_surfaces_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    provider, client = _provider(handler, max_retries=2, max_retry_delay_seconds=0)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="gpt-test", system="s", messages=[UserMessage(content="hi")], tools=[]
            )
        )

    assert len(requests) == 1
    assert isinstance(events[-1], AssistantErrorEvent)
    assert "invalid api key" in events[-1].error.error_message


async def test_transient_status_is_retried_then_succeeds() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, text="rate limited")
        body = (
            'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider, client = _provider(handler, max_retries=1, max_retry_delay_seconds=0)
    async with client:
        events = await _collect(
            provider.stream_response(
                model="gpt-test", system="s", messages=[UserMessage(content="hi")], tools=[]
            )
        )

    assert len(requests) == 2
    done = events[-1]
    assert isinstance(done, AssistantDoneEvent)
    assert done.message.text == "ok"


def test_openai_provider_factory_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    provider = openai_provider()
    assert provider._base_url == "https://api.openai.com/v1"  # noqa: SLF001
    assert provider._api_key == "env-openai-key"  # noqa: SLF001
    assert provider._provider_name == "OpenAI"  # noqa: SLF001


def test_openrouter_provider_factory_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-openrouter-key")
    provider = openrouter_provider()
    assert provider._base_url == "https://openrouter.ai/api/v1"  # noqa: SLF001
    assert provider._api_key == "env-openrouter-key"  # noqa: SLF001
    assert provider._provider_name == "OpenRouter"  # noqa: SLF001


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API key is required"):
        openai_provider()


def test_reasoning_effort_none_is_sent_verbatim_and_unset_is_omitted() -> None:
    from knot.providers.openai_compatible import _build_chat_payload

    # Explicit "none" must reach the wire literally: reasoning-capable
    # chat-completions models reject function tools unless the payload pins
    # reasoning off, and omitting the field lets the server pick a default
    # it then refuses to combine with tools.
    explicit = _build_chat_payload(
        model="m",
        system="s",
        messages=[UserMessage(content="hi")],
        tools=[],
        reasoning_effort="none",
    )
    assert explicit["reasoning_effort"] == "none"

    explicit_level = _build_chat_payload(
        model="m",
        system="s",
        messages=[UserMessage(content="hi")],
        tools=[],
        reasoning_effort="high",
    )
    assert explicit_level["reasoning_effort"] == "high"

    # Unset omits the field entirely — older non-reasoning models reject the
    # parameter outright.
    unset = _build_chat_payload(
        model="m",
        system="s",
        messages=[UserMessage(content="hi")],
        tools=[],
        reasoning_effort=None,
    )
    assert "reasoning_effort" not in unset
