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
limits:
  max_turns: 12
  max_result_bytes: null
  delegation_max_per_turn: 2
  delegation_max_concurrent: 1
use:
  - crm
approvals:
  update_customer: always
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `description` | `str \| null` | `null` | For a top-level agent, shown by `GET /agents`. For a **subagent**, this is required (see below) and becomes the description of the delegation tool the parent model sees. |
| `model.provider` | `"anthropic" \| "openai" \| "openrouter" \| "litellm"` | — (required if `model:` is present) | Which provider family this agent's model belongs to. In v0, `knot serve` talks to every session through one shared provider instance regardless of this field — it is informational/forward-looking, not yet a per-agent provider switch. |
| `model.name` | `str` | — (required if `model:` is present) | The model name/id passed to the provider. |
| `model.max_tokens` | `int \| null` | `null` | Passed through to the provider on every call. |
| `limits.max_turns` | `int \| null` | `null` (unlimited) | Maximum number of assistant turns before the run stops. |
| `limits.max_result_bytes` | `int \| null` | `null` | Caps a tool result's serialized size; oversized results are truncated. |
| `limits.delegation_max_per_turn` | `int` | `4` | How many delegation (subagent) calls this agent may make in a single turn; exceeding it fails the delegation call with an error instead of running it. |
| `limits.delegation_max_concurrent` | `int` | `2` | How many delegation calls may be in flight at once, enforced by a semaphore. |
| `use` | `list[str]` | `[]` | Bundle ids (directory names under `shared/`) whose tools, skills, and approvals this agent pulls in. See [`docs/bundles.md`](bundles.md). |
| `approvals` | `dict[str, "never"\|"once"\|"always"]` | `{}` | Per-tool approval policy overrides. **Agent-level approvals always win over a bundle's** — see [`docs/bundles.md`](bundles.md) for the full vocabulary and suffix-matching rule. |

If `model:` is omitted entirely, the session runs with the runtime's
`default_model` (a fallback the server operator configures, not part of
`agent.yaml`).

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

Every compiled agent also automatically gets `ask_user` (always) and, iff it
has at least one skill, `load_skill` (see [`docs/skills.md`](skills.md)) —
these are the "framework floor": tools present without being authored, and
subject to the same name-collision check as everything else.

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
