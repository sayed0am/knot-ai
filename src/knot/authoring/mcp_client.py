"""Live MCP transport: connection pooling, auth, and the runtime executor.

Where ``knot.authoring.connections`` only ever talks to a server offline
(``refresh``/health checks, driven by a human or a CLI), this module is the
one place a *running* agent talks to a live MCP server: a process-wide
:class:`ConnectionPool` of already-``initialize()``'d :class:`mcp.ClientSession`
objects keyed by ``(server_url, principal)``, a :class:`TokenCache` with
refresh-ahead renewal, and :func:`build_connection_executor`, which turns one
connection's committed snapshot tool into a live ``AgentTool.execute_fn``.

Auth is layered entirely inside httpx's own request pipeline
(:class:`BearerTokenAuth`, an ``httpx2.Auth`` flow): a token is attached to
every outbound request, and a 401 response is handled by evicting the cached
token, resolving a fresh one, and retrying exactly once — transparently to
the ``mcp`` SDK above it. A token is therefore never visible to model
context, never lives in a tool result, and any error text that might
accidentally quote it is scrubbed defensively before it can reach a session's
durable entry log (see :func:`scrub_secrets`).

Session lifecycle is per-connection, not per-session: nothing here is owned
by (or torn down with) a chat session. A tool call acquires a pooled client,
uses it, and releases nothing of its own — parking a session therefore
leaves no open connection behind by construction (the pool holds the only
reference).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING

import httpx2
import mcp_types as types
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from knot.core.tools import AgentToolResult
from knot.providers.messages import ImageContent, TextContent

if TYPE_CHECKING:
    from knot.authoring.config import ConnectionConfig
    from knot.providers.provider import CancellationToken

logger = logging.getLogger(__name__)

#: Either a bare token value, or ``(value, expires_at_ms)`` when the resolver
#: knows the token's expiry. ``expires_at_ms`` of ``None`` means "cache
#: indefinitely" (only an observed 401 evicts it).
Token = str | tuple[str, "int | None"]

#: ``(connection_name, config) -> Token | None``, sync or async. ``None``
#: means "no token for this call" (distinct from "auth_env not configured",
#: which is decided by the caller before ever reaching a resolver).
TokenResolver = Callable[[str, "ConnectionConfig"], "Token | None | Awaitable[Token | None]"]

#: Builds a replacement (or wrapping) transport for one server URL. Tests use
#: this to point the client at an in-process ASGI app instead of a real
#: socket; production code leaves it ``None`` and gets httpx's normal
#: connection-pooled HTTP transport.
TransportFactory = Callable[[str], httpx2.AsyncBaseTransport]


@dataclass(frozen=True, slots=True)
class CallContext:
    """What a ``provided_argument_resolver`` is told about one call."""

    session_id: str
    agent_id: str
    connection: str
    tool_name: str


#: ``(resolver_key, CallContext) -> value``, sync or async. ``resolver_key``
#: is the value side of a connection's ``provided_arguments`` mapping (see
#: ``knot.authoring.config.ConnectionConfig``); the returned value replaces
#: whatever the model supplied for that argument name.
ProvidedArgumentResolver = Callable[[str, CallContext], object]

_DEFAULT_REFRESH_MARGIN_MS = 60_000


async def _maybe_await(value: object) -> object:
    if isawaitable(value):
        return await value
    return value


def scrub_secrets(text: str, secrets: Sequence[str]) -> str:
    """Replace every occurrence of a known secret value in ``text``.

    Defensive, not a primary control: the real boundary is that a token
    never reaches model-visible content or a tool result in the first place.
    This only guards against a token value leaking into an exception's
    ``str()`` (e.g. a transport library echoing request headers back in an
    error message).
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _normalize_token(token: Token | None) -> tuple[str, int | None] | None:
    if token is None:
        return None
    if isinstance(token, str):
        return token, None
    return token


@dataclass(slots=True)
class _TokenEntry:
    value: str
    expires_at_ms: int | None


class TokenCache:
    """Per-``(server_url, principal)`` bearer tokens, with refresh-ahead.

    A cached token is reused until it is within ``margin_ms`` of its known
    ``expires_at_ms`` (or forever, when the resolver reported no expiry);
    past that margin (or on an explicit :meth:`evict`, e.g. after an
    observed 401) the next :meth:`get` call re-resolves it.
    """

    def __init__(self, *, margin_ms: int = _DEFAULT_REFRESH_MARGIN_MS) -> None:
        self._margin_ms = margin_ms
        self._entries: dict[tuple[str, str], _TokenEntry] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def get(
        self, key: tuple[str, str], resolve: Callable[[], Awaitable[Token | None]]
    ) -> str | None:
        async with self._lock_for(key):
            entry = self._entries.get(key)
            now_ms = int(time.time() * 1000)
            if entry is not None and (
                entry.expires_at_ms is None or entry.expires_at_ms - now_ms > self._margin_ms
            ):
                return entry.value
            normalized = _normalize_token(await resolve())
            if normalized is None:
                self._entries.pop(key, None)
                return None
            value, expires_at_ms = normalized
            self._entries[key] = _TokenEntry(value=value, expires_at_ms=expires_at_ms)
            return value

    def evict(self, key: tuple[str, str]) -> None:
        self._entries.pop(key, None)

    def known_values(self) -> list[str]:
        """Every currently cached token value, for defensive error scrubbing."""
        return [entry.value for entry in self._entries.values()]


class BearerTokenAuth(httpx2.Auth):
    """httpx auth flow: attach a bearer token; on 401, evict + retry once.

    ``get_token(force_refresh)`` is expected to consult (and, on
    ``force_refresh``, bypass) a :class:`TokenCache`; the retry itself is
    invisible to the ``mcp`` SDK above — it sees at most one HTTP response
    per logical request, whichever attempt finally returns.
    """

    def __init__(self, get_token: Callable[[bool], Awaitable[str | None]]) -> None:
        self._get_token = get_token

    async def async_auth_flow(self, request: httpx2.Request):
        token = await self._get_token(False)
        if token is not None:
            request.headers["authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            token = await self._get_token(True)
            if token is not None:
                request.headers["authorization"] = f"Bearer {token}"
            yield request


def build_auth(
    config: ConnectionConfig,
    connection_name: str,
    *,
    token_resolver: TokenResolver,
    cache: TokenCache | None = None,
    cache_key: tuple[str, str] | None = None,
) -> httpx2.Auth | None:
    """Build the ``httpx2.Auth`` for one connection, or ``None`` when unauthenticated.

    ``config.auth_env is None`` means "no token config" (per
    ``ConnectionConfig``): unauthenticated, no ``Authorization`` header at
    all — ``token_resolver`` is never even called in that case. With a
    ``cache``/``cache_key`` given (the runtime pool path), resolution goes
    through :class:`TokenCache`'s refresh-ahead and evict-on-401 machinery;
    without one (the one-shot refresh/health path), each call re-resolves
    directly.
    """
    if config.auth_env is None:
        return None

    async def resolve() -> Token | None:
        return await _maybe_await(token_resolver(connection_name, config))

    async def get_token(force_refresh: bool) -> str | None:
        if cache is None or cache_key is None:
            normalized = _normalize_token(await resolve())
            return normalized[0] if normalized else None
        if force_refresh:
            cache.evict(cache_key)
        return await cache.get(cache_key, resolve)

    return BearerTokenAuth(get_token)


async def _log_notifications(message: object) -> None:
    if isinstance(message, Exception):
        logger.warning("mcp transport error: %s", message)
        return
    if isinstance(message, types.ToolListChangedNotification):
        logger.info(
            "connection sent tools/list_changed; ignoring — the compiled tool "
            "surface is pinned to its committed snapshot until the next refresh"
        )


@asynccontextmanager
async def open_mcp_session(
    config: ConnectionConfig,
    *,
    auth: httpx2.Auth | None = None,
    transport_factory: TransportFactory | None = None,
    message_handler: Callable[[object], Awaitable[None]] | None = None,
):
    """One connected, ``initialize()``'d :class:`mcp.ClientSession`.

    Selects streamable HTTP or SSE per ``config.transport`` (stdio is
    already rejected at config-validation time). ``transport_factory``, when
    given, replaces the underlying httpx transport — the seam tests use to
    point at an in-process ASGI app instead of a real socket.
    """
    handler = message_handler or _log_notifications
    async with AsyncExitStack() as stack:
        if config.transport == "streamable_http":
            transport = transport_factory(config.url) if transport_factory else None
            http_client = httpx2.AsyncClient(
                auth=auth, transport=transport, timeout=httpx2.Timeout(30.0, read=300.0)
            )
            await stack.enter_async_context(http_client)
            read, write = await stack.enter_async_context(
                streamable_http_client(config.url, http_client=http_client)
            )
        else:

            def _sse_client_factory(
                headers: dict[str, str] | None = None,
                timeout: httpx2.Timeout | None = None,
                auth: httpx2.Auth | None = None,
            ) -> httpx2.AsyncClient:
                transport = transport_factory(config.url) if transport_factory else None
                return httpx2.AsyncClient(
                    headers=headers, timeout=timeout, auth=auth, transport=transport
                )

            read, write = await stack.enter_async_context(
                sse_client(config.url, auth=auth, httpx_client_factory=_sse_client_factory)
            )

        session = await stack.enter_async_context(
            ClientSession(read, write, message_handler=handler)
        )
        await session.initialize()
        yield session


async def list_all_tools(session: ClientSession) -> list[types.Tool]:
    """Every tool the server reports, paging through ``tools/list`` to completion."""
    tools: list[types.Tool] = []
    cursor: str | None = None
    while True:
        params = types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
        result = await session.list_tools(params=params)
        tools.extend(result.tools)
        if result.next_cursor is None:
            return tools
        cursor = result.next_cursor


# ---------------------------------------------------------------------------
# Process-wide connection pool
# ---------------------------------------------------------------------------
#
# A pooled MCP session is owned by one dedicated, long-lived asyncio task per
# ``(server_url, principal)`` — never by whichever short-lived tool-call task
# happens to be first to need it. This matters because the ``mcp`` SDK's
# transports are built on anyio cancel scopes, which may only be exited by
# the task that entered them; the core loop runs every tool call as its own
# ``asyncio.create_task`` (see ``knot.core.loop``), so a connection "owned"
# by whichever call first opened it would become unusable — even to close —
# the moment that call's task finished. Each :class:`ConnectionPool` entry is
# therefore a tiny actor (:meth:`ConnectionPool._run_worker`): it opens the
# session once, then services a queue of ``(tool_name, arguments, future)``
# work items from *any* caller task until told to stop, and every anyio
# operation for that connection happens inside this one stable task for the
# connection's whole life.


@dataclass(slots=True)
class _ConnectionWorker:
    call_queue: asyncio.Queue[tuple[str, dict, asyncio.Future] | None]
    ready: asyncio.Event
    closed: asyncio.Event
    task: asyncio.Task | None = None
    error: BaseException | None = None


class ConnectionPool:
    """Live MCP sessions, pooled and reused by ``(server_url, principal)``.

    Per-call lifecycle on top of a pooled connection: :meth:`call_tool`
    enqueues one unit of work with the connection's owning worker task and
    awaits its result — it releases nothing of its own, since the pool (via
    its worker task) holds the only reference to the connection. Parking a
    session therefore leaves no open connection behind by construction:
    nothing session-scoped ever held one to begin with.
    """

    def __init__(self, *, transport_factory: TransportFactory | None = None) -> None:
        self._transport_factory = transport_factory
        self._workers: dict[tuple[str, str], _ConnectionWorker] = {}
        self._lock = asyncio.Lock()
        #: How many times a new connection was actually opened — introspection
        #: for pool-reuse tests, distinct from ``size()`` (currently-live count).
        self.connect_count = 0

    async def call_tool(
        self,
        key: tuple[str, str],
        config: ConnectionConfig,
        auth_factory: Callable[[], httpx2.Auth | None],
        tool_name: str,
        arguments: dict,
    ) -> types.CallToolResult:
        """Run one tool call through the pooled connection for ``key``,
        connecting lazily (via ``auth_factory``, called only on a fresh
        connect) if none is live yet."""
        worker = await self._worker_for(key, config, auth_factory)
        future: asyncio.Future[types.CallToolResult] = asyncio.get_running_loop().create_future()
        await worker.call_queue.put((tool_name, arguments, future))
        return await future

    async def _worker_for(
        self,
        key: tuple[str, str],
        config: ConnectionConfig,
        auth_factory: Callable[[], httpx2.Auth | None],
    ) -> _ConnectionWorker:
        async with self._lock:
            worker = self._workers.get(key)
            if worker is None:
                worker = _ConnectionWorker(
                    call_queue=asyncio.Queue(), ready=asyncio.Event(), closed=asyncio.Event()
                )
                self._workers[key] = worker
                worker.task = asyncio.create_task(
                    self._run_worker(key, config, auth_factory(), worker)
                )
                self.connect_count += 1
        await worker.ready.wait()
        if worker.error is not None:
            raise worker.error
        return worker

    async def _run_worker(
        self,
        key: tuple[str, str],
        config: ConnectionConfig,
        auth: httpx2.Auth | None,
        worker: _ConnectionWorker,
    ) -> None:
        try:
            async with open_mcp_session(
                config, auth=auth, transport_factory=self._transport_factory
            ) as session:
                worker.ready.set()
                while True:
                    item = await worker.call_queue.get()
                    if item is None:
                        break
                    tool_name, arguments, future = item
                    try:
                        result = await session.call_tool(tool_name, arguments)
                    except BaseException as exc:  # noqa: BLE001 - relayed to the caller's future
                        if not future.cancelled():
                            future.set_exception(exc)
                    else:
                        if not future.cancelled():
                            future.set_result(result)
        except BaseException as exc:
            worker.error = exc
            worker.ready.set()
        finally:
            worker.closed.set()
            async with self._lock:
                if self._workers.get(key) is worker:
                    del self._workers[key]

    async def evict(self, key: tuple[str, str]) -> None:
        async with self._lock:
            worker = self._workers.pop(key, None)
        if worker is not None:
            await worker.call_queue.put(None)
            await worker.closed.wait()

    async def aclose_all(self) -> None:
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            await worker.call_queue.put(None)
        for worker in workers:
            await worker.closed.wait()

    def size(self) -> int:
        """Number of live pooled connections — introspection for tests."""
        return len(self._workers)


_default_pool: ConnectionPool | None = None
_default_token_cache: TokenCache | None = None


def default_pool() -> ConnectionPool:
    """The module-global :class:`ConnectionPool` used when ``AgentRuntime``
    is not given one explicitly. Deliberately process-wide, not
    session-scoped (see the module docstring); tests should call
    :func:`reset_default_pool` in teardown."""
    global _default_pool
    if _default_pool is None:
        _default_pool = ConnectionPool()
    return _default_pool


def default_token_cache() -> TokenCache:
    global _default_token_cache
    if _default_token_cache is None:
        _default_token_cache = TokenCache()
    return _default_token_cache


async def reset_default_pool() -> None:
    """Close and drop the module-global pool and token cache. Test-only."""
    global _default_pool, _default_token_cache
    if _default_pool is not None:
        await _default_pool.aclose_all()
    _default_pool = None
    _default_token_cache = None


# ---------------------------------------------------------------------------
# The runtime executor
# ---------------------------------------------------------------------------


def flatten_exception_text(exc: BaseException, *, _seen: int = 0) -> str:
    """A readable message for ``exc``, unwrapping ``BaseExceptionGroup`` and
    ``__cause__`` chains down to their leaf messages.

    A failure raised from inside an ``httpx2.Auth`` flow (e.g. a
    ``token_resolver`` error) surfaces through the ``mcp`` SDK's own
    per-request anyio task, which anyio reports as an opaque "unhandled
    errors in a TaskGroup" at the top — this recovers the actual reason
    a caller needs to see.
    """
    if _seen > 8:  # defensive: never loop forever on a pathological chain
        return str(exc)
    parts = [str(exc)]
    for sub in getattr(exc, "exceptions", ()):
        parts.append(flatten_exception_text(sub, _seen=_seen + 1))
    if exc.__cause__ is not None:
        parts.append(flatten_exception_text(exc.__cause__, _seen=_seen + 1))
    return " | ".join(dict.fromkeys(p for p in parts if p))


def _content_to_result(result: types.CallToolResult) -> AgentToolResult:
    blocks: list[TextContent | ImageContent] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            blocks.append(TextContent(text=block.text))
        elif isinstance(block, types.ImageContent):
            blocks.append(ImageContent(data=block.data, mime_type=block.mime_type))
        else:
            blocks.append(TextContent(text=str(block)))
    return AgentToolResult(content=blocks)


def build_connection_executor(
    *,
    connection_name: str,
    bare_tool_name: str,
    config: ConnectionConfig,
    pool: ConnectionPool,
    token_resolver: TokenResolver,
    provided_argument_resolver: ProvidedArgumentResolver | None,
    session_id: str,
    agent_id: str,
    principal: str,
) -> Callable[..., Awaitable[AgentToolResult]]:
    """Build one connection tool's live ``execute_fn``.

    Every call: resolves provided-argument values (replacing any
    model-supplied value for the same key — the decision hook already ran on
    the *unmodified* model arguments before this ever executes), acquires a
    pooled session for ``(config.url, principal)``, and calls the tool by
    its bare (unqualified) name. Any failure — auth, transport, or a
    server-reported tool error — is raised as an exception with secret
    values scrubbed from its text, which the core loop turns into an error
    tool result; nothing here ever crashes the run.
    """
    key = (config.url, principal)

    async def execute_fn(
        tool_call_id: str,
        arguments: object,
        signal: CancellationToken | None = None,
        on_update: object = None,
    ) -> AgentToolResult:
        cache = default_token_cache()

        def auth_factory() -> httpx2.Auth | None:
            return build_auth(
                config, connection_name, token_resolver=token_resolver, cache=cache, cache_key=key
            )

        final_arguments: dict[str, object] = (
            dict(arguments) if isinstance(arguments, Mapping) else {}
        )
        if provided_argument_resolver is not None and config.provided_arguments:
            context = CallContext(
                session_id=session_id,
                agent_id=agent_id,
                connection=connection_name,
                tool_name=bare_tool_name,
            )
            for arg_name, resolver_key in config.provided_arguments.items():
                value = await _maybe_await(provided_argument_resolver(resolver_key, context))
                final_arguments[arg_name] = value

        try:
            result = await pool.call_tool(
                key, config, auth_factory, bare_tool_name, final_arguments
            )
        except Exception as exc:
            message = scrub_secrets(
                f"connection {connection_name!r} tool {bare_tool_name!r} failed: "
                f"{flatten_exception_text(exc)}",
                cache.known_values(),
            )
            raise RuntimeError(message) from exc

        if result.is_error:
            text = "".join(
                block.text for block in result.content if isinstance(block, types.TextContent)
            )
            message = scrub_secrets(text or "tool call failed", cache.known_values())
            raise RuntimeError(message)

        return _content_to_result(result)

    return execute_fn


__all__ = [
    "BearerTokenAuth",
    "CallContext",
    "ConnectionPool",
    "ProvidedArgumentResolver",
    "Token",
    "TokenCache",
    "TokenResolver",
    "TransportFactory",
    "build_auth",
    "build_connection_executor",
    "default_pool",
    "default_token_cache",
    "flatten_exception_text",
    "list_all_tools",
    "open_mcp_session",
    "reset_default_pool",
    "scrub_secrets",
]
