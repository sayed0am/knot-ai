# Design: repeat-tool-call guard

## Context

See proposal.md — Why. Relevant current state:

- The loop's tool phase (`_run_tool_phase` in `knot/core/loop.py`) sees every call in model order, including denials and unknown-tool resolutions — one place already observes exactly the stream the chain is defined over.
- The loop already has a between-turns injection mechanism: `get_steering_messages` delivers messages that are appended to history and emitted as `MessageStart/End` events, which persistence turns into durable `"message"` entries.
- Steering delivery and turn boundaries are loop-owned, so "reset on new human input" aligns with a seam the loop already has.

**Landing order**: last of four (invariant → spill → compaction → **guard**). It depends on the invariant being strict in tests — the injected advisory is precisely the "new model-visible input" case the invariant exists to police, and landing it last proves the rule works for feature authors. It benefits from (but does not require) spill/compaction being in first, since those remove the pathological-context conditions that make models loop in the first place.

## Goals / Non-Goals

**Goals:**

- Detection and injection entirely inside the core loop with zero new subsystems — knot has no plugin bus to hang this on (dsh's version is a `tools/post-execute` listener; knot's equivalent altitude is loop code).
- The advisory is an ordinary durable message: no new entry type, no event-grammar change.

**Non-Goals:**

- Blocking or rate-limiting repeated calls (polling loops are legitimate; the model decides).
- Durable chain state across park/resume (a heuristic nudge, not a logged invariant — dsh reached the same conclusion).
- Detecting *semantic* repetition (similar-but-not-identical calls); only exact canonical identity is claimed.
- Fleet-level loop analytics (observable later from the entry log if wanted).

## Decisions

### D1: Chain tracking lives in `run_agent_loop`; config arrives as a value object

A small `RepeatGuardConfig` (thresholds, exclusion patterns, preview cap, enabled) travels `agent.yaml` → compile → manifest → `AgentHarnessConfig` → loop parameter, like `max_result_bytes` does today. The loop keeps one mutable chain record `(key, count, fired_thresholds)` per run. No registry, no hook: this is core behavior of the tool phase, and knot's philosophy is that the fixed grammar owns fixed behaviors.

- *Alternative rejected — a `ToolDecisionHook` wrapper*: the decision hook is a policy seam answering allow/deny/approve per call; the guard is not a decision (it never changes one) and needs post-phase injection the hook cannot express. Overloading the policy seam would blur its contract.

### D2: Canonical key = deep key-sort + compact JSON of `(name, arguments)`

`json.dumps(arguments, sort_keys=True, separators=(",", ":"))` prefixed by the tool name. Matches dsh's rule (property order never defeats detection); full canonical string is always compared (the preview cap bounds only advisory text).

- *Concurrency note*: knot executes allowed calls concurrently within one assistant message, but the chain is defined over *model-emitted call order* (`assistant.tool_calls` order), which is deterministic regardless of execution interleaving. Counting happens in the decision sweep at the top of `_run_tool_phase` — which also gives denials and unknown-tool calls their tick for free (spec: denied calls count).

### D3: Advisory injection rides the existing steering path

When a threshold fires during a tool phase, the loop appends the advisory as a `UserMessage` whose text is wrapped in `<repeat-tool-reminder>` tags, delivered through the same append+emit path steering messages use at the next turn start — history append, `MessageStart/End` events, persistence to a `"message"` entry, invariant-green by construction.

- *Why a tagged `UserMessage` and not a new message/entry type*: providers accept it unchanged (wire compatibility), rehydration needs no new case, and the tag satisfies the spec's "distinguishable from human words" the same way `<compacted-summary>` does for compaction. A new role would touch every provider adapter for one advisory.
- *Reset rule placement*: the chain resets wherever a message with a non-guard user origin enters the run (prompts, steering, follow-ups) — implemented at the loop's existing message-admission points, so the rule can't drift from delivery.

### D4: Escalation texts are fixed framework copy

First threshold: the short generic nudge; later thresholds: tool name + count + head-truncated canonical-args preview (`preview_cap` chars, default 500, with an omitted-count marker). Copy lives beside the loop as constants — not configurable per agent in v1 (configurable thresholds already tune sensitivity; configurable prose invites prompt-injection-shaped review burden in bundles).

### D5: Exclusions are name patterns resolved per call, defaulting to knot's own bookkeeping surface

`exclude` entries support `*` wildcards over qualified tool names (matching the approval-policy naming convention, e.g. `crm__*`). Patterns are predicates at call time, not registry references — a pattern matching nothing is valid. Default exclusion: `ask_user` (its repetition is already governed by parking) and `load_skill` (a legitimate repeated fetch is near-identical by design and harmless). Excluded = transparent (no increment, no reset), per the spec's laundering rule.

## Risks / Trade-offs

- [Advisory noise on legitimate repetition (pollers, retry-after-transient-error)] → Advisory-only by contract; thresholds start at 3; per-threshold `fired_thresholds` set means each fires once per run; exclusions cover known-legit repeaters. Cost of a false fire is ~2 sentences of context.
- [Chain resets on park/resume let a persistent loop restart clean] → Accepted (matches dsh): a loop that spans parks is being interrupted by approvals/questions — a human is already in it. Durable chains would need log-derived reconstruction for marginal value.
- [Advisory grows context in an already-looping session] → Bounded preview cap; and a looping session is burning full tool-call turns per iteration — two sentences that can end the loop are cheap against that.
- [Tag collision if a tool result legitimately contains `<repeat-tool-reminder>`] → The tag marks only guard-authored `UserMessage`s; nothing parses it back out of arbitrary content. Cosmetic risk only.

## Migration Plan

Pure addition: config block + loop logic + injection copy in one change, default-on. Existing sessions unaffected (no schema, no new entry type). Rollback = `repeat_guard: {enabled: false}` per agent, or revert; delivered advisories are ordinary history either way.

## Open Questions

- Whether `flag_account`-style `once`-gated tools should be default-excluded too (their repetition is throttled by approval anyway) — tune the default list from example-fleet experience; the mechanism is unaffected.
