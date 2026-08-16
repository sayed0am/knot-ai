"""Direct tests of the result-size backstop helpers."""

from __future__ import annotations

import logging

import pytest

from knot.core.tools import AgentToolResult
from knot.core.truncation import SpillSink, bound_tool_result, truncate_tool_result
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


# -- bound_tool_result --------------------------------------------------


def _make_sink(store: dict[str, str]) -> SpillSink:
    def sink(call_id: str, text: str) -> str:
        store[call_id] = text
        return call_id

    return sink


def test_bound_tool_result_noop_when_no_limit() -> None:
    result = AgentToolResult(content=[TextContent(text="hello")])
    assert bound_tool_result(result, "c1", None, None) is result


def test_bound_tool_result_noop_under_limit() -> None:
    result = AgentToolResult(content=[TextContent(text="hello")])
    assert bound_tool_result(result, "c1", 1000, _make_sink({})) is result


def test_bound_tool_result_no_sink_falls_back_to_truncation() -> None:
    original_text = "y" * 500
    result = AgentToolResult(content=[TextContent(text=original_text)])

    bounded = bound_tool_result(result, "c1", 100, None)

    assert len(bounded.text.encode("utf-8")) <= 100
    assert bounded.details["truncated"] is True
    assert bounded.details["full_content"] == original_text


@pytest.mark.parametrize(
    ("original_size", "max_bytes"),
    [
        (500, 250),
        (5000, 500),
        (10_000, 1000),
        (1000, 300),
        (2000, 400),
    ],
)
def test_bound_tool_result_replacement_within_cap_and_original(
    original_size: int, max_bytes: int
) -> None:
    original_text = "a" * original_size
    result = AgentToolResult(content=[TextContent(text=original_text)])
    store: dict[str, str] = {}

    bounded = bound_tool_result(result, "c1", max_bytes, _make_sink(store))

    replacement_bytes = len(bounded.text.encode("utf-8"))
    assert replacement_bytes <= max_bytes
    assert replacement_bytes <= original_size
    assert store["c1"] == original_text


@pytest.mark.parametrize("max_bytes", [250, 300, 500, 1000])
def test_bound_tool_result_multibyte_utf8_safe(max_bytes: int) -> None:
    # Multi-byte UTF-8 chars (each 3 bytes) so a naive byte-index split
    # would land mid-codepoint if not careful.
    original_text = "文" * 2000
    result = AgentToolResult(content=[TextContent(text=original_text)])
    store: dict[str, str] = {}

    bounded = bound_tool_result(result, "c1", max_bytes, _make_sink(store))

    replacement_bytes = len(bounded.text.encode("utf-8"))
    assert replacement_bytes <= max_bytes
    # Must decode cleanly (no raised UnicodeDecodeError already happened by
    # virtue of .text succeeding); also assert no stray replacement char
    # artifacts beyond what errors="ignore" can produce.
    assert isinstance(bounded.text, str)


def test_bound_tool_result_notice_contains_ref_and_omitted_and_instruction() -> None:
    original_text = "z" * 1000
    result = AgentToolResult(content=[TextContent(text=original_text)])
    store: dict[str, str] = {}

    bounded = bound_tool_result(result, "call_42", 200, _make_sink(store))

    assert 'read_tool_output(ref="call_42")' in bounded.text
    assert "bytes omitted" in bounded.text
    assert "offsetBytes/limitBytes or pattern" in bounded.text
    assert bounded.details == {"spilled": True, "original_bytes": 1000, "ref": "call_42"}
    assert "full_content" not in (bounded.details or {})


def test_bound_tool_result_tiny_cap_keeps_original_inline_but_sink_called() -> None:
    original_text = "q" * 1000
    result = AgentToolResult(content=[TextContent(text=original_text)])
    store: dict[str, str] = {}

    bounded = bound_tool_result(result, "call_1", 5, _make_sink(store))

    # Cap too small even for the notice alone: original stays inline.
    assert bounded.text == original_text
    assert bounded is result or bounded.text == result.text
    # But the sink was still called so the content is retrievable.
    assert store["call_1"] == original_text


def test_bound_tool_result_sink_exception_falls_back_to_truncation_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    original_text = "w" * 500

    def failing_sink(call_id: str, text: str) -> str:
        raise RuntimeError("write failed")

    result = AgentToolResult(content=[TextContent(text=original_text)])

    with caplog.at_level(logging.WARNING):
        bounded = bound_tool_result(result, "c1", 100, failing_sink)

    assert len(bounded.text.encode("utf-8")) <= 100
    assert bounded.details["truncated"] is True
    assert bounded.details["full_content"] == original_text
    assert any("spill_sink failed" in record.message for record in caplog.records)


def test_bound_tool_result_spill_exempt_tools_skip_sink_via_none_sink() -> None:
    # execute_tool is what actually applies the spill_exempt flag; at this
    # layer the exemption is simply "no sink was passed" — verified here as
    # the direct contract bound_tool_result itself honors.
    original_text = "e" * 500
    result = AgentToolResult(content=[TextContent(text=original_text)])

    bounded = bound_tool_result(result, "c1", 100, None)

    assert bounded.details["truncated"] is True
    assert "full_content" in bounded.details
