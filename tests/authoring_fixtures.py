"""Shared helpers for building small fleets under ``tmp_path`` in authoring tests.

Not itself a test module (no ``test_`` prefix): pytest never collects it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path


def write_files(root: Path, files: dict[str, str]) -> None:
    """Write each ``{relative_path: content}`` pair under ``root``.

    Content is run through ``textwrap.dedent`` so callers can write it as an
    indented triple-quoted string alongside the surrounding test code.
    """
    for rel_path, content in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        text = textwrap.dedent(content)
        if text.startswith("\n"):
            text = text[1:]
        path.write_text(text, encoding="utf-8")
