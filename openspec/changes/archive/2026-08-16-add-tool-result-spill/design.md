# Design: tool-result spill

## Context

See proposal.md — Why. Relevant current state:

- `truncate_tool_result` (`knot/core/truncation.py`) is the single choke point: called from `execute_tool` (`knot/core/tools.py`) for every executed call, loop or standalone-resume alike. It already stashes the full text under `details["full_content"]` — i.e. today the full blob is *persisted and streamed* but *model-inaccessible*: exactly backwards.
- Framework tools (`ask_user`, `load_skill`) are injected per agent at compile/runtime assembly (`knot/authoring/compile.py` / `runtime.py`) — the retrieval tool follows the same pattern.
- The session store is one embedded SQLite file whose stated design goal is "no database server, one file holds every session" (`knot/core/session/store.py`).

**Landing order**: second of four (invariant → **spill** → compaction → guard). Depends on the invariant change being in: spilled results alter what is model-visible, and the strict-mode e2e suite is what proves the preview+reference (not the full text) is the logged, replayed fact. Compaction lands after spill because spill shrinks the surface compaction has to manage — tool output is the dominant context consumer.

## Goals / Non-Goals

**Goals:**

- One bounded representation on every surface (provider context, SSE, entry log) with model-driven recovery of the rest.
- Keep the single-file durability story: spill contents live in the same SQLite database, cleaned up with their session.
- Strictly reduce today's payload sizes (drop `full_content` from `details`).

**Non-Goals:**

- Spilling non-text content (images pass through untouched, as today).
- Recovering content a *tool itself* already truncated before returning (only what reaches the result boundary can be kept).
- A general artifact/file store for agents. This is one table with one purpose; a future workspace feature is separate.
- Cross-session retrieval (references resolve within the owning session only — subagents retrieve from their own results).

## Decisions

### D1: Spill storage is a table in the existing session SQLite store

A `spills` table: `(session_id, tool_call_id, content TEXT, original_bytes, created_at)`, primary key `(session_id, tool_call_id)`, deleted by session-deletion alongside entries. The retrieval reference in the notice is simply the originating `tool_call_id` — the model already sees those ids, they are unique per session, and no new id scheme is needed.

- *Why SQLite over per-session files (dsh's choice)*: dsh spills to files because its agents hold filesystem tools that can `read`/`grep` a path — the locator doubles as an ordinary path. Knot agents have no ambient filesystem tools, so a path is not directly actionable; a dedicated retrieval tool is needed either way, at which point files would only add a second durability domain, permission/symlink hardening (dsh needed 0700 dirs + `wx` opens), path portability across restarts/hosts, and an orphan-cleanup lifecycle. SQLite gives transactional writes, session-scoped cleanup by construction, and preserves knot's one-file operational story. Multi-MB TEXT values are comfortably within SQLite's limits.
- *Why not a new entry type in the log*: entries are the replayed conversation record; a spill blob is referenced data, not a durable *event* — putting it in the log would make rehydration carry megabytes it must then ignore, and would re-create today's bloat.

### D2: Spill happens at the truncation choke point via an injectable sink

`truncate_tool_result` grows into `bound_tool_result(result, max_bytes, spill_sink)` at the same call site in `execute_tool`. `spill_sink` is an optional callable `(tool_call_id, text) -> str-reference | None`; the loop/harness thread it through the same way `max_result_bytes` flows today, and the runtime supplies one bound to `(store, session_id)`. Sink present and write succeeds → preview+notice replacement (`details` carries `{spilled: true, original_bytes, ref}` — `full_content` is gone). Sink absent (bare harness, tests) or write fails → today's truncation, plus a logged warning in the failure case (spec's best-effort fallback).

- *Head/tail preview sizing*: notice text cost is reserved out of `max_bytes` first; the remainder splits ~half head, ~half tail on UTF-8-safe boundaries. If the remainder is ≤ 0, notice-only; if even the notice cannot fit, keep the original inline (never-larger invariant), while still writing the spill row so the content is searchable later.
- *`read` exemption, knot-shaped*: dsh must exempt its `read` tool to avoid read→spill→read. Knot's equivalent rule is simpler and structural: the retrieval tool bounds its own output by paging parameters and is marked exempt from the sink — encoded as a flag on `AgentTool` (`spill_exempt: bool = False`) rather than name-matching, so bundles can mark other pager-like tools too.

### D3: One framework retrieval tool, injected like `ask_user`

`read_tool_output(ref, offset_bytes?, limit_bytes?, pattern?)` — always present on every compiled agent (same injection path as `ask_user`/`load_skill`), resolving `ref` against the current session's spills. `pattern` runs a Python regex over the stored text and returns bounded match windows with their byte offsets; offset/limit page raw slices; both clamp `limit` to a hard ceiling (default 16KB) so retrieval output is self-bounding (D2's exemption is safe by construction). Unknown ref → ordinary `is_error` tool result naming the ref (spec scenario), never an exception.

- *Byte offsets over line numbers*: spilled output is frequently not line-structured (JSON blobs, binary-ish dumps); byte addressing is exact, cheap (`substr`), and the notice already speaks in bytes. Pattern-match results carry byte offsets to bridge search → page.

### D4: Delegation results are out of scope by pointing, not by leaking

A subagent's *final answer* returned through the delegation tool is itself a tool result in the parent and is bounded by the parent's own spill policy like any other result — no special case. The child's internal spills stay retrievable only within the child session (Non-Goal: cross-session retrieval); if the parent needs detail the child summarized away, that is a delegation-prompting concern, not a storage one.

## Risks / Trade-offs

- [SQLite file growth from accumulated spills] → Bounded per result by reality (a spill is one tool result), removed with the session; `original_bytes` is recorded so an operator TTL/size-cap sweep can be added later without schema change.
- [Model ignores the notice and reasons from the preview alone] → The notice explicitly instructs retrieval with the exact tool name and ref; the head/tail split (vs. today's head-only) preserves the most commonly decisive regions. Behavioral, monitored in evals rather than guaranteed.
- [Existing consumers depending on `details["full_content"]`] → It is an internal detail never documented in the HTTP API doc; the change removes it in the spilled case only (truncation fallback keeps it), and release notes call it out. **BREAKING** only in this soft, undocumented sense.
- [Regex `pattern` cost on multi-MB text] → Hard ceiling on stored size is not imposed, but regex runs are per-call, synchronous, and on one blob; a match-count cap and result-window cap bound the output. Pathological patterns are the tool author's classic risk, accepted for a framework-owned tool with clamped output.

## Migration Plan

1. Schema: `CREATE TABLE IF NOT EXISTS spills (...)` — additive, idempotent, applied on store open like the existing schema.
2. Code: `bound_tool_result` + sink threading + retrieval tool injection + notice format, one change.
3. Existing sessions: nothing to migrate — old entries keep their `full_content` details and remain readable; only new results spill.
4. Rollback: revert code; the `spills` table is inert data (dropped rows only on session deletion, harmless if never read).

## Open Questions

- Default retrieval `limit` ceiling (16KB proposed) — tune against real model paging behavior; contract unaffected.
- Whether the fleet HTTP API should expose spill contents to *human* frontends (e.g. `GET /sessions/{id}/spills/{ref}`) — additive endpoint, decide with the frontend team; the model-facing contract stands alone.
