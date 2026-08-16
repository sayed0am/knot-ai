# Assert the model-visible-means-logged invariant

## Why

Knot's durability story rests on one implicit contract: everything the model sees must be reconstructable from the session's entry log, because park/resume, crash repair, `once` approvals, and restart recovery all rehydrate from that log. Today nothing enforces it — a future code path that appends a message to the in-memory harness without persisting it (or persists something rehydration renders differently) would cause *silent replay divergence*: the resumed session would see different history than the live one did, and nothing would notice.

## What Changes

- State the invariant explicitly and assert it at runtime: at the point where provider-visible context is assembled for a model request, the message history derivable from the durable entry log must match the in-memory history being sent (for persisted sessions).
- Add a comparison utility that projects both surfaces to a canonical form (the same normalization `provider_context` applies) and reports the first divergence with enough detail to debug — index, entry seq, and a field-level diff.
- Enforcement modes: `strict` (raise, fail the run before the provider call — divergence becomes an immediate, attributable error instead of corrupted resume behavior later) and `warn` (log and continue). Strict is the default for tests and the dev server; warn for production serving, configurable.
- The check runs only for sessions with persistence attached (a bare in-memory `AgentHarness` has no log to diverge from) and is cheap enough to run per request; if profiling shows otherwise, it degrades to sampling or first-request-after-resume checks without changing the contract.
- Document the invariant as a named design rule so future features (compaction, spill, steering variants) are written against it: any new model-visible input requires a new durable entry or an extension of an existing one, never an in-memory-only injection.

## Capabilities

### New Capabilities

- `model-visible-logged-invariant`: the invariant's definition, the canonical projection both sides are compared under, enforcement modes and their defaults, scope (persisted sessions only), and the divergence report contract.

### Modified Capabilities

<!-- none -->

## Impact

- `knot/core/session/state.py` / `persistence.py`: comparison utility over rehydrated vs. live history.
- `knot/core/harness.py` or the loop's provider-call boundary: the assertion hook point.
- `knot/server/`: config for enforcement mode.
- Tests: the e2e scenarios gain an always-on strict check, which also hardens every future feature's test coverage for free.
- No breaking changes for correct code; code that was already violating the invariant will now fail loudly — that is the point.
