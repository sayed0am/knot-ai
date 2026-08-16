"""Shared helpers for ``knot.server.app`` HTTP tests.

Not itself a test module (no ``test_`` prefix): pytest never collects it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI

from knot.authoring.compile import compile_fleet
from knot.core.session.store import SessionStore
from knot.providers.fake import FakeProvider
from knot.providers.provider import ModelProvider
from knot.server.app import ServerState, create_app


def make_app(
    tmp_path,
    scripts,
    *,
    db_path: str = ":memory:",
    provider: ModelProvider | None = None,
    **create_app_kwargs,
) -> FastAPI:
    """Compile the fleet already written under ``tmp_path`` and build an app.

    ``scripts`` is passed straight to ``FakeProvider`` unless ``provider``
    is given explicitly (e.g. a test that needs to keep a handle on the
    provider to inspect its recorded calls).
    """
    fleet = compile_fleet(tmp_path)
    store = SessionStore(db_path)
    resolved_provider = provider if provider is not None else FakeProvider(scripts)
    # Test/validation harnesses default to "strict" (design.md D3, spec
    # "Enforcement modes"): every scenario driven through this helper runs
    # under the model-visible-logged invariant unless a caller explicitly
    # asks for something else via runtime_kwargs. The *serving* default
    # stays "warn" (see knot.server.cli.resolve_invariant_mode) — this is
    # deliberately a different default, scoped to tests only.
    runtime_kwargs = dict(create_app_kwargs.pop("runtime_kwargs", None) or {})
    runtime_kwargs.setdefault("invariant_mode", "strict")
    return create_app(
        fleet=fleet,
        store=store,
        provider=resolved_provider,
        runtime_kwargs=runtime_kwargs,
        **create_app_kwargs,
    )


def state_of(app: FastAPI) -> ServerState:
    """Reach back into an app built by ``make_app``/``create_app`` for its
    ``ServerState`` — tests use this to inspect ``running``/``follow_ups``
    or the underlying ``store``/``fleet`` directly."""
    return app.state.knot


def client_for(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@asynccontextmanager
async def live_server(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Serve ``app`` over a real loopback socket for the duration of the block.

    ``httpx.ASGITransport`` (used by ``client_for``) drives an ASGI app to
    full completion before handing the client anything at all — it collects
    every ``send()`` chunk into a list and only then returns a response, so
    it can never represent genuine partial delivery or a real mid-stream
    disconnect. Tests that need either (checking a session is truly
    ``running`` while a stream is still open; a client that disconnects
    before a run finishes) bind an actual socket instead, via a real
    ``uvicorn.Server`` on an OS-assigned port.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            yield client
    finally:
        server.should_exit = True
        await server_task


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse a raw SSE response body into ``(event_type, data_dict)`` pairs.

    Splits on blank-line-separated frames; each frame is expected to carry
    exactly one ``event:`` line and one ``data:`` line, in that order —
    exactly what ``knot.server.app``'s ``_sse_agent_event``/``_sse_raw_event``
    produce.
    """
    import json

    events: list[tuple[str, dict]] = []
    for frame in text.strip("\n").split("\n\n"):
        if not frame.strip():
            continue
        event_type: str | None = None
        data_line: str | None = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                event_type = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_line = line.removeprefix("data: ")
        assert event_type is not None and data_line is not None, f"malformed SSE frame: {frame!r}"
        events.append((event_type, json.loads(data_line)))
    return events


SIMPLE_ROOT_FLEET = {
    "agents/root/instructions.md": "you are a helpful assistant\n",
}
