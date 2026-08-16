"""Round-trip serialization tests for knot.providers.messages."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from knot.providers.messages import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
    assistant_content,
    content_text,
    message_text,
    message_to_user,
)

_AGENT_MESSAGE_ADAPTER: TypeAdapter[AgentMessage] = TypeAdapter(AgentMessage)


def _round_trip(model):
    dumped = model.model_dump(mode="json", by_alias=True)
    return type(model).model_validate(dumped), dumped


def test_wire_model_uses_camel_case_aliases_and_forbids_extra() -> None:
    call = ToolCall(id="1", name="foo", arguments={}, thought_signature="sig")
    dumped = call.model_dump(mode="json", by_alias=True)
    assert dumped["thoughtSignature"] == "sig"
    assert "thought_signature" not in dumped

    # extra="forbid": an unknown field must be rejected.
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"id": "1", "name": "foo", "unknownField": 1})


def test_user_message_round_trip_with_string_content() -> None:
    message = UserMessage(content="hello there")
    restored, dumped = _round_trip(message)
    assert restored == message
    assert dumped["role"] == "user"


def test_user_message_round_trip_with_block_content() -> None:
    message = UserMessage(
        content=[TextContent(text="hi"), ImageContent(data="Zm9v", mime_type="image/png")]
    )
    restored, dumped = _round_trip(message)
    assert restored == message
    assert dumped["content"][1]["mimeType"] == "image/png"


def test_assistant_message_round_trip_with_tool_calls() -> None:
    message = AssistantMessage(
        content=[
            TextContent(text="thinking about it"),
            ToolCall(id="call_1", name="lookup", arguments={"q": "invoices"}),
        ],
        api="anthropic-messages",
        provider="anthropic",
        model="claude-test",
        usage=Usage(input=10, output=5, total_tokens=15),
        stop_reason="toolUse",
    )
    restored, dumped = _round_trip(message)
    assert restored == message
    assert dumped["stopReason"] == "toolUse"
    assert dumped["content"][1]["type"] == "toolCall"


def test_assistant_message_accepts_string_content_as_convenience() -> None:
    message = AssistantMessage(content="hello")
    assert message.text == "hello"
    assert isinstance(message.content[0], TextContent)


def test_assistant_message_thinking_and_tool_calls_properties() -> None:
    message = AssistantMessage(
        content=[
            ThinkingContent(thinking="reasoning..."),
            TextContent(text="answer"),
            ToolCall(id="1", name="t", arguments={}),
        ]
    )
    assert message.thinking_text == "reasoning..."
    assert message.text == "answer"
    assert len(message.tool_calls) == 1
    assert message.tool_calls[0].name == "t"


def test_tool_result_message_round_trip() -> None:
    message = ToolResultMessage(
        tool_call_id="call_1",
        tool_name="lookup",
        content=[TextContent(text="result text")],
        is_error=False,
    )
    restored, dumped = _round_trip(message)
    assert restored == message
    assert dumped["role"] == "toolResult"
    assert dumped["toolCallId"] == "call_1"


def test_custom_message_round_trip() -> None:
    message = CustomMessage(custom_type="note", content="a note", display=False)
    restored, dumped = _round_trip(message)
    assert restored == message
    assert dumped["customType"] == "note"


def test_agent_message_union_discriminates_by_role() -> None:
    for message in (
        UserMessage(content="hi"),
        AssistantMessage(content="hi"),
        ToolResultMessage(tool_call_id="1", tool_name="t", content="done"),
        CustomMessage(custom_type="note", content="hi"),
    ):
        dumped = message.model_dump(mode="json", by_alias=True)
        restored = _AGENT_MESSAGE_ADAPTER.validate_python(dumped)
        assert type(restored) is type(message)
        assert restored == message


def test_assistant_content_builds_ordered_blocks() -> None:
    calls = [ToolCall(id="1", name="a", arguments={}), ToolCall(id="2", name="b", arguments={})]
    blocks = assistant_content("hi", calls)
    assert isinstance(blocks[0], TextContent)
    assert blocks[1:] == calls

    assert assistant_content("", []) == []


def test_content_text_handles_string_and_blocks() -> None:
    assert content_text("plain") == "plain"
    assert (
        content_text([TextContent(text="a"), ImageContent(data="x", mime_type="image/png")]) == "a"
    )


def test_message_text_delegates_to_text_property() -> None:
    user = UserMessage(content="hi")
    assistant = AssistantMessage(content="hello")
    tool_result = ToolResultMessage(tool_call_id="1", tool_name="t", content="ok")
    custom = CustomMessage(custom_type="note", content="note text")
    assert message_text(user) == "hi"
    assert message_text(assistant) == "hello"
    assert message_text(tool_result) == "ok"
    assert message_text(custom) == "note text"


def test_message_to_user_converts_custom_message() -> None:
    custom = CustomMessage(custom_type="note", content="note text", timestamp=123)
    converted = message_to_user(custom)
    assert isinstance(converted, UserMessage)
    assert converted.text == "note text"
    assert converted.timestamp == 123
