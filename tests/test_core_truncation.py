"""Direct tests of the result-size backstop helper."""

from __future__ import annotations

from knot.core.tools import AgentToolResult
from knot.core.truncation import truncate_tool_result
from knot.providers.messages import TextContent


def test_truncate_tool_result_noop_when_no_limit() -> None:
    result = AgentToolResult(content=[TextContent(text="hello")])
    assert truncate_tool_result(result, None) is result


def test_truncate_tool_result_noop_under_limit() -> None:
    result = AgentToolResult(content=[TextContent(text="hello")])
    assert truncate_tool_result(result, 1000) is result


def test_truncate_tool_result_truncates_oversized_content() -> None:
    original_text = "y" * 500
    result = AgentToolResult(content=[TextContent(text=original_text)])

    truncated = truncate_tool_result(result, 100)

    assert len(truncated.text.encode("utf-8")) <= 100
    assert "[truncated: result was 500 bytes, limit 100]" in truncated.text
    assert truncated.details["truncated"] is True
    assert truncated.details["original_bytes"] == 500
    assert truncated.details["full_content"] == original_text
