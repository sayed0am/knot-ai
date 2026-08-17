"""Opt-in bearer-token auth (design D9): ``create_app(..., auth_token=...)``."""

from __future__ import annotations

from pathlib import Path

from authoring_fixtures import write_files
from server_fixtures import client_for, make_app, parse_sse

from knot.providers.fake import reply


def _simple_fleet(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "you are root\n"})


async def test_no_token_configured_is_zero_behavior_change(tmp_path: Path) -> None:
    """No ``auth_token`` at all: a request with no ``Authorization`` header
    still works, exactly as the whole rest of the test suite already
    assumes (design D9: "no auth requirement" when unconfigured)."""
    _simple_fleet(tmp_path)
    app = make_app(tmp_path, [reply("hi")])

    async with client_for(app) as client:
        resp = await client.get("/agents")
        assert resp.status_code == 200


async def test_missing_header_is_401_when_a_token_is_configured(tmp_path: Path) -> None:
    _simple_fleet(tmp_path)
    app = make_app(tmp_path, [], auth_token="s3cret")

    async with client_for(app) as client:
        resp = await client.get("/agents")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"


async def test_wrong_token_is_401(tmp_path: Path) -> None:
    _simple_fleet(tmp_path)
    app = make_app(tmp_path, [], auth_token="s3cret")

    async with client_for(app) as client:
        resp = await client.get("/agents", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401


async def test_correct_token_is_accepted_on_a_plain_get(tmp_path: Path) -> None:
    _simple_fleet(tmp_path)
    app = make_app(tmp_path, [], auth_token="s3cret")

    async with client_for(app) as client:
        resp = await client.get("/agents", headers={"Authorization": "Bearer s3cret"})
        assert resp.status_code == 200


async def test_correct_token_streams_an_sse_run(tmp_path: Path) -> None:
    """A long-lived SSE stream is gated exactly like a plain request — the
    middleware runs once, before routing, not per-chunk."""
    _simple_fleet(tmp_path)
    app = make_app(tmp_path, [reply("hello there")], auth_token="s3cret")
    headers = {"Authorization": "Bearer s3cret"}

    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions", headers=headers)
        session_id = created.json()["sessionId"]
        resp = await client.post(
            f"/sessions/{session_id}/messages", json={"text": "hi"}, headers=headers
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert events[-1][0] == "agent_end"
        assert events[-1][1]["outcome"] == "completed"


async def test_sse_endpoint_401s_without_a_token_too(tmp_path: Path) -> None:
    """The auth check runs before routing, so it covers the streaming
    endpoint exactly like any other — no header means no stream at all."""
    _simple_fleet(tmp_path)
    app = make_app(tmp_path, [reply("hi")], auth_token="s3cret")

    async with client_for(app) as client:
        created = await client.post(
            "/agents/root/sessions", headers={"Authorization": "Bearer s3cret"}
        )
        session_id = created.json()["sessionId"]
        resp = await client.post(f"/sessions/{session_id}/messages", json={"text": "hi"})
        assert resp.status_code == 401


async def test_401_body_never_contains_the_token(tmp_path: Path) -> None:
    _simple_fleet(tmp_path)
    app = make_app(tmp_path, [], auth_token="the-real-secret-value")

    async with client_for(app) as client:
        resp = await client.get("/agents", headers={"Authorization": "Bearer wrong-guess"})
        assert resp.status_code == 401
        assert "the-real-secret-value" not in resp.text
        assert "wrong-guess" not in resp.text
