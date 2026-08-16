"""End-to-end park/resolve/resume scenarios against a real file-backed store.

Every scenario asserts durable entries (not just in-memory harness state):
this package's whole point is that a park survives a process restart, so
the tests exercise the same store-file-plus-fresh-connection pattern
``tests/test_session_state.py`` uses for crash recovery.
"""

from __future__ import annotations

from pathlib import Path

from knot.core.decisions import RequireApproval
from knot.core.events import AgentEndEvent
from knot.core.harness import AgentHarness, AgentHarnessConfig
from knot.core.hitl.policies import build_decision_hook
from knot.core.hitl.resume import (
    ApproveResponse,
    DenyResponse,
    resolve_inputs,
)
from knot.core.session import PersistenceSubscriber, SessionStore, harness_from_session
from knot.core.session.state import rehydrate
from knot.core.tools import AgentTool, AgentToolResult
from knot.providers.fake import FakeProvider, reply
from knot.providers.messages import ToolCall


def _make_harness(
    store: SessionStore, session_id: str, provider: FakeProvider, **overrides
) -> tuple[AgentHarness, PersistenceSubscriber]:
    config = AgentHarnessConfig(
        provider=provider, model="m", system="s", session_id=session_id, **overrides
    )
    harness = AgentHarness(config)
    subscriber = PersistenceSubscriber(store, session_id)
    harness.subscribe(subscriber)
    return harness, subscriber


def _recording_tool(name: str, *, idempotent: bool = False) -> tuple[AgentTool, list[dict]]:
    """An echo tool that records every call it actually receives."""
    calls: list[dict] = []

    async def execute_fn(tool_call_id, arguments, signal=None, on_update=None):
        calls.append(dict(arguments))
        return AgentToolResult(content=f"executed {name} with {dict(arguments)}")

    tool = AgentTool(
        name=name, description="", parameters={}, execute_fn=execute_fn, idempotent=idempotent
    )
    return tool, calls


def _entries_by_type(store: SessionStore, session_id: str, type_: str) -> list:
    return [e for e in store.entries(session_id) if e.type == type_]


async def test_approve_writes_three_ordered_records_then_continue_completes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session = store.create_session("agent_a")

    tool, tool_calls = _recording_tool("sensitive_op")
    hook = build_decision_hook(
        {"sensitive_op": "always"}, store=store, session_id=session.session_id
    )
    call = ToolCall(id="call_1", name="sensitive_op", arguments={"x": 1})
    provider = FakeProvider([reply(tool_calls=[call])])
    harness, subscriber = _make_harness(
        store, session.session_id, provider, tools=[tool], tool_decision_hook=hook
    )

    events = [event async for event in harness.prompt("go")]
    subscriber.release()

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "waiting_input"
    assert len(end.pending_requests) == 1
    request = end.pending_requests[0]
    assert request.kind == "tool_approval"
    assert tool_calls == []  # not executed yet: still parked

    outcome = await resolve_inputs(
        store,
        session.session_id,
        {request.id: ApproveResponse(resolved_by="tester")},
        tools={"sensitive_op": tool},
    )
    assert outcome.resolved == [request.id]
    assert outcome.rejected == []
    assert outcome.invalidated == []
    assert outcome.expired == []
    assert outcome.ready_to_continue is True
    assert tool_calls == [{"x": 1}]  # executed exactly once, with the real args

    # The three ordered records: input_resolved -> execution_started -> message.
    resolved_entry = _entries_by_type(store, session.session_id, "input_resolved")[0]
    started_entry = _entries_by_type(store, session.session_id, "execution_started")[0]
    result_entry = next(
        e
        for e in _entries_by_type(store, session.session_id, "message")
        if e.payload.get("toolCallId") == "call_1"
    )
    assert resolved_entry.seq < started_entry.seq < result_entry.seq

    provider2 = FakeProvider([reply("all done")])
    config2 = AgentHarnessConfig(
        provider=provider2,
        model="m",
        system="s",
        session_id=session.session_id,
        tools=[tool],
        tool_decision_hook=hook,
    )
    harness2 = harness_from_session(store, session.session_id, config2)
    assert harness2.state == "idle"
    subscriber2 = PersistenceSubscriber(store, session.session_id)
    harness2.subscribe(subscriber2)

    events2 = [event async for event in harness2.continue_()]
    subscriber2.release()
    assert events2[-1].outcome == "completed"
    texts = [m.text for m in harness2.messages]
    assert any("executed sensitive_op" in t for t in texts)
    assert any(t == "all done" for t in texts)
    store.close()


async def test_approved_resume_spills_oversized_result_via_threaded_sink(
    tmp_path: Path,
) -> None:
    """An approved-then-resumed tool call must spill exactly like a live
    one: ``resolve_inputs``'s approve path threads ``spill_sink`` into its
    own ``execute_tool`` call the same way ``max_result_bytes`` already
    flows, so a call that only completes after a park is bounded no
    differently than one that completed synchronously mid-run.
    """
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session = store.create_session("agent_a")

    big_text = "y" * 1000

    async def execute_fn(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content=big_text)

    tool = AgentTool(name="sensitive_op", description="", parameters={}, execute_fn=execute_fn)
    hook = build_decision_hook(
        {"sensitive_op": "always"}, store=store, session_id=session.session_id
    )
    call = ToolCall(id="call_1", name="sensitive_op", arguments={})
    provider = FakeProvider([reply(tool_calls=[call])])
    harness, subscriber = _make_harness(
        store, session.session_id, provider, tools=[tool], tool_decision_hook=hook
    )

    events = [event async for event in harness.prompt("go")]
    subscriber.release()
    request = events[-1].pending_requests[0]

    spilled: dict[str, str] = {}

    def spill_sink(call_id: str, text: str) -> str:
        spilled[call_id] = text
        return call_id

    outcome = await resolve_inputs(
        store,
        session.session_id,
        {request.id: ApproveResponse(resolved_by="tester")},
        tools={"sensitive_op": tool},
        max_result_bytes=200,
        spill_sink=spill_sink,
    )
    assert outcome.resolved == [request.id]

    result_entry = next(
        e
        for e in _entries_by_type(store, session.session_id, "message")
        if e.payload.get("toolCallId") == "call_1"
    )
    details = result_entry.payload["details"]
    assert details["spilled"] is True
    assert details["ref"] == "call_1"
    assert "fullContent" not in details

    content_text = "".join(
        block["text"] for block in result_entry.payload["content"] if block.get("type") == "text"
    )
    assert len(content_text.encode("utf-8")) <= 200

    assert spilled["call_1"] == big_text
    store.close()


async def test_deny_carries_reason_into_next_provider_call(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session = store.create_session("agent_a")

    tool, tool_calls = _recording_tool("sensitive_op")
    hook = build_decision_hook({"sensitive_op": "always"})
    call = ToolCall(id="call_1", name="sensitive_op", arguments={})
    provider = FakeProvider([reply(tool_calls=[call])])
    harness, subscriber = _make_harness(
        store, session.session_id, provider, tools=[tool], tool_decision_hook=hook
    )

    events = [event async for event in harness.prompt("go")]
    subscriber.release()
    request = events[-1].pending_requests[0]

    outcome = await resolve_inputs(
        store,
        session.session_id,
        {request.id: DenyResponse(resolved_by="tester", reason="not authorized")},
        tools={"sensitive_op": tool},
    )
    assert outcome.resolved == [request.id]
    assert outcome.ready_to_continue is True
    assert tool_calls == []  # denied calls never execute

    message_entry = next(
        e
        for e in _entries_by_type(store, session.session_id, "message")
        if e.payload.get("toolCallId") == "call_1"
    )
    assert message_entry.payload["isError"] is True
    assert "User denied: not authorized" in message_entry.payload["content"][0]["text"]

    provider2 = FakeProvider([reply("okay, skipping that")])
    config2 = AgentHarnessConfig(
        provider=provider2, model="m", system="s", session_id=session.session_id, tools=[tool]
    )
    harness2 = harness_from_session(store, session.session_id, config2)
    subscriber2 = PersistenceSubscriber(store, session.session_id)
    harness2.subscribe(subscriber2)
    events2 = [event async for event in harness2.continue_()]
    subscriber2.release()
    assert events2[-1].outcome == "completed"

    # The denial text reached the provider as context for the next turn.
    _, _, context_messages, _ = provider2.calls[0]
    assert any(
        "User denied: not authorized" in m.text for m in context_messages if hasattr(m, "text")
    )
    store.close()


async def test_mixed_parallel_calls_park_only_the_gated_one(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session = store.create_session("agent_a")

    dangerous, dangerous_calls = _recording_tool("dangerous")
    safe1, safe1_calls = _recording_tool("safe1")
    safe2, safe2_calls = _recording_tool("safe2")
    hook = build_decision_hook({"dangerous": "always"}, default="never")

    calls = [
        ToolCall(id="c_dangerous", name="dangerous", arguments={}),
        ToolCall(id="c_safe1", name="safe1", arguments={}),
        ToolCall(id="c_safe2", name="safe2", arguments={}),
    ]
    provider = FakeProvider([reply(tool_calls=calls)])
    harness, subscriber = _make_harness(
        store,
        session.session_id,
        provider,
        tools=[dangerous, safe1, safe2],
        tool_decision_hook=hook,
    )

    events = [event async for event in harness.prompt("go")]
    subscriber.release()
    end = events[-1]
    assert end.outcome == "waiting_input"
    assert len(end.pending_requests) == 1
    assert end.pending_requests[0].tool_name == "dangerous"
    assert safe1_calls == [{}]
    assert safe2_calls == [{}]
    assert dangerous_calls == []

    message_entries = _entries_by_type(store, session.session_id, "message")
    result_ids = {e.payload["toolCallId"] for e in message_entries if "toolCallId" in e.payload}
    assert {"c_safe1", "c_safe2"} <= result_ids
    assert "c_dangerous" not in result_ids

    request = end.pending_requests[0]
    outcome = await resolve_inputs(
        store,
        session.session_id,
        {request.id: ApproveResponse(resolved_by="tester")},
        tools={"dangerous": dangerous, "safe1": safe1, "safe2": safe2},
    )
    assert outcome.resolved == [request.id]
    assert outcome.ready_to_continue is True
    assert dangerous_calls == [{}]

    provider2 = FakeProvider([reply("all three handled")])
    config2 = AgentHarnessConfig(
        provider=provider2, model="m", system="s", session_id=session.session_id
    )
    harness2 = harness_from_session(store, session.session_id, config2)
    subscriber2 = PersistenceSubscriber(store, session.session_id)
    harness2.subscribe(subscriber2)
    events2 = [event async for event in harness2.continue_()]
    subscriber2.release()
    assert events2[-1].outcome == "completed"
    store.close()


async def test_expired_request_is_auto_denied_and_late_response_is_rejected(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session = store.create_session("agent_a")

    tool, tool_calls = _recording_tool("sensitive_op")

    # Real path: the decision hook itself sets the ttl via
    # RequireApproval(ttl_seconds=...), which the loop plumbs straight into
    # the resulting PendingInputRequest.
    async def hook(call, tool_):
        return RequireApproval(ttl_seconds=1)

    call = ToolCall(id="call_1", name="sensitive_op", arguments={})
    provider = FakeProvider([reply(tool_calls=[call])])
    harness, subscriber = _make_harness(
        store, session.session_id, provider, tools=[tool], tool_decision_hook=hook
    )

    events = [event async for event in harness.prompt("go")]
    request = events[-1].pending_requests[0]
    assert request.ttl_seconds == 1
    subscriber.release()

    past_expiry = request.created_at + 5_000  # well past a 1-second ttl

    outcome = await resolve_inputs(
        store,
        session.session_id,
        {request.id: ApproveResponse(resolved_by="tester")},
        tools={"sensitive_op": tool},
        now_ms=past_expiry,
    )
    assert outcome.expired == [request.id]
    assert outcome.rejected == [(request.id, "expired")]
    assert outcome.resolved == []
    assert outcome.ready_to_continue is True
    assert tool_calls == []  # never ran: expiry always wins over a late approval

    resolution_entry = _entries_by_type(store, session.session_id, "input_resolved")[0]
    assert resolution_entry.payload["resolvedBy"] == "system:expiry"
    assert resolution_entry.payload["decision"] == "denied"

    rehydrated = rehydrate(store, session.session_id)
    assert rehydrated.status == "idle"
    store.close()


async def test_revalidation_denies_when_tool_no_longer_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session = store.create_session("agent_a")

    tool, _ = _recording_tool("retiring_tool")
    hook = build_decision_hook({"retiring_tool": "always"})
    call = ToolCall(id="call_1", name="retiring_tool", arguments={})
    provider = FakeProvider([reply(tool_calls=[call])])
    harness, subscriber = _make_harness(
        store, session.session_id, provider, tools=[tool], tool_decision_hook=hook
    )

    events = [event async for event in harness.prompt("go")]
    subscriber.release()
    request = events[-1].pending_requests[0]

    # The tool is no longer part of the current manifest at resolve time.
    outcome = await resolve_inputs(
        store,
        session.session_id,
        {request.id: ApproveResponse(resolved_by="tester")},
        tools={},
    )
    assert outcome.invalidated == [request.id]
    assert outcome.resolved == []
    assert outcome.ready_to_continue is True

    resolution_entry = _entries_by_type(store, session.session_id, "input_resolved")[0]
    assert resolution_entry.payload["decision"] == "denied"
    assert resolution_entry.payload["resolvedBy"] == "system:revalidation"
    assert "retiring_tool" in resolution_entry.payload["reason"]
    store.close()


async def test_once_policy_survives_a_simulated_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    writer_store = SessionStore(db_path)
    session = writer_store.create_session("agent_a")

    tool, tool_calls = _recording_tool("repeatable")
    hook = build_decision_hook(
        {"repeatable": "once"}, store=writer_store, session_id=session.session_id
    )
    call = ToolCall(id="call_1", name="repeatable", arguments={})
    provider = FakeProvider([reply(tool_calls=[call])])
    harness, subscriber = _make_harness(
        writer_store, session.session_id, provider, tools=[tool], tool_decision_hook=hook
    )

    events = [event async for event in harness.prompt("go")]
    request = events[-1].pending_requests[0]
    await resolve_inputs(
        writer_store,
        session.session_id,
        {request.id: ApproveResponse(resolved_by="tester")},
        tools={"repeatable": tool},
    )
    subscriber.release()
    writer_store.close()

    # Simulated restart: fresh store, fresh connection, same file, fresh hook.
    reader_store = SessionStore(db_path)
    fresh_hook = build_decision_hook(
        {"repeatable": "once"}, store=reader_store, session_id=session.session_id
    )

    # Hook-level: the second call to the same tool is allowed without a park.
    decision = await fresh_hook(ToolCall(id="call_2", name="repeatable", arguments={}), None)
    from knot.core.decisions import Allow

    assert isinstance(decision, Allow)

    # Full-run confirmation: a second prompt calling the same tool never parks.
    call2 = ToolCall(id="call_2", name="repeatable", arguments={"y": 2})
    provider2 = FakeProvider([reply(tool_calls=[call2]), reply("thanks")])
    config2 = AgentHarnessConfig(
        provider=provider2,
        model="m",
        system="s",
        session_id=session.session_id,
        tools=[tool],
        tool_decision_hook=fresh_hook,
    )
    harness2 = harness_from_session(reader_store, session.session_id, config2)
    subscriber2 = PersistenceSubscriber(reader_store, session.session_id)
    harness2.subscribe(subscriber2)

    events2 = [event async for event in harness2.prompt("again")]
    subscriber2.release()
    assert events2[-1].outcome == "completed"
    assert tool_calls == [{}, {"y": 2}]  # first (via resolve) then second (direct)
    reader_store.close()


async def test_follow_up_while_parked_replays_after_the_resolution(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    session = store.create_session("agent_a")

    tool, _ = _recording_tool("sensitive_op")
    hook = build_decision_hook({"sensitive_op": "always"})
    call = ToolCall(id="call_1", name="sensitive_op", arguments={})
    provider = FakeProvider([reply(tool_calls=[call])])
    harness, subscriber = _make_harness(
        store, session.session_id, provider, tools=[tool], tool_decision_hook=hook
    )

    events = [event async for event in harness.prompt("go")]
    request = events[-1].pending_requests[0]

    # Free text queued while parked: not sent yet, just queued.
    harness.follow_up("also, what about the second thing?")
    subscriber.release()

    await resolve_inputs(
        store,
        session.session_id,
        {request.id: ApproveResponse(resolved_by="tester")},
        tools={"sensitive_op": tool},
    )

    provider2 = FakeProvider([reply("first thing done"), reply("second thing handled too")])
    config2 = AgentHarnessConfig(
        provider=provider2, model="m", system="s", session_id=session.session_id, tools=[tool]
    )
    harness2 = harness_from_session(store, session.session_id, config2)
    harness2.follow_up("also, what about the second thing?")
    subscriber2 = PersistenceSubscriber(store, session.session_id)
    harness2.subscribe(subscriber2)

    events2 = [event async for event in harness2.continue_()]
    subscriber2.release()
    assert events2[-1].outcome == "completed"

    texts = [m.text for m in harness2.messages]
    tool_result_index = next(
        i for i, m in enumerate(harness2.messages) if getattr(m, "role", None) == "toolResult"
    )
    follow_up_index = texts.index("also, what about the second thing?")
    assert follow_up_index > tool_result_index  # replays after the park resolution
    assert texts[-1] == "second thing handled too"
    store.close()


async def test_restart_mid_park_is_the_flagship_resume_path(tmp_path: Path) -> None:
    """Park, drop every live object, reconnect fresh, resolve, resume."""
    db_path = tmp_path / "sessions.db"

    # --- Phase 1: park, in one process. ---
    writer_store = SessionStore(db_path)
    session = writer_store.create_session("agent_a")
    tool, tool_calls = _recording_tool("book_flight")
    hook = build_decision_hook({"book_flight": "always"})
    call = ToolCall(id="call_1", name="book_flight", arguments={"dest": "LGA"})
    provider = FakeProvider([reply(tool_calls=[call])])
    harness, subscriber = _make_harness(
        writer_store, session.session_id, provider, tools=[tool], tool_decision_hook=hook
    )
    events = [event async for event in harness.prompt("book me a flight")]
    assert events[-1].outcome == "waiting_input"
    session_id = session.session_id
    subscriber.release()
    writer_store.close()
    # Drop every live object from phase 1.
    del harness, subscriber, writer_store, provider, events

    # --- Phase 2: totally fresh process state, same file. ---
    from knot.core.hitl.resume import current_state

    reader_store = SessionStore(db_path)
    state = current_state(reader_store, session_id)
    assert state.status == "waiting"
    assert len(state.pending_requests) == 1
    request = state.pending_requests[0]
    assert request.tool_name == "book_flight"

    outcome = await resolve_inputs(
        reader_store,
        session_id,
        {request.id: ApproveResponse(resolved_by="ops_operator")},
        tools={"book_flight": tool},
    )
    assert outcome.resolved == [request.id]
    assert outcome.ready_to_continue is True
    assert tool_calls == [{"dest": "LGA"}]

    provider2 = FakeProvider([reply("Booked LGA for you.")])
    config2 = AgentHarnessConfig(
        provider=provider2, model="m", system="s", session_id=session_id, tools=[tool]
    )
    harness2 = harness_from_session(reader_store, session_id, config2)
    assert harness2.state == "idle"
    subscriber2 = PersistenceSubscriber(reader_store, session_id)
    harness2.subscribe(subscriber2)

    events2 = [event async for event in harness2.continue_()]
    subscriber2.release()
    assert events2[-1].outcome == "completed"
    assert harness2.messages[-1].text == "Booked LGA for you."
    reader_store.close()
