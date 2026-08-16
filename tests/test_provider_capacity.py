"""Model context-window capacity resolution (`knot.providers.capacity`)."""

from __future__ import annotations

from knot.providers.capacity import context_window_for, resolve_context_window


def test_known_exact_model_resolves_from_table() -> None:
    assert context_window_for("gpt-4o") == 128_000
    assert context_window_for("deepseek-chat") == 128_000


def test_known_dated_snapshot_resolves_from_prefix_table() -> None:
    assert context_window_for("claude-sonnet-4-20250514") == 200_000
    assert context_window_for("claude-opus-4-20250514") == 200_000
    assert context_window_for("gpt-4o-2024-08-06") == 128_000


def test_unknown_model_resolves_to_none() -> None:
    assert context_window_for("some-fine-tune-nobody-heard-of") is None


def test_resolve_explicit_manifest_value_wins_over_table() -> None:
    assert resolve_context_window(model_name="gpt-4o", configured=999_000) == 999_000


def test_resolve_falls_back_to_table_when_unconfigured() -> None:
    assert resolve_context_window(model_name="gpt-4o", configured=None) == 128_000


def test_resolve_unknown_model_and_unconfigured_is_none() -> None:
    assert resolve_context_window(model_name="totally-unknown-model", configured=None) is None
