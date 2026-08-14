"""Child session access over HTTP: control-plane events on the parent's own
stream, direct access to a child session's endpoints, and the ``chain``
terminal event chaining a resume up from a child to its parent.
"""

from __future__ import annotations

from pathlib import Path

from authoring_fixtures import write_files
from server_fixtures import client_for, make_app, parse_sse

from knot.providers.fake import reply, tool_call


def _delegation_fleet(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are the root agent\n",
            "agents/root/subagents/researcher/instructions.md": "you are the researcher\n",
            "agents/root/subagents/researcher/agent.yaml": (
                "description: Digs up facts.\napprovals:\n  dangerous_lookup: always\n"
            ),
            "agents/root/subagents/researcher/tools/dangerous_lookup.py": (
                "from knot.authoring.tools import tool\n\n\n"
                "@tool\n"
                "def dangerous_lookup(query: str) -> str:\n"
                '    """Look something up that needs a human\'s sign-off."""\n'
                "    return f'classified result for {query}'\n"
            ),
        },
    )


async def test_parent_stream_carries_subagent_called_with_the_child_session_id(
    tmp_path: Path,
) -> None:
    _delegation_fleet(tmp_path)
    app = make_app(
        tmp_path,
        [
            tool_call("researcher", {"message": "x"}, id="call_delegate"),
            tool_call("dangerous_lookup", {"query": "x"}, id="call_lookup"),
        ],
    )

    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        root_session_id = created.json()["sessionId"]

        stream = await client.post(
            f"/sessions/{root_session_id}/messages", json={"text": "please research x"}
        )
        assert stream.status_code == 200
        events = parse_sse(stream.text)

        called = [data for etype, data in events if etype == "subagent_called"]
        assert len(called) == 1
        assert called[0]["toolCallId"] == "call_delegate"
        assert called[0]["subagentId"] == "researcher"
        child_session_id = called[0]["childSessionId"]

        # SubagentCompletedEvent fires for ANY outcome, including the child
        # itself parking — that's exactly what happened here.
        completed = [data for etype, data in events if etype == "subagent_completed"]
        assert len(completed) == 1
        assert completed[0]["childSessionId"] == child_session_id
        assert completed[0]["outcome"] == "waiting_input"
        end_type, end_data = events[-1]
        assert end_type == "agent_end"
        assert end_data["outcome"] == "waiting_input"
        assert end_data["pendingRequests"][0]["kind"] == "child_session"
        assert end_data["pendingRequests"][0]["payload"]["childSessionId"] == child_session_id
        # no chain event: the root's own run parked, it did not complete.
        assert "chain" not in {etype for etype, _ in events}

        # The child session is independently reachable.
        got_child = await client.get(f"/sessions/{child_session_id}")
        assert got_child.status_code == 200
        cbody = got_child.json()
        assert cbody["agentId"] == "researcher"
        assert cbody["state"] == "waiting"
        assert cbody["parentSessionId"] == root_session_id
        assert cbody["pendingRequests"][0]["kind"] == "tool_approval"


async def test_child_continue_ends_with_chain_event_and_root_can_then_continue(
    tmp_path: Path,
) -> None:
    _delegation_fleet(tmp_path)
    app = make_app(
        tmp_path,
        [
            tool_call("researcher", {"message": "x"}, id="call_delegate"),
            tool_call("dangerous_lookup", {"query": "x"}, id="call_lookup"),
            reply("the classified result for x is now in hand"),
            reply("root: delegation complete, x has been researched"),
        ],
    )

    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        root_session_id = created.json()["sessionId"]

        stream = await client.post(
            f"/sessions/{root_session_id}/messages", json={"text": "please research x"}
        )
        events = parse_sse(stream.text)
        called = next(data for etype, data in events if etype == "subagent_called")
        child_session_id = called["childSessionId"]

        got_child = await client.get(f"/sessions/{child_session_id}")
        approval_request_id = got_child.json()["pendingRequests"][0]["id"]

        # Approve the child's own gated call, directly against the child.
        resolved = await client.post(
            f"/sessions/{child_session_id}/input",
            json={"responses": {approval_request_id: {"action": "approve", "by": "ops"}}},
        )
        assert resolved.status_code == 200
        assert resolved.json()["readyToContinue"] is True

        # The child's own /continue streams the child's events and ends
        # with the chain terminal event announcing the parent is now ready.
        child_cont = await client.post(f"/sessions/{child_session_id}/continue")
        assert child_cont.status_code == 200
        child_events = parse_sse(child_cont.text)
        assert child_events[-1][0] == "chain"
        assert child_events[-1][1] == {
            "parentSessionId": root_session_id,
            "parentReady": True,
        }
        # agent_end (completed) is the second-to-last frame, before chain.
        assert child_events[-2][0] == "agent_end"
        assert child_events[-2][1]["outcome"] == "completed"

        child_final = await client.get(f"/sessions/{child_session_id}")
        assert child_final.json()["state"] == "idle"
        assert (
            "the classified result for x is now in hand"
            in child_final.json()["transcript"][-1]["content"][0]["text"]
        )

        # The parent is now ready; the server never auto-ran it — this is a
        # deliberate, separate client call.
        root_status = await client.get(f"/sessions/{root_session_id}")
        assert root_status.json()["state"] == "idle"  # resolved, ready to continue

        root_cont = await client.post(f"/sessions/{root_session_id}/continue")
        assert root_cont.status_code == 200
        root_events = parse_sse(root_cont.text)
        assert "chain" not in {etype for etype, _ in root_events}  # root has no parent
        assert root_events[-1][1]["outcome"] == "completed"

        root_final = await client.get(f"/sessions/{root_session_id}")
        assert root_final.json()["state"] == "idle"
        transcript_texts = [
            block["text"]
            for m in root_final.json()["transcript"]
            if m.get("role") == "assistant"
            for block in m["content"]
            if block.get("type") == "text"
        ]
        assert any("root: delegation complete" in t for t in transcript_texts)


async def test_child_park_and_completed_subagent_completed_outcome(tmp_path: Path) -> None:
    """A synchronous (non-parking) delegation still announces both control
    events on the parent's own stream, with the child's real outcome."""
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/subagents/researcher/instructions.md": "you research\n",
            "agents/root/subagents/researcher/agent.yaml": "description: Digs up facts.\n",
        },
    )
    app = make_app(
        tmp_path,
        [
            tool_call("researcher", {"message": "x"}, id="call_delegate"),
            reply("the answer is 42"),
            reply("root says: 42"),
        ],
    )
    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        stream = await client.post(f"/sessions/{session_id}/messages", json={"text": "research x"})
        events = parse_sse(stream.text)

        called = next(data for etype, data in events if etype == "subagent_called")
        completed = next(data for etype, data in events if etype == "subagent_completed")
        assert called["childSessionId"] == completed["childSessionId"]
        assert completed["outcome"] == "completed"
        assert events.index(("subagent_called", called)) < events.index(
            ("subagent_completed", completed)
        )
        assert events[-1][1]["outcome"] == "completed"
