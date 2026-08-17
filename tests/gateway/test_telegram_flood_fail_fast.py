"""Telegram send flood-control retry must fail fast on long waits.

Regression test for the 2026-08-14 incident: a RetryAfter of ~2 minutes
blocked the legacy send path (and with it message processing) for the whole
wait, because the send loop slept on any ``retry_after`` without a cap.
The edit path already treated ``wait > 5s`` as a hard failure so streaming
could fall back; the send path now mirrors that behaviour.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter


class _FloodError(Exception):
    """Minimal stand-in for ``telegram.error.RetryAfter`` (carries retry_after)."""

    def __init__(self, retry_after):
        super().__init__("Flood control exceeded. Retry in %d seconds" % retry_after)
        self.retry_after = retry_after


def _make_adapter(monkeypatch, send_side_effect):
    """Build a TelegramAdapter whose legacy send path uses ``send_side_effect``."""
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    # Force the legacy MarkdownV2 path (skip the rich-message fast path).
    monkeypatch.setattr(adapter, "_should_attempt_rich", lambda *a, **k: False)
    adapter._bot = AsyncFloodBot(send_side_effect)
    return adapter


class AsyncFloodBot:
    """send_message stub: pops one side effect per call until exhausted."""

    def __init__(self, side_effects):
        self._queue = list(side_effects)
        self.calls = 0

    async def send_message(self, *args, **kwargs):
        self.calls += 1
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return SimpleNamespace(message_id="ok")


@pytest.mark.asyncio
async def test_send_long_flood_wait_fails_fast_without_sleep(monkeypatch):
    """A RetryAfter > 5s must fail immediately — never sleep on the send path."""
    sleeps = []
    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    adapter = _make_adapter(monkeypatch, [_FloodError(retry_after=117.0)])
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    result = await adapter.send("12345", "hello")

    assert isinstance(result, SendResult)
    assert result.success is False
    assert result.retryable is False
    assert result.retry_after == 117.0
    assert result.error_kind == "rate_limited"
    # The long wait must NOT be slept through — fail fast instead.
    assert sleeps == [], f"send() slept on a long flood wait: {sleeps}"


@pytest.mark.asyncio
async def test_send_short_flood_wait_still_retries(monkeypatch):
    """A short RetryAfter (<= 5s) is still retried inline and can succeed."""
    sleeps = []
    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    # First call raises a short flood, second call succeeds.
    adapter = _make_adapter(monkeypatch, [_FloodError(retry_after=2.0)])
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    result = await adapter.send("12345", "hello")

    assert result.success is True
    assert sleeps == [2.0], f"expected one inline retry sleep(2.0), got {sleeps}"
