"""No-secrets boundary: runtime secret material must never reach durable storage.

A tool may legitimately hold secret material as a Python-level closure
variable to use internally (e.g. an API credential), as long as it returns
only sanitized output. That sanitized output is all a real harness run ever
persists, so the secret should never appear in the session database — on
disk or through ``scan_payloads``. The inverse control proves the scanner
itself works: a secret placed in a tool *result* (which is legitimately
persisted) must be found.
"""

from __future__ import annotations

from knot.core.harness import AgentHarness, AgentHarnessConfig
from knot.core.session import PersistenceSubscriber, SessionStore, scan_payloads
from knot.core.tools import AgentTool, AgentToolResult
from knot.providers.fake import FakeProvider, reply, tool_call

SENTINEL = "sk-SECRET-SENTINEL"


async def _run_with_tool(store: SessionStore, session_id: str, execute_fn) -> None:
    tools = [AgentTool(name="call_api", description="", parameters={}, execute_fn=execute_fn)]
    provider = FakeProvider([tool_call("call_api", {}), reply("done")])
    config = AgentHarnessConfig(
        provider=provider, model="m", system="s", tools=tools, session_id=session_id
    )
    harness = AgentHarness(config)
    subscriber = PersistenceSubscriber(store, session_id)
    harness.subscribe(subscriber)
    [event async for event in harness.prompt("fetch the records")]
    subscriber.release()


async def test_secret_used_only_as_a_tool_closure_never_reaches_storage(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    credential = SENTINEL  # a runtime secret, held only in this closure

    async def call_api(tool_call_id, arguments, signal=None, on_update=None):
        # Simulate calling an authenticated API with the credential; only
        # the sanitized result is ever returned to the agent loop.
        assert credential  # the secret is used here, never surfaced
        return AgentToolResult(content="fetched 3 records")

    store = SessionStore(db_path)
    session = store.create_session("agent_a")
    await _run_with_tool(store, session.session_id, call_api)
    store.close()

    reopened = SessionStore(db_path)
    assert scan_payloads(reopened, SENTINEL) == []
    reopened.close()

    raw_bytes = db_path.read_bytes()
    assert SENTINEL.encode() not in raw_bytes


async def test_secret_placed_in_a_tool_result_is_found_by_the_scanner(tmp_path) -> None:
    """Inverse control: proves scan_payloads actually detects a leak."""
    db_path = tmp_path / "sessions.db"

    async def leaky_call_api(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content=f"leaked credential: {SENTINEL}")

    store = SessionStore(db_path)
    session = store.create_session("agent_a")
    await _run_with_tool(store, session.session_id, leaky_call_api)
    store.close()

    reopened = SessionStore(db_path)
    hits = scan_payloads(reopened, SENTINEL)
    reopened.close()

    assert len(hits) >= 1
    assert all(session_id == session.session_id for session_id, _seq, _type in hits)

    raw_bytes = db_path.read_bytes()
    assert SENTINEL.encode() in raw_bytes
