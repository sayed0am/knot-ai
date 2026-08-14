"""Provider-neutral assistant event stream over httpx.

Bottom layer: imports nothing from other knot layers.

``knot.providers.litellm`` is deliberately NOT imported here: it imports the
optional, heavyweight ``litellm`` SDK lazily and only when a
``LiteLLMProvider`` is actually constructed or used. Import it explicitly
(``from knot.providers.litellm import LiteLLMProvider``) when you need it.
"""

from knot.providers.anthropic import AnthropicProvider
from knot.providers.events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
    DoneReason,
    ErrorReason,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from knot.providers.fake import FakeProvider, error, reply, tool_call
from knot.providers.messages import (
    AgentMessage,
    AssistantContent,
    AssistantDiagnosticError,
    AssistantMessage,
    AssistantMessageDiagnostic,
    CustomMessage,
    ImageContent,
    StopReason,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultContent,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserContent,
    UserMessage,
    WireModel,
    assistant_content,
    content_text,
    current_timestamp_ms,
    message_text,
    message_to_user,
)
from knot.providers.openai_compatible import (
    OpenAICompatibleProvider,
    openai_provider,
    openrouter_provider,
)
from knot.providers.provider import (
    CancellationToken,
    ModelProvider,
    SimpleCancellationToken,
    ToolSpec,
)
from knot.providers.types import JSONObject, JSONPrimitive, JSONValue

__all__ = [
    "AgentMessage",
    "AnthropicProvider",
    "AssistantContent",
    "AssistantDiagnosticError",
    "AssistantDoneEvent",
    "AssistantErrorEvent",
    "AssistantMessage",
    "AssistantMessageDiagnostic",
    "AssistantMessageEvent",
    "AssistantStartEvent",
    "CancellationToken",
    "CustomMessage",
    "DoneReason",
    "ErrorReason",
    "FakeProvider",
    "ImageContent",
    "JSONObject",
    "JSONPrimitive",
    "JSONValue",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "SimpleCancellationToken",
    "StopReason",
    "TextContent",
    "TextDeltaEvent",
    "TextEndEvent",
    "TextStartEvent",
    "ThinkingContent",
    "ThinkingDeltaEvent",
    "ThinkingEndEvent",
    "ThinkingStartEvent",
    "ToolCall",
    "ToolCallDeltaEvent",
    "ToolCallEndEvent",
    "ToolCallStartEvent",
    "ToolResultContent",
    "ToolResultMessage",
    "ToolSpec",
    "Usage",
    "UsageCost",
    "UserContent",
    "UserMessage",
    "WireModel",
    "assistant_content",
    "content_text",
    "current_timestamp_ms",
    "error",
    "message_text",
    "message_to_user",
    "openai_provider",
    "openrouter_provider",
    "reply",
    "tool_call",
]
