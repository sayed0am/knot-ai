"""The framework-floor ``read_tool_output`` retrieval tool.

Companion to spilling (``knot.core.truncation.bound_tool_result``): when a
tool result is spilled, the model-facing replacement is a bounded
head/tail preview plus a notice naming a ``ref`` (the originating
``tool_call_id``, see ``knot.core.truncation._spill_notice``) and telling
the model to retrieve the rest with this tool. Unlike ``ask_user``
(execute-less, always parks) this tool actually runs — but it needs
``(store, session_id)`` to resolve a ``ref`` against the right session's
spills, which only exist at runtime assembly, not at compile time. So
``knot.authoring.compile`` adds an execute-less placeholder (mirroring how a
connection tool compiles execute-less) and ``knot.authoring.runtime`` wires
the real executor built here, exactly the same two-phase split every other
runtime-bound capability uses.
"""

from __future__ import annotations

import re

from knot.core.session.store import SessionStore
from knot.core.tools import AgentTool, AgentToolResult
from knot.providers.messages import TextContent

READ_TOOL_OUTPUT_TOOL_NAME = "read_tool_output"

#: Hard ceiling on any one call's returned bytes (paging or search) — keeps
#: this tool's own output self-bounding, which is what makes it safe to mark
#: ``spill_exempt`` (see ``AgentTool.spill_exempt``): its result can never
#: itself be large enough to need spilling.
HARD_LIMIT_BYTES = 16384
_DEFAULT_LIMIT_BYTES = HARD_LIMIT_BYTES
_MAX_MATCHES = 20
_MATCH_CONTEXT_BYTES = 200

_PARAMETERS = {
    "type": "object",
    "properties": {
        "ref": {
            "type": "string",
            "description": (
                "The retrieval reference named in a spill notice (the originating tool call's id)."
            ),
        },
        "offsetBytes": {
            "type": "integer",
            "minimum": 0,
            "description": "Byte offset into the spilled content to start reading from. Default 0.",
        },
        "limitBytes": {
            "type": "integer",
            "description": (
                f"Maximum bytes to return, clamped to a hard ceiling of "
                f"{HARD_LIMIT_BYTES}. Default {_DEFAULT_LIMIT_BYTES}."
            ),
        },
        "pattern": {
            "type": "string",
            "description": (
                "A Python regular expression to search the full spilled content for, "
                "instead of paging. Returns up to "
                f"{_MAX_MATCHES} matches, each with its byte offset and a small "
                "context window, so a match's offset can be paged from with "
                "offsetBytes on a follow-up call."
            ),
        },
    },
    "required": ["ref"],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "Retrieve the full content of a tool result that was spilled (replaced by a "
    "bounded preview and a notice naming a ref). Page through it with "
    "offsetBytes/limitBytes (raw byte slices of the stored text), or search it with "
    "a regex pattern to find where something is before paging to it."
)


def _clamp_limit(limit_bytes: object) -> int:
    if not isinstance(limit_bytes, int) or isinstance(limit_bytes, bool):
        return _DEFAULT_LIMIT_BYTES
    return min(max(limit_bytes, 1), HARD_LIMIT_BYTES)


def _clamp_offset(offset_bytes: object) -> int:
    if not isinstance(offset_bytes, int) or isinstance(offset_bytes, bool):
        return 0
    return max(offset_bytes, 0)


def _page(text: str, ref: str, offset_bytes: int, limit_bytes: int) -> str:
    encoded = text.encode("utf-8")
    total = len(encoded)
    start = min(offset_bytes, total)
    end = min(start + limit_bytes, total)
    slice_text = encoded[start:end].decode("utf-8", errors="ignore")
    header = f"[ref={ref} total_bytes={total} range={start}:{end}]\n"
    return header + slice_text


def _byte_offset(text: str, char_index: int) -> int:
    return len(text[:char_index].encode("utf-8"))


def _search(text: str, ref: str, pattern: str, limit_bytes: int) -> str:
    """Return the rendered result of a regex search over ``text``.

    Up to ``_MAX_MATCHES`` matches are rendered, each with its byte offset
    and a small surrounding context window; the whole rendered result is
    then clamped to ``limit_bytes`` like any other retrieval output. Raises
    ``ValueError`` for an invalid ``pattern`` (``execute_tool`` turns that
    into an ordinary ``is_error`` result, matching ``load_skill``'s
    unknown-id convention — never an unhandled exception).
    """
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid pattern {pattern!r}: {exc}") from exc

    encoded = text.encode("utf-8")
    total = len(encoded)
    matches = list(compiled.finditer(text))
    shown = matches[:_MAX_MATCHES]

    blocks = [f"[ref={ref} pattern={pattern!r} matches={len(matches)} shown={len(shown)}]"]
    for match in shown:
        start_byte = _byte_offset(text, match.start())
        end_byte = _byte_offset(text, match.end())
        ctx_start = max(start_byte - _MATCH_CONTEXT_BYTES, 0)
        ctx_end = min(end_byte + _MATCH_CONTEXT_BYTES, total)
        context = encoded[ctx_start:ctx_end].decode("utf-8", errors="ignore")
        blocks.append(f"-- offset={start_byte} --\n{context}")

    rendered = "\n".join(blocks)
    rendered_bytes = rendered.encode("utf-8")
    if len(rendered_bytes) > limit_bytes:
        rendered = rendered_bytes[:limit_bytes].decode("utf-8", errors="ignore")
    return rendered


def build_read_tool_output_tool(store: SessionStore, session_id: str) -> AgentTool:
    """Build the live ``read_tool_output`` tool, bound to one session's spills.

    Marked ``spill_exempt`` (its own output is already bounded by
    ``limitBytes``/the hard ceiling, so it must never be spilled itself —
    that would be a spill-retrieve-spill loop) and ``idempotent`` (paging or
    searching the same stored content twice is always safe to re-run, e.g.
    after a crash-window repair; see ``knot.core.hitl.resume``).
    """

    async def execute_fn(
        tool_call_id: str,
        arguments: object,
        signal: object = None,
        on_update: object = None,
    ) -> AgentToolResult:
        args = arguments if isinstance(arguments, dict) else {}
        ref = args.get("ref")
        if not isinstance(ref, str) or not ref:
            raise ValueError("'ref' is required and must be a non-empty string")

        spill = store.read_spill(session_id, ref)
        if spill is None:
            raise ValueError(
                f"unknown ref {ref!r}: no spilled content in this session for that reference"
            )
        text, _original_bytes = spill

        limit_bytes = _clamp_limit(args.get("limitBytes"))
        pattern = args.get("pattern")
        if isinstance(pattern, str) and pattern:
            rendered = _search(text, ref, pattern, limit_bytes)
            return AgentToolResult(content=[TextContent(text=rendered)])

        offset_bytes = _clamp_offset(args.get("offsetBytes"))
        rendered = _page(text, ref, offset_bytes, limit_bytes)
        return AgentToolResult(content=[TextContent(text=rendered)])

    return AgentTool(
        name=READ_TOOL_OUTPUT_TOOL_NAME,
        description=_DESCRIPTION,
        parameters=_PARAMETERS,
        execute_fn=execute_fn,
        idempotent=True,
        spill_exempt=True,
    )


def build_read_tool_output_placeholder() -> AgentTool:
    """Build the execute-less, compile-time placeholder for this tool.

    Same name/description/schema as the live tool, but ``execute_fn=None``:
    used by ``knot.authoring.compile`` to give every compiled agent a
    manifest entry for it (the framework floor, alongside ``ask_user`` —
    see the insertion point in ``compile_agent``), exactly like a
    connection tool compiles execute-less pending its own runtime wiring
    (``knot.authoring.runtime._build_harness`` replaces this placeholder
    with ``build_read_tool_output_tool``, bound to the session's own store).
    """
    return AgentTool(
        name=READ_TOOL_OUTPUT_TOOL_NAME,
        description=_DESCRIPTION,
        parameters=_PARAMETERS,
        execute_fn=None,
        idempotent=True,
        spill_exempt=True,
    )


__all__ = [
    "HARD_LIMIT_BYTES",
    "READ_TOOL_OUTPUT_TOOL_NAME",
    "build_read_tool_output_placeholder",
    "build_read_tool_output_tool",
]
