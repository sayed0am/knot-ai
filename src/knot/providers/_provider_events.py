"""Provider-neutral streaming events emitted internally by adapter parsers.

This is a private bridge: adapters parse provider-native chunks into these
coarse events, then ``_stream.py`` canonicalizes them into the public
``AssistantMessageEvent`` stream.

``ProviderAbortedEvent`` lets adapters report a cancelled request as
``AssistantErrorEvent(reason="aborted")`` distinctly from a network/HTTP
error.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from knot.providers.messages import AssistantMessage, ToolCall
from knot.providers.types import JSONValue


class ProviderResponseStartEvent(BaseModel):
    """The provider has started a model response."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["response_start"] = "response_start"
    model: str
    response_provider: str | None = None


class ProviderRetryEvent(BaseModel):
    """The provider adapter is retrying a transient request failure."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["retry"] = "retry"
    attempt: int
    max_attempts: int
    delay_seconds: float
    message: str
    data: dict[str, JSONValue] | None = None


class ProviderTextDeltaEvent(BaseModel):
    """A streamed text fragment from the provider."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text_delta"] = "text_delta"
    delta: str


class ProviderThinkingDeltaEvent(BaseModel):
    """A streamed thinking/reasoning fragment from the provider."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["thinking_delta"] = "thinking_delta"
    delta: str


class ProviderToolCallEvent(BaseModel):
    """A complete tool call requested by the model."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"] = "tool_call"
    tool_call: ToolCall


class ProviderResponseEndEvent(BaseModel):
    """The provider has completed a model response."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["response_end"] = "response_end"
    message: AssistantMessage
    finish_reason: str | None = None


class ProviderErrorEvent(BaseModel):
    """A provider-level error that can be surfaced by the agent layer."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["error"] = "error"
    message: str
    data: dict[str, JSONValue] | None = None
    response_provider: str | None = None


class ProviderAbortedEvent(BaseModel):
    """The request was cancelled via the ``CancellationToken``."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["aborted"] = "aborted"


type ProviderEvent = (
    ProviderResponseStartEvent
    | ProviderRetryEvent
    | ProviderTextDeltaEvent
    | ProviderThinkingDeltaEvent
    | ProviderToolCallEvent
    | ProviderResponseEndEvent
    | ProviderErrorEvent
    | ProviderAbortedEvent
)
