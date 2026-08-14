"""Unit tests for the shared retry policy (knot.providers._retry).

Uses a monkeypatched, injectable clock (``asyncio.sleep`` replaced with a
fast recorder) so backoff waits never actually block the test suite.
"""

from __future__ import annotations

import pytest

from knot.providers import _retry
from knot.providers.provider import SimpleCancellationToken


def test_is_transient_status_covers_expected_codes() -> None:
    for code in (408, 409, 425, 429, 500, 502, 503, 529, 599):
        assert _retry.is_transient_status(code)
    for code in (400, 401, 403, 404, 422):
        assert not _retry.is_transient_status(code)


def test_retry_delay_seconds_grows_exponentially_and_is_capped() -> None:
    delays = [_retry.retry_delay_seconds(attempt, max_delay_seconds=1.0) for attempt in range(6)]
    assert delays[0] == pytest.approx(0.25)
    assert delays[1] == pytest.approx(0.5)
    assert delays[2] == pytest.approx(1.0)
    # Capped at max_delay_seconds beyond this point.
    assert delays[3] == pytest.approx(1.0)
    assert delays[5] == pytest.approx(1.0)


def test_retry_delay_seconds_zero_when_max_delay_is_zero() -> None:
    assert _retry.retry_delay_seconds(0, max_delay_seconds=0) == 0.0
    assert _retry.retry_delay_seconds(5, max_delay_seconds=0) == 0.0


def test_provider_retry_event_reports_human_readable_progress() -> None:
    event = _retry.provider_retry_event(
        attempt=0, max_retries=2, delay_seconds=0.5, reason="HTTP 429"
    )
    assert event.attempt == 2
    assert event.max_attempts == 3
    assert event.delay_seconds == 0.5
    assert "2/3" in event.message
    assert "HTTP 429" in event.message


async def test_wait_for_retry_returns_true_without_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(_retry, "sleep", fake_sleep)

    result = await _retry.wait_for_retry(0.3, signal=None)

    assert result is True
    assert sum(sleep_calls) == pytest.approx(0.3)


async def test_wait_for_retry_zero_delay_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_sleep(seconds: float) -> None:  # pragma: no cover - must not be called
        raise AssertionError("sleep should not be called for a zero delay")

    monkeypatch.setattr(_retry, "sleep", fail_sleep)

    assert await _retry.wait_for_retry(0, signal=None) is True


async def test_wait_for_retry_stops_immediately_when_already_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_sleep(seconds: float) -> None:  # pragma: no cover - must not be called
        raise AssertionError("sleep should not be called once cancelled")

    monkeypatch.setattr(_retry, "sleep", fail_sleep)
    signal = SimpleCancellationToken()
    signal.cancel()

    assert await _retry.wait_for_retry(0.3, signal=signal) is False


async def test_wait_for_retry_interrupted_mid_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    signal = SimpleCancellationToken()
    calls = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            signal.cancel()

    monkeypatch.setattr(_retry, "sleep", fake_sleep)
    monkeypatch.setattr(_retry, "RETRY_POLL_SECONDS", 0.05)

    result = await _retry.wait_for_retry(1.0, signal=signal)

    assert result is False
    # Backoff stopped early rather than sleeping out the full delay.
    assert calls < 1.0 / 0.05
