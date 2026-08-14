"""Regression coverage for profile-aware adapter busy-session keys."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


class _DummyAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, *args, **kwargs) -> SendResult:
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str):
        return None


def _event(*, profile: str | None = None) -> MessageEvent:
    return MessageEvent(
        text="routed follow-up",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            chat_type="dm",
            user_id="user-1",
            profile=profile,
        ),
        message_id="message-1",
    )


def _adapter(*, profile_name: str | None = None) -> _DummyAdapter:
    adapter = _DummyAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.TELEGRAM,
    )
    adapter.profile_name = profile_name
    adapter.set_message_handler(AsyncMock(return_value=None))
    adapter._busy_text_mode = ""
    adapter._start_session_processing = MagicMock()
    return adapter


@pytest.mark.asyncio
async def test_busy_session_preserves_profile_already_routed_on_event():
    """A route stamped at ingress must win before the busy-session lookup."""
    adapter = _adapter(profile_name="secondary")
    event = _event(profile="pilot")
    routed_key = build_session_key(event.source, profile="pilot")
    legacy_key = build_session_key(event.source, profile=None)
    assert routed_key != legacy_key
    adapter._active_sessions[routed_key] = asyncio.Event()

    await adapter.handle_message(event)

    assert event.source.profile == "pilot"
    assert routed_key in adapter._pending_messages
    assert legacy_key not in adapter._pending_messages
    adapter._start_session_processing.assert_not_called()
