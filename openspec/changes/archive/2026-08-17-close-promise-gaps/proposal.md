## Why

A source-grounded audit (see `agentic-designs/README.md`, cross-cutting findings) confirmed a set of places where knot's schema, config, or docs promise behavior the runtime does not deliver — `UsageCost` ships all-zeros, `model.provider` and `model.max_tokens` are validated but never read, crash-window repair exists but is never invoked by `knot serve`, approval TTLs are unreachable from config — plus a handful of small, high-leverage capability gaps (mid-run steering, event timing, a token budget, HTTP auth). Closing them in one batch makes the wire and config surfaces honest and turns already-written library code into shipped capability.

## What Changes

- **Crash-window repair wired into serving.** `knot serve` runs `detect_crash_windows`/`repair_crash_windows` at startup; unrepairable windows surface via an operator endpoint backed by `operator_skip`.
- **Honest cost reporting.** `Usage.cost` becomes optional (`UsageCost | None`); the litellm adapter populates it from provider-reported cost, other adapters leave it `None`. No more all-zeros cost objects masquerading as data.
- **Per-agent provider routing.** A provider registry replaces the single hardcoded `AnthropicProvider()` in `knot serve`; `agent.yaml`'s `model.provider` selects the provider instance per session. `model.max_tokens` is actually passed through per call, and a per-agent thinking budget becomes configurable.
- **Event timing and progress.** `ToolExecutionStartEvent`/`UpdateEvent`/`EndEvent` gain a `timestamp`; `TurnStartEvent`/`TurnEndEvent` gain a `turn` counter. Additive wire schema change.
- **Mid-run steering over HTTP.** New `POST /sessions/{id}/steer` exposes the existing `harness.steer` mechanism so a live human can inject guidance into a running turn.
- **Session token budget.** New `limits.max_session_tokens` in `agent.yaml`; the runtime aggregates persisted per-turn usage and ends the run (error outcome) when the budget is exhausted.
- **Configurable approval TTLs.** `agent.yaml` approvals accept an object form carrying `ttl_seconds`, making the existing expiry-as-denial path reachable without custom hook code.
- **Optional HTTP auth.** `knot serve --token <t>` (or env var) enables static bearer-token auth middleware; without it, behavior is unchanged.
- **Docs only (no spec):** the typed-verdict authoring convention (structured critic/judge outcomes as a tool with typed `Annotated` args) is added to `docs/authoring-agents.md`, including the context-threading recipe for multi-round loops.

Explicitly out of scope, recorded as a deliberate decision: **delegation stays memoryless** — no `resume_child` mode. Every delegation call remains a pure function of its `{message}` string; multi-round patterns thread context explicitly through the parent.

## Capabilities

### New Capabilities

- `crash-window-repair`: startup repair of ambiguous crash windows and operator resolution of unrepairable ones.
- `usage-cost-reporting`: optional, honestly-populated USD cost on the wire usage model.
- `per-agent-provider-routing`: provider registry keyed by `model.provider`, plus honoring `model.max_tokens` and per-agent thinking budget.
- `event-timing-and-progress`: timestamps on tool-execution events and turn counters on turn events.
- `session-steering`: HTTP endpoint injecting steering messages into a running session.
- `session-token-budget`: per-session token ceiling enforced by the loop.
- `approval-ttl-config`: TTL-bearing approval policy configuration in `agent.yaml`.
- `http-auth`: optional static bearer-token authentication for the HTTP API.

### Modified Capabilities

_None — the four existing specs (context-compaction, model-visible-logged-invariant, repeat-tool-guard, tool-result-spill) are untouched at the requirement level._

## Impact

- `src/knot/providers/`: `messages.py` (`Usage.cost` optionality), `litellm.py` (cost population), `anthropic.py` (per-call max_tokens/thinking overrides), `provider.py` (request param surface).
- `src/knot/core/`: `events.py` (timestamps, turn counters), `loop.py` (turn stamping, budget check), `hitl/resume.py` consumers.
- `src/knot/authoring/`: `config.py` (`model.provider` semantics, `limits.max_session_tokens`, approval object form, thinking budget), `runtime.py` (provider selection per session, budget aggregation, TTL plumbing).
- `src/knot/server/`: `cli.py` (registry construction, `--token`), `app.py` (steer endpoint, repair-at-startup, operator endpoint, auth middleware).
- `docs/`: `authoring-agents.md` (provider routing now real, budget, TTL, typed-verdict convention), `http-api.md` (new endpoints, auth, new event fields).
- Wire compatibility: all event changes are additive; `usage.cost` changes from always-present-zeros to nullable — SSE consumers that read `.cost.total` must handle `null` (**BREAKING** for such consumers, though no real consumer exists today).
