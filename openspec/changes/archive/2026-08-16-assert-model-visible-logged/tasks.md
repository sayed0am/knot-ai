# Tasks: assert-model-visible-logged (lands 1st of 4 — nothing gates it; spill, compaction, and guard all gate on this)

## 1. Canonical projection

- [x] 1.1 Implement `canonical_history(messages)` (beside `provider_context` in `knot/core/tool_history.py` or a sibling module): apply `provider_context`, dump to plain JSON, exclude provider-invisible fields (tool-result `details`, message ids, timestamps), with the exclusion list defined in one place next to the provider message models
- [x] 1.2 Unit tests for the projection: details-only difference projects equal; content/role/tool-linkage differences project unequal; repaired interrupted-tool-result messages round-trip equal through entry serialization

## 2. Loop hook

- [x] 2.1 Add optional `pre_request_hook: Callable[[Sequence[AgentMessage]], Awaitable[None]] | None` to `run_agent_loop`, invoked with the exact message list once before each provider request; a raised `HistoryDivergenceError` flows into the existing error-outcome path (no new outcome kind)
- [x] 2.2 Thread the hook through `AgentHarnessConfig` into the loop call in `AgentHarness._run`
- [x] 2.3 Unit tests: hook fires once per provider request (multi-turn run), not at all when `None`; strict raise ends the run with outcome `error` and no provider call is made (fake provider asserts zero calls after divergence)

## 3. Divergence checker and report

- [x] 3.1 Implement the checker: rehydrate via `derive_state(store.entries(session_id)).messages`, compare `canonical_history` of both sides, locate first diverging index, build the structured report (index, entry seq where applicable, which side differs, field-level diff)
- [x] 3.2 Implement modes: `strict` raises `HistoryDivergenceError` carrying the report; `warn` logs the report and returns
- [x] 3.3 Unit tests: report pinpoints first divergence with seq and fields for (a) in-memory extra message, (b) log extra message, (c) field mutation; equal histories produce no report

## 4. Runtime and server wiring

- [x] 4.1 Add `invariant_mode: strict | warn | off` to runtime/server configuration with env override; serving default `warn`
- [x] 4.2 Install the checker as the `pre_request_hook` wherever the runtime builds a harness for a persisted session (`knot/authoring/runtime.py`); bare harnesses without persistence get no hook
- [x] 4.3 Default `strict` in test fixtures: flip the e2e runtime construction to strict so every scenario (plain turn, park/approve, delegation chain, restart recovery) runs under the invariant

## 5. Verification and documentation

- [x] 5.1 Add a regression test that deliberately violates the invariant (append to harness memory without persistence) and asserts strict failure before the provider call and warn-mode continuation
- [x] 5.2 Run the full suite with strict e2e enabled; fix any latent divergence it surfaces (finding one is the change working)
- [x] 5.3 Document the named design rule — "any new model-visible input requires a durable entry" — in `docs/` where contributors adding features will meet it, and note the `invariant_mode` setting in the server docs
