"""AgentTool, the standalone execute_tool unit, and its truncation hook."""

from __future__ import annotations

import asyncio

import pytest

from knot.core.tools import AgentTool, AgentToolResult, execute_tool
from knot.providers.messages import TextContent, ToolCall


async def test_execute_tool_runs_outside_a_live_loop() -> None:
    async def echo(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content=[TextContent(text=f"echo:{arguments['x']}")])

    tool = AgentTool(name="echo", description="", parameters={}, execute_fn=echo)
    call = ToolCall(id="c1", name="echo", arguments={"x": "hi"})

    result, is_error = await execute_tool(tool, call)

    assert is_error is False
    assert result.text == "echo:hi"


async def test_execute_tool_converts_exception_to_error_result() -> None:
    async def boom(tool_call_id, arguments, signal=None, on_update=None):
        raise RuntimeError("kaboom")

    tool = AgentTool(name="boom", description="", parameters={}, execute_fn=boom)
    call = ToolCall(id="c1", name="boom", arguments={})

    result, is_error = await execute_tool(tool, call)

    assert is_error is True
    assert "kaboom" in result.text


async def test_execute_tool_reraises_cancelled_error() -> None:
    async def cancels(tool_call_id, arguments, signal=None, on_update=None):
        raise asyncio.CancelledError

    tool = AgentTool(name="c", description="", parameters={}, execute_fn=cancels)
    call = ToolCall(id="c1", name="c", arguments={})

    with pytest.raises(asyncio.CancelledError):
        await execute_tool(tool, call)


async def test_execute_tool_forwards_streaming_updates() -> None:
    updates: list[str] = []

    async def streaming(tool_call_id, arguments, signal=None, on_update=None):
        if on_update is not None:
            on_update(AgentToolResult(content=[TextContent(text="step 1")]))
            on_update(AgentToolResult(content=[TextContent(text="step 2")]))
        return AgentToolResult(content=[TextContent(text="final")])

    tool = AgentTool(name="s", description="", parameters={}, execute_fn=streaming)
    call = ToolCall(id="c1", name="s", arguments={})

    def on_update(partial: AgentToolResult) -> None:
        updates.append(partial.text)

    result, is_error = await execute_tool(tool, call, on_update=on_update)

    assert is_error is False
    assert result.text == "final"
    assert updates == ["step 1", "step 2"]


async def test_execute_tool_rejects_execute_less_tool() -> None:
    tool = AgentTool(name="ask", description="", parameters={}, execute_fn=None)
    call = ToolCall(id="c1", name="ask", arguments={})

    with pytest.raises(ValueError, match="no executor"):
        await execute_tool(tool, call)


def test_agent_tool_input_schema_aliases_parameters() -> None:
    schema = {"type": "object", "properties": {}}
    tool = AgentTool(name="t", description="d", parameters=schema)
    assert tool.input_schema is schema


async def test_execute_tool_applies_max_result_bytes() -> None:
    async def big(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content=[TextContent(text="x" * 100)])

    tool = AgentTool(name="big", description="", parameters={}, execute_fn=big)
    call = ToolCall(id="c1", name="big", arguments={})

    result, is_error = await execute_tool(tool, call, max_result_bytes=20)

    assert is_error is False
    assert "truncated" in result.text
    assert result.details["truncated"] is True
    assert result.details["original_bytes"] == 100
    assert result.details["full_content"] == "x" * 100
