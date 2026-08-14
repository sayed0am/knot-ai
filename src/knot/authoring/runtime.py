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
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass

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
from knot.core.events import (
    AgentEndEvent,
    AgentEvent,
    PendingInputRequest,
    SubagentCalledEvent,
    SubagentCompletedEvent,
    TurnStartEvent,
)
from knot.core.harness import AgentHarness, AgentHarnessConfig
from knot.core.hitl.policies import build_decision_hook
from knot.core.hitl.resume import is_ready_to_continue, write_child_completion
from knot.core.loop import emit_loop_event
from knot.core.session.persistence import PersistenceSubscriber
from knot.core.session.state import DerivedState, harness_from_session, rehydrate
from knot.core.session.store import SessionRecord, SessionStore
from knot.core.tools import AgentTool, AgentToolResult, ToolParkedError
from knot.providers.messages import AssistantMessage, TextContent, ToolResultMessage
from knot.providers.provider import CancellationToken, ModelProvider

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
            self._wire_connection_tool(
                tool,
                manifest_tools_by_name.get(tool.name),
                session_id=session_id,
                agent_id=compiled.agent_id,
            )
            for tool in compiled.tools.values()
        ]
        tools: list[AgentTool] = [*live_capability_tools, *delegation_tools]

        policies = {tool.name: tool.approval for tool in manifest.tools}
        hook = build_decision_hook(policies, store=self.store, session_id=session_id)

        model_name = manifest.model.name if manifest.model is not None else self.default_model
        max_result_bytes = (
            manifest.limits.max_result_bytes
            if manifest.limits.max_result_bytes is not None
            else self.max_result_bytes
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
        )
        harness = harness_from_session(self.store, session_id, config)
        if delegation_tools:
            harness.subscribe(_make_turn_reset_listener(turn_state))
        return harness

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
        """
        harness = self._build_harness(session_id)
        subscriber = PersistenceSubscriber(self.store, session_id)
        harness.subscribe(subscriber)
        try:
            stream = harness.prompt(user_text) if user_text is not None else harness.continue_()
            async for event in stream:
                yield event
        finally:
            subscriber.release()

    async def resume_ready(self, session_id: str) -> AgentEndEvent:
        """Continue a session that has become ready to (``is_ready_to_continue``).

        Rehydrates its messages and seeds its (now-resolved) pending
        requests exactly as ``harness_from_session`` always does, runs it to
        completion under a fresh ``PersistenceSubscriber``, and returns the
        run's terminal ``AgentEndEvent``.
        """
        if not is_ready_to_continue(self.store, session_id):
            raise ValueError(f"session {session_id!r} is not ready to continue")

        harness = self._build_harness(session_id)
        subscriber = PersistenceSubscriber(self.store, session_id)
        harness.subscribe(subscriber)

        end_event: AgentEndEvent | None = None
        try:
            async for event in harness.continue_():
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
                if r.kind == "child_session"
                and r.tool_call_id == child_record.parent_tool_call_id
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
