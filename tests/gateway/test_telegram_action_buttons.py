from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
import plugins.platforms.telegram.adapter as telegram_mod
from plugins.platforms.telegram.adapter import TelegramAdapter, check_telegram_requirements
from tools.action_buttons_gateway import get_entry, register, resolve
from tools.action_buttons_tool import action_buttons_tool


_SECRET_TOKEN = "123456789:***"


def _make_connected_adapter() -> TelegramAdapter:
    assert check_telegram_requirements()
    config = PlatformConfig(enabled=True, token=_SECRET_TOKEN, extra={})
    adapter = TelegramAdapter(config)
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    return adapter


@pytest.mark.asyncio
async def test_send_action_buttons_renders_compact_numeric_buttons(monkeypatch):
    adapter = _make_connected_adapter()
    sent = SimpleNamespace(message_id=42)
    adapter._send_message_with_thread_fallback = AsyncMock(return_value=sent)

    class _Button:
        def __init__(self, text, callback_data):
            self.text = text
            self.callback_data = callback_data

    class _Markup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    monkeypatch.setattr(telegram_mod, "InlineKeyboardButton", _Button)
    monkeypatch.setattr(telegram_mod, "InlineKeyboardMarkup", _Markup)

    result = await adapter.send_action_buttons(
        "123",
        "Deploy where?",
        ["staging", "production"],
        "aid",
        "session-1",
    )

    assert result.success is True
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "1. staging" in kwargs["text"]
    assert "2. production" in kwargs["text"]
    rows = kwargs["reply_markup"].inline_keyboard
    assert len(rows) == 1
    assert rows[0][0].text == "1"
    assert rows[0][0].callback_data == "act:aid:0"
    assert rows[0][1].text == "2"
    assert rows[0][1].callback_data == "act:aid:1"
    assert adapter._action_button_state["aid"] == "session-1"


@pytest.mark.asyncio
async def test_action_button_callback_resolves_full_choice_and_clears_keyboard():
    adapter = _make_connected_adapter()
    action_id = "aid-callback"
    register(action_id, "session-1", "Deploy where?", ["staging", "production"])
    adapter._action_button_state[action_id] = "session-1"

    query = SimpleNamespace(
        data=f"act:{action_id}:1",
        from_user=SimpleNamespace(id="123", first_name="Ivan", username="ivan"),
        message=SimpleNamespace(
            chat_id=123,
            message_id=77,
            message_thread_id=None,
            chat=SimpleNamespace(type="private"),
        ),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)

    adapter._is_callback_user_authorized = lambda *args, **kwargs: True

    await adapter._handle_callback_query(update, SimpleNamespace())

    assert adapter._action_button_state.get(action_id) is None
    entry = get_entry(action_id)
    assert entry is not None
    assert entry.response == "production"
    query.answer.assert_awaited_once_with(text="✅ 2")
    assert "production" in query.edit_message_text.await_args.kwargs["text"]


def test_action_buttons_tool_uses_callback_response():
    out = action_buttons_tool(
        "Choose",
        ["one", "two"],
        callback=lambda question, choices: choices[1],
    )

    assert '"user_response": "two"' in out


def test_action_buttons_gateway_register_resolve_roundtrip():
    action_id = "aid-roundtrip"
    register(action_id, "session-1", "Choose", ["one", "two"])

    assert resolve(action_id, "two") is True
    entry = get_entry(action_id)
    assert entry is not None
    assert entry.response == "two"
