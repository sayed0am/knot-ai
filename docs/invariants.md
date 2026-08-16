# Invariants

Design rules that hold across the whole framework, not just one module —
the kind of contract a future change can silently break without any single
file's tests catching it. This doc is for contributors to knot itself
(anyone touching `knot.core.loop`, `knot.core.harness`, or the runtime
wiring in `knot.authoring.runtime`), not for fleet authors.

## Model-visible means logged

**The rule:** any new model-visible input requires a durable entry — never
an in-memory-only injection. If a change introduces content the model will
see (a new message type, an injected advisory, a synthesized tool result),
that content must be persisted as a durable entry (a new entry type, or an
extension of an existing one) before it reaches the provider request. There
is no code path in which the harness's in-memory history is allowed to run
ahead of what the session's entry log can reconstruct.

**Why it matters:** knot's whole durability story — park/resume, crash
repair, `once` approvals, restart recovery — works by rehydrating a
session's in-memory history from its durable entry log
(`derive_state(store.entries(session_id))`, see `knot/core/session/state.py`).
A code path that appends a message to the in-memory harness without
persisting it causes *silent replay divergence*: the resumed session sees
different history than the live one did, and nothing notices until much
later, far from the cause — a resumed conversation quietly missing a turn,
or a repaired tool result that doesn't match what the model was actually
shown.

**How it's enforced:** the model-visible-logged invariant is asserted at
runtime, per provider request, for every persisted session. Before each
request, the check rehydrates the session's durable log and compares it
against the exact in-memory message list the request is about to be built
from, under the same canonical projection (`provider_context`) the request
itself uses — so a difference confined to provider-invisible fields (a
tool result's `details`, timestamps) never counts as a divergence, but any
in-memory-only content does. The check runs before the provider is called;
in `strict` mode it is a veto, not a report.

Implementation: `knot.core.invariant` (`canonical_history`, `diff_histories`,
`build_invariant_hook`). A bare `AgentHarness` built without a session
store never gets the hook — there's no durable log to diverge from, and
running the invariant would be a no-op. Every harness `AgentRuntime` builds
for a persisted session gets it automatically.

### Modes

| Mode | Behavior | Default in |
|---|---|---|
| `strict` | The run ends with an `error` outcome before any provider request is made, naming the divergence. | Tests and validation harnesses (every e2e scenario runs under `strict`). |
| `warn` | The divergence is logged (with the full structured report) and the run proceeds with the in-memory history. | `knot serve`. |
| `off` | No check runs at all — operational escape hatch for an emergency, deliberately undocumented for fleet authors. | Neither; must be set explicitly. |

`strict` by default in tests is what actually ratchets correctness: every
future change to the loop, the harness, or the runtime wiring runs under
it, so a change that introduces an in-memory-only injection fails its own
test suite rather than shipping. `warn` is the serving default because a
false positive in production must degrade to telemetry, not an outage.

### Configuring it

`knot serve` takes `--invariant-mode {strict,warn,off}`; the equivalent
environment variable is `KNOT_INVARIANT_MODE`. Precedence: `--invariant-mode`
wins over `$KNOT_INVARIANT_MODE`, which wins over the serving default
(`warn`). An unrecognized value from either source is a startup error, not
a silent fallback — a typo'd env var meant to set `strict` should never
quietly demote to `warn` without anyone noticing.

```bash
knot serve --root myfleet --db knot.db --invariant-mode strict
KNOT_INVARIANT_MODE=strict knot serve --root myfleet --db knot.db
```
