"""Anthropic Messages API provider.

Streams Anthropic's Messages API SSE format, translating it into knot's
provider-neutral assistant event stream. Deliberately scoped down: no
prompt-cache breakpoints, OAuth identity system-prompt blocks, runtime
credential resolver, or provider-specific header special-casing — those
belong to a coding-agent CLI's multi-backend story, which knot does not
have. Streaming, tool-call, retry, and cancellation behavior are the core
of this adapter.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from json import loads
from typing import Any, cast

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
    ErrorType,
    ImageContent,
    TextContent,
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

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_RETRY_DELAY_SECONDS = 1.0

_TRANSIENT_ANTHROPIC_STREAM_ERROR_TYPES = frozenset(
    {"api_error", "overloaded_error", "rate_limit_error"}
)


class AnthropicProvider:
    """Provider adapter for Anthropic's streaming Messages API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_ANTHROPIC_BASE_URL,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_delay_seconds: float = DEFAULT_MAX_RETRY_DELAY_SECONDS,
        max_tokens: int | None = None,
        thinking_budget_tokens: int | None = None,
        provider_name: str = "Anthropic",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_api_key:
            raise RuntimeError(
                "Anthropic API key is required: pass api_key= or set ANTHROPIC_API_KEY"
            )
        self._api_key = resolved_api_key
        self._base_url = base_url
        self._headers = dict(headers or {})
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._max_retry_delay_seconds = max_retry_delay_seconds
        self._max_tokens = max_tokens
        self._thinking_budget_tokens = thinking_budget_tokens
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
    ) -> AsyncIterator[AssistantMessageEvent]:
        """Stream one response as assistant message events."""
        del session_id
        raw = self._stream_provider_events(
            model=model, system=system, messages=messages, tools=tools, signal=signal
        )
        return canonicalize_provider_stream(
            raw, api="anthropic-messages", provider="anthropic", model=model
        )

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolSpec],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        async def iterator() -> AsyncIterator[ProviderEvent]:
            if signal is not None and signal.is_cancelled():
                yield ProviderAbortedEvent()
                return

            client = self._get_client()
            payload = _build_messages_payload(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=self._max_tokens,
                thinking_budget_tokens=self._thinking_budget_tokens,
            )
            headers = {
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
                "x-api-key": self._api_key,
                **self._headers,
            }
            url = f"{self._base_url.rstrip('/')}/messages"

            attempt = 0
            while True:
                emitted_content = False
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
                        stream_error: dict[str, JSONValue] | None = None
                        content_parts: list[str] = []
                        thinking_parts: list[str] = []
                        thinking_signature: str | None = None
                        tool_builders: dict[int, _AnthropicToolBuilder] = {}
                        finish_reason: str | None = None
                        usage: Usage | None = None

                        async for line in response.aiter_lines():
                            if signal is not None and signal.is_cancelled():
                                yield ProviderAbortedEvent()
                                return

                            event = _parse_sse_line(line)
                            if event is None:
                                continue
                            chunk = _loads_object(event)
                            if chunk is None:
                                yield ProviderErrorEvent(
                                    message="Provider returned invalid JSON chunk"
                                )
                                return

                            event_type = chunk.get("type")
                            if event_type == "message_start":
                                message = chunk.get("message")
                                if isinstance(message, Mapping):
                                    usage = _usage_from_message_start(message.get("usage"))
                            elif event_type == "content_block_start":
                                block = chunk.get("content_block")
                                if isinstance(block, Mapping) and block.get("type") == "tool_use":
                                    index = int(chunk.get("index", 0))
                                    builder = tool_builders.setdefault(
                                        index, _AnthropicToolBuilder()
                                    )
                                    builder.id = _string_or_empty(block.get("id"))
                                    builder.name = _string_or_empty(block.get("name"))
                                    emitted_content = True
                            elif event_type == "content_block_delta":
                                delta = chunk.get("delta")
                                if not isinstance(delta, Mapping):
                                    continue
                                delta_type = delta.get("type")
                                if delta_type == "text_delta":
                                    text = _string_or_empty(delta.get("text"))
                                    if text:
                                        emitted_content = True
                                        content_parts.append(text)
                                        yield ProviderTextDeltaEvent(delta=text)
                                elif delta_type == "thinking_delta":
                                    thinking = _string_or_empty(delta.get("thinking"))
                                    if thinking:
                                        emitted_content = True
                                        thinking_parts.append(thinking)
                                        yield ProviderThinkingDeltaEvent(delta=thinking)
                                elif delta_type == "signature_delta":
                                    signature = _string_or_empty(delta.get("signature"))
                                    if signature:
                                        thinking_signature = (
                                            f"{thinking_signature or ''}{signature}"
                                        )
                                elif delta_type == "input_json_delta":
                                    index = int(chunk.get("index", 0))
                                    builder = tool_builders.setdefault(
                                        index, _AnthropicToolBuilder()
                                    )
                                    builder.arguments_parts.append(
                                        _string_or_empty(delta.get("partial_json"))
                                    )
                                    emitted_content = True
                            elif event_type == "message_delta":
                                delta = chunk.get("delta")
                                if isinstance(delta, Mapping):
                                    finish_reason = (
                                        _string_or_empty(delta.get("stop_reason")) or finish_reason
                                    )
                                usage = _apply_message_delta_usage(usage, chunk.get("usage"))
                            elif event_type == "error":
                                error_type, message = _anthropic_stream_error_details(chunk)
                                if (
                                    not emitted_content
                                    and self._should_retry(attempt)
                                    and _retryable_anthropic_stream_error(error_type)
                                ):
                                    stream_error = chunk
                                    break
                                yield ProviderErrorEvent(
                                    message=message,
                                    data={"event": chunk, "attempts": attempt + 1},
                                    error_type=_classify_anthropic_stream_error(
                                        error_type, message
                                    ),
                                )
                                return

                        if stream_error is not None:
                            error_type, _message = _anthropic_stream_error_details(stream_error)
                            delay = retry_delay_seconds(
                                attempt, max_delay_seconds=self._max_retry_delay_seconds
                            )
                            yield provider_retry_event(
                                attempt=attempt,
                                max_retries=self._max_retries,
                                delay_seconds=delay,
                                reason=f"stream error ({error_type or 'unknown'})",
                                data={"event": stream_error},
                            )
                            attempt += 1
                            should_continue = await wait_for_retry(delay, signal=signal)
                            if not should_continue:
                                yield ProviderAbortedEvent()
                                return
                            continue

                        tool_calls = [
                            builder.build(index) for index, builder in sorted(tool_builders.items())
                        ]
                        for tool_call in tool_calls:
                            yield ProviderToolCallEvent(tool_call=tool_call)

                        content = assistant_content("".join(content_parts), tool_calls)
                        if thinking_parts:
                            content.insert(
                                0,
                                ThinkingContent(
                                    thinking="".join(thinking_parts),
                                    thinking_signature=thinking_signature,
                                ),
                            )
                        yield ProviderResponseEndEvent(
                            message=AssistantMessage(content=content, usage=usage or Usage()),
                            finish_reason=finish_reason,
                        )
                        return
                except httpx.HTTPError as exc:
                    if not emitted_content and self._should_retry(attempt):
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


def _anthropic_stream_error_details(event: Mapping[str, JSONValue]) -> tuple[str, str]:
    """Return the provider classification and message from an Anthropic SSE error."""
    error = event.get("error")
    if not isinstance(error, Mapping):
        return "", "Provider returned an error"
    error_type = _string_or_empty(error.get("type"))
    message = _string_or_empty(error.get("message")) or "Provider returned an error"
    return error_type, message


def _retryable_anthropic_stream_error(error_type: str) -> bool:
    """Return whether an Anthropic SSE error is transient and safe to retry."""
    return error_type.lower() in _TRANSIENT_ANTHROPIC_STREAM_ERROR_TYPES


def _classify_anthropic_stream_error(error_type: str, message: str) -> ErrorType:
    """Classify a non-retryable mid-stream Anthropic SSE ``error`` event.

    Mirrors ``classify_provider_error_type`` for the HTTP>=400 path, but this
    error arrives as an SSE ``error`` event's ``type``/``message`` pair
    rather than an HTTP status/body, so it can't reuse that helper directly.
    """
    if error_type.lower() == "rate_limit_error":
        return "rate_limit"
    if classify_provider_error_type(status_code=None, body=message) == "context_overflow":
        return "context_overflow"
    return "other"


class _AnthropicToolBuilder:
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments_parts: list[str] = []

    def build(self, index: int) -> ToolCall:
        arguments_text = "".join(self.arguments_parts)
        arguments = _loads_object(arguments_text) if arguments_text else {}
        if arguments is None:
            arguments = {"_raw_arguments": arguments_text}
        return ToolCall(id=self.id or f"tool-call-{index}", name=self.name, arguments=arguments)


def _build_messages_payload(
    *,
    model: str,
    system: str,
    messages: Sequence[AgentMessage],
    tools: Sequence[ToolSpec],
    max_tokens: int | None = None,
    thinking_budget_tokens: int | None = None,
) -> dict[str, JSONValue]:
    resolved_max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    if thinking_budget_tokens is not None:
        resolved_max_tokens = max(resolved_max_tokens, thinking_budget_tokens + 1024)
    payload_messages = []
    for message in messages:
        converted = _anthropic_message(message)
        # Dropping foreign provider reasoning can empty a reasoning-only turn.
        # Anthropic rejects empty assistant content, so omit that inert turn too.
        if converted.get("role") == "assistant" and not converted.get("content"):
            continue
        payload_messages.append(converted)
    payload: dict[str, JSONValue] = {
        "model": model,
        "max_tokens": resolved_max_tokens,
        "stream": True,
        "system": system,
        "messages": cast("JSONValue", payload_messages),
    }
    if thinking_budget_tokens is not None:
        payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget_tokens}
    if tools:
        payload["tools"] = [_anthropic_tool(tool) for tool in tools]
    return payload


def _anthropic_message(message: AgentMessage) -> dict[str, JSONValue]:
    if isinstance(message, UserMessage):
        if isinstance(message.content, str):
            return {"role": "user", "content": message.content}
        user_content: list[JSONValue] = []
        for block in message.content:
            if isinstance(block, TextContent):
                user_content.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                user_content.append(_anthropic_image(block))
        return {"role": "user", "content": user_content}
    if isinstance(message, AssistantMessage):
        content: list[JSONValue] = []
        for block in message.content:
            if isinstance(block, TextContent):
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ThinkingContent):
                # Thinking signatures are provider-owned opaque state. Replaying
                # a foreign provider's signature as an Anthropic thinking block
                # makes an otherwise portable model switch fail validation.
                if message.api != "anthropic-messages":
                    continue
                thinking: dict[str, JSONValue] = {"type": "thinking", "thinking": block.thinking}
                if block.thinking_signature is not None:
                    thinking["signature"] = block.thinking_signature
                content.append(thinking)
            elif isinstance(block, ToolCall):
                content.append(
                    {
                        "type": "tool_use",
                        "id": portable_tool_call_id(block.id),
                        "name": block.name,
                        "input": block.arguments,
                    }
                )
        return {"role": "assistant", "content": content}
    if isinstance(message, ToolResultMessage):
        result_content: list[JSONValue] = []
        for block in message.content:
            if isinstance(block, TextContent):
                result_content.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                result_content.append(_anthropic_image(block))
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": portable_tool_call_id(message.tool_call_id),
                    "content": result_content,
                    "is_error": bool(message.is_error),
                }
            ],
        }
    return _anthropic_message(message_to_user(message))


def _anthropic_image(image: ImageContent) -> dict[str, JSONValue]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": image.mime_type, "data": image.data},
    }


def _anthropic_tool(tool: ToolSpec) -> dict[str, JSONValue]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.input_schema),
    }


def _parse_sse_line(line: str) -> str | None:
    if not line.startswith("data:"):
        return None
    return line.removeprefix("data:").strip()


def _loads_object(text: str) -> dict[str, Any] | None:
    try:
        value = loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _string_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage_from_message_start(raw: object) -> Usage:
    """Build a Usage from the ``message_start`` event's ``message.usage``."""
    data = raw if isinstance(raw, Mapping) else {}
    cache_creation = data.get("cache_creation")
    cache_write_1h = (
        _int_or_none(cache_creation.get("ephemeral_1h_input_tokens"))
        if isinstance(cache_creation, Mapping)
        else None
    )
    usage = Usage(
        input=_int_or_none(data.get("input_tokens")) or 0,
        output=_int_or_none(data.get("output_tokens")) or 0,
        cache_read=_int_or_none(data.get("cache_read_input_tokens")) or 0,
        cache_write=_int_or_none(data.get("cache_creation_input_tokens")) or 0,
        cache_write_1h=cache_write_1h,
    )
    usage.total_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
    return usage


def _apply_message_delta_usage(usage: Usage | None, raw: object) -> Usage | None:
    """Apply the ``message_delta`` event's ``usage`` onto the running Usage."""
    if not isinstance(raw, Mapping):
        return usage
    usage = usage or Usage()
    if (value := _int_or_none(raw.get("input_tokens"))) is not None:
        usage.input = value
    if (value := _int_or_none(raw.get("output_tokens"))) is not None:
        usage.output = value
    if (value := _int_or_none(raw.get("cache_read_input_tokens"))) is not None:
        usage.cache_read = value
    if (value := _int_or_none(raw.get("cache_creation_input_tokens"))) is not None:
        usage.cache_write = value
    details = raw.get("output_tokens_details")
    if isinstance(details, Mapping):
        thinking = _int_or_none(details.get("thinking_tokens"))
        if thinking is not None:
            usage.reasoning = thinking
    usage.total_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
    return usage


__all__ = ["AnthropicProvider"]
