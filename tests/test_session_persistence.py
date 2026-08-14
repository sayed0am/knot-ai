"""PersistenceSubscriber writes exactly the durable facts a run produces."""

from __future__ import annotations

import pytest

from knot.core.decisions import RequireApproval
from knot.core.events import AgentEndEvent, MessageEndEvent
from knot.core.harness import AgentHarness, AgentHarnessConfig
from knot.core.session import (
    ENTRY_TYPE_INPUT_REQUESTED,
    ENTRY_TYPE_MESSAGE,
    PersistenceSubscriber,
    SessionStore,
    WriterAlreadyClaimedError,
    entry_to_message,
    entry_to_request,
)
from knot.providers.fake import FakeProvider, reply
from knot.providers.messages import ToolCall


def _make_harness(store: SessionStore, session_id: str, provider: FakeProvider, **overrides):
    config = AgentHarnessConfig(
        provider=provider, model="m", system="s", session_id=session_id, **overrides
    )
    harness = AgentHarness(config)
    subscriber = PersistenceSubscriber(store, session_id)
    harness.subscribe(subscriber)
    return harness, subscriber


async def test_persistence_writes_exactly_the_message_entries_a_run_produces() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        provider = FakeProvider([reply("hello there")])
        harness, subscriber = _make_harness(store, session.session_id, provider)

        events = [event async for event in harness.prompt("hi")]
        subscriber.release()

        expected_messages = [
            e.message for e in events if isinstance(e, MessageEndEvent)
        ]

        entries = store.entries(session.session_id)
        assert [e.type for e in entries] == [ENTRY_TYPE_MESSAGE] * len(expected_messages)
        persisted_messages = [entry_to_message(e) for e in entries]
        assert persisted_messages == expected_messages


async def test_persistence_writes_input_requested_entries_on_parked_run() -> None:
    async def hook(call: ToolCall, tool):
        return RequireApproval()

    from knot.core.tools import AgentTool, AgentToolResult

    async def echo(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content="ok")

    tools = [AgentTool(name="sensitive_op", description="", parameters={}, execute_fn=echo)]
    calls = [ToolCall(id="call_sensitive", name="sensitive_op", arguments={})]
    provider = FakeProvider([reply(tool_calls=calls)])

    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        harness, subscriber = _make_harness(
            store, session.session_id, provider, tools=tools, tool_decision_hook=hook
        )

        events = [event async for event in harness.prompt("go")]
        subscriber.release()

        end = events[-1]
        assert isinstance(end, AgentEndEvent)
        assert end.outcome == "waiting_input"

        entries = store.entries(session.session_id)
        requested_entries = [e for e in entries if e.type == ENTRY_TYPE_INPUT_REQUESTED]
        assert len(requested_entries) == 1
        persisted_request = entry_to_request(requested_entries[0])
        assert persisted_request == end.pending_requests[0]

        # Message entries were also written for the user prompt and the
        # assistant turn (no tool result was synthesized for the parked call).
        message_entries = [e for e in entries if e.type == ENTRY_TYPE_MESSAGE]
        assert len(message_entries) == 2


async def test_persistence_subscriber_is_the_only_writer_for_its_session() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        first = PersistenceSubscriber(store, session.session_id)

        with pytest.raises(WriterAlreadyClaimedError):
            PersistenceSubscriber(store, session.session_id)

        first.release()
        # After release, a fresh subscriber can claim the session again.
        second = PersistenceSubscriber(store, session.session_id)
        second.release()


async def test_persistence_subscriber_as_context_manager() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        provider = FakeProvider([reply("hi")])
        config = AgentHarnessConfig(
            provider=provider, model="m", system="s", session_id=session.session_id
        )
        harness = AgentHarness(config)

        with PersistenceSubscriber(store, session.session_id) as subscriber:
            harness.subscribe(subscriber)
            [event async for event in harness.prompt("hi")]

        entries = store.entries(session.session_id)
        assert len(entries) > 0
