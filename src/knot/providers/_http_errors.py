"""Helpers for surfacing safe provider HTTP error details.

Also home to the shared HTTP-shaped classification the Anthropic and
OpenAI-compatible adapters both use to populate `AssistantMessage.error_type`
(see `knot.providers.messages.ErrorType`): both providers report errors as an
HTTP status plus a JSON body carrying a `message`/`code`, so one status-code
and phrase-matching heuristic covers both without duplicating it per
adapter. LiteLLM's classification lives in `litellm.py` instead, since its
errors arrive as typed Python exceptions rather than this HTTP shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from json import JSONDecodeError, loads
from typing import Any

from knot.providers.messages import ErrorType

_MAX_ERROR_DETAIL_LENGTH = 1000

# Conservative, deliberately narrow phrase match against the real wording
# both APIs use for a context/token-limit rejection. Anthropic: "prompt is
# too long: N tokens > M maximum". OpenAI-compatible: error code
# "context_length_exceeded" and/or a message mentioning "maximum context
# length". False negatives (an overflow that isn't caught) just fall back to
# "other" and are still handled — reactive compaction simply doesn't trigger
# for that response. False positives would misfire compaction, which is the
# worse failure, hence staying narrow rather than guessing broadly.
_CONTEXT_OVERFLOW_MARKERS = (
    "prompt is too long",
    "maximum context length",
    "context_length_exceeded",
)


def provider_http_error_message(
    *,
    provider_name: str,
    status_code: int,
    body: str,
    model: str | None = None,
) -> str:
    """Return an actionable, secret-free HTTP error message for a provider response."""
    prefix = f"{provider_name} request failed with status {status_code}"
    if model:
        prefix = f"{prefix} for model {model}"
    detail = provider_http_error_detail(body)
    if detail:
        return f"{prefix}: {detail}"
    return prefix


def provider_http_error_detail(body: str) -> str:
    """Extract a concise provider-supplied error detail from an HTTP body."""
    parsed = _loads_object(body)
    if parsed is not None:
        detail = provider_error_detail_from_mapping(parsed)
        if detail:
            return detail
    return body.strip()[:_MAX_ERROR_DETAIL_LENGTH]


def provider_error_detail_from_mapping(value: Mapping[str, Any]) -> str:
    """Return the most useful message/code from a provider error object."""
    error = value.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
        code = error.get("code")
        if isinstance(code, str) and code:
            return code
    for key in ("message", "detail", "error"):
        detail = value.get(key)
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, Mapping):
            nested = provider_error_detail_from_mapping(detail)
            if nested:
                return nested
    return ""


def _loads_object(value: str) -> Mapping[str, Any] | None:
    try:
        parsed = loads(value)
    except JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def classify_provider_error_type(*, status_code: int | None, body: str) -> ErrorType:
    """Classify an HTTP-shaped provider error into knot's coarse ``ErrorType``.

    ``status_code`` is checked first because it is the most reliable signal
    a provider gives (429 always means rate limiting); the message/code
    phrase match then covers context-window overflow, which providers
    otherwise report as a plain 400 indistinguishable from any other bad
    request. Anything that matches neither is ``"other"`` — never ``None``,
    since this is only ever called for a genuine error response.
    """
    if status_code == 429:
        return "rate_limit"
    if _looks_like_context_overflow(body):
        return "context_overflow"
    return "other"


def _looks_like_context_overflow(body: str) -> bool:
    candidates = [body]
    parsed = _loads_object(body)
    if parsed is not None:
        error = parsed.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            if isinstance(message, str):
                candidates.append(message)
            code = error.get("code")
            if isinstance(code, str):
                candidates.append(code)
            error_type = error.get("type")
            if isinstance(error_type, str):
                candidates.append(error_type)
    lowered = " ".join(candidates).lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


__all__ = [
    "classify_provider_error_type",
    "provider_error_detail_from_mapping",
    "provider_http_error_detail",
    "provider_http_error_message",
]
