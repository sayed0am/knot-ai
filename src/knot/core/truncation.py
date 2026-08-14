"""Result-size backstop shared by the loop and standalone tool execution.

Providers and transports both have practical payload limits, and a single
oversized tool result can blow past them. This truncates only the
model-facing text; the full original content is always retained under the
result's ``details`` so nothing is permanently lost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from knot.providers.messages import TextContent

if TYPE_CHECKING:
    from knot.core.tools import AgentToolResult


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


__all__ = ["truncate_tool_result"]
