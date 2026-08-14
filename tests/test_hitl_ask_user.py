"""End-to-end: the model calls ask_user, a human answers, the run resumes."""

from __future__ import annotations

from pathlib import Path

from knot.core.harness import AgentHarness, AgentHarnessConfig
from knot.core.hitl.ask_user import ASK_USER_TOOL_NAME, build_ask_user_tool
from knot.core.hitl.resume import AnswerResponse, resolve_inputs
from knot.core.session import PersistenceSubscriber, SessionStore, harness_from_session
from knot.providers.fake import FakeProvider, reply
from knot.providers.messages import ToolCall


async def test_ask_user_parks_as_a_question_and_answer_resumes_the_run(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session = store.create_session("agent_a")

    ask_user_tool = build_ask_user_tool()
    assert ask_user_tool.name == ASK_USER_TOOL_NAME
    assert ask_user_tool.execute_fn is None

    call = ToolCall(
        id="call_1", name=ASK_USER_TOOL_NAME, arguments={"question": "What's the target city?"}
    )
    provider = FakeProvider([reply(tool_calls=[call])])
    config = AgentHarnessConfig(
        provider=provider,
        model="m",
        system="s",
        session_id=session.session_id,
        tools=[ask_user_tool],
    )
    harness = AgentHarness(config)
    subscriber = PersistenceSubscriber(store, session.session_id)
    harness.subscribe(subscriber)

    events = [event async for event in harness.prompt("book a flight")]
    subscriber.release()

    end = events[-1]
    assert end.outcome == "waiting_input"
    assert len(end.pending_requests) == 1
    request = end.pending_requests[0]
    assert request.kind == "question"
    assert request.tool_name == ASK_USER_TOOL_NAME

    outcome = await resolve_inputs(
        store,
        session.session_id,
        {request.id: AnswerResponse(resolved_by="human_user", text="New York")},
        tools={ASK_USER_TOOL_NAME: ask_user_tool},
    )
    assert outcome.resolved == [request.id]
    assert outcome.ready_to_continue is True

    entries = store.entries(session.session_id)
    assert not any(e.type == "execution_started" for e in entries)  # nothing ever executes

    result_entry = next(
        e for e in entries if e.type == "message" and e.payload.get("toolCallId") == "call_1"
    )
    assert result_entry.payload["isError"] is False
    assert result_entry.payload["content"][0]["text"] == "New York"

    provider2 = FakeProvider([reply("Booking a flight to New York.")])
    config2 = AgentHarnessConfig(
        provider=provider2,
        model="m",
        system="s",
        session_id=session.session_id,
        tools=[ask_user_tool],
    )
    harness2 = harness_from_session(store, session.session_id, config2)
    assert harness2.state == "idle"
    subscriber2 = PersistenceSubscriber(store, session.session_id)
    harness2.subscribe(subscriber2)

    events2 = [event async for event in harness2.continue_()]
    subscriber2.release()
    assert events2[-1].outcome == "completed"
    assert harness2.messages[-1].text == "Booking a flight to New York."
    store.close()


async def test_default_question_ttl_seconds_flows_into_the_parked_request(tmp_path: Path) -> None:
    """A question park's ttl comes from AgentHarnessConfig's
    ``default_question_ttl_seconds``, plumbed through run_agent_loop."""
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session = store.create_session("agent_a")

    ask_user_tool = build_ask_user_tool()
    call = ToolCall(id="call_1", name=ASK_USER_TOOL_NAME, arguments={"question": "Which city?"})
    provider = FakeProvider([reply(tool_calls=[call])])
    config = AgentHarnessConfig(
        provider=provider,
        model="m",
        system="s",
        session_id=session.session_id,
        tools=[ask_user_tool],
        default_question_ttl_seconds=3600,
    )
    harness = AgentHarness(config)
    subscriber = PersistenceSubscriber(store, session.session_id)
    harness.subscribe(subscriber)

    events = [event async for event in harness.prompt("book a flight")]
    subscriber.release()

    request = events[-1].pending_requests[0]
    assert request.kind == "question"
    assert request.ttl_seconds == 3600
    store.close()
