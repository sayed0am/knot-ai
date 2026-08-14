"""History repair and provider-context filtering."""

from __future__ import annotations

from knot.core.tool_history import (
    INTERRUPTED_TOOL_RESULT,
    provider_context,
    repair_tool_history,
)
from knot.providers.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


def test_dangling_call_gets_synthetic_error_result() -> None:
    call = ToolCall(id="c1", name="get", arguments={})
    assistant = AssistantMessage(content=[call], stop_reason="toolUse")
    messages = [UserMessage(content="hi"), assistant]

    report = repair_tool_history(messages)

    assert report.synthesized_results == 1
    assert report.changed is True
    assert len(report.messages) == 3
    result = report.messages[-1]
    assert isinstance(result, ToolResultMessage)
    assert result.tool_call_id == "c1"
    assert result.is_error is True
    assert result.text == INTERRUPTED_TOOL_RESULT


def test_orphan_result_is_dropped() -> None:
    orphan = ToolResultMessage(tool_call_id="ghost", tool_name="get", content="oops")
    messages = [UserMessage(content="hi"), orphan]

    report = repair_tool_history(messages)

    assert report.dropped_orphan_results == 1
    assert orphan not in report.messages


def test_duplicate_result_prefers_real_over_synthetic() -> None:
    call = ToolCall(id="c1", name="get", arguments={})
    assistant = AssistantMessage(content=[call], stop_reason="toolUse")
    real_result = ToolResultMessage(tool_call_id="c1", tool_name="get", content="real answer")
    # A duplicate result for the same call id, out of adjacency order.
    duplicate = ToolResultMessage(tool_call_id="c1", tool_name="get", content="real answer")
    messages = [UserMessage(content="hi"), assistant, real_result, duplicate]

    report = repair_tool_history(messages)

    result_messages = [m for m in report.messages if isinstance(m, ToolResultMessage)]
    assert len(result_messages) == 1
    assert result_messages[0].text == "real answer"
    assert report.dropped_duplicate_results == 1


def test_provider_context_excludes_empty_error_turn_but_keeps_in_history() -> None:
    user = UserMessage(content="hi")
    empty_error = AssistantMessage(content=[], stop_reason="error", error_message="boom")
    history = [user, empty_error]

    context = provider_context(history)

    assert empty_error not in context
    assert user in context
    # Durable history itself is untouched by provider_context.
    assert empty_error in history


def test_provider_context_keeps_non_empty_error_turn() -> None:
    user = UserMessage(content="hi")
    error_with_content = AssistantMessage(
        content=[TextContent(text="partial")], stop_reason="error", error_message="boom"
    )
    history = [user, error_with_content]

    context = provider_context(history)

    assert error_with_content in context


def test_provider_context_repairs_dangling_calls() -> None:
    call = ToolCall(id="c1", name="get", arguments={})
    assistant = AssistantMessage(content=[call], stop_reason="toolUse")
    history = [UserMessage(content="hi"), assistant]

    context = provider_context(history)

    assert any(isinstance(m, ToolResultMessage) and m.tool_call_id == "c1" for m in context)
