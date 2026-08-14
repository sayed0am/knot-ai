"""JSONL export round-trip and a CLI smoke test."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from knot.core.session import SessionStore, export_session_jsonl

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_knot_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the installed ``knot`` console script via ``uv run``.

    Falls back to ``python -m knot.server.cli`` when ``uv`` cannot reach the
    network (offline dev/test environments) so the smoke test still proves
    the CLI wiring works end to end.
    """
    env = {**os.environ, "UV_OFFLINE": "1"}
    result = subprocess.run(
        ["uv", "run", "--offline", "knot", *args],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        check=False,
    )
    if result.returncode != 0 and "hatchling" in (result.stderr or ""):
        result = subprocess.run(
            [sys.executable, "-m", "knot.server.cli", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    return result


def _seed_session(store: SessionStore) -> str:
    session = store.create_session("agent_a")
    store.append_entry(session.session_id, "message", {"role": "user", "content": "hi"})
    store.append_entry(
        session.session_id,
        "input_requested",
        {
            "id": "req_1",
            "kind": "question",
            "toolCallId": "call_1",
            "toolName": "ask",
            "payload": {},
            "createdAt": 1,
            "ttlSeconds": None,
        },
    )
    return session.session_id


def test_export_round_trip_matches_stored_entries() -> None:
    with SessionStore(":memory:") as store:
        session_id = _seed_session(store)
        entries = store.entries(session_id)

        buffer = io.StringIO()
        count = export_session_jsonl(store, session_id, buffer)

        assert count == len(entries)
        lines = buffer.getvalue().splitlines()
        assert len(lines) == len(entries)

        for line, entry in zip(lines, entries, strict=True):
            record = json.loads(line)
            assert list(record.keys()) == ["seq", "type", "createdAt", "payload"]
            assert record["seq"] == entry.seq
            assert record["type"] == entry.type
            assert record["createdAt"] == entry.created_at
            assert record["payload"] == entry.payload


def test_export_to_path(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    out_path = tmp_path / "out.jsonl"

    with SessionStore(db_path) as store:
        session_id = _seed_session(store)
        export_session_jsonl(store, session_id, out_path)

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert records[0]["type"] == "message"
    assert records[1]["type"] == "input_requested"


def test_cli_export_smoke_via_subprocess(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    with SessionStore(db_path) as store:
        session_id = _seed_session(store)

    result = _run_knot_cli("export", session_id, "--db", str(db_path))

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line]
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert records[0]["type"] == "message"
    assert records[1]["type"] == "input_requested"


def test_cli_export_unknown_session_fails(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    with SessionStore(db_path):
        pass  # bootstrap an empty database

    result = _run_knot_cli("export", "sess_missing", "--db", str(db_path))

    assert result.returncode == 1
    assert "no such session" in result.stderr
