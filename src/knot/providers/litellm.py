"""LiteLLM adapter: streams any LiteLLM-supported model as knot events.

HARD REQUIREMENT: ``litellm`` is a large optional dependency. ``import
litellm`` must never happen just because someone imported ``knot.providers``
or even ``knot.providers.litellm`` itself — only constructing or using
``LiteLLMProvider`` may trigger it. ``knot/providers/__init__.py`` does not
import this module at all, and this module itself only imports ``litellm``
lazily, inside ``_require_litellm()``, called from ``LiteLLMProvider``
methods. If ``litellm`` is not installed, that call raises a clear
``ImportError`` pointing at ``pip install knot-ai[litellm]``.

LiteLLM normalizes every backend's streaming chunks to (approximately) the
OpenAI ``/chat/completions`` chunk shape, so this adapter reuses the wire
parser and message/tool conversion helpers from ``openai_compatible.py``
rather than re-implementing chunk parsing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from json import dumps
from typing import Any

from knot.providers._provider_events import (
    ProviderAbortedEvent,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseStartEvent,
)
from knot.providers._retry import (
    is_transient_status,
    provider_retry_event,
    retry_delay_seconds,
    wait_for_retry,
)
from knot.providers._stream import canonicalize_provider_stream
from knot.providers.events import AssistantMessageEvent
from knot.providers.messages import AgentMessage
from knot.providers.openai_compatible import (
    _ChatStreamParser,
    _messages_to_openai_chat,
    _tool_to_openai,
)
from knot.providers.provider import CancellationToken, ToolSpec

DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_RETRY_DELAY_SECONDS = 1.0

_TRANSIENT_LITELLM_EXCEPTION_NAMES = frozenset(
    {
        "RateLimitError",
        "Timeout",
        "APIConnectionError",
        "ServiceUnavailableError",
        "InternalServerError",
    }
)

_litellm_module: Any = None


def _require_litellm() -> Any:
    """Import and cache the ``litellm`` module, or raise a clear install hint."""
    global _litellm_module
    if _litellm_module is None:
        try:
            import litellm as litellm_module  # noqa: PLC0415 - intentionally lazy
        except ImportError as exc:
            raise ImportError(
                "knot.providers.litellm requires the 'litellm' package. "
                "Install it with: pip install 'knot-ai[litellm]'"
            ) from exc
        _litellm_module = litellm_module
    return _litellm_module


class LiteLLMProvider:
    """Provider adapter over the LiteLLM SDK's async streaming completion."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        max_tokens: int | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_delay_seconds: float = DEFAULT_MAX_RETRY_DELAY_SECONDS,
        provider_name: str = "LiteLLM",
        extra_completion_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        _require_litellm()  # fail fast at construction if litellm is missing
        self._api_key = api_key
        self._api_base = api_base
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._max_retry_delay_seconds = max_retry_delay_seconds
        self._provider_name = provider_name
        self._extra_completion_kwargs = dict(extra_completion_kwargs or {})

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
            raw, api="litellm", provider=self._provider_name, model=model
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

            litellm_module = _require_litellm()
            messages_payload = [
                {"role": "system", "content": system},
                *_messages_to_openai_chat(messages),
            ]
            tools_payload = [_tool_to_openai(tool) for tool in tools] if tools else None

            completion_kwargs: dict[str, Any] = dict(self._extra_completion_kwargs)
            if self._api_key is not None:
                completion_kwargs["api_key"] = self._api_key
            if self._api_base is not None:
                completion_kwargs["api_base"] = self._api_base
            if self._max_tokens is not None:
                completion_kwargs["max_tokens"] = self._max_tokens

            attempt = 0
            while True:
                parser = _ChatStreamParser()
                try:
                    response = await litellm_module.acompletion(
                        model=model,
                        messages=messages_payload,
                        tools=tools_payload,
                        stream=True,
                        stream_options={"include_usage": True},
                        **completion_kwargs,
                    )
                except Exception as exc:  # noqa: BLE001 - litellm raises many provider types
                    if self._should_retry(attempt, exc):
                        delay = retry_delay_seconds(
                            attempt, max_delay_seconds=self._max_retry_delay_seconds
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._max_retries,
                            delay_seconds=delay,
                            reason=type(exc).__name__,
                            data={"error": str(exc), "error_type": type(exc).__name__},
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            yield ProviderAbortedEvent()
                            return
                        continue
                    yield ProviderErrorEvent(
                        message=str(exc),
                        data={"error_type": type(exc).__name__, "attempts": attempt + 1},
                    )
                    return

                yield ProviderResponseStartEvent(model=model)
                try:
                    async for chunk in response:
                        if signal is not None and signal.is_cancelled():
                            yield ProviderAbortedEvent()
                            return
                        events, stop = parser.feed(dumps(_chunk_to_dict(chunk)))
                        for event in events:
                            yield event
                        if stop:
                            break
                except Exception as exc:  # noqa: BLE001
                    if not parser.emitted_content and self._should_retry(attempt, exc):
                        delay = retry_delay_seconds(
                            attempt, max_delay_seconds=self._max_retry_delay_seconds
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._max_retries,
                            delay_seconds=delay,
                            reason=type(exc).__name__,
                            data={"error": str(exc), "error_type": type(exc).__name__},
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            yield ProviderAbortedEvent()
                            return
                        continue
                    yield ProviderErrorEvent(
                        message=str(exc), data={"attempts": attempt + 1}
                    )
                    return

                if parser.fatal:
                    return
                for event in parser.finalize():
                    yield event
                return

        return iterator()

    def _should_retry(self, attempt: int, exc: Exception) -> bool:
        if attempt >= self._max_retries:
            return False
        return _is_transient_litellm_error(exc)


def _is_transient_litellm_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return is_transient_status(status_code)
    return type(exc).__name__ in _TRANSIENT_LITELLM_EXCEPTION_NAMES


def _chunk_to_dict(chunk: Any) -> dict[str, Any]:
    """Normalize a LiteLLM streaming chunk object into a plain dict."""
    for attr in ("model_dump", "dict"):
        method = getattr(chunk, attr, None)
        if callable(method):
            try:
                dumped = method()
            except Exception:  # noqa: BLE001 - best-effort normalization
                continue
            if isinstance(dumped, dict):
                return dumped
    if isinstance(chunk, Mapping):
        return dict(chunk)
    return {}


__all__ = ["LiteLLMProvider"]
