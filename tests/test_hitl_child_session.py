"""``child_session`` park handling in ``knot.core.hitl.resume``.

A ``child_session`` park is unlike ``tool_approval``/``question``: it never
accepts a direct human response. It resolves only via
``write_child_completion``, called by ``knot.authoring.runtime.AgentRuntime``
once the child session it names has actually finished.
"""

from __future__ import annotations

from knot.core.events import PendingInputRequest
from knot.core.hitl.resume import (
    AnswerResponse,
    ApproveResponse,
    is_ready_to_continue,
    resolve_inputs,
    write_child_completion,
)
from knot.core.session.entries import ENTRY_TYPE_INPUT_RESOLVED, ENTRY_TYPE_MESSAGE
from knot.core.session.store import SessionStore
from knot.providers.messages import ToolResultMessage


def _child_session_request(request_id: str = "req_1") -> PendingInputRequest:
    return PendingInputRequest(
        id=request_id,
        kind="child_session",
        tool_call_id="call_delegate",
        tool_name="researcher",
        payload={"childSessionId": "sess_child_1"},
    )


async def test_resolve_inputs_rejects_any_response_to_a_child_session_request() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_root")
        request = _child_session_request()
        store.append_entry(session.session_id, "input_requested", request.model_dump(by_alias=True))

        outcome = await resolve_inputs(
            store,
            session.session_id,
            {request.id: ApproveResponse(resolved_by="tester")},
            tools={},
        )
        assert outcome.resolved == []
        assert outcome.rejected == [(request.id, "child_session")]
        assert outcome.ready_to_continue is False

        # An AnswerResponse (the shape a `question` park would accept) is
        # rejected identically: no response shape is ever accepted here.
        outcome2 = await resolve_inputs(
            store,
            session.session_id,
            {request.id: AnswerResponse(resolved_by="tester", text="42")},
            tools={},
        )
        assert outcome2.rejected == [(request.id, "child_session")]

        # Still pending: nothing was written for it.
        assert is_ready_to_continue(store, session.session_id) is False


async def test_write_child_completion_writes_two_ordered_records() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_root")
        request = _child_session_request()
        store.append_entry(session.session_id, "input_requested", request.model_dump(by_alias=True))

        result_message = ToolResultMessage(
            tool_call_id=request.tool_call_id, tool_name=request.tool_name, content="the answer"
        )
        write_child_completion(
            store,
            session.session_id,
            request,
            resolved_by="system:child:sess_child_1",
            result_message=result_message,
        )

        entries = store.entries(session.session_id)
        resolved_entry = next(e for e in entries if e.type == ENTRY_TYPE_INPUT_RESOLVED)
        message_entry = next(e for e in entries if e.type == ENTRY_TYPE_MESSAGE)
        assert resolved_entry.seq < message_entry.seq
        assert resolved_entry.payload["decision"] == "approved"
        assert resolved_entry.payload["resolvedBy"] == "system:child:sess_child_1"
        assert message_entry.payload["toolCallId"] == "call_delegate"
        assert message_entry.payload["content"][0]["text"] == "the answer"

        assert is_ready_to_continue(store, session.session_id) is True
