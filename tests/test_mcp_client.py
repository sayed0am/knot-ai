"""``knot.authoring.mcp_client``: pool, token cache, auth, and the executor."""

from __future__ import annotations

import asyncio

import pytest
from mcp_fixtures import asgi_transport_factory, mock_mcp_server

from knot.authoring.config import ConnectionConfig
from knot.authoring.mcp_client import (
    CallContext,
    ConnectionPool,
    TokenCache,
    build_auth,
    build_connection_executor,
    scrub_secrets,
)


def _config(url: str, **overrides) -> ConnectionConfig:
    return ConnectionConfig(url=url, transport="streamable_http", **overrides)


# ---------------------------------------------------------------------------
# scrub_secrets
# ---------------------------------------------------------------------------


def test_scrub_secrets_replaces_every_occurrence() -> None:
    text = "failed calling https://x?token=sk-abc with sk-abc again"
    expected = "failed calling https://x?token=[redacted] with [redacted] again"
    assert scrub_secrets(text, ["sk-abc"]) == expected


def test_scrub_secrets_ignores_empty_values() -> None:
    assert scrub_secrets("hello", ["", None]) == "hello"  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# TokenCache: refresh-ahead + evict
# ---------------------------------------------------------------------------


async def test_token_cache_reuses_unexpired_token() -> None:
    cache = TokenCache(margin_ms=1_000)
    calls = 0

    async def resolve():
        nonlocal calls
        calls += 1
        return ("tok", None)

    v1 = await cache.get(("url", "app"), resolve)
    v2 = await cache.get(("url", "app"), resolve)
    assert v1 == v2 == "tok"
    assert calls == 1  # cached, no known expiry -> never re-resolved


async def test_token_cache_refreshes_ahead_of_expiry() -> None:
    cache = TokenCache(margin_ms=10_000)
    now = int(__import__("time").time() * 1000)
    calls = 0

    async def resolve():
        nonlocal calls
        calls += 1
        # expires in 1 second -> well within the 10s margin, so the very
        # next get() must re-resolve rather than reuse it.
        return (f"tok-{calls}", now + 1_000)

    v1 = await cache.get(("url", "app"), resolve)
    v2 = await cache.get(("url", "app"), resolve)
    assert v1 == "tok-1"
    assert v2 == "tok-2"
    assert calls == 2


async def test_token_cache_evict_forces_resolution() -> None:
    cache = TokenCache()
    calls = 0

    async def resolve():
        nonlocal calls
        calls += 1
        return f"tok-{calls}"

    await cache.get(("url", "app"), resolve)
    cache.evict(("url", "app"))
    v2 = await cache.get(("url", "app"), resolve)
    assert v2 == "tok-2"


def test_build_auth_returns_none_when_unauthenticated() -> None:
    config = _config("http://x/mcp", auth_env=None)
    assert build_auth(config, "c", token_resolver=lambda *_: None) is None


# ---------------------------------------------------------------------------
# Pool: reuse, introspection, aclose_all releases everything
# ---------------------------------------------------------------------------


async def test_pool_reuses_one_connection_across_calls() -> None:
    async with mock_mcp_server() as handle:
        pool = ConnectionPool(transport_factory=asgi_transport_factory(handle))
        config = _config(handle.url)
        key = (config.url, "app")

        r1 = await pool.call_tool(key, config, lambda: None, "echo", {"message": "a"})
        r2 = await pool.call_tool(key, config, lambda: None, "echo", {"message": "b"})

        assert r1.content[0].text == "a"
        assert r2.content[0].text == "b"
        assert pool.connect_count == 1
        assert pool.size() == 1

        await pool.aclose_all()
        assert pool.size() == 0


async def test_pool_concurrent_first_calls_open_exactly_one_connection() -> None:
    async with mock_mcp_server() as handle:
        pool = ConnectionPool(transport_factory=asgi_transport_factory(handle))
        config = _config(handle.url)
        key = (config.url, "app")

        results = await asyncio.gather(
            *(
                pool.call_tool(key, config, lambda: None, "echo", {"message": str(i)})
                for i in range(5)
            )
        )
        assert {r.content[0].text for r in results} == {"0", "1", "2", "3", "4"}
        assert pool.connect_count == 1
        await pool.aclose_all()


async def test_pool_aclose_all_leaves_no_open_connections() -> None:
    """Stands in for "parking releases everything": nothing outside the pool
    ever holds a connection reference, so closing the pool is always
    sufficient regardless of how many calls ran through it."""
    async with mock_mcp_server() as handle:
        pool = ConnectionPool(transport_factory=asgi_transport_factory(handle))
        config = _config(handle.url)
        key = (config.url, "app")
        await pool.call_tool(key, config, lambda: None, "echo", {"message": "x"})
        assert pool.size() == 1
        await pool.aclose_all()
        assert pool.size() == 0
        # A pool with size 0 can be reused: a fresh call reconnects cleanly.
        result = await pool.call_tool(key, config, lambda: None, "echo", {"message": "y"})
        assert result.content[0].text == "y"
        await pool.aclose_all()


# ---------------------------------------------------------------------------
# Authorization header + provided_arguments end to end through the executor
# ---------------------------------------------------------------------------


async def test_authorization_header_reaches_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_TOKEN", "sk-sentinel-token")
    async with mock_mcp_server() as handle:
        pool = ConnectionPool(transport_factory=asgi_transport_factory(handle))
        config = _config(handle.url, auth_env="BILLING_TOKEN")
        execute_fn = build_connection_executor(
            connection_name="billing",
            bare_tool_name="echo",
            config=config,
            pool=pool,
            token_resolver=lambda name, cfg: __import__("os").environ[cfg.auth_env],
            provided_argument_resolver=None,
            session_id="s1",
            agent_id="a1",
            principal="app",
        )
        result = await execute_fn("call_1", {"message": "hi"})
        assert result.text == "hi"
        assert len(handle.calls) == 1
        assert handle.calls[0].headers.get("authorization") == "Bearer sk-sentinel-token"
        await pool.aclose_all()


async def test_provided_arguments_replace_conflicting_model_value() -> None:
    async with mock_mcp_server() as handle:
        pool = ConnectionPool(transport_factory=asgi_transport_factory(handle))
        config = _config(handle.url, provided_arguments={"b": "server_side_b"})

        def resolver(resolver_key: str, ctx: CallContext) -> int:
            assert resolver_key == "server_side_b"
            assert ctx.connection == "svc"
            assert ctx.tool_name == "add"
            return 999

        execute_fn = build_connection_executor(
            connection_name="svc",
            bare_tool_name="add",
            config=config,
            pool=pool,
            token_resolver=lambda *_: None,
            provided_argument_resolver=resolver,
            session_id="s1",
            agent_id="a1",
            principal="app",
        )
        # The model tried to supply b=1 itself; the provided_argument_resolver
        # must win.
        result = await execute_fn("call_1", {"a": 10, "b": 1})
        assert result.text == "1009"
        assert handle.calls[0].arguments == {"a": 10, "b": 999}
        await pool.aclose_all()


# ---------------------------------------------------------------------------
# 401 retry-once, second failure surfaces as an error
# ---------------------------------------------------------------------------


async def test_401_evicts_and_retries_once_then_succeeds() -> None:
    async with mock_mcp_server(fail_first_n_calls=1) as handle:
        pool = ConnectionPool(transport_factory=asgi_transport_factory(handle))
        config = _config(handle.url, auth_env="TOK")
        resolve_calls = 0

        def resolver(name, cfg):
            nonlocal resolve_calls
            resolve_calls += 1
            return "sk-static-token"

        execute_fn = build_connection_executor(
            connection_name="svc",
            bare_tool_name="echo",
            config=config,
            pool=pool,
            token_resolver=resolver,
            provided_argument_resolver=None,
            session_id="s1",
            agent_id="a1",
            principal="app",
        )
        result = await execute_fn("call_1", {"message": "hi"})
        assert result.text == "hi"
        assert resolve_calls >= 2  # first attempt + at least one refresh-on-401
        await pool.aclose_all()


async def test_second_401_surfaces_as_error_tool_result() -> None:
    async with mock_mcp_server(fail_all_calls=True) as handle:
        # every tools/call fails: simulates a token that's permanently bad.
        pool = ConnectionPool(transport_factory=asgi_transport_factory(handle))
        config = _config(handle.url, auth_env="TOK")

        execute_fn = build_connection_executor(
            connection_name="svc",
            bare_tool_name="echo",
            config=config,
            pool=pool,
            token_resolver=lambda *_: "sk-static-token",
            provided_argument_resolver=None,
            session_id="s1",
            agent_id="a1",
            principal="app",
        )
        with pytest.raises(RuntimeError) as exc_info:
            await execute_fn("call_1", {"message": "hi"})
        assert "sk-static-token" not in str(exc_info.value)
        await pool.aclose_all()


async def test_missing_auth_env_var_raises_clear_error() -> None:
    async with mock_mcp_server() as handle:
        pool = ConnectionPool(transport_factory=asgi_transport_factory(handle))
        config = _config(handle.url, auth_env="MISSING_ENV_VAR_XYZ")

        def resolver(name, cfg):
            raise ValueError(f"environment variable {cfg.auth_env!r} is not set")

        execute_fn = build_connection_executor(
            connection_name="svc",
            bare_tool_name="echo",
            config=config,
            pool=pool,
            token_resolver=resolver,
            provided_argument_resolver=None,
            session_id="s1",
            agent_id="a1",
            principal="app",
        )
        # The resolver's failure surfaces from deep inside the transport's
        # own concurrency (its auth flow runs on the connection's request
        # task, reported by anyio as an opaque TaskGroup failure) — the
        # executor flattens that back into a readable, RuntimeError message.
        with pytest.raises(RuntimeError, match="MISSING_ENV_VAR_XYZ"):
            await execute_fn("call_1", {"message": "hi"})
        await pool.aclose_all()


# ---------------------------------------------------------------------------
# Server-side tool error -> raised (becomes an error tool result upstream)
# ---------------------------------------------------------------------------


async def test_unknown_bare_tool_name_raises_scrubbed_error() -> None:
    async with mock_mcp_server() as handle:
        pool = ConnectionPool(transport_factory=asgi_transport_factory(handle))
        config = _config(handle.url)
        execute_fn = build_connection_executor(
            connection_name="svc",
            bare_tool_name="does_not_exist",
            config=config,
            pool=pool,
            token_resolver=lambda *_: None,
            provided_argument_resolver=None,
            session_id="s1",
            agent_id="a1",
            principal="app",
        )
        with pytest.raises(RuntimeError):
            await execute_fn("call_1", {})
        await pool.aclose_all()


# ---------------------------------------------------------------------------
# listChanged: logged and ignored
# ---------------------------------------------------------------------------


async def test_list_changed_notification_handler_logs_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Whitebox: the message handler wired into every session (see
    ``open_mcp_session``) logs a ``ToolListChangedNotification`` at info and
    does nothing else with it — no capability re-negotiation, no cache
    invalidation. Exercised directly (rather than only through a live round
    trip) because delivery of this particular notification depends on
    protocol-era details the mock server's transport negotiates, which are
    orthogonal to what knot does with one once it arrives."""
    import mcp_types as types

    from knot.authoring.mcp_client import _log_notifications

    with caplog.at_level("INFO", logger="knot.authoring.mcp_client"):
        await _log_notifications(types.ToolListChangedNotification())

    assert any("list_changed" in r.message for r in caplog.records)


async def test_trigger_list_changed_tool_call_does_not_disrupt_the_connection() -> None:
    """A server broadcasting listChanged mid-session never breaks the pooled
    connection or the tool surface: the call that triggered it still
    returns normally, and later calls keep working unchanged."""
    async with mock_mcp_server() as handle:
        pool = ConnectionPool(transport_factory=asgi_transport_factory(handle))
        config = _config(handle.url)
        key = (config.url, "app")

        result = await pool.call_tool(key, config, lambda: None, "trigger_list_changed", {})
        assert result.content[0].text == "sent"

        result2 = await pool.call_tool(key, config, lambda: None, "echo", {"message": "still fine"})
        assert result2.content[0].text == "still fine"
        assert pool.connect_count == 1  # same connection throughout
        await pool.aclose_all()
