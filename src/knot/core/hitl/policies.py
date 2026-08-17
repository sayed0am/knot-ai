"""Approval policies: how a tool name maps to a ``ToolDecisionHook`` outcome.

Three named policies plus an escape hatch:

- ``"never"``: always ``Allow``.
- ``"always"``: always ``RequireApproval``.
- ``"once"``: ``RequireApproval`` unless the tool was already approved at
  least once *in this session*. That fact is never held in memory — it is
  derived fresh from the durable entry log on every check (see
  ``approved_tools``), which is what makes ``once`` survive a process
  restart with no special-cased recovery path.
- a custom async callable (``CustomPolicy``): its ``ToolDecision`` is
  returned verbatim.

Policy lookup on a qualified tool name (e.g. ``"crm__delete_customer"``) is
exact-key first, then suffix: a policy key matches iff the call name ends
with ``"__" + key``, so ``"delete_customer"`` matches
``"crm__delete_customer"`` but never ``"nodelete_customer"`` (no ``__``
boundary — this is deliberately not a substring match).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

from knot.core.decisions import Allow, RequireApproval, ToolDecision, ToolDecisionHook
from knot.core.session.entries import (
    ENTRY_TYPE_INPUT_REQUESTED,
    ENTRY_TYPE_INPUT_RESOLVED,
    entry_to_request,
    entry_to_resolution,
)
from knot.core.session.store import SessionStore
from knot.core.tools import AgentTool
from knot.providers.messages import ToolCall
from knot.providers.types import JSONValue

NamedPolicy = Literal["never", "once", "always"]

CustomPolicy = Callable[[str, Mapping[str, JSONValue], "str | None"], Awaitable[ToolDecision]]

ApprovalPolicy = NamedPolicy | CustomPolicy


def approved_tools(store: SessionStore, session_id: str) -> set[str]:
    """Tool names durably approved at least once in ``session_id``.

    Computed fresh from the entry log, never cached: an ``input_resolved`` entry with
    decision ``approved`` whose originating ``input_requested`` entry
    (matched by ``request_id``) has kind ``tool_approval`` contributes its
    tool name. This is the sole source of truth ``"once"`` policies read,
    so restart-durability falls out for free.
    """
    requested_by_id = {}
    approved_request_ids = set()
    for entry in store.entries(session_id):
        if entry.type == ENTRY_TYPE_INPUT_REQUESTED:
            request = entry_to_request(entry)
            requested_by_id[request.id] = request
        elif entry.type == ENTRY_TYPE_INPUT_RESOLVED:
            resolution = entry_to_resolution(entry)
            if resolution.decision == "approved":
                approved_request_ids.add(resolution.request_id)

    names: set[str] = set()
    for request_id in approved_request_ids:
        request = requested_by_id.get(request_id)
        if request is not None and request.kind == "tool_approval":
            names.add(request.tool_name)
    return names


def _resolve_policy[T](policies: Mapping[str, T], tool_name: str, default: T) -> T:
    """Exact key first, then longest matching ``__``-qualified suffix key.

    Generic over the mapped value: shared verbatim by ``build_decision_hook``
    for both its ``policies`` lookup and its ``ttls`` lookup (design D7), so
    a tool's configured TTL resolves through the exact same suffix-matching
    rule as its policy.
    """
    if tool_name in policies:
        return policies[tool_name]

    best_key: str | None = None
    for key in policies:
        suffix = f"__{key}"
        if tool_name.endswith(suffix) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is not None:
        return policies[best_key]
    return default


def build_decision_hook(
    policies: Mapping[str, ApprovalPolicy],
    *,
    store: SessionStore | None = None,
    session_id: str | None = None,
    default: ApprovalPolicy = "never",
    ttls: Mapping[str, int] | None = None,
) -> ToolDecisionHook:
    """Build a ``ToolDecisionHook`` that evaluates ``policies`` per call.

    ``store``/``session_id`` are required for ``"once"`` policies to check
    durable approval history; a ``"once"`` policy with either missing falls
    back to requiring approval every time (fail closed, never fail open).
    Call arguments are treated as untrusted: no schema is assumed of them,
    they are only ever passed through to a custom policy verbatim.

    ``ttls`` (design D7) supplies a per-tool TTL, resolved with the same
    exact-then-suffix matching rule as ``policies``; every ``RequireApproval``
    this hook returns for a matched tool carries that TTL (``None`` when
    ``ttls`` is absent or has no matching key), so a configured expiry reaches
    the resulting ``PendingInputRequest`` regardless of which of the three
    named policies produced the park.
    """

    async def hook(call: ToolCall, tool: AgentTool | None) -> ToolDecision:
        policy = _resolve_policy(policies, call.name, default)
        ttl = _resolve_policy(ttls, call.name, None) if ttls is not None else None

        if not isinstance(policy, str):
            return await policy(call.name, call.arguments, session_id)

        if policy == "never":
            return Allow()
        if policy == "always":
            return RequireApproval(ttl_seconds=ttl)
        # policy == "once"
        if store is None or session_id is None:
            return RequireApproval(ttl_seconds=ttl)
        if call.name in approved_tools(store, session_id):
            return Allow()
        return RequireApproval(ttl_seconds=ttl)

    return hook


__all__ = [
    "ApprovalPolicy",
    "CustomPolicy",
    "NamedPolicy",
    "approved_tools",
    "build_decision_hook",
]
