"""Crash-window detection and repair: the gap between execution_started and
a tool-result message, as if the process died mid-execution."""

from __future__ import annotations

from knot.core.hitl.resume import detect_crash_windows, operator_skip, repair_crash_windows
from knot.core.session import SessionStore
from knot.core.tools import AgentTool, AgentToolResult


def _counting_tool(name: str, *, idempotent: bool) -> tuple[AgentTool, list[dict]]:
    calls: list[dict] = []

    async def execute_fn(tool_call_id, arguments, signal=None, on_update=None):
        calls.append(dict(arguments))
        return AgentToolResult(content=f"redone {name}")

    tool = AgentTool(
        name=name, description="", parameters={}, execute_fn=execute_fn, idempotent=idempotent
    )
    return tool, calls


def _simulate_crash(store: SessionStore, session_id: str, *, request_id, tool_call_id, tool_name):
    """Append input_requested + input_resolved(approved) + execution_started
    with no tool-result message — exactly what a crash between steps 2 and 3
    of the approve path in ``knot.core.hitl.resume`` leaves behind."""
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


async def test_crash_windows_are_detected_and_repaired_by_idempotence() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")

        idempotent_tool, idempotent_calls = _counting_tool("safe_retry", idempotent=True)
        risky_tool, risky_calls = _counting_tool("risky_write", idempotent=False)

        _simulate_crash(
            store,
            session.session_id,
            request_id="req_idempotent",
            tool_call_id="call_idempotent",
            tool_name="safe_retry",
        )
        _simulate_crash(
            store,
            session.session_id,
            request_id="req_risky",
            tool_call_id="call_risky",
            tool_name="risky_write",
        )

        tools = {"safe_retry": idempotent_tool, "risky_write": risky_tool}
        windows = detect_crash_windows(store, session.session_id, tools)
        assert {w.request_id for w in windows} == {"req_idempotent", "req_risky"}

        report = await repair_crash_windows(store, session.session_id, tools)

        assert [r.request_id for r in report.repaired] == ["req_idempotent"]
        assert [r.request_id for r in report.needs_operator] == ["req_risky"]
        assert idempotent_calls == [{}]  # re-executed exactly once
        assert risky_calls == []  # never silently re-run

        entries = store.entries(session.session_id)
        message_entries = [e for e in entries if e.type == "message"]
        assert any(e.payload.get("toolCallId") == "call_idempotent" for e in message_entries)
        assert not any(e.payload.get("toolCallId") == "call_risky" for e in message_entries)

        # The idempotent window is now closed; the risky one still needs an operator.
        remaining = detect_crash_windows(store, session.session_id, tools)
        assert [w.request_id for w in remaining] == ["req_risky"]

        operator_skip(
            store,
            session.session_id,
            "call_risky",
            by="ops_operator",
            reason="unsafe to auto-retry a payment",
        )

        entries_after_skip = store.entries(session.session_id)
        skip_message = next(
            e
            for e in entries_after_skip
            if e.type == "message" and e.payload.get("toolCallId") == "call_risky"
        )
        assert skip_message.payload["isError"] is True
        assert "Operator skip by ops_operator" in skip_message.payload["content"][0]["text"]
        assert "unsafe to auto-retry a payment" in skip_message.payload["content"][0]["text"]

        assert detect_crash_windows(store, session.session_id, tools) == []
