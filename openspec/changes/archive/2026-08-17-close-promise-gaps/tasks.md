## 1. Provider request params (design D1)

- [x] 1.1 Add optional keyword-only `max_tokens` and `thinking_budget_tokens` to `ModelProvider.stream_response` and update all four adapters (`anthropic`, `openai_compatible`, `litellm`, `fake`) to accept them, honoring what each supports per-call
- [x] 1.2 Add `thinking_budget_tokens` to `ModelConfig` in `authoring/config.py`; emit a compile diagnostic when set for a provider that cannot honor it
- [x] 1.3 Thread `manifest.model.max_tokens` and thinking budget from `authoring/runtime.py` through `core/loop.py` into the provider call
- [x] 1.4 Tests: per-call `max_tokens` reaches the request payload; thinking enabled only for the configured agent; non-thinking-provider config fails compile
- [x] 1.5 Docs: fix the `model.max_tokens` line in `docs/authoring-agents.md` to match now-true behavior; document the thinking budget field

## 2. Honest usage cost (design D3)

- [x] 2.1 Change `Usage.cost` to `UsageCost | None = None` and make `UsageCost` category fields nullable with `total` required, in `providers/messages.py`
- [x] 2.2 Populate `cost` in the litellm adapter from its native response-cost figure; leave other adapters at `None`
- [x] 2.3 Tests: litellm response carries pass-through cost; anthropic/openai responses serialize `cost: null`; persisted entries round-trip the null
- [x] 2.4 Docs: mark `usage.cost` nullable in `docs/http-api.md` with the breaking note for consumers reading `.cost.total`

## 3. Per-agent provider routing (design D2)

- [x] 3.1 Replace `AgentRuntime`'s single `provider` with `providers: Mapping[str, ModelProvider]` + `default_provider`, resolving each session's provider from its own agent's `model.provider` at harness build (children included)
- [x] 3.2 In `server/cli.py`, scan compiled manifests for referenced providers, construct exactly those adapters from their env configuration, and exit with an agent-naming diagnostic on unknown name or failed construction
- [x] 3.3 Tests: two agents on different providers route separately in one process; provider-less fleet reproduces v0 behavior; unknown provider fails startup
- [x] 3.4 Docs: rewrite the `model.provider` "informational in v0" paragraph in `docs/authoring-agents.md` to describe real routing and startup failure modes

## 4. Event timing and turn counters (design D4)

- [x] 4.1 Add `timestamp` to `ToolExecutionStart/Update/EndEvent` and `turn` to `TurnStart/EndEvent` in `core/events.py`; stamp them at emission in `core/loop.py`
- [x] 4.2 Tests: start/end timestamps are ordered and subtractable; turn numbers increment 1-based per run; existing event consumers (SSE serialization) unchanged
- [x] 4.3 Docs: add the new fields to the event table in `docs/http-api.md`

## 5. Session steering endpoint (design D5)

- [x] 5.1 Verify a drained steer message emits `MessageEndEvent` (so persistence logs it); fix the harness to emit one if not
- [x] 5.2 Add `POST /sessions/{id}/steer` in `server/app.py`: deliver via `state.running[id].harness.steer`, 409 with a `/messages` pointer when not running
- [x] 5.3 Tests: steer mid-run reaches the next turn and lands in the durable log; steer on idle session returns 409 and writes nothing; steer/finish race yields 409 or delivery, never a silent drop
- [x] 5.4 Docs: document the endpoint and its relationship to `/messages` follow-ups in `docs/http-api.md`

## 6. Session token budget (design D6)

- [x] 6.1 Add `limits.max_session_tokens` to `LimitsConfig` and the compiled manifest
- [x] 6.2 Compute the persisted-usage baseline at harness build (sum over entry-log usage, not the compacted projection) and enforce pre-turn in `core/loop.py`, ending the run with a budget-naming error outcome before the provider request
- [x] 6.3 Tests: budget exhaustion ends the run without a provider call; baseline survives restart and compaction; unset budget changes nothing
- [x] 6.4 Docs: document the limit in `docs/authoring-agents.md`

## 7. Approval TTL config (design D7)

- [x] 7.1 Extend `approvals` values in `authoring/config.py` to `ApprovalPolicyName | ApprovalSetting({policy, ttl_seconds})`, threading `ttl_seconds` through `build_decision_hook` into `RequireApproval`
- [x] 7.2 Add `ttlSeconds` to `GET /approvals` items in `server/app.py`
- [x] 7.3 Tests: bare form unchanged; object form parks with TTL and expiry denies durably with the expiry reason; inbox shows the TTL
- [x] 7.4 Docs: document the object form in `docs/authoring-agents.md` and the inbox field in `docs/http-api.md`

## 8. Serve-time crash-window repair (design D8)

- [x] 8.1 Add `AgentRuntime.repair_all_crash_windows()` wrapping detect/repair with each session's compiled tools, returning the `needs_operator` residue; call it from the FastAPI lifespan before serving and log counts
- [x] 8.2 Add `GET /crash-windows` and `POST /crash-windows/skip` backed by `operator_skip`
- [x] 8.3 Tests: idempotent window repaired at startup with durable result; non-idempotent window listed and resolvable via skip; resolved session becomes resumable
- [x] 8.4 Docs: document startup repair and the operator endpoints in `docs/http-api.md`

## 9. HTTP bearer-token auth (design D9)

- [x] 9.1 Add `--token` / `KNOT_SERVE_TOKEN` to `server/cli.py` and an ASGI middleware in `server/app.py` using `hmac.compare_digest`, covering all routes including SSE; generic 401 body
- [x] 9.2 Tests: no-token server unchanged; wrong/missing header 401s before any handler; valid token streams SSE; token never appears in logs or responses
- [x] 9.3 Docs: replace the "no authentication layer in v0" paragraph in `docs/http-api.md` with the opt-in token story

## 10. Authoring docs: typed-verdict convention (docs-only item)

- [x] 10.1 Add the typed-verdict recipe to `docs/authoring-agents.md`: structured critic/judge outcomes as a tool whose `Annotated` args are the schema, plus the context-threading recipe for multi-round loops, and record that delegation is deliberately memoryless
- [x] 10.2 Update `agentic-designs/README.md` cross-cutting findings to reflect what this change closes and move delegation memory to the recommended-against section

## 11. Wrap-up

- [x] 11.1 Run the full test suite and `ruff format`/lint; fix fallout
- [x] 11.2 Re-run `knot validate` on `examples/fleet` to confirm no authored-fleet regressions
