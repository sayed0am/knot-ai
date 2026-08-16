"""Pure provider/tool agent loop.

An async generator over the fixed event grammar in ``knot.core.events``. Three
things distinguish this from a plain tool-call loop, all in service of
durable human-in-the-loop "park by persist":

1. Every tool call is first passed through a ``ToolDecisionHook`` (allow /
   deny / require approval) before it may execute.
2. A run ends in one of four outcomes — ``completed``, ``error``,
   ``aborted``, ``waiting_input`` — the last of which can happen mid-turn,
   once results for every non-parked call in the current assistant message
   have been emitted.
3. A tool with no executor (``execute_fn is None``) parks instead of
   producing a "tool not found" error; execution itself is a standalone unit
   (``knot.core.tools.execute_tool``) usable outside of a live run.

Within one assistant message, allowed tool calls with an executor run
concurrently as asyncio tasks; their events are merged onto the generator's
output as they arrive, while their resulting ``ToolResultMessage``s are
appended to history in the calls' original order once every runnable call
has finished, so history stays deterministic even though execution overlaps.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass

from knot.core.decisions import Allow, Deny, RequireApproval, ToolDecision, ToolDecisionHook
from knot.core.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    PendingInputRequest,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from knot.core.invariant import HistoryDivergenceError
from knot.core.tool_history import provider_context
from knot.core.tools import AgentTool, AgentToolResult, ToolParkedError, execute_tool
from knot.core.truncation import SpillSink
from knot.providers.events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
)
from knot.providers.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
)
from knot.providers.provider import CancellationToken, ModelProvider
from knot.providers.types import JSONValue

_TOOL_EVENT_POLL_SECONDS = 0.02

#: The concurrent tool phase's merge queue, visible only while that phase is
#: actually running (see ``_run_concurrent_tools``). A tool executor running
#: as one of the phase's ``asyncio.Task``s (or anything it itself spawns —
#: ``asyncio.create_task`` copies the current context) can reach it through
#: ``emit_loop_event`` to fold a control-plane event onto the run's own
#: output stream, interleaved with that call's own
#: ``ToolExecutionStart``/``ToolExecutionEnd`` events. Unset outside a tool
#: phase (e.g. a bare call from a test, or a delegation executor invoked
#: directly rather than through the loop), in which case ``emit_loop_event``
#: is a harmless no-op.
_current_event_queue: ContextVar[asyncio.Queue[AgentEvent | _ParkedSignal] | None] = ContextVar(
    "_current_event_queue", default=None
)


def emit_loop_event(event: AgentEvent) -> bool:
    """Fold ``event`` onto the currently-running tool phase's event stream.

    Returns whether there was a channel to emit onto — ``False`` is a
    harmless no-op, not an error, so callers (e.g.
    ``knot.authoring.runtime``'s delegation executor) never need to know
    whether they're actually running inside a live loop.
    """
    queue = _current_event_queue.get()
    if queue is None:
        return False
    queue.put_nowait(event)
    return True


async def run_agent_loop(
    *,
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    prompts: Sequence[AgentMessage] = (),
    prelude_messages: Sequence[AgentMessage] = (),
    max_turns: int | None = None,
    signal: CancellationToken | None = None,
    session_id: str | None = None,
    get_steering_messages: Callable[[], Sequence[AgentMessage]] | None = None,
    get_follow_up_messages: Callable[[], Sequence[AgentMessage]] | None = None,
    tool_decision_hook: ToolDecisionHook | None = None,
    max_result_bytes: int | None = None,
    spill_sink: SpillSink | None = None,
    default_question_ttl_seconds: int | None = None,
    pre_request_hook: Callable[[Sequence[AgentMessage]], Awaitable[None]] | None = None,
    pre_turn_hook: Callable[[list[AgentMessage]], Awaitable[Sequence[AgentEvent]]] | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the provider/tool loop, emitting the core agent event grammar.

    ``spill_sink`` threads alongside ``max_result_bytes`` down to
    ``execute_tool`` for every executed call (see
    ``knot.core.truncation.bound_tool_result``); ``None`` (the default)
    preserves today's plain-truncation behavior exactly.

    ``default_question_ttl_seconds`` sets ``PendingInputRequest.ttl_seconds``
    for execute-less (question) parks, e.g. ``ask_user``; ``None`` (the
    default) means such requests never expire on their own. A
    ``tool_approval`` park's ttl instead comes from the decision hook's
    ``RequireApproval(ttl_seconds=...)``, per call.

    ``pre_request_hook``, when given, is awaited exactly once immediately
    before each provider request, with the exact ``messages`` list that
    request is about to be built from (``knot.core.invariant`` installs the
    model-visible-logged invariant check here). A raised
    ``HistoryDivergenceError`` is treated like any other fatal turn error:
    it ends the run through the same error-outcome path, before any
    provider call is made — no new outcome kind.

    ``pre_turn_hook``, when given, is awaited exactly once at the start of
    every turn, right where ``TurnStartEvent`` is emitted — before
    ``pre_request_hook``, before this turn's steering messages are drained,
    and before the ``max_turns`` check. It receives the loop's own live
    ``messages`` list (not a copy) and may mutate it in place; this is how
    context compaction (``knot.authoring.runtime``) rewrites history
    between turns — the loop itself stays pure and knows nothing about why.
    Any events the hook returns are yielded immediately, ahead of that
    turn's own events. Unlike tool-phase control events (see
    ``emit_loop_event``), a pre-turn hook runs outside the tool phase's
    queue context, so it cannot use ``emit_loop_event``; returning its
    events directly is the only channel it has.
    """
    new_messages = list(prompts)
    if prompts:
        messages.extend(prompts)

    yield AgentStartEvent()
    yield TurnStartEvent()
    async for event in _run_pre_turn_hook(pre_turn_hook, messages):
        yield event
    for message in prelude_messages:
        yield MessageStartEvent(message=message)
        yield MessageEndEvent(message=message)
    for prompt in prompts:
        yield MessageStartEvent(message=prompt)
        yield MessageEndEvent(message=prompt)

    if max_turns is not None and max_turns < 1:
        async for event in _end_with_error(
            model, "max_turns must be at least 1", messages, new_messages
        ):
            yield event
        return

    tool_by_name = {tool.name: tool for tool in tools}
    turn = 1
    first_turn = True
    pending = tuple(get_steering_messages() if get_steering_messages else ())

    while True:
        has_more_tools = True
        while has_more_tools or pending:
            if not first_turn:
                yield TurnStartEvent()
                async for event in _run_pre_turn_hook(pre_turn_hook, messages):
                    yield event
            first_turn = False

            for message in pending:
                messages.append(message)
                new_messages.append(message)
                yield MessageStartEvent(message=message)
                yield MessageEndEvent(message=message)
            pending = ()

            if signal is not None and signal.is_cancelled():
                yield AgentEndEvent(outcome="aborted", messages=new_messages, pending_requests=[])
                return

            if max_turns is not None and turn > max_turns:
                async for event in _end_with_error(
                    model, f"Agent stopped after max_turns={max_turns}", messages, new_messages
                ):
                    yield event
                return

            if pre_request_hook is not None:
                try:
                    await pre_request_hook(messages)
                except HistoryDivergenceError as exc:
                    async for event in _end_with_error(model, str(exc), messages, new_messages):
                        yield event
                    return

            # Python async generators cannot pass a yielding callback through a
            # normal await cleanly, so consume the assistant sub-generator and
            # retain its final message through the terminal event.
            assistant = None
            async for event in _assistant_events(
                provider=provider,
                model=model,
                system=system,
                messages=provider_context(messages),
                tools=tools,
                signal=signal,
                session_id=session_id,
            ):
                yield event
                if isinstance(event, MessageEndEvent) and isinstance(
                    event.message, AssistantMessage
                ):
                    assistant = event.message

            if assistant is None:  # defensive: _assistant_events always terminates
                if signal is not None and signal.is_cancelled():
                    assistant = _stopped_message(model, "aborted", "Operation aborted")
                else:
                    assistant = _stopped_message(
                        model, "error", "Provider produced no assistant message"
                    )
                yield MessageStartEvent(message=assistant)
                yield MessageEndEvent(message=assistant)

            messages.append(assistant)
            new_messages.append(assistant)
            if assistant.stop_reason in {"error", "aborted"}:
                yield TurnEndEvent(message=assistant)
                yield AgentEndEvent(
                    outcome=assistant.stop_reason, messages=new_messages, pending_requests=[]
                )
                return

            calls = list(assistant.tool_calls)
            has_more_tools = bool(calls)
            tool_results: list[ToolResultMessage] = []
            pending_requests: list[PendingInputRequest] = []

            if calls:
                async for event in _run_tool_phase(
                    calls,
                    tool_by_name,
                    signal,
                    tool_decision_hook,
                    max_result_bytes,
                    spill_sink,
                    tool_results,
                    pending_requests,
                    default_question_ttl_seconds,
                ):
                    yield event
                for result in tool_results:
                    messages.append(result)
                    new_messages.append(result)

                if signal is not None and signal.is_cancelled():
                    yield TurnEndEvent(message=assistant, tool_results=tool_results)
                    yield AgentEndEvent(
                        outcome="aborted", messages=new_messages, pending_requests=[]
                    )
                    return

                if pending_requests:
                    yield TurnEndEvent(message=assistant, tool_results=tool_results)
                    yield AgentEndEvent(
                        outcome="waiting_input",
                        messages=new_messages,
                        pending_requests=pending_requests,
                    )
                    return

            yield TurnEndEvent(message=assistant, tool_results=tool_results)
            turn += 1
            pending = tuple(get_steering_messages() if get_steering_messages else ())

        follow_ups = tuple(get_follow_up_messages() if get_follow_up_messages else ())
        if follow_ups:
            pending = follow_ups
            continue
        break

    yield AgentEndEvent(outcome="completed", messages=new_messages, pending_requests=[])


async def _run_pre_turn_hook(
    pre_turn_hook: Callable[[list[AgentMessage]], Awaitable[Sequence[AgentEvent]]] | None,
    messages: list[AgentMessage],
) -> AsyncIterator[AgentEvent]:
    """Await ``pre_turn_hook`` (a no-op when ``None``) and yield its events.

    A tiny wrapper rather than an inline ``if`` at each of the two
    ``TurnStartEvent`` call sites (see ``run_agent_loop``'s docstring for
    why there are two), so both sites stay identical one-liners.
    """
    if pre_turn_hook is None:
        return
    for event in await pre_turn_hook(messages):
        yield event


async def _assistant_events(
    *,
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    signal: CancellationToken | None,
    session_id: str | None,
) -> AsyncIterator[AgentEvent]:
    source: AsyncIterator[AssistantMessageEvent] = provider.stream_response(
        model=model,
        system=system,
        messages=messages,
        tools=tools,
        signal=signal,
        session_id=session_id,
    )
    started = False
    async for event in source:
        if isinstance(event, AssistantStartEvent):
            started = True
            yield MessageStartEvent(message=event.partial)
        elif isinstance(event, AssistantDoneEvent):
            if not started:
                yield MessageStartEvent(message=event.message)
            yield MessageEndEvent(message=event.message)
        elif isinstance(event, AssistantErrorEvent):
            if not started:
                yield MessageStartEvent(message=event.error)
            yield MessageEndEvent(message=event.error)
        else:
            yield MessageUpdateEvent(message=event.partial, assistant_message_event=event)


async def _run_tool_phase(
    calls: Sequence[ToolCall],
    tool_by_name: Mapping[str, AgentTool],
    signal: CancellationToken | None,
    tool_decision_hook: ToolDecisionHook | None,
    max_result_bytes: int | None,
    spill_sink: SpillSink | None,
    tool_results: list[ToolResultMessage],
    pending_requests: list[PendingInputRequest],
    default_question_ttl_seconds: int | None = None,
) -> AsyncIterator[AgentEvent]:
    """Evaluate the decision hook for every call, then run allowed calls.

    Denied and unknown-tool calls are resolved immediately and in order.
    Allowed calls with an executor run concurrently. Gated (``RequireApproval``)
    and execute-less calls are parked: no result is synthesized for them.
    """
    runnable: list[tuple[ToolCall, AgentTool]] = []
    results_by_id: dict[str, ToolResultMessage] = {}

    for call in calls:
        tool = tool_by_name.get(call.name)
        decision: ToolDecision = (
            await tool_decision_hook(call, tool) if tool_decision_hook is not None else Allow()
        )
        if isinstance(decision, Deny):
            message = _synthetic_result(call, decision.reason)
            results_by_id[call.id] = message
            async for event in _emit_immediate_result(call, message):
                yield event
        elif isinstance(decision, RequireApproval):
            pending_requests.append(_approval_request(call, decision.payload, decision.ttl_seconds))
        elif tool is None:
            message = _synthetic_result(call, f"Tool {call.name} not found")
            results_by_id[call.id] = message
            async for event in _emit_immediate_result(call, message):
                yield event
        elif tool.execute_fn is None:
            pending_requests.append(_question_request(call, default_question_ttl_seconds))
        else:
            runnable.append((call, tool))

    async for event in _run_concurrent_tools(
        runnable, signal, max_result_bytes, spill_sink, results_by_id, pending_requests
    ):
        yield event

    for call in calls:
        result = results_by_id.get(call.id)
        if result is not None:
            tool_results.append(result)


async def _emit_immediate_result(
    call: ToolCall, message: ToolResultMessage
) -> AsyncIterator[AgentEvent]:
    yield ToolExecutionStartEvent(tool_call_id=call.id, tool_name=call.name, args=call.arguments)
    result = AgentToolResult(content=message.content, details=message.details)
    yield ToolExecutionEndEvent(
        tool_call_id=call.id, tool_name=call.name, result=result, is_error=message.is_error
    )
    yield MessageStartEvent(message=message)
    yield MessageEndEvent(message=message)


@dataclass(frozen=True, slots=True)
class _ParkedSignal:
    """Internal-only queue item: a concurrently-run tool call that parked
    mid execution by raising ``ToolParkedError``. Never yielded as an
    ``AgentEvent`` — ``_run_concurrent_tools`` consumes it directly into
    ``pending_out``, joining the same park path as a gated or execute-less
    call.
    """

    request: PendingInputRequest


def _drain_item(
    item: AgentEvent | _ParkedSignal,
    results_out: dict[str, ToolResultMessage],
    pending_out: list[PendingInputRequest],
) -> bool:
    """Record one queue item's effect; return whether it counts toward completion."""
    if isinstance(item, _ParkedSignal):
        pending_out.append(item.request)
        return True
    if isinstance(item, MessageEndEvent) and isinstance(item.message, ToolResultMessage):
        results_out[item.message.tool_call_id] = item.message
        return True
    return False


async def _run_concurrent_tools(
    runnable: list[tuple[ToolCall, AgentTool]],
    signal: CancellationToken | None,
    max_result_bytes: int | None,
    spill_sink: SpillSink | None,
    results_out: dict[str, ToolResultMessage],
    pending_out: list[PendingInputRequest],
) -> AsyncIterator[AgentEvent]:
    if not runnable:
        return

    queue: asyncio.Queue[AgentEvent | _ParkedSignal] = asyncio.Queue()
    # Set before launching any task: asyncio.create_task snapshots the
    # current context, so every task started below (and anything it in turn
    # spawns with create_task) sees this queue through emit_loop_event, even
    # several async-call-frames deep (e.g. a delegation tool's child run).
    token = _current_event_queue.set(queue)
    try:
        tasks = [
            asyncio.create_task(
                _execute_and_report(call, tool, signal, queue, max_result_bytes, spill_sink)
            )
            for call, tool in runnable
        ]
        target = len(tasks)
        received = 0
        try:
            while received < target:
                if signal is not None and signal.is_cancelled():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_TOOL_EVENT_POLL_SECONDS)
                except TimeoutError:
                    continue
                if _drain_item(item, results_out, pending_out):
                    received += 1
                if not isinstance(item, _ParkedSignal):
                    yield item
            while not queue.empty():
                item = queue.get_nowait()
                _drain_item(item, results_out, pending_out)
                if not isinstance(item, _ParkedSignal):
                    yield item
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task
    finally:
        _current_event_queue.reset(token)


async def _execute_and_report(
    call: ToolCall,
    tool: AgentTool,
    signal: CancellationToken | None,
    queue: asyncio.Queue[AgentEvent | _ParkedSignal],
    max_result_bytes: int | None,
    spill_sink: SpillSink | None,
) -> None:
    await queue.put(
        ToolExecutionStartEvent(tool_call_id=call.id, tool_name=call.name, args=call.arguments)
    )

    def on_update(partial: AgentToolResult) -> None:
        queue.put_nowait(
            ToolExecutionUpdateEvent(
                tool_call_id=call.id,
                tool_name=call.name,
                args=call.arguments,
                partial_result=partial.model_copy(deep=True),
            )
        )

    try:
        result, is_error = await execute_tool(
            tool,
            call,
            signal,
            on_update,
            max_result_bytes=max_result_bytes,
            spill_sink=spill_sink,
        )
    except ToolParkedError as exc:
        await queue.put(_ParkedSignal(request=exc.request))
        return

    await queue.put(
        ToolExecutionEndEvent(
            tool_call_id=call.id, tool_name=call.name, result=result, is_error=is_error
        )
    )
    message = ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=result.content,
        details=result.details,
        is_error=is_error,
    )
    await queue.put(MessageStartEvent(message=message))
    await queue.put(MessageEndEvent(message=message))


def _synthetic_result(call: ToolCall, text: str) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=[TextContent(text=text)],
        is_error=True,
    )


def _approval_request(
    call: ToolCall, payload: dict[str, JSONValue] | None, ttl_seconds: int | None
) -> PendingInputRequest:
    return PendingInputRequest(
        id=f"req_{uuid.uuid4().hex}",
        kind="tool_approval",
        tool_call_id=call.id,
        tool_name=call.name,
        payload=payload if payload is not None else {"args": dict(call.arguments)},
        ttl_seconds=ttl_seconds,
    )


def _question_request(call: ToolCall, ttl_seconds: int | None) -> PendingInputRequest:
    return PendingInputRequest(
        id=f"req_{uuid.uuid4().hex}",
        kind="question",
        tool_call_id=call.id,
        tool_name=call.name,
        payload={"args": dict(call.arguments)},
        ttl_seconds=ttl_seconds,
    )


async def _end_with_error(
    model: str,
    text: str,
    messages: list[AgentMessage],
    new_messages: list[AgentMessage],
) -> AsyncIterator[AgentEvent]:
    error_message = _stopped_message(model, "error", text)
    messages.append(error_message)
    new_messages.append(error_message)
    yield MessageStartEvent(message=error_message)
    yield MessageEndEvent(message=error_message)
    yield TurnEndEvent(message=error_message)
    yield AgentEndEvent(outcome="error", messages=new_messages, pending_requests=[])


def _stopped_message(model: str, stop_reason: str, text: str) -> AssistantMessage:
    return AssistantMessage(
        model=model,
        content=[],
        stop_reason=stop_reason,  # type: ignore[arg-type]
        error_message=text,
    )


__all__ = ["emit_loop_event", "run_agent_loop"]
