"""Mechanical seam contract for the Task-10 Telegram extraction."""

import inspect
import typing
from types import SimpleNamespace

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as adapter_module
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.telegram_config_mention import (
    TelegramConfigMentionMixin,
)


MOVED_METHODS = (
    "format_message",
    "_telegram_require_mention",
    "_telegram_observe_unmentioned_group_messages",
    "_telegram_guest_mode",
    "_telegram_exclusive_bot_mentions",
    "_telegram_free_response_chats",
    "_telegram_free_response_topics",
    "_telegram_is_free_response_topic",
    "_telegram_allowed_chats",
    "_telegram_group_allowed_chats",
    "_telegram_observe_allowed_chats",
    "_telegram_allowed_topics",
    "_telegram_ignored_threads",
    "_compile_mention_patterns",
    "_is_group_chat",
    "_effective_message_thread_id",
    "_current_bot_username",
    "_note_bot_username",
    "_observe_bot_identity_from_message",
    "_bot_identity_is_fresh",
    "_refresh_bot_identity",
    "_is_reply_to_bot",
    "_extract_bot_mention_usernames",
    "_message_mentions_bot",
    "_schedule_bot_identity_recheck",
    "_explicit_bot_mentions_exclude_self",
    "_message_matches_mention_patterns",
    "_is_guest_mention",
    "_clean_bot_trigger_text",
    "_should_observe_unmentioned_group_message",
    "_telegram_group_observe_shared_source",
    "_telegram_group_observe_attributed_text",
    "_telegram_group_observe_channel_prompt",
    "_apply_telegram_group_observe_attribution",
    "_append_observed_note",
)


def test_task10_mixin_is_the_unique_composed_owner_and_is_introspectable():
    """Every authorized method resolves through one explicit MRO seam."""
    assert TelegramAdapter.__mro__.index(TelegramConfigMentionMixin) < TelegramAdapter.__mro__.index(
        adapter_module.BasePlatformAdapter
    )
    assert len(MOVED_METHODS) == 35
    mixin_globals = vars(__import__(
        "plugins.platforms.telegram.telegram_config_mention",
        fromlist=["*"],
    ))
    for name in MOVED_METHODS:
        assert name in TelegramConfigMentionMixin.__dict__, name
        assert name not in TelegramAdapter.__dict__, name
        assert inspect.getattr_static(TelegramAdapter, name) is inspect.getattr_static(
            TelegramConfigMentionMixin, name
        )
        typing.get_type_hints(
            getattr(TelegramConfigMentionMixin, name),
            globalns=mixin_globals,
            localns=mixin_globals,
        )


def test_task10_lazy_seam_reads_rebound_original_gate_environment(monkeypatch):
    """Callers patching adapter globals retain authority after extraction."""
    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(enabled=True, token="***", extra={})
    monkeypatch.setattr(
        adapter_module,
        "_scoped_gate_env",
        lambda name, default="": "-100" if name == "TELEGRAM_ALLOWED_CHATS" else default,
    )
    assert adapter._telegram_allowed_chats() == {"-100"}


def test_task10_composed_method_behavior_remains_available():
    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(
        enabled=True,
        token="***",
        extra={"require_mention": "yes", "allowed_topics": []},
    )
    adapter._bot = SimpleNamespace(id=999, username="hermes_bot")
    adapter._mention_patterns = []
    assert adapter._telegram_require_mention() is True
    assert adapter._append_observed_note("prior", "later") == "prior\n\nlater"
