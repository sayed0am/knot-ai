"""``AgentRuntime`` scenario tests: delegation execution, caps, cancellation.

Companion to ``tests/test_delegation_chain.py``, which covers the full
park/restart/resume cascade; this file covers the mechanics of a live
``AgentRuntime`` run: synchronous delegation, cap enforcement, concurrency
bounding, cancellation cascade, child failure, and context isolation.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from authoring_fixtures import write_files

from knot.authoring.compile import compile_fleet
from knot.authoring.runtime import AgentRuntime
from knot.core.events import AgentEndEvent, SubagentCalledEvent, SubagentCompletedEvent
from knot.core.repeat_guard import ADVISORY_TAG_OPEN
from knot.core.session.persistence import PersistenceSubscriber
from knot.core.session.queries import child_chain
from knot.core.session.state import derive_state
from knot.core.tools import AgentTool, AgentToolResult
from knot.providers.fake import FakeProvider, error, reply, tool_call
from knot.providers.messages import ToolCall, ToolResultMessage, UserMessage


def _single_subagent_fleet(tmp_path: Path, *, limits: str = "") -> None:
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/agent.yaml": limits,
            "agents/root/subagents/researcher/instructions.md": "you research\n",
            "agents/root/subagents/researcher/agent.yaml": "description: Digs up facts.\n",
        },
    )


async def test_synchronous_delegation_completes_within_one_turn(tmp_path: Path) -> None:
    _single_subagent_fleet(tmp_path)
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    provider = FakeProvider(
        [
            reply(tool_calls=[ToolCall(id="c1", name="researcher", arguments={"message": "x"})]),
            reply("the answer is 42"),
            reply("root says: 42"),
        ]
    )
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    events = [event async for event in runtime.run_turn(session.session_id, "research x")]
    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"
    assert end.messages[-1].text == "root says: 42"

    children = child_chain(store, session.session_id)
    assert len(children) == 1
    assert children[0].session.agent_id == "researcher"
    store.close()


async def test_delegation_emits_subagent_called_and_completed_control_events(
    tmp_path: Path,
) -> None:
    """A delegation call's child session announces itself on the parent's own
    event stream: ``SubagentCalledEvent`` right after the child session is
    created, ``SubagentCompletedEvent`` (outcome ``"completed"``) once the
    child's run finishes — both control-plane events, not durable facts."""
    _single_subagent_fleet(tmp_path)
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    provider = FakeProvider(
        [
            reply(tool_calls=[ToolCall(id="c1", name="researcher", arguments={"message": "x"})]),
            reply("the answer is 42"),
            reply("root says: 42"),
        ]
    )
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    events = [event async for event in runtime.run_turn(session.session_id, "research x")]

    called = [e for e in events if isinstance(e, SubagentCalledEvent)]
    completed = [e for e in events if isinstance(e, SubagentCompletedEvent)]
    assert len(called) == 1
    assert len(completed) == 1
    assert called[0].tool_call_id == "c1"
    assert called[0].subagent_id == "researcher"
    assert completed[0].tool_call_id == "c1"
    assert completed[0].subagent_id == "researcher"
    assert completed[0].outcome == "completed"
    assert called[0].child_session_id == completed[0].child_session_id

    children = child_chain(store, session.session_id)
    assert children[0].session.session_id == called[0].child_session_id

    # Ordering: announced before completed, both between the assistant's
    # tool call and the eventual root completion.
    assert events.index(called[0]) < events.index(completed[0])
    store.close()


async def test_delegation_cap_exceeded_produces_error_result_naming_the_cap(
    tmp_path: Path,
) -> None:
    _single_subagent_fleet(tmp_path, limits="limits:\n  delegation_max_per_turn: 1\n")
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    calls = [
        ToolCall(id="c1", name="researcher", arguments={"message": "a"}),
        ToolCall(id="c2", name="researcher", arguments={"message": "b"}),
    ]
    provider = FakeProvider([reply(tool_calls=calls), reply("ok"), reply("done")])
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    events = [event async for event in runtime.run_turn(session.session_id, "go")]
    end = events[-1]
    assert end.outcome == "completed"  # the cap failure is a tool error, not a run failure

    results = {m.tool_call_id: m for m in end.messages if isinstance(m, ToolResultMessage)}
    assert results["c1"].is_error is False
    assert results["c2"].is_error is True
    assert "delegation cap exceeded" in results["c2"].text
    assert "1" in results["c2"].text

    # Only one child session was actually created.
    assert len(child_chain(store, session.session_id)) == 1
    store.close()


async def test_child_failure_produces_error_result_and_parent_turn_continues(
    tmp_path: Path,
) -> None:
    _single_subagent_fleet(tmp_path)
    fleet = compile_fleet(tmp_path)

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    provider = FakeProvider(
        [
            reply(tool_calls=[ToolCall(id="c1", name="researcher", arguments={"message": "x"})]),
            error("child provider exploded"),
            reply("sorry, that failed, moving on"),
        ]
    )
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    events = [event async for event in runtime.run_turn(session.session_id, "go")]
    end = events[-1]
    assert end.outcome == "completed"

    result = next(m for m in end.messages if isinstance(m, ToolResultMessage))
    assert result.is_error is True
    assert "child provider exploded" in result.text
    assert end.messages[-1].text == "sorry, that failed, moving on"
    store.close()


async def test_delegation_result_over_parent_cap_is_spilled_by_parents_own_sink(
    tmp_path: Path,
) -> None:
    """A subagent's final answer is itself a tool result in the parent (the
    delegation call's result) and is bounded by the PARENT's own spill
    policy like any other oversized result — no special case (design D4).
    """
    _single_subagent_fleet(tmp_path)
    fleet = compile_fleet(tmp_path)

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    big_answer = "the answer is 42, " * 50  # well over the 200-byte cap below
    provider = FakeProvider(
        [
            reply(tool_calls=[ToolCall(id="c1", name="researcher", arguments={"message": "x"})]),
            reply(big_answer),
            reply("root says: done"),
        ]
    )
    runtime = AgentRuntime(
        fleet=fleet, store=store, provider=provider, max_result_bytes=200, invariant_mode="strict"
    )
    session = runtime.create_session("root")

    events = [event async for event in runtime.run_turn(session.session_id, "research x")]
    end = events[-1]
    assert end.outcome == "completed"

    # The model-visible result on the parent's own event stream is bounded.
    tool_result = next(m for m in end.messages if isinstance(m, ToolResultMessage))
    assert tool_result.is_error is False
    assert len(tool_result.text.encode("utf-8")) <= 200
    assert tool_result.details["spilled"] is True
    assert tool_result.details["ref"] == "c1"
    assert "full_content" not in tool_result.details

    # The persisted entry for the parent session carries the same
    # preview+ref, not the full answer (bounded-surfaces invariant).
    parent_entries = derive_state(store.entries(session.session_id))
    persisted_result = next(
        m
        for m in parent_entries.messages
        if isinstance(m, ToolResultMessage) and m.tool_call_id == "c1"
    )
    assert persisted_result.details["spilled"] is True
    assert persisted_result.details["ref"] == "c1"
    assert len(persisted_result.text.encode("utf-8")) <= 200

    # The parent session's own spills table holds the full original text —
    # the child's session is a completely separate store row.
    spill = store.read_spill(session.session_id, "c1")
    assert spill is not None
    assert spill[0] == big_answer
    store.close()


async def test_parallel_delegations_to_two_subagents_run_concurrently(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "ROOT_INSTRUCTIONS\n",
            "agents/root/subagents/sub_a/instructions.md": "INSTR_a\n",
            "agents/root/subagents/sub_a/agent.yaml": "description: sub a\n",
            "agents/root/subagents/sub_b/instructions.md": "INSTR_b\n",
            "agents/root/subagents/sub_b/agent.yaml": "description: sub b\n",
        },
    )
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")

    started = {"a": asyncio.Event(), "b": asyncio.Event()}
    release = asyncio.Event()

    class GatedProvider:
        def __init__(self) -> None:
            self.root_scripts = [
                reply(
                    tool_calls=[
                        ToolCall(id="c_a", name="sub_a", arguments={"message": "a"}),
                        ToolCall(id="c_b", name="sub_b", arguments={"message": "b"}),
                    ]
                ),
                reply("both done"),
            ]

        def stream_response(self, *, model, system, messages, tools, signal=None, session_id=None):
            key = "a" if "INSTR_a" in system else "b" if "INSTR_b" in system else None
            if key is not None:

                async def gen():
                    started[key].set()
                    await release.wait()
                    for event in reply(f"done {key}"):
                        yield event

                return gen()

            script = self.root_scripts.pop(0) if self.root_scripts else []

            async def gen2():
                for event in script:
                    yield event

            return gen2()

    provider = GatedProvider()
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    async def drain() -> AgentEndEvent:
        end: AgentEndEvent | None = None
        async for event in runtime.run_turn(session.session_id, "go"):
            if isinstance(event, AgentEndEvent):
                end = event
        assert end is not None
        return end

    task = asyncio.create_task(drain())

    # Both children actually overlap: neither is released until both started.
    await asyncio.wait_for(asyncio.gather(started["a"].wait(), started["b"].wait()), timeout=2)
    release.set()

    end = await asyncio.wait_for(task, timeout=2)
    assert end.outcome == "completed"
    assert end.messages[-1].text == "both done"
    store.close()


async def test_concurrency_semaphore_bounds_overlapping_delegations(tmp_path: Path) -> None:
    files = {
        "agents/root/instructions.md": "ROOT_INSTRUCTIONS\n",
        "agents/root/agent.yaml": "limits:\n  delegation_max_concurrent: 2\n",
    }
    for key in ("a", "b", "c"):
        files[f"agents/root/subagents/sub_{key}/instructions.md"] = f"INSTR_{key}\n"
        files[f"agents/root/subagents/sub_{key}/agent.yaml"] = f"description: sub {key}\n"
    write_files(tmp_path, files)
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    started = {key: asyncio.Event() for key in ("a", "b", "c")}
    release = asyncio.Event()

    class GatedProvider:
        def __init__(self) -> None:
            self.root_scripts = [
                reply(
                    tool_calls=[
                        ToolCall(id=f"c_{k}", name=f"sub_{k}", arguments={"message": k})
                        for k in ("a", "b", "c")
                    ]
                ),
                reply("all three done"),
            ]

        def stream_response(self, *, model, system, messages, tools, signal=None, session_id=None):
            key = next((k for k in started if f"INSTR_{k}" in system), None)
            if key is not None:

                async def gen():
                    started[key].set()
                    await release.wait()
                    for event in reply(f"done {key}"):
                        yield event

                return gen()

            script = self.root_scripts.pop(0) if self.root_scripts else []

            async def gen2():
                for event in script:
                    yield event

            return gen2()

    provider = GatedProvider()
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    async def drain() -> AgentEndEvent:
        end: AgentEndEvent | None = None
        async for event in runtime.run_turn(session.session_id, "go"):
            if isinstance(event, AgentEndEvent):
                end = event
        assert end is not None
        return end

    task = asyncio.create_task(drain())

    async def wait_for_n_started(n: int) -> None:
        while sum(e.is_set() for e in started.values()) < n:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_for_n_started(2), timeout=2)
    # Give the (bounded-out) third call every chance to wrongly start too.
    await asyncio.sleep(0.05)
    assert sum(e.is_set() for e in started.values()) == 2

    release.set()
    end = await asyncio.wait_for(task, timeout=2)
    assert end.outcome == "completed"
    assert sum(e.is_set() for e in started.values()) == 3
    store.close()


async def test_child_context_isolation_sees_only_its_own_system_and_message(
    tmp_path: Path,
) -> None:
    _single_subagent_fleet(tmp_path)
    fleet = compile_fleet(tmp_path)

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    provider = FakeProvider(
        [
            reply(
                tool_calls=[
                    ToolCall(
                        id="c1", name="researcher", arguments={"message": "the packed message"}
                    )
                ]
            ),
            reply("child reply"),
            reply("root reply"),
        ]
    )
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    events = [event async for event in runtime.run_turn(session.session_id, "delegate please")]
    assert events[-1].outcome == "completed"

    assert len(provider.calls) == 3
    root_system = provider.calls[0][1]
    child_system = provider.calls[1][1]
    child_messages = provider.calls[1][2]

    assert root_system != child_system
    assert "you research" in child_system
    assert "you are root" not in child_system

    # The child sees exactly one message: the packed delegation text — no
    # parent transcript, no parent user prompt.
    assert len(child_messages) == 1
    assert isinstance(child_messages[0], UserMessage)
    assert child_messages[0].text == "the packed message"
    store.close()


async def test_cancellation_cascade_records_aborted_boundaries_in_both_sessions(
    tmp_path: Path,
) -> None:
    _single_subagent_fleet(tmp_path)
    fleet = compile_fleet(tmp_path)

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(tool_call_id, arguments, signal=None, on_update=None):
        started.set()
        await release.wait()
        return AgentToolResult(content="unreachable")

    slow_tool = AgentTool(name="slow", description="", parameters={}, execute_fn=slow)
    fleet.agents["root"].subagents["researcher"].tools["slow"] = slow_tool

    provider = FakeProvider(
        [
            reply(tool_calls=[ToolCall(id="c1", name="researcher", arguments={"message": "go"})]),
            reply(tool_calls=[ToolCall(id="c2", name="slow", arguments={})]),
        ]
    )
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    # `run_turn` doesn't hand back the harness, so `build_harness` is used
    # directly here to reach `.cancel()` — the same public entry point the
    # HTTP server layer uses to drive a turn while retaining a cancel handle.
    root_harness = runtime.build_harness(session.session_id)
    subscriber = PersistenceSubscriber(store, session.session_id)
    root_harness.subscribe(subscriber)

    async def drain() -> None:
        async for _event in root_harness.prompt("go"):
            pass

    task = asyncio.create_task(drain())
    await asyncio.wait_for(started.wait(), timeout=2)
    root_harness.cancel()
    await asyncio.wait_for(task, timeout=2)
    subscriber.release()

    root_state = derive_state(store.entries(session.session_id))
    assert root_state.status == "idle"
    root_result = next(m for m in root_state.messages if isinstance(m, ToolResultMessage))
    assert root_result.tool_call_id == "c1"
    assert root_result.is_error is True
    assert "interrupted" in root_result.text.lower()

    children = child_chain(store, session.session_id)
    assert len(children) == 1
    child_state = derive_state(store.entries(children[0].session.session_id))
    assert child_state.status == "idle"
    child_result = next(m for m in child_state.messages if isinstance(m, ToolResultMessage))
    assert child_result.tool_call_id == "c2"
    assert child_result.is_error is True
    assert "interrupted" in child_result.text.lower()
    store.close()


# ---------------------------------------------------------------------------
# invariant hook wiring (assert-model-visible-logged stage 2, task 4.2):
# every harness `_build_harness`/`build_harness` assembles is store-backed,
# so it always carries a `pre_request_hook`; a plain `AgentHarness` built
# directly (no runtime, no store) never does.
# ---------------------------------------------------------------------------


def test_build_harness_installs_the_invariant_hook_for_a_persisted_session(
    tmp_path: Path,
) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "you are root\n"})
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    provider = FakeProvider([reply("hi")])
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    harness = runtime.build_harness(session.session_id)
    assert harness.config.pre_request_hook is not None
    store.close()


def test_build_harness_installs_no_hook_when_invariant_mode_is_off(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "you are root\n"})
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    provider = FakeProvider([reply("hi")])
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="off")
    session = runtime.create_session("root")

    harness = runtime.build_harness(session.session_id)
    assert harness.config.pre_request_hook is None
    store.close()


def test_a_bare_agentharness_built_directly_has_no_invariant_hook() -> None:
    """A harness constructed by hand — no ``AgentRuntime``, no store — is
    exactly the "bare in-memory harness" the invariant spec exempts: nothing
    installs a hook on it unless the caller explicitly passes one."""
    from knot.core.harness import AgentHarness, AgentHarnessConfig

    provider = FakeProvider([reply("hi")])
    config = AgentHarnessConfig(provider=provider, model="default", system="you are root")
    harness = AgentHarness(config)
    assert harness.config.pre_request_hook is None


# ---------------------------------------------------------------------------
# integration regression (assert-model-visible-logged stage 5, task 5.1):
# the unit tests in test_core_invariant.py and test_core_loop.py cover the
# checker and the loop hook in isolation; this proves the wiring end-to-end
# at the runtime level — a real persisted session, corrupted the way a bug
# actually would (an in-memory-only append via ``harness.append_message``,
# never reaching the durable log), caught before the next provider request.
# ---------------------------------------------------------------------------


async def test_runtime_strict_mode_fails_run_before_provider_call_on_memory_only_append(
    tmp_path: Path,
) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "you are root\n"})
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    provider = FakeProvider([reply("first turn done"), reply("should never be sent")])
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    # A normal, correctly-persisted first turn: baseline that the wiring
    # itself doesn't false-positive.
    events = [event async for event in runtime.run_turn(session.session_id, "hi")]
    assert events[-1].outcome == "completed"
    assert len(provider.calls) == 1

    # Corrupt the *next* harness the same way a buggy code path would: an
    # in-memory-only injection that never becomes a durable entry. The
    # harness is rehydrated from the (uncorrupted) store, then we append
    # directly to its in-memory history without going through persistence.
    harness = runtime.build_harness(session.session_id)
    harness.append_message(UserMessage(content="steered in memory, never persisted"))
    subscriber = PersistenceSubscriber(store, session.session_id)
    harness.subscribe(subscriber)
    try:
        end_events = [event async for event in harness.continue_()]
    finally:
        subscriber.release()

    end = end_events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "error"
    assert "invariant" in (end.messages[-1].error_message or "").lower()

    # The divergence was caught *before* the second provider request — the
    # fake provider's call count is still exactly the one from the first,
    # correctly-persisted turn.
    assert len(provider.calls) == 1
    store.close()


async def test_runtime_warn_mode_logs_divergence_and_continues_on_memory_only_append(
    tmp_path: Path, caplog
) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "you are root\n"})
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    provider = FakeProvider([reply("first turn done"), reply("second turn done")])
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="warn")
    session = runtime.create_session("root")

    events = [event async for event in runtime.run_turn(session.session_id, "hi")]
    assert events[-1].outcome == "completed"
    assert len(provider.calls) == 1

    harness = runtime.build_harness(session.session_id)
    harness.append_message(UserMessage(content="steered in memory, never persisted"))
    subscriber = PersistenceSubscriber(store, session.session_id)
    harness.subscribe(subscriber)
    try:
        with caplog.at_level(logging.WARNING, logger="knot.core.invariant"):
            end_events = [event async for event in harness.continue_()]
    finally:
        subscriber.release()

    end = end_events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"
    assert end.messages[-1].text == "second turn done"

    # The run proceeded, so the fake provider did receive the second
    # request -- unlike strict mode, warn never vetoes the call.
    assert len(provider.calls) == 2

    warnings = [r for r in caplog.records if "invariant diverged" in r.getMessage()]
    assert len(warnings) == 1
    assert "memory_extra" in warnings[0].getMessage()
    store.close()


# ---------------------------------------------------------------------------
# Repeat-tool-call guard (task 5.1): a persisted session whose advisory rode
# the steering append+emit path (see knot.core.loop, knot.core.repeat_guard)
# must rehydrate with that advisory in the exact same position the live
# harness saw it -- proving the advisory is ordinary durable history, not
# just a live-run artifact. Chain detection and advisory content are already
# covered at the unit level (tests/test_core_repeat_guard.py) and the loop
# level (tests/test_core_loop.py); this is the persistence/replay proof.
# ---------------------------------------------------------------------------


async def test_repeat_guard_advisory_survives_derive_state_at_the_same_position(
    tmp_path: Path,
) -> None:
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/agent.yaml": "repeat_guard:\n  thresholds: [2]\n",
            "agents/root/tools/search.py": """
                from knot.authoring.tools import tool


                @tool
                def search(q: str) -> str:
                    \"\"\"Search for something.\"\"\"
                    return f"result:{q}"
            """,
        },
    )
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True

    from knot.core.session.store import SessionStore

    store = SessionStore(":memory:")
    provider = FakeProvider(
        [
            tool_call("search", {"q": "cats"}, id="call_1"),
            tool_call("search", {"q": "cats"}, id="call_2"),
            reply("done"),
        ]
    )
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    session = runtime.create_session("root")

    harness = runtime.build_harness(session.session_id)
    subscriber = PersistenceSubscriber(store, session.session_id)
    harness.subscribe(subscriber)
    try:
        events = [event async for event in harness.prompt("find cats twice")]
    finally:
        subscriber.release()

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.outcome == "completed"

    live_messages = harness.messages
    live_advisory_positions = [
        i
        for i, m in enumerate(live_messages)
        if isinstance(m, UserMessage) and ADVISORY_TAG_OPEN in m.text
    ]
    assert len(live_advisory_positions) == 1

    derived = derive_state(store.entries(session.session_id))
    derived_advisory_positions = [
        i
        for i, m in enumerate(derived.messages)
        if isinstance(m, UserMessage) and ADVISORY_TAG_OPEN in m.text
    ]
    assert len(derived_advisory_positions) == 1

    # Same tail shape either way: the live harness's full history and the
    # rehydrated (derive_state) history agree on (role, text) throughout,
    # so the advisory necessarily lands at the same position in both.
    assert [(m.role, m.text) for m in derived.messages] == [(m.role, m.text) for m in live_messages]
    assert derived_advisory_positions == live_advisory_positions
    store.close()
