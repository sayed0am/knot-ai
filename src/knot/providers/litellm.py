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

from knot.providers._http_errors import classify_provider_error_type
from knot.providers._provider_events import (
    ProviderAbortedEvent,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseEndEvent,
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
from knot.providers.messages import AgentMessage, ErrorType, UsageCost
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
        max_tokens: int | None = None,
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
            resolved_max_tokens = max_tokens if max_tokens is not None else self._max_tokens
            if resolved_max_tokens is not None:
                completion_kwargs["max_tokens"] = resolved_max_tokens

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
                        error_type=_classify_litellm_exception(exc),
                    )
                    return

                yield ProviderResponseStartEvent(model=model)
                response_cost: float | None = None
                try:
                    async for chunk in response:
                        if signal is not None and signal.is_cancelled():
                            yield ProviderAbortedEvent()
                            return
                        response_cost = _extract_response_cost(chunk) or response_cost
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
                        message=str(exc),
                        data={"attempts": attempt + 1},
                        error_type=_classify_litellm_exception(exc),
                    )
                    return

                if parser.fatal:
                    return
                response_cost = response_cost or _extract_response_cost(response)
                finalized = parser.finalize()
                if response_cost is not None:
                    _apply_response_cost(finalized, response_cost)
                for event in finalized:
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


_LITELLM_CONTEXT_OVERFLOW_EXCEPTION_NAMES = frozenset({"ContextWindowExceededError"})
_LITELLM_RATE_LIMIT_EXCEPTION_NAMES = frozenset({"RateLimitError"})


def _classify_litellm_exception(exc: Exception) -> ErrorType:
    """Best-effort classification of a LiteLLM-raised exception.

    LiteLLM normalizes every backend's errors into its own typed exception
    hierarchy (``litellm.ContextWindowExceededError``,
    ``litellm.RateLimitError``, ...), so the exception's class name is the
    most reliable signal — checked by name rather than ``isinstance`` so this
    module never needs to import ``litellm`` eagerly (see the module
    docstring). Falls back to the shared HTTP-shaped heuristic against the
    exception's status code and message, then to ``"other"`` when nothing
    matches — this is explicitly best-effort per the design.
    """
    name = type(exc).__name__
    if name in _LITELLM_CONTEXT_OVERFLOW_EXCEPTION_NAMES:
        return "context_overflow"
    if name in _LITELLM_RATE_LIMIT_EXCEPTION_NAMES:
        return "rate_limit"
    status_code = getattr(exc, "status_code", None)
    return classify_provider_error_type(
        status_code=status_code if isinstance(status_code, int) else None,
        body=str(exc),
    )


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


def _extract_response_cost(candidate: Any) -> float | None:
    """Read litellm's native per-response cost off a chunk or stream object.

    litellm attaches ``_hidden_params["response_cost"]`` once cost is known
    for a response; depending on version that lands on a streamed chunk, the
    final chunk, or the stream wrapper itself, so this is called against
    every chunk as it arrives and, as a fallback, against the exhausted
    stream object — never computed locally (design D3: pass-through only).
    """
    hidden_params = getattr(candidate, "_hidden_params", None)
    if not isinstance(hidden_params, Mapping):
        return None
    cost = hidden_params.get("response_cost")
    return cost if isinstance(cost, int | float) and not isinstance(cost, bool) else None


def _apply_response_cost(events: list[ProviderEvent], cost: float) -> None:
    """Stamp ``cost`` onto the finalized response-end event's usage, if any."""
    for event in events:
        if isinstance(event, ProviderResponseEndEvent):
            event.message.usage.cost = UsageCost(total=cost)


__all__ = ["LiteLLMProvider"]
