"""Resuming a parked session: resolving pending requests, and crash repair.

A parked session holds nothing in memory — everything needed to resume it
lives in the durable entry log (see ``knot.core.session``). This module is
the sole write path for that resume while the session is parked (the
harness/``PersistenceSubscriber`` are not running), and it is built around
one guarantee for the approve path: three ordered durable records —

    1. ``input_resolved``  (the human decision)
    2. ``execution_started`` (the tool is about to run)
    3. ``message``           (the tool result)

written in that order, each a separate ``store.append_entry`` call, so a
crash between any two of them is unambiguous: ``detect_crash_windows`` finds
exactly the requests that reached step 2 but never reached step 3.

Requests park for different *reasons* (``PendingInputRequest.kind``), and
each reason accepts a different set of structured responses. That dispatch
is a small registry (``_PARK_HANDLERS``) keyed by kind, which is what let
the third park cause — ``"child_session"`` (see
``knot.authoring.runtime``) — land as one more registry entry rather than a
rewrite of ``resolve_inputs``. Unlike the other two, ``"child_session"``
never accepts a direct response through this module at all: it resolves
only via ``write_child_completion``, called from the child session's own
completion, not a human decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from knot.core.events import PendingInputRequest
from knot.core.session.entries import (
    ENTRY_TYPE_EXECUTION_STARTED,
    ENTRY_TYPE_INPUT_REQUESTED,
    ENTRY_TYPE_INPUT_RESOLVED,
    ENTRY_TYPE_MESSAGE,
    ExecutionStarted,
    InputResolution,
    entry_to_execution_started,
    entry_to_message,
    entry_to_request,
    entry_to_resolution,
)
from knot.core.session.state import DerivedState, derive_state, rehydrate
from knot.core.session.store import Entry, SessionStore
from knot.core.tools import AgentTool, execute_tool
from knot.providers.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    current_timestamp_ms,
)

# ---------------------------------------------------------------------------
# Structured responses, keyed by request id when calling resolve_inputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApproveResponse:
    """Approve a ``tool_approval`` request: the tool will actually execute."""

    resolved_by: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DenyResponse:
    """Deny any pending request. Denial is information, not an error path:
    the turn continues with an error tool result carrying ``reason``."""

    resolved_by: str
    reason: str


@dataclass(frozen=True, slots=True)
class AnswerResponse:
    """Answer a ``question`` request. Nothing executes; ``text`` becomes the
    tool result the model reads on resume."""

    resolved_by: str
    text: str


Response = ApproveResponse | DenyResponse | AnswerResponse


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    """The result of one ``resolve_inputs`` call."""

    resolved: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    invalidated: list[str] = field(default_factory=list)
    ready_to_continue: bool = False


@dataclass(frozen=True, slots=True)
class CrashWindow:
    """An approved tool call that started executing but never produced a result."""

    request_id: str
    tool_call_id: str
    tool_name: str
    approved_at: int
    started_at: int


@dataclass(frozen=True, slots=True)
class CrashWindowReport:
    """The disposition of one crash window after ``repair_crash_windows``."""

    request_id: str
    tool_call_id: str
    tool_name: str
    needs_operator: bool


@dataclass(frozen=True, slots=True)
class RepairReport:
    """Every crash window found, split by whether it could be auto-repaired."""

    repaired: list[CrashWindowReport] = field(default_factory=list)
    needs_operator: list[CrashWindowReport] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Durable writers. Every write to the store while parked goes through one of
# these — see the module docstring for why the approve path's three calls
# must happen in this order and never be combined into one transaction.
# ---------------------------------------------------------------------------


def _write_resolution(
    store: SessionStore,
    session_id: str,
    *,
    request_id: str,
    decision: Literal["approved", "denied"],
    resolved_by: str,
    reason: str | None,
) -> None:
    resolution = InputResolution(
        request_id=request_id, decision=decision, resolved_by=resolved_by, reason=reason
    )
    store.append_entry(session_id, ENTRY_TYPE_INPUT_RESOLVED, resolution.model_dump(by_alias=True))


def _write_execution_started(
    store: SessionStore, session_id: str, request: PendingInputRequest
) -> None:
    started = ExecutionStarted(
        request_id=request.id,
        tool_call_id=request.tool_call_id,
        tool_name=request.tool_name,
        started_at=current_timestamp_ms(),
    )
    store.append_entry(session_id, ENTRY_TYPE_EXECUTION_STARTED, started.model_dump(by_alias=True))


def _write_message(store: SessionStore, session_id: str, message: AgentMessage) -> None:
    store.append_entry(session_id, ENTRY_TYPE_MESSAGE, message.model_dump(by_alias=True))


def _error_result(tool_call_id: str, tool_name: str, text: str) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        content=[TextContent(text=text)],
        is_error=True,
    )


def write_child_completion(
    store: SessionStore,
    session_id: str,
    request: PendingInputRequest,
    *,
    resolved_by: str,
    result_message: ToolResultMessage,
) -> None:
    """Resolve a parked ``child_session`` request once its child session
    reaches a durable terminal state.

    Writes exactly two ordered records: ``input_resolved`` (decision
    ``approved`` — a completed child is not a denial, even when the child's
    own run itself failed, in which case ``result_message.is_error`` carries
    that fact) then the tool-result ``message`` entry carrying the child's
    final answer or its failure description. This is the ONLY way a
    ``child_session`` request is ever resolved — never through
    ``resolve_inputs`` (see the ``"child_session"`` entry in
    ``_PARK_HANDLERS``, which always rejects a direct response to one).
    Called by ``knot.authoring.runtime.AgentRuntime.on_child_complete``.
    """
    _write_resolution(
        store,
        session_id,
        request_id=request.id,
        decision="approved",
        resolved_by=resolved_by,
        reason=None,
    )
    _write_message(store, session_id, result_message)


def _write_denial(
    store: SessionStore,
    session_id: str,
    request: PendingInputRequest,
    *,
    resolved_by: str,
    reason: str,
) -> None:
    """Denial: ``input_resolved`` (denied) then an error tool-result message.

    Used uniformly for an operator's ``DenyResponse``, an expired request,
    and a revalidation failure — denial is always exactly these two records.
    """
    _write_resolution(
        store,
        session_id,
        request_id=request.id,
        decision="denied",
        resolved_by=resolved_by,
        reason=reason,
    )
    _write_message(
        store,
        session_id,
        _error_result(request.tool_call_id, request.tool_name, f"User denied: {reason}"),
    )


def _expiry_reason(request: PendingInputRequest, now: int) -> str:
    return (
        f"request expired: ttl_seconds={request.ttl_seconds} "
        f"created_at={request.created_at} now={now}"
    )


def _sweep_expired(
    store: SessionStore, session_id: str, pending: Sequence[PendingInputRequest], now: int
) -> list[PendingInputRequest]:
    expired: list[PendingInputRequest] = []
    for request in pending:
        expires_at = (
            None if request.ttl_seconds is None else request.created_at + request.ttl_seconds * 1000
        )
        if expires_at is not None and expires_at <= now:
            _write_denial(
                store,
                session_id,
                request,
                resolved_by="system:expiry",
                reason=_expiry_reason(request, now),
            )
            expired.append(request)
    return expired


# ---------------------------------------------------------------------------
# Reading durable state needed to resolve requests.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RequestIndex:
    """Every request ever raised, and which request ids are already resolved.

    Distinct from ``DerivedState``: that only exposes the currently-pending
    subset, but ``resolve_inputs`` also needs to tell "never requested" apart
    from "requested and already resolved" for a response's rejection reason.
    """

    requested: dict[str, PendingInputRequest]
    resolved_ids: set[str]


def _load_request_index(entries: Sequence[Entry]) -> _RequestIndex:
    requested: dict[str, PendingInputRequest] = {}
    resolved_ids: set[str] = set()
    for entry in entries:
        if entry.type == ENTRY_TYPE_INPUT_REQUESTED:
            request = entry_to_request(entry)
            requested[request.id] = request
        elif entry.type == ENTRY_TYPE_INPUT_RESOLVED:
            resolution = entry_to_resolution(entry)
            resolved_ids.add(resolution.request_id)
    return _RequestIndex(requested=requested, resolved_ids=resolved_ids)


def _find_tool_call(messages: Sequence[AgentMessage], tool_call_id: str) -> ToolCall | None:
    """Recover the model-authored ``ToolCall`` (with its real arguments) for a
    pending request from durable history, rather than from the request's
    ``payload`` — ``payload`` may be a curated display summary a decision
    hook supplied via ``RequireApproval(payload=...)``, not the literal
    arguments to execute with.
    """
    for message in messages:
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                if call.id == tool_call_id:
                    return call
    return None


def _resolve_call(request: PendingInputRequest, messages: Sequence[AgentMessage]) -> ToolCall:
    call = _find_tool_call(messages, request.tool_call_id)
    if call is not None:
        return call
    # Fallback for requests with no backing assistant message (e.g. seeded
    # directly in a test): reconstruct from the default request payload shape.
    args = request.payload.get("args") if isinstance(request.payload.get("args"), dict) else {}
    return ToolCall(id=request.tool_call_id, name=request.tool_name, arguments=dict(args or {}))


# ---------------------------------------------------------------------------
# Park-cause registry: one handler per PendingInputRequest.kind. Adding a
# park cause later means adding one entry here and one Response type, never
# touching resolve_inputs's control flow.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Disposition:
    status: Literal["resolved", "invalidated", "rejected"]
    reason: str | None = None


class _ParkHandler(Protocol):
    async def __call__(
        self,
        *,
        store: SessionStore,
        session_id: str,
        request: PendingInputRequest,
        response: Response,
        tools: Mapping[str, AgentTool],
        messages: Sequence[AgentMessage],
        max_result_bytes: int | None,
    ) -> _Disposition: ...


async def _resolve_tool_approval(
    *,
    store: SessionStore,
    session_id: str,
    request: PendingInputRequest,
    response: Response,
    tools: Mapping[str, AgentTool],
    messages: Sequence[AgentMessage],
    max_result_bytes: int | None,
) -> _Disposition:
    if isinstance(response, DenyResponse):
        _write_denial(
            store, session_id, request, resolved_by=response.resolved_by, reason=response.reason
        )
        return _Disposition(status="resolved")

    if not isinstance(response, ApproveResponse):
        return _Disposition(status="rejected", reason="invalid_response_type")

    # Revalidation: the tool must still exist and be executable in the
    # CURRENT manifest's tools. A tool removed/renamed since the request was
    # raised is auto-denied, and the approval is reported invalidated, not
    # resolved or silently applied.
    tool = tools.get(request.tool_name)
    if tool is None or tool.execute_fn is None:
        reason = f"tool {request.tool_name!r} is no longer available for execution"
        _write_denial(store, session_id, request, resolved_by="system:revalidation", reason=reason)
        return _Disposition(status="invalidated")

    # Three ordered records: approval -> execution start -> result.
    _write_resolution(
        store,
        session_id,
        request_id=request.id,
        decision="approved",
        resolved_by=response.resolved_by,
        reason=response.reason,
    )
    _write_execution_started(store, session_id, request)

    call = _resolve_call(request, messages)
    result, is_error = await execute_tool(tool, call, max_result_bytes=max_result_bytes)
    _write_message(
        store,
        session_id,
        ToolResultMessage(
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            content=result.content,
            details=result.details,
            is_error=is_error,
        ),
    )
    return _Disposition(status="resolved")


async def _resolve_question(
    *,
    store: SessionStore,
    session_id: str,
    request: PendingInputRequest,
    response: Response,
    tools: Mapping[str, AgentTool],
    messages: Sequence[AgentMessage],
    max_result_bytes: int | None,
) -> _Disposition:
    if isinstance(response, DenyResponse):
        _write_denial(
            store, session_id, request, resolved_by=response.resolved_by, reason=response.reason
        )
        return _Disposition(status="resolved")

    if not isinstance(response, AnswerResponse):
        return _Disposition(status="rejected", reason="invalid_response_type")

    # Answer path: approval + tool-result carrying the answer text. Nothing
    # executes (there is no tool to run) — no execution_started is written,
    # asymmetric with the tool_approval approve path on purpose.
    _write_resolution(
        store,
        session_id,
        request_id=request.id,
        decision="approved",
        resolved_by=response.resolved_by,
        reason=None,
    )
    _write_message(
        store,
        session_id,
        ToolResultMessage(
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            content=[TextContent(text=response.text)],
            is_error=False,
        ),
    )
    return _Disposition(status="resolved")


async def _resolve_child_session(
    *,
    store: SessionStore,
    session_id: str,
    request: PendingInputRequest,
    response: Response,
    tools: Mapping[str, AgentTool],
    messages: Sequence[AgentMessage],
    max_result_bytes: int | None,
) -> _Disposition:
    """A ``child_session`` park never accepts a direct human response: it
    resolves only through the child session's own eventual completion (see
    ``write_child_completion``). Any response targeting one here is rejected."""
    return _Disposition(status="rejected", reason="child_session")


_PARK_HANDLERS: dict[str, _ParkHandler] = {
    "tool_approval": _resolve_tool_approval,
    "question": _resolve_question,
    "child_session": _resolve_child_session,
}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def resolve_inputs(
    store: SessionStore,
    session_id: str,
    responses: Mapping[str, Response],
    *,
    tools: Mapping[str, AgentTool],
    now_ms: int | None = None,
    max_result_bytes: int | None = None,
) -> ResolveOutcome:
    """Resolve a batch of pending requests against their structured responses.

    Resolving one of several pending requests writes its records
    immediately and independently of the others; the caller learns whether
    the run is ready to continue via ``ResolveOutcome.ready_to_continue``
    (also available standalone as ``is_ready_to_continue``).
    """
    now = now_ms if now_ms is not None else current_timestamp_ms()
    entries = store.entries(session_id)
    index = _load_request_index(entries)
    derived = derive_state(entries)

    # Lazy expiry first, over every currently pending request, independent
    # of what this call's responses target.
    just_expired = _sweep_expired(store, session_id, derived.pending_requests, now)
    just_expired_ids = {request.id for request in just_expired}

    resolved: list[str] = []
    rejected: list[tuple[str, str]] = []
    invalidated: list[str] = []

    for request_id, response in responses.items():
        request = index.requested.get(request_id)
        if request is None:
            rejected.append((request_id, "unknown"))
            continue
        if request_id in just_expired_ids:
            rejected.append((request_id, "expired"))
            continue
        if request_id in index.resolved_ids:
            rejected.append((request_id, "already_resolved"))
            continue

        handler = _PARK_HANDLERS.get(request.kind)
        if handler is None:
            rejected.append((request_id, f"unsupported_kind:{request.kind}"))
            continue

        disposition = await handler(
            store=store,
            session_id=session_id,
            request=request,
            response=response,
            tools=tools,
            messages=derived.messages,
            max_result_bytes=max_result_bytes,
        )
        if disposition.status == "resolved":
            resolved.append(request_id)
        elif disposition.status == "invalidated":
            invalidated.append(request_id)
        else:
            rejected.append((request_id, disposition.reason or "rejected"))

    return ResolveOutcome(
        resolved=resolved,
        rejected=rejected,
        expired=[request.id for request in just_expired],
        invalidated=invalidated,
        ready_to_continue=is_ready_to_continue(store, session_id),
    )


def expire_pending(store: SessionStore, session_id: str, now_ms: int) -> list[PendingInputRequest]:
    """Auto-deny every currently pending request past its ``ttl_seconds``.

    Standalone lazy-expiry entry point for read paths (see ``current_state``);
    ``resolve_inputs`` performs the equivalent sweep itself.
    """
    derived = rehydrate(store, session_id)
    return _sweep_expired(store, session_id, derived.pending_requests, now_ms)


def current_state(store: SessionStore, session_id: str, now_ms: int | None = None) -> DerivedState:
    """``expire_pending`` then ``rehydrate`` — what a read path should call."""
    now = now_ms if now_ms is not None else current_timestamp_ms()
    expire_pending(store, session_id, now)
    return rehydrate(store, session_id)


def _last_tool_calls_resolved(messages: Sequence[AgentMessage]) -> bool:
    result_ids = {
        message.tool_call_id for message in messages if isinstance(message, ToolResultMessage)
    }
    last_with_calls: AssistantMessage | None = None
    for message in messages:
        if isinstance(message, AssistantMessage) and message.tool_calls:
            last_with_calls = message
    if last_with_calls is None:
        return True
    return all(call.id in result_ids for call in last_with_calls.tool_calls)


def is_ready_to_continue(store: SessionStore, session_id: str) -> bool:
    """True iff no pending requests remain and every tool call in the parked
    assistant message has a result entry — the exact condition under which
    re-entering the loop (``AgentHarness.continue_()``) can make progress.
    """
    derived = rehydrate(store, session_id)
    if derived.pending_requests:
        return False
    return _last_tool_calls_resolved(derived.messages)


# ---------------------------------------------------------------------------
# Crash-window detection and repair (6.4)
# ---------------------------------------------------------------------------


def detect_crash_windows(
    store: SessionStore, session_id: str, tools: Mapping[str, AgentTool]
) -> list[CrashWindow]:
    """Find every approved tool call that started but never finished.

    An ``input_resolved(approved)`` for a ``tool_approval`` followed by an
    ``execution_started`` with no matching tool-result ``message`` entry is
    exactly the window a crash between steps 2 and 3 of the approve path
    would leave behind.
    """
    entries = store.entries(session_id)
    requests: dict[str, PendingInputRequest] = {}
    approvals: dict[str, InputResolution] = {}
    starts: dict[str, ExecutionStarted] = {}
    result_ids: set[str] = set()

    for entry in entries:
        if entry.type == ENTRY_TYPE_INPUT_REQUESTED:
            request = entry_to_request(entry)
            requests[request.id] = request
        elif entry.type == ENTRY_TYPE_INPUT_RESOLVED:
            resolution = entry_to_resolution(entry)
            if resolution.decision == "approved":
                approvals[resolution.request_id] = resolution
        elif entry.type == ENTRY_TYPE_EXECUTION_STARTED:
            started = entry_to_execution_started(entry)
            starts[started.request_id] = started
        elif entry.type == ENTRY_TYPE_MESSAGE:
            message = entry_to_message(entry)
            if isinstance(message, ToolResultMessage):
                result_ids.add(message.tool_call_id)

    windows: list[CrashWindow] = []
    for request_id, resolution in approvals.items():
        started = starts.get(request_id)
        if started is None or started.tool_call_id in result_ids:
            continue
        request = requests.get(request_id)
        if request is not None and request.kind != "tool_approval":
            continue
        windows.append(
            CrashWindow(
                request_id=request_id,
                tool_call_id=started.tool_call_id,
                tool_name=started.tool_name,
                approved_at=resolution.resolved_at,
                started_at=started.started_at,
            )
        )
    return windows


async def repair_crash_windows(
    store: SessionStore, session_id: str, tools: Mapping[str, AgentTool]
) -> RepairReport:
    """Repair every crash window: idempotent tools re-execute automatically;
    non-idempotent tools are reported (``needs_operator``) and never
    silently re-run — that decision belongs to ``operator_skip`` (or a
    future manual re-approval), never to this function.
    """
    windows = detect_crash_windows(store, session_id, tools)
    messages = rehydrate(store, session_id).messages

    repaired: list[CrashWindowReport] = []
    needs_operator: list[CrashWindowReport] = []

    for window in windows:
        tool = tools.get(window.tool_name)
        if tool is not None and tool.idempotent and tool.execute_fn is not None:
            call = _find_tool_call(messages, window.tool_call_id) or ToolCall(
                id=window.tool_call_id, name=window.tool_name, arguments={}
            )
            started = ExecutionStarted(
                request_id=window.request_id,
                tool_call_id=window.tool_call_id,
                tool_name=window.tool_name,
                started_at=current_timestamp_ms(),
            )
            store.append_entry(
                session_id, ENTRY_TYPE_EXECUTION_STARTED, started.model_dump(by_alias=True)
            )
            result, is_error = await execute_tool(tool, call)
            _write_message(
                store,
                session_id,
                ToolResultMessage(
                    tool_call_id=window.tool_call_id,
                    tool_name=window.tool_name,
                    content=result.content,
                    details=result.details,
                    is_error=is_error,
                ),
            )
            repaired.append(
                CrashWindowReport(
                    request_id=window.request_id,
                    tool_call_id=window.tool_call_id,
                    tool_name=window.tool_name,
                    needs_operator=False,
                )
            )
        else:
            needs_operator.append(
                CrashWindowReport(
                    request_id=window.request_id,
                    tool_call_id=window.tool_call_id,
                    tool_name=window.tool_name,
                    needs_operator=True,
                )
            )

    return RepairReport(repaired=repaired, needs_operator=needs_operator)


def operator_skip(
    store: SessionStore, session_id: str, tool_call_id: str, *, by: str, reason: str
) -> None:
    """Manually resolve a non-idempotent crash window: append an error
    tool-result entry documenting the operator's decision not to re-run it.
    Does not touch ``input_resolved`` — the original approval stands; this
    only supplies the missing tool-result record.
    """
    tool_name = "unknown"
    for entry in store.entries(session_id):
        if entry.type == ENTRY_TYPE_EXECUTION_STARTED:
            started = entry_to_execution_started(entry)
            if started.tool_call_id == tool_call_id:
                tool_name = started.tool_name
    _write_message(
        store,
        session_id,
        _error_result(tool_call_id, tool_name, f"Operator skip by {by}: {reason}"),
    )


__all__ = [
    "AnswerResponse",
    "ApproveResponse",
    "CrashWindow",
    "CrashWindowReport",
    "DenyResponse",
    "RepairReport",
    "ResolveOutcome",
    "current_state",
    "detect_crash_windows",
    "expire_pending",
    "is_ready_to_continue",
    "operator_skip",
    "repair_crash_windows",
    "resolve_inputs",
    "write_child_completion",
]
