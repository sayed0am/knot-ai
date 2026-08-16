# HTTP API

For the frontend team: this is the complete, current surface of the knot
HTTP API — every endpoint, the SSE wire protocol, and the state machine a
client needs to drive a session through a turn, a park, and a delegation
chain. Every JSON shape below is a real, captured example (generated
against [`examples/fleet`](../examples/fleet) with a scripted fake model —
see `tests/test_e2e_scenarios.py`), not a hand-written guess.

One app instance (`create_app`) serves one compiled fleet. There is no
authentication layer in v0 — that's a deployment concern, not part of this
API.

## The turn-stitched model, in one paragraph

A "turn" is one HTTP request that streams Server-Sent Events until the run
either finishes (`completed`/`error`/`aborted`) or **parks**
(`waiting_input`, because some tool call needs a human decision). A parked
run is *not* resumed automatically — resuming it is always a new HTTP call
the client makes: `POST /sessions/{id}/input` to answer the pending
request(s), then `POST /sessions/{id}/continue` to open a new SSE stream
that picks the run back up. The server never advances a session on its own;
every state transition is one explicit client call.

## Endpoints

### `GET /agents`

List every agent in the fleet, whether or not it compiled.

```json
[
  {"agentId": "support", "ok": true, "description": "Front-line customer support triage agent..."}
]
```

### `GET /agents/{agent_id}`

The agent's full compiled manifest (see `docs/authoring-agents.md` for its
shape) as `application/json`.

- `404` — unknown agent id.
- `409` — the agent exists but failed to compile; body is
  `{"diagnostics": [{"severity", "path", "message", "agentId", "bundleId"}, ...]}`.

### `POST /agents/{agent_id}/sessions`

Create a new top-level session. `201`:

```json
{"sessionId": "sess_...", "agentId": "support", "state": "idle", "createdAt": 1786712230136}
```

- `404` — unknown agent id.
- `409` — the agent failed to compile.

### `GET /sessions/{session_id}`

The session's full current state — this is the one endpoint you poll (or
re-fetch after a stream ends) to know what's going on:

```json
{
  "sessionId": "sess_3f1688f90f6d409f9577346f2fc478c7",
  "agentId": "support",
  "state": "waiting",
  "pendingRequests": [
    {
      "id": "req_e4f29844fcca4a2f92f67eb046a233ab",
      "kind": "tool_approval",
      "toolCallId": "call_uc",
      "toolName": "update_customer",
      "payload": {"args": {"customer_id": "CUST-1", "field": "tier", "value": "gold"}},
      "createdAt": 1786712242219,
      "ttlSeconds": null
    }
  ],
  "transcript": [ /* full AgentMessage list, oldest first — see "Message shapes" below */ ],
  "parentSessionId": null,
  "parentToolCallId": null
}
```

`404` if the session id doesn't exist. See [State semantics](#state-semantics)
for `state`'s three possible values.

### `POST /sessions/{session_id}/messages`

Start a turn with a new user message. Body: `{"text": "..."}`.

- If the session is **idle**: opens a `text/event-stream` response (see
  below) and starts running immediately.
- If the session is **waiting** (already parked): the text is **queued**,
  not run — `202 {"queued": true}`. Queued text is drained into the harness
  on the *next* `/continue` call. **Queued text is in-memory only and does
  not survive a server restart** (see [State semantics](#state-semantics)).
- `404` — unknown session. `409` — the session already has a run in flight.

### `POST /sessions/{session_id}/continue`

Resume a parked, now-ready session (every pending request resolved, no
dangling unresolved tool call) with a fresh SSE stream — no request body.

- `404` — unknown session.
- `409 "session is currently running"` — a turn is already in flight.
- `409 "session is not ready to continue"` — still has an unresolved
  pending request, or has never run at all (an empty transcript is never
  "ready to continue").

### `POST /sessions/{session_id}/input`

Answer one or more pending requests. Body:

```json
{"responses": {"req_e4f29844fcca4a2f92f67eb046a233ab": {"action": "approve", "by": "ops"}}}
```

Each response item:

| Field | Required | Meaning |
|---|---|---|
| `action` | yes | `"approve"`, `"deny"`, or `"answer"`. |
| `by` | yes | Who resolved it (a free-text identity string, e.g. an operator name). |
| `reason` | only for `"deny"` | Why it was denied — becomes the tool result's error text (`"User denied: <reason>"`). |
| `text` | only for `"answer"` | The human's answer text, for an `ask_user` (`question`-kind) request. |

Response, `200`:

```json
{"resolved": ["req_e4f29844fcca4a2f92f67eb046a233ab"], "rejected": [], "expired": [], "invalidated": [], "readyToContinue": true}
```

- `resolved` — request ids this call actually resolved.
- `rejected` — `[requestId, reason]` pairs for ids that couldn't be resolved
  (`"unknown"`, `"already_resolved"`, `"expired"`, or — for a
  `child_session`-kind request — `"child_session"`, since those can only be
  resolved by the child session's own completion, never a direct human
  response).
- `expired` — request ids that had already passed their TTL and were
  auto-denied by this call (a subset informs `rejected`, but is broken out
  separately here).
- `invalidated` — approved, but the tool it named is no longer available
  (e.g. removed by a connection refresh) — auto-denied instead of executed.
- `readyToContinue` — whether the session has no pending requests left and
  is safe to `/continue`. Always freshly recomputed, never cached.
- `404` — unknown session. `409` — every targeted request id was rejected
  (a wholly stale call); a call that resolves at least one id, or merely
  invalidates one, is still `200`.

A tool call's arguments are re-executed from the session's own durable
transcript on approval (never client-supplied), so a request body cannot
smuggle different arguments in at approval time.

### `POST /sessions/{session_id}/cancel`

Cancel an in-flight run, if any.

```json
{"cancelled": true, "state": "waiting"}
```

`{"cancelled": false, ...}` if there was nothing running. `404` for an
unknown session id.

### `GET /approvals?status=pending`

The fleet-wide approvals inbox — every pending `tool_approval`/`question`
request across every session, with its full delegation-chain attribution
(`child_session`-kind parks are deliberately excluded, since a human never
resolves those directly):

```json
[
  {
    "requestId": "req_e4f29844fcca4a2f92f67eb046a233ab",
    "kind": "tool_approval",
    "toolName": "update_customer",
    "agentId": "support",
    "sessionId": "sess_3f1688f90f6d409f9577346f2fc478c7",
    "rootSessionId": "sess_3f1688f90f6d409f9577346f2fc478c7",
    "path": [{"sessionId": "sess_3f1688f90f6d409f9577346f2fc478c7", "agentId": "support"}],
    "ageSeconds": 0.003,
    "payload": {"args": {"customer_id": "CUST-1", "field": "tier", "value": "gold"}},
    "createdAt": 1786712242219
  }
]
```

For a request parked deep in a delegation chain, `sessionId` is the session
that actually owns the park, `rootSessionId` is the top-level session, and
`path` lists every hop's `(sessionId, agentId)` from root down to the
owning session — enough to render "support → researcher: search_notes
needs approval" in an inbox UI without a second round trip. `status` only
supports `"pending"` today — anything else is `422`.

### `GET /connections/health`

Reconnects to every declared MCP connection in the fleet and diffs its live
tools against its committed snapshot, without writing anything:

```json
[
  {"bundle": "crm", "connection": "support_mcp", "status": "ok", "added": [], "removed": [], "schemaChanged": []}
]
```

`status` is `"ok"`, `"drift"` (the live server's tools differ from the
committed snapshot), or `"unreachable"` (with an added `"error"` field).
This never fails the request as a whole — one connection's failure is just
one row with `status: "unreachable"`.

## The SSE wire protocol

`POST .../messages` and `POST .../continue` both respond
`Content-Type: text/event-stream`. Every frame is exactly:

```
event: <type>
data: <JSON>

```

For an ordinary agent event, `<type>` is the event's own `type` field and
`data` is that event model dumped **camelCase, verbatim** — nothing is
reshaped for the wire. Every field name below is exactly what appears on
the wire.

### Event grammar

| `type` | Fields (beyond `type`) | When |
|---|---|---|
| `agent_start` | — | Once, at the very start of the run. |
| `turn_start` | — | Once per model turn. |
| `message_start` / `message_update` / `message_end` | `message: AgentMessage` (`message_update` also carries `assistantMessageEvent`) | Streaming lifecycle of one message — the user message that started the turn, then the assistant's reply as it streams in. |
| `tool_execution_start` | `toolCallId, toolName, args` | A tool call is about to run. |
| `tool_execution_update` | `..., partialResult` | A tool reported incremental progress (rare; most tools don't). |
| `tool_execution_end` | `toolCallId, toolName, result, isError` | A tool call finished (or failed). If the result's text exceeded the agent's `max_result_bytes`, `result` is the bounded preview+notice, not the full text — see [An oversized result, spilled](#an-oversized-result-spilled) below. |
| `turn_end` | `message, toolResults` | One model turn's assistant message plus any tool results produced for it. |
| `subagent_called` | `toolCallId, subagentId, childSessionId` | A delegation call just created a child session — control-plane only, not durably recorded. |
| `subagent_completed` | `toolCallId, subagentId, childSessionId, outcome` | That child session reached *some* terminal state for this call — `outcome` is the child's own `agent_end.outcome`, including `"waiting_input"` if the child itself parked. |
| `compaction` | `coversThroughSeq, summaryBytes, trigger` | The session's history was just compacted — see [Context compaction](#context-compaction) below. Control-plane only, like `subagent_called`; the durable fact is the `"compaction"` session entry, already appended by the time this frame is sent. |
| `agent_end` | `outcome, messages, pendingRequests` | **Always the last `AgentEvent` frame.** `outcome` is `"completed"`, `"error"`, `"aborted"`, or `"waiting_input"`. |
| `chain` | `parentSessionId, parentReady` | Server-layer-only frame (not part of the core event grammar) — see [Delegation and the chain event](#delegation-and-the-chain-event) below. |

`message_update`'s nested `assistantMessageEvent.type` is one of `start`,
`text_start`/`text_delta`/`text_end`, `thinking_start`/`thinking_delta`/`thinking_end`,
`toolcall_start`/`toolcall_delta`/`toolcall_end`, `done`, `error` — the
provider-level streaming grammar underneath the loop-level one, for a
client that wants to render text/thinking token-by-token rather than
waiting for `message_end`.

### A full captured stream — plain reply, no tools

```
event: agent_start
data: {"type":"agent_start"}

event: turn_start
data: {"type":"turn_start"}

event: message_start
data: {"type":"message_start","message":{"role":"user","content":"Where is order ORD-1001?","timestamp":1786712230138}}

event: message_end
data: {"type":"message_end","message":{"role":"user","content":"Where is order ORD-1001?","timestamp":1786712230138}}

event: message_start
data: {"type":"message_start","message":{"role":"assistant","content":[],"api":"fake","provider":"fake","model":"fake-model", ...}}

event: message_update
data: {"type":"message_update","message":{...,"content":[{"type":"text","text":""}]},"assistantMessageEvent":{"type":"text_start", ...}}

event: message_update
data: {"type":"message_update","message":{...,"content":[{"type":"text","text":"Order ORD-1001 shipped via UPS and should arrive 2026-08-16."}]},"assistantMessageEvent":{"type":"text_delta", ...}}

event: message_update
data: {"type":"message_update","message":{...},"assistantMessageEvent":{"type":"text_end", ...}}

event: message_end
data: {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Order ORD-1001 shipped via UPS and should arrive 2026-08-16."}], ...}}

event: turn_end
data: {"type":"turn_end","message":{...},"toolResults":[]}

event: agent_end
data: {"type":"agent_end","outcome":"completed","messages":[{"role":"user", ...},{"role":"assistant", ...}],"pendingRequests":[]}
```

(`...` above elides repeated boilerplate fields — `api`, `provider`,
`model`, `usage`, `stopReason`, `errorMessage`, `timestamp` — present on
every real `AssistantMessage`; the field list is exact, just not repeated
here for readability.)

### A park, mid-stream

The same shape, but the model's reply is a tool call instead of text, and
the stream ends in `waiting_input` instead of `completed`:

```
event: message_end
data: {"type":"message_end","message":{"role":"assistant","content":[{"type":"toolCall","id":"call_uc","name":"update_customer","arguments":{"customer_id":"CUST-1","field":"tier","value":"gold"}}], ...}}

event: turn_end
data: {"type":"turn_end","message":{...},"toolResults":[]}

event: agent_end
data: {"type":"agent_end","outcome":"waiting_input","messages":[...],"pendingRequests":[{"id":"req_e4f29844fcca4a2f92f67eb046a233ab","kind":"tool_approval","toolCallId":"call_uc","toolName":"update_customer","payload":{"args":{"customer_id":"CUST-1","field":"tier","value":"gold"}},"createdAt":1786712242219,"ttlSeconds":null}]}
```

Note there is no `tool_execution_start`/`tool_execution_end` pair here at
all — a call that parks is never executed in this run; execution (or not)
happens only once a human resolves it via `/input`, as part of the *next*
`/continue` stream (or synchronously inside `/input` itself for the
approve-and-execute case — see the endpoint's own description above).

### An oversized result, spilled

When a tool result's text exceeds the agent's `max_result_bytes` (see
`docs/authoring-agents.md`), it is *spilled*: `result.content` carries a
bounded head/tail preview of the original text plus a notice naming the
omitted byte count and how to retrieve the rest, and `result.details`
carries `{"spilled": true, "original_bytes": <int>, "ref": <string>}`.
**The full original text never rides this event, the persisted transcript,
or any later provider request — only the preview does.** Retrieve the full
content with the always-available `read_tool_output` tool, called with
`ref` (see `docs/authoring-agents.md` for its full parameter set):

```
event: tool_execution_end
data: {"type":"tool_execution_end","toolCallId":"call_lookup","toolName":"lookup_order","result":{"content":[{"type":"text","text":"Order O\n[... 406 bytes omitted ...]\n6-08-20.\n[spilled: result was 421 bytes, limit 200. Full content stored; retrieve with read_tool_output(ref=\"call_lookup\") using offsetBytes/limitBytes or pattern.]","textSignature":null}],"details":{"spilled":true,"original_bytes":421,"ref":"call_lookup"}},"isError":false}
```

A result within the cap carries `details: null` (or whatever the tool
itself returned) and its content is untouched — spilling only ever replaces
a result that would otherwise exceed the cap, and the replacement is always
within it.

## Context compaction

A long-lived session can outgrow its model's context window. When that
happens, knot durably replaces the oldest span of the conversation with a
single model-generated summary message (see `docs/authoring-agents.md`'s
`compaction:` block for the full trigger/config surface) and announces it on
the stream with a `compaction` frame:

```
event: compaction
data: {"type":"compaction","coversThroughSeq":1,"summaryBytes":96,"trigger":"proactive"}
```

- `coversThroughSeq` — the highest durable entry `seq` this compaction
  replaced (an internal log coordinate, not a transcript index — useful for
  correlating with the raw entry log, not for indexing `transcript`).
- `summaryBytes` — the UTF-8 byte length of the summary text, a cheap size
  signal if you don't want to inspect the message itself.
- `trigger` — `"proactive"` (fired between turns, before the next provider
  request, because reported context usage crossed the configured
  threshold) or `"reactive"` (fired in response to a provider context-
  overflow error — see below).

Once compaction runs, the summary message is an ordinary part of
`transcript`/`agent_end.messages` going forward — a `user`-role message
whose text is wrapped in `<compacted-summary>...</compacted-summary>` tags,
so a client can recognize and render it distinctly from words the human
user actually typed. The messages it replaced are never removed from the
session's durable log — only from the model-visible projection — so nothing
about the API surface *requires* a client to do anything special with a
`compaction` frame; it's purely informational.

### Reactive recovery: the stream shape

When a provider request fails with a context-overflow error, knot compacts
and retries automatically instead of ending the run with that error — the
client sees one continuous run, never a mid-stream error terminal for a run
that in fact recovered. Concretely: the failed request's own
`message_end` still appears on the stream (with `stopReason: "error"` and
`errorType: "context_overflow"`), but the `agent_end` frame that would
normally follow it is **withheld** — replaced by a `compaction` frame and
then the retried request's own events — so the stream carries exactly one
terminal `agent_end` for the whole turn:

```
event: message_end
data: {"type":"message_end","message":{"role":"assistant","content":[],...,"stopReason":"error","errorMessage":"context window exceeded","errorType":"context_overflow",...}}

event: compaction
data: {"type":"compaction","coversThroughSeq":1,"summaryBytes":76,"trigger":"reactive"}

event: message_end
data: {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Continuing after recovery.",...}],...,"stopReason":"stop",...}}

event: agent_end
data: {"type":"agent_end","outcome":"completed","messages":[{"role":"assistant","content":[{"type":"text","text":"Continuing after recovery."}],...}],"pendingRequests":[]}
```

If compaction cannot shrink the history, or the configured
`max_overflow_retries` is exhausted, recovery does not happen: the withheld
`agent_end` is yielded as the run's one and only terminal, with its original
`outcome: "error"` — the stream still ends cleanly (a `None` sentinel closes
it the same as any other run), it simply never recovered. A client that
only ever looks at the terminal `agent_end` needs no special handling
either way; the `compaction` frame is only there for a client that wants to
render "the session was summarized" to a human.

## Delegation and the chain event

A delegation call (a subagent tool call) either returns the child's answer
directly, in the same turn — `subagent_called` then `subagent_completed`
with `outcome: "completed"` (or `"error"`/`"aborted"`), no park — or, if the
child session itself doesn't finish synchronously (it parks, or is still
mid-run), the *parent's own* run parks too, with a `child_session`-kind
pending request:

```
event: subagent_called
data: {"type":"subagent_called","toolCallId":"call_delegate","subagentId":"researcher","childSessionId":"sess_30e0641e8c0e45d594caa7f1b8ac65d1"}

event: subagent_completed
data: {"type":"subagent_completed","toolCallId":"call_delegate","subagentId":"researcher","childSessionId":"sess_30e0641e8c0e45d594caa7f1b8ac65d1","outcome":"waiting_input"}

event: agent_end
data: {"type":"agent_end","outcome":"waiting_input","messages":[...],"pendingRequests":[{"id":"req_...","kind":"child_session","toolCallId":"call_delegate","toolName":"researcher","payload":{"childSessionId":"sess_30e0641e8c0e45d594caa7f1b8ac65d1"},"createdAt":...,"ttlSeconds":null}]}
```

A `child_session` request is **never** resolved by `/input` — the only way
to unblock the parent is to resolve *the child's own* pending request
(directly against `/sessions/{childSessionId}/input`) and then call
`/sessions/{childSessionId}/continue`. When that child stream reaches a
genuinely terminal outcome (anything but `waiting_input`) and the session
has a parent, the server appends one extra frame after `agent_end` —
`event: chain` — reporting whether the parent is now ready:

```
event: agent_end
data: {"type":"agent_end","outcome":"completed","messages":[{"role":"assistant","content":[{"type":"text","text":"Found a note about a delayed shipment."}]}],"pendingRequests":[]}

event: chain
data: {"parentSessionId": "sess_00a18b1e50c04e05860c7ab3ef52dfa4", "parentReady": true}
```

The parent's own turn is **never auto-run** — `chain` only tells you it's
now safe to `POST /sessions/{parentSessionId}/continue`; making that call is
always a separate, deliberate client action. A root session (no parent) never
gets a `chain` frame at all. Nested chains work by repeating this: resolve
and continue the deepest parked session first, check `chain.parentReady`,
continue its parent, and so on up to the root.

## Message shapes

`AgentMessage` (used in `transcript`, `agent_end.messages`, and every
`message_*` event) is a discriminated union on `role`:

- **`user`** — `{"role": "user", "content": "<text>", "timestamp": ...}`. A
  compaction summary (see [Context compaction](#context-compaction) above)
  is also a `user`-role message, distinguishable by its
  `<compacted-summary>...</compacted-summary>`-wrapped `content`.
- **`assistant`** — `{"role": "assistant", "content": [TextContent | ThinkingContent | ToolCall, ...], "api", "provider", "model", "usage", "stopReason", "errorMessage", "timestamp", ...}`. A `ToolCall` content block is `{"type": "toolCall", "id", "name", "arguments"}`.
- **`toolResult`** — `{"role": "toolResult", "toolCallId", "toolName", "content": [...], "details", "isError", "timestamp"}`. This is what a delegation call's result looks like in the *parent's* transcript too: `toolName` is the subagent's id, and `content` is the child's final answer text. `details` is normally `null`; for a spilled result it is `{"spilled": true, "original_bytes", "ref"}` and `content` is the bounded preview, never the full text — see [An oversized result, spilled](#an-oversized-result-spilled) above.
- **`custom`** — an escape hatch for provider-specific message shapes; not produced by anything described in this document.

## State semantics

`GET /sessions/{id}.state` (and `POST .../cancel`'s response) is one of:

- **`idle`** — no pending requests, nothing running; ready for a new
  `/messages` call.
- **`waiting`** — parked: at least one unresolved pending request. Durable
  — derived fresh from the session's entry log every time, so it survives a
  restart exactly as shown.
- **`running`** — a turn's SSE stream is currently in flight for this
  session, tracked purely in the server process's own memory. `running`
  always wins over the durable `idle`/`waiting` read when both could apply.

**What's durable vs. in-memory** (the v0 contract, worth internalizing as a
frontend developer): the full transcript, every park signal, every approval
decision, and session identity/parentage are all written to SQLite before a
stream's events are ever forwarded to a client, and survive a server
restart intact — a fresh `create_app` pointed at the same database file
reports exactly the same `state` and `pendingRequests` a client would have
seen right before the restart. Two things do **not** survive a restart:
which sessions were mid-run (`running` degrades to `waiting`/`idle` after a
restart, since there's no in-flight stream to reattach to) and any text
queued by a `202 {"queued": true}` response that hasn't been drained by a
`/continue` call yet (it is simply gone — post it again after confirming
the session's state).

## Error/conflict semantics per endpoint

| Endpoint | 404 | 409 | Other |
|---|---|---|---|
| `GET /agents/{id}` | unknown agent | agent failed to compile (body: diagnostics) | — |
| `POST /agents/{id}/sessions` | unknown agent | agent failed to compile | `201` on success |
| `GET /sessions/{id}` | unknown session | — | — |
| `POST .../messages` | unknown session | a run is already in flight | `202` if queued (session was `waiting`) |
| `POST .../continue` | unknown session | a run is already in flight, or the session isn't ready to continue | — |
| `POST .../input` | unknown session | every targeted request id was rejected | `200` even on partial success |
| `POST .../cancel` | unknown session | — | `{"cancelled": false, ...}` if nothing was running |
| `GET /approvals` | — | — | `422` if `status` isn't `pending` |
| `GET /connections/health` | — | — | never fails as a whole; per-row `"unreachable"` |

Every error body follows FastAPI's default `HTTPException` shape,
`{"detail": "<message>"}`, except the two endpoints noted above that return
a structured JSON body on `409`.
