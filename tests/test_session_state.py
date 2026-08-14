"""State derivation and rehydration, including a simulated process restart."""

from __future__ import annotations

from knot.core.decisions import RequireApproval
from knot.core.events import PendingInputRequest
from knot.core.harness import AgentHarnessConfig
from knot.core.session import (
    ENTRY_TYPE_MESSAGE,
    InputResolution,
    PersistenceSubscriber,
    SessionStore,
    derive_state,
    harness_from_session,
    rehydrate,
)
from knot.providers.fake import FakeProvider, reply
from knot.providers.messages import ToolCall, UserMessage


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
        store.append_entry(
            session.session_id, "input_requested", request.model_dump(by_alias=True)
        )

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
        store.append_entry(
            session.session_id, "input_requested", request.model_dump(by_alias=True)
        )
        resolution = InputResolution(
            request_id="req_1", decision="approved", resolved_by="human_1"
        )
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
        store.append_entry(
            session.session_id, "input_requested", request.model_dump(by_alias=True)
        )
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
        store.append_entry(
            session.session_id, "input_requested", request.model_dump(by_alias=True)
        )

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
