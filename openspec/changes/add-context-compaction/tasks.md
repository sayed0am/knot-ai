# Tasks: add-context-compaction (lands 3rd of 4, after assert-model-visible-logged and add-tool-result-spill)

## 1. Sequencing gate

- [ ] 1.1 Confirm `assert-model-visible-logged` is landed with strict e2e (compaction's projection change is proven equivalent by it) and `add-tool-result-spill` is landed (tool output bounded at the origin; no pruner needed here)

## 2. Provider groundwork

- [ ] 2.1 Add `error_type: "context_overflow" | "rate_limit" | "other" | None` to the error `AssistantMessage`; classify native error shapes in the Anthropic, OpenAI-compatible, and LiteLLM adapters; let the fake provider script typed errors for tests
- [ ] 2.2 Add optional `context_window` to the `agent.yaml` model block plus a small built-in defaults table for well-known models; expose the resolved capacity to the runtime (None ⇒ proactive trigger disabled)
- [ ] 2.3 Unit tests: overflow classification per adapter from captured error fixtures; capacity resolution precedence (explicit config over defaults table over None)

## 3. Durable compaction record

- [ ] 3.1 Add the `"compaction"` entry type (`knot/core/session/entries.py`) with payload `{covers_through_seq, summary_message}` and its deserializer
- [ ] 3.2 Extend `derive_state` to fold compaction entries: drop accumulated messages originating from entries with `seq <= covers_through_seq`, prepend the summary message; requests/resolutions unaffected
- [ ] 3.3 Unit tests: rehydration equals post-compaction live history; repeated compactions compose (later covers more); compaction interleaved with parks preserves pending-request derivation; unknown-entry forward-compat behavior unchanged for older types

## 4. Boundary selection and summarization

- [ ] 4.1 Implement boundary selection: keep a `retain_budget` tail (estimated conservatively from per-message usage deltas), then widen so no assistant message is split from its tool results and the boundary never lands inside the current run's un-persisted turn
- [ ] 4.2 Implement the summarization call: one `provider.stream_response` replaying the session's own system prompt, tool schemas, and the messages being compacted (plus any prior summary), with the summarize instruction appended as the final user message; `max_tokens` capped; model overridable per config
- [ ] 4.3 Validate the outcome: reject responses containing tool calls; reject summaries that fail the shrink check; wrap accepted summaries in `<compacted-summary>` tags as a `UserMessage`
- [ ] 4.4 Unit tests: tool-pair-whole widening cases; shrink rejection leaves surface untouched; text-only enforcement; prefix-replay request shape (asserted against the fake provider's captured request)

## 5. Harness and runtime wiring

- [ ] 5.1 Add the between-turns hook to `run_agent_loop`/`AgentHarness` (same pattern as `get_steering_messages`) and run the proactive pressure check there: latest assistant `usage.input + cache_read + cache_write` vs `threshold_ratio × context_window`
- [ ] 5.2 On accepted compaction: append the `"compaction"` entry under the session's existing writer claim, then `harness.replace_messages(...)` with the post-compaction projection so log and memory agree before the next request (strict invariant stays green)
- [ ] 5.3 Implement the reactive driver in the runtime: a run ending `error` with `error_type == "context_overflow"` triggers compact-then-`continue_()`, bounded by `max_overflow_retries` per originating request; exhausted retries or non-shrinking compaction surface the original provider error
- [ ] 5.4 Unit tests: proactive trigger fires at threshold and not below; unknown capacity skips proactive; reactive retry loop honors the cap; failed summarization in the reactive path preserves the original error

## 6. Configuration

- [ ] 6.1 Add the optional `compaction` block to `agent.yaml` (`enabled`, `threshold_ratio`, `retain_budget`, `summarization_model`, `max_overflow_retries`) with defaults enabling compaction unconfigured; strict-schema validation failing compile with field-naming diagnostics
- [ ] 6.2 Compile/manifest plumbing into the harness config; `knot validate` diagnostics tests for out-of-range ratio and non-numeric cap

## 7. Observability and API

- [ ] 7.1 Add a `compaction` control-plane event (covered span, summary size) emitted on the run's stream via `emit_loop_event`; `PersistenceSubscriber` ignores it by construction
- [ ] 7.2 Surface it over SSE and document in `docs/http-api.md`; update `docs/authoring-agents.md` for the config block

## 8. End-to-end verification

- [ ] 8.1 e2e: scripted fake-provider usage crosses the threshold → compaction event on the stream → next request built from summary+tail → park → restart → resumed session derives identical post-compaction history (strict invariant on)
- [ ] 8.2 e2e: scripted overflow error → reactive compact-and-retry succeeds; second scenario with retries exhausted surfaces the original overflow error
- [ ] 8.3 e2e: raw entry export after compaction still contains every pre-compaction message entry (audit preservation)
