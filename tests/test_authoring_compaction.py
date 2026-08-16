"""``AgentRuntime`` compaction wiring: proactive trigger, reactive retry.

Companion to ``tests/test_core_compaction.py`` (the pure engine) and
``tests/test_session_state.py`` (durable folding); this file covers the
policy layer in ``knot.authoring.runtime`` that binds them to a live,
store-backed session — the proactive between-turns hook installed on
``AgentHarnessConfig.pre_turn_hook``, and the reactive overflow-retry driver
wrapping ``run_turn``.
"""

from __future__ import annotations

from pathlib import Path

from authoring_fixtures import write_files

from knot.authoring.compile import compile_fleet
from knot.authoring.runtime import AgentRuntime
from knot.core.events import AgentEndEvent, CompactionEvent
from knot.core.session.entries import ENTRY_TYPE_COMPACTION
from knot.core.session.state import derive_state
from knot.core.session.store import SessionStore
from knot.providers.fake import FakeProvider, error, reply
from knot.providers.messages import Usage

_LONG_A = "x" * 400
_LONG_B = "y" * 400


def _fleet(tmp_path: Path, agent_yaml: str):
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/agent.yaml": agent_yaml,
        },
    )
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True, fleet.agents["root"].diagnostics
    return fleet


def _runtime(
    fleet, provider, *, invariant_mode: str = "strict"
) -> tuple[AgentRuntime, SessionStore]:
    store = SessionStore(":memory:")
    runtime = AgentRuntime(
        fleet=fleet, store=store, provider=provider, invariant_mode=invariant_mode
    )
    return runtime, store


# ---------------------------------------------------------------------------
# Proactive trigger
# ---------------------------------------------------------------------------


async def test_proactive_compaction_fires_at_threshold_and_stays_invariant_green(
    tmp_path: Path,
) -> None:
    yaml = """
        model:
          provider: anthropic
          name: some-model
          context_window: 1000
        compaction:
          enabled: true
          threshold_ratio: 0.5
          retain_budget: 0.05
    """
    fleet = _fleet(tmp_path, yaml)
    provider = FakeProvider(
        [
            reply(_LONG_B),  # turn 1 response: below threshold, nothing to trigger yet
            reply("ack two", usage=Usage(input=600)),  # turn 2: crosses 0.5 * 1000 = 500
            reply("<summary text>"),  # the summarization call, made on turn 3's pre-turn hook
            reply("ack three"),  # turn 3's real request, built from the compacted history
        ]
    )
    runtime, store = _runtime(fleet, provider)
    session = runtime.create_session("root")

    events1 = [e async for e in runtime.run_turn(session.session_id, _LONG_A)]
    assert events1[-1].outcome == "completed"

    events2 = [e async for e in runtime.run_turn(session.session_id, "two")]
    assert events2[-1].outcome == "completed"
    assert not any(isinstance(e, CompactionEvent) for e in events2)

    events3 = [e async for e in runtime.run_turn(session.session_id, "three")]
    assert events3[-1].outcome == "completed"
    assert events3[-1].messages[-1].text == "ack three"

    compaction_events = [e for e in events3 if isinstance(e, CompactionEvent)]
    assert len(compaction_events) == 1
    assert compaction_events[0].trigger == "proactive"

    entries = store.entries(session.session_id)
    assert any(entry.type == ENTRY_TYPE_COMPACTION for entry in entries)

    # The invariant hook (strict mode) would have failed the run with an
    # error outcome had the post-compaction in-memory surface diverged from
    # what derive_state folds the durable log into — proving D3's handoff.
    derived = derive_state(entries)
    assert derived.messages[0].text.startswith("<compacted-summary>")

    store.close()


async def test_proactive_compaction_does_not_fire_below_threshold(tmp_path: Path) -> None:
    yaml = """
        model:
          provider: anthropic
          name: some-model
          context_window: 1000
        compaction:
          enabled: true
          threshold_ratio: 0.9
          retain_budget: 0.05
    """
    fleet = _fleet(tmp_path, yaml)
    provider = FakeProvider(
        [
            reply(_LONG_B, usage=Usage(input=600)),  # well under 0.9 * 1000 = 900
            reply("ack two"),
        ]
    )
    runtime, store = _runtime(fleet, provider)
    session = runtime.create_session("root")

    events1 = [e async for e in runtime.run_turn(session.session_id, _LONG_A)]
    assert events1[-1].outcome == "completed"

    events2 = [e async for e in runtime.run_turn(session.session_id, "two")]
    assert events2[-1].outcome == "completed"
    assert not any(isinstance(e, CompactionEvent) for e in events2)

    entries = store.entries(session.session_id)
    assert not any(entry.type == ENTRY_TYPE_COMPACTION for entry in entries)
    # Only the two real requests were ever made -- no summarization call.
    assert len(provider.calls) == 2
    store.close()


async def test_unknown_capacity_disables_proactive_compaction(tmp_path: Path) -> None:
    yaml = """
        model:
          provider: anthropic
          name: totally-unmapped-model-name
        compaction:
          enabled: true
          threshold_ratio: 0.01
          retain_budget: 0.005
    """
    fleet = _fleet(tmp_path, yaml)
    provider = FakeProvider(
        [
            reply(_LONG_B, usage=Usage(input=10_000_000)),  # would blow any real threshold
            reply("ack two"),
        ]
    )
    runtime, store = _runtime(fleet, provider)
    session = runtime.create_session("root")

    events1 = [e async for e in runtime.run_turn(session.session_id, _LONG_A)]
    assert events1[-1].outcome == "completed"
    events2 = [e async for e in runtime.run_turn(session.session_id, "two")]
    assert events2[-1].outcome == "completed"
    assert not any(isinstance(e, CompactionEvent) for e in events2)

    entries = store.entries(session.session_id)
    assert not any(entry.type == ENTRY_TYPE_COMPACTION for entry in entries)
    assert len(provider.calls) == 2
    store.close()


# ---------------------------------------------------------------------------
# Reactive trigger
# ---------------------------------------------------------------------------


def _reactive_fleet(tmp_path: Path, *, max_overflow_retries: int) -> object:
    yaml = f"""
        model:
          provider: anthropic
          name: some-model
          context_window: 1000
        compaction:
          enabled: true
          threshold_ratio: 0.99
          retain_budget: 0.1
          max_overflow_retries: {max_overflow_retries}
    """
    return _fleet(tmp_path, yaml)


async def test_reactive_compaction_recovers_from_context_overflow(tmp_path: Path) -> None:
    fleet = _reactive_fleet(tmp_path, max_overflow_retries=1)
    provider = FakeProvider(
        [
            reply(_LONG_B),  # turn 1: ordinary success, builds up history to compact later
            error("context window exceeded", error_type="context_overflow"),  # turn 2 overflows
            reply("<summary>"),  # the reactive summarization call
            reply("ack two"),  # the retried request succeeds
        ]
    )
    runtime, store = _runtime(fleet, provider)
    session = runtime.create_session("root")

    events1 = [e async for e in runtime.run_turn(session.session_id, _LONG_A)]
    assert events1[-1].outcome == "completed"

    events2 = [e async for e in runtime.run_turn(session.session_id, "two")]

    # The overflow error terminal is withheld from the stream: the consumer
    # sees one continuous recovered run, never a mid-stream error end
    # (spec: "the run continues without surfacing the overflow error").
    outcomes = [e.outcome for e in events2 if isinstance(e, AgentEndEvent)]
    assert outcomes == ["completed"]

    compaction_events = [e for e in events2 if isinstance(e, CompactionEvent)]
    assert len(compaction_events) == 1
    assert compaction_events[0].trigger == "reactive"

    final = events2[-1]
    assert isinstance(final, AgentEndEvent)
    assert final.messages[-1].text == "ack two"

    entries = store.entries(session.session_id)
    assert any(entry.type == ENTRY_TYPE_COMPACTION for entry in entries)
    assert len(provider.calls) == 4
    store.close()


async def test_reactive_compaction_honors_max_overflow_retries(tmp_path: Path) -> None:
    fleet = _reactive_fleet(tmp_path, max_overflow_retries=1)
    provider = FakeProvider(
        [
            reply(_LONG_B),  # turn 1: builds history
            error("overflow again", error_type="context_overflow"),  # first overflow
            reply("<summary>"),  # compaction succeeds once
            error("still overflowing", error_type="context_overflow"),  # retry overflows too
        ]
    )
    runtime, store = _runtime(fleet, provider)
    session = runtime.create_session("root")

    events1 = [e async for e in runtime.run_turn(session.session_id, _LONG_A)]
    assert events1[-1].outcome == "completed"

    events2 = [e async for e in runtime.run_turn(session.session_id, "two")]

    outcomes = [e.outcome for e in events2 if isinstance(e, AgentEndEvent)]
    # Exactly one retry was attempted (the cap). The first overflow's error
    # terminal was withheld (its retry proceeded); the second failure has no
    # retries left, so its error terminal surfaces as the run's one final,
    # un-retried outcome.
    assert outcomes == ["error"]

    compaction_events = [e for e in events2 if isinstance(e, CompactionEvent)]
    assert len(compaction_events) == 1  # the cap allowed exactly one compaction attempt

    final = events2[-1]
    assert isinstance(final, AgentEndEvent)
    assert final.outcome == "error"
    assert len(provider.calls) == 4  # no further retry beyond the cap
    store.close()


async def test_reactive_compaction_with_nothing_to_compact_preserves_original_error(
    tmp_path: Path,
) -> None:
    fleet = _reactive_fleet(tmp_path, max_overflow_retries=1)
    provider = FakeProvider(
        [error("overflow on the very first request", error_type="context_overflow")]
    )
    runtime, store = _runtime(fleet, provider)
    session = runtime.create_session("root")

    events = [e async for e in runtime.run_turn(session.session_id, "hi")]

    outcomes = [e for e in events if isinstance(e, AgentEndEvent)]
    assert len(outcomes) == 1
    assert outcomes[0].outcome == "error"
    assert not any(isinstance(e, CompactionEvent) for e in events)

    entries = store.entries(session.session_id)
    assert not any(entry.type == ENTRY_TYPE_COMPACTION for entry in entries)
    store.close()
