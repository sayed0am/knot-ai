"""Canonical projection and the model-visible-logged invariant checker."""

from __future__ import annotations

import logging

from knot.core.invariant import (
    HistoryDivergenceError,
    build_invariant_hook,
    canonical_history,
    diff_histories,
)
from knot.core.session.entries import ENTRY_TYPE_MESSAGE, entry_to_message
from knot.core.session.store import SessionStore
from knot.core.tool_history import repair_tool_history
from knot.providers.messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

# -- canonical_history projection -----------------------------------------


def test_details_only_difference_projects_equal() -> None:
    call = ToolCall(id="c1", name="get", arguments={})
    assistant = AssistantMessage(content=[call], stop_reason="toolUse")
    result_a = ToolResultMessage(
        tool_call_id="c1", tool_name="get", content="answer", details={"raw": "a"}
    )
    result_b = ToolResultMessage(
        tool_call_id="c1", tool_name="get", content="answer", details={"raw": "b"}
    )

    history_a = [UserMessage(content="hi"), assistant, result_a]
    history_b = [UserMessage(content="hi"), assistant, result_b]

    assert canonical_history(history_a) == canonical_history(history_b)


def test_content_difference_projects_unequal() -> None:
    call = ToolCall(id="c1", name="get", arguments={})
    assistant = AssistantMessage(content=[call], stop_reason="toolUse")
    result_a = ToolResultMessage(tool_call_id="c1", tool_name="get", content="answer one")
    result_b = ToolResultMessage(tool_call_id="c1", tool_name="get", content="answer two")

    history_a = [UserMessage(content="hi"), assistant, result_a]
    history_b = [UserMessage(content="hi"), assistant, result_b]

    assert canonical_history(history_a) != canonical_history(history_b)


def test_error_type_only_difference_projects_equal() -> None:
    """An error_type-only difference is diagnostic metadata (see
    PROVIDER_INVISIBLE_FIELDS), not model-visible content, and must never be
    reported as invariant divergence."""
    error_a = AssistantMessage(stop_reason="error", error_message="boom", error_type="other")
    error_b = AssistantMessage(
        stop_reason="error", error_message="boom", error_type="context_overflow"
    )

    history_a = [UserMessage(content="hi"), error_a]
    history_b = [UserMessage(content="hi"), error_b]

    assert canonical_history(history_a) == canonical_history(history_b)


def test_role_difference_projects_unequal() -> None:
    history_a = [UserMessage(content="hi")]
    history_b = [AssistantMessage(content="hi", stop_reason="stop")]

    assert canonical_history(history_a) != canonical_history(history_b)


def test_tool_linkage_difference_projects_unequal() -> None:
    call = ToolCall(id="c1", name="get", arguments={})
    assistant = AssistantMessage(content=[call], stop_reason="toolUse")
    result_a = ToolResultMessage(tool_call_id="c1", tool_name="get", content="answer")
    result_b = ToolResultMessage(tool_call_id="c2", tool_name="get", content="answer")

    history_a = [UserMessage(content="hi"), assistant, result_a]
    history_b = [UserMessage(content="hi"), assistant, result_b]

    assert canonical_history(history_a) != canonical_history(history_b)


def test_repaired_interrupted_result_round_trips_equal_through_entry_serialization() -> None:
    """A synthesized interruption result, dumped to an entry payload and read
    back, must canonicalize identically to the in-memory repaired history --
    the exact shape persistence and rehydration exercise.
    """
    call = ToolCall(id="c1", name="get", arguments={})
    assistant = AssistantMessage(content=[call], stop_reason="toolUse")
    in_memory = repair_tool_history([UserMessage(content="hi"), assistant]).messages

    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        entries = [
            store.append_entry(session.session_id, ENTRY_TYPE_MESSAGE, m.model_dump(by_alias=True))
            for m in in_memory
        ]
        rehydrated = tuple(entry_to_message(entry) for entry in entries)

    assert canonical_history(in_memory) == canonical_history(rehydrated)


# -- diff_histories / DivergenceReport -------------------------------------


def test_equal_histories_produce_no_report() -> None:
    history = [UserMessage(content="hi"), AssistantMessage(content="hello", stop_reason="stop")]
    assert diff_histories(history, history) is None


def test_report_pinpoints_in_memory_extra_message() -> None:
    log = [UserMessage(content="hi")]
    memory = [UserMessage(content="hi"), UserMessage(content="steer")]

    report = diff_histories(memory, log)

    assert report is not None
    assert report.kind == "memory_extra"
    assert report.index == 1


def test_report_pinpoints_log_extra_message() -> None:
    log = [UserMessage(content="hi"), UserMessage(content="only in log")]
    memory = [UserMessage(content="hi")]

    report = diff_histories(memory, log)

    assert report is not None
    assert report.kind == "log_extra"
    assert report.index == 1


def test_report_pinpoints_field_mutation_with_field_diff_and_seq() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        first = UserMessage(content="hi")
        store.append_entry(session.session_id, ENTRY_TYPE_MESSAGE, first.model_dump(by_alias=True))
        second_log = AssistantMessage(content="original", stop_reason="stop")
        store.append_entry(
            session.session_id, ENTRY_TYPE_MESSAGE, second_log.model_dump(by_alias=True)
        )
        entries = store.entries(session.session_id)

    log_messages = [first, second_log]
    memory_messages = [first, AssistantMessage(content="mutated", stop_reason="stop")]

    report = diff_histories(memory_messages, log_messages, entries)

    assert report is not None
    assert report.kind == "field_mismatch"
    assert report.index == 1
    assert report.entry_seq == entries[1].seq
    assert "content" in report.field_diff


# -- build_invariant_hook ----------------------------------------------------


async def test_build_invariant_hook_off_returns_none() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        assert build_invariant_hook(store, session.session_id, mode="off") is None


async def test_build_invariant_hook_strict_raises_on_divergence() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        store.append_entry(
            session.session_id,
            ENTRY_TYPE_MESSAGE,
            UserMessage(content="hi").model_dump(by_alias=True),
        )
        hook = build_invariant_hook(store, session.session_id, mode="strict")
        assert hook is not None

        memory_messages = [UserMessage(content="hi"), UserMessage(content="never persisted")]
        try:
            await hook(memory_messages)
        except HistoryDivergenceError as exc:
            assert exc.report.kind == "memory_extra"
        else:
            raise AssertionError("expected HistoryDivergenceError")


async def test_build_invariant_hook_strict_passes_silently_when_equal() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        message = UserMessage(content="hi")
        store.append_entry(
            session.session_id, ENTRY_TYPE_MESSAGE, message.model_dump(by_alias=True)
        )
        hook = build_invariant_hook(store, session.session_id, mode="strict")
        assert hook is not None

        await hook([message])  # no exception


async def test_build_invariant_hook_warn_logs_and_continues(caplog) -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        store.append_entry(
            session.session_id,
            ENTRY_TYPE_MESSAGE,
            UserMessage(content="hi").model_dump(by_alias=True),
        )
        hook = build_invariant_hook(store, session.session_id, mode="warn")
        assert hook is not None

        memory_messages = [UserMessage(content="hi"), UserMessage(content="never persisted")]
        with caplog.at_level(logging.WARNING, logger="knot.core.invariant"):
            await hook(memory_messages)  # does not raise

        assert any("invariant diverged" in record.getMessage() for record in caplog.records)
