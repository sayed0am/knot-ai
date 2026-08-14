"""Cross-session queries: fleet inbox, per-agent listing, child chains."""

from __future__ import annotations

from knot.core.events import PendingInputRequest
from knot.core.session import (
    InputResolution,
    SessionStore,
    child_chain,
    pending_requests_fleet,
    sessions_for_agent,
)


def _request(request_id: str) -> PendingInputRequest:
    return PendingInputRequest(
        id=request_id, kind="question", tool_call_id=f"call_{request_id}", tool_name="ask"
    )


def test_pending_requests_fleet_attributes_child_request_to_root() -> None:
    with SessionStore(":memory:") as store:
        root = store.create_session("agent_root")
        child = store.create_session(
            "agent_child", parent_session_id=root.session_id, parent_tool_call_id="call_spawn"
        )
        store.append_entry(
            child.session_id, "input_requested", _request("req_1").model_dump(by_alias=True)
        )

        rows = pending_requests_fleet(store)

        assert len(rows) == 1
        row = rows[0]
        assert row.session_id == child.session_id
        assert row.agent_id == "agent_child"
        assert row.root_session_id == root.session_id
        assert [h.session_id for h in row.path] == [root.session_id, child.session_id]
        assert [h.agent_id for h in row.path] == ["agent_root", "agent_child"]
        assert row.request.id == "req_1"


def test_pending_requests_fleet_excludes_resolved_requests() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        store.append_entry(
            session.session_id, "input_requested", _request("req_1").model_dump(by_alias=True)
        )
        resolution = InputResolution(request_id="req_1", decision="denied", resolved_by="human")
        store.append_entry(
            session.session_id, "input_resolved", resolution.model_dump(by_alias=True)
        )

        assert pending_requests_fleet(store) == []


def test_pending_requests_fleet_attributes_top_level_session_to_itself() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        store.append_entry(
            session.session_id, "input_requested", _request("req_1").model_dump(by_alias=True)
        )

        rows = pending_requests_fleet(store)
        assert len(rows) == 1
        assert rows[0].root_session_id == session.session_id
        assert [h.session_id for h in rows[0].path] == [session.session_id]


def test_pending_requests_fleet_orders_multiple_requests_across_sessions() -> None:
    with SessionStore(":memory:") as store:
        session_a = store.create_session("agent_a")
        session_b = store.create_session("agent_b")
        store.append_entry(
            session_a.session_id, "input_requested", _request("req_a").model_dump(by_alias=True)
        )
        store.append_entry(
            session_b.session_id, "input_requested", _request("req_b").model_dump(by_alias=True)
        )

        rows = pending_requests_fleet(store)
        assert {row.session_id for row in rows} == {session_a.session_id, session_b.session_id}


def _child_session_request(request_id: str) -> PendingInputRequest:
    return PendingInputRequest(
        id=request_id, kind="child_session", tool_call_id=f"call_{request_id}", tool_name="sub"
    )


def test_pending_requests_fleet_kinds_filter_excludes_child_session_rows() -> None:
    with SessionStore(":memory:") as store:
        root = store.create_session("agent_root")
        child = store.create_session(
            "agent_child", parent_session_id=root.session_id, parent_tool_call_id="call_spawn"
        )
        # The parent's own child-session park signal (internal linkage, not
        # something a human ever acts on) plus the child's own real approval
        # request (what an approvals inbox should actually show).
        store.append_entry(
            root.session_id, "input_requested", _child_session_request("req_link").model_dump(
                by_alias=True
            )
        )
        store.append_entry(
            child.session_id, "input_requested", _request("req_approval").model_dump(by_alias=True)
        )

        unfiltered = pending_requests_fleet(store)
        assert {row.request.id for row in unfiltered} == {"req_link", "req_approval"}

        filtered = pending_requests_fleet(store, kinds=("tool_approval", "question"))
        assert len(filtered) == 1
        assert filtered[0].request.id == "req_approval"
        assert filtered[0].session_id == child.session_id
        assert filtered[0].root_session_id == root.session_id
        assert [h.session_id for h in filtered[0].path] == [root.session_id, child.session_id]


def test_pending_requests_fleet_kinds_none_returns_every_kind() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        store.append_entry(
            session.session_id, "input_requested", _request("req_1").model_dump(by_alias=True)
        )
        store.append_entry(
            session.session_id,
            "input_requested",
            _child_session_request("req_2").model_dump(by_alias=True),
        )

        assert {row.request.id for row in pending_requests_fleet(store)} == {"req_1", "req_2"}
        assert {row.request.id for row in pending_requests_fleet(store, kinds=None)} == {
            "req_1",
            "req_2",
        }


def test_sessions_for_agent_orders_newest_first() -> None:
    with SessionStore(":memory:") as store:
        first = store.create_session("agent_a", session_id="sess_first")
        second = store.create_session("agent_a", session_id="sess_second")
        store.create_session("agent_b", session_id="sess_other")

        records = sessions_for_agent(store, "agent_a")

        assert [r.session_id for r in records] == [second.session_id, first.session_id]
        assert all(r.agent_id == "agent_a" for r in records)


def test_sessions_for_agent_empty_for_unknown_agent() -> None:
    with SessionStore(":memory:") as store:
        store.create_session("agent_a")
        assert sessions_for_agent(store, "agent_missing") == []


def test_child_chain_returns_descendants_with_parent_linkage() -> None:
    with SessionStore(":memory:") as store:
        root = store.create_session("agent_root")
        child = store.create_session(
            "agent_child", parent_session_id=root.session_id, parent_tool_call_id="call_1"
        )
        grandchild = store.create_session(
            "agent_grandchild", parent_session_id=child.session_id, parent_tool_call_id="call_2"
        )

        rows = child_chain(store, root.session_id)

        assert [r.session.session_id for r in rows] == [child.session_id, grandchild.session_id]
        assert rows[0].depth == 1
        assert rows[0].session.parent_tool_call_id == "call_1"
        assert rows[1].depth == 2
        assert rows[1].session.parent_session_id == child.session_id
        assert rows[1].session.parent_tool_call_id == "call_2"


def test_child_chain_empty_for_leaf_session() -> None:
    with SessionStore(":memory:") as store:
        leaf = store.create_session("agent_a")
        assert child_chain(store, leaf.session_id) == []
