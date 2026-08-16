"""End-to-end scenarios over the HTTP API, using the real ``examples/fleet``.

Where ``test_server_delegation.py`` and ``test_delegation_chain.py`` are the
flagship, minimal-fixture tests for delegation mechanics, these four tests
exercise the same machinery through the committed example fleet (support +
its crm bundle + its researcher subagent) end to end, as a frontend
integration test would: create a session, drive it purely over HTTP with a
``FakeProvider`` standing in for the model, and assert on the wire shapes a
real client would see. Each test is named after, and documents, one of the
four named scenarios in the work package.
"""

from __future__ import annotations

from pathlib import Path

from server_fixtures import client_for, make_app, parse_sse

from knot.authoring.compile import compile_fleet
from knot.core.session.state import derive_state
from knot.core.session.store import SessionStore
from knot.providers.fake import FakeProvider, reply, tool_call
from knot.providers.messages import ToolResultMessage
from knot.server.app import create_app

_FLEET_ROOT = Path(__file__).resolve().parent.parent / "examples" / "fleet"


def _pending_request_id(events: list[tuple[str, dict]]) -> str:
    end_type, end_data = events[-1]
    assert end_type == "agent_end"
    assert end_data["outcome"] == "waiting_input"
    return end_data["pendingRequests"][0]["id"]


# ---------------------------------------------------------------------------
# Scenario 1: plain chat turn
# ---------------------------------------------------------------------------


async def test_scenario_plain_chat_turn() -> None:
    """Create a session, post one message, stream it to completion over SSE,
    and read the finished transcript back over GET — no tools, no parking."""
    app = make_app(
        _FLEET_ROOT,
        [reply("Order ORD-1001 shipped via UPS and should arrive 2026-08-16.")],
    )

    async with client_for(app) as client:
        created = await client.post("/agents/support/sessions")
        assert created.status_code == 201
        session_id = created.json()["sessionId"]
        assert created.json()["state"] == "idle"

        stream = await client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "Where is order ORD-1001?"},
        )
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(stream.text)

        assert events[0][0] == "agent_start"
        end_type, end_data = events[-1]
        assert end_type == "agent_end"
        assert end_data["outcome"] == "completed"
        assert end_data["pendingRequests"] == []

        got = await client.get(f"/sessions/{session_id}")
        assert got.status_code == 200
        body = got.json()
        assert body["state"] == "idle"
        roles = [m["role"] for m in body["transcript"]]
        assert roles == ["user", "assistant"]
        assistant_text = body["transcript"][-1]["content"][0]["text"]
        assert "ORD-1001" in assistant_text


# ---------------------------------------------------------------------------
# Scenario 2: park/approve round-trip, then a "once" tool runs ungated
# ---------------------------------------------------------------------------


async def test_scenario_park_approve_round_trip_then_once_tool_runs_ungated() -> None:
    """An 'always'-gated tool (update_customer) parks, shows up in the
    approvals inbox, and completes after being approved via /input +
    /continue. Then a 'once'-gated tool (flag_account) parks on its first
    call in the session but runs straight through, ungated, on its second."""
    scripts = [
        tool_call(
            "update_customer",
            {"customer_id": "CUST-1", "field": "tier", "value": "gold"},
            id="call_uc",
        ),
        reply("I've applied the tier update for CUST-1."),
        tool_call(
            "flag_account",
            {"customer_id": "CUST-1", "reason": "suspicious activity"},
            id="call_flag1",
        ),
        reply("Flagged CUST-1 for review."),
        tool_call(
            "flag_account",
            {"customer_id": "CUST-1", "reason": "second look"},
            id="call_flag2",
        ),
        reply("Flagged CUST-1 again — no approval needed this time."),
    ]
    app = make_app(_FLEET_ROOT, scripts)

    async with client_for(app) as client:
        session_id = (await client.post("/agents/support/sessions")).json()["sessionId"]

        # -- update_customer: 'always' gated -----------------------------
        stream = await client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "Please bump CUST-1 to the gold tier."},
        )
        events = parse_sse(stream.text)
        request_id = _pending_request_id(events)

        inbox = await client.get("/approvals")
        assert inbox.status_code == 200
        rows = inbox.json()
        assert len(rows) == 1
        assert rows[0]["requestId"] == request_id
        assert rows[0]["kind"] == "tool_approval"
        assert rows[0]["toolName"] == "update_customer"
        assert rows[0]["sessionId"] == session_id

        resolved = await client.post(
            f"/sessions/{session_id}/input",
            json={"responses": {request_id: {"action": "approve", "by": "ops"}}},
        )
        assert resolved.status_code == 200
        assert resolved.json()["readyToContinue"] is True
        assert (await client.get("/approvals")).json() == []

        cont = await client.post(f"/sessions/{session_id}/continue")
        assert cont.status_code == 200
        cont_events = parse_sse(cont.text)
        assert cont_events[-1][1]["outcome"] == "completed"

        # -- flag_account, first call: 'once' gated, parks ----------------
        stream2 = await client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "Flag CUST-1 for suspicious activity."},
        )
        events2 = parse_sse(stream2.text)
        request_id_2 = _pending_request_id(events2)
        assert events2[-1][1]["pendingRequests"][0]["kind"] == "tool_approval"

        resolved2 = await client.post(
            f"/sessions/{session_id}/input",
            json={"responses": {request_id_2: {"action": "approve", "by": "ops"}}},
        )
        assert resolved2.status_code == 200

        cont2 = await client.post(f"/sessions/{session_id}/continue")
        cont2_events = parse_sse(cont2.text)
        assert cont2_events[-1][1]["outcome"] == "completed"

        # -- flag_account, second call in the same session: ungated --------
        stream3 = await client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "Flag CUST-1 again, one more look."},
        )
        assert stream3.status_code == 200
        events3 = parse_sse(stream3.text)
        # No park this time: the whole turn (tool call + result + reply)
        # completes inside this one stream.
        assert events3[-1][1]["outcome"] == "completed"
        assert events3[-1][1]["pendingRequests"] == []
        tool_ends = [data for etype, data in events3 if etype == "tool_execution_end"]
        assert any(t["toolName"] == "flag_account" for t in tool_ends)
        assert (await client.get("/approvals")).json() == []


# ---------------------------------------------------------------------------
# Scenario 3: delegation chain with a gated child tool
# ---------------------------------------------------------------------------


async def test_scenario_delegation_chain_with_child_approval() -> None:
    """support delegates to researcher, whose own search_notes tool is
    gated 'always' in its agent.yaml. The child parks; approving it on the
    CHILD session and continuing the child ends with the chain event; only
    then does continuing the root complete, with the child's answer folded
    into the root's transcript."""
    scripts = [
        tool_call("researcher", {"message": "check notes for ORD-1001"}, id="call_delegate"),
        tool_call("search_notes", {"order_id": "ORD-1001"}, id="call_search"),
        reply("Found a note: customer called 2026-08-10 about a delayed shipment."),
        reply("Per research, the delay was already resolved; nothing further to do."),
    ]
    app = make_app(_FLEET_ROOT, scripts)

    async with client_for(app) as client:
        root_id = (await client.post("/agents/support/sessions")).json()["sessionId"]

        stream = await client.post(
            f"/sessions/{root_id}/messages",
            json={"text": "Before I reply, check notes on ORD-1001."},
        )
        assert stream.status_code == 200
        events = parse_sse(stream.text)

        called = next(data for etype, data in events if etype == "subagent_called")
        assert called["subagentId"] == "researcher"
        child_id = called["childSessionId"]

        end_type, end_data = events[-1]
        assert end_type == "agent_end"
        assert end_data["outcome"] == "waiting_input"
        assert end_data["pendingRequests"][0]["kind"] == "child_session"
        assert end_data["pendingRequests"][0]["payload"]["childSessionId"] == child_id
        assert "chain" not in {etype for etype, _ in events}  # root itself didn't finish

        got_child = await client.get(f"/sessions/{child_id}")
        assert got_child.status_code == 200
        child_body = got_child.json()
        assert child_body["agentId"] == "researcher"
        assert child_body["state"] == "waiting"
        assert child_body["parentSessionId"] == root_id
        child_request = child_body["pendingRequests"][0]
        assert child_request["kind"] == "tool_approval"
        assert child_request["toolName"] == "search_notes"

        # Approve the CHILD session's own gated call, directly against the child.
        approved = await client.post(
            f"/sessions/{child_id}/input",
            json={"responses": {child_request["id"]: {"action": "approve", "by": "ops"}}},
        )
        assert approved.status_code == 200
        assert approved.json()["readyToContinue"] is True

        child_cont = await client.post(f"/sessions/{child_id}/continue")
        assert child_cont.status_code == 200
        child_events = parse_sse(child_cont.text)
        assert child_events[-2][0] == "agent_end"
        assert child_events[-2][1]["outcome"] == "completed"
        assert child_events[-1][0] == "chain"
        assert child_events[-1][1] == {"parentSessionId": root_id, "parentReady": True}

        root_status = await client.get(f"/sessions/{root_id}")
        assert root_status.json()["state"] == "idle"

        root_cont = await client.post(f"/sessions/{root_id}/continue")
        assert root_cont.status_code == 200
        root_events = parse_sse(root_cont.text)
        assert "chain" not in {etype for etype, _ in root_events}  # root has no parent of its own
        assert root_events[-1][1]["outcome"] == "completed"

        root_final = (await client.get(f"/sessions/{root_id}")).json()
        assert root_final["state"] == "idle"
        tool_results = [m for m in root_final["transcript"] if m["role"] == "toolResult"]
        delegation_result = next(m for m in tool_results if m["toolName"] == "researcher")
        assert "delayed shipment" in delegation_result["content"][0]["text"]
        assistant_texts = [
            block["text"]
            for m in root_final["transcript"]
            if m["role"] == "assistant"
            for block in m["content"]
            if block.get("type") == "text"
        ]
        assert any("already resolved" in t for t in assistant_texts)


# ---------------------------------------------------------------------------
# Scenario 4: restart recovery mid-park
# ---------------------------------------------------------------------------


async def test_scenario_restart_recovery_mid_park(tmp_path: Path) -> None:
    """Park a gated call, then simulate a process restart: build a
    completely new app (fresh create_app + fresh SessionStore) on the same
    sqlite file. The new app still shows the session waiting with its
    pending request, and approving + continuing on the new app completes
    the run."""
    db_path = str(tmp_path / "knot.db")
    downgrade_args = {"customer_id": "CUST-9", "field": "tier", "value": "silver"}
    app1 = make_app(
        _FLEET_ROOT,
        [tool_call("update_customer", downgrade_args, id="call_uc")],
        db_path=db_path,
    )

    async with client_for(app1) as client1:
        session_id = (await client1.post("/agents/support/sessions")).json()["sessionId"]
        stream = await client1.post(
            f"/sessions/{session_id}/messages",
            json={"text": "Downgrade CUST-9 to silver."},
        )
        events = parse_sse(stream.text)
        request_id = _pending_request_id(events)

    # Simulated restart: drop the first app's store entirely and build a
    # brand-new app (fresh compile, fresh SessionStore) on the same file.
    app1.state.knot.store.close()
    fleet2 = compile_fleet(_FLEET_ROOT)
    store2 = SessionStore(db_path)
    provider2 = FakeProvider([reply("Downgraded CUST-9 to the silver tier.")])
    app2 = create_app(fleet=fleet2, store=store2, provider=provider2)

    async with client_for(app2) as client2:
        got = await client2.get(f"/sessions/{session_id}")
        assert got.status_code == 200
        body = got.json()
        assert body["state"] == "waiting"
        assert body["pendingRequests"][0]["id"] == request_id
        assert body["pendingRequests"][0]["toolName"] == "update_customer"

        resolved = await client2.post(
            f"/sessions/{session_id}/input",
            json={"responses": {request_id: {"action": "approve", "by": "ops"}}},
        )
        assert resolved.status_code == 200
        assert resolved.json()["readyToContinue"] is True

        cont = await client2.post(f"/sessions/{session_id}/continue")
        assert cont.status_code == 200
        cont_events = parse_sse(cont.text)
        assert cont_events[-1][1]["outcome"] == "completed"

        final = (await client2.get(f"/sessions/{session_id}")).json()
        assert final["state"] == "idle"
        assistant_text = final["transcript"][-1]["content"][0]["text"]
        assert "silver" in assistant_text


# ---------------------------------------------------------------------------
# Scenario 5: oversized tool result spills, is retrieved, parks, and
# survives a restart
# ---------------------------------------------------------------------------


async def test_scenario_oversized_result_spills_is_retrieved_and_survives_restart(
    tmp_path: Path,
) -> None:
    """``lookup_order`` returns a result over the configured
    ``max_result_bytes`` cap, so it is spilled: the streamed result carries
    a bounded preview and a retrieval notice (not the full text), and the
    scripted model immediately calls the framework ``read_tool_output`` tool
    with the notice's ref and gets the full content back paged. The model
    then requests a gated call (``update_customer``), which parks the
    session. The process is then "restarted" (a fresh app + store over the
    same sqlite file), and after the gated call is approved and the run
    continues, the resumed model calls ``read_tool_output`` with the same
    ref again and gets exactly the same content back — proving the spill
    survived the restart.

    Along the way (task 6.2) this also asserts the three bounded surfaces
    for the spilled result: the persisted message entry (read from the
    store directly), the SSE ``tool_execution_end`` payload, and the next
    provider request's message history each carry only the preview text and
    ``{spilled, original_bytes, ref}`` — never the full original text.
    """
    db_path = str(tmp_path / "knot.db")
    ref = "call_lookup"
    downgrade_args = {"customer_id": "CUST-1", "field": "tier", "value": "gold"}
    provider1 = FakeProvider(
        [
            tool_call("lookup_order", {"order_id": "ORD-9001"}, id=ref),
            tool_call("read_tool_output", {"ref": ref}, id="call_read1"),
            tool_call("update_customer", downgrade_args, id="call_uc"),
        ]
    )
    app1 = make_app(
        _FLEET_ROOT,
        [],
        db_path=db_path,
        provider=provider1,
        runtime_kwargs={"max_result_bytes": 200},
    )

    async with client_for(app1) as client1:
        session_id = (await client1.post("/agents/support/sessions")).json()["sessionId"]
        stream = await client1.post(
            f"/sessions/{session_id}/messages",
            json={"text": "Where is order ORD-9001, and please bump CUST-1 to gold."},
        )
        assert stream.status_code == 200
        events = parse_sse(stream.text)

        # -- (a) the streamed tool result carries the preview + notice ----
        tool_ends = {
            data["toolCallId"]: data for etype, data in events if etype == "tool_execution_end"
        }
        lookup_end = tool_ends[ref]
        lookup_text = "".join(
            block["text"]
            for block in lookup_end["result"]["content"]
            if block.get("type") == "text"
        )
        assert len(lookup_text.encode("utf-8")) <= 200
        assert "spilled" in lookup_text
        assert "read_tool_output" in lookup_text
        details = lookup_end["result"]["details"]
        assert details["spilled"] is True
        assert details["ref"] == ref
        assert isinstance(details["original_bytes"], int) and details["original_bytes"] > 200
        assert "full_content" not in details
        assert "GlobalFreight" not in lookup_text  # the full carrier note didn't ride the wire

        # -- (b) the model calls read_tool_output and gets the full content
        read_end = tool_ends["call_read1"]
        read_text = "".join(
            block["text"] for block in read_end["result"]["content"] if block.get("type") == "text"
        )
        assert read_end["isError"] is False
        assert "GlobalFreight" in read_text
        assert f"ref={ref}" in read_text

        # -- (c) the session parks on the gated update_customer call ------
        request_id = _pending_request_id(events)
        assert events[-1][1]["pendingRequests"][0]["toolName"] == "update_customer"

        # -- 6.2: persisted entry, SSE payload, and provider history are
        #         each bounded (preview + ref, never the full text) -------
        persisted = derive_state(SessionStore(db_path).entries(session_id))
        persisted_result = next(
            m
            for m in persisted.messages
            if isinstance(m, ToolResultMessage) and m.tool_call_id == ref
        )
        assert persisted_result.details["spilled"] is True
        assert persisted_result.details["ref"] == ref
        assert len(persisted_result.text.encode("utf-8")) <= 200
        assert "GlobalFreight" not in persisted_result.text

        second_request_messages = provider1.calls[1][2]
        provider_visible_result = next(
            m
            for m in second_request_messages
            if isinstance(m, ToolResultMessage) and m.tool_call_id == ref
        )
        assert provider_visible_result.details["spilled"] is True
        assert provider_visible_result.details["ref"] == ref
        assert len(provider_visible_result.text.encode("utf-8")) <= 200
        assert "GlobalFreight" not in provider_visible_result.text

    # -- (d) simulated restart: fresh app + store over the same db file ---
    app1.state.knot.store.close()
    fleet2 = compile_fleet(_FLEET_ROOT)
    store2 = SessionStore(db_path)
    provider2 = FakeProvider(
        [
            tool_call("read_tool_output", {"ref": ref}, id="call_read2"),
            reply("Order ORD-9001 confirmed and CUST-1 is now gold tier."),
        ]
    )
    app2 = create_app(
        fleet=fleet2,
        store=store2,
        provider=provider2,
        runtime_kwargs={"max_result_bytes": 200, "invariant_mode": "strict"},
    )

    async with client_for(app2) as client2:
        got = await client2.get(f"/sessions/{session_id}")
        assert got.status_code == 200
        assert got.json()["state"] == "waiting"

        resolved = await client2.post(
            f"/sessions/{session_id}/input",
            json={"responses": {request_id: {"action": "approve", "by": "ops"}}},
        )
        assert resolved.status_code == 200
        assert resolved.json()["readyToContinue"] is True

        cont = await client2.post(f"/sessions/{session_id}/continue")
        assert cont.status_code == 200
        cont_events = parse_sse(cont.text)
        assert cont_events[-1][1]["outcome"] == "completed"

        # -- (e) the resumed model retrieves the same ref, gets the same
        #        content back, exactly as before the restart -------------
        resumed_read_end = next(
            data
            for etype, data in cont_events
            if etype == "tool_execution_end" and data["toolCallId"] == "call_read2"
        )
        resumed_read_text = "".join(
            block["text"]
            for block in resumed_read_end["result"]["content"]
            if block.get("type") == "text"
        )
        assert resumed_read_end["isError"] is False
        assert resumed_read_text == read_text

        final = (await client2.get(f"/sessions/{session_id}")).json()
        assert final["state"] == "idle"
