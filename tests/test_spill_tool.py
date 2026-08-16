"""Unit tests for the framework ``read_tool_output`` retrieval tool."""

from __future__ import annotations

from knot.core.session.store import SessionStore
from knot.core.spill_tool import (
    HARD_LIMIT_BYTES,
    READ_TOOL_OUTPUT_TOOL_NAME,
    build_read_tool_output_placeholder,
    build_read_tool_output_tool,
)
from knot.core.tools import execute_tool
from knot.providers.messages import ToolCall


def _store_with_spill(session_id: str, call_id: str, text: str) -> SessionStore:
    store = SessionStore(":memory:")
    store.create_session("agent_a", session_id=session_id)
    store.save_spill(session_id, call_id, text)
    return store


async def _call(store: SessionStore, session_id: str, arguments: dict) -> tuple[str, bool]:
    tool = build_read_tool_output_tool(store, session_id)
    result, is_error = await execute_tool(
        tool, ToolCall(id="reader1", name=READ_TOOL_OUTPUT_TOOL_NAME, arguments=arguments)
    )
    return result.text, is_error


def test_tool_flags_are_spill_exempt_and_idempotent() -> None:
    store = SessionStore(":memory:")
    tool = build_read_tool_output_tool(store, "sess_1")

    assert tool.name == READ_TOOL_OUTPUT_TOOL_NAME
    assert tool.spill_exempt is True
    assert tool.idempotent is True
    store.close()


def test_placeholder_is_execute_less_but_otherwise_matches_the_live_tool() -> None:
    placeholder = build_read_tool_output_placeholder()
    store = SessionStore(":memory:")
    live = build_read_tool_output_tool(store, "sess_1")

    assert placeholder.execute_fn is None
    assert placeholder.name == live.name
    assert placeholder.description == live.description
    assert placeholder.parameters == live.parameters
    assert placeholder.spill_exempt is True
    assert placeholder.idempotent is True
    store.close()


async def test_paging_returns_exact_byte_slice() -> None:
    text = "0123456789" * 100  # 1000 bytes, all ASCII
    store = _store_with_spill("sess_1", "call_1", text)

    result_text, is_error = await _call(
        store, "sess_1", {"ref": "call_1", "offsetBytes": 10, "limitBytes": 20}
    )

    assert is_error is False
    body = result_text.split("\n", 1)[1]
    assert body == text.encode("utf-8")[10:30].decode("utf-8")
    store.close()


async def test_paging_header_states_ref_total_and_range() -> None:
    text = "y" * 500
    store = _store_with_spill("sess_1", "call_1", text)

    result_text, is_error = await _call(
        store, "sess_1", {"ref": "call_1", "offsetBytes": 0, "limitBytes": 50}
    )

    assert is_error is False
    header = result_text.split("\n", 1)[0]
    assert "ref=call_1" in header
    assert "total_bytes=500" in header
    assert "range=0:50" in header
    store.close()


async def test_default_offset_and_limit_when_unspecified() -> None:
    text = "z" * 100
    store = _store_with_spill("sess_1", "call_1", text)

    result_text, is_error = await _call(store, "sess_1", {"ref": "call_1"})

    assert is_error is False
    assert "range=0:100" in result_text
    store.close()


async def test_limit_bytes_above_ceiling_clamps_rather_than_errors() -> None:
    text = "a" * (HARD_LIMIT_BYTES + 5000)
    store = _store_with_spill("sess_1", "call_1", text)

    result_text, is_error = await _call(
        store, "sess_1", {"ref": "call_1", "limitBytes": HARD_LIMIT_BYTES * 10}
    )

    assert is_error is False
    assert f"range=0:{HARD_LIMIT_BYTES}" in result_text
    assert len(result_text.encode("utf-8")) <= HARD_LIMIT_BYTES + 100  # header overhead only
    store.close()


async def test_unknown_ref_in_session_is_an_error_naming_the_ref() -> None:
    store = SessionStore(":memory:")
    store.create_session("agent_a", session_id="sess_1")

    result_text, is_error = await _call(store, "sess_1", {"ref": "no_such_call"})

    assert is_error is True
    assert "no_such_call" in result_text
    store.close()


async def test_ref_from_another_session_is_unknown_here() -> None:
    store = SessionStore(":memory:")
    store.create_session("agent_a", session_id="sess_1")
    store.create_session("agent_a", session_id="sess_2")
    store.save_spill("sess_2", "call_1", "secret content")

    result_text, is_error = await _call(store, "sess_1", {"ref": "call_1"})

    assert is_error is True
    assert "call_1" in result_text
    store.close()


async def test_invalid_regex_is_an_error_result() -> None:
    text = "hello world"
    store = _store_with_spill("sess_1", "call_1", text)

    result_text, is_error = await _call(store, "sess_1", {"ref": "call_1", "pattern": "("})

    assert is_error is True
    assert "invalid pattern" in result_text
    store.close()


async def test_search_offsets_bridge_to_paging() -> None:
    # Build content where a distinctive marker sits well past a UTF-8
    # multibyte prefix, so byte offsets and character offsets diverge.
    prefix = "文" * 50  # 150 bytes
    marker = "NEEDLE"
    text = prefix + ("filler " * 200) + marker + ("filler " * 200)
    store = _store_with_spill("sess_1", "call_1", text)

    search_text, is_error = await _call(store, "sess_1", {"ref": "call_1", "pattern": "NEEDLE"})
    assert is_error is False
    assert "matches=1" in search_text

    offset_line = next(line for line in search_text.splitlines() if line.startswith("-- offset="))
    offset = int(offset_line.split("=")[1].split(" ")[0])

    # Bridging: page starting exactly at the reported offset must begin with
    # the matched text itself.
    page_text, is_error = await _call(
        store, "sess_1", {"ref": "call_1", "offsetBytes": offset, "limitBytes": len(marker)}
    )
    assert is_error is False
    body = page_text.split("\n", 1)[1]
    assert body == marker
    store.close()


async def test_search_caps_match_count_and_reports_total() -> None:
    text = "hit " * 30  # 30 occurrences of "hit"
    store = _store_with_spill("sess_1", "call_1", text)

    result_text, is_error = await _call(store, "sess_1", {"ref": "call_1", "pattern": "hit"})

    assert is_error is False
    assert "matches=30" in result_text
    assert "shown=20" in result_text
    store.close()
