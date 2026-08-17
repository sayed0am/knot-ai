"""Stateful reusable agent harness built on the core loop.

Wraps ``run_agent_loop`` with the bits a live agent needs that a pure
generator shouldn't own: steering/follow-up queues, cancellation, event fan
out to subscribers, and derived run state. ``state`` is read straight from
the harness's most recent ``AgentEndEvent`` record rather than any ad-hoc
flag, so it always agrees with what the loop actually reported: ``waiting``
means the last run ended with outcome ``waiting_input`` and at least one of
its pending requests hasn't been resolved yet.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Literal

from knot.core.decisions import ToolDecisionHook
from knot.core.events import (
    AgentEndEvent,
    AgentEvent,
    MessageEndEvent,
    MessageStartEvent,
    PendingInputRequest,
)
from knot.core.loop import run_agent_loop
from knot.core.repeat_guard import RepeatGuardSettings
from knot.core.tool_history import INTERRUPTED_TOOL_RESULT
from knot.core.tools import AgentTool
from knot.core.truncation import SpillSink
from knot.providers.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolResultMessage,
    UserMessage,
)
from knot.providers.provider import ModelProvider, SimpleCancellationToken

EventListener = Callable[[AgentEvent], Awaitable[None] | None]
QueueMode = Literal["one_at_a_time", "all"]
HarnessState = Literal["idle", "running", "waiting"]


@dataclass(frozen=True, slots=True)
class QueuedMessages:
    steering: tuple[AgentMessage, ...] = ()
    follow_up: tuple[AgentMessage, ...] = ()

    @property
    def count(self) -> int:
        return len(self.steering) + len(self.follow_up)


@dataclass(slots=True)
class AgentHarnessConfig:
    provider: ModelProvider
    model: str
    system: str
    tools: list[AgentTool] = field(default_factory=list)
    max_turns: int | None = None
    max_tokens: int | None = None
    thinking_budget_tokens: int | None = None
    # Session token budget (design D6): `max_session_tokens` is the
    # configured ceiling (`None` disables the check entirely) and
    # `session_tokens_baseline` is the usage already accumulated by prior
    # runs of this session, summed from the durable entry log at harness
    # build time (see `knot.authoring.runtime._session_token_baseline`).
    max_session_tokens: int | None = None
    session_tokens_baseline: int = 0
    queue_mode: QueueMode = "one_at_a_time"
    session_id: str | None = None
    tool_decision_hook: ToolDecisionHook | None = None
    max_result_bytes: int | None = None
    spill_sink: SpillSink | None = None
    default_question_ttl_seconds: int | None = None
    pre_request_hook: Callable[[Sequence[AgentMessage]], Awaitable[None]] | None = None
    pre_turn_hook: Callable[[list[AgentMessage]], Awaitable[Sequence[AgentEvent]]] | None = None
    repeat_guard: RepeatGuardSettings | None = None


class AgentHarness:
    """Reusable stateful agent brain independent of transport/UI policy."""

    def __init__(
        self,
        config: AgentHarnessConfig,
        *,
        messages: Sequence[AgentMessage] = (),
    ) -> None:
        self._config = config
        self._messages = list(messages)
        self._listeners: list[EventListener] = []
        self._current_signal: SimpleCancellationToken | None = None
        self._running = False
        self._steering_queue: deque[AgentMessage] = deque()
        self._follow_up_queue: deque[AgentMessage] = deque()
        self._pending_requests: tuple[PendingInputRequest, ...] = ()

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        return tuple(self._messages)

    @property
    def config(self) -> AgentHarnessConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def pending_requests(self) -> tuple[PendingInputRequest, ...]:
        return self._pending_requests

    @property
    def state(self) -> HarnessState:
        if self._running:
            return "running"
        if self._pending_requests:
            return "waiting"
        return "idle"

    @property
    def queued_messages(self) -> QueuedMessages:
        return QueuedMessages(tuple(self._steering_queue), tuple(self._follow_up_queue))

    @property
    def pending_message_count(self) -> int:
        return self.queued_messages.count

    def has_queued_messages(self) -> bool:
        return bool(self._steering_queue or self._follow_up_queue)

    def append_message(self, message: AgentMessage) -> None:
        self._messages.append(message)

    def replace_messages(self, messages: Sequence[AgentMessage]) -> None:
        self._messages = list(messages)

    def seed_pending_requests(self, requests: Sequence[PendingInputRequest]) -> None:
        """Seed pending requests without running the loop.

        For rehydrating a harness from durable state (see
        ``knot.core.session.state.harness_from_session``): after construction
        with a session's rehydrated ``messages``, this makes ``state`` and
        ``pending_requests`` immediately agree with what was computed from
        the session's entry log, exactly as if the harness itself had just
        produced that ``AgentEndEvent``. Not meant to be called while running.
        """
        self._pending_requests = tuple(requests)

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def cancel(self) -> None:
        if self._current_signal is not None:
            self._current_signal.cancel()

    def steer(self, content: str) -> QueuedMessages:
        return self.steer_message(UserMessage(content=content))

    def steer_message(self, message: AgentMessage) -> QueuedMessages:
        self._steering_queue.append(message)
        return self.queued_messages

    def follow_up(self, content: str) -> QueuedMessages:
        return self.follow_up_message(UserMessage(content=content))

    def follow_up_message(self, message: AgentMessage) -> QueuedMessages:
        self._follow_up_queue.append(message)
        return self.queued_messages

    def clear_queues(self) -> QueuedMessages:
        snapshot = self.queued_messages
        self._steering_queue.clear()
        self._follow_up_queue.clear()
        return snapshot

    def pop_latest_follow_up(self) -> AgentMessage | None:
        return self._follow_up_queue.pop() if self._follow_up_queue else None

    def pop_latest_steering(self) -> AgentMessage | None:
        return self._steering_queue.pop() if self._steering_queue else None

    def resolve_pending(self, request_id: str) -> PendingInputRequest | None:
        """Remove and return one pending request, or ``None`` if not found.

        Full resume semantics (re-entering the loop carrying the human's
        decision) land in a later work package; for now this only clears
        bookkeeping so ``state`` correctly stops reporting ``waiting`` once
        every pending request has been resolved.
        """
        remaining: list[PendingInputRequest] = []
        resolved: PendingInputRequest | None = None
        for request in self._pending_requests:
            if resolved is None and request.id == request_id:
                resolved = request
                continue
            remaining.append(request)
        self._pending_requests = tuple(remaining)
        return resolved

    def prompt_message(self, message: AgentMessage) -> AsyncIterator[AgentEvent]:
        self._ensure_not_running()
        self._running = True
        return self._run(prompts=(message,))

    def prompt(self, content: str) -> AsyncIterator[AgentEvent]:
        return self.prompt_message(UserMessage(content=content))

    def continue_(self) -> AsyncIterator[AgentEvent]:
        self._ensure_not_running()
        self._running = True
        return self._run()

    async def _run(
        self,
        *,
        prompts: Sequence[AgentMessage] = (),
    ) -> AsyncIterator[AgentEvent]:
        signal = SimpleCancellationToken()
        self._current_signal = signal
        try:
            # Repair dangling tool calls here, not in prompt()/continue_(),
            # so the synthetic results flow through events and reach push
            # subscribers (persistence) as well as the consumer.
            repaired_from = len(self._messages)
            self._append_interrupted_tool_results()
            repairs = self._messages[repaired_from:]
            async for event in run_agent_loop(
                provider=self._config.provider,
                model=self._config.model,
                system=self._config.system,
                messages=self._messages,
                prompts=prompts,
                prelude_messages=repairs,
                tools=self._config.tools,
                max_turns=self._config.max_turns,
                max_tokens=self._config.max_tokens,
                thinking_budget_tokens=self._config.thinking_budget_tokens,
                max_session_tokens=self._config.max_session_tokens,
                session_tokens_baseline=self._config.session_tokens_baseline,
                signal=signal,
                session_id=self._config.session_id,
                get_steering_messages=self._drain_steering_messages,
                get_follow_up_messages=self._drain_follow_up_messages,
                tool_decision_hook=self._config.tool_decision_hook,
                max_result_bytes=self._config.max_result_bytes,
                spill_sink=self._config.spill_sink,
                default_question_ttl_seconds=self._config.default_question_ttl_seconds,
                pre_request_hook=self._config.pre_request_hook,
                pre_turn_hook=self._config.pre_turn_hook,
                repeat_guard=self._config.repeat_guard,
            ):
                if isinstance(event, AgentEndEvent):
                    self._pending_requests = (
                        tuple(event.pending_requests) if event.outcome == "waiting_input" else ()
                    )
                await self._notify(event)
                yield event
        finally:
            if signal.is_cancelled():
                repaired_from = len(self._messages)
                self._append_interrupted_tool_results()
                # The consumer is usually gone here; push the repairs to
                # subscribers. Listener errors are suppressed; cancellation
                # itself is not.
                for message in self._messages[repaired_from:]:
                    with suppress(Exception):
                        await self._notify(MessageStartEvent(message=message))
                        await self._notify(MessageEndEvent(message=message))
            if self._current_signal is signal:
                self._current_signal = None
            self._running = False

    async def _notify(self, event: AgentEvent) -> None:
        for listener in list(self._listeners):
            result = listener(event)
            if isawaitable(result):
                await result

    def _ensure_not_running(self) -> None:
        if self._running:
            raise RuntimeError(
                "AgentHarness is already running; use steer() or follow_up() to queue messages."
            )

    def _drain_steering_messages(self) -> tuple[AgentMessage, ...]:
        return self._drain_queue(self._steering_queue)

    def _drain_follow_up_messages(self) -> tuple[AgentMessage, ...]:
        return self._drain_queue(self._follow_up_queue)

    def _drain_queue(self, queue: deque[AgentMessage]) -> tuple[AgentMessage, ...]:
        if not queue:
            return ()
        if self._config.queue_mode == "all":
            messages = tuple(queue)
            queue.clear()
            return messages
        return (queue.popleft(),)

    def append_interrupted_tool_results(self) -> int:
        before = len(self._messages)
        self._append_interrupted_tool_results()
        return len(self._messages) - before

    def _append_interrupted_tool_results(self) -> None:
        returned_ids = {
            message.tool_call_id
            for message in self._messages
            if isinstance(message, ToolResultMessage)
        }
        for message in tuple(self._messages):
            if not isinstance(message, AssistantMessage):
                continue
            for call in message.tool_calls:
                if call.id in returned_ids:
                    continue
                returned_ids.add(call.id)
                self._messages.append(
                    ToolResultMessage(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        content=[TextContent(text=INTERRUPTED_TOOL_RESULT)],
                        is_error=True,
                    )
                )


__all__ = [
    "AgentHarness",
    "AgentHarnessConfig",
    "EventListener",
    "HarnessState",
    "QueueMode",
    "QueuedMessages",
]
