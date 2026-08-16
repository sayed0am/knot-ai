# Add context compaction

## Why

Knot's headline feature is sessions that live indefinitely — parked for a week, resumed after restarts — but its only context control is `max_result_bytes` truncation of individual tool results (`knot/core/truncation.py`). A session that accumulates enough history will eventually exceed the model's context window and every subsequent run will hard-fail with a provider error, permanently wedging exactly the long-lived durable sessions the framework is built for.

## What Changes

- Introduce a compaction step that, when the conversation approaches the model's context budget, replaces the oldest span of messages with a single model-generated summary message (clearly framed, e.g. wrapped in `<compacted-summary>` tags), while keeping a recent tail of messages verbatim.
- Record compaction durably as a new session entry type. Rehydration (`knot.core.session.state`) applies compaction entries when deriving provider-visible history, so a compacted session survives restart identically to a live one — same guarantee as every other knot durable fact.
- The full pre-compaction entry log is never deleted or rewritten: compaction is an append-only projection change, preserving knot's append-only audit trail.
- Trigger compaction two ways:
  - **Proactive**: a token-pressure estimate checked between turns against a configurable threshold ratio of the model's context window.
  - **Reactive**: a provider context-overflow error triggers compact-and-retry instead of surfacing the error, bounded by a retry cap.
- The summarization request replays the session's own system prompt and message prefix and appends the summarize instruction as a final user message, so it reuses the provider's warm prefix cache rather than paying to re-ingest history.
- A summary that fails to shrink its source, or a summarization error, leaves the conversation surface untouched and (for the reactive path) surfaces the original provider error.
- Configuration lands in `agent.yaml` (threshold ratio, retained tail budget, summarization model override, retry cap) with safe defaults; compaction is on by default.

## Capabilities

### New Capabilities

- `context-compaction`: when and how a session's provider-visible history is compacted, the durable compaction entry, rehydration semantics, trigger conditions, failure handling, and configuration surface.

### Modified Capabilities

<!-- none — no existing specs cover context management -->

## Impact

- `knot/core/session/entries.py`, `state.py`: new entry type; rehydration honors compaction boundaries.
- `knot/core/loop.py` / `harness.py`: turn-boundary pressure check; overflow-retry path around the provider call.
- `knot/providers/*`: expose token usage from responses and a context-window size per model (needed for the pressure estimate); classify context-overflow errors distinctly from other provider errors.
- `knot/authoring/config.py`: new optional `compaction` block in `agent.yaml`.
- HTTP API/events: compaction should be observable on the event stream (a control-plane event), and `docs/http-api.md` updated.
- No breaking changes: existing sessions without compaction entries rehydrate exactly as today.
