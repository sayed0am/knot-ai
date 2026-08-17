"""Serve-time crash-window repair (design D8): startup repair, and the
``GET /crash-windows`` / ``POST /crash-windows/skip`` operator endpoints.
"""

from __future__ import annotations

from pathlib import Path

from authoring_fixtures import write_files
from server_fixtures import client_for, make_app, running_lifespan, state_of

from knot.providers.fake import reply

_FLEET = {
    "agents/root/instructions.md": "you are root\n",
    "agents/root/tools/safe_retry.py": (
        "from knot.authoring.tools import tool\n\n\n"
        "@tool(idempotent=True)\n"
        "def safe_retry(x: int = 1) -> str:\n"
        '    """Safe to re-run."""\n'
        "    return f'redone {x}'\n"
    ),
    "agents/root/tools/risky_write.py": (
        "from knot.authoring.tools import tool\n\n\n"
        "@tool\n"
        "def risky_write(x: int = 1) -> str:\n"
        '    """NOT safe to re-run."""\n'
        "    return f'wrote {x}'\n"
    ),
}


def _simulate_crash(
    store, session_id: str, *, request_id: str, tool_call_id: str, tool_name: str
) -> None:
    """Append ``input_requested`` + ``input_resolved(approved)`` +
    ``execution_started`` with no tool-result ``message`` — exactly what a
    crash between steps 2 and 3 of the approve path (see
    ``knot.core.hitl.resume``) leaves behind. Same fixture shape as
    ``tests/test_hitl_crash.py``'s ``_simulate_crash``, reproduced here for
    the HTTP layer.
    """
    store.append_entry(
        session_id,
        "input_requested",
        {
            "id": request_id,
            "kind": "tool_approval",
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "payload": {},
        },
    )
    store.append_entry(
        session_id,
        "input_resolved",
        {"requestId": request_id, "decision": "approved", "resolvedBy": "tester"},
    )
    store.append_entry(
        session_id,
        "execution_started",
        {"requestId": request_id, "toolCallId": tool_call_id, "toolName": tool_name},
    )


async def test_idempotent_crash_window_repaired_at_startup(tmp_path: Path) -> None:
    write_files(tmp_path, _FLEET)
    app = make_app(tmp_path, [])
    state = state_of(app)
    session = state.store.create_session("root")
    _simulate_crash(
        state.store,
        session.session_id,
        request_id="req_idempotent",
        tool_call_id="call_idempotent",
        tool_name="safe_retry",
    )

    async with running_lifespan(app):
        pass

    entries = state.store.entries(session.session_id)
    result_entry = next(
        e
        for e in entries
        if e.type == "message" and e.payload.get("toolCallId") == "call_idempotent"
    )
    assert result_entry.payload["isError"] is False
    assert "redone" in result_entry.payload["content"][0]["text"]
    # No longer an open crash window, and nothing needed an operator.
    assert state.crash_windows == []


async def test_non_idempotent_crash_window_listed_and_skippable(tmp_path: Path) -> None:
    write_files(tmp_path, _FLEET)
    app = make_app(tmp_path, [])
    state = state_of(app)
    session = state.store.create_session("root")
    _simulate_crash(
        state.store,
        session.session_id,
        request_id="req_risky",
        tool_call_id="call_risky",
        tool_name="risky_write",
    )

    async with running_lifespan(app), client_for(app) as client:
        rows = (await client.get("/crash-windows")).json()
        assert len(rows) == 1
        assert rows[0]["sessionId"] == session.session_id
        assert rows[0]["toolCallId"] == "call_risky"
        assert rows[0]["toolName"] == "risky_write"

        skip_resp = await client.post(
            "/crash-windows/skip",
            json={"sessionId": session.session_id, "toolCallId": "call_risky"},
        )
        assert skip_resp.status_code == 200
        body = skip_resp.json()
        assert body["sessionId"] == session.session_id
        assert body["toolCallId"] == "call_risky"

        # Durably recorded as an error tool result naming the operator
        # resolution (spec: "Operator skips a window").
        entries = state.store.entries(session.session_id)
        result_entry = next(
            e
            for e in entries
            if e.type == "message" and e.payload.get("toolCallId") == "call_risky"
        )
        assert result_entry.payload["isError"] is True
        assert "Operator skip by" in result_entry.payload["content"][0]["text"]

        # Gone from the list.
        assert (await client.get("/crash-windows")).json() == []

        # A second skip of the same (now-resolved) window is a 404.
        second = await client.post(
            "/crash-windows/skip",
            json={"sessionId": session.session_id, "toolCallId": "call_risky"},
        )
        assert second.status_code == 404


async def test_skip_of_unknown_window_is_404(tmp_path: Path) -> None:
    write_files(tmp_path, _FLEET)
    app = make_app(tmp_path, [])

    async with running_lifespan(app), client_for(app) as client:
        resp = await client.post(
            "/crash-windows/skip", json={"sessionId": "sess_nope", "toolCallId": "call_nope"}
        )
        assert resp.status_code == 404


async def test_session_is_resumable_after_a_skip(tmp_path: Path) -> None:
    """Spec: "the session becomes resumable" — once the operator skip
    supplies the missing tool result, the session has no pending requests
    and no dangling tool call, so ``/continue`` succeeds."""
    write_files(tmp_path, _FLEET)
    app = make_app(tmp_path, [reply("all done")])
    state = state_of(app)
    session = state.store.create_session("root")
    _simulate_crash(
        state.store,
        session.session_id,
        request_id="req_risky",
        tool_call_id="call_risky",
        tool_name="risky_write",
    )

    async with running_lifespan(app), client_for(app) as client:
        rows = (await client.get("/crash-windows")).json()
        assert len(rows) == 1

        await client.post(
            "/crash-windows/skip",
            json={"sessionId": session.session_id, "toolCallId": "call_risky"},
        )

        resp = await client.post(f"/sessions/{session.session_id}/continue")
        assert resp.status_code == 200
