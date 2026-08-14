<div align="center">
  <a href="">
    <img alt="Knot logo" src="logo.png" width="128">
  </a>
  <h1>Knot</h1>
</div>

**Ship a fleet of AI agents the way you ship code: as files in a repo — reviewable, testable, and safe to leave running.**

- 📁 **Agents are directories** — `instructions.md`, a few `tools/*.py`, done. The tree is the registry; no registration code, ever.
- 🛠️ **Tools from plain functions** — one `@tool`-decorated function per file; the JSON schema is derived from the signature and docstring.
- 📦 **Capability bundles, zero inheritance** — shared tools, skills, connections, and approval gates travel as one reviewable unit; nothing leaks in ambiently.
- ⏸️ **Human-in-the-loop that survives anything** — approvals and agent questions park a run *durably*: restart, redeploy, or answer a week later, and the run resumes exactly where it stopped.
- 🤝 **Subagents as specialists** — a subagent is just another agent directory, lowered into a parent tool; parked children park the parent, approvals bubble to one fleet inbox, and cancellation cascades.
- 🔌 **MCP connections, pinned by snapshot** — remote tool surfaces are committed to the repo at compile time; a server silently adding `delete_all` changes nothing until a human reviews the refresh.
- 🗄️ **Durable sessions in embedded SQLite** — append-only entry logs, crash recovery by construction, a fleet-wide approvals inbox in one SQL query, no database server.
- 📡 **A frontend-ready HTTP API** — turn-stitched SSE streaming where the loop's typed events *are* the wire protocol.
- 🧪 **Fleet validation in CI** — compile every agent headless, fail on any diagnostic, and see a bundle edit's full blast radius in the PR.

## Quick start

```bash
git clone https://github.com/sayed0am/knot-ai && cd knot-ai
uv sync --all-extras
uv run pytest            # 337 tests, all offline
```

Author your first agent — a directory is all it takes:

```
myfleet/
└── agents/greeter/
    ├── instructions.md      # the system prompt
    └── tools/get_time.py
```

```python
# myfleet/agents/greeter/tools/get_time.py
from knot.authoring.tools import tool
from datetime import datetime, UTC

@tool(idempotent=True)
def get_time() -> str:
    """Return the current UTC time."""
    return datetime.now(UTC).isoformat()
```

Validate the fleet, then serve it:

```bash
uv run knot validate --root myfleet
ANTHROPIC_API_KEY=... uv run knot serve --root myfleet --db knot.db
```

Chat with it over the API — each turn streams the loop's events as SSE:

```bash
curl -s -X POST localhost:8000/agents/greeter/sessions          # -> {"sessionId": "sess_..."}
curl -N -X POST localhost:8000/sessions/sess_.../messages \
     -H 'content-type: application/json' -d '{"text": "what time is it?"}'
```

## Example

A complete working fleet lives in [`examples/fleet`](examples/fleet): a
support-triage agent that uses a shared CRM bundle (an approval-gated
`update_customer`, a `once`-gated `flag_account`, and an MCP connection with a
committed snapshot), loads a refund-policy skill on demand, and delegates
research to a subagent whose own gated tool bubbles its approval request up to
the fleet inbox — parked durably across restarts at every level.

```bash
uv run knot validate --root examples/fleet   # compiles the whole fleet, exit 0
```

The four end-to-end scenarios in
[`tests/test_e2e_scenarios.py`](tests/test_e2e_scenarios.py) run that fleet
over the HTTP API: a plain chat turn, a park/approve round-trip, a delegation
chain with a child approval, and restart recovery mid-park.

## Documentation

- [Authoring agents](docs/authoring-agents.md) — the agent directory
  contract, `agent.yaml` fields, the `@tool` decorator, subagents, limits.
- [Bundles](docs/bundles.md) — shared tools, approvals, MCP connections,
  the snapshot lifecycle.
- [Skills](docs/skills.md) — the `SKILL.md` format and `load_skill`.
- [HTTP API](docs/http-api.md) — every endpoint, the SSE wire protocol, and
  the turn/park/delegation state machine, for frontend integration.
- [`examples/fleet`](examples/fleet) — a complete, working example fleet
  (a support-triage agent, a researcher subagent, and a shared CRM bundle)
  demonstrating every feature the docs above describe; validate it with
  `uv run knot validate --root examples/fleet`.

## Acknowledgments

With thanks to the projects that inspired Knot: **Tau** , **Pi**, and **Eve**.
