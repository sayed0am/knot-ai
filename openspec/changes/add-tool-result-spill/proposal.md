# Add tool-result spill-to-file

## Why

When a tool result exceeds `max_result_bytes`, knot truncates the model-facing text (`knot/core/truncation.py`) and stashes the full original under `details["full_content"]`. That gives the worst of both worlds: the model can never recover the tail it may turn out to need (details are invisible to it, so the data is a dead end), while the full blob still rides every persisted entry and every SSE payload, unbounded. Oversized results should stay retrievable by the model on demand, and the things that must stay small (provider context, wire events, log entries) should actually be bounded.

## What Changes

- Replace destructive truncation with **spill**: when a tool result exceeds the inline byte cap, write the *full* result to a session-scoped spill location and hand the model a bounded head/tail preview plus a notice naming the omitted byte count, the spill locator, and how to retrieve it.
- Provide a framework retrieval tool (e.g. `read_spill(locator, offset, limit)`), always available to agents, so the model can page through or search spilled content on demand. Retrieval results are themselves exempt from spilling (no spill→read→spill loop).
- The preview-plus-notice replacement is always sized within the inline cap (the notice's cost is reserved out of the budget, shrinking the preview to fit). If even a notice-only replacement cannot fit, the original result is kept inline — spilling never makes a result larger.
- Spill is best-effort: a spill-write failure logs a warning and falls back to current truncation behavior; it never converts a successful tool call into an error.
- The session entry log records the replaced (preview) result as the model-visible fact plus the spill locator, keeping the "what the model saw" audit trail intact while the full payload lives in the spill store.
- Spill storage is session-scoped and keyed by tool call id; its backing (filesystem vs. the existing SQLite store) is a design decision, with lifecycle (cleanup on session deletion) defined alongside.
- `max_result_bytes` keeps its meaning as the inline cap; spill becomes the default behavior where it applies, with truncation as the fallback.

## Capabilities

### New Capabilities

- `tool-result-spill`: the spill decision rule, preview/notice format and sizing invariant, the retrieval tool contract, spill storage layout and permissions, failure fallback, and interaction with the session log.

### Modified Capabilities

<!-- none — no existing specs cover result truncation -->

## Impact

- `knot/core/truncation.py` and the `execute_tool` path in `knot/core/tools.py`: spill decision replaces byte-cap truncation.
- `knot/core/session/`: entry payloads gain an optional spill locator; store/queries unaffected otherwise.
- `knot/authoring/runtime.py` / `compile.py`: inject the framework `read_spill` tool the same way `load_skill` and `ask_user` are injected today.
- `knot/server/`: spill configuration surface (if the chosen backing needs any).
- Docs: `docs/authoring-agents.md`, `docs/http-api.md`.
- No breaking changes: where spill is unavailable, behavior degrades to today's truncation.
