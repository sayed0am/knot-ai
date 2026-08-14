"""End-to-end: a bundle connection, compiled and run through a real AgentRuntime.

Companion to ``tests/test_mcp_client.py`` (which exercises the executor,
pool, and auth in isolation) and ``tests/test_authoring_connections.py``
(refresh/health) — this file is the "does the whole thing actually work
wired together" proof: compile -> refresh -> AgentRuntime -> a real, pooled
call through the mock server.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from authoring_fixtures import write_files
from mcp_fixtures import asgi_transport_factory, mock_mcp_server

from knot.authoring.compile import compile_fleet
from knot.authoring.connections import refresh_snapshots
from knot.authoring.mcp_client import CallContext, ConnectionPool
from knot.authoring.runtime import AgentRuntime
from knot.core.events import AgentEndEvent
from knot.core.hitl.resume import ApproveResponse, resolve_inputs
from knot.core.session.queries import scan_payloads
from knot.core.session.store import SessionStore
from knot.providers.fake import FakeProvider, reply
from knot.providers.messages import ToolCall


def _write_fleet(
    tmp_path: Path,
    *,
    handle,
    connection_name: str = "crm",
    allow: list[str] | None = None,
    approvals: str = "",
    provided_arguments: str = "",
    auth_env: str | None = None,
) -> None:
    allow_line = f"    allow: {allow}\n" if allow is not None else ""
    auth_line = f"    auth_env: {auth_env}\n" if auth_env is not None else ""
    write_files(
        tmp_path,
        {
            "shared/svc/bundle.yaml": (
                "connections:\n"
                f"  {connection_name}:\n"
                f"    url: {handle.url}\n"
                "    transport: streamable_http\n"
                f"{allow_line}{auth_line}{provided_arguments}"
                f"{approvals}"
            ),
            "agents/root/instructions.md": "you help with billing\n",
            "agents/root/agent.yaml": "use: [svc]\n",
        },
    )


async def test_agent_calls_connection_tool_through_the_real_pool(tmp_path: Path) -> None:
    async with mock_mcp_server() as handle:
        tf = asgi_transport_factory(handle)
        _write_fleet(tmp_path, handle=handle, allow=["echo"])
        await refresh_snapshots(tmp_path, transport_factory=tf, captured_at="t")

        fleet = compile_fleet(tmp_path)
        assert fleet.agents["root"].ok is True

        store = SessionStore(":memory:")
        provider = FakeProvider(
            [
                reply(
                    tool_calls=[
                        ToolCall(id="c1", name="crm__echo", arguments={"message": "hello crm"})
                    ]
                ),
                reply("the crm said: hello crm"),
            ]
        )
        pool = ConnectionPool(transport_factory=tf)
        runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, connection_pool=pool)
        session = runtime.create_session("root")

        events = [event async for event in runtime.run_turn(session.session_id, "ping crm")]
        end = events[-1]
        assert isinstance(end, AgentEndEvent)
        assert end.outcome == "completed"
        assert end.messages[-1].text == "the crm said: hello crm"

        tool_result = next(
            m for m in end.messages if getattr(m, "tool_call_id", None) == "c1"
        )
        assert tool_result.is_error is False
        assert tool_result.text == "hello crm"
        assert len(handle.calls) == 1
        assert handle.calls[0].tool == "echo"

        await pool.aclose_all()
        store.close()


async def test_unknown_connection_tool_is_rejected_with_no_network_attempt(tmp_path: Path) -> None:
    """A model call to a connection tool absent from the snapshot (or never
    allow-listed) never reaches the executor — it's an unknown tool to the
    loop, resolved with no pooled connection ever opened."""
    async with mock_mcp_server() as handle:
        tf = asgi_transport_factory(handle)
        _write_fleet(tmp_path, handle=handle, allow=["echo"])  # "add" not allow-listed
        await refresh_snapshots(tmp_path, transport_factory=tf, captured_at="t")

        fleet = compile_fleet(tmp_path)
        assert fleet.agents["root"].ok is True

        store = SessionStore(":memory:")
        provider = FakeProvider(
            [
                reply(tool_calls=[ToolCall(id="c1", name="crm__add", arguments={"a": 1, "b": 2})]),
                reply("gave up"),
            ]
        )
        pool = ConnectionPool(transport_factory=tf)
        runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, connection_pool=pool)
        session = runtime.create_session("root")

        events = [event async for event in runtime.run_turn(session.session_id, "add 1 and 2")]
        end = events[-1]
        assert end.outcome == "completed"
        tool_result = next(m for m in end.messages if getattr(m, "tool_call_id", None) == "c1")
        assert tool_result.is_error is True
        assert "not found" in tool_result.text.lower()

        assert pool.connect_count == 0  # never even tried to connect
        assert handle.calls == []
        await pool.aclose_all()
        store.close()


async def test_approval_gate_sees_only_model_arguments_then_executes_via_real_pool(
    tmp_path: Path,
) -> None:
    """provided_arguments end to end: compile strips ``a`` from the model
    schema; the ``__``-suffix approval policy parks on ``crm__add`` and its
    request payload carries only the model-authored ``b``; once approved,
    the real connection executor injects the resolved ``a`` and the server
    actually receives both."""
    async with mock_mcp_server() as handle:
        tf = asgi_transport_factory(handle)
        _write_fleet(
            tmp_path,
            handle=handle,
            allow=["add"],
            provided_arguments="    provided_arguments:\n      a: current_account_id\n",
            approvals="approvals:\n  add: always\n",
        )
        await refresh_snapshots(tmp_path, transport_factory=tf, captured_at="t")

        fleet = compile_fleet(tmp_path)
        assert fleet.agents["root"].ok is True
        manifest_tool = next(t for t in fleet.agents["root"].manifest.tools if t.name == "crm__add")
        assert "a" not in manifest_tool.input_schema["properties"]  # stripped at compile

        store = SessionStore(":memory:")
        provider = FakeProvider(
            [
                reply(tool_calls=[ToolCall(id="c1", name="crm__add", arguments={"b": 5})]),
                reply("added it"),
            ]
        )
        pool = ConnectionPool(transport_factory=tf)

        def resolver(resolver_key: str, ctx: CallContext) -> int:
            assert resolver_key == "current_account_id"
            return 777

        runtime = AgentRuntime(
            fleet=fleet,
            store=store,
            provider=provider,
            connection_pool=pool,
            provided_argument_resolver=resolver,
        )
        session = runtime.create_session("root")

        events = [event async for event in runtime.run_turn(session.session_id, "add 5")]
        end = events[-1]
        assert end.outcome == "waiting_input"
        request = end.pending_requests[0]
        assert request.kind == "tool_approval"
        assert request.tool_name == "crm__add"
        # The policy only ever saw the model-authored argument, never the
        # injected one: it ran before build_connection_executor exists.
        assert request.payload["args"] == {"b": 5}
        assert handle.calls == []  # nothing executed yet: still parked

        outcome = await resolve_inputs(
            store,
            session.session_id,
            {request.id: ApproveResponse(resolved_by="tester")},
            tools=runtime.build_live_tools(session.session_id),
        )
        assert outcome.resolved == [request.id]
        assert outcome.ready_to_continue is True

        assert len(handle.calls) == 1
        assert handle.calls[0].arguments == {"a": 777, "b": 5}

        end_events = await runtime.resume_chain(session.session_id)
        assert end_events[0].outcome == "completed"
        assert end_events[0].messages[-1].text == "added it"

        await pool.aclose_all()
        store.close()


async def test_park_leaves_no_open_connection_in_the_pool(tmp_path: Path) -> None:
    """A run that parks on a connection-tool approval never touched the
    pool at all: the pool only opens a connection once execution actually
    happens (after approval), so a parked run holds nothing open."""
    async with mock_mcp_server() as handle:
        tf = asgi_transport_factory(handle)
        _write_fleet(
            tmp_path, handle=handle, allow=["echo"], approvals="approvals:\n  echo: always\n"
        )
        await refresh_snapshots(tmp_path, transport_factory=tf, captured_at="t")

        fleet = compile_fleet(tmp_path)
        store = SessionStore(":memory:")
        provider = FakeProvider(
            [reply(tool_calls=[ToolCall(id="c1", name="crm__echo", arguments={"message": "hi"})])]
        )
        pool = ConnectionPool(transport_factory=tf)
        runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, connection_pool=pool)
        session = runtime.create_session("root")

        events = [event async for event in runtime.run_turn(session.session_id, "go")]
        assert events[-1].outcome == "waiting_input"

        assert pool.size() == 0
        assert pool.connect_count == 0
        store.close()


async def test_oversized_connection_result_is_truncated_by_max_result_bytes(
    tmp_path: Path,
) -> None:
    async with mock_mcp_server(big_result_size=5_000) as handle:
        tf = asgi_transport_factory(handle)
        _write_fleet(tmp_path, handle=handle, allow=["big_result"])
        await refresh_snapshots(tmp_path, transport_factory=tf, captured_at="t")

        fleet = compile_fleet(tmp_path)
        store = SessionStore(":memory:")
        provider = FakeProvider(
            [
                reply(tool_calls=[ToolCall(id="c1", name="crm__big_result", arguments={})]),
                reply("that was big"),
            ]
        )
        pool = ConnectionPool(transport_factory=tf)
        runtime = AgentRuntime(
            fleet=fleet, store=store, provider=provider, connection_pool=pool, max_result_bytes=200
        )
        session = runtime.create_session("root")

        events = [event async for event in runtime.run_turn(session.session_id, "fetch it")]
        assert events[-1].outcome == "completed"
        tool_result = next(
            m for m in events[-1].messages if getattr(m, "tool_call_id", None) == "c1"
        )
        assert len(tool_result.text.encode("utf-8")) <= 200
        assert "truncated" in tool_result.text
        assert tool_result.details["original_bytes"] == 5_000

        await pool.aclose_all()
        store.close()


async def test_no_secrets_reach_durable_storage_or_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "sk-e2e-sentinel-token"
    monkeypatch.setenv("CRM_TOKEN", sentinel)

    async with mock_mcp_server() as handle:
        tf = asgi_transport_factory(handle)
        _write_fleet(tmp_path, handle=handle, allow=["echo"], auth_env="CRM_TOKEN")
        await refresh_snapshots(tmp_path, transport_factory=tf, captured_at="t")

        fleet = compile_fleet(tmp_path)
        store = SessionStore(":memory:")
        provider = FakeProvider(
            [
                reply(
                    tool_calls=[ToolCall(id="c1", name="crm__echo", arguments={"message": "hi"})]
                ),
                reply("done"),
            ]
        )
        pool = ConnectionPool(transport_factory=tf)
        runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, connection_pool=pool)
        session = runtime.create_session("root")

        events = [event async for event in runtime.run_turn(session.session_id, "go")]
        assert events[-1].outcome == "completed"
        assert handle.calls[0].headers.get("authorization") == f"Bearer {sentinel}"

        assert scan_payloads(store, sentinel) == []
        for event in events:
            assert sentinel not in event.model_dump_json()

        await pool.aclose_all()
        store.close()
