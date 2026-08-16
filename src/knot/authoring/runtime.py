"""Runtime assembly: turning a compiled fleet into live, running sessions.

Where ``knot.authoring.compile`` only *lowers* a subagent into a manifest
entry (a name, a description, a fixed schema, ``executable: true``), this
module is where that entry becomes something that actually runs. An
``AgentRuntime`` owns every mechanical concern a live agent needs on top of
the pure ``AgentHarness``/``run_agent_loop`` primitives from ``knot.core``:
assembling a session's system prompt and live tools from its compiled
manifest, building the delegation tool for each subagent, enforcing
delegation caps, and driving the durable park/resume protocol both downward
(a delegation call whose child parks) and upward (a child's completion
unwinding its whole ancestor chain). A later HTTP server package is meant to
be a thin transport shell over this.

Child-park representation
--------------------------
A delegation call that cannot complete synchronously because its child
session itself parked or is still running raises ``ToolParkedError`` with a
``PendingInputRequest(kind="child_session", ...)``; the core loop's tool
phase converts that into the same durable park path as any gated or
execute-less call, so the parent session ends its run ``waiting_input`` and
its ``PersistenceSubscriber`` durably records it exactly like any other
pending request (see ``knot.core.tools.ToolParkedError`` and
``knot.core.loop``). Resolution runs the other way: the child's own
eventual completion is what resolves the parent's park (see
``on_child_complete`` / ``knot.core.hitl.resume.write_child_completion`` —
never a direct human response, which ``resolve_inputs`` always rejects for
a ``child_session`` request).

Restart recovery
-----------------
``resume_chain`` is the entry point that ties the whole thing together: a
caller resolves a leaf session's own pending request (a human approves a
gated call, say) via ``knot.core.hitl.resolve_inputs``, then calls
``resume_chain(leaf_session_id)``. That continues the leaf, resolves its
parent's park, continues the parent if it is now ready, and so on up the
ancestor chain — with nothing held in memory between park and resume, so
this works identically whether the resolution happens moments later in the
same process or after a full restart against the same durable store.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from knot.authoring.compile import CompiledAgent, CompiledFleet
from knot.authoring.connections import default_token_resolver
from knot.authoring.manifest import AgentManifest, ManifestSkill, ManifestTool
from knot.authoring.mcp_client import (
    ConnectionPool,
    ProvidedArgumentResolver,
    TokenResolver,
    build_connection_executor,
    default_pool,
)
from knot.core.compaction import (
    CompactionRejected,
    CompactionSettings,
    context_pressure,
    select_boundary,
    summarize,
    validate_shrink,
)
from knot.core.events import (
    AgentEndEvent,
    AgentEvent,
    CompactionEvent,
    PendingInputRequest,
    SubagentCalledEvent,
    SubagentCompletedEvent,
    TurnStartEvent,
)
from knot.core.harness import AgentHarness, AgentHarnessConfig
from knot.core.hitl.policies import build_decision_hook
from knot.core.hitl.resume import is_ready_to_continue, write_child_completion
from knot.core.invariant import InvariantMode, build_invariant_hook
from knot.core.loop import emit_loop_event
from knot.core.session.entries import ENTRY_TYPE_COMPACTION, Compaction
from knot.core.session.persistence import PersistenceSubscriber
from knot.core.session.state import DerivedState, derive_state, harness_from_session, rehydrate
from knot.core.session.store import SessionRecord, SessionStore
from knot.core.spill_tool import READ_TOOL_OUTPUT_TOOL_NAME, build_read_tool_output_tool
from knot.core.tools import AgentTool, AgentToolResult, ToolParkedError
from knot.core.truncation import SpillSink
from knot.providers.capacity import resolve_context_window
from knot.providers.messages import AgentMessage, AssistantMessage, TextContent, ToolResultMessage
from knot.providers.provider import CancellationToken, ModelProvider

logger = logging.getLogger(__name__)

#: v0 has exactly one principal: every connection is reached as "the app",
#: never as a per-user identity. Parameterized (see ``AgentRuntime.principal``)
#: so a later work package can key the connection pool and token cache by a
#: real per-user principal without changing this module's shape.
DEFAULT_PRINCIPAL = "app"


@dataclass(slots=True)
class _DelegationTurnState:
    """Delegation bookkeeping shared by every subagent tool built for one
    assembled harness: a per-turn call counter (reset on every
    ``TurnStartEvent`` — see ``_make_turn_reset_listener``) and a semaphore
    bounding how many delegation calls may actually be running at once.
    """

    max_per_turn: int
    semaphore: asyncio.Semaphore
    count: int = 0

    @classmethod
    def build(cls, *, max_per_turn: int, max_concurrent: int) -> _DelegationTurnState:
        return cls(max_per_turn=max_per_turn, semaphore=asyncio.Semaphore(max_concurrent))


def _make_turn_reset_listener(turn_state: _DelegationTurnState) -> Callable[[AgentEvent], None]:
    def listener(event: AgentEvent) -> None:
        if isinstance(event, TurnStartEvent):
            turn_state.count = 0

    return listener


def _skill_listing_block(skills: Sequence[ManifestSkill]) -> str:
    """Render a system-prompt block listing a manifest's skills.

    A small adaptation of ``knot.authoring.skills.render_skill_listing`` for
    ``ManifestSkill`` (``.id``) rather than the authoring-time ``Skill``
    (``.skill_id``) it was written for.
    """
    if not skills:
        return ""
    lines = ["Available skills (call load_skill with the id to load full instructions):"]
    for skill in sorted(skills, key=lambda s: s.id):
        lines.append(f"- {skill.id}: {skill.name} - {skill.description}")
    return "\n".join(lines)


def _final_text(harness: AgentHarness) -> str:
    if not harness.messages:
        return ""
    last = harness.messages[-1]
    return last.text if isinstance(last, AssistantMessage) else ""


def _final_error_text(harness: AgentHarness) -> str | None:
    """The failing assistant message's own diagnostic text, if any — the
    provider/loop-level failure reason (``AssistantMessage.error_message``),
    distinct from ``.text`` (a failed turn's content is normally empty).
    """
    if not harness.messages:
        return None
    last = harness.messages[-1]
    return last.error_message if isinstance(last, AssistantMessage) else None


def _child_result_message(
    request: PendingInputRequest, child_state: DerivedState
) -> ToolResultMessage:
    """Map a finished child session's durable end-state into the
    ``ToolResultMessage`` its parent's delegation call should receive."""
    last = child_state.messages[-1] if child_state.messages else None
    if isinstance(last, AssistantMessage) and last.stop_reason in ("error", "aborted"):
        text = last.error_message or (
            "delegation aborted" if last.stop_reason == "aborted" else "subagent run failed"
        )
        return ToolResultMessage(
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            content=[TextContent(text=text)],
            is_error=True,
        )
    text = last.text if isinstance(last, AssistantMessage) else ""
    return ToolResultMessage(
        tool_call_id=request.tool_call_id,
        tool_name=request.tool_name,
        content=[TextContent(text=text)] if text else [],
        is_error=False,
    )


async def _drain_child(child_harness: AgentHarness, message_text: str) -> AgentEndEvent:
    end_event: AgentEndEvent | None = None
    async for event in child_harness.prompt(message_text):
        if isinstance(event, AgentEndEvent):
            end_event = event
    assert end_event is not None  # run_agent_loop always ends with one
    return end_event


# ---------------------------------------------------------------------------
# Context compaction wiring (design D2/D3). The pure engine
# (``knot.core.compaction``) knows nothing about the store or the harness;
# everything below is the policy layer that binds it to a live session —
# resolving settings from the compiled manifest, selecting a boundary over
# the CURRENT durable log, appending the durable "compaction" entry, and
# handing back the events plus the post-compaction message list for the
# caller to apply (a pre-turn hook mutates its ``messages`` list in place; a
# reactive retry has a real ``AgentHarness`` and calls
# ``harness.replace_messages`` instead — see ``_build_pre_turn_hook`` and
# ``_drive_with_reactive_compaction`` below).
# ---------------------------------------------------------------------------


def _compaction_settings(manifest: AgentManifest) -> CompactionSettings:
    cfg = manifest.compaction
    return CompactionSettings(
        enabled=cfg.enabled,
        threshold_ratio=cfg.threshold_ratio,
        retain_budget=cfg.retain_budget,
        summarization_model=cfg.summarization_model,
        max_overflow_retries=cfg.max_overflow_retries,
        summarization_max_tokens=cfg.summarization_max_tokens,
    )


async def _attempt_compaction(
    *,
    store: SessionStore,
    session_id: str,
    provider: ModelProvider,
    model: str,
    system: str,
    tools: Sequence[AgentTool],
    settings: CompactionSettings,
    context_window: int,
    trigger: Literal["proactive", "reactive"],
) -> tuple[list[AgentEvent], list[AgentMessage] | None]:
    """One compaction attempt over ``session_id``'s current durable log.

    Returns ``([], None)`` on anything that means "nothing changed" — no
    boundary worth cutting, or a rejected summary — and the caller must
    leave the conversation surface untouched (spec: "Compaction must
    shrink or abort"). On success, returns the ``CompactionEvent`` to
    surface plus the exact post-compaction message list (``[summary,
    *retained_tail]``) the caller must install as the live history —
    constructed to match ``derive_state``'s own fold of the just-appended
    entry byte for byte, which is what keeps the strict model-visible-
    logged invariant green on the very next request.

    ``trigger="reactive"`` halves ``settings.retain_budget`` for this one
    attempt: a reactive compaction is already responding to a request that
    just overflowed, so it aims for more headroom than the proactive
    threshold's default cushion affords, at the cost of summarizing a
    larger span.
    """
    entries = store.entries(session_id)
    derived = derive_state(entries)
    pairs = list(zip(derived.message_seqs, derived.messages, strict=True))

    retain_ratio = settings.retain_budget if trigger == "proactive" else settings.retain_budget / 2
    retain_tokens = retain_ratio * context_window

    boundary_seq = select_boundary(pairs, retain_tokens)
    if boundary_seq is None:
        logger.warning(
            "compaction (%s) for session %s: no usable boundary; skipping", trigger, session_id
        )
        return [], None

    compacted_span = [message for seq, message in pairs if seq <= boundary_seq]
    retained_tail = [message for seq, message in pairs if seq > boundary_seq]

    try:
        summary = await summarize(
            provider=provider,
            model=settings.summarization_model or model,
            system=system,
            tools=tools,
            messages_to_compact=compacted_span,
            max_tokens=settings.summarization_max_tokens,
            session_id=session_id,
        )
        if not validate_shrink(compacted_span, summary):
            raise CompactionRejected("summary did not shrink the compacted span")
    except CompactionRejected as exc:
        logger.warning(
            "compaction (%s) for session %s rejected: %s", trigger, session_id, exc.reason
        )
        return [], None

    compaction = Compaction(covers_through_seq=boundary_seq, summary_message=summary)
    # Appended directly, not through a PersistenceSubscriber: SessionStore's
    # writer "claim" (see SessionStore.claim_writer) is advisory coordination
    # between subscribers, never enforced by append_entry itself — the same
    # reason knot.core.hitl.resume already appends durable records straight
    # through the store while a session is parked and no subscriber is live.
    store.append_entry(session_id, ENTRY_TYPE_COMPACTION, compaction.model_dump(by_alias=True))

    new_messages: list[AgentMessage] = [summary, *retained_tail]
    event = CompactionEvent(
        covers_through_seq=boundary_seq,
        summary_bytes=len(summary.text.encode("utf-8")),
        trigger=trigger,
    )
    return [event], new_messages


class AgentRuntime:
    """Turns a compiled fleet into live, runnable sessions and owns delegation.

    Constructed once with the fleet, a durable session store, and a single
    model provider (v0 is single-provider: every session, parent and every
    descendant subagent alike, is served by the same ``provider`` object;
    the model name comes from each agent's own manifest, or ``default_model``
    when it has none). A later HTTP server package is meant to be a thin
    transport shell over the methods below.
    """

    def __init__(
        self,
        *,
        fleet: CompiledFleet,
        store: SessionStore,
        provider: ModelProvider,
        default_model: str = "default",
        max_result_bytes: int | None = None,
        connection_pool: ConnectionPool | None = None,
        token_resolver: TokenResolver = default_token_resolver,
        provided_argument_resolver: ProvidedArgumentResolver | None = None,
        principal: str = DEFAULT_PRINCIPAL,
        invariant_mode: InvariantMode = "warn",
    ) -> None:
        self.fleet = fleet
        self.store = store
        self.provider = provider
        self.default_model = default_model
        self.max_result_bytes = max_result_bytes
        #: Defaults to the module-global pool (see ``knot.authoring.mcp_client``)
        #: so unrelated ``AgentRuntime`` instances in the same process share
        #: connections by default; pass an explicit pool (e.g. one built with
        #: a test ``transport_factory``) to isolate one runtime's connections.
        self.connection_pool = connection_pool if connection_pool is not None else default_pool()
        self.token_resolver = token_resolver
        self.provided_argument_resolver = provided_argument_resolver
        self.principal = principal
        #: Serving default is "warn" (see design.md D3); ``knot serve`` and
        #: test fixtures each pass their own value explicitly rather than
        #: relying on this default — production degrades a false positive
        #: to telemetry, tests ratchet on strict.
        self.invariant_mode = invariant_mode

    # -- session identity --------------------------------------------------

    def create_session(
        self,
        agent_id: str,
        *,
        parent_session_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> SessionRecord:
        """Create a new top-level session for one of the fleet's compiled agents.

        Only for top-level agents: a subagent's session is always created
        internally by its parent's delegation tool (see
        ``_build_delegation_tool``), which resolves the child's compiled
        agent through the parent's own ``CompiledAgent.subagents`` rather
        than this method's fleet-wide lookup.
        """
        compiled = self.fleet.agents.get(agent_id)
        if compiled is None or not compiled.ok:
            raise ValueError(f"unknown or failed-to-compile agent {agent_id!r}")
        return self.store.create_session(
            agent_id, parent_session_id=parent_session_id, parent_tool_call_id=parent_tool_call_id
        )

    # -- resolving a session's compiled agent -------------------------------

    def _resolve_compiled_agent(self, session_id: str) -> CompiledAgent:
        """Find the ``CompiledAgent`` a session belongs to by walking its
        parent chain, never a flat id lookup: two different agents may each
        declare a same-named subagent (a subagent id is only unique within
        its own parent), so identity is always resolved through parentage.
        """
        record = self.store.get_session(session_id)
        if record is None:
            raise ValueError(f"unknown session {session_id!r}")

        if record.parent_session_id is None:
            compiled = self.fleet.agents.get(record.agent_id)
            if compiled is None or not compiled.ok:
                raise ValueError(
                    f"session {session_id!r}: agent {record.agent_id!r} is not a "
                    "compiled top-level agent"
                )
            return compiled

        parent_compiled = self._resolve_compiled_agent(record.parent_session_id)
        child = parent_compiled.subagents.get(record.agent_id)
        if child is None:
            raise ValueError(
                f"session {session_id!r}: agent {record.agent_id!r} is not a subagent "
                f"of {parent_compiled.agent_id!r}"
            )
        return child

    # -- harness assembly ----------------------------------------------------

    def _build_harness(self, session_id: str) -> AgentHarness:
        """Assemble a fully-configured, rehydrated ``AgentHarness`` for a
        session: system prompt (instructions plus a skill listing block, if
        it has skills), live tools (its compiled tools plus a freshly-built
        delegation tool per subagent), and its approval decision hook — all
        built from its own compiled manifest alone, nothing borrowed from a
        parent or child.
        """
        compiled = self._resolve_compiled_agent(session_id)
        manifest = compiled.manifest
        assert manifest is not None  # a resolvable CompiledAgent is always ok=True

        instructions = (self.fleet.root / manifest.instructions_path).read_text(encoding="utf-8")
        skill_block = _skill_listing_block(manifest.skills)
        system = f"{instructions}\n\n{skill_block}" if skill_block else instructions

        turn_state = _DelegationTurnState.build(
            max_per_turn=manifest.limits.delegation_max_per_turn,
            max_concurrent=manifest.limits.delegation_max_concurrent,
        )
        delegation_tools = [
            self._build_delegation_tool(
                parent_session_id=session_id,
                subagent_id=subagent_id,
                parent_manifest=manifest,
                turn_state=turn_state,
            )
            for subagent_id in manifest.subagent_ids
        ]
        manifest_tools_by_name = {t.name: t for t in manifest.tools}
        live_capability_tools = [
            self._wire_read_tool_output(
                self._wire_connection_tool(
                    tool,
                    manifest_tools_by_name.get(tool.name),
                    session_id=session_id,
                    agent_id=compiled.agent_id,
                ),
                session_id=session_id,
            )
            for tool in compiled.tools.values()
        ]
        tools: list[AgentTool] = [*live_capability_tools, *delegation_tools]

        policies = {tool.name: tool.approval for tool in manifest.tools}
        hook = build_decision_hook(policies, store=self.store, session_id=session_id)

        model_name = manifest.model.name if manifest.model is not None else self.default_model
        max_result_bytes = self._max_result_bytes(manifest)

        # Every harness this method builds is store-backed by construction
        # (``harness_from_session`` below), so it always gets the
        # model-visible-logged invariant hook — "off" makes
        # ``build_invariant_hook`` return ``None``, the loop's ordinary
        # no-op path, at no extra cost. A caller that wants a bare,
        # hook-free harness (tests, library use with no store) constructs
        # ``AgentHarness``/``AgentHarnessConfig`` directly and never comes
        # through here.
        invariant_hook = build_invariant_hook(self.store, session_id, self.invariant_mode)

        compaction_settings = _compaction_settings(manifest)
        context_window = resolve_context_window(
            model_name=model_name,
            configured=manifest.model.context_window if manifest.model is not None else None,
        )
        pre_turn_hook = self._build_pre_turn_hook(
            session_id=session_id,
            model=model_name,
            system=system,
            tools=tools,
            settings=compaction_settings,
            context_window=context_window,
        )

        config = AgentHarnessConfig(
            provider=self.provider,
            model=model_name,
            system=system,
            tools=tools,
            session_id=session_id,
            tool_decision_hook=hook,
            max_turns=manifest.limits.max_turns,
            max_result_bytes=max_result_bytes,
            pre_request_hook=invariant_hook,
            pre_turn_hook=pre_turn_hook,
            spill_sink=self.build_spill_sink(session_id),
        )
        harness = harness_from_session(self.store, session_id, config)
        if delegation_tools:
            harness.subscribe(_make_turn_reset_listener(turn_state))
        return harness

    def _build_pre_turn_hook(
        self,
        *,
        session_id: str,
        model: str,
        system: str,
        tools: list[AgentTool],
        settings: CompactionSettings,
        context_window: int | None,
    ) -> Callable[[list[AgentMessage]], Awaitable[Sequence[AgentEvent]]] | None:
        """Build the proactive-compaction ``pre_turn_hook`` for one session.

        ``None`` when compaction is disabled outright, matching the "cheap
        when disabled" pattern ``build_invariant_hook`` already uses (see
        ``_build_harness``). Deliberately harness-reference-free: it closes
        only over ``self.store``/``self.provider`` and the per-session
        values captured above, so ``run_agent_loop``'s pre-turn contract
        (mutate the ``messages`` list it is given, nothing else) is the only
        thing this needs to satisfy — see ``knot.core.loop.run_agent_loop``.
        """
        if not settings.enabled:
            return None

        async def hook(messages: list[AgentMessage]) -> Sequence[AgentEvent]:
            if context_window is None:
                return ()
            pressure = context_pressure(messages)
            if pressure is None or pressure < settings.threshold_ratio * context_window:
                return ()
            # ``messages`` may already carry this very turn's fresh prompt(s)
            # ahead of the store: the loop's very first pre-turn-hook call
            # site appends prompts to its live list *before* invoking the
            # hook (see ``run_agent_loop``), so on the common
            # ``harness.prompt(...)`` path this hook sees one more message
            # than the durable log currently has. ``_attempt_compaction``
            # only ever reads/covers the durably-persisted span (never "the
            # current run's un-persisted turn" — spec), so anything past
            # that point must be preserved verbatim rather than dropped by
            # a wholesale replacement below.
            persisted_count = len(derive_state(self.store.entries(session_id)).messages)
            events, new_messages = await _attempt_compaction(
                store=self.store,
                session_id=session_id,
                provider=self.provider,
                model=model,
                system=system,
                tools=tools,
                settings=settings,
                context_window=context_window,
                trigger="proactive",
            )
            if new_messages is not None:
                messages[:] = [*new_messages, *messages[persisted_count:]]
            return events

        return hook

    def _compaction_context(self, session_id: str) -> tuple[CompactionSettings, int | None]:
        """Resolve one session's compaction settings and context window,
        for the reactive overflow-retry driver (``run_turn``/
        ``resume_ready``), which needs them but doesn't otherwise assemble
        a harness's full configuration."""
        compiled = self._resolve_compiled_agent(session_id)
        manifest = compiled.manifest
        assert manifest is not None
        model_name = manifest.model.name if manifest.model is not None else self.default_model
        context_window = resolve_context_window(
            model_name=model_name,
            configured=manifest.model.context_window if manifest.model is not None else None,
        )
        return _compaction_settings(manifest), context_window

    async def _drive_with_reactive_compaction(
        self,
        harness: AgentHarness,
        stream: AsyncIterator[AgentEvent],
        settings: CompactionSettings,
        context_window: int | None,
        retries_left: int,
    ) -> AsyncIterator[AgentEvent]:
        """Drain ``stream``, yielding its events; on a context-overflow
        error outcome, compact and retry instead of letting the error
        stand, bounded by ``retries_left`` (design D4/D3's reactive path).

        The terminal ``AgentEndEvent`` is *withheld* from the stream until
        the recovery decision is made: a successful compact-and-retry
        replaces it with a ``CompactionEvent`` followed by the retry run's
        own events, so the consumer never sees an error terminal for a run
        that in fact continued (spec: "the run continues without surfacing
        the overflow error"). Only when recovery does not happen — not an
        overflow, retries exhausted, compaction rejected — is the withheld
        error terminal yielded, standing as the final record (spec:
        "Retries exhausted"). The durable log keeps the error message
        either way; only the stream's shape changes. Shared by
        ``run_turn`` and ``resume_ready`` — both drive a harness the same
        way, so both get reactive recovery.
        """
        held_end: AgentEndEvent | None = None
        async for event in stream:
            if held_end is not None:  # defensive: an end event is always last
                yield held_end
                held_end = None
            if isinstance(event, AgentEndEvent):
                held_end = event
                continue
            yield event

        async def surface_held() -> AsyncIterator[AgentEvent]:
            if held_end is not None:
                yield held_end

        if (
            held_end is None
            or held_end.outcome != "error"
            or retries_left <= 0
            or not settings.enabled
            or context_window is None
        ):
            async for event in surface_held():
                yield event
            return

        last = held_end.messages[-1] if held_end.messages else None
        if not (isinstance(last, AssistantMessage) and last.error_type == "context_overflow"):
            async for event in surface_held():
                yield event
            return

        events, new_messages = await _attempt_compaction(
            store=self.store,
            session_id=harness.config.session_id or "",
            provider=harness.config.provider,
            model=harness.config.model,
            system=harness.config.system,
            tools=harness.config.tools,
            settings=settings,
            context_window=context_window,
            trigger="reactive",
        )
        if new_messages is None:
            # Rejected or nothing to cut: the original error stands.
            async for event in surface_held():
                yield event
            return

        for event in events:
            yield event
        harness.replace_messages(new_messages)

        async for event in self._drive_with_reactive_compaction(
            harness, harness.continue_(), settings, context_window, retries_left - 1
        ):
            yield event

    async def drive_with_reactive_compaction(
        self, harness: AgentHarness, session_id: str, stream: AsyncIterator[AgentEvent]
    ) -> AsyncIterator[AgentEvent]:
        """Public entry point onto ``_drive_with_reactive_compaction`` for a
        caller that must retain its own harness handle (e.g. the HTTP server
        layer, which keeps ``harness`` around for ``harness.cancel()`` and so
        builds it itself via ``build_harness`` rather than going through
        ``run_turn``/``resume_ready`` — see ``build_harness``'s docstring).

        Resolves ``session_id``'s compaction settings/context window the
        same way ``run_turn``/``resume_ready`` do, then wraps ``stream``
        (typically ``harness.prompt(...)`` or ``harness.continue_()``) with
        the same reactive overflow-retry behavior (design D3): without this,
        a caller driving its own harness stream would see the raw
        context-overflow error terminal instead of a recovered run.
        """
        settings, context_window = self._compaction_context(session_id)
        async for event in self._drive_with_reactive_compaction(
            harness, stream, settings, context_window, settings.max_overflow_retries
        ):
            yield event

    def _max_result_bytes(self, manifest: AgentManifest) -> int | None:
        """One agent's effective inline cap: its own manifest limit, falling
        back to this runtime's fleet-wide default. Shared by ``_build_harness``
        and ``max_result_bytes_for`` (the latter for callers outside harness
        assembly, e.g. the HTTP server's resume-with-approval path)."""
        return (
            manifest.limits.max_result_bytes
            if manifest.limits.max_result_bytes is not None
            else self.max_result_bytes
        )

    def max_result_bytes_for(self, session_id: str) -> int | None:
        """The effective inline cap for one session, resolved through its
        compiled agent's manifest (see ``_max_result_bytes``).

        Meant for the resume-with-approval path (``knot.core.hitl.resume.
        resolve_inputs``), which executes a tool outside ``_build_harness``
        and needs the same cap a live turn would use so an approved-then-
        resumed call is bounded identically to one executed mid-run.
        """
        compiled = self._resolve_compiled_agent(session_id)
        assert compiled.manifest is not None
        return self._max_result_bytes(compiled.manifest)

    def build_spill_sink(self, session_id: str) -> SpillSink:
        """Build one session's spill sink: durably stores a spilled tool
        result's full text via the session store and hands back the
        originating tool call id as the retrieval reference.

        Bound to ``session_id`` by closure so every result spilled during
        that session's runs lands in its own row of the store's ``spills``
        table (see ``knot.core.session.store.SessionStore.save_spill``) —
        the exact ref a later ``read_tool_output`` call resolves (see
        ``knot.core.spill_tool``). Public (not ``_build_spill_sink``) for
        the same reason as ``max_result_bytes_for``: the resume-with-approval
        path needs this outside harness assembly, sourcing the same
        ``(store, session_id)`` it already has.
        """
        store = self.store

        def spill_sink(call_id: str, text: str) -> str:
            store.save_spill(session_id, call_id, text)
            return call_id

        return spill_sink

    def _wire_read_tool_output(self, tool: AgentTool, *, session_id: str) -> AgentTool:
        """Give the ``read_tool_output`` placeholder its live executor.

        Every other capability compiles fully live except connection tools
        (see ``_wire_connection_tool``) and this one: it needs
        ``(store, session_id)`` (see ``knot.core.spill_tool``), which only
        exist here at runtime assembly, so ``knot.authoring.compile`` gives
        it an execute-less placeholder and this replaces it, matching the
        exact same two-phase split as a connection tool.
        """
        if tool.name != READ_TOOL_OUTPUT_TOOL_NAME:
            return tool
        return build_read_tool_output_tool(self.store, session_id)

    def _wire_connection_tool(
        self,
        tool: AgentTool,
        manifest_tool: ManifestTool | None,
        *,
        session_id: str,
        agent_id: str,
    ) -> AgentTool:
        """Give one compiled connection tool its live executor.

        Every other capability (authored tools, subagent delegation-lowered
        entries — never live here, see ``CompiledAgent``) already has
        ``execute_fn`` set by compile time, so this is a no-op for them.
        Only a connection tool compiles execute-less (see
        ``knot.authoring.compile._connection_tools``); its manifest entry's
        ``source`` (``connection:<bundle_id>/<connection>``) is the only
        place the bundle/connection identity survives compile, which is why
        this needs the manifest tool alongside the live capability tool.
        """
        if tool.execute_fn is not None or manifest_tool is None:
            return tool
        if not manifest_tool.source.startswith("connection:"):
            return tool

        source_tail = manifest_tool.source.removeprefix("connection:")
        bundle_id, _, connection_name = source_tail.partition("/")
        bundle = self.fleet.bundles.get(bundle_id)
        if bundle is None:
            return tool
        connection_config = bundle.connections.get(connection_name)
        if connection_config is None:
            return tool

        bare_name = tool.name.removeprefix(f"{connection_name}__")
        execute_fn = build_connection_executor(
            connection_name=connection_name,
            bare_tool_name=bare_name,
            config=connection_config,
            pool=self.connection_pool,
            token_resolver=self.token_resolver,
            provided_argument_resolver=self.provided_argument_resolver,
            session_id=session_id,
            agent_id=agent_id,
            principal=self.principal,
        )
        return AgentTool(
            name=tool.name,
            description=tool.description,
            parameters=tool.parameters,
            execute_fn=execute_fn,
            idempotent=tool.idempotent,
            label=tool.label,
        )

    def build_harness(self, session_id: str) -> AgentHarness:
        """Public entry point for assembling one session's fully-wired,
        rehydrated ``AgentHarness`` (see ``_build_harness``).

        Meant for a caller (the HTTP server layer) that needs to drive a
        turn itself — e.g. to retain a handle for ``harness.cancel()`` —
        rather than going through ``run_turn``/``resume_ready``.
        """
        return self._build_harness(session_id)

    def build_live_tools(self, session_id: str) -> dict[str, AgentTool]:
        """The fully-wired tool set a session's harness would run with,
        keyed by name — including live connection executors.

        Meant for the resume-with-approval path: resolve a park via
        ``knot.core.hitl.resume.resolve_inputs(..., tools=runtime.build_live_tools(session_id))``
        so a connection tool's approval, once granted, actually executes
        through the real pool rather than finding an execute-less stand-in.
        """
        return {tool.name: tool for tool in self._build_harness(session_id).config.tools}

    # -- delegation ------------------------------------------------------

    def _build_delegation_tool(
        self,
        *,
        parent_session_id: str,
        subagent_id: str,
        parent_manifest: AgentManifest,
        turn_state: _DelegationTurnState,
    ) -> AgentTool:
        """Build the live, executable delegation tool for one subagent.

        Name, description, and schema come straight from the parent
        manifest's own lowered entry for it (see
        ``knot.authoring.compile``) — one source of truth for what the
        model sees, whether at compile time or run time.
        """
        manifest_tool = next(t for t in parent_manifest.tools if t.name == subagent_id)

        async def execute_fn(
            tool_call_id: str,
            arguments: object,
            signal: CancellationToken | None = None,
            on_update: object = None,
        ) -> AgentToolResult:
            message_text = arguments.get("message") if isinstance(arguments, dict) else None
            if not isinstance(message_text, str):
                raise ValueError(
                    f"delegation to {subagent_id!r} requires a string 'message' argument"
                )
            if turn_state.count >= turn_state.max_per_turn:
                raise ValueError(
                    f"delegation cap exceeded: at most {turn_state.max_per_turn} "
                    f"delegation(s) allowed per turn (subagent {subagent_id!r})"
                )
            turn_state.count += 1

            async with turn_state.semaphore:
                child = self.store.create_session(
                    subagent_id,
                    parent_session_id=parent_session_id,
                    parent_tool_call_id=tool_call_id,
                )
                emit_loop_event(
                    SubagentCalledEvent(
                        tool_call_id=tool_call_id,
                        subagent_id=subagent_id,
                        child_session_id=child.session_id,
                    )
                )
                return await self._run_child(
                    child_session_id=child.session_id,
                    child_agent_id=subagent_id,
                    message_text=message_text,
                    parent_tool_call_id=tool_call_id,
                )

        return AgentTool(
            name=subagent_id,
            description=manifest_tool.description,
            parameters=manifest_tool.input_schema,
            execute_fn=execute_fn,
            idempotent=False,
        )

    async def _run_child(
        self,
        *,
        child_session_id: str,
        child_agent_id: str,
        message_text: str,
        parent_tool_call_id: str,
    ) -> AgentToolResult:
        """Run one child session one-shot, prompted with exactly
        ``message_text``, and map its outcome into the parent's tool result
        (or park the parent, via ``ToolParkedError``, if the child itself
        didn't finish this run).

        Cancellation cascade: the parent's own tool phase hard-cancels this
        method's own task (the delegation call) once the parent's signal is
        set, the same way it would cancel any other tool. ``asyncio.shield``
        keeps the actual child-draining coroutine (``drain_task``) alive
        through that cancellation so it can be told to stop the *right*
        way — calling ``child_harness.cancel()`` — rather than being killed
        mid-frame; that is what lets the child's own ``run_agent_loop``
        notice at its normal cancellation checkpoints and unwind gracefully,
        recording its own aborted boundary through its own
        ``PersistenceSubscriber``, before this method's cancellation is
        allowed to keep propagating up to the parent.
        """
        child_harness = self._build_harness(child_session_id)
        subscriber = PersistenceSubscriber(self.store, child_session_id)
        child_harness.subscribe(subscriber)

        drain_task = asyncio.create_task(_drain_child(child_harness, message_text))
        try:
            end_event = await asyncio.shield(drain_task)
        except asyncio.CancelledError:
            child_harness.cancel()
            with suppress(Exception):
                await drain_task
            subscriber.release()
            raise
        subscriber.release()

        # Announce the child's outcome on the parent's own event stream —
        # any outcome, including a park ("waiting_input"), which is why this
        # sits right after end_event is known rather than after the
        # completed/aborted/error branches below (see SubagentCompletedEvent).
        emit_loop_event(
            SubagentCompletedEvent(
                tool_call_id=parent_tool_call_id,
                subagent_id=child_agent_id,
                child_session_id=child_session_id,
                outcome=end_event.outcome,
            )
        )

        if end_event.outcome == "waiting_input":
            raise ToolParkedError(
                PendingInputRequest(
                    id=f"req_{uuid.uuid4().hex}",
                    kind="child_session",
                    tool_call_id=parent_tool_call_id,
                    tool_name=child_agent_id,
                    payload={"childSessionId": child_session_id},
                )
            )

        await self.on_child_complete(child_session_id)

        if end_event.outcome == "completed":
            return AgentToolResult(content=_final_text(child_harness))
        if end_event.outcome == "aborted":
            raise RuntimeError("delegation aborted")
        raise RuntimeError(
            _final_error_text(child_harness) or f"subagent {child_agent_id!r} failed"
        )

    # -- resume: downward (one session) and upward (a whole chain) ---------

    async def run_turn(
        self, session_id: str, user_text: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Run one turn of ``session_id``: prompt with ``user_text``, or
        (when ``None``) continue an idle/just-resolved session with nothing
        new to say. A fresh ``PersistenceSubscriber`` is attached for the
        run's duration and released the moment it ends, whatever the outcome.

        Driven through ``_drive_with_reactive_compaction`` so a run that
        ends in a context-overflow error is compacted and retried in place
        (design D3's reactive path) rather than surfacing the overflow to
        the caller — see that method's docstring.
        """
        harness = self._build_harness(session_id)
        subscriber = PersistenceSubscriber(self.store, session_id)
        harness.subscribe(subscriber)
        settings, context_window = self._compaction_context(session_id)
        try:
            stream = harness.prompt(user_text) if user_text is not None else harness.continue_()
            async for event in self._drive_with_reactive_compaction(
                harness, stream, settings, context_window, settings.max_overflow_retries
            ):
                yield event
        finally:
            subscriber.release()

    async def resume_ready(self, session_id: str) -> AgentEndEvent:
        """Continue a session that has become ready to (``is_ready_to_continue``).

        Rehydrates its messages and seeds its (now-resolved) pending
        requests exactly as ``harness_from_session`` always does, runs it to
        completion under a fresh ``PersistenceSubscriber``, and returns the
        run's terminal ``AgentEndEvent`` — driven through
        ``_drive_with_reactive_compaction`` the same as ``run_turn`` (see
        its docstring).
        """
        if not is_ready_to_continue(self.store, session_id):
            raise ValueError(f"session {session_id!r} is not ready to continue")

        harness = self._build_harness(session_id)
        subscriber = PersistenceSubscriber(self.store, session_id)
        harness.subscribe(subscriber)
        settings, context_window = self._compaction_context(session_id)

        end_event: AgentEndEvent | None = None
        try:
            async for event in self._drive_with_reactive_compaction(
                harness,
                harness.continue_(),
                settings,
                context_window,
                settings.max_overflow_retries,
            ):
                if isinstance(event, AgentEndEvent):
                    end_event = event
        finally:
            subscriber.release()

        assert end_event is not None
        return end_event

    async def on_child_complete(self, child_session_id: str) -> bool:
        """Resolve the parent's ``child_session`` park, if any, now that
        ``child_session_id`` has reached a durable terminal state.

        Called both from the delegation executor's own normal (synchronous)
        completion path and from ``resume_chain``'s walk back up a chain
        that parked. In the synchronous case there is nothing to do: the
        parent never parked in the first place (the delegation call is
        about to return its result directly), so this is a harmless no-op —
        it looks for a matching pending request, finds none, and returns.
        Returns whether the parent session is now ready to continue.
        """
        child_record = self.store.get_session(child_session_id)
        if child_record is None:
            raise ValueError(f"unknown session {child_session_id!r}")
        parent_session_id = child_record.parent_session_id
        if parent_session_id is None or child_record.parent_tool_call_id is None:
            raise ValueError(f"session {child_session_id!r} has no parent to resolve")

        pending = rehydrate(self.store, parent_session_id).pending_requests
        request = next(
            (
                r
                for r in pending
                if r.kind == "child_session" and r.tool_call_id == child_record.parent_tool_call_id
            ),
            None,
        )
        if request is None:
            return is_ready_to_continue(self.store, parent_session_id)

        child_state = rehydrate(self.store, child_session_id)
        result_message = _child_result_message(request, child_state)
        write_child_completion(
            self.store,
            parent_session_id,
            request,
            resolved_by=f"system:child:{child_session_id}",
            result_message=result_message,
        )
        return is_ready_to_continue(self.store, parent_session_id)

    async def resume_chain(self, leaf_session_id: str) -> list[AgentEndEvent]:
        """THE restart-recovery entry point: resolve a leaf session's own
        pending request via ``knot.core.hitl.resolve_inputs``, then call
        this. Continues the leaf, resolves its parent's park and continues
        the parent if that made it ready, and repeats up the ancestor chain
        until reaching a session that either isn't ready or has no parent.
        Returns every session's terminal ``AgentEndEvent``, leaf first.
        """
        outcomes: list[AgentEndEvent] = []
        session_id: str | None = leaf_session_id

        while session_id is not None and is_ready_to_continue(self.store, session_id):
            end_event = await self.resume_ready(session_id)
            outcomes.append(end_event)

            record = self.store.get_session(session_id)
            assert record is not None
            parent_id = record.parent_session_id
            if parent_id is None or end_event.outcome == "waiting_input":
                break

            ready = await self.on_child_complete(session_id)
            session_id = parent_id if ready else None

        return outcomes


__all__ = ["AgentRuntime"]
