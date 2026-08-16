"""Result-size backstop shared by the loop and standalone tool execution.

Providers and transports both have practical payload limits, and a single
oversized tool result can blow past them. Two strategies bound an oversized
result to fit:

- ``truncate_tool_result`` (the original, destructive strategy): keep a
  prefix and drop the rest. The full original text is retained under the
  result's ``details["full_content"]`` so nothing is *lost*, but nothing
  outside that one in-memory result can ever recover it — every persisted
  entry and wire payload downstream still carries the full blob, unbounded.
- ``bound_tool_result`` (the default strategy): when a ``spill_sink`` is
  supplied, the full text is handed to it for durable, session-scoped
  storage, and the model-facing replacement carries a bounded head/tail
  preview plus a notice naming the omitted byte count and how to retrieve
  the rest. This is strictly better on every surface (provider context,
  SSE, entry log) and is what ``knot.core.tools.execute_tool`` uses whenever
  a sink is available; ``truncate_tool_result`` remains the fallback for a
  bare harness (no sink), an exempt tool, or a failed spill write.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from knot.providers.messages import TextContent

if TYPE_CHECKING:
    from knot.core.tools import AgentToolResult

logger = logging.getLogger(__name__)

#: Given ``(tool_call_id, full_text)``, durably store the text and return the
#: retrieval reference (see ``knot.core.session.store.SessionStore.save_spill``
#: for the concrete implementation the runtime binds this to). May raise; a
#: raised exception is caught by ``bound_tool_result`` and treated as a
#: failed spill (fall back to truncation), never propagated to the caller.
SpillSink = Callable[[str, str], str]

_ELISION_MARKER = "\n[... {omitted} bytes omitted ...]\n"


def truncate_tool_result(result: AgentToolResult, max_bytes: int | None) -> AgentToolResult:
    """Return ``result`` unchanged, or with its text content truncated.

    When the UTF-8 encoded text content exceeds ``max_bytes``, the kept
    prefix is followed by an explicit marker naming the original size, and
    the full original text is preserved under
    ``details["full_content"]``. Non-text content blocks (e.g. images) are
    left untouched. A ``None`` limit, or content already within budget,
    returns ``result`` as-is.
    """
    if max_bytes is None:
        return result
    text = result.text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return result

    marker = f"\n[truncated: result was {len(encoded)} bytes, limit {max_bytes}]"
    budget = max(max_bytes - len(marker.encode("utf-8")), 0)
    prefix = encoded[:budget].decode("utf-8", errors="ignore")
    kept_text = prefix + marker

    non_text = [block for block in result.content if not isinstance(block, TextContent)]
    return result.model_copy(
        update={
            "content": [TextContent(text=kept_text), *non_text],
            "details": {
                "full_content": text,
                "truncated": True,
                "original_bytes": len(encoded),
            },
        }
    )


def bound_tool_result(
    result: AgentToolResult,
    call_id: str,
    max_bytes: int | None,
    spill_sink: SpillSink | None,
) -> AgentToolResult:
    """Return ``result`` unchanged, spilled, or truncated to fit ``max_bytes``.

    - ``max_bytes`` is ``None``, or the text already fits: ``result`` as-is.
    - No ``spill_sink``: falls back to ``truncate_tool_result``.
    - A ``spill_sink`` is given: the full text is handed to it. On success,
      the model-facing content becomes a head/tail preview plus a retrieval
      notice, and ``details`` becomes
      ``{"spilled": True, "original_bytes": N, "ref": ref}`` —
      ``full_content`` is deliberately absent (that is the whole point: the
      full payload no longer rides every surface). On a sink exception, a
      warning is logged and behavior falls back to ``truncate_tool_result``
      (best-effort: a storage failure must never fail the tool call).

    Non-text content blocks (e.g. images) always pass through untouched;
    only the text content is ever bounded or spilled.
    """
    if max_bytes is None:
        return result
    text = result.text
    encoded = text.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return result

    if spill_sink is None:
        return truncate_tool_result(result, max_bytes)

    try:
        ref = spill_sink(call_id, text)
    except Exception:  # noqa: BLE001 - spill storage is a best-effort boundary
        logger.warning("spill_sink failed for tool_call_id=%s; falling back to truncation", call_id)
        return truncate_tool_result(result, max_bytes)

    notice = _spill_notice(original_bytes, max_bytes, ref)
    notice_bytes = len(notice.encode("utf-8"))
    non_text = [block for block in result.content if not isinstance(block, TextContent)]

    if notice_bytes > max_bytes:
        # Never-larger invariant: even a notice-only replacement doesn't
        # fit, so keep the original result inline unchanged. The spill row
        # was still written above, so the content remains retrievable. A
        # notice exactly at the cap still fits and replaces.
        return result

    preview_budget = max_bytes - notice_bytes
    preview = _head_tail_preview(encoded, preview_budget)
    kept_text = preview + notice

    return result.model_copy(
        update={
            "content": [TextContent(text=kept_text), *non_text],
            "details": {
                "spilled": True,
                "original_bytes": original_bytes,
                "ref": ref,
            },
        }
    )


def _spill_notice(original_bytes: int, max_bytes: int, ref: str) -> str:
    return (
        f"\n[spilled: result was {original_bytes} bytes, limit {max_bytes}. "
        f'Full content stored; retrieve with read_tool_output(ref="{ref}") '
        f"using offsetBytes/limitBytes or pattern.]"
    )


def _head_tail_preview(encoded: bytes, budget: int) -> str:
    """Split ``budget`` bytes ~half head / ~half tail of ``encoded``.

    Decoding uses ``errors="ignore"`` (matching ``truncate_tool_result``) so
    a byte-offset split that lands mid-codepoint never raises; the marker
    joining head and tail makes the elision visible rather than silently
    concatenating unrelated text.
    """
    if budget <= 0:
        return ""

    # The marker's own text depends on the omitted (middle-gap) byte count,
    # which in turn depends on how much of the budget the marker itself
    # consumes. Estimate the marker cost using a worst-case digit count
    # first, then rebuild the exact marker once the split is known — the
    # marker's byte cost does not depend on which digits it contains, only
    # how many, so a single fixed-point step is exact.
    approx_marker_bytes = len(_ELISION_MARKER.format(omitted=len(encoded)).encode("utf-8"))

    if approx_marker_bytes >= budget:
        # Not enough room for a marker either: just take a head slice.
        return encoded[:budget].decode("utf-8", errors="ignore")

    remaining = budget - approx_marker_bytes
    head_budget = remaining // 2
    tail_budget = remaining - head_budget
    omitted = len(encoded) - head_budget - tail_budget

    marker = _ELISION_MARKER.format(omitted=omitted)
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    tail = (
        encoded[len(encoded) - tail_budget :].decode("utf-8", errors="ignore")
        if tail_budget
        else ""
    )
    return head + marker + tail


__all__ = ["SpillSink", "bound_tool_result", "truncate_tool_result"]
