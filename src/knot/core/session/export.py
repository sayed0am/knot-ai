"""JSONL export of a session's durable entry log.

One JSON object per line, in ``seq`` order, with a stable key order
(``seq``, ``type``, ``createdAt``, ``payload``) so exports are byte-for-byte
reproducible from the same entries.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TextIO

from .store import SessionStore


def export_session_jsonl(store: SessionStore, session_id: str, out: TextIO | Path) -> int:
    """Write ``session_id``'s entries as JSONL to ``out``; return the line count.

    ``out`` may be an open text stream (e.g. ``sys.stdout``) or a ``Path``,
    which is opened, written, and closed here.
    """
    if isinstance(out, Path):
        with out.open("w", encoding="utf-8") as handle:
            return _write_jsonl(store, session_id, handle)
    return _write_jsonl(store, session_id, out)


def _write_jsonl(store: SessionStore, session_id: str, handle: TextIO) -> int:
    count = 0
    for entry in store.entries(session_id):
        record = {
            "seq": entry.seq,
            "type": entry.type,
            "createdAt": entry.created_at,
            "payload": entry.payload,
        }
        handle.write(json.dumps(record))
        handle.write("\n")
        count += 1
    return count


__all__ = ["export_session_jsonl"]
