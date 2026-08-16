# Add repeat-tool-call guard

## Why

Knot agents are designed to run unattended (fleet server, parked sessions, subagent chains), and an unattended model that gets stuck re-issuing the same tool call with the same arguments burns tokens and turns until `max_turns` kills the run — with nothing in the loop today that even notices. A cheap advisory guard breaks these loops early without ever blocking a legitimately repeated call.

## What Changes

- Track, per live run, consecutive tool calls with the same `(tool name, canonicalized arguments)` key — canonicalization is a deep key-sort plus JSON serialization, so argument objects differing only in property order count as identical.
- At configurable run-length thresholds (default `[3, 5, 8]`), inject an escalating advisory message into the conversation telling the model it is repeating itself and should re-read the last result, change approach, or conclude. The first threshold gives a short generic nudge; later thresholds name the tool, the run length, and a bounded preview of the arguments.
- The guard is advisory only: it never denies, delays, or rewrites a call, and it is invisible in the tool list. The decision to stop stays with the model.
- Chain semantics:
  - Excluded tools (configurable; e.g. bookkeeping tools) are *transparent* to the chain — they neither increment nor reset the counter, so interleaving them cannot launder a loop.
  - Denied calls count toward the chain: a model hammering a call the decision hook keeps denying is exactly the loop worth breaking.
  - A new user/steering message resets the chain.
- The injected advisory is a model-visible message and therefore flows through the normal entry log (per knot's model-visible-means-logged discipline) — no new entry type needed.
- State is in-memory per run: a resumed session starts with a fresh chain. The guard is a heuristic nudge, not a durable invariant.
- Configuration in `agent.yaml` (thresholds, exclude list, arguments-preview cap) with validation failing loud at compile time; sensible defaults make it on by default.

## Capabilities

### New Capabilities

- `repeat-tool-guard`: the chain key and canonicalization rule, threshold/escalation behavior, transparency and reset semantics, advisory delivery as a logged message, and the configuration surface.

### Modified Capabilities

<!-- none -->

## Impact

- `knot/core/loop.py`: chain tracking in the tool phase; advisory injection alongside tool results.
- `knot/authoring/config.py` / `compile.py`: optional `repeat_guard` block in `agent.yaml`, validated at compile time.
- Docs: `docs/authoring-agents.md`.
- No breaking changes; no API surface changes — the advisory rides existing message events.
