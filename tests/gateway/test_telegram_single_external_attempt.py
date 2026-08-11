from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter


class FloodError(Exception):
    retry_after = 30.0


def _adapter_with_send(side_effect) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._should_attempt_rich = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    adapter._bot = SimpleNamespace(send_message=AsyncMock(side_effect=side_effect))
    return adapter


def test_adapter_advertises_single_external_attempt_capability() -> None:
    assert TelegramAdapter.supports_single_external_attempt is True


@pytest.mark.asyncio
async def test_single_external_attempt_disables_internal_network_retry(monkeypatch) -> None:
    adapter = _adapter_with_send(NetworkError("connection lost after write"))
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send(
        "12345",
        "hello",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result.success is False
    assert adapter._bot.send_message.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_send_keeps_existing_network_retry_behavior(monkeypatch) -> None:
    adapter = _adapter_with_send(
        [
            NetworkError("first failure"),
            NetworkError("second failure"),
            SimpleNamespace(message_id=901),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send("12345", "hello", metadata={"notify": True})

    assert result.success is True
    assert result.message_id == "901"
    assert adapter._bot.send_message.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_single_external_attempt_does_not_sleep_for_flood_retry(monkeypatch) -> None:
    adapter = _adapter_with_send(FloodError("retry after 30"))
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send(
        "12345",
        "hello",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result.success is False
    assert adapter._bot.send_message.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_external_attempt_preserves_safe_deleted_reply_fallback() -> None:
    adapter = _adapter_with_send(
        [
            BadRequest("Message to be replied not found"),
            SimpleNamespace(message_id=902),
        ]
    )

    result = await adapter.send(
        "12345",
        "hello",
        reply_to="999",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result.success is True
    assert result.message_id == "902"
    assert adapter._bot.send_message.await_count == 2
    calls = adapter._bot.send_message.await_args_list
    assert calls[0].kwargs["reply_to_message_id"] == 999
    assert calls[1].kwargs["reply_to_message_id"] is None


@pytest.mark.asyncio
async def test_single_external_attempt_preserves_safe_markdown_parse_fallback() -> None:
    adapter = _adapter_with_send(
        [
            BadRequest("can't parse entities"),
            SimpleNamespace(message_id=903),
        ]
    )

    result = await adapter.send(
        "12345",
        "hello _world_",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result.success is True
    assert result.message_id == "903"
    assert adapter._bot is not None
    calls = adapter._bot.send_message.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["parse_mode"] is not None
    assert calls[1].kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_shared_send_wrapper_honors_single_external_attempt(monkeypatch) -> None:
    adapter = _adapter_with_send(NetworkError("connection lost after write"))
    sleep = AsyncMock()
    monkeypatch.setattr("gateway.platforms.base.asyncio.sleep", sleep)

    result = await adapter._send_with_retry(
        "12345",
        "hello",
        metadata={"single_external_attempt": True, "notify": True},
        max_retries=2,
        base_delay=0,
    )

    assert result.success is False
    assert adapter._bot.send_message.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_external_attempt_rejects_multi_chunk_before_first_send() -> None:
    adapter = _adapter_with_send(SimpleNamespace(message_id=903))
    object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", 12)

    result = await adapter.send(
        "12345",
        "this response requires multiple Telegram chunks",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result.success is False
    assert result.error_kind == "too_long"
    assert adapter._bot is not None
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_wrapper_never_fallback_sends_for_single_attempt() -> None:
    adapter = _adapter_with_send(SimpleNamespace(message_id=904))
    first_failure = SendResult(
        success=False,
        error="definitive formatting rejection",
        error_kind="formatting",
        retryable=False,
    )
    adapter.send = AsyncMock(
        side_effect=[first_failure, SendResult(success=True, message_id="duplicate")]
    )

    result = await adapter._send_with_retry(
        "12345",
        "hello",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result is first_failure
    assert adapter.send.await_count == 1
