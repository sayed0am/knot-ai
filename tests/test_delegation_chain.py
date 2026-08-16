"""The flagship delegation-chain scenario: park deep, restart, resume up.

Root delegates to a child subagent; the child's own tool call is gated
(``always``), so it parks; that leaves the parent parked too, on a
``child_session`` request. Every live object is then dropped and a fresh
``SessionStore`` opened on the same file — a simulated process restart —
before the fleet inbox, ``resolve_inputs``, and ``AgentRuntime.resume_chain``
unwind the whole thing back to completion.
"""

from __future__ import annotations

from pathlib import Path

from authoring_fixtures import write_files

from knot.authoring.compile import compile_fleet
from knot.authoring.runtime import AgentRuntime
from knot.core.hitl.resume import ApproveResponse, resolve_inputs
from knot.core.session.entries import ENTRY_TYPE_EXECUTION_STARTED, ENTRY_TYPE_INPUT_RESOLVED
from knot.core.session.queries import pending_requests_fleet
from knot.core.session.state import derive_state
from knot.core.session.store import SessionStore
from knot.providers.fake import FakeProvider, reply
from knot.providers.messages import ToolCall


def _write_fleet(tmp_path: Path) -> None:
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


async def test_delegation_chain_parks_restarts_and_resumes_end_to_end(tmp_path: Path) -> None:
    _write_fleet(tmp_path)
    fleet = compile_fleet(tmp_path)
    assert fleet.agents["root"].ok is True, fleet.agents["root"].diagnostics
    researcher = fleet.agents["root"].subagents["researcher"]
    assert researcher.ok is True, researcher.diagnostics

    db_path = tmp_path / "sessions.db"

    # --- Phase 1: root delegates, the child's gated call parks the child,
    # which parks the root too. All in one process. ---
    store = SessionStore(db_path)
    provider = FakeProvider(
        [
            reply(
                tool_calls=[
                    ToolCall(id="call_delegate", name="researcher", arguments={"message": "x"})
                ]
            ),
            reply(
                tool_calls=[
                    ToolCall(id="call_lookup", name="dangerous_lookup", arguments={"query": "x"})
                ]
            ),
        ]
    )
    runtime = AgentRuntime(fleet=fleet, store=store, provider=provider, invariant_mode="strict")
    root_session = runtime.create_session("root")

    events = [
        event async for event in runtime.run_turn(root_session.session_id, "please research x")
    ]
    root_end = events[-1]
    assert root_end.outcome == "waiting_input"
    assert len(root_end.pending_requests) == 1
    child_park = root_end.pending_requests[0]
    assert child_park.kind == "child_session"
    assert child_park.tool_call_id == "call_delegate"
    assert child_park.tool_name == "researcher"
    child_session_id = child_park.payload["childSessionId"]

    # The child itself is durably waiting on its own gated call.
    child_derived_mid = derive_state(store.entries(child_session_id))
    assert child_derived_mid.status == "waiting"
    assert len(child_derived_mid.pending_requests) == 1
    approval_request = child_derived_mid.pending_requests[0]
    assert approval_request.kind == "tool_approval"
    assert approval_request.tool_name == "dangerous_lookup"

    store.close()
    # Drop every live object from phase 1 (process-restart simulation).
    del store, runtime, provider, events

    # --- Phase 2: totally fresh process state, same file. ---
    store2 = SessionStore(db_path)

    # The approvals inbox (filtered, as a real inbox would query it) shows
    # exactly the child's own approval request, attributed to the root
    # session with the full path — never the root's own `child_session` row.
    inbox = pending_requests_fleet(store2, kinds=("tool_approval", "question"))
    assert len(inbox) == 1
    inbox_row = inbox[0]
    assert inbox_row.session_id == child_session_id
    assert inbox_row.request.id == approval_request.id
    assert inbox_row.root_session_id == root_session.session_id
    assert [hop.session_id for hop in inbox_row.path] == [
        root_session.session_id,
        child_session_id,
    ]
    assert [hop.agent_id for hop in inbox_row.path] == ["root", "researcher"]

    # The unfiltered view also surfaces the root's own child_session park.
    unfiltered = pending_requests_fleet(store2)
    assert {row.request.kind for row in unfiltered} == {"tool_approval", "child_session"}

    # Approve the child's gated call directly against the child's own store
    # and tools — three ordered durable records land in the CHILD's log.
    outcome = await resolve_inputs(
        store2,
        child_session_id,
        {approval_request.id: ApproveResponse(resolved_by="ops_operator")},
        tools=researcher.tools,
    )
    assert outcome.resolved == [approval_request.id]
    assert outcome.ready_to_continue is True

    child_entries = store2.entries(child_session_id)
    resolved_entry = next(e for e in child_entries if e.type == ENTRY_TYPE_INPUT_RESOLVED)
    started_entry = next(e for e in child_entries if e.type == ENTRY_TYPE_EXECUTION_STARTED)
    result_entry = next(
        e
        for e in child_entries
        if e.type == "message" and e.payload.get("toolCallId") == "call_lookup"
    )
    assert resolved_entry.seq < started_entry.seq < result_entry.seq

    # --- Resume the whole chain from the leaf. ---
    provider2 = FakeProvider(
        [
            reply("the classified result for x is now in hand"),
            reply("root: delegation complete, x has been researched"),
        ]
    )
    runtime2 = AgentRuntime(fleet=fleet, store=store2, provider=provider2, invariant_mode="strict")
    outcomes = await runtime2.resume_chain(child_session_id)

    assert [o.outcome for o in outcomes] == ["completed", "completed"]
    child_outcome, root_outcome = outcomes
    assert child_outcome.messages[-1].text == "the classified result for x is now in hand"
    assert root_outcome.messages[-1].text == "root: delegation complete, x has been researched"

    # The parent's final transcript contains the child's answer.
    root_state = derive_state(store2.entries(root_session.session_id))
    assert root_state.status == "idle"
    root_texts = [m.text for m in root_state.messages if hasattr(m, "text")]
    assert any("classified result for x" in t for t in root_texts)
    assert root_state.messages[-1].text == "root: delegation complete, x has been researched"

    # Both sessions individually replay to idle from their own logs alone.
    child_state = derive_state(store2.entries(child_session_id))
    assert child_state.status == "idle"
    assert child_state.messages[-1].text == "the classified result for x is now in hand"

    store2.close()
