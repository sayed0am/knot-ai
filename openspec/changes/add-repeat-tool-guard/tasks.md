# Tasks: add-repeat-tool-guard (lands 4th of 4, after assert-model-visible-logged, add-tool-result-spill, and add-context-compaction)

## 1. Sequencing gate

- [x] 1.1 Confirm `assert-model-visible-logged` is landed with strict e2e (the injected advisory is the invariant's "new model-visible input" case) and that spill + compaction are merged per the landing order

## 2. Configuration

- [x] 2.1 Define `RepeatGuardConfig` (`enabled`, `thresholds` default `[3, 5, 8]`, `exclude` default `[ask_user, load_skill]`, `preview_cap` default 500) and add the optional `repeat_guard` block to `agent.yaml`
- [x] 2.2 Compile-time validation failing loud with field-naming diagnostics: empty thresholds, non-integer, value < 2, duplicates, non-positive preview cap; thresholds normalized ascending
- [x] 2.3 Plumb through manifest → `AgentHarnessConfig` → loop parameter (the `max_result_bytes` pattern); `knot validate` diagnostic tests for the invalid cases

## 3. Detection in the loop

- [x] 3.1 Implement the canonical key: tool name + `json.dumps(arguments, sort_keys=True, separators=(",", ":"))`
- [x] 3.2 Track the per-run chain `(key, count, fired_thresholds)` in the decision sweep at the top of `_run_tool_phase`, counting in model-emitted call order — denied and unknown-tool calls tick the chain there too
- [x] 3.3 Implement `*`-wildcard exclusion matching over qualified names; excluded calls are transparent (no increment, no reset)
- [x] 3.4 Reset the chain at every non-guard user-message admission point (prompts, steering, follow-ups)
- [x] 3.5 Unit tests: property-order-insensitive matching; different-args reset; transparency laundering case (`grep X → excluded → grep X` counts 2); denied calls count; steering resets; concurrency does not affect counting order

## 4. Advisory injection

- [x] 4.1 Add the fixed advisory copy as constants beside the loop: short generic first-threshold text; detailed later-threshold text naming tool, count, and head-truncated args preview (`preview_cap` chars with omitted-count marker)
- [x] 4.2 Inject the advisory as a `<repeat-tool-reminder>`-tagged `UserMessage` through the existing steering append+emit path at the next turn start, so it lands in history, events, and the entry log identically to steering
- [x] 4.3 Fire each threshold at most once per run (`fired_thresholds`); detection always compares full canonical args regardless of `preview_cap`
- [x] 4.4 Unit tests: first vs later advisory content; once-per-threshold; preview truncation marker; full-args comparison with tiny `preview_cap`

## 5. Verification and documentation

- [x] 5.1 Persistence test: a run that received an advisory rehydrates with the advisory at the same position (strict invariant on)
- [x] 5.2 e2e: scripted fake provider loops an identical call past the thresholds → advisories appear on the SSE stream and in the entry log → chain is fresh after park/resume
- [x] 5.3 Advisory-only proof: test asserting a threshold-crossing call executes normally with an unchanged result
- [x] 5.4 Update `docs/authoring-agents.md` with the `repeat_guard` block, defaults, and exclusion semantics
