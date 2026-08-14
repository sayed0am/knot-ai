"""SSE wire format, client-disconnect survival, and the root-session "no
chain event" case.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from authoring_fixtures import write_files
from server_fixtures import client_for, live_server, make_app, parse_sse

from knot.core.events import AgentStartEvent
from knot.providers.fake import reply, tool_call


async def test_sse_frames_are_camel_case_and_discriminated(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [reply("hello there")])

    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        resp = await client.post(f"/sessions/{session_id}/messages", json={"text": "hi"})
        assert resp.status_code == 200

        events = parse_sse(resp.text)
        assert events[0] == ("agent_start", {"type": "agent_start"})


async def test_sse_data_is_the_verbatim_pydantic_dump_of_the_event(tmp_path: Path) -> None:
    """Byte-compare one event's ``data:`` payload against
    ``model_dump_json(by_alias=True)`` of the equivalently reconstructed
    pydantic model — proving the wire body is the event grammar itself,
    with no server-side reshaping."""
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [reply("hello there")])

    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        resp = await client.post(f"/sessions/{session_id}/messages", json={"text": "hi"})

        raw_frame = resp.text.split("\n\n")[0]
        data_line = next(line for line in raw_frame.splitlines() if line.startswith("data: "))
        raw_data_json = data_line.removeprefix("data: ")

        reconstructed = AgentStartEvent()
        assert raw_data_json == reconstructed.model_dump_json(by_alias=True)
        assert json.loads(raw_data_json) == {"type": "agent_start"}


async def test_chain_event_is_omitted_for_a_root_session(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    app = make_app(tmp_path, [reply("hello there")])

    async with client_for(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]
        resp = await client.post(f"/sessions/{session_id}/messages", json={"text": "hi"})
        events = parse_sse(resp.text)
        assert "chain" not in {etype for etype, _ in events}
        assert events[-1][0] == "agent_end"  # agent_end is the true last frame


async def test_disconnect_mid_stream_run_continues_to_completion(tmp_path: Path) -> None:
    """The defining durability property: the run belongs to the session, not
    the socket. A real client disconnect (not achievable through the
    buffering ASGI test transport — see ``live_server``) must not stop the
    background run; persistence must keep writing until the run is done.
    """
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/tools/slow_tool.py": (
                "import asyncio\n"
                "from knot.authoring.tools import tool\n\n\n"
                "@tool\n"
                "async def slow_tool(x: str) -> str:\n"
                '    """A tool that takes a moment to finish."""\n'
                "    await asyncio.sleep(0.4)\n"
                "    return f'done with {x}'\n"
            ),
        },
    )
    app = make_app(tmp_path, [tool_call("slow_tool", {"x": "y"}), reply("final answer")])

    async with live_server(app) as client:
        created = await client.post("/agents/root/sessions")
        session_id = created.json()["sessionId"]

        # Open the stream, read only up to the tool call actually starting,
        # then leave the `async with` block early — httpx tears the
        # connection down, a real client disconnect for the server to see.
        async with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"text": "go"}
        ) as resp:
            assert resp.status_code == 200
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                if "tool_execution_start" in buf:
                    break

        # The run must still be in flight right after disconnecting (proves
        # this test actually disconnected mid-run, not after it finished).
        just_after = await client.get(f"/sessions/{session_id}")
        assert just_after.json()["state"] in ("running", "waiting", "idle")

        for _ in range(100):
            got = await client.get(f"/sessions/{session_id}")
            if got.json()["state"] == "idle":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("run never completed server-side after disconnect")

        body = got.json()
        assert body["state"] == "idle"
        tool_results = [m for m in body["transcript"] if m.get("role") == "toolResult"]
        assert len(tool_results) == 1
        assert "done with y" in tool_results[0]["content"][0]["text"]
        assistant_texts = [
            block["text"]
            for m in body["transcript"]
            if m.get("role") == "assistant"
            for block in m["content"]
            if block.get("type") == "text"
        ]
        assert any("final answer" in t for t in assistant_texts)
