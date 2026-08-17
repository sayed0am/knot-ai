"""OpenAI-compatible chat completions provider.

Serves the standard ``/chat/completions`` streaming endpoint shared by
OpenAI, OpenRouter, and most self-hosted OpenAI-compatible gateways.
Deliberately scoped down: no per-backend reasoning-format matrix (vendor
field variants), prompt-cache-key/session-affinity headers, or a runtime
credential resolver — those are coding-agent-CLI-specific concerns. Reasoning
effort is sent as the single standard ``reasoning_effort`` field when set.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from json import JSONDecodeError, dumps, loads
from typing import Any

import httpx

from knot.providers._http import create_async_client
from knot.providers._http_errors import classify_provider_error_type, provider_http_error_message
from knot.providers._provider_events import (
    ProviderAbortedEvent,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseEndEvent,
    ProviderResponseStartEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
)
from knot.providers._retry import (
    is_transient_status,
    provider_retry_event,
    retry_delay_seconds,
    wait_for_retry,
)
from knot.providers._stream import canonicalize_provider_stream
from knot.providers._tool_call_ids import portable_tool_call_id
from knot.providers.events import AssistantMessageEvent
from knot.providers.messages import (
    AgentMessage,
    AssistantMessage,
    ImageContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
    assistant_content,
    message_to_user,
)
from knot.providers.provider import CancellationToken, ToolSpec
from knot.providers.types import JSONValue

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_RETRY_DELAY_SECONDS = 1.0


class OpenAICompatibleProvider:
    """Provider adapter for OpenAI-compatible ``/chat/completions`` APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        api_key_env_var: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_delay_seconds: float = DEFAULT_MAX_RETRY_DELAY_SECONDS,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        provider_name: str = "OpenAI-compatible provider",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_api_key = api_key or (os.environ.get(api_key_env_var) if api_key_env_var else None)
        if not resolved_api_key:
            hint = f" or set {api_key_env_var}" if api_key_env_var else ""
            raise RuntimeError(f"API key is required: pass api_key={hint}")
        self._api_key = resolved_api_key
        self._base_url = base_url
        self._headers = dict(headers or {})
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._max_retry_delay_seconds = max_retry_delay_seconds
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._provider_name = provider_name
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this provider created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolSpec],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
        max_tokens: int | None = None,
        thinking_budget_tokens: int | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        """Stream one response as assistant message events.

        ``thinking_budget_tokens`` is accepted for protocol conformance but
        has no chat-completions equivalent this adapter can honor, so it is
        ignored — the compile-time diagnostic in ``knot.authoring.compile``
        is what stops it from being set here silently.
        """
        del session_id, thinking_budget_tokens
        raw = self._stream_provider_events(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            signal=signal,
            max_tokens=max_tokens,
        )
        return canonicalize_provider_stream(
            raw, api="openai-completions", provider=self._provider_name, model=model
        )

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolSpec],
        signal: CancellationToken | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        async def iterator() -> AsyncIterator[ProviderEvent]:
            if signal is not None and signal.is_cancelled():
                yield ProviderAbortedEvent()
                return

            client = self._get_client()
            payload = _build_chat_payload(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens if max_tokens is not None else self._max_tokens,
                reasoning_effort=self._reasoning_effort,
            )
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
                **self._headers,
            }
            url = f"{self._base_url.rstrip('/')}/chat/completions"

            attempt = 0
            while True:
                parser = _ChatStreamParser()
                try:
                    async with client.stream(
                        "POST", url, json=payload, headers=headers
                    ) as response:
                        if response.status_code >= 400:
                            body = await response.aread()
                            body_text = body.decode(errors="replace")
                            if self._should_retry(attempt, status_code=response.status_code):
                                delay = retry_delay_seconds(
                                    attempt, max_delay_seconds=self._max_retry_delay_seconds
                                )
                                yield provider_retry_event(
                                    attempt=attempt,
                                    max_retries=self._max_retries,
                                    delay_seconds=delay,
                                    reason=f"HTTP {response.status_code}",
                                    data={
                                        "status_code": response.status_code,
                                        "body": body_text,
                                    },
                                )
                                attempt += 1
                                should_continue = await wait_for_retry(delay, signal=signal)
                                if not should_continue:
                                    yield ProviderAbortedEvent()
                                    return
                                continue
                            yield ProviderErrorEvent(
                                message=provider_http_error_message(
                                    provider_name=self._provider_name,
                                    status_code=response.status_code,
                                    body=body_text,
                                    model=model,
                                ),
                                data={
                                    "status_code": response.status_code,
                                    "body": body_text,
                                    "attempts": attempt + 1,
                                },
                                error_type=classify_provider_error_type(
                                    status_code=response.status_code, body=body_text
                                ),
                            )
                            return

                        yield ProviderResponseStartEvent(model=model)

                        async for line in response.aiter_lines():
                            if signal is not None and signal.is_cancelled():
                                yield ProviderAbortedEvent()
                                return

                            event = _parse_sse_line(line)
                            if event is None:
                                continue

                            events, stop = parser.feed(event)
                            for parser_event in events:
                                yield parser_event
                            if stop:
                                break

                        if parser.fatal:
                            return
                        for parser_event in parser.finalize():
                            yield parser_event
                        return
                except httpx.HTTPError as exc:
                    if not parser.emitted_content and self._should_retry(attempt):
                        delay = retry_delay_seconds(
                            attempt, max_delay_seconds=self._max_retry_delay_seconds
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._max_retries,
                            delay_seconds=delay,
                            reason="network error",
                            data={"error": str(exc), "error_type": type(exc).__name__},
                        )
                        attempt += 1
                        should_continue = await wait_for_retry(delay, signal=signal)
                        if not should_continue:
                            yield ProviderAbortedEvent()
                            return
                        continue
                    yield ProviderErrorEvent(message=str(exc), data={"attempts": attempt + 1})
                    return

        return iterator()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = create_async_client(timeout=self._timeout_seconds)
        return self._client

    def _should_retry(self, attempt: int, *, status_code: int | None = None) -> bool:
        if attempt >= self._max_retries:
            return False
        return status_code is None or is_transient_status(status_code)


def openai_provider(
    *,
    api_key: str | None = None,
    **kwargs: Any,
) -> OpenAICompatibleProvider:
    """Build a provider for api.openai.com, reading ``OPENAI_API_KEY`` by default."""
    return OpenAICompatibleProvider(
        base_url=DEFAULT_OPENAI_BASE_URL,
        api_key=api_key,
        api_key_env_var="OPENAI_API_KEY",
        provider_name="OpenAI",
        **kwargs,
    )


def openrouter_provider(
    *,
    api_key: str | None = None,
    **kwargs: Any,
) -> OpenAICompatibleProvider:
    """Build a provider for openrouter.ai, reading ``OPENROUTER_API_KEY`` by default."""
    return OpenAICompatibleProvider(
        base_url=DEFAULT_OPENROUTER_BASE_URL,
        api_key=api_key,
        api_key_env_var="OPENROUTER_API_KEY",
        provider_name="OpenRouter",
        **kwargs,
    )


class _ChatStreamParser:
    """Parser for OpenAI ``/chat/completions`` SSE chunks."""

    def __init__(self) -> None:
        self.emitted_content = False
        self.fatal = False
        self._content_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._thinking_signature: str | None = None
        self._tool_call_builders: dict[int, _ToolCallBuilder] = {}
        self._finish_reason: str | None = None
        self._usage: Usage | None = None

    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        if event == "[DONE]":
            return [], True

        chunk = _loads_object(event)
        if chunk is None:
            self.fatal = True
            return [ProviderErrorEvent(message="Provider returned invalid JSON chunk")], True

        # The final usage chunk (from stream_options) carries usage at the top
        # level and often has empty choices.
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, Mapping):
            self._usage = _parse_chunk_usage(chunk_usage)

        choice = _first_choice(chunk)
        if choice is None:
            return [], False

        self._finish_reason = choice.get("finish_reason") or self._finish_reason
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            return [], False

        events: list[ProviderEvent] = []
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.emitted_content = True
            self._content_parts.append(content)
            events.append(ProviderTextDeltaEvent(delta=content))

        thinking = _thinking_delta(delta)
        if thinking is not None:
            field_name, text = thinking
            self.emitted_content = True
            self._thinking_parts.append(text)
            self._thinking_signature = self._thinking_signature or field_name
            events.append(ProviderThinkingDeltaEvent(delta=text))

        for tool_call_delta in _tool_call_deltas(delta):
            self.emitted_content = True
            index = int(tool_call_delta.get("index", 0))
            builder = self._tool_call_builders.setdefault(index, _ToolCallBuilder())
            builder.add_delta(tool_call_delta)

        return events, False

    def finalize(self) -> list[ProviderEvent]:
        tool_calls = [
            builder.build(index) for index, builder in sorted(self._tool_call_builders.items())
        ]
        events: list[ProviderEvent] = [
            ProviderToolCallEvent(tool_call=tool_call) for tool_call in tool_calls
        ]
        content = assistant_content("".join(self._content_parts), tool_calls)
        if self._thinking_parts:
            content.insert(
                0,
                ThinkingContent(
                    thinking="".join(self._thinking_parts),
                    thinking_signature=self._thinking_signature,
                ),
            )
        events.append(
            ProviderResponseEndEvent(
                message=AssistantMessage(content=content, usage=self._usage or Usage()),
                finish_reason=self._finish_reason,
            )
        )
        return events


class _ToolCallBuilder:
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments_parts: list[str] = []

    def add_delta(self, delta: Mapping[str, Any]) -> None:
        call_id = delta.get("id")
        if isinstance(call_id, str):
            self.id = call_id

        function = delta.get("function")
        if not isinstance(function, Mapping):
            return

        name = function.get("name")
        if isinstance(name, str):
            self.name = name

        arguments = function.get("arguments")
        if isinstance(arguments, str):
            self.arguments_parts.append(arguments)

    def build(self, index: int) -> ToolCall:
        arguments_text = "".join(self.arguments_parts)
        arguments = _loads_object(arguments_text) if arguments_text else {}
        if arguments is None:
            arguments = {"_raw_arguments": arguments_text}
        return ToolCall(id=self.id or f"tool-call-{index}", name=self.name, arguments=arguments)


def _build_chat_payload(
    *,
    model: str,
    system: str,
    messages: Sequence[AgentMessage],
    tools: Sequence[ToolSpec],
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": system},
            *_messages_to_openai_chat(messages),
        ],
    }
    if max_tokens is not None:
        payload["max_completion_tokens"] = max_tokens
    # An explicit reasoning_effort is always sent verbatim — including the
    # literal "none". Reasoning-capable chat-completions models reject
    # function tools unless the payload explicitly pins reasoning off;
    # omitting the field lets the server apply a reasoning default it then
    # refuses to combine with tools (HTTP 400). Only an unset (None) effort
    # omits the field, since older non-reasoning models reject the parameter
    # entirely.
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    if tools:
        payload["tools"] = [_tool_to_openai(tool) for tool in tools]
    return payload


def _messages_to_openai_chat(messages: Sequence[AgentMessage]) -> list[dict[str, JSONValue]]:
    converted: list[dict[str, JSONValue]] = []
    pending_tool_images: list[ImageContent] = []
    for message in messages:
        if pending_tool_images and not isinstance(message, ToolResultMessage):
            converted.append(_openai_tool_image_message(pending_tool_images))
            pending_tool_images = []
        if isinstance(message, UserMessage):
            converted.append(_openai_user_message(message))
            continue
        if isinstance(message, ToolResultMessage):
            text, images = _text_and_images(message.content)
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": portable_tool_call_id(message.tool_call_id),
                    "content": text or ("(see attached image)" if images else "(no tool output)"),
                }
            )
            pending_tool_images.extend(images)
            continue
        converted.append(_message_to_openai(message))
    if pending_tool_images:
        converted.append(_openai_tool_image_message(pending_tool_images))
    return converted


def _text_and_images(
    content: str | Sequence[Any],
) -> tuple[str, list[ImageContent]]:
    if isinstance(content, str):
        return content, []
    text_parts: list[str] = []
    images: list[ImageContent] = []
    for block in content:
        if isinstance(block, ImageContent):
            images.append(block)
        elif hasattr(block, "text"):
            text_parts.append(block.text)
    return "".join(text_parts), images


def _openai_user_message(message: UserMessage) -> dict[str, JSONValue]:
    text, images = _text_and_images(message.content)
    if not images:
        return {"role": "user", "content": text}
    content: list[JSONValue] = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend(
        {"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{image.data}"}}
        for image in images
    )
    return {"role": "user", "content": content}


def _openai_tool_image_message(images: list[ImageContent]) -> dict[str, JSONValue]:
    content: list[JSONValue] = [{"type": "text", "text": "Attached image(s) from tool result:"}]
    content.extend(
        {"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{image.data}"}}
        for image in images
    )
    return {"role": "user", "content": content}


def _message_to_openai(message: AgentMessage) -> dict[str, JSONValue]:
    if isinstance(message, UserMessage):
        return _openai_user_message(message)
    if isinstance(message, AssistantMessage):
        item: dict[str, JSONValue] = {"role": "assistant", "content": message.text}
        if message.tool_calls:
            item["tool_calls"] = [_tool_call_to_openai(call) for call in message.tool_calls]
        return item
    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": portable_tool_call_id(message.tool_call_id),
            "content": message.text,
        }
    return _message_to_openai(message_to_user(message))


def _tool_to_openai(tool: ToolSpec) -> dict[str, JSONValue]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema),
        },
    }


def _tool_call_to_openai(tool_call: ToolCall) -> dict[str, JSONValue]:
    return {
        "id": portable_tool_call_id(tool_call.id),
        "type": "function",
        "function": {"name": tool_call.name, "arguments": dumps(tool_call.arguments)},
    }


def _parse_sse_line(line: str) -> str | None:
    line = line.strip()
    if not line or not line.startswith("data:"):
        return None
    return line.removeprefix("data:").strip()


def _loads_object(value: str) -> dict[str, JSONValue] | None:
    try:
        loaded = loads(value)
    except JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _first_choice(chunk: Mapping[str, Any]) -> Mapping[str, Any] | None:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    return choice if isinstance(choice, Mapping) else None


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_chunk_usage(raw: Mapping[str, Any]) -> Usage:
    """Parse an OpenAI-compatible ``usage`` payload into a Usage."""
    prompt_tokens = _int_or_zero(raw.get("prompt_tokens"))
    prompt_details = raw.get("prompt_tokens_details")
    cached_tokens: int | None = None
    if isinstance(prompt_details, Mapping):
        cached_tokens = _int_or_none(prompt_details.get("cached_tokens"))
    cache_read = cached_tokens or 0
    fresh_input = max(0, prompt_tokens - cache_read)
    output = _int_or_zero(raw.get("completion_tokens"))
    reasoning = None
    completion_details = raw.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        reasoning = _int_or_zero(completion_details.get("reasoning_tokens"))
    return Usage(
        input=fresh_input,
        output=output,
        cache_read=cache_read,
        reasoning=reasoning,
        total_tokens=fresh_input + output + cache_read,
    )


def _tool_call_deltas(delta: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tool_calls = delta.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, Mapping)]


def _thinking_delta(delta: Mapping[str, Any]) -> tuple[str, str] | None:
    for field_name in ("reasoning_content", "reasoning", "thinking"):
        value = delta.get(field_name)
        if isinstance(value, str) and value:
            return field_name, value
    return None


__all__ = [
    "DEFAULT_OPENAI_BASE_URL",
    "DEFAULT_OPENROUTER_BASE_URL",
    "OpenAICompatibleProvider",
    "openai_provider",
    "openrouter_provider",
]
