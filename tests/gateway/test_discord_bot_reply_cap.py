"""Behavioral tests for bounded Discord bot-to-bot reply chains."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform
from plugins.platforms.discord.adapter import DiscordAdapter


def _message(*, bot: bool, parent=None, message_id: int):
    reference = None
    if parent is not None:
        reference = SimpleNamespace(message_id=parent.id, resolved=parent)
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=message_id + 1000, bot=bot),
        reference=reference,
        channel=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_default_cap_allows_four_replies_then_stops_the_next():
    adapter = object.__new__(DiscordAdapter)
    setattr(adapter, "config", SimpleNamespace(extra={}))

    human = _message(bot=False, message_id=1)
    initial_tag = _message(bot=True, parent=human, message_id=2)
    first_reply = _message(bot=True, parent=initial_tag, message_id=3)
    second_reply = _message(bot=True, parent=first_reply, message_id=4)
    third_reply = _message(bot=True, parent=second_reply, message_id=5)
    fourth_reply = _message(bot=True, parent=third_reply, message_id=6)

    assert await adapter._discord_bot_reply_cap_reached(third_reply) is False
    assert await adapter._discord_bot_reply_cap_reached(fourth_reply) is True


@pytest.mark.asyncio
async def test_custom_cap_and_zero_disable_are_honored():
    adapter = object.__new__(DiscordAdapter)
    human = _message(bot=False, message_id=11)
    initial_tag = _message(bot=True, parent=human, message_id=12)
    first_reply = _message(bot=True, parent=initial_tag, message_id=13)
    second_reply = _message(bot=True, parent=first_reply, message_id=14)

    setattr(adapter, "config", SimpleNamespace(extra={"bot_reply_cap": 2}))
    assert await adapter._discord_bot_reply_cap_reached(first_reply) is False
    assert await adapter._discord_bot_reply_cap_reached(second_reply) is True

    setattr(adapter, "config", SimpleNamespace(extra={"bot_reply_cap": 0}))
    assert await adapter._discord_bot_reply_cap_reached(second_reply) is False


@pytest.mark.parametrize("configured", [-1, True, "invalid", None])
def test_invalid_reply_caps_fall_back_to_safe_default(configured):
    adapter = object.__new__(DiscordAdapter)
    setattr(adapter, "config", SimpleNamespace(extra={"bot_reply_cap": configured}))

    assert adapter._discord_bot_reply_cap() == 4


@pytest.mark.asyncio
async def test_reply_ancestors_are_fetched_when_discord_does_not_resolve_them():
    adapter = object.__new__(DiscordAdapter)
    setattr(adapter, "config", SimpleNamespace(extra={"bot_reply_cap": 1}))
    parent = _message(bot=True, message_id=21)
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=parent))
    child = _message(bot=True, message_id=22)
    child.channel = channel
    child.reference = SimpleNamespace(message_id=parent.id, resolved=None)

    assert await adapter._discord_bot_reply_cap_reached(child) is True
    channel.fetch_message.assert_awaited_once_with(parent.id)


@pytest.mark.asyncio
async def test_unresolvable_reply_ancestor_fails_closed():
    adapter = object.__new__(DiscordAdapter)
    setattr(adapter, "config", SimpleNamespace(extra={"bot_reply_cap": 4}))
    adapter.platform = Platform.DISCORD
    channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=RuntimeError("unavailable")))
    message = _message(bot=True, message_id=23)
    message.channel = channel
    message.reference = SimpleNamespace(message_id=22, resolved=None)

    assert await adapter._discord_bot_reply_cap_reached(message) is True


@pytest.mark.asyncio
async def test_dispatch_drops_message_when_bot_reply_cap_is_reached():
    adapter = object.__new__(DiscordAdapter)
    setattr(adapter, "config", SimpleNamespace(extra={}))
    adapter._ready_event = asyncio.Event()
    adapter._ready_event.set()
    adapter._discord_message_admission = Mock(return_value=(True, False))
    adapter._discord_bot_reply_cap_reached = AsyncMock(return_value=True)
    adapter._handle_message = AsyncMock(return_value=True)
    message = SimpleNamespace(id=9)

    assert await adapter._dispatch_discord_message(message) is False
    adapter._handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_drops_message_when_bot_reply_cap_is_reached():
    adapter = object.__new__(DiscordAdapter)
    setattr(adapter, "config", SimpleNamespace(extra={}))
    adapter._get_parent_channel_id = Mock(return_value=None)
    adapter._discord_channel_keys = Mock(return_value=set())
    adapter._discord_free_response_channels = Mock(return_value={"*"})
    adapter._discord_message_admission = Mock(return_value=(True, False))
    adapter._discord_bot_reply_cap_reached = AsyncMock(return_value=True)
    adapter._handle_message = AsyncMock(return_value=True)
    message = SimpleNamespace(id=10, channel=SimpleNamespace())

    assert await adapter._dispatch_recovered_message(message) is False
    adapter._handle_message.assert_not_awaited()
