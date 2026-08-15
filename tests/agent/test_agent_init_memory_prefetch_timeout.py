"""Regression coverage for the external-memory prefetch config handoff."""

import math
import time

import pytest

from agent.agent_init import _external_prefetch_timeout_from_config
from agent.memory_manager import MemoryManager
from tests.agent.test_memory_provider import BlockingPrefetchProvider

HUGE_INT = int("1" + "0" * 309)


def test_configured_external_prefetch_timeout_is_parsed():
    assert (
        _external_prefetch_timeout_from_config({"external_prefetch_timeout": "0.5"})
        == 0.5
    )
    assert (
        _external_prefetch_timeout_from_config({"external_prefetch_timeout": 60}) == 60
    )


@pytest.mark.parametrize(
    "raw",
    [
        True,
        -1,
        HUGE_INT,
        1e308,
        math.nan,
        math.inf,
        -math.inf,
        "nan",
        "inf",
        "1e999",
    ],
)
def test_malformed_or_nonfinite_timeout_uses_manager_default(raw):
    assert (
        _external_prefetch_timeout_from_config({"external_prefetch_timeout": raw})
        is None
    )


@pytest.mark.parametrize("raw", [HUGE_INT, 1e308, math.nan, math.inf, -math.inf])
def test_memory_manager_rejects_direct_invalid_timeouts(raw):
    with pytest.raises(ValueError, match="external_prefetch_timeout"):
        MemoryManager(external_prefetch_timeout=raw)


def test_configured_half_second_timeout_bounds_external_recall():
    manager = MemoryManager(
        external_prefetch_timeout=_external_prefetch_timeout_from_config({
            "external_prefetch_timeout": 0.5
        })
    )
    provider = BlockingPrefetchProvider()
    manager.add_provider(provider)

    started = time.monotonic()
    assert manager.prefetch_all("relevant query") == ""
    elapsed = time.monotonic() - started

    assert provider.started.is_set()
    assert 0.45 <= elapsed < 0.8
    provider.release.set()


def test_missing_or_invalid_external_prefetch_timeout_uses_manager_default():
    assert _external_prefetch_timeout_from_config({}) is None
    assert (
        _external_prefetch_timeout_from_config({"external_prefetch_timeout": "invalid"})
        is None
    )
    assert (
        _external_prefetch_timeout_from_config({"external_prefetch_timeout": 0}) is None
    )
