"""#68771: 503/529 "upstream capacity limits" overloads retry over a bounded
backoff window on the primary BEFORE the fallback chain activates.

The Z.AI overload path already gets a long adaptive backoff; this generalizes
that policy to provider-agnostic 503/529 overloads so a capacity outage gets
a few retries over ~a minute instead of one quick retry then fallback (or
give-up when no fallback chain is configured).
"""

from __future__ import annotations

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.retry_utils import (
    capacity_overload_backoff,
    capacity_overload_retry_ceiling,
    is_capacity_overload_error,
)


class _Err(Exception):
    def __init__(self, status_code, message="Service Unavailable"):
        super().__init__(message)
        self.status_code = status_code
        self.response = None


def test_503_and_529_are_capacity_overloads():
    assert is_capacity_overload_error(_Err(503)) is True
    assert is_capacity_overload_error(_Err(529)) is True
    assert is_capacity_overload_error(_Err(500)) is False
    assert is_capacity_overload_error(_Err(502)) is False
    assert is_capacity_overload_error(_Err(429)) is False
    assert is_capacity_overload_error(ValueError("no status")) is False


def test_503_classified_as_retryable_overloaded():
    result = classify_api_error(_Err(503), provider="nous")
    assert result.reason == FailoverReason.overloaded
    assert result.retryable is True


def test_capacity_backoff_schedule():
    # First retry keeps the caller's default (short) wait.
    wait, policy = capacity_overload_backoff(1, 2.0)
    assert wait == 2.0
    assert policy == "capacity_overload_short"
    # Long tier walks 5/10/20/40 (light jitter: delay in [base, base*1.2])
    # and caps at the last entry.
    waits = [capacity_overload_backoff(a, 2.0)[0] for a in range(2, 8)]
    expected_bases = [5.0, 10.0, 20.0, 40.0, 40.0, 40.0]
    for got, base in zip(waits, expected_bases):
        assert base <= got <= base * 1.2, f"{got} not in [{base}, {base * 1.2}]"
    assert waits[3] == pytest.approx(waits[4], rel=0.2)  # capped, same tier
    assert all(
        capacity_overload_backoff(a, 2.0)[1] == "capacity_overload_long"
        for a in range(2, 8)
    )


def test_capacity_ceiling_reaches_all_long_tiers():
    # 1 short + 4 long + 1 (the pre-backoff ceiling check) = 6
    assert capacity_overload_retry_ceiling() == 6


def _gate(is_rate_limited, is_transport, is_capacity, retry_count):
    """Mirror the conversation_loop _should_fallback expression."""
    return is_rate_limited or (
        is_transport and retry_count >= 2 and not is_capacity
    )


def test_capacity_overload_does_not_eager_fallback():
    # Capacity overloads must NOT trip the transport eager-fallback gate.
    assert _gate(False, True, True, 2) is False
    assert _gate(False, True, True, 5) is False
    # Other transport failures still fall back after one real retry.
    assert _gate(False, True, False, 2) is True
    # Rate limits stay immediate.
    assert _gate(True, False, False, 1) is True


def test_503_retries_beyond_default_budget_then_fallback():
    """Simulate the loop's retry/ceiling math for a 503: the primary must get
    retries past the default api_max_retries (3) — the eager-fallback gate
    stays off — and the loop only stops at the capacity ceiling, where the
    ceiling path activates fallback."""
    ceiling = capacity_overload_retry_ceiling()
    max_retries = max(3, ceiling)  # default api_max_retries, raised by the 503 path

    retry_count = 0
    while retry_count < max_retries:
        retry_count += 1
        # The eager-fallback gate must never fire during the window.
        assert _gate(False, True, True, retry_count) is False

    assert retry_count == ceiling
    assert retry_count > 3  # more than the default retry budget
