"""State derivation and rehydration, including a simulated process restart."""

from __future__ import annotations

from knot.core.decisions import RequireApproval
from knot.core.events import PendingInputRequest
from knot.core.harness import AgentHarnessConfig
from knot.core.session import (
    ENTRY_TYPE_COMPACTION,
    ENTRY_TYPE_MESSAGE,
    Compaction,
    InputResolution,
    PersistenceSubscriber,
    SessionStore,
    derive_state,
    harness_from_session,
    rehydrate,
)
from knot.providers.fake import FakeProvider, reply
from knot.providers.messages import AssistantMessage, ToolCall, UserMessage


def test_derive_state_empty_entries_is_idle() -> None:
    state = derive_state([])
    assert state.status == "idle"
    assert state.pending_requests == ()
    assert state.messages == ()


def test_derive_state_park_is_waiting_with_the_right_requests() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        store.append_entry(
            session.session_id,
            ENTRY_TYPE_MESSAGE,
            UserMessage(content="hi").model_dump(by_alias=True),
        )
        request = PendingInputRequest(
            id="req_1", kind="question", tool_call_id="call_1", tool_name="ask"
        )
        store.append_entry(session.session_id, "input_requested", request.model_dump(by_alias=True))

        state = rehydrate(store, session.session_id)
        assert state.status == "waiting"
        assert state.pending_requests == (request,)
        assert len(state.messages) == 1


def test_derive_state_park_plus_resolution_is_idle() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        request = PendingInputRequest(
            id="req_1", kind="question", tool_call_id="call_1", tool_name="ask"
        )
        store.append_entry(session.session_id, "input_requested", request.model_dump(by_alias=True))
        resolution = InputResolution(request_id="req_1", decision="approved", resolved_by="human_1")
        store.append_entry(
            session.session_id, "input_resolved", resolution.model_dump(by_alias=True)
        )

        state = rehydrate(store, session.session_id)
        assert state.status == "idle"
        assert state.pending_requests == ()


def test_derive_state_ignores_resolution_for_a_different_request() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        request = PendingInputRequest(
            id="req_1", kind="question", tool_call_id="call_1", tool_name="ask"
        )
        store.append_entry(session.session_id, "input_requested", request.model_dump(by_alias=True))
        resolution = InputResolution(
            request_id="req_other", decision="approved", resolved_by="human_1"
        )
        store.append_entry(
            session.session_id, "input_resolved", resolution.model_dump(by_alias=True)
        )

        state = rehydrate(store, session.session_id)
        assert state.status == "waiting"
        assert state.pending_requests == (request,)


async def test_rehydrate_after_park_equals_pre_restart_state_across_processes(tmp_path) -> None:
    """Write with one store instance, then read with a fresh instance on the
    same file, simulating a process restart between park and resume.
    """
    db_path = tmp_path / "sessions.db"

    async def hook(call: ToolCall, tool):
        return RequireApproval()

    from knot.core.tools import AgentTool, AgentToolResult

    async def echo(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content="ok")

    tools = [AgentTool(name="sensitive_op", description="", parameters={}, execute_fn=echo)]
    calls = [ToolCall(id="call_sensitive", name="sensitive_op", arguments={})]
    provider = FakeProvider([reply(tool_calls=calls)])

    writer_store = SessionStore(db_path)
    session = writer_store.create_session("agent_a")

    from knot.core.harness import AgentHarness

    config = AgentHarnessConfig(
        provider=provider,
        model="m",
        system="s",
        session_id=session.session_id,
        tools=tools,
        tool_decision_hook=hook,
    )
    harness = AgentHarness(config)
    subscriber = PersistenceSubscriber(writer_store, session.session_id)
    harness.subscribe(subscriber)

    [event async for event in harness.prompt("go")]
    pre_restart_state = (harness.state, harness.pending_requests)
    subscriber.release()
    writer_store.close()

    # Simulate a process restart: fresh store, fresh connection, same file.
    reader_store = SessionStore(db_path)
    rehydrated = rehydrate(reader_store, session.session_id)

    assert pre_restart_state[0] == "waiting"
    assert rehydrated.status == "waiting"
    assert tuple(rehydrated.pending_requests) == pre_restart_state[1]
    reader_store.close()


async def test_harness_from_session_seeds_waiting_state() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        request = PendingInputRequest(
            id="req_1", kind="question", tool_call_id="call_1", tool_name="ask"
        )
        store.append_entry(session.session_id, "input_requested", request.model_dump(by_alias=True))

        config = AgentHarnessConfig(provider=FakeProvider([]), model="m", system="s")
        harness = harness_from_session(store, session.session_id, config)

        assert harness.state == "waiting"
        assert harness.pending_requests == (request,)


async def test_harness_from_session_seeds_idle_state_with_no_entries() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        config = AgentHarnessConfig(provider=FakeProvider([]), model="m", system="s")
        harness = harness_from_session(store, session.session_id, config)

        assert harness.state == "idle"
        assert harness.messages == ()


# -- compaction folding -------------------------------------------------


def _append_message(store: SessionStore, session_id: str, message) -> None:
    store.append_entry(session_id, ENTRY_TYPE_MESSAGE, message.model_dump(by_alias=True))


def test_derive_state_folds_compaction_into_summary_plus_tail() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        sid = session.session_id
        _append_message(store, sid, UserMessage(content="one"))
        _append_message(store, sid, AssistantMessage(content="two", stop_reason="stop"))
        covered_seq = store.entries(sid)[-1].seq
        _append_message(store, sid, UserMessage(content="three (tail)"))

        summary = UserMessage(content="<compacted-summary>one, two</compacted-summary>")
        store.append_entry(
            sid,
            ENTRY_TYPE_COMPACTION,
            Compaction(covers_through_seq=covered_seq, summary_message=summary).model_dump(
                by_alias=True
            ),
        )

        state = derive_state(store.entries(sid))

        assert len(state.messages) == 2
        assert state.messages[0] == summary
        # Content comparison, not model equality: a freshly constructed
        # UserMessage gets a new millisecond timestamp and would only
        # match the rehydrated one by luck.
        assert isinstance(state.messages[1], UserMessage)
        assert state.messages[1].text == "three (tail)"


def test_derive_state_repeated_compactions_compose() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        sid = session.session_id
        _append_message(store, sid, UserMessage(content="one"))
        first_covered_seq = store.entries(sid)[-1].seq
        _append_message(store, sid, UserMessage(content="two (kept after first compaction)"))

        first_summary = UserMessage(content="<compacted-summary>one</compacted-summary>")
        first_compaction_entry = store.append_entry(
            sid,
            ENTRY_TYPE_COMPACTION,
            Compaction(
                covers_through_seq=first_covered_seq, summary_message=first_summary
            ).model_dump(by_alias=True),
        )

        _append_message(store, sid, UserMessage(content="three (tail)"))

        # A second compaction covers everything through the first
        # compaction's own entry seq, meaning it also covers the first
        # summary — composition should just drop it, no special-casing.
        second_summary = UserMessage(
            content="<compacted-summary>one, two (kept after first compaction)</compacted-summary>"
        )
        store.append_entry(
            sid,
            ENTRY_TYPE_COMPACTION,
            Compaction(
                covers_through_seq=first_compaction_entry.seq, summary_message=second_summary
            ).model_dump(by_alias=True),
        )

        state = derive_state(store.entries(sid))

        assert len(state.messages) == 2
        assert state.messages[0] == second_summary
        # Content comparison, not model equality: a freshly constructed
        # UserMessage gets a new millisecond timestamp and would only
        # match the rehydrated one by luck.
        assert isinstance(state.messages[1], UserMessage)
        assert state.messages[1].text == "three (tail)"


def test_derive_state_compaction_interleaved_with_park_leaves_pending_derivation_untouched() -> (
    None
):
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        sid = session.session_id
        _append_message(store, sid, UserMessage(content="one"))
        covered_seq = store.entries(sid)[-1].seq

        request = PendingInputRequest(
            id="req_1", kind="question", tool_call_id="call_1", tool_name="ask"
        )
        store.append_entry(sid, "input_requested", request.model_dump(by_alias=True))

        summary = UserMessage(content="<compacted-summary>one</compacted-summary>")
        store.append_entry(
            sid,
            ENTRY_TYPE_COMPACTION,
            Compaction(covers_through_seq=covered_seq, summary_message=summary).model_dump(
                by_alias=True
            ),
        )

        state = derive_state(store.entries(sid))

        assert state.status == "waiting"
        assert state.pending_requests == (request,)
        assert state.messages == (summary,)


def test_derive_state_ignores_unknown_entry_types_alongside_compaction() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        sid = session.session_id
        _append_message(store, sid, UserMessage(content="one"))
        store.append_entry(sid, "some_future_entry_type", {"anything": "goes"})

        state = derive_state(store.entries(sid))

        # Compare content, not full model equality: a freshly constructed
        # UserMessage gets a new millisecond timestamp and would only match
        # the rehydrated one by luck.
        assert len(state.messages) == 1
        assert isinstance(state.messages[0], UserMessage)
        assert state.messages[0].text == "one"
        assert state.status == "idle"


# -- Compaction entry round-trip -----------------------------------------


def test_compaction_payload_round_trips_with_user_message_summary() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        sid = session.session_id
        summary = UserMessage(content="<compacted-summary>recap</compacted-summary>")
        compaction = Compaction(covers_through_seq=3, summary_message=summary)

        entry = store.append_entry(sid, ENTRY_TYPE_COMPACTION, compaction.model_dump(by_alias=True))

        from knot.core.session.entries import entry_to_compaction

        rehydrated = entry_to_compaction(entry)
        assert rehydrated.covers_through_seq == 3
        assert rehydrated.summary_message == summary


def test_compaction_payload_round_trips_with_assistant_message_summary() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        sid = session.session_id
        summary = AssistantMessage(content="recap", stop_reason="stop")
        compaction = Compaction(covers_through_seq=1, summary_message=summary)

        entry = store.append_entry(sid, ENTRY_TYPE_COMPACTION, compaction.model_dump(by_alias=True))

        from knot.core.session.entries import entry_to_compaction

        rehydrated = entry_to_compaction(entry)
        assert rehydrated.summary_message == summary
