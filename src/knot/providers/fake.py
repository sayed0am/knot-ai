"""Scripted fake model provider for deterministic offline tests.

A provider that replays predefined assistant event streams, with ergonomic
script builders (``reply``, ``tool_call``, ``error``) so a script reads as a
plain description of one turn's outcome::

    provider = FakeProvider([
        reply("hello"),
        tool_call("get_invoice", {"id": "inv_1"}),
        error("boom"),
    ])

Each helper returns a realistic ``start`` -> deltas -> ``done``/``error``
event sequence, matching what a live adapter would emit for that outcome.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from typing import Literal

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
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from knot.providers.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
)
from knot.providers.provider import CancellationToken
from knot.providers.types import JSONValue


def reply(
    text: str = "",
    *,
    tool_calls: Sequence[ToolCall] = (),
    thinking: str = "",
    model: str = "fake-model",
    provider: str = "fake",
    usage: Usage | None = None,
) -> list[AssistantMessageEvent]:
    """Build a start -> deltas -> done event sequence for one scripted reply."""
    partial = AssistantMessage(api="fake", provider=provider, model=model)
    events: list[AssistantMessageEvent] = [
        AssistantStartEvent(partial=partial.model_copy(deep=True))
    ]

    if thinking:
        index = len(partial.content)
        partial.content.append(ThinkingContent(thinking=""))
        events.append(
            ThinkingStartEvent(content_index=index, partial=partial.model_copy(deep=True))
        )
        block = partial.content[index]
        assert isinstance(block, ThinkingContent)
        block.thinking = thinking
        events.append(
            ThinkingDeltaEvent(
                content_index=index, delta=thinking, partial=partial.model_copy(deep=True)
            )
        )
        events.append(
            ThinkingEndEvent(
                content_index=index, content=thinking, partial=partial.model_copy(deep=True)
            )
        )

    if text:
        index = len(partial.content)
        partial.content.append(TextContent(text=""))
        events.append(TextStartEvent(content_index=index, partial=partial.model_copy(deep=True)))
        block = partial.content[index]
        assert isinstance(block, TextContent)
        block.text = text
        events.append(
            TextDeltaEvent(content_index=index, delta=text, partial=partial.model_copy(deep=True))
        )
        events.append(
            TextEndEvent(content_index=index, content=text, partial=partial.model_copy(deep=True))
        )

    for call in tool_calls:
        index = len(partial.content)
        partial.content.append(call.model_copy(deep=True))
        events.append(
            ToolCallStartEvent(content_index=index, partial=partial.model_copy(deep=True))
        )
        events.append(
            ToolCallEndEvent(
                content_index=index, tool_call=call, partial=partial.model_copy(deep=True)
            )
        )

    final = partial.model_copy(deep=True)
    final.usage = usage or Usage()
    final.stop_reason = "toolUse" if tool_calls else "stop"  # type: ignore[assignment]
    events.append(AssistantDoneEvent(reason=final.stop_reason, message=final))  # type: ignore[arg-type]
    return events


def tool_call(
    name: str,
    arguments: Mapping[str, JSONValue],
    *,
    id: str | None = None,
    text: str = "",
    model: str = "fake-model",
) -> list[AssistantMessageEvent]:
    """Build an event sequence for a scripted single tool-call reply."""
    call = ToolCall(id=id or f"call_{name}", name=name, arguments=dict(arguments))
    return reply(text, tool_calls=[call], model=model)


def error(
    message: str,
    *,
    reason: Literal["error", "aborted"] = "error",
    model: str = "fake-model",
    provider: str = "fake",
) -> list[AssistantMessageEvent]:
    """Build an event sequence for a scripted error (or aborted) reply."""
    partial = AssistantMessage(api="fake", provider=provider, model=model)
    start = AssistantStartEvent(partial=partial.model_copy(deep=True))
    failed = partial.model_copy(deep=True)
    failed.stop_reason = reason  # type: ignore[assignment]
    failed.error_message = message
    return [start, AssistantErrorEvent(reason=reason, error=failed)]


class FakeProvider:
    """A provider that replays predefined, scripted assistant event streams.

    Each item in ``scripts`` is a full event sequence for one
    ``stream_response`` call (typically built with ``reply``/``tool_call``/
    ``error``). Calls beyond the number of scripts replay an empty stream.
    """

    def __init__(self, scripts: Iterable[Sequence[AssistantMessageEvent]]) -> None:
        self._scripts: list[list[AssistantMessageEvent]] = [list(script) for script in scripts]
        self.calls: list[tuple[str, str, list[AgentMessage], list[object]]] = []
        self.session_ids: list[str | None] = []

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[object],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        self.calls.append((model, system, list(messages), list(tools)))
        self.session_ids.append(session_id)
        script = self._scripts.pop(0) if self._scripts else []

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            for event in script:
                if signal is not None and signal.is_cancelled():
                    return
                yield event

        return iterator()


__all__ = ["FakeProvider", "error", "reply", "tool_call"]
