"""Model context-window capacity resolution.

Context compaction's proactive trigger (design D2) needs to know how big a
model's context window is in order to compare provider-reported usage
against a threshold ratio. That capacity can come from two places, in
priority order:

1. An explicit ``context_window`` on the agent's ``agent.yaml`` model block
   (``knot.authoring.config.ModelConfig.context_window``), carried verbatim
   into the compiled manifest — an author-supplied value always wins,
   since the author may know something this table doesn't (a custom
   deployment, a newer dated snapshot, a fine-tune).
2. This module's small built-in defaults table, for well-known model names.

Deliberately kept small and conservative: every entry here is a model
family whose context window is uniform and well-documented, not a guess.
An unmapped model resolves to ``None`` rather than a guessed fallback —
per the spec, that just disables *proactive* compaction for that model
(the reactive overflow-retry path still protects the session regardless of
whether capacity is known).
"""

from __future__ import annotations

# Exact model-name matches, for names that don't carry a dated suffix.
_EXACT_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-3-5-haiku-latest": 200_000,
    "claude-3-5-sonnet-latest": 200_000,
    "gpt-4o": 128_000,
    "gpt-4.1": 128_000,
    "deepseek-chat": 128_000,
}

# Prefix matches, for model-name families that append a dated snapshot
# suffix (e.g. "claude-sonnet-4-20250514") but share one uniform capacity
# across every dated release in knot's supported window. Each prefix here
# is deliberately specific to one family with a *certain*, documented
# capacity — this is not a general "guess from the vendor name" fallback.
_PREFIX_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("claude-3-5-haiku-", 200_000),
    ("claude-3-5-sonnet-", 200_000),
    ("claude-sonnet-4-", 200_000),
    ("claude-opus-4-", 200_000),
    ("gpt-4o-", 128_000),
    ("gpt-4.1-", 128_000),
)


def context_window_for(model_name: str) -> int | None:
    """Return the built-in default context window for ``model_name``, if known.

    Checks the exact-match table first, then the family-prefix table.
    Returns ``None`` for anything unmapped — callers must treat that as
    "capacity unknown", not zero.
    """
    exact = _EXACT_CONTEXT_WINDOWS.get(model_name)
    if exact is not None:
        return exact
    for prefix, window in _PREFIX_CONTEXT_WINDOWS:
        if model_name.startswith(prefix):
            return window
    return None


def resolve_context_window(*, model_name: str, configured: int | None) -> int | None:
    """Resolve the context window the runtime should use for ``model_name``.

    An explicit ``configured`` value (from ``agent.yaml``) always wins over
    the built-in table; falls back to :func:`context_window_for`; ``None``
    means capacity is unknown and proactive compaction stays disabled.
    """
    if configured is not None:
        return configured
    return context_window_for(model_name)


__all__ = ["context_window_for", "resolve_context_window"]
