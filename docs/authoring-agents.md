# Authoring agents

This is the contract for writing an agent as a directory on disk: what
files and subdirectories `knot` recognizes, what every `agent.yaml` field
means and defaults to, how the `@tool` decorator turns a plain Python
function into a callable tool, how subagents work, and how to validate what
you've written before it ever runs. A complete, working example that uses
every feature described here lives in [`examples/fleet`](../examples/fleet)
— `agents/support` in particular is worth reading alongside this document.

## The agent directory contract

An agent is a directory; its id is the directory's own name, and every id
(agent, bundle, skill, subagent, tool) must match `^[a-z][a-z0-9_-]*$`
(lowercase, digits, `_`, `-`; must start with a letter).

```
agents/<agent_id>/
  instructions.md          # REQUIRED — the agent's system prompt
  agent.yaml                # optional — every field in it is optional too
  tools/*.py                  # optional — one @tool-decorated function per file
  skills/<skill_id>/SKILL.md  # optional — see docs/skills.md
  subagents/<subagent_id>/    # optional — a full agent directory, recursively
```

- **`instructions.md` is the only required file.** An agent with no
  `agent.yaml` at all still compiles — it just gets no model override, no
  bundles, no approvals, and default limits.
- **Unrecognized directories** at the agent root (anything other than
  `tools/`, `skills/`, `subagents/`) produce a compile warning and are never
  descended into. Unrecognized *files* are silently ignored (treat them as
  author notes).
- **`tools/*.py`** — every non-underscore-prefixed `.py` file directly
  inside `tools/` is imported and expected to define exactly one
  `@tool`-decorated function (see below). Files starting with `_` are
  skipped, so `tools/_helpers.py` full of shared code you `import` from
  your other tool files is fine.
- A subagent directory (`subagents/<id>/`) is a full agent directory in its
  own right — see [Subagents](#subagents) below.

## `agent.yaml` fields

Every field is optional. Unknown keys are a hard compile error (the schema
is strict) — a typo is caught immediately rather than silently ignored.

```yaml
description: Front-line customer support triage agent.
model:
  provider: anthropic
  name: claude-3-5-haiku-20241022
  max_tokens: 1024
  context_window: 200000
  thinking_budget_tokens: null
limits:
  max_turns: 12
  max_result_bytes: null
  max_session_tokens: null
  delegation_max_per_turn: 2
  delegation_max_concurrent: 1
use:
  - crm
approvals:
  update_customer: always
  refund_customer:
    policy: always
    ttl_seconds: 3600
compaction:
  enabled: true
  threshold_ratio: 0.8
  retain_budget: 0.16
  summarization_model: null
  max_overflow_retries: 1
repeat_guard:
  enabled: true
  thresholds: [3, 5, 8]
  exclude: [ask_user, load_skill]
  preview_cap: 500
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `description` | `str \| null` | `null` | For a top-level agent, shown by `GET /agents`. For a **subagent**, this is required (see below) and becomes the description of the delegation tool the parent model sees. |
| `model.provider` | `"anthropic" \| "openai" \| "openrouter" \| "litellm"` | — (required if `model:` is present) | Which provider serves this agent's sessions — `knot serve` routes each session's model requests to the named provider, parent and delegated child sessions alike (a child always uses its own agent's configured provider, never its parent's — never inherited). If this agent's manifest omits the whole `model:` block, its sessions use the serving default provider instead (see below). `knot serve` constructs exactly the providers referenced anywhere in the fleet, plus the serving default, at startup — a provider name it cannot construct (unknown name, or a construction failure such as missing credentials) fails startup with a diagnostic naming the agent and provider, rather than failing at the first request. |
| `model.name` | `str` | — (required if `model:` is present) | The model name/id passed to the provider. |
| `model.max_tokens` | `int \| null` | `null` | Passed through to the provider as the response-token ceiling on every call. |
| `model.context_window` | `int \| null` | `null` | The model's context window, in tokens — see [Context compaction](#context-compaction) below. Must be a positive integer if set. `null` doesn't disable compaction outright: knot falls back to a small built-in table of well-known model names (`knot.providers.capacity`); only an unmapped model name with no explicit override leaves capacity — and therefore the *proactive* trigger — unknown. |
| `model.thinking_budget_tokens` | `int \| null` | `null` | Enables extended thinking with this token budget on every call, for providers that support it. Today only `model.provider: anthropic` can honor it — setting it alongside any other provider is a compile error, not a silent no-op. |
| `limits.max_turns` | `int \| null` | `null` (unlimited) | Maximum number of assistant turns before the run stops. |
| `limits.max_result_bytes` | `int \| null` | `null` | Caps a tool result's serialized size; an oversized result is spilled — replaced with a bounded preview plus a retrieval notice — with truncation as the fallback. See [Oversized tool results (spill)](#oversized-tool-results-spill) below. |
| `limits.max_session_tokens` | `int \| null` | `null` (unlimited) | Maximum total token budget for the session: the sum of provider-reported input, output, and cache (read + write) tokens across *every* run of the session, derived from the durable entry log — so it survives a process restart and is unaffected by context compaction folding old messages behind a summary. Checked before each model request; once accumulated usage meets or exceeds the budget, the run ends with an error outcome naming the budget and the amount consumed, and no further provider request is made. Must be a positive integer if set. |
| `limits.delegation_max_per_turn` | `int` | `4` | How many delegation (subagent) calls this agent may make in a single turn; exceeding it fails the delegation call with an error instead of running it. |
| `limits.delegation_max_concurrent` | `int` | `2` | How many delegation calls may be in flight at once, enforced by a semaphore. |
| `use` | `list[str]` | `[]` | Bundle ids (directory names under `shared/`) whose tools, skills, and approvals this agent pulls in. See [`docs/bundles.md`](bundles.md). |
| `approvals` | `dict[str, "never"\|"once"\|"always" \| {policy, ttl_seconds}]` | `{}` | Per-tool approval policy overrides. Each entry is either the bare policy name shown above, or an object naming the policy plus an optional `ttl_seconds` (a positive integer) — see [Approval TTLs](#approval-ttls) below. **Agent-level approvals always win over a bundle's** — see [`docs/bundles.md`](bundles.md) for the full vocabulary and suffix-matching rule. |
| `compaction.*` | — | enabled, defaults below | Context compaction settings — see [Context compaction](#context-compaction) below for the full field list and validation rules. |
| `repeat_guard.*` | — | enabled, defaults below | Repeat-tool-call guard settings — see [Repeat-tool-call guard](#repeat-tool-call-guard) below for the full field list, exclusion semantics, and validation rules. |

If `model:` is omitted entirely, the session runs with the runtime's
`default_model` (a fallback the server operator configures, not part of
`agent.yaml`) and its serving default provider (see `model.provider` above).

## The `@tool` decorator

A tool file is exactly one Python function decorated with `@tool`:

```python
from typing import Annotated
from knot.authoring.tools import tool

@tool(idempotent=True)
def lookup_order(
    order_id: Annotated[str, "The order id to look up, e.g. 'ORD-1001'."],
) -> str:
    """Look up an order's current shipping status by its order id.

    Returns a short, human-readable summary.
    """
    ...
```

See [`examples/fleet/agents/support/tools/lookup_order.py`](../examples/fleet/agents/support/tools/lookup_order.py)
for the full working version.

- **The tool's name** is the file's stem (`tools/lookup_order.py` →
  `lookup_order`) unless overridden with `@tool(name="...")`. The resolved
  name must match the same `^[a-z][a-z0-9_-]*$` pattern as every other id.
- **Usable bare or with keyword arguments**: `@tool` and
  `@tool(idempotent=True, name="...")` both work.
- **One tool per file, defined (not merely imported) in that file.** Zero or
  more than one `@tool`-decorated function defined in the module is a
  compile error.
- **Schema derivation**: every parameter must carry a type annotation (bare
  untyped parameters are a compile error), and `*args`/`**kwargs` are
  rejected outright. A pydantic model is built dynamically from the
  function's signature (`extra="forbid"`, so the model cannot send
  unexpected keys), and its `model_json_schema()` becomes the tool's
  parameter schema. A parameter with no default is required; one with a
  plain Python default is optional.
- **`Annotated[T, "description"]`** attaches that string as the parameter's
  JSON-schema `description` — this is the primary way to describe an
  individual argument to the model. A `pydantic.Field(default=..., description=...)`
  default works the same way if you need more control (e.g. a non-string
  default).
- **The tool's own description** comes from the *first paragraph* of the
  function's docstring (text up to the first blank line), not the whole
  docstring.
- **`idempotent`** (default `False`) is carried through to the compiled
  manifest verbatim (`ManifestTool.idempotent`) for HITL crash-window
  repair to consume: after a restart, an approved-and-executed-but-crashed
  call to an idempotent tool can be safely re-run automatically, where a
  non-idempotent one requires an operator to supply the missing result by
  hand. It has no other runtime effect.
- **Execution**: an `async def` tool function runs directly; a plain `def`
  function runs in a worker thread (`asyncio.to_thread`) so it never blocks
  the event loop. A raised exception (including argument validation
  failure) becomes an error tool result — it never crashes the run.

### Execute-less tools ("parking" by construction)

An `@tool` function you write always gets a real executor — there is no way
to author a tool that parks directly. Three things in the framework
itself compile *execute-less* (`execute_fn=None`), and a call to any of them
always parks the run as a pending input request rather than executing
anything:

- **`ask_user`** — a framework-floor tool every agent gets automatically
  (see below); a call parks as a `question`.
- **A connection tool** (from an MCP-backed bundle connection) — execute-less
  at compile time; the HTTP server layer wires it a real executor when a
  session actually runs. See [`docs/bundles.md`](bundles.md).
- **A subagent's delegation tool** — likewise execute-less at compile time;
  wired to a real delegation executor at harness-assembly time. A call
  parks only if the *child session itself* parks or doesn't finish
  synchronously (kind `child_session`) — otherwise it returns the child's
  answer directly, in the same turn.

Every compiled agent also automatically gets `ask_user` (always),
`read_tool_output` (always — see [Oversized tool results (spill)](#oversized-tool-results-spill)
below), and, iff it has at least one skill, `load_skill` (see
[`docs/skills.md`](skills.md)) — these are the "framework floor": tools
present without being authored, and subject to the same name-collision
check as everything else.

## Oversized tool results (spill)

When a tool's result text exceeds `limits.max_result_bytes`, knot doesn't
drop the excess — it *spills* it. The model-facing result is replaced with
a bounded head/tail preview of the original text plus a short notice naming
how many bytes were omitted and how to retrieve them; the full original
text is stored durably, keyed to the tool call that produced it, and stays
retrievable across a process restart for as long as the session exists
(removed with it). Results within the cap, and results with no text content
(e.g. images), pass through unchanged.

Every compiled agent automatically gets the `read_tool_output` tool
(framework floor, alongside `ask_user`) to retrieve spilled content:

```
read_tool_output(ref, offsetBytes?, limitBytes?, pattern?)
```

- **`ref`** — the retrieval reference named in the spill notice (the
  originating tool call's own id).
- **`offsetBytes` / `limitBytes`** — page through the stored text as a raw
  byte slice; `limitBytes` is clamped to a hard ceiling (16KB by default).
- **`pattern`** — search the stored text with a Python regular expression
  instead of paging; returns up to 20 matches, each with a byte offset and
  a small context window, so a match can be paged to directly with a
  follow-up `offsetBytes` call.
- An unknown `ref` (not one from this session) returns an ordinary error
  result naming the ref — never an unhandled exception.

`read_tool_output`'s own results are never themselves spilled — its output
is already bounded by its paging parameters, so there's no
spill-retrieve-spill loop.

If storing the spilled content fails, or if even a notice-only replacement
can't fit within `max_result_bytes`, knot falls back to the older,
destructive truncation behavior for that one result (a warning is logged on
a storage failure) — a spill-storage failure never turns a successful tool
call into an error.

**Authoring note**: there is currently no author-facing way to mark your
own `@tool` as exempt from spilling. `spill_exempt` exists as an internal
`AgentTool` field (set on `read_tool_output` itself, to prevent the
spill-retrieve loop above), but it is not a parameter the `@tool` decorator
accepts — an authored tool's results are always eligible for spilling.

## Approval TTLs

Each entry in `approvals` may be the bare policy name (`never`/`once`/`always`,
unchanged from before) or an object form that adds a TTL:

```yaml
approvals:
  update_customer: always              # bare form: never expires
  refund_customer:
    policy: always
    ttl_seconds: 3600                  # object form: expires after an hour
```

`ttl_seconds` (a positive integer, seconds) is carried onto every pending
approval request that tool's policy parks. The expiry mechanism itself isn't
new — knot already lazily sweeps overdue pending requests — this just makes
it reachable from configuration instead of a custom decision hook. If a
`ttl_seconds`-bearing request is still unresolved once its TTL elapses, it is
durably denied with an expiry reason fed back to the model as the tool's
result, exactly as if a human had denied it; a response that arrives after
expiry is rejected, never silently applied. A bare-form entry (or an object
form with `ttl_seconds` omitted) never expires on its own, matching today's
behavior. The fleet-wide `GET /approvals` inbox reports each pending
request's configured TTL alongside its age — see [`docs/http-api.md`](http-api.md).

## Context compaction

A session that lives long enough will eventually accumulate more history
than its model's context window can hold. knot handles this by durably
replacing the oldest span of a session's conversation with a single
model-generated summary message, rather than letting every subsequent run
hard-fail with a provider error. It is on by default — an agent with no
`compaction:` block still gets it, under the defaults below.

```yaml
compaction:
  enabled: true
  threshold_ratio: 0.8
  retain_budget: 0.16
  summarization_model: null
  max_overflow_retries: 1
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | `bool` | `true` | Turns compaction off entirely for this agent (both the proactive and reactive paths) when `false`. |
| `threshold_ratio` | `float`, `(0, 1]` | `0.8` | Proactive trigger: compact when the most recent provider-reported context usage (`input + cacheRead + cacheWrite` of the latest assistant response) is at or above `threshold_ratio × model.context_window`. |
| `retain_budget` | `float`, `(0, 1)` | `0.16` | The fraction of `model.context_window` to keep as a verbatim tail when compacting — an estimated budget (see below), not an exact count. **Must be strictly less than `threshold_ratio`** — otherwise the retained tail alone could already exceed the trigger threshold, immediately re-triggering compaction on the very next turn. Compile fails if it isn't. |
| `summarization_model` | `str \| null` | `null` | Override the model used for the summarization call itself. `null` (the default) reuses the session's own model. |
| `max_overflow_retries` | `int`, `>= 0` | `1` | How many times the reactive path (below) will compact-and-retry a single context-overflow failure before giving up and surfacing the original error. |
| `summarization_max_tokens` | `int`, `> 0` | `8192` | Accepted for forward compatibility with a future per-call token-limit parameter on the provider layer; not yet enforced (every provider adapter bakes its ceiling in at construction time today). |

Invalid values fail fleet compilation with a diagnostic naming the
offending field (`knot validate` reports it like any other compile error)
— an out-of-range `threshold_ratio`, a non-numeric `max_overflow_retries`,
or `retain_budget >= threshold_ratio`.

### When compaction runs

Two independent triggers, both governed by the same config block:

- **Proactive** — checked between turns, before the next provider request:
  if the latest assistant response's reported context usage crossed
  `threshold_ratio × model.context_window`, compaction runs first, and that
  next request is built from the compacted history instead. Needs
  `model.context_window` (explicit or resolved from the built-in defaults
  table) to be known; if it isn't, the proactive trigger is simply
  disabled for that agent — the reactive path still protects the session.
- **Reactive** — a provider request that fails with a context-overflow
  error (classified from the provider's own error shape, not guessed from
  text) is compacted-and-retried automatically instead of surfacing the
  error, up to `max_overflow_retries` times. See `docs/http-api.md`'s
  [Context compaction](http-api.md#context-compaction) section for exactly
  what a client sees on the event stream when this happens.

A compaction that would not actually shrink the history — the generated
summary plus the retained tail turns out at least as large as the span it
replaces — is rejected outright: the conversation surface is left
untouched, and (on the reactive path) the original provider error is what
surfaces. A summarization call that itself errors, or returns an empty or
tool-call-containing response, is rejected the same way. Either way,
nothing is ever silently lost — see [The entry log keeps everything](#the-entry-log-keeps-everything)
below.

### What the model sees

Compaction replaces the oldest contiguous span of messages with one
summary message, and always keeps a recent tail verbatim — sized by
`retain_budget`, estimated conservatively from each message's own length
(a cheap `len(text) // 4` proxy, or the provider's own reported output-
token count where one is available), never an exact token count. That
estimate only ever decides *where* to cut; whether the result was actually
small enough is settled by the next request's real, provider-reported
usage.

The boundary itself never splits an assistant message from the tool
results answering it, and never lands after the most recent user turn, so
a compaction can't cut a conversation off mid-exchange.

The summary itself enters the model's context as an ordinary message — a
`user`-role turn whose text is wrapped in `<compacted-summary>...
</compacted-summary>` tags, so the model can tell it is reading a summary
of earlier conversation rather than something the human user actually
typed:

```
<compacted-summary>
The user asked about order ORD-1001; it shipped via UPS and is expected to
arrive 2026-08-16. No further action was requested.
</compacted-summary>
```

The summarization call that produces this text replays the session's own
system prompt, tool schemas, and the exact messages being compacted (plus
any prior summary, if this isn't the session's first compaction), with the
summarize instruction appended as one final message — matching the shape
of a real request lets the provider serve it from its own warm prefix
cache, so compacting doesn't mean paying to re-ingest the whole history a
second time.

### The entry log keeps everything

Compaction is an append-only projection change, not a rewrite. The
underlying `"message"` entries a compaction replaces are never deleted or
modified in the session's durable entry log — only the *provider-visible*
history (what the model actually sees on the next request) changes. A
durable `"compaction"` entry records which span was covered and the
summary that replaced it; reading a session's raw entries (an export, a
fleet query, or just `SessionStore.entries`) after compaction still shows
every original message, in full, exactly as it was written, alongside that
one compaction entry — a complete audit trail survives regardless of how
much of the model-visible history has been summarized away. Rehydrating a
compacted session after a restart derives exactly the same post-compaction
history the live session had — same durability guarantee as every other
fact knot records.

## Repeat-tool-call guard

An unattended model that gets stuck re-issuing the same tool call with the
same arguments burns tokens and turns until `max_turns` kills the run. The
repeat-tool-call guard breaks these loops early by injecting escalating
advisory messages — it is on by default, and never blocks, delays, or
rewrites a call.

```yaml
repeat_guard:
  enabled: true
  thresholds: [3, 5, 8]
  exclude: [ask_user, load_skill]
  preview_cap: 500
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | `bool` | `true` | Turns the guard off entirely for this agent when `false`. |
| `thresholds` | `list[int]` | `[3, 5, 8]` | Consecutive-call counts at which an advisory fires. Normalized ascending on read, so the *lowest* value is always what decides "first vs. later" advisory wording (see below), regardless of the order written in `agent.yaml`. |
| `exclude` | `list[str]` | `[ask_user, load_skill]` | Tool-name patterns that are transparent to the chain — see below. |
| `preview_cap` | `int`, `> 0` | `500` | Maximum characters of the repeated call's canonicalized arguments shown in a later-threshold advisory. Bounds only the advisory *text*; detection always compares the full arguments regardless of this cap. |

Invalid values fail fleet compilation with a diagnostic naming the
offending field (`knot validate` reports it like any other compile error):
an empty `thresholds` list, a non-integer or boolean entry, a threshold
below `2`, duplicate thresholds, or a non-positive `preview_cap`. An agent
with no `repeat_guard:` block at all still gets the guard, enabled, at the
default thresholds.

### Detection: consecutive identical calls

The guard tracks, per live run, consecutive tool calls sharing the same key
of `(tool name, canonicalized arguments)` — canonicalization is a deep
key-sort plus compact JSON serialization, so two argument objects differing
only in property order count as identical. A call with a different key
resets the running count to 1. Calls are counted in the exact order the
model emitted them, including calls a decision hook denies and calls naming
an unknown tool — a model hammering a call that keeps getting denied is
exactly the loop worth breaking, so denial doesn't exempt a call from the
count.

### Exclusions are transparent, not exempt

`exclude` entries match tool names with the same `*`-wildcard and
`__`-qualified-suffix convention `approvals` uses (see
[`docs/bundles.md`](bundles.md)) — so `exclude: [load_skill]` also covers a
connection-qualified `crm__load_skill` without spelling out
`*__load_skill`. A call matching an exclusion pattern is *transparent* to
the chain: it neither increments nor resets the running count, so
interleaving an excluded call between two identical tracked calls can't
launder a loop — `search("x")`, `load_skill(...)`, `search("x")` still
counts as two consecutive `search` calls, not a reset-and-restart. The
default exclusions cover knot's own bookkeeping surface: `ask_user`'s
repetition is already governed by parking, and a repeated `load_skill`
fetch is near-identical by design and harmless.

### What the model sees

When a consecutive run reaches a configured threshold, the guard queues an
advisory and delivers it as an ordinary `user`-role message at the *next*
turn start, through the same append+emit path steering messages use —
appended to history, emitted as `MessageStart`/`MessageEnd` events, and
logged to the entry log exactly like a steering message. Its text is
wrapped in `<repeat-tool-reminder>...</repeat-tool-reminder>` tags, the
same way a compaction summary is wrapped in `<compacted-summary>` tags, so
the model can tell it's reading framework-authored guidance rather than
something the human user actually typed — providers still see it as
ordinary user text.

The **first** (lowest) configured threshold gets a short, generic nudge: go
re-read the previous result, and either try a different approach or
conclude. **Later** thresholds name the tool, the consecutive count, and a
`preview_cap`-bounded preview of the repeated canonical arguments, so the
model has enough context to actually change course:

```
<repeat-tool-reminder>
You have now called `search` with identical arguments 5 times in a row.
Carefully analyze the previous result before calling again: if the task is
not complete, try a different approach or different arguments, or conclude
instead of repeating the call. Repeated arguments: {"q":"cats"}
</repeat-tool-reminder>
```

Each configured threshold fires **at most once per run** — crossing the
same threshold again later in the same run (after the chain resets and
regrows past it) does not re-nag. The call that crosses a threshold still
executes (or is denied) exactly as it would without the guard; the guard
never inspects, delays, or alters a result.

### Chain resets

A new user message, a steering message, or a follow-up message entering the
conversation resets the running count to zero — the chain only ever tracks
one *uninterrupted* stretch of identical tool calls. Thresholds already
fired this run stay fired, though: the once-per-threshold rule survives a
reset, so a chain that resets, regrows, and crosses the same threshold
again does not advise twice.

### Chain state is per-run; advisories are durable history

The consecutive-call counter itself is in-memory and scoped to one live
run — a session that parks mid-run and later resumes starts counting from
zero again on its first tracked call after resume. This is a heuristic
nudge, not a logged invariant: the framework doesn't reconstruct chain
state from the entry log the way it reconstructs conversation history.
Advisories *already delivered*, however, are ordinary durable `"message"`
entries like any other model-visible input — they survive a restart in
their original position in the session's history, same as any other turn.

## Subagents

A subagent is a full agent directory nested under `subagents/<id>/` — its
own `instructions.md`, its own `agent.yaml`, its own `tools/`, `skills/`,
and even its own nested `subagents/`. See
[`examples/fleet/agents/support/subagents/researcher`](../examples/fleet/agents/support/subagents/researcher).

- **`description` is required** in a subagent's own `agent.yaml`. If it's
  missing, the *parent's* compile fails (not just the subagent's) — the
  description becomes the delegation tool's description, i.e. what the
  parent model reads to decide whether and how to delegate. Write it as
  instructions for the calling model: what the subagent is for, and what to
  put in the one message it receives.
- **The `{message}` contract**: every delegation tool has exactly one
  schema, everywhere — a single required string field, `message`. The
  parent packs its whole request into that one string; the subagent
  receives it as its own first (and only) user turn.
- **No inheritance.** A subagent gets nothing from its parent automatically
  — not tools, not skills, not `use:` bundle references, not approvals. If
  a subagent needs a bundle's tools, it declares its own `use:`. It does
  still get its own independent framework floor (its own `ask_user`, and
  its own `load_skill` if it has skills).
- Identity is scoped to the parent: a subagent id only has to be unique
  within its own parent, not fleet-wide, since two different agents may
  each have their own, unrelated `researcher` subagent.

## Authoring recipes: typed verdicts and context-threading

Two recipes for a common shape of multi-agent design: a critic/judge
subagent that needs to report a structured outcome, and a producer/critic
loop that needs more than one round.

### Typed verdicts: structured critic/judge outcomes

When an author needs a structured outcome from a critic/judge/reviewer
agent — "approve vs. revise, with reasons", say — the knot-native pattern
is the same `@tool` mechanism described [above](#the-tool-decorator): a
tool whose typed `Annotated` args *are* the schema, which the critic must
call to report its verdict.

```python
from typing import Annotated, Literal
from knot.authoring.tools import tool

@tool
def submit_verdict(
    decision: Annotated[Literal["approve", "revise"], "Whether the draft is ready to ship."],
    reasons: Annotated[str, "Why — enough detail for the parent to act on a 'revise' verdict."],
) -> str:
    """Record this review's verdict.

    Call this exactly once, as your last action — do not describe the
    verdict in prose instead.
    """
    return f"Verdict recorded: {decision}"
```

A critic subagent's `instructions.md` tells it to call `submit_verdict`
exactly once, as its final action, rather than describing its verdict in
free text. Because the call is validated against the tool's pydantic-derived
schema before it's even accepted, the resulting tool-call entry — `decision`
and `reasons` as typed, already-valid fields — is itself the machine-readable
verdict: visible in the transcript like any other durable entry (see
[The entry log keeps everything](#the-entry-log-keeps-everything)), and
directly parseable by whatever reads the child's session afterward, with no
separate encoding step.

Contrast this with the fragile alternative: a magic string buried in free
text, e.g. checking whether the child's answer contains the literal
substring `"CODE_IS_PERFECT"`. That approach is brittle exactly where the
tool form is solid — a model can phrase its answer differently, misspell or
punctuate around the magic string, or produce prose that happens to
*contain* it in a negated sentence ("this is **not** CODE_IS_PERFECT"), and
every one of those is a silent misparse rather than a validation error. The
tool form has no parsing step to get wrong: the schema is enforced up front
by the same mechanism as any other `@tool`, the result is durably logged
the moment the call is made, and there is nothing for a caller to
re-extract from prose.

### Context-threading: the recipe for multi-round loops

Delegation in knot is deliberately memoryless: as [Subagents](#subagents)
above describes, every delegation call creates a brand-new child session
and is a pure function of its single `{message}` string — a child holds no
state between calls, and there is no way to resume a *finished* child with
more input.

This is a design decision, not a missing feature. Multi-round patterns —
producer/critic, generator/judge, draft/revise — are expressed by the
**parent** threading context explicitly: each new `{message}` includes
whatever prior state the next round actually needs (the prior draft, the
prior verdict, and so on). Two things fall out of this that a hidden-memory
child wouldn't give you for free:

- **Every delegation call's args are the child's full input, in the log.**
  Because nothing is implicit, reading a single tool-call entry for a
  delegation tells you exactly what that child session knew when it ran —
  no need to reconstruct state from a chain of prior calls to understand
  any one of them.
- **Prompt caching absorbs most of the cost of re-sending context.** Each
  round's `{message}` repeats content from earlier rounds verbatim (the
  same draft text, the same brief), which is exactly the shape a provider's
  prompt cache is good at — the token *cost* of re-threading context is
  much smaller than its size on the wire suggests.

A compact worked example — instructions to a parent agent running a
draft → critique → revise loop against a `writer` subagent and a `critic`
subagent (the one built around `submit_verdict` above):

```
Round 1 — draft:
  Delegate to `writer` with message:
    "Write a 200-word product description for <product>.
     Constraints: <...>."
  → holds draft_v1.

Round 2 — critique:
  Delegate to `critic` with message:
    "Review this draft against the brief below and call submit_verdict.

     Brief: <...>

     Draft:
     <draft_v1>"
  → holds verdict.decision, verdict.reasons.

Round 3 — revise (only if verdict.decision == "revise"):
  Delegate to `writer` with message:
    "Revise this draft to address the feedback below.

     Original brief: <...>

     Previous draft:
     <draft_v1>

     Reviewer feedback:
     <verdict.reasons>"
  → holds draft_v2.
```

Each round's message is self-contained: the child never needs to have
"remembered" anything from a prior round, because the parent hands it
everything it needs, every time.

## Validating a fleet

```
uv run knot validate --root <fleet-root>
```

Compiles every agent and bundle under the root and prints one diagnostic
block per agent/bundle that has any (errors first, then warnings), then a
summary line and exit code:

- **0** — every agent compiled.
- **1** — at least one agent failed to compile (always wins over drift).
- **2** — only reachable with `--manifests <dir> --check` (see below): the
  fleet compiled cleanly, but at least one committed manifest is stale.

Add `--manifests <dir>` to also diff each agent's freshly compiled manifest
against a committed `<dir>/<agent_id>.json`; add `--write` to persist the
fresh manifests there. `--check` requires `--manifests` and promotes any
drift to exit code 2 — this is what CI runs (see the repo's
`.github/workflows/ci.yml`) to catch an agent whose compiled shape changed
without its committed manifest being regenerated:

```
uv run knot validate --root examples/fleet --manifests examples/manifests --write   # regenerate
uv run knot validate --root examples/fleet --manifests examples/manifests --check   # verify clean
```

A single agent's (or bundle's) failure never stops the rest of the fleet
from compiling — diagnostics are collected per agent/bundle, and only an
agent/bundle with an actual error diagnostic is excluded from the compiled
result.
