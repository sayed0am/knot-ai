# Design: context compaction

## Context

See proposal.md — Why. Relevant current state:

- History is derived from the entry log by `derive_state` (`knot/core/session/state.py`), which folds `"message"` entries in log order and deliberately ignores unknown entry types (forward compatibility is already the stated contract).
- `run_agent_loop` (`knot/core/loop.py`) is a pure generator: no store access, no policy. The harness (`knot/core/harness.py`) owns queues/cancellation; the runtime (`knot/authoring/runtime.py`) owns durable park/resume.
- Providers already report per-response `Usage` (input/output/cache tokens) on every `AssistantMessage` (`knot/providers/messages.py`), so a context-pressure signal exists with no new metering machinery — dsh needed a token-meter service because its log is chunk-level; knot doesn't.
- Provider errors surface as an error `AssistantMessage` (`stop_reason="error"`, `error_message` text) ending the run with outcome `error`; there is no typed error classification today.

**Landing order**: third of four (invariant → spill → **compaction** → guard). Two prerequisites by design: the model-visible-logged invariant must be in and strict in tests, because compaction is the riskiest projection change this framework will have made — D1's rehydration rule and D3's `replace_messages` handoff are exactly what the invariant check proves equivalent on every request. And spill lands first because oversized tool results are the dominant context consumer; with them bounded, compaction triggers later and summarizes less (it is also why knot needs no dsh-style pre-compaction tool-result pruner — see Non-Goals).

## Goals / Non-Goals

**Goals:**

- Compaction that survives restart by construction: the durable record *is* the mechanism, not a mirror of in-memory state.
- Keep `run_agent_loop` pure — compaction is a harness/runtime concern layered around it.
- Reuse the provider's prefix cache for the summarization call.

**Non-Goals:**

- Tool-result pruning as a separate pre-compaction pass (dsh has one; knot's spill change covers the same pressure source at the origin).
- Cross-model token *estimation* (tiktoken-style counting). We only consume provider-reported usage.
- Compacting a *parked* session offline. Compaction runs only around live runs, where the writer claim is already held.
- Manual/user-triggered compaction endpoints (can layer on later; the primitive is the same).

## Decisions

### D1: Compaction is a new entry type interpreted by `derive_state`

A new `"compaction"` entry whose payload carries `covers_through_seq` (the entry seq of the last message entry it replaces) and `summary_message` (a complete `AgentMessage` — a `UserMessage` whose text is wrapped in `<compacted-summary>` tags). `derive_state` gains one rule: on a compaction entry, drop every message that originated from an entry with `seq <= covers_through_seq` from the accumulated list and prepend the summary message.

- *Why seq-based*: seq is the store's own stable, monotonic coordinate (`PRIMARY KEY (session_id, seq)`); message indexes shift as later compactions land, seqs never do. Repeated compactions compose naturally (a later compaction's `covers_through_seq` is simply higher).
- *Why a full `AgentMessage` payload*: the summary enters provider context as an ordinary message, so every downstream surface (rehydration, export, HTTP API, the model-visible-means-logged invariant) handles it with zero new cases.
- *Alternative rejected — rewriting the log*: deleting/updating rows breaks the append-only audit contract and every consumer that assumes seqs are stable (crash-window detection, `once` approvals).
- *Downgrade note*: older knot code ignores unknown entry types, so a downgraded process would silently replay the *full* uncompacted history. Accepted risk (documented); it fails toward too-much-context, never toward lost facts, and the overflow it may cause is exactly what this feature handles once upgraded again.

### D2: Proactive trigger reads the last assistant `Usage`; capacity comes from config

Pressure = `usage.input + usage.cache_read + usage.cache_write` of the latest `AssistantMessage`, compared against `threshold_ratio × context_window`. `context_window` comes from a new optional field on the agent's model block in `agent.yaml`, with a small built-in defaults table for well-known models in the provider adapters. No capacity ⇒ proactive trigger disabled (spec'd behavior), reactive path still protects the session.

- *Why last-response usage*: it is the provider's own statement of what the context cost at the previous request — strictly more accurate than any client-side estimate, and free. The one-turn lag (this turn may add tool results before the next request) is absorbed by the threshold headroom (default 0.8).
- *Alternative rejected — client-side tokenizers*: per-provider tokenizer dependencies and drift, for accuracy we get from the API anyway.

### D3: Compaction runs in the harness seam, not the loop

The check-and-compact runs where the harness hands control back between runs and between turns: a `pre_turn` hook the harness installs around `run_agent_loop` (same pattern as `get_steering_messages` — the loop calls out, stays pure, and the harness supplies behavior). On trigger, the harness:

1. Selects the boundary: newest span that keeps `retain_budget` tokens of tail (estimated from per-message usage deltas, conservatively), then walks the boundary outward so no assistant message is separated from its tool results (spec: tool pair kept whole) and it never lands inside the current run's un-persisted turn.
2. Runs the summarization call (D4).
3. Validates shrinkage; on success appends the `"compaction"` entry via the session's existing writer claim and calls `harness.replace_messages(...)` with the post-compaction projection so the in-memory surface and the log agree before the next request (which also keeps the model-visible-means-logged invariant green).

The reactive path lives in the runtime's run driver: a run that ends `error` with an overflow-classified error triggers compact-then-re-`continue_()`, bounded by `max_overflow_retries` per originating request.

### D4: Overflow classification is a typed field on the error message

`AssistantMessage` gains an optional `error_type` (e.g. `"context_overflow" | "rate_limit" | "other"`), populated by provider adapters from their native error shapes (Anthropic: `invalid_request_error` mentioning context/tokens; OpenAI-compatible: `context_length_exceeded`). Only `"context_overflow"` authorizes reactive compaction; everything else keeps today's behavior.

- *Alternative rejected — string-matching in the harness*: provider-specific knowledge belongs in the provider adapters; the harness consumes a closed enum.

### D5: Summarization is a direct provider call replaying the session's own prefix

One `provider.stream_response` call with: the same system prompt, the messages being compacted (plus any prior summary), the same tool schemas, and a final appended user instruction to produce a structured summary. Matching the real request's prefix (system + tools + messages) is what lets the provider serve it from the warm prefix cache — the dsh trick, adopted wholesale. The response must be text-only; a response containing tool calls is rejected (treated as summarization failure). `max_tokens` capped by config; the summarization model may be overridden per agent (default: the session's own model).

## Risks / Trade-offs

- [Summary loses task-relevant detail] → Retained verbatim tail (default ~16% of window), `<compacted-summary>` framing so the model knows it is reading a summary, and the full log remains queryable/exportable for humans. Nothing is destroyed.
- [Boundary selection uses estimated per-message costs] → Estimates only pick *where* to cut, never whether the result is acceptable: shrink validation re-checks the real outcome at the next request's reported usage, and the reactive path is the backstop when the estimate was too generous.
- [Compaction loops (compact → still over → compact …)] → Per-run retry caps on both paths; a compaction that fails to shrink is rejected outright, so each accepted compaction strictly reduces the surface.
- [Concurrent writer conflict] → Compaction appends only under the session's existing `PersistenceSubscriber` writer claim, inside the harness that owns it; the store's single-writer invariant is untouched.
- [Downgrade replays uncompacted history (D1)] → Documented; fails toward overflow, which the upgraded code recovers from.

## Migration Plan

Pure addition: new entry type, new optional config block, new event type on the stream. Existing sessions and fleets compile and rehydrate unchanged. Rollback = disable via config (`compaction: {enabled: false}`); already-written compaction entries remain valid history and continue to be honored by `derive_state` (rollback of the *code* is the downgrade case in D1).

## Open Questions

- Default `retain_budget` value (ratio vs. absolute tokens) — tune once real fleets exercise it; spec only requires it be configurable with a working default.
- Whether the compaction event should also be exposed as a queryable fact (e.g. compaction count in fleet session listings) — additive API surface, decide with the frontend consumer.
