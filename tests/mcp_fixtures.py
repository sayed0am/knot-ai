"""A real ``mcp`` SDK server, served in-process for offline tests.

Not itself a test module (no ``test_`` prefix): pytest never collects it.

Transport: streamable HTTP over ``httpx2.ASGITransport`` pointed straight at
the server's own Starlette app — no socket, no port, nothing to bind or
race on. The ``mcp`` SDK's streamable-HTTP session manager needs its
lifespan entered for its background task group to run (``ASGITransport``
never sends ASGI lifespan events on its own), so :func:`mock_mcp_server`
drives that manually via ``MCPServer.session_manager.run()`` around the
whole fixture body, and every test builds its own ``httpx2.AsyncClient``
against a ``transport_factory`` pointed at the returned handle's app.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx2
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

#: DNS-rebinding protection rejects a bare "127.0.0.1" Host header (no
#: port), which is exactly what an ASGITransport-routed request carries —
#: there is no real socket, so there is nothing to protect against here.
_NO_DNS_PROTECTION = TransportSecuritySettings(enable_dns_rebinding_protection=False)


@dataclass
class RecordedCall:
    tool: str
    arguments: dict
    headers: dict[str, str]


@dataclass
class MockServerHandle:
    """Everything a test needs to talk to (and inspect) the mock server."""

    app: object
    url: str
    calls: list[RecordedCall] = field(default_factory=list)
    mcp_server: MCPServer = field(repr=False, default=None)  # type: ignore[assignment]

    def transport_factory(self, _url: str) -> httpx2.AsyncBaseTransport:
        return httpx2.ASGITransport(app=self.app)


class _FailNRequestsMiddleware(BaseHTTPMiddleware):
    """Fail some ``tools/call`` JSON-RPC requests with a bare 401 response,
    then let every other request through untouched.

    Targets by JSON-RPC method (not raw request order) so the handshake
    (``initialize``, ``notifications/initialized``) never accidentally trips
    it regardless of how many round trips it takes. ``fail_on_call_number``
    fails exactly that 1-indexed ``tools/call`` (the "one 401, then it
    recovers" scenario); ``always_fail=True`` fails every ``tools/call``
    forever (the "credentials are just bad" scenario).
    """

    def __init__(
        self, app, *, fail_on_call_number: int = 1, always_fail: bool = False
    ) -> None:
        super().__init__(app)
        self._fail_on_call_number = fail_on_call_number
        self._always_fail = always_fail
        self._call_count = 0

    async def dispatch(self, request, call_next):
        body = await request.body()
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        if isinstance(data, dict) and data.get("method") == "tools/call":
            self._call_count += 1
            if self._always_fail or self._call_count == self._fail_on_call_number:
                return PlainTextResponse("unauthorized", status_code=401)
        return await call_next(request)


def build_mock_server(
    *,
    calls: list[RecordedCall] | None = None,
    big_result_size: int = 300_000,
) -> MCPServer:
    """A small MCP server: an echo tool, an adder, an oversized result, and
    a tool that broadcasts a ``tools/list_changed`` notification."""
    server = MCPServer("knot-test-mock")
    recorded = calls if calls is not None else []

    @server.tool()
    async def echo(message: str, ctx: Context) -> str:
        headers = dict(ctx.headers or {})
        recorded.append(RecordedCall(tool="echo", arguments={"message": message}, headers=headers))
        return message

    @server.tool()
    async def add(a: int, b: int, ctx: Context) -> int:
        headers = dict(ctx.headers or {})
        recorded.append(RecordedCall(tool="add", arguments={"a": a, "b": b}, headers=headers))
        return a + b

    @server.tool()
    async def big_result(ctx: Context) -> str:
        headers = dict(ctx.headers or {})
        recorded.append(RecordedCall(tool="big_result", arguments={}, headers=headers))
        return "x" * big_result_size

    @server.tool()
    async def trigger_list_changed(ctx: Context) -> str:
        await ctx.notify_tools_changed()
        return "sent"

    return server


@asynccontextmanager
async def mock_mcp_server(
    *,
    calls: list[RecordedCall] | None = None,
    fail_first_n_calls: int = 0,
    fail_all_calls: bool = False,
    big_result_size: int = 300_000,
) -> AsyncIterator[MockServerHandle]:
    """Serve a real ``mcp`` SDK server in-process for the duration of the block.

    ``fail_first_n_calls=1``: the first ``tools/call`` request gets a bare
    401 instead of being handled, then every later one succeeds — exercises
    the runtime's evict-and-retry-once behavior. ``fail_all_calls=True``:
    every ``tools/call`` gets a 401 forever — exercises "second 401 is a
    real failure".

    Every instance gets its own URL path (a random suffix): the runtime's
    connection pool and token cache are keyed by server URL, so two mock
    servers used in the same test process — including across two different
    tests sharing a process-wide default pool/cache — must never collide on
    that key the way a hardcoded path would.
    """
    recorded = calls if calls is not None else []
    server = build_mock_server(calls=recorded, big_result_size=big_result_size)
    path = f"/mcp-{uuid.uuid4().hex[:12]}"
    app: object = server.streamable_http_app(
        streamable_http_path=path, transport_security=_NO_DNS_PROTECTION
    )
    if fail_all_calls:
        app = _FailNRequestsMiddleware(app, always_fail=True)
    elif fail_first_n_calls > 0:
        app = _FailNRequestsMiddleware(app, fail_on_call_number=fail_first_n_calls)

    async with server.session_manager.run():
        yield MockServerHandle(
            app=app, url=f"http://127.0.0.1{path}", calls=recorded, mcp_server=server
        )


def asgi_transport_factory(handle: MockServerHandle) -> Callable[[str], httpx2.AsyncBaseTransport]:
    """A ``TransportFactory`` (see ``knot.authoring.mcp_client``) pointed at
    one mock server handle, regardless of what URL a connection config names."""
    return handle.transport_factory


__all__ = [
    "MockServerHandle",
    "RecordedCall",
    "asgi_transport_factory",
    "build_mock_server",
    "mock_mcp_server",
]
