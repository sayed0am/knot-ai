## Context

See `proposal.md` — Why. Design-relevant current state, verified against `develop`:

- `ModelProvider.stream_response(model, system, messages, tools, signal, session_id)` (`src/knot/providers/provider.py:67`) has no per-call sampling params; `max_tokens` and thinking budget exist only as provider-constructor state. Four implementations: `anthropic`, `openai_compatible`, `litellm`, `fake`.
- `knot serve` hardcodes one `AnthropicProvider()` (`server/cli.py:117-131`); `AgentRuntime` holds a single `provider` object. `model.provider` has zero read sites; `model.name` is already passed per session (`authoring/runtime.py:462`).
- `Usage.cost: UsageCost = UsageCost()` (`providers/messages.py:63`) — always all-zeros; no adapter assigns it. litellm reports per-response cost natively.
- The loop owns `turn` internally (`core/loop.py:201`) but never stamps it on events; `current_timestamp_ms` already exists and is used by `PendingInputRequest.created_at`.
- The server tracks live runs in `state.running[session_id] = _RunningTurn(harness, task)` (`server/app.py:271`); `harness.steer` exists and is drained every turn (`core/loop.py:327-328`).
- `detect_crash_windows` / `repair_crash_windows` / `operator_skip` (`core/hitl/resume.py`) are exported but have no callers outside tests.
- Approvals in `agent.yaml` are `dict[str, ApprovalPolicyName]` (`authoring/config.py:226`); `RequireApproval.ttl_seconds` and the lazy expiry sweep already work end-to-end when a TTL is present.
- Per-turn `AssistantMessage.usage` is durably persisted in the entry log; compaction already reads its token fields (`core/compaction.py:95`).

Constraint: everything model-visible must be a durable entry (model-visible-logged invariant); steering and budget-termination messages must go through the ordinary event→persistence path.

## Goals / Non-Goals

**Goals:**
- One coherent request-parameter seam (`max_tokens`, thinking budget) rather than piecemeal provider constructor flags.
- Provider routing that fails at startup, not at first request.
- All new wire fields additive; all new config optional with unchanged defaults.

**Non-Goals:**
- No delegation memory / `resume_child` (deliberate exclusion — delegation stays a pure function of `{message}`).
- No USD price table maintained in-repo; no USD budget (token budget only, until cost data is broadly populated).
- No dynamic mid-session model or provider switching; routing is fixed per agent at compile time.
- No auth beyond a single static bearer token (multi-user/RBAC stays a deployment concern).

## Decisions

**D1 — Request params become per-call keyword args on the protocol.**
Add optional keyword-only `max_tokens: int | None = None` and `thinking_budget_tokens: int | None = None` to `stream_response`. The loop threads them from the manifest (as it already does `model`). Providers that can't honor a param ignore it at request time; the *compile* step diagnoses thinking-budget-on-non-thinking-provider so ignoring never happens silently.
*Alternative considered:* a `RequestParams` dataclass — more extensible, but two params don't justify the indirection yet; kwargs keep the Protocol diff obvious. Revisit if a third param appears.

**D2 — Provider registry: build only what the fleet references.**
`AgentRuntime` takes `providers: Mapping[str, ModelProvider]` plus `default_provider: str` (replacing the single `provider` arg). `knot serve` scans compiled manifests for the set of distinct `model.provider` values, constructs exactly those adapters (each from its existing env-var configuration), and exits with a diagnostic naming agent + provider on unknown names or failed construction. Sessions resolve their provider from their agent's manifest at `build_harness` time; children resolve from their own agent's manifest.
*Alternative considered:* construct all known adapters eagerly — wasteful and forces credentials for providers nobody uses.

**D3 — `Usage.cost` becomes `UsageCost | None`, litellm-populated, pass-through only.**
Default flips from `UsageCost()` to `None`. `UsageCost` category fields become `float | None` so litellm's total-only report is expressible without fabricated zeros; `total` is required whenever the object is present. Only litellm assigns it (from its native response-cost figure). This is the one non-additive wire change; `docs/http-api.md` documents `usage.cost` as nullable.
*Alternative considered:* in-repo Anthropic price table — rejected (user decision): stale-table risk outweighs coverage.

**D4 — Timestamps and turn stamping at the emission site.**
`ToolExecutionStart/Update/EndEvent` gain `timestamp` (ms, via existing `current_timestamp_ms`); `TurnStart/EndEvent` gain `turn`, stamped from the loop's existing counter. Purely additive; persisted entries are unaffected (these events aren't entry types).

**D5 — Steering rides the existing running-turn registry.**
`POST /sessions/{id}/steer` looks up `state.running[session_id]` and calls `harness.steer(text)`; absent → 409 pointing at `/messages`. The steering message reaches the model as an ordinary user message drained at the turn boundary, so the existing `PersistenceSubscriber` path logs it — invariant holds with no new entry type. Verify at apply time that the drained steer emits a `MessageEndEvent`; if it doesn't, fix the harness to emit one rather than adding a bespoke write.

**D6 — Token budget derives from the durable log, checked pre-turn.**
`limits.max_session_tokens` lands in `LimitsConfig`. At harness build, the runtime computes the session's accumulated total (sum of persisted per-turn `usage` input+output+cache fields — same arithmetic compaction already uses) and passes the loop a budget context; the loop adds in-run usage per turn and, when exhausted, ends the run with an error outcome before the provider request (mirroring the `max_turns` check at `loop.py:227`). Restart-safe because the baseline is recomputed from the log.
*Alternative considered:* enforcing only in-run — cheaper but trivially escaped by run-splitting.

**D7 — Approval TTL via a union config form.**
`approvals` values become `ApprovalPolicyName | ApprovalSetting` where `ApprovalSetting = {policy: ApprovalPolicyName, ttl_seconds: int | None}`. `build_decision_hook` passes `ttl_seconds` into `RequireApproval`; everything downstream already works. `GET /approvals` adds `ttlSeconds` (additive).

**D8 — Startup repair as a runtime method invoked from app lifespan.**
`AgentRuntime.repair_all_crash_windows()` wraps `detect_crash_windows` + `repair_crash_windows` with each session's compiled tool set, and returns the `needs_operator` residue; the FastAPI lifespan calls it before serving. New endpoints: `GET /crash-windows` and `POST /crash-windows/skip` (session id + tool call id) → `operator_skip`. A session with an unresolved window is reported but not auto-resumed.

**D9 — Auth as ASGI middleware with constant-time compare.**
`--token` / `KNOT_SERVE_TOKEN` env var; when set, middleware checks `Authorization: Bearer` via `hmac.compare_digest` on every request including SSE. 401 body is generic. Middleware sits outside routing so new endpoints are covered by default.

## Risks / Trade-offs

- [Protocol change (D1) breaks third-party `ModelProvider` implementations] → kwargs are optional-with-defaults; a structural `Protocol` match still works for callers that never pass them. Release note suffices at this stage.
- [`usage.cost` nullability (D3) breaks a consumer reading `.cost.total`] → no known consumer exists; documented as **BREAKING** in proposal and `http-api.md` anyway.
- [Registry startup failure (D2) makes a previously-booting fleet fail after setting `model.provider`] → intended fail-fast; diagnostic names the agent and the missing credential/provider.
- [Budget baseline recomputation (D6) mis-sums after compaction] → sum over *entries*, not over the compacted projection — entries are append-only and survive compaction.
- [Steer race: run finishes between lookup and `steer()` (D5)] → harness treats steering into a finished run as a no-op; endpoint returns 409 based on registry state at call time; document at-least-409-or-delivered semantics.
- [Startup repair (D8) lengthens boot on large stores] → detection is a single indexed scan; repair is bounded by actual crash windows, which are rare. Log a count either way.

## Migration Plan

1. Land D1 (protocol params) and D3 (cost) first — both are provider-layer only and independently testable.
2. D2 (registry) next; `providers={"anthropic": AnthropicProvider()}` + `default_provider="anthropic"` reproduces v0 exactly for fleets that never set `model.provider`.
3. D4–D9 are independent of each other and can land in any order.
4. Rollback: every feature is behind optional config (token, TTL, budget, thinking) or additive schema; reverting the two breaking-ish pieces (D1 signature, D3 nullability) is a clean revert with no data migration — the entry log gains no new entry types.

## Open Questions

- Whether the drained steer message already emits a `MessageEndEvent` (D5 verification note) — answerable at apply time without changing the approach.
- Exact env-var names for non-default provider construction in serve (follow each adapter's existing convention).
