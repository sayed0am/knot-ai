"""``GET /approvals``: the fleet-wide pending-request inbox."""

from __future__ import annotations

from pathlib import Path

from authoring_fixtures import write_files
from server_fixtures import client_for, make_app, parse_sse

from knot.providers.fake import reply, tool_call


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
                '    """Needs sign-off."""\n'
                "    return f'moved {amount}'\n"
            ),
        },
    )


async def test_approvals_status_query_only_supports_pending(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        resp = await client.get("/approvals?status=resolved")
        assert resp.status_code == 422


async def test_approvals_empty_when_nothing_pending(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        resp = await client.get("/approvals?status=pending")
        assert resp.status_code == 200
        assert resp.json() == []


async def test_approvals_lists_a_question_park_too(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "you are root\n"})
    app = make_app(tmp_path, [tool_call("ask_user", {"question": "which account?"})])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        stream = await client.post(f"/sessions/{session_id}/messages", json={"text": "go"})
        assert parse_sse(stream.text)[-1][1]["outcome"] == "waiting_input"

        resp = await client.get("/approvals?status=pending")
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["kind"] == "question"
        assert rows[0]["toolName"] == "ask_user"
        assert rows[0]["payload"]["args"]["question"] == "which account?"


async def test_approvals_never_lists_a_child_session_park(tmp_path: Path) -> None:
    """``child_session`` is an internal linkage record, not a human decision —
    never surfaced in the approvals inbox (see
    ``knot.core.session.queries.pending_requests_fleet``)."""
    # researcher gates its own tool so the child parks, which parks the root
    # too on a child_session request.
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/subagents/researcher/instructions.md": "you research\n",
            "agents/root/subagents/researcher/agent.yaml": (
                "description: Digs up facts.\napprovals:\n  dangerous_lookup: always\n"
            ),
            "agents/root/subagents/researcher/tools/dangerous_lookup.py": (
                "from knot.authoring.tools import tool\n\n\n"
                "@tool\n"
                "def dangerous_lookup(query: str) -> str:\n"
                '    """Needs a human\'s sign-off."""\n'
                "    return f'classified: {query}'\n"
            ),
        },
    )
    app = make_app(
        tmp_path,
        [
            tool_call("researcher", {"message": "x"}),
            tool_call("dangerous_lookup", {"query": "x"}),
        ],
    )
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        stream = await client.post(f"/sessions/{session_id}/messages", json={"text": "research x"})
        assert parse_sse(stream.text)[-1][1]["outcome"] == "waiting_input"

        rows = (await client.get("/approvals?status=pending")).json()
        assert {row["kind"] for row in rows} == {"tool_approval"}
        assert len(rows) == 1
        # Attributed up to the root session, with the full path.
        assert rows[0]["rootSessionId"] == session_id
        assert rows[0]["sessionId"] != session_id
        assert [hop["agentId"] for hop in rows[0]["path"]] == ["root", "researcher"]


async def test_approvals_resolves_and_disappears_from_the_inbox(tmp_path: Path) -> None:
    _gated_fleet(tmp_path)
    app = make_app(tmp_path, [tool_call("sensitive_op", {"amount": 1}), reply("done")])
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        stream = await client.post(f"/sessions/{session_id}/messages", json={"text": "go"})
        request_id = parse_sse(stream.text)[-1][1]["pendingRequests"][0]["id"]

        assert len((await client.get("/approvals?status=pending")).json()) == 1

        await client.post(
            f"/sessions/{session_id}/input",
            json={"responses": {request_id: {"action": "approve", "by": "ops"}}},
        )
        assert (await client.get("/approvals?status=pending")).json() == []
