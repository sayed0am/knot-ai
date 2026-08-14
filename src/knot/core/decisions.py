"""Verdict hooks evaluated before every tool execution.

A ``ToolDecisionHook`` is asked about every tool call before it runs,
including calls to unknown tool names (``tool`` is ``None`` in that case) and
execute-less tools. The three outcomes drive the loop's tool phase:

- ``Allow`` (the default when no hook is configured): execute normally.
- ``Deny``: the turn continues with an error tool result carrying the
  reason; the tool never executes.
- ``RequireApproval``: the call is parked as a ``PendingInputRequest``
  instead of a tool result — no result is synthesized for it this run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from knot.providers.messages import ToolCall
from knot.providers.types import JSONValue

if TYPE_CHECKING:
    from knot.core.tools import AgentTool


@dataclass(frozen=True, slots=True)
class Allow:
    """Permit the tool call to execute normally."""


@dataclass(frozen=True, slots=True)
class Deny:
    """Refuse the tool call; the turn continues with an error result."""

    reason: str


@dataclass(frozen=True, slots=True)
class RequireApproval:
    """Park the tool call pending a human decision.

    ``payload`` becomes the resulting ``PendingInputRequest.payload``; when
    omitted, the model-authored call arguments are used. ``ttl_seconds``
    becomes ``PendingInputRequest.ttl_seconds``; ``None`` (the default)
    means the request never expires.
    """

    payload: dict[str, JSONValue] | None = None
    ttl_seconds: int | None = None


ToolDecision = Allow | Deny | RequireApproval

ToolDecisionHook = Callable[[ToolCall, "AgentTool | None"], Awaitable[ToolDecision]]

__all__ = ["Allow", "Deny", "RequireApproval", "ToolDecision", "ToolDecisionHook"]
