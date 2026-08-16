# Design: model-visible-means-logged invariant

## Context

See proposal.md — Why. Relevant current state:

- Provider context is assembled in the loop via `provider_context(messages)` (`knot/core/tool_history.py`) immediately before `provider.stream_response` (`knot/core/loop.py`).
- The durable side already has the exact reconstruction function the check needs: `derive_state(entries).messages` (`knot/core/session/state.py`).
- Persistence is a harness subscriber (`PersistenceSubscriber`) that appends entries synchronously on `MessageEndEvent` — so at any between-events moment, the log is up to date with everything the harness has emitted.
- The harness repairs dangling tool calls (`_append_interrupted_tool_results`) *before* the loop starts, and those repairs flow through events to persistence — repair-then-persist ordering already exists.

**Landing order**: this change lands FIRST of the four (invariant → spill → compaction → guard). It has no dependency on the others; the other three are written against it — spill's locator-bearing results, compaction's summary projection, and the guard's injected advisory all have to keep this check green, which is precisely why it goes in first.

## Goals / Non-Goals

**Goals:**

- Turn silent replay divergence into an immediate, attributable failure at the moment it is introduced — not at the eventual resume where the cause is long gone.
- Zero behavior change for correct code; near-zero cost when disabled.
- Keep `run_agent_loop` policy-free: the loop gains a neutral hook, not knowledge of stores.

**Non-Goals:**

- Detecting divergence for unpersisted harnesses (nothing to diverge from).
- Guaranteeing the *provider adapters* faithfully serialize the canonical projection (that is the provider layer's existing contract, tested there).
- A general pre-request middleware/plugin system. One typed hook, one purpose; knot's fixed-grammar philosophy stands.

## Decisions

### D1: A neutral `pre_request_hook` on the loop; the runtime supplies the checker

`run_agent_loop` gains an optional `pre_request_hook: Callable[[Sequence[AgentMessage]], Awaitable[None]] | None`, invoked with the exact message list a provider request is about to be built from, once per request. The harness threads it through from config; the runtime (which owns both the store and the session id) installs the invariant checker there. Strict mode raises a dedicated exception the loop converts into its existing error-outcome path (no new outcome kind).

- *Why a loop hook and not a subscriber*: subscribers see events after the fact; this check must run *before* the provider call to stop a bad request in strict mode. The hook sits exactly at the boundary the invariant is defined over.
- *Why not inside `provider_context`*: that function is a pure projection used in several contexts (including tests and resume); hoisting policy into it would couple projection to persistence.
- *Alternative rejected — checking in `PersistenceSubscriber`*: it only observes emitted events; it cannot see what the next request will contain, and it has no veto.

### D2: Compare under a dedicated canonical projection shared with providers

A `canonical_history(messages)` helper produces the comparison form: the output of `provider_context(...)`, dumped to plain JSON with provider-invisible fields excluded — tool-result `details`, message ids and timestamps, and any field the provider layer documents as non-wire. Both sides go through the same helper: `canonical_history(in_memory)` vs `canonical_history(derive_state(store.entries(sid)).messages)`. First inequality wins; the report walks the two JSON lists to the first differing index and diffs keys at that position.

- *Why exclusion-based projection*: the spec requires details-only differences to pass; enumerating what is *visible* per message type would drift from provider adapters. Excluding the documented-invisible set keeps one list to maintain, colocated with the provider message models.
- *Repair ordering*: the check naturally runs after the harness's dangling-call repair (repairs are appended to `messages` and persisted via prelude events before the first request), so repairs are on both sides — no special-casing.

### D3: Mode plumbing and defaults

An `invariant_mode: "strict" | "warn" | "off"` setting on the runtime/server config (env-overridable), defaulting to `warn` in `knot serve` and `strict` wherever tests construct runtimes (the e2e scenarios flip it on globally). `off` exists for emergency operational bypass, deliberately undocumented in authoring docs.

- *Why warn as the serving default*: a false positive in production must degrade to telemetry, not an outage; strict-by-default in CI is what actually ratchets correctness (every future feature runs under it in tests).

### D4: Full check every request; measured before optimizing

The comparison is O(history) JSON dumps per request. Provider calls dwarf it by orders of magnitude, and knot's history lengths are bounded by compaction (landing right after this). If profiling ever disagrees, degrade to checking the first request after rehydration plus sampling — the contract in the spec doesn't change, so this is safely deferred.

## Risks / Trade-offs

- [False positives from legitimate normalization differences (pydantic defaults, alias round-trips)] → Both sides pass through the identical projection helper and serializer; the e2e suite running strict across park/resume/delegation scenarios is the regression net that keeps the exclusion list honest.
- [Warn-mode divergence goes unnoticed in production] → The warning carries the structured report and rides normal logging; operators can escalate to strict per deployment. CI strictness catches the bug class before it ships.
- [Hook adds a per-request await to the hot path] → It is a no-op `None` for unpersisted harnesses and gated by mode; cost is one function call when off.

## Migration Plan

Land as: projection helper + loop hook + runtime wiring + strict-mode e2e flip, in one change (it is small). No data migration; no API surface change. Rollback = set mode `off` (or revert; nothing durable depends on it).

## Open Questions

- Whether the warn-mode report should also be surfaced as a control-plane SSE event for frontend visibility — additive; decide alongside compaction's event, which faces the same choice.
