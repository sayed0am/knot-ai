"""The knot HTTP API: a single FastAPI app serving every compiled agent in
one fleet.

Layering: this module may import anything (providers, core, authoring); no
lower-layer module may import it back (enforced by
``tests/test_import_direction.py``).

Design in one paragraph: a run's own event grammar (``knot.core.events``) IS
the streaming wire protocol — ``POST /sessions/{id}/messages`` and
``POST /sessions/{id}/continue`` write each ``AgentEvent`` to the response as
one SSE frame, ``event: <type>`` / ``data: <model_dump_json(by_alias=True)>``,
verbatim, no reshaping. A turn is "turn-stitched" streaming: one HTTP stream
per turn, which may end in ``waiting_input``; resuming a parked session is a
*new* stream, started by the client calling ``/continue`` again — the server
never auto-runs an ancestor turn on a child's behalf (see ``_stream_turn``'s
``chain`` terminal event).

In-memory v0 contract (deliberate, documented here once): a durable PARK
(``waiting_input``, written by ``PersistenceSubscriber``) always survives a
process restart. Two things introduced at this layer do NOT: which
sessions currently have an in-flight run (``ServerState.running``) and any
free text a client posted while a session was ``waiting`` but not yet
resumed (``ServerState.follow_ups``, drained into the harness on the next
``/continue``). Both are lost on restart — a restarted server simply sees
every previously-``running`` session as ``waiting`` (or ``idle``, if it had
already finished) again, and any not-yet-drained queued text is gone. This
is acceptable for v0 because the thing that must never be lost — the park
itself, and the full transcript up to it — is durable; only cheaply
replaceable in-flight/UI-adjacent state lives in memory.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, model_validator

from knot.authoring.compile import CompiledFleet, compile_fleet
from knot.authoring.config import load_agent_config
from knot.authoring.connections import check_connection_health
from knot.authoring.discovery import Diagnostic
from knot.authoring.manifest import serialize_manifest
from knot.authoring.mcp_client import TransportFactory
from knot.authoring.runtime import AgentRuntime
from knot.core.events import AgentEndEvent, AgentEvent
from knot.core.harness import AgentHarness
from knot.core.hitl.resume import (
    AnswerResponse,
    ApproveResponse,
    DenyResponse,
    current_state,
    is_ready_to_continue,
    resolve_inputs,
)
from knot.core.hitl.resume import (
    Response as HitlResponse,
)
from knot.core.session.persistence import PersistenceSubscriber
from knot.core.session.queries import pending_requests_fleet
from knot.core.session.state import rehydrate
from knot.core.session.store import SessionStore, WriterAlreadyClaimedError
from knot.providers.messages import current_timestamp_ms
from knot.providers.provider import ModelProvider

__all__ = ["ServerState", "create_app", "create_app_from_paths"]


# ---------------------------------------------------------------------------
# App-owned state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RunningTurn:
    """One in-flight turn: the harness driving it (for ``cancel()``) and the
    background task actually running it (for ``/cancel`` to await)."""

    harness: AgentHarness
    task: asyncio.Task[None]


@dataclass(slots=True)
class ServerState:
    """Everything one running app instance owns, beyond the durable store.

    ``running`` and ``follow_ups`` are keyed by session id and are both
    in-memory only — see the module docstring for why. ``running`` doubles
    as the app's concurrency guard for decision C (``idle``/``waiting``/
    ``running``): a session id present in it has an active background task,
    checked before starting a new one and popped by that task itself, from
    its own ``finally``, the moment it ends (whatever the outcome).
    """

    fleet: CompiledFleet
    store: SessionStore
    runtime: AgentRuntime
    transport_factory: TransportFactory | None = None
    running: dict[str, _RunningTurn] = field(default_factory=dict)
    follow_ups: dict[str, list[str]] = field(default_factory=dict)


def create_app(
    *,
    fleet: CompiledFleet,
    store: SessionStore,
    provider: ModelProvider,
    runtime_kwargs: Mapping[str, object] | None = None,
    transport_factory: TransportFactory | None = None,
) -> FastAPI:
    """Build the knot HTTP API app over one compiled fleet.

    Every dependency is passed in explicitly rather than constructed
    internally, so a test can swap in a ``FakeProvider``, an in-memory
    ``SessionStore``, and a mock MCP ``transport_factory`` (used only by
    ``GET /connections/health``) with nothing ever touching the network.
    ``runtime_kwargs`` reaches ``AgentRuntime`` verbatim (e.g. a
    ``connection_pool`` built with the same test transport).
    """
    runtime = AgentRuntime(
        fleet=fleet, store=store, provider=provider, **dict(runtime_kwargs or {})
    )
    state = ServerState(
        fleet=fleet, store=store, runtime=runtime, transport_factory=transport_factory
    )

    app = FastAPI(title="knot", version="0.1.0")
    app.state.knot = state  # reach back into ServerState (tests, introspection)
    _install_routes(app, state)
    return app


def create_app_from_paths(
    root: Path,
    db_path: Path,
    provider: ModelProvider,
    *,
    runtime_kwargs: Mapping[str, object] | None = None,
    transport_factory: TransportFactory | None = None,
) -> FastAPI:
    """Convenience wrapper: compile ``root`` and open ``db_path`` directly."""
    fleet = compile_fleet(root)
    store = SessionStore(db_path)
    return create_app(
        fleet=fleet,
        store=store,
        provider=provider,
        runtime_kwargs=runtime_kwargs,
        transport_factory=transport_factory,
    )


# ---------------------------------------------------------------------------
# SSE wire formatting — the loop's own event grammar, verbatim.
# ---------------------------------------------------------------------------


def _sse_agent_event(event: AgentEvent) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json(by_alias=True)}\n\n"


def _sse_raw_event(event_type: str, payload: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


def _can_continue(state: ServerState, session_id: str) -> bool:
    """Whether ``/continue`` may resume this session right now.

    ``is_ready_to_continue`` alone answers "no pending requests and no
    dangling tool calls" — trivially true for a session that has never run
    at all (empty transcript), which is not what "ready to continue" means
    here: there is no parked turn to resume. This adds that one extra,
    necessary condition: the session must actually have some history.
    """
    if not rehydrate(state.store, session_id).messages:
        return False
    return is_ready_to_continue(state.store, session_id)


def _effective_state(state: ServerState, session_id: str) -> str:
    """The app-visible session state: ``running`` (an active background
    task) takes priority over the durable-log-derived ``idle``/``waiting``.
    """
    if session_id in state.running:
        return "running"
    return current_state(state.store, session_id).status


# ---------------------------------------------------------------------------
# Turn streaming: decision C (idle -> stream, waiting -> queue, running ->
# 409) and decision B (turn-stitched resume + the "chain" terminal event).
# ---------------------------------------------------------------------------


def _stream_turn(
    state: ServerState,
    session_id: str,
    *,
    user_text: str | None,
    follow_up_texts: Sequence[str] = (),
) -> StreamingResponse:
    """Drive one turn in a background task, forwarding its events onto an
    SSE response read from a queue.

    Durability over the socket: the task is created with ``asyncio.create_task``
    (independent of this request's connection) and registered in
    ``state.running`` before this function returns, so it keeps running to
    completion — and persistence keeps writing — even if the client that
    started it disconnects mid-stream; only ``event_stream`` (the thing
    actually attached to the HTTP response) unwinds early in that case.

    The chain hand-off (decision B): once the run reaches a genuinely
    terminal outcome (not ``waiting_input``) and the session has a parent,
    the parent's ``child_session`` park is resolved (a harmless no-op if it
    wasn't parked on this child at all — see ``AgentRuntime.on_child_complete``)
    and the stream ends with one extra ``event: chain`` frame reporting
    whether the parent became ready. The parent's own turn is never run
    here: chaining up the tree is always the client's next call.
    """
    try:
        harness = state.runtime.build_harness(session_id)
        subscriber = PersistenceSubscriber(state.store, session_id)
    except WriterAlreadyClaimedError as exc:
        raise HTTPException(status_code=409, detail="session is currently running") from exc

    harness.subscribe(subscriber)
    for text in follow_up_texts:
        harness.follow_up(text)

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def runner() -> None:
        last_outcome: str | None = None
        try:
            raw_stream = harness.prompt(user_text) if user_text is not None else harness.continue_()
            # Reactive overflow-retry (design D3) is wired through
            # ``AgentRuntime.run_turn``/``resume_ready`` normally; this layer
            # drives its own harness directly instead (it needs to retain
            # ``harness`` for ``harness.cancel()`` — see ``build_harness``'s
            # docstring), so it opts into the same recovery explicitly here.
            stream = state.runtime.drive_with_reactive_compaction(harness, session_id, raw_stream)
            async for event in stream:
                await queue.put(_sse_agent_event(event))
                if isinstance(event, AgentEndEvent):
                    last_outcome = event.outcome
        finally:
            subscriber.release()
            state.running.pop(session_id, None)
            if last_outcome is not None and last_outcome != "waiting_input":
                record = state.store.get_session(session_id)
                if record is not None and record.parent_session_id is not None:
                    parent_ready = await state.runtime.on_child_complete(session_id)
                    await queue.put(
                        _sse_raw_event(
                            "chain",
                            {
                                "parentSessionId": record.parent_session_id,
                                "parentReady": parent_ready,
                            },
                        )
                    )
            await queue.put(None)  # sentinel: no more events, whether or not anyone is listening

    task = asyncio.create_task(runner())
    state.running[session_id] = _RunningTurn(harness=harness, task=task)

    async def event_stream() -> AsyncIterator[str]:
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class MessageBody(BaseModel):
    text: str


class InputResponseItem(BaseModel):
    action: Literal["approve", "deny", "answer"]
    by: str
    reason: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _check_required_fields(self) -> InputResponseItem:
        if self.action == "deny" and not self.reason:
            raise ValueError("a 'deny' response requires 'reason'")
        if self.action == "answer" and not self.text:
            raise ValueError("an 'answer' response requires 'text'")
        return self

    def to_hitl_response(self) -> HitlResponse:
        if self.action == "approve":
            return ApproveResponse(resolved_by=self.by, reason=self.reason)
        if self.action == "deny":
            assert self.reason is not None  # enforced by _check_required_fields
            return DenyResponse(resolved_by=self.by, reason=self.reason)
        assert self.text is not None  # enforced by _check_required_fields
        return AnswerResponse(resolved_by=self.by, text=self.text)


class InputBody(BaseModel):
    responses: dict[str, InputResponseItem]


# ---------------------------------------------------------------------------
# Small serialization helpers
# ---------------------------------------------------------------------------


def _diagnostic_dict(diagnostic: Diagnostic) -> dict:
    return {
        "severity": diagnostic.severity,
        "path": str(diagnostic.path),
        "message": diagnostic.message,
        "agentId": diagnostic.agent_id,
        "bundleId": diagnostic.bundle_id,
    }


def _agent_description(fleet: CompiledFleet, agent_id: str) -> str | None:
    """A top-level agent's own ``description``, read straight from its
    ``agent.yaml`` — not carried on ``AgentManifest`` itself (that field
    only matters for a *subagent*'s delegation-tool description; see
    ``knot.authoring.compile``), so this is a small, independent read
    rather than a manifest field. Works for a failed-compile agent too, as
    long as its ``agent.yaml`` itself is present and valid.
    """
    config_path = fleet.root / "agents" / agent_id / "agent.yaml"
    if not config_path.is_file():
        return None
    config, _diagnostics = load_agent_config(config_path, agent_id=agent_id)
    return config.description if config is not None else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _install_routes(app: FastAPI, state: ServerState) -> None:  # noqa: C901 - one flat route table
    # -- 9.1 fleet / manifest --------------------------------------------

    @app.get("/agents")
    def list_agents() -> list[dict]:
        return [
            {
                "agentId": agent_id,
                "ok": compiled.ok,
                "description": _agent_description(state.fleet, agent_id),
            }
            for agent_id, compiled in sorted(state.fleet.agents.items())
        ]

    @app.get("/agents/{agent_id}")
    def get_agent(agent_id: str) -> Response:
        compiled = state.fleet.agents.get(agent_id)
        if compiled is None:
            raise HTTPException(status_code=404, detail=f"unknown agent {agent_id!r}")
        if not compiled.ok:
            return JSONResponse(
                status_code=409,
                content={"diagnostics": [_diagnostic_dict(d) for d in compiled.diagnostics]},
            )
        assert compiled.manifest is not None  # ok=True always carries a manifest
        return Response(
            content=serialize_manifest(compiled.manifest), media_type="application/json"
        )

    # -- 9.2 sessions -------------------------------------------------------

    @app.post("/agents/{agent_id}/sessions", status_code=201)
    def create_session(agent_id: str) -> dict:
        compiled = state.fleet.agents.get(agent_id)
        if compiled is None:
            raise HTTPException(status_code=404, detail=f"unknown agent {agent_id!r}")
        if not compiled.ok:
            raise HTTPException(status_code=409, detail=f"agent {agent_id!r} failed to compile")
        record = state.runtime.create_session(agent_id)
        return {
            "sessionId": record.session_id,
            "agentId": record.agent_id,
            "state": "idle",  # a freshly created session has no entries yet
            "createdAt": record.created_at,
        }

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        record = state.store.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")
        derived = current_state(state.store, session_id)  # lazy-expiry read path
        return {
            "sessionId": record.session_id,
            "agentId": record.agent_id,
            "state": _effective_state(state, session_id),
            "pendingRequests": [
                r.model_dump(mode="json", by_alias=True) for r in derived.pending_requests
            ],
            "transcript": [m.model_dump(mode="json", by_alias=True) for m in derived.messages],
            "parentSessionId": record.parent_session_id,
            "parentToolCallId": record.parent_tool_call_id,
        }

    # -- 9.3 turn streaming (decision C) ------------------------------------

    @app.post("/sessions/{session_id}/messages")
    async def post_message(session_id: str, body: MessageBody):
        record = state.store.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")
        if session_id in state.running:
            raise HTTPException(status_code=409, detail="session is currently running")

        derived = current_state(state.store, session_id)
        if derived.status == "waiting":
            # Queued, not sent: the harness only sees this on the next
            # /continue (see _stream_turn's follow_up_texts). In-memory
            # only — see the module docstring's v0 contract.
            state.follow_ups.setdefault(session_id, []).append(body.text)
            return JSONResponse(status_code=202, content={"queued": True})

        return _stream_turn(state, session_id, user_text=body.text)

    @app.post("/sessions/{session_id}/continue")
    async def continue_session(session_id: str):
        record = state.store.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")
        if session_id in state.running:
            raise HTTPException(status_code=409, detail="session is currently running")
        if not _can_continue(state, session_id):
            raise HTTPException(status_code=409, detail="session is not ready to continue")

        follow_up_texts = state.follow_ups.pop(session_id, [])
        return _stream_turn(state, session_id, user_text=None, follow_up_texts=follow_up_texts)

    # -- 9.4 input + cancel ---------------------------------------------

    @app.post("/sessions/{session_id}/input")
    async def post_input(session_id: str, body: InputBody):
        record = state.store.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")

        tools = state.runtime.build_live_tools(session_id)
        responses = {rid: item.to_hitl_response() for rid, item in body.responses.items()}
        outcome = await resolve_inputs(
            state.store,
            session_id,
            responses,
            tools=tools,
            max_result_bytes=state.runtime.max_result_bytes_for(session_id),
            spill_sink=state.runtime.build_spill_sink(session_id),
        )

        result = {
            "resolved": outcome.resolved,
            "rejected": [[rid, reason] for rid, reason in outcome.rejected],
            "expired": outcome.expired,
            "invalidated": outcome.invalidated,
            "readyToContinue": outcome.ready_to_continue,
        }
        # Stale response: the body targeted at least one request id, and
        # every single one of them was rejected (unknown/already
        # resolved/expired) — none resolved, none merely invalidated.
        stale = (
            bool(body.responses)
            and not outcome.resolved
            and not outcome.invalidated
            and len(outcome.rejected) == len(body.responses)
        )
        return JSONResponse(status_code=409 if stale else 200, content=result)

    @app.post("/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str) -> dict:
        record = state.store.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")

        running = state.running.get(session_id)
        if running is None:
            return {"cancelled": False, "state": _effective_state(state, session_id)}

        running.harness.cancel()
        try:
            await running.task
        except Exception:  # noqa: BLE001 - cancellation must not surface as a 500
            pass
        return {"cancelled": True, "state": _effective_state(state, session_id)}

    # -- 9.5 fleet views ------------------------------------------------

    @app.get("/approvals")
    def list_approvals(status: str = "pending") -> list[dict]:
        if status != "pending":
            raise HTTPException(status_code=422, detail="only status=pending is supported")

        now = current_timestamp_ms()
        rows = pending_requests_fleet(state.store, kinds=("tool_approval", "question"))
        return [
            {
                "requestId": row.request.id,
                "kind": row.request.kind,
                "toolName": row.request.tool_name,
                "agentId": row.agent_id,
                "sessionId": row.session_id,
                "rootSessionId": row.root_session_id,
                "path": [
                    {"sessionId": hop.session_id, "agentId": hop.agent_id} for hop in row.path
                ],
                "ageSeconds": max(0.0, (now - row.created_at) / 1000),
                "payload": row.request.payload,
                "createdAt": row.created_at,
            }
            for row in rows
        ]

    @app.get("/connections/health")
    async def connections_health() -> list[dict]:
        targets = [
            (bundle_id, connection_name)
            for bundle_id, bundle in state.fleet.bundles.items()
            if bundle is not None
            for connection_name in bundle.connections
        ]

        async def _one(bundle_id: str, connection_name: str) -> dict:
            try:
                report = await check_connection_health(
                    state.fleet.root,
                    bundle_id,
                    connection_name,
                    token_resolver=state.runtime.token_resolver,
                    transport_factory=state.transport_factory,
                )
            except Exception as exc:  # noqa: BLE001 - unreachable must not fail the endpoint
                return {
                    "bundle": bundle_id,
                    "connection": connection_name,
                    "status": "unreachable",
                    "added": [],
                    "removed": [],
                    "schemaChanged": [],
                    "error": str(exc),
                }
            return {
                "bundle": report.bundle_id,
                "connection": report.connection,
                "status": report.status,
                "added": list(report.added),
                "removed": list(report.removed),
                "schemaChanged": list(report.schema_changed),
            }

        return list(await asyncio.gather(*(_one(b, c) for b, c in targets)))
