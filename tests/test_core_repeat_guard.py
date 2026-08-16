"""Unit tests for the pure repeat-tool-call guard module."""

from __future__ import annotations

from knot.core.repeat_guard import (
    ADVISORY_TAG_CLOSE,
    ADVISORY_TAG_OPEN,
    FIRST_REMINDER,
    RepeatChain,
    RepeatGuardSettings,
    build_advisory,
    canonical_key,
    is_excluded,
)


def test_canonical_key_is_insensitive_to_property_order() -> None:
    key_a = canonical_key("search", {"query": "cats", "limit": 5})
    key_b = canonical_key("search", {"limit": 5, "query": "cats"})

    assert key_a == key_b


def test_canonical_key_differs_on_different_arguments() -> None:
    key_a = canonical_key("search", {"query": "cats"})
    key_b = canonical_key("search", {"query": "dogs"})

    assert key_a != key_b


def test_canonical_key_differs_on_tool_name() -> None:
    key_a = canonical_key("search", {"query": "cats"})
    key_b = canonical_key("lookup", {"query": "cats"})

    assert key_a != key_b


def test_is_excluded_matches_exact_name() -> None:
    assert is_excluded("ask_user", ["ask_user"])
    assert not is_excluded("ask_user_v2", ["ask_user"])


def test_is_excluded_matches_wildcard() -> None:
    assert is_excluded("crm__delete_customer", ["crm__*"])
    assert not is_excluded("billing__delete_customer", ["crm__*"])


def test_is_excluded_matches_dunder_qualified_suffix() -> None:
    """Mirrors ``knot.core.hitl.policies``' suffix convention: a bare
    pattern with no wildcard also matches as a ``__``-qualified suffix."""
    assert is_excluded("crm__add", ["add"])
    assert not is_excluded("crm__nonadd", ["add"])  # no "__" boundary


def test_repeat_chain_counts_consecutive_identical_calls() -> None:
    chain = RepeatChain()
    settings = RepeatGuardSettings(thresholds=(3,))

    assert chain.observe("search", {"q": "a"}, settings) is None  # count 1
    assert chain.observe("search", {"q": "a"}, settings) is None  # count 2
    assert chain.observe("search", {"q": "a"}, settings) == 3  # count 3, fires


def test_repeat_chain_resets_count_on_different_arguments() -> None:
    chain = RepeatChain()
    settings = RepeatGuardSettings(thresholds=(3,))

    chain.observe("search", {"q": "a"}, settings)
    chain.observe("search", {"q": "a"}, settings)
    chain.observe("search", {"q": "different"}, settings)

    assert chain.count == 1


def test_repeat_chain_excluded_tool_is_transparent() -> None:
    """An excluded call in between two identical tracked calls neither
    increments nor resets — the tracked chain keeps growing across it."""
    chain = RepeatChain()
    settings = RepeatGuardSettings(thresholds=(2,), exclude=("bookkeeping",))

    assert chain.observe("search", {"q": "a"}, settings) is None  # count 1
    assert chain.observe("bookkeeping", {"note": "x"}, settings) is None  # transparent
    assert chain.count == 1  # untouched by the excluded call
    assert chain.observe("search", {"q": "a"}, settings) == 2  # count 2, fires


def test_repeat_chain_disabled_never_fires() -> None:
    chain = RepeatChain()
    settings = RepeatGuardSettings(enabled=False, thresholds=(2,))

    assert chain.observe("search", {"q": "a"}, settings) is None
    assert chain.observe("search", {"q": "a"}, settings) is None


def test_repeat_chain_reset_restarts_count_but_keeps_fired() -> None:
    chain = RepeatChain()
    settings = RepeatGuardSettings(thresholds=(2, 3))

    chain.observe("search", {"q": "a"}, settings)
    assert chain.observe("search", {"q": "a"}, settings) == 2

    chain.reset()
    assert chain.count == 0
    assert chain.key is None
    assert chain.fired == {2}  # fired thresholds survive reset


def test_repeat_chain_threshold_fires_at_most_once_per_run() -> None:
    """Even if the chain regrows past a threshold after a reset, an
    already-fired threshold does not fire again this run (design D4)."""
    chain = RepeatChain()
    settings = RepeatGuardSettings(thresholds=(2,))

    chain.observe("search", {"q": "a"}, settings)
    assert chain.observe("search", {"q": "a"}, settings) == 2

    chain.reset()
    chain.observe("search", {"q": "a"}, settings)
    assert chain.observe("search", {"q": "a"}, settings) is None  # count 2 again, no re-fire


def test_repeat_chain_last_call_reports_name_and_canonical_args() -> None:
    chain = RepeatChain()
    settings = RepeatGuardSettings(thresholds=(1,))

    chain.observe("search", {"q": "a"}, settings)

    name, canonical_args = chain.last_call()
    assert name == "search"
    assert canonical_args == canonical_key("search", {"q": "a"}).split("\x00", 1)[1]


def test_build_advisory_first_threshold_is_generic() -> None:
    advisory = build_advisory(
        is_first_threshold=True,
        tool_name="search",
        count=3,
        canonical_args='{"q":"a"}',
        preview_cap=500,
    )

    assert FIRST_REMINDER in advisory.text
    assert advisory.text.startswith(ADVISORY_TAG_OPEN)
    assert advisory.text.endswith(ADVISORY_TAG_CLOSE)
    assert "search" not in advisory.text  # the generic nudge names no tool


def test_build_advisory_later_threshold_names_tool_and_count() -> None:
    advisory = build_advisory(
        is_first_threshold=False,
        tool_name="search",
        count=5,
        canonical_args='{"q":"a"}',
        preview_cap=500,
    )

    assert "search" in advisory.text
    assert "5" in advisory.text
    assert '{"q":"a"}' in advisory.text
    assert advisory.text.startswith(ADVISORY_TAG_OPEN)
    assert advisory.text.endswith(ADVISORY_TAG_CLOSE)


def test_build_advisory_truncates_preview_with_marker() -> None:
    long_args = '{"q":"' + ("x" * 100) + '"}'

    advisory = build_advisory(
        is_first_threshold=False,
        tool_name="search",
        count=5,
        canonical_args=long_args,
        preview_cap=10,
    )

    assert long_args[:10] in advisory.text
    assert long_args not in advisory.text  # full string was not embedded
    assert "more chars" in advisory.text


def test_repeat_chain_detection_uses_full_args_not_preview_cap() -> None:
    """Detection always compares the full canonical key; ``preview_cap``
    only bounds the advisory text (see ``build_advisory``), never what
    counts as "the same call"."""
    chain = RepeatChain()
    settings = RepeatGuardSettings(thresholds=(2,), preview_cap=5)
    long_a = {"data": "x" * 100 + "A"}
    long_b = {"data": "x" * 100 + "B"}  # differs only beyond preview_cap

    chain.observe("tool", long_a, settings)
    assert chain.observe("tool", long_b, settings) is None  # treated as a different chain
    assert chain.count == 1


def test_build_advisory_preview_within_cap_is_not_truncated() -> None:
    short_args = '{"q":"a"}'

    advisory = build_advisory(
        is_first_threshold=False,
        tool_name="search",
        count=5,
        canonical_args=short_args,
        preview_cap=500,
    )

    assert short_args in advisory.text
    assert "more chars" not in advisory.text
