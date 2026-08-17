"""Session lifecycle over HTTP: create, read, message, input, continue, cancel."""

from __future__ import annotations

import asyncio
from pathlib import Path

from authoring_fixtures import write_files
from server_fixtures import client_for, live_server, make_app, parse_sse, state_of

from knot.core.session.entries import ENTRY_TYPE_MESSAGE
from knot.providers.fake import FakeProvider, reply, tool_call
from knot.providers.messages import UserMessage


def _gated_fleet(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/agent.yaml": "approvals:\n  sensitive_op: always\n",
            "agents/root/tools/sensitive_op.py": (
                "from knot.authoring.tools import tool\n\n\n"
                "@tool\n"
                "def sensitive_op(amount: int) -> str:\n"
                '    """Do something that needs a human\'s sign-off."""\n'
                "    return f'moved {amount}'\n"
            ),
        },
    )


# -- create / get -------------------------------------------------------


async def test_create_session_unknown_agent_is_404(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        resp = await client.post("/agents/nope/sessions")
        assert resp.status_code == 404


async def test_create_session_failed_compile_agent_is_409(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/broken/agent.yaml": "description: x\n"})
    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        resp = await client.post("/agents/broken/sessions")
        assert resp.status_code == 409


async def test_get_session_unknown_is_404(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        resp = await client.get("/sessions/sess_nope")
        assert resp.status_code == 404


# -- full park/approve round trip over HTTP -----------------------------


async def test_full_approve_round_trip_over_http(tmp_path: Path) -> None:
    _gated_fleet(tmp_path)
    app = make_app(
        tmp_path,
        [
            tool_call("sensitive_op", {"amount": 100}),
            reply("all done, moved 100"),
        ],
    )

    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]

        stream = await client.post(f"/sessions/{session_id}/messages", json={"text": "move 100"})
        assert stream.status_code == 200
        events = parse_sse(stream.text)
        end_type, end_data = events[-1]
        assert end_type == "agent_end"
        assert end_data["outcome"] == "waiting_input"
        assert len(end_data["pendingRequests"]) == 1
        request_id = end_data["pendingRequests"][0]["id"]

        got = await client.get(f"/sessions/{session_id}")
        assert got.json()["state"] == "waiting"
        assert len(got.json()["pendingRequests"]) == 1

        approvals = await client.get("/approvals")
        assert approvals.status_code == 200
        rows = approvals.json()
        assert len(rows) == 1
        assert rows[0]["requestId"] == request_id
        assert rows[0]["kind"] == "tool_approval"
        assert rows[0]["toolName"] == "sensitive_op"
        assert rows[0]["sessionId"] == session_id
        assert rows[0]["rootSessionId"] == session_id
        assert rows[0]["ageSeconds"] >= 0

        resolved = await client.post(
            f"/sessions/{session_id}/input",
            json={"responses": {request_id: {"action": "approve", "by": "ops"}}},
        )
        assert resolved.status_code == 200
        rbody = resolved.json()
        assert rbody["resolved"] == [request_id]
        assert rbody["readyToContinue"] is True

        cont = await client.post(f"/sessions/{session_id}/continue")
        assert cont.status_code == 200
        cont_events = parse_sse(cont.text)
        assert cont_events[-1] == (
            "agent_end",
            cont_events[-1][1],
        )
        assert cont_events[-1][1]["outcome"] == "completed"

        final = await client.get(f"/sessions/{session_id}")
        fbody = final.json()
        assert fbody["state"] == "idle"
        tool_results = [m for m in fbody["transcript"] if m.get("role") == "toolResult"]
        assert len(tool_results) == 1
        assert tool_results[0]["toolCallId"] == "call_sensitive_op"
        assert "moved 100" in tool_results[0]["content"][0]["text"]
        assert tool_results[0]["isError"] is False

        # approvals inbox is empty again now that it's resolved.
        approvals_after = await client.get("/approvals")
        assert approvals_after.json() == []


async def test_full_deny_round_trip_over_http(tmp_path: Path) -> None:
    _gated_fleet(tmp_path)
    app = make_app(
        tmp_path,
        [
            tool_call("sensitive_op", {"amount": 100}),
            reply("okay, skipping that"),
        ],
    )

    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]

        stream = await client.post(f"/sessions/{session_id}/messages", json={"text": "move 100"})
        request_id = parse_sse(stream.text)[-1][1]["pendingRequests"][0]["id"]

        resolved = await client.post(
            f"/sessions/{session_id}/input",
            json={
                "responses": {
                    request_id: {"action": "deny", "by": "ops", "reason": "not authorized"}
                }
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["resolved"] == [request_id]

        cont = await client.post(f"/sessions/{session_id}/continue")
        assert cont.status_code == 200
        assert parse_sse(cont.text)[-1][1]["outcome"] == "completed"

        final = await client.get(f"/sessions/{session_id}")
        tool_results = [m for m in final.json()["transcript"] if m.get("role") == "toolResult"]
        assert len(tool_results) == 1
        assert tool_results[0]["isError"] is True
        assert "not authorized" in tool_results[0]["content"][0]["text"]


# -- decision C: idle/waiting/running -----------------------------------


async def test_post_message_while_waiting_is_queued_202(tmp_path: Path) -> None:
    _gated_fleet(tmp_path)
    app = make_app(
        tmp_path,
        [
            tool_call("sensitive_op", {"amount": 5}),
            reply("first done"),
            reply("second thing handled too"),
        ],
    )

    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]

        stream = await client.post(f"/sessions/{session_id}/messages", json={"text": "move 5"})
        request_id = parse_sse(stream.text)[-1][1]["pendingRequests"][0]["id"]

        queued = await client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "also, what about the second thing?"},
        )
        assert queued.status_code == 202
        assert queued.json() == {"queued": True}

        await client.post(
            f"/sessions/{session_id}/input",
            json={"responses": {request_id: {"action": "approve", "by": "ops"}}},
        )
        cont = await client.post(f"/sessions/{session_id}/continue")
        events = parse_sse(cont.text)
        texts = [
            block["text"]
            for etype, edata in events
            if etype == "message_end" and edata["message"].get("role") == "assistant"
            for block in edata["message"]["content"]
            if block.get("type") == "text"
        ]
        assert any("second thing handled too" in t for t in texts)

        final = await client.get(f"/sessions/{session_id}")
        assert final.json()["state"] == "idle"
        assert final.json()["transcript"][-1]["content"][0]["text"] == "second thing handled too"


async def test_post_message_while_running_is_409(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/tools/slow_tool.py": (
                "import asyncio\n"
                "from knot.authoring.tools import tool\n\n\n"
                "@tool\n"
                "async def slow_tool(x: str) -> str:\n"
                '    """Slow."""\n'
                "    await asyncio.sleep(0.3)\n"
                "    return 'ok'\n"
            ),
        },
    )
    app = make_app(tmp_path, [tool_call("slow_tool", {"x": "y"}), reply("done")])

    # A real socket, not the ASGI test transport: httpx.ASGITransport always
    # buffers a streaming response to completion before the client sees
    # anything (see `live_server`'s docstring), so it can never observe a
    # session actually mid-run the way this test needs to.
    async with live_server(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]

        async with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"text": "go"}
        ) as resp:
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                if "tool_execution_start" in buf:
                    break

            conflict = await client.post(f"/sessions/{session_id}/messages", json={"text": "again"})
            assert conflict.status_code == 409

        for _ in range(50):
            got = await client.get(f"/sessions/{session_id}")
            if got.json()["state"] == "idle":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("run never completed")


async def test_continue_not_ready_is_409(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [reply("hello")])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        resp = await client.post(f"/sessions/{session_id}/continue")
        assert resp.status_code == 409


# -- input edge cases -----------------------------------------------------


async def test_input_malformed_body_is_422(tmp_path: Path) -> None:
    _gated_fleet(tmp_path)
    app = make_app(tmp_path, [tool_call("sensitive_op", {"amount": 1})])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        await client.post(f"/sessions/{session_id}/messages", json={"text": "go"})

        # 'deny' without 'reason' fails the response model's validator.
        resp = await client.post(
            f"/sessions/{session_id}/input",
            json={"responses": {"req_x": {"action": "deny", "by": "ops"}}},
        )
        assert resp.status_code == 422


async def test_input_all_stale_is_409(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [reply("hello")])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        resp = await client.post(
            f"/sessions/{session_id}/input",
            json={"responses": {"req_unknown": {"action": "approve", "by": "ops"}}},
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["rejected"] == [["req_unknown", "unknown"]]
        assert body["resolved"] == []


async def test_input_empty_responses_is_200_noop(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [reply("hello")])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        resp = await client.post(f"/sessions/{session_id}/input", json={"responses": {}})
        assert resp.status_code == 200
        assert resp.json()["resolved"] == []


# -- cancel -----------------------------------------------------------------


async def test_cancel_idle_session_is_a_noop(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        resp = await client.post(f"/sessions/{session_id}/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"cancelled": False, "state": "idle"}


async def test_cancel_running_session_stops_it(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/tools/slow_tool.py": (
                "import asyncio\n"
                "from knot.authoring.tools import tool\n\n\n"
                "@tool\n"
                "async def slow_tool(x: str) -> str:\n"
                '    """Slow."""\n'
                "    await asyncio.sleep(5)\n"
                "    return 'ok'\n"
            ),
        },
    )
    app = make_app(tmp_path, [tool_call("slow_tool", {"x": "y"}), reply("done")])

    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]

        async def run_stream() -> None:
            await client.post(f"/sessions/{session_id}/messages", json={"text": "go"})

        task = asyncio.create_task(run_stream())
        # Let the run actually start (and the tool call begin) before cancelling.
        for _ in range(50):
            got = await client.get(f"/sessions/{session_id}")
            if got.json()["state"] == "running":
                break
            await asyncio.sleep(0.02)

        cancel = await client.post(f"/sessions/{session_id}/cancel")
        assert cancel.status_code == 200
        assert cancel.json()["cancelled"] is True

        await asyncio.wait_for(task, timeout=5)
        final = await client.get(f"/sessions/{session_id}")
        assert final.json()["state"] == "idle"


# -- steer (design D5) -------------------------------------------------------


async def test_steer_mid_run_reaches_next_turn_and_is_durably_logged(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/tools/slow_tool.py": (
                "import asyncio\n"
                "from knot.authoring.tools import tool\n\n\n"
                "@tool\n"
                "async def slow_tool(x: str) -> str:\n"
                '    """Slow."""\n'
                "    await asyncio.sleep(0.3)\n"
                "    return 'ok'\n"
            ),
        },
    )
    provider = FakeProvider([tool_call("slow_tool", {"x": "y"}), reply("done")])
    app = make_app(tmp_path, [], provider=provider)

    async with live_server(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]

        async with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"text": "go"}
        ) as resp:
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                if "tool_execution_start" in buf:
                    break

            steer = await client.post(
                f"/sessions/{session_id}/steer", json={"message": "also check the total"}
            )
            assert steer.status_code == 202
            assert steer.json() == {"delivered": True}

        for _ in range(50):
            got = await client.get(f"/sessions/{session_id}")
            if got.json()["state"] == "idle":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("run never completed")

    # The steered message reached the *next* turn's provider request...
    assert len(provider.calls) == 2
    second_call_messages = provider.calls[1][2]
    assert any(
        isinstance(m, UserMessage) and m.text == "also check the total"
        for m in second_call_messages
    )

    # ...and is durably logged as an ordinary "message" entry, not a
    # bespoke steering entry type (design D5: no new entry type).
    state = state_of(app)
    message_entries = [e for e in state.store.entries(session_id) if e.type == ENTRY_TYPE_MESSAGE]
    assert any(
        e.payload.get("role") == "user" and e.payload.get("content") == "also check the total"
        for e in message_entries
    )


async def test_steer_idle_session_is_409_and_writes_nothing(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [reply("hello")])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]

        resp = await client.post(f"/sessions/{session_id}/steer", json={"message": "hi"})
        assert resp.status_code == 409
        assert "/messages" in resp.json()["detail"]

    state = state_of(app)
    assert state.store.entries(session_id) == []


async def test_steer_waiting_session_is_409(tmp_path: Path) -> None:
    """A parked session has no run in progress either — steering only ever
    targets an in-flight turn, not the merely-``waiting`` state that
    ``/messages`` treats as queueable."""
    _gated_fleet(tmp_path)
    app = make_app(tmp_path, [tool_call("sensitive_op", {"amount": 5}), reply("done")])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        await client.post(f"/sessions/{session_id}/messages", json={"text": "move 5"})

        resp = await client.post(f"/sessions/{session_id}/steer", json={"message": "hi"})
        assert resp.status_code == 409


async def test_steer_unknown_session_is_404(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        resp = await client.post("/sessions/sess_nope/steer", json={"message": "hi"})
        assert resp.status_code == 404


async def test_steer_empty_message_is_422(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [reply("hello")])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        resp = await client.post(f"/sessions/{session_id}/steer", json={"message": ""})
        assert resp.status_code == 422


async def test_steer_racing_run_completion_never_silently_drops(tmp_path: Path) -> None:
    """A steer that lands after the run has already finished (registry
    entry popped) 409s rather than vanishing — the endpoint's own
    conflict path (no run in ``state.running``), exercised directly right
    after a run completes rather than through an artificial timing race:
    ``httpx.ASGITransport`` fully drains the stream before returning, so by
    the time this request is made the run is guaranteed to be over, which
    is exactly the "steer after completion" case design D5 documents as
    409-or-delivered, never a silent drop."""
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [reply("hello")])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]

        stream = await client.post(f"/sessions/{session_id}/messages", json={"text": "go"})
        assert stream.status_code == 200
        assert parse_sse(stream.text)[-1][1]["outcome"] == "completed"

        resp = await client.post(f"/sessions/{session_id}/steer", json={"message": "too late"})
        assert resp.status_code == 409

    state = state_of(app)
    assert not any(
        e.payload.get("content") == "too late"
        for e in state.store.entries(session_id)
        if e.type == ENTRY_TYPE_MESSAGE
    )
