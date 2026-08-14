# Bundles

A bundle is a directory of tools, skills, and MCP connections shared by
whichever agents opt into it. Bundles are how a fleet avoids copy-pasting
the same tool file into ten different agent directories, and how a shared
external system (a CRM, a billing API) gets one place its approval policy
and connection config are declared. The [`examples/fleet/shared/crm`](../examples/fleet/shared/crm)
bundle is a complete, working example of everything in this document.

## Bundle anatomy

```
shared/<bundle_id>/
  bundle.yaml                 # REQUIRED
  tools/*.py                    # optional — same @tool contract as an agent's own tools/
  skills/<skill_id>/SKILL.md    # optional — see docs/skills.md
  snapshots/<connection>.json   # one per connection declared in bundle.yaml
```

`bundle.yaml` is missing → the bundle fails to compile as a whole (an error
against the bundle, not a warning); any agent that references it via `use:`
then fails to compile too, with a clear "bundle X failed to compile" error
rather than a partial/half-resolved bundle.

```yaml
description: >-
  Shared CRM tools for support-facing agents.
approvals:
  update_customer: always
  flag_account: once
connections:
  support_mcp:
    url: https://mcp.internal.example.com/support
    transport: streamable_http
    description: Internal support MCP server.
    allow:
      - add
    auth_env: SUPPORT_MCP_TOKEN
    provided_arguments:
      a: account_id
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `description` | `str \| null` | `null` | Free text, not otherwise used by the framework. |
| `approvals` | `dict[str, "never"\|"once"\|"always"]` | `{}` | Approval policy for this bundle's own tools and connection tools. Merged into an agent that `use:`s this bundle; an agent's own `approvals:` overrides a bundle's for the same key. |
| `connections` | `dict[str, ConnectionConfig]` | `{}` | Named MCP-style connections this bundle exposes. |

An agent pulls a bundle's tools, skills, and approvals in with:

```yaml
use:
  - crm
```

## Approvals vocabulary

Three named policies, evaluated by an agent's decision hook on every tool
call:

- **`never`** — always allowed, no park.
- **`always`** — every call requires a human approval; the run parks with a
  `tool_approval` request every single time.
- **`once`** — the *first* call to that tool name in a session parks and
  requires approval; every later call to the same tool name in the *same
  session* runs straight through, ungated. This is derived fresh from the
  session's durable entry log on every check (never cached in memory), so
  it survives a process restart with no special recovery logic — see
  [`docs/http-api.md`](http-api.md#state-semantics).

A tool with no matching policy anywhere defaults to `never`.

### Suffix matching for qualified names

A connection's tools are exposed to the model under a qualified name,
`<connection>__<tool>` (e.g. `support_mcp__add`). An approval key is
matched **exact-key first, then by `__`-qualified suffix**: writing

```yaml
approvals:
  add: always
```

gates *every* connection's `<connection>__add` the same way, without the
bundle author needing to know (or repeat) each connection's own name. This
is deliberately not a plain substring match — `add` matches
`support_mcp__add` but never, say, `bad__add`'s reversed cousin
`nonadd_tool` — the match only happens on a real `__` boundary. When more
than one approval key's suffix matches, the *longest* matching key wins.

## Connection declarations

Each entry under `connections:` is one MCP-style server:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `url` | `str` | — (required) | The MCP server's endpoint. |
| `transport` | `"streamable_http" \| "sse"` | — (required) | Only HTTP transports are supported — `stdio` is explicitly rejected at validation time with guidance to wrap it as an HTTP service first, since a stdio server has no meaningful way to be snapshotted or reached without a live subprocess. |
| `description` | `str` | `""` | Free text. |
| `allow` | `list[str] \| null` | `null` (every tool the server reports) | An allow-list of bare tool names (as the server itself reports them — never write the `connection__tool` qualification here; that's applied automatically). `null` exposes everything in the committed snapshot. |
| `auth_env` | `str \| null` | `null` (unauthenticated) | Name of an environment variable holding a bearer token. When set, every call attaches `Authorization: Bearer <value>`; a 401 evicts the cached token and retries once. When unset, no `Authorization` header is sent at all — the token resolver is never even called. |
| `provided_arguments` | `dict[str, str]` | `{}` | `{model-visible arg name: resolver key}`. Each key is **stripped from the tool's schema shown to the model** at compile time and injected by a resolver at execution time — the model never sees, and can never override, a provided argument. A key absent from a particular tool's own schema is only a warning (the same `provided_arguments` map is declared once per connection, but only some of its tools may actually accept that argument). |

In the example above, `support_mcp`'s `add` tool is allow-listed (so no
other tool on that server reaches the model at all), and its `a` argument is
provided automatically — the model-facing schema for `support_mcp__add`
only shows `b`.

## The snapshot lifecycle

Compiling a fleet **never touches the network** — a connection's tools come
exclusively from its committed snapshot file,
`shared/<bundle>/snapshots/<connection>.json`:

```json
{
  "connection": "support_mcp",
  "capturedAt": "2026-08-01T00:00:00Z",
  "tools": [
    {"name": "add", "description": "", "inputSchema": {"type": "object", "...": "..."}}
  ]
}
```

A missing or malformed snapshot is a compile *error* for that bundle, not a
fetch-and-continue. To (re)generate it, connect to the live server:

```
uv run knot refresh --root <fleet-root>                       # every connection in every bundle
uv run knot refresh --root <fleet-root> --bundle crm            # one bundle
uv run knot refresh --root <fleet-root> --bundle crm --connection support_mcp   # one connection
```

`refresh` connects, lists the server's live tools, intersects with `allow`,
overwrites the snapshot file, and reports what changed — tool names
**added**, **removed**, or **schema-changed** — plus, if anything did
change, which agents in the fleet are actually affected (any agent whose
compiled manifest carries a tool sourced from that connection). This is the
bundle-edit **blast radius**: before touching a shared connection, `refresh`
tells you exactly which agents need re-review.

Snapshot drift is distinct from **manifest** drift (`knot validate
--manifests <dir> --check`, see [`docs/authoring-agents.md`](authoring-agents.md#validating-a-fleet)):
a snapshot changing (a live server added a tool) only becomes visible as
manifest drift once you recompile — `refresh` doesn't rewrite committed
manifests itself. A separate, read-only check,
`knot.authoring.connections.check_connection_health`, reconnects and diffs
live tools against the committed snapshot without writing anything (surfaced
over HTTP as `GET /connections/health` — see
[`docs/http-api.md`](http-api.md)) — this is how a server operator monitors
for drift between deploys without running a full `refresh`.

## The no-ambient-inheritance model

Nothing is implicit. An agent gets exactly the tools, skills, and
connections it explicitly asks for:

- A bundle's tools/skills/approvals only reach an agent that lists it under
  `use:`.
- A subagent gets **nothing** from its parent automatically — not the
  parent's bundles, not its own tools, not its approvals. A subagent that
  needs the `crm` bundle declares `use: [crm]` itself, independently.
- A tool name collision — two capabilities (an agent's own tool, a used
  bundle's tool, a connection tool, a subagent's delegation entry, or a
  framework-floor tool) resolving to the same name — is always a hard
  compile error, never a silent override.

This means every agent's actual capability surface is fully readable from
its own `agent.yaml` plus the bundles it names — nothing reaches it by
sitting nearby in the filesystem.
