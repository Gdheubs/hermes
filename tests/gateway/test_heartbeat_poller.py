"""Gateway heartbeat polling must wake idle sessions without backlogging ticks."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _HeartbeatAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:  # pragma: no cover
        return True

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:  # pragma: no cover
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):  # pragma: no cover
        return {}


class _DueHeartbeatManager:
    due_calls = 0

    def __init__(self, session_id: str):
        self.session_id = session_id

    def has_heartbeat(self) -> bool:
        return True

    def due_prompt(self) -> str:
        type(self).due_calls += 1
        return "[Heartbeat]\ncheck status"


def _make_runner(monkeypatch):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="42",
        chat_type="dm",
        user_id="42",
    )
    key = build_session_key(source)
    adapter = _HeartbeatAdapter(
        PlatformConfig(enabled=True, token="test", typing_indicator=False),
        Platform.TELEGRAM,
    )

    runner = object.__new__(GatewayRunner)
    runner._heartbeat_watch = {key: (source, "session-1")}
    runner._adapter_for_source = lambda _source: adapter
    runner._peek_session_state = lambda _key: None

    from hermes_cli import heartbeat

    _DueHeartbeatManager.due_calls = 0
    monkeypatch.setattr(heartbeat, "HeartbeatManager", _DueHeartbeatManager)
    return runner, adapter, key


@pytest.mark.asyncio
async def test_idle_heartbeat_poll_wakes_adapter_without_user_message(monkeypatch):
    runner, adapter, _key = _make_runner(monkeypatch)
    handled = asyncio.Event()
    received: list[str] = []

    async def handler(event):
        received.append(event.text)
        handled.set()
        return None

    adapter.set_message_handler(handler)

    await runner._poll_heartbeat_watches_once()
    await asyncio.wait_for(handled.wait(), timeout=1)

    assert received == ["[Heartbeat]\ncheck status"]
    assert adapter._pending_messages == {}
    assert _DueHeartbeatManager.due_calls == 1


@pytest.mark.asyncio
async def test_busy_polls_leave_tick_due_then_dispatch_once_when_idle(monkeypatch):
    runner, adapter, key = _make_runner(monkeypatch)
    handled = asyncio.Event()
    received: list[str] = []

    async def handler(event):
        received.append(event.text)
        handled.set()
        return None

    adapter.set_message_handler(handler)
    adapter._active_sessions[key] = asyncio.Event()

    await runner._poll_heartbeat_watches_once()
    await runner._poll_heartbeat_watches_once()

    assert _DueHeartbeatManager.due_calls == 0
    assert received == []

    adapter._active_sessions.clear()
    await runner._poll_heartbeat_watches_once()
    await asyncio.wait_for(handled.wait(), timeout=1)

    assert received == ["[Heartbeat]\ncheck status"]
    assert _DueHeartbeatManager.due_calls == 1


@pytest.mark.asyncio
async def test_pending_user_followup_wins_before_due_tick_is_claimed(monkeypatch):
    runner, adapter, key = _make_runner(monkeypatch)
    adapter._pending_messages[key] = object()

    await runner._poll_heartbeat_watches_once()

    assert _DueHeartbeatManager.due_calls == 0
    assert adapter._pending_messages[key] is not None
