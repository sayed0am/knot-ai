"""Unit tests for approval-policy resolution and the ``once`` durability fact."""

from __future__ import annotations

from knot.core.decisions import Allow, Deny, RequireApproval
from knot.core.events import PendingInputRequest
from knot.core.hitl.policies import approved_tools, build_decision_hook
from knot.core.session import (
    ENTRY_TYPE_INPUT_REQUESTED,
    ENTRY_TYPE_INPUT_RESOLVED,
    InputResolution,
    SessionStore,
)
from knot.providers.messages import ToolCall


async def test_never_policy_allows() -> None:
    hook = build_decision_hook({"anything": "never"})
    decision = await hook(ToolCall(id="c1", name="anything", arguments={}), None)
    assert isinstance(decision, Allow)


async def test_always_policy_requires_approval() -> None:
    hook = build_decision_hook({"sensitive_op": "always"})
    decision = await hook(ToolCall(id="c1", name="sensitive_op", arguments={}), None)
    assert isinstance(decision, RequireApproval)


async def test_default_policy_applies_when_no_key_matches() -> None:
    hook = build_decision_hook({}, default="always")
    decision = await hook(ToolCall(id="c1", name="untouched", arguments={}), None)
    assert isinstance(decision, RequireApproval)

    hook_never = build_decision_hook({}, default="never")
    decision_never = await hook_never(ToolCall(id="c1", name="untouched", arguments={}), None)
    assert isinstance(decision_never, Allow)


async def test_suffix_matching_respects_dunder_boundary() -> None:
    hook = build_decision_hook({"delete_customer": "always"})

    matched = await hook(ToolCall(id="c1", name="crm__delete_customer", arguments={}), None)
    assert isinstance(matched, RequireApproval)

    # "nodelete_customer" contains "delete_customer" as a bare substring but
    # has no "__" boundary before it, so the suffix match must reject it.
    unmatched = await hook(ToolCall(id="c2", name="nodelete_customer", arguments={}), None)
    assert isinstance(unmatched, Allow)


async def test_exact_match_wins_over_suffix_match() -> None:
    hook = build_decision_hook({"delete_customer": "always", "crm__delete_customer": "never"})

    decision = await hook(ToolCall(id="c1", name="crm__delete_customer", arguments={}), None)
    assert isinstance(decision, Allow)


async def test_longest_suffix_match_wins_among_candidates() -> None:
    hook = build_decision_hook({"customer": "always", "delete_customer": "never"})

    decision = await hook(ToolCall(id="c1", name="crm__delete_customer", arguments={}), None)
    assert isinstance(decision, Allow)  # the more specific "delete_customer" key wins


async def test_custom_policy_callable_passthrough() -> None:
    async def custom(name: str, arguments, session_id):
        assert name == "custom_tool"
        assert arguments == {"x": 1}
        return Deny(reason="policy says no")

    hook = build_decision_hook({"custom_tool": custom})
    decision = await hook(ToolCall(id="c1", name="custom_tool", arguments={"x": 1}), None)
    assert isinstance(decision, Deny)
    assert decision.reason == "policy says no"


async def test_once_without_store_fails_closed_to_require_approval() -> None:
    hook = build_decision_hook({"repeatable": "once"})
    decision = await hook(ToolCall(id="c1", name="repeatable", arguments={}), None)
    assert isinstance(decision, RequireApproval)


async def test_once_allows_after_durable_approval_in_this_session() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")

        hook = build_decision_hook(
            {"repeatable": "once"}, store=store, session_id=session.session_id
        )
        call = ToolCall(id="c1", name="repeatable", arguments={})

        first = await hook(call, None)
        assert isinstance(first, RequireApproval)

        # Durably record the approval the way resolve_inputs would.
        request = PendingInputRequest(
            id="req_1", kind="tool_approval", tool_call_id="c1", tool_name="repeatable"
        )
        store.append_entry(
            session.session_id, ENTRY_TYPE_INPUT_REQUESTED, request.model_dump(by_alias=True)
        )
        resolution = InputResolution(request_id="req_1", decision="approved", resolved_by="tester")
        store.append_entry(
            session.session_id, ENTRY_TYPE_INPUT_RESOLVED, resolution.model_dump(by_alias=True)
        )

        second = await hook(ToolCall(id="c2", name="repeatable", arguments={}), None)
        assert isinstance(second, Allow)


def test_approved_tools_ignores_denials_and_non_tool_approval_kinds() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")

        # A denied tool_approval never counts.
        denied_request = PendingInputRequest(
            id="req_denied", kind="tool_approval", tool_call_id="c1", tool_name="denied_tool"
        )
        store.append_entry(
            session.session_id,
            ENTRY_TYPE_INPUT_REQUESTED,
            denied_request.model_dump(by_alias=True),
        )
        store.append_entry(
            session.session_id,
            ENTRY_TYPE_INPUT_RESOLVED,
            InputResolution(
                request_id="req_denied", decision="denied", resolved_by="tester"
            ).model_dump(by_alias=True),
        )

        # An approved question is not a tool approval and never counts.
        question_request = PendingInputRequest(
            id="req_question", kind="question", tool_call_id="c2", tool_name="ask_user"
        )
        store.append_entry(
            session.session_id,
            ENTRY_TYPE_INPUT_REQUESTED,
            question_request.model_dump(by_alias=True),
        )
        store.append_entry(
            session.session_id,
            ENTRY_TYPE_INPUT_RESOLVED,
            InputResolution(
                request_id="req_question", decision="approved", resolved_by="tester"
            ).model_dump(by_alias=True),
        )

        # An approved tool_approval counts.
        approved_request = PendingInputRequest(
            id="req_approved", kind="tool_approval", tool_call_id="c3", tool_name="good_tool"
        )
        store.append_entry(
            session.session_id,
            ENTRY_TYPE_INPUT_REQUESTED,
            approved_request.model_dump(by_alias=True),
        )
        store.append_entry(
            session.session_id,
            ENTRY_TYPE_INPUT_RESOLVED,
            InputResolution(
                request_id="req_approved", decision="approved", resolved_by="tester"
            ).model_dump(by_alias=True),
        )

        assert approved_tools(store, session.session_id) == {"good_tool"}
