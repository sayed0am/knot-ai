"""Entry types stored in a session's append-only log, and their payload models.

Every row in ``entries`` is one of a small, fixed set of ``type`` values.
The payload each type carries is a serialized wire model (dumped by alias,
so it round-trips through the same JSON the transport layer already uses):

- ``"message"``: one ``AgentMessage`` (the harness's ``MessageEndEvent``).
- ``"input_requested"``: one ``PendingInputRequest`` — a durable park signal.
- ``"input_resolved"``: one ``InputResolution`` — a human decision on a prior
  ``input_requested`` entry, matched by ``request_id``. Written by the
  human-in-the-loop package; defined here because state derivation
  (``knot.core.session.state``) needs to know its shape.
- ``"execution_started"``: one ``ExecutionStarted`` — durably marks that an
  approved tool call has begun executing, written by
  ``knot.core.hitl.resume`` between the ``input_resolved`` approval and the
  eventual tool-result ``message`` entry. Its sole purpose is crash-window
  detection: an ``execution_started`` with no matching tool-result means the
  process died mid-execution (see ``knot.core.hitl.resume.detect_crash_windows``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, TypeAdapter

from knot.core.events import PendingInputRequest
from knot.providers.messages import AgentMessage, WireModel, current_timestamp_ms

from .store import Entry

ENTRY_TYPE_MESSAGE: Literal["message"] = "message"
ENTRY_TYPE_INPUT_REQUESTED: Literal["input_requested"] = "input_requested"
ENTRY_TYPE_INPUT_RESOLVED: Literal["input_resolved"] = "input_resolved"
ENTRY_TYPE_EXECUTION_STARTED: Literal["execution_started"] = "execution_started"

ResolutionDecision = Literal["approved", "denied"]


class InputResolution(WireModel):
    """A human decision resolving one prior ``PendingInputRequest``."""

    request_id: str
    decision: ResolutionDecision
    resolved_by: str
    reason: str | None = None
    resolved_at: int = Field(default_factory=current_timestamp_ms)


class ExecutionStarted(WireModel):
    """Durable marker that an approved tool call has begun executing.

    Written between the ``input_resolved`` approval and the eventual
    tool-result ``message`` entry, so a process that dies mid-execution
    leaves a detectable gap (see ``knot.core.hitl.resume.detect_crash_windows``).
    """

    request_id: str
    tool_call_id: str
    tool_name: str
    started_at: int = Field(default_factory=current_timestamp_ms)


_agent_message_adapter: TypeAdapter[AgentMessage] = TypeAdapter(AgentMessage)


def entry_to_message(entry: Entry) -> AgentMessage:
    """Deserialize a ``"message"`` entry's payload back into an ``AgentMessage``."""
    return _agent_message_adapter.validate_python(entry.payload)


def entry_to_request(entry: Entry) -> PendingInputRequest:
    """Deserialize an ``"input_requested"`` entry's payload."""
    return PendingInputRequest.model_validate(entry.payload)


def entry_to_resolution(entry: Entry) -> InputResolution:
    """Deserialize an ``"input_resolved"`` entry's payload."""
    return InputResolution.model_validate(entry.payload)


def entry_to_execution_started(entry: Entry) -> ExecutionStarted:
    """Deserialize an ``"execution_started"`` entry's payload."""
    return ExecutionStarted.model_validate(entry.payload)


__all__ = [
    "ENTRY_TYPE_EXECUTION_STARTED",
    "ENTRY_TYPE_INPUT_REQUESTED",
    "ENTRY_TYPE_INPUT_RESOLVED",
    "ENTRY_TYPE_MESSAGE",
    "ExecutionStarted",
    "InputResolution",
    "ResolutionDecision",
    "entry_to_execution_started",
    "entry_to_message",
    "entry_to_request",
    "entry_to_resolution",
]
