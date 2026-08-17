"""Provider contract for knot's model adapters.

``ToolSpec`` is the provider-facing view of a tool: the minimal structural
surface (``name``, ``description``, ``input_schema``) needed to send a tool
definition to a model. knot's providers layer must not import from
``knot.core`` (where a richer tool type will live), so this defines that
surface as a ``Protocol``; ``knot.core``'s richer tool type conforms to it
structurally, with no import required.

``SimpleCancellationToken`` is a small mutable implementation of
``CancellationToken``, useful for tests and callers that don't need a
bespoke cancellation mechanism.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from knot.providers.events import AssistantMessageEvent
from knot.providers.messages import AgentMessage
from knot.providers.types import JSONValue


@runtime_checkable
class CancellationToken(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether the current stream should stop."""
        ...


@dataclass
class SimpleCancellationToken:
    """A minimal mutable ``CancellationToken`` implementation."""

    _cancelled: bool = field(default=False, repr=False)

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled


@runtime_checkable
class ToolSpec(Protocol):
    """The provider-facing view of a tool.

    ``knot.core``'s richer ``AgentTool`` conforms to this structurally, so
    providers never need to import from ``knot.core``.
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def input_schema(self) -> Mapping[str, JSONValue]: ...


class ModelProvider(Protocol):
    """Provider-neutral model stream interface."""

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
        """Stream one model response as assistant message events.

        Providers may use ``session_id`` for request routing or prompt-cache
        affinity. Unsupported providers ignore it.

        ``max_tokens`` and ``thinking_budget_tokens`` are per-call overrides
        of a provider's own constructor defaults (design D1): a provider
        that cannot honor one of them ignores it at request time rather than
        raising — the compile-time diagnostic in
        ``knot.authoring.compile`` is what keeps that silent-ignore case from
        actually happening for thinking budgets.
        """
        ...


__all__ = [
    "CancellationToken",
    "ModelProvider",
    "SimpleCancellationToken",
    "ToolSpec",
]
