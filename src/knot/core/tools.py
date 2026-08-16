"""Provider-neutral tool definitions and the standalone execution unit.

A tool's ``execute_fn`` may be ``None``: an execute-less tool. Calling such a
tool is not this module's decision to make (that lives in the loop's
tool-decision phase, see ``knot.core.decisions``) — ``execute_tool`` here
always assumes it has been handed a tool with an executor, and is usable on
its own, with no loop running, e.g. by a later resume path that executes an
approved tool call outside of a live run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import Field, model_validator

from knot.core.truncation import SpillSink, bound_tool_result
from knot.providers.messages import ImageContent, TextContent, ToolCall, WireModel
from knot.providers.provider import CancellationToken
from knot.providers.types import JSONValue

if TYPE_CHECKING:
    from knot.core.events import PendingInputRequest


class AgentToolResult(WireModel):
    """Final or partial result produced by a tool.

    Error status is not carried here: it is decided by the caller (a denial,
    a raised exception, an unknown tool) and lives on the resulting
    ``ToolResultMessage.is_error``, mirroring the provider layer's message
    model.
    """

    content: list[TextContent | ImageContent] = Field(default_factory=list)
    details: JSONValue = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_text_content(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        return data

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))


class ToolParkedError(Exception):
    """Raised by a tool's own executor to park its call instead of completing it.

    Lets a tool decide, mid-execution, that it cannot produce a result this
    run and must instead wait on something durable — the delegation tool in
    ``knot.authoring.runtime`` raises this when the subagent call it made
    itself parked or is still running. ``execute_tool`` lets this propagate
    rather than converting it into an error result, exactly like
    ``asyncio.CancelledError``: it is a control-flow signal, not a tool
    failure. The core loop's tool phase (``knot.core.loop``) catches it and
    converts it into a pending request for that call, joining the same park
    path as a gated (``RequireApproval``) or execute-less call.
    """

    def __init__(self, request: PendingInputRequest) -> None:
        super().__init__(f"tool call parked: {request.tool_call_id} ({request.kind})")
        self.request = request


ToolUpdateCallback = Callable[[AgentToolResult], None]


class ToolExecutor(Protocol):
    def __call__(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: CancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> Awaitable[AgentToolResult]:
        """Execute one validated tool call."""
        ...


@dataclass(frozen=True, slots=True)
class AgentTool:
    """A tool exposed to the core agent loop.

    Structurally conforms to ``knot.providers.provider.ToolSpec`` via
    ``name``, ``description``, and ``input_schema`` so providers never need
    to import this richer type.
    """

    name: str
    description: str
    parameters: Mapping[str, JSONValue]
    execute_fn: ToolExecutor | None = None
    idempotent: bool = False
    label: str | None = None
    #: Pager-like tools whose own output is already bounded by their
    #: parameters (offset/limit, page size, ...) opt out of the spill sink
    #: here so their results fall back to plain truncation instead —
    #: structurally prevents a spill→retrieve→spill loop on the framework's
    #: own retrieval tool without name-matching it.
    spill_exempt: bool = False

    @property
    def input_schema(self) -> Mapping[str, JSONValue]:
        """Alias for ``parameters``, matching the provider-facing ``ToolSpec``."""
        return self.parameters


async def execute_tool(
    tool: AgentTool,
    call: ToolCall,
    signal: CancellationToken | None = None,
    on_update: ToolUpdateCallback | None = None,
    *,
    max_result_bytes: int | None = None,
    spill_sink: SpillSink | None = None,
) -> tuple[AgentToolResult, bool]:
    """Run one validated tool call, standalone or from within the loop.

    Returns ``(result, is_error)``. Any exception raised by
    ``tool.execute_fn`` is converted into an error result and never
    propagates, except ``asyncio.CancelledError``, which always re-raises.
    When ``max_result_bytes`` is set, an oversized result is bounded (see
    ``knot.core.truncation.bound_tool_result``): ``spill_sink``, when given,
    stores the full text and replaces it with a bounded preview+notice;
    ``tool.spill_exempt`` tools always skip the sink and fall back to plain
    truncation, since their own output is already bounded by their
    parameters (see ``AgentTool.spill_exempt``).

    ``tool.execute_fn`` must not be ``None``. An execute-less tool is a
    parking decision made by the caller, not something this function can
    execute; calling it here is a programming error, not a tool failure.
    """
    if tool.execute_fn is None:
        raise ValueError(f"Tool {tool.name!r} has no executor and cannot be run directly")

    try:
        result = await tool.execute_fn(call.id, call.arguments, signal, on_update)
        is_error = False
    except asyncio.CancelledError:
        raise
    except ToolParkedError:
        raise
    except Exception as exc:  # noqa: BLE001 - tools are an isolation boundary
        result = AgentToolResult(content=[TextContent(text=str(exc))])
        is_error = True

    effective_sink = None if tool.spill_exempt else spill_sink
    bounded = bound_tool_result(result, call.id, max_result_bytes, effective_sink)
    return bounded, is_error


__all__ = [
    "AgentTool",
    "AgentToolResult",
    "ToolExecutor",
    "ToolParkedError",
    "ToolUpdateCallback",
    "execute_tool",
]
