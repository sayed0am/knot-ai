# Tasks: add-tool-result-spill (lands 2nd of 4, after assert-model-visible-logged)

## 1. Sequencing gate

- [x] 1.1 Confirm `assert-model-visible-logged` is landed and the e2e suite runs with `invariant_mode=strict` (this change's bounded-surfaces guarantee is proven by that check)

## 2. Spill storage in the session store

- [x] 2.1 Add the `spills` table to the store schema (`session_id`, `tool_call_id` PK pair, `content`, `original_bytes`, `created_at`), created idempotently on store open like the existing schema
- [x] 2.2 Add store methods: `save_spill(session_id, tool_call_id, text)`, `read_spill(session_id, tool_call_id) -> (text, original_bytes) | None`, and deletion of a session's spills wherever session deletion removes entries
- [x] 2.3 Store unit tests: round-trip, overwrite-safety on duplicate key, multi-MB content, cleanup with session, absent-key returns None

## 3. Bounding seam

- [x] 3.1 Evolve `truncate_tool_result` into `bound_tool_result(result, max_bytes, spill_sink)` in `knot/core/truncation.py`: sink present and write succeeds → head/tail preview + notice replacement with `details = {spilled: true, original_bytes, ref}` (no `full_content`); sink absent → today's truncation; sink raises → truncation fallback plus warning log
- [x] 3.2 Implement preview sizing: reserve the notice's UTF-8 cost out of `max_bytes`, split the remainder ~half head / ~half tail on UTF-8-safe boundaries; notice-only when remainder ≤ 0; keep original inline when even the notice cannot fit (never-larger invariant), still writing the spill row
- [x] 3.3 Thread `spill_sink` through `execute_tool`, `run_agent_loop`, and `AgentHarnessConfig` the same way `max_result_bytes` flows; skip the sink for tools flagged `spill_exempt`
- [x] 3.4 Unit tests: replacement always ≤ cap and ≤ original (property-style across sizes/caps), UTF-8 multibyte boundary safety, notice content (omitted bytes, ref, retrieval instruction), fallback paths, exemption flag honored

## 4. Retrieval tool

- [x] 4.1 Add `spill_exempt: bool = False` to `AgentTool`
- [x] 4.2 Implement `read_tool_output(ref, offset_bytes?, limit_bytes?, pattern?)` as a framework tool bound to `(store, session_id)`: raw slice paging via substr; regex search returning bounded match windows with byte offsets; `limit` clamped to the hard ceiling (default 16KB); unknown ref → ordinary `is_error` result naming the ref; marked `spill_exempt`
- [x] 4.3 Inject it into every compiled agent alongside `ask_user`/`load_skill` (`knot/authoring/compile.py` / `runtime.py`), including subagents
- [x] 4.4 Unit tests: paging exactness, search offsets bridge to paging, clamping, unknown ref, session isolation (a ref from another session is unknown)

## 5. Runtime wiring

- [x] 5.1 Construct the spill sink in the runtime wherever a persisted session's harness is assembled; bare harnesses keep truncation behavior
- [x] 5.2 Verify delegation needs no special case: a child's final answer is bounded by the parent's own sink like any tool result (add a test asserting exactly that)

## 6. Verification and documentation

- [x] 6.1 e2e scenario: oversized tool result spills → model retrieves via `read_tool_output` → session parks → process restarts → resumed model retrieves the same ref successfully — all under strict invariant mode
- [x] 6.2 e2e assertion that after a spill, the persisted entry, the SSE payloads, and the provider-visible history each carry preview+ref only
- [x] 6.3 Update `docs/authoring-agents.md` (retrieval tool, `spill_exempt`) and `docs/http-api.md` (result shape); release-note the removal of `details["full_content"]` in the spilled case
