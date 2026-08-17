"""Event grammar emitted by the core agent loop.

A run's events fall into a fixed, discriminated-on-``type`` grammar. The
notable departure from a plain tool-call loop is ``AgentEndEvent``: it
carries an explicit ``outcome`` (``completed``, ``error``, ``aborted``, or
``waiting_input``) and, for ``waiting_input``, the ``PendingInputRequest``
park signals the run stopped for — durable human-in-the-loop "park by
persist" primitives that a later work package resumes from.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from knot.core.tools import AgentToolResult
from knot.providers.events import AssistantMessageEvent
from knot.providers.messages import (
    AgentMessage,
    ToolResultMessage,
    WireModel,
    current_timestamp_ms,
)
from knot.providers.types import JSONValue

RunOutcome = Literal["completed", "error", "aborted", "waiting_input"]
PendingRequestKind = Literal["tool_approval", "question", "authorization", "child_session"]


class PendingInputRequest(WireModel):
    """A durable park signal describing one thing a human must decide.

    ``authorization`` is reserved for a later work package and is never
    produced by this layer yet. ``child_session`` is produced when a
    delegation tool call cannot complete synchronously because the child
    session it spawned itself parked or is still running (see
    ``knot.authoring.runtime``); unlike the other kinds it is never resolved
    by a direct human response — only by the child session's own eventual
    completion (see ``knot.core.hitl.resume.write_child_completion``).
    ``ttl_seconds`` (``None`` = never expires) is live: a ``tool_approval``
    park's ttl comes from the deciding ``RequireApproval(ttl_seconds=...)``
    (see ``knot.core.decisions``); a ``question`` park's ttl comes from
    ``run_agent_loop``'s ``default_question_ttl_seconds``. Expiry itself —
    turning an overdue pending request into an automatic denial — is handled
    by ``knot.core.hitl.resume``.
    """

    id: str
    kind: PendingRequestKind
    tool_call_id: str
    tool_name: str
    payload: dict[str, JSONValue] = Field(default_factory=dict)
    created_at: int = Field(default_factory=current_timestamp_ms)
    ttl_seconds: int | None = None


class AgentStartEvent(WireModel):
    type: Literal["agent_start"] = "agent_start"


class TurnStartEvent(WireModel):
    type: Literal["turn_start"] = "turn_start"
    turn: int


class MessageStartEvent(WireModel):
    type: Literal["message_start"] = "message_start"
    message: AgentMessage


class MessageUpdateEvent(WireModel):
    type: Literal["message_update"] = "message_update"
    message: AgentMessage
    assistant_message_event: AssistantMessageEvent


class MessageEndEvent(WireModel):
    type: Literal["message_end"] = "message_end"
    message: AgentMessage


class ToolExecutionStartEvent(WireModel):
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call_id: str
    tool_name: str
    args: dict[str, JSONValue] = Field(default_factory=dict)
    timestamp: int = Field(default_factory=current_timestamp_ms)


class ToolExecutionUpdateEvent(WireModel):
    type: Literal["tool_execution_update"] = "tool_execution_update"
    tool_call_id: str
    tool_name: str
    args: dict[str, JSONValue] = Field(default_factory=dict)
    partial_result: AgentToolResult
    timestamp: int = Field(default_factory=current_timestamp_ms)


class ToolExecutionEndEvent(WireModel):
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call_id: str
    tool_name: str
    result: AgentToolResult
    is_error: bool
    timestamp: int = Field(default_factory=current_timestamp_ms)


class TurnEndEvent(WireModel):
    type: Literal["turn_end"] = "turn_end"
    turn: int
    message: AgentMessage
    tool_results: list[ToolResultMessage] = Field(default_factory=list)


class AgentEndEvent(WireModel):
    type: Literal["agent_end"] = "agent_end"
    outcome: RunOutcome
    messages: list[AgentMessage] = Field(default_factory=list)
    pending_requests: list[PendingInputRequest] = Field(default_factory=list)


class SubagentCalledEvent(WireModel):
    """A delegation tool call just spawned a child session.

    Emitted by ``knot.authoring.runtime`` (via ``emit_loop_event``, see
    ``knot.core.loop``) right after the child session is created, on the
    parent's own event stream — a control-plane event, not a durable fact:
    ``PersistenceSubscriber`` ignores it by construction (it only matches
    ``MessageEndEvent``/``AgentEndEvent``).
    """

    type: Literal["subagent_called"] = "subagent_called"
    tool_call_id: str
    subagent_id: str
    child_session_id: str


class SubagentCompletedEvent(WireModel):
    """A previously-announced delegation call's child session reached a
    terminal state for this run — ``outcome`` is the child's own
    ``AgentEndEvent.outcome`` verbatim, including ``"waiting_input"`` when
    the child itself parked. Emitted alongside ``SubagentCalledEvent``; see
    its docstring.
    """

    type: Literal["subagent_completed"] = "subagent_completed"
    tool_call_id: str
    subagent_id: str
    child_session_id: str
    outcome: str


class CompactionEvent(WireModel):
    """A compaction just ran during this run — a control-plane event, not a
    durable fact: ``PersistenceSubscriber`` ignores it by construction (it
    only matches ``MessageEndEvent``/``AgentEndEvent``), the same way
    ``SubagentCalledEvent``/``SubagentCompletedEvent`` do. The durable
    record is the ``"compaction"`` session entry (see
    ``knot.core.session.entries.Compaction``), appended before this event is
    emitted; this is only the stream-side announcement of that fact, so a
    live consumer (the HTTP/SSE layer) can render what happened without
    re-deriving it from the log.

    ``covers_through_seq`` mirrors the durable entry's own field: the
    highest entry ``seq`` this compaction replaced. ``summary_bytes`` is the
    UTF-8 byte length of the summary message's text, a cheap size signal for
    a consumer that doesn't want to inspect the message itself.
    ``trigger`` distinguishes a between-turns pressure check
    (``"proactive"``) from a context-overflow retry (``"reactive"``); see
    ``knot.authoring.runtime`` for both call sites.
    """

    type: Literal["compaction"] = "compaction"
    covers_through_seq: int
    summary_bytes: int
    trigger: Literal["proactive", "reactive"]


type AgentEvent = Annotated[
    AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
    | SubagentCalledEvent
    | SubagentCompletedEvent
    | CompactionEvent,
    Field(discriminator="type"),
]

__all__ = [
    "AgentEndEvent",
    "AgentEvent",
    "AgentStartEvent",
    "CompactionEvent",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "PendingInputRequest",
    "PendingRequestKind",
    "RunOutcome",
    "SubagentCalledEvent",
    "SubagentCompletedEvent",
    "ToolExecutionEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "TurnEndEvent",
    "TurnStartEvent",
]
