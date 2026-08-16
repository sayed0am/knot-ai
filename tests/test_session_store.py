"""Schema bootstrap, append/read round-trips, seq monotonicity, writer claims."""

from __future__ import annotations

import pytest

from knot.core.session import SessionStore, WriterAlreadyClaimedError


def test_bootstrap_is_idempotent_on_open_twice(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"

    first = SessionStore(db_path)
    session = first.create_session("agent_a")
    first.close()

    # Re-opening the same file must not fail and must see prior data.
    second = SessionStore(db_path)
    assert second.get_session(session.session_id) is not None
    second.close()


def test_create_and_get_session_round_trip() -> None:
    store = SessionStore(":memory:")
    record = store.create_session("agent_a")

    assert record.session_id.startswith("sess_")
    assert record.agent_id == "agent_a"
    assert record.parent_session_id is None
    assert record.parent_tool_call_id is None

    fetched = store.get_session(record.session_id)
    assert fetched == record
    store.close()


def test_get_session_missing_returns_none() -> None:
    with SessionStore(":memory:") as store:
        assert store.get_session("sess_nope") is None


def test_create_session_with_parent_linkage() -> None:
    with SessionStore(":memory:") as store:
        root = store.create_session("agent_root")
        child = store.create_session(
            "agent_child",
            parent_session_id=root.session_id,
            parent_tool_call_id="call_1",
        )
        assert child.parent_session_id == root.session_id
        assert child.parent_tool_call_id == "call_1"


def test_append_and_read_round_trip_for_all_entry_types() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")

        message_payload = {"role": "user", "content": "hello"}
        requested_payload = {
            "id": "req_1",
            "kind": "question",
            "toolCallId": "call_1",
            "toolName": "ask",
            "payload": {},
            "createdAt": 1,
            "ttlSeconds": None,
        }
        resolved_payload = {
            "requestId": "req_1",
            "decision": "approved",
            "resolvedBy": "human_1",
            "reason": None,
            "resolvedAt": 2,
        }

        message_entry = store.append_entry(session.session_id, "message", message_payload)
        requested_entry = store.append_entry(
            session.session_id, "input_requested", requested_payload
        )
        resolved_entry = store.append_entry(
            session.session_id, "input_resolved", resolved_payload
        )

        assert message_entry.seq == 0
        assert requested_entry.seq == 1
        assert resolved_entry.seq == 2

        entries = store.entries(session.session_id)
        assert [e.type for e in entries] == ["message", "input_requested", "input_resolved"]
        assert entries[0].payload == message_payload
        assert entries[1].payload == requested_payload
        assert entries[2].payload == resolved_payload


def test_seq_is_monotonic_and_independent_per_session_under_interleaving() -> None:
    with SessionStore(":memory:") as store:
        session_a = store.create_session("agent_a")
        session_b = store.create_session("agent_b")

        seqs_a = []
        seqs_b = []
        for i in range(5):
            seqs_a.append(store.append_entry(session_a.session_id, "message", {"i": i}).seq)
            seqs_b.append(store.append_entry(session_b.session_id, "message", {"i": i}).seq)

        assert seqs_a == [0, 1, 2, 3, 4]
        assert seqs_b == [0, 1, 2, 3, 4]

        entries_a = store.entries(session_a.session_id)
        entries_b = store.entries(session_b.session_id)
        assert [e.payload["i"] for e in entries_a] == [0, 1, 2, 3, 4]
        assert [e.payload["i"] for e in entries_b] == [0, 1, 2, 3, 4]


def test_claim_writer_enforces_single_live_writer() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        claim = store.claim_writer(session.session_id)

        with pytest.raises(WriterAlreadyClaimedError):
            store.claim_writer(session.session_id)

        claim.release()
        # After release, a new claim succeeds.
        second_claim = store.claim_writer(session.session_id)
        second_claim.release()


def test_claim_writer_as_context_manager_releases_on_exit() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")

        with store.claim_writer(session.session_id):
            with pytest.raises(WriterAlreadyClaimedError):
                store.claim_writer(session.session_id)

        # Released automatically on context-manager exit.
        claim = store.claim_writer(session.session_id)
        claim.release()


def test_claim_writer_release_is_idempotent() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        claim = store.claim_writer(session.session_id)
        claim.release()
        claim.release()  # no error


def test_save_and_read_spill_round_trip() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        store.save_spill(session.session_id, "call_1", "hello world")

        result = store.read_spill(session.session_id, "call_1")
        assert result == ("hello world", len(b"hello world"))


def test_read_spill_absent_key_returns_none() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        assert store.read_spill(session.session_id, "call_missing") is None


def test_save_spill_overwrites_on_duplicate_key() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        store.save_spill(session.session_id, "call_1", "first")
        store.save_spill(session.session_id, "call_1", "second, and longer")

        text, original_bytes = store.read_spill(session.session_id, "call_1")
        assert text == "second, and longer"
        assert original_bytes == len(b"second, and longer")


def test_save_and_read_spill_multi_mb_content() -> None:
    with SessionStore(":memory:") as store:
        session = store.create_session("agent_a")
        big_text = "z" * (3 * 1024 * 1024)  # 3 MB
        store.save_spill(session.session_id, "call_1", big_text)

        text, original_bytes = store.read_spill(session.session_id, "call_1")
        assert text == big_text
        assert original_bytes == len(big_text)


def test_delete_spills_removes_only_target_sessions_spills() -> None:
    with SessionStore(":memory:") as store:
        session_a = store.create_session("agent_a")
        session_b = store.create_session("agent_b")
        store.save_spill(session_a.session_id, "call_1", "a-content")
        store.save_spill(session_b.session_id, "call_1", "b-content")

        store.delete_spills(session_a.session_id)

        assert store.read_spill(session_a.session_id, "call_1") is None
        assert store.read_spill(session_b.session_id, "call_1") == (
            "b-content",
            len(b"b-content"),
        )
