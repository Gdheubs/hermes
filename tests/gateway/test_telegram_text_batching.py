"""Tests for Telegram text message aggregation.

When a user sends a long message, Telegram clients split it into multiple
updates.  The TelegramAdapter should buffer rapid successive text messages
from the same session and aggregate them before dispatching.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.session import build_session_key


def _make_adapter():
    """Create a minimal TelegramAdapter for testing text batching."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    config = PlatformConfig(enabled=True, token="test-token")
    adapter = object.__new__(TelegramAdapter)
    adapter._platform = Platform.TELEGRAM
    adapter.platform = Platform.TELEGRAM
    adapter.config = config
    adapter._running = True
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._drop_delayed_deliveries = False
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._pending_photo_batches = {}
    adapter._pending_photo_batch_tasks = {}
    adapter._media_group_events = {}
    adapter._media_group_tasks = {}
    adapter._polling_error_task = None
    adapter._polling_heartbeat_task = None
    adapter._app = None
    adapter._bot = None
    adapter._set_status_indicator = AsyncMock()
    adapter._release_platform_lock = lambda: None
    adapter._text_batch_delay_seconds = 0.1  # fast for tests
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._message_handler = AsyncMock()
    adapter.handle_message = AsyncMock()
    return adapter


def _make_event(text: str, chat_id: str = "12345") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"),
    )


def _make_text_update(text: str = "received text"):
    message = SimpleNamespace(
        message_id=42,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(
            id=100,
            type="private",
            title=None,
            full_name="Test User",
            is_forum=False,
        ),
        from_user=SimpleNamespace(
            id=1,
            full_name="Test User",
            first_name="Test",
            is_bot=False,
        ),
        reply_to_message=None,
        date=None,
        location=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        media_group_id=None,
    )
    return SimpleNamespace(update_id=1, message=message, effective_message=None)


class TestTextBatching:
    @pytest.mark.asyncio
    async def test_single_message_dispatched_after_delay(self):
        adapter = _make_adapter()
        event = _make_event("hello world")

        adapter._enqueue_text_event(event)

        # Not dispatched yet
        adapter.handle_message.assert_not_called()

        # Wait for flush
        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.text == "hello world"

    @pytest.mark.asyncio
    async def test_split_messages_aggregated(self):
        """Two rapid messages from the same chat should be merged."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("This is part one of a long"))
        await asyncio.sleep(0.02)  # small gap, within batch window
        adapter._enqueue_text_event(_make_event("message that was split by Telegram."))

        # Not dispatched yet (timer restarted)
        adapter.handle_message.assert_not_called()

        # Wait for flush
        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert "part one" in dispatched.text
        assert "split by Telegram" in dispatched.text

    @pytest.mark.asyncio
    async def test_three_way_split_aggregated(self):
        """Three rapid messages should all merge."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("chunk 1"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("chunk 2"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("chunk 3"))

        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        text = adapter.handle_message.call_args[0][0].text
        assert "chunk 1" in text
        assert "chunk 2" in text
        assert "chunk 3" in text

    def test_text_batch_key_falls_back_to_secondary_adapter_profile(self):
        """A secondary adapter owns its profile before its handler can stamp it."""
        adapter = _make_adapter()
        adapter.profile_name = "pilot"
        event = _make_event("secondary profile text")

        assert event.source.profile is None
        assert adapter._text_batch_key(event) == build_session_key(
            event.source,
            group_sessions_per_user=adapter.config.extra.get(
                "group_sessions_per_user", True
            ),
            thread_sessions_per_user=adapter.config.extra.get(
                "thread_sessions_per_user", False
            ),
            profile="pilot",
        )

    @pytest.mark.parametrize(
        ("routed_profile", "adapter_profile", "expected_profile"),
        [
            ("pilot", "secondary", "pilot"),
            (None, "pilot", "pilot"),
            (None, None, None),
        ],
        ids=["shared_route_wins", "secondary_adapter_fallback", "legacy_main"],
    )
    @pytest.mark.asyncio
    async def test_receive_path_uses_profile_aware_text_batch_key(
        self, routed_profile, adapter_profile, expected_profile
    ):
        """Text ingress must preserve a routed profile before debounce enqueue."""
        adapter = _make_adapter()
        adapter.profile_name = adapter_profile
        adapter.gateway_runner = SimpleNamespace(
            _profile_name_for_source=lambda source: routed_profile
        )
        adapter._is_user_authorized_from_message = lambda message: True
        adapter._should_process_message = lambda message, **kwargs: True
        adapter._ensure_forum_commands = AsyncMock()
        adapter._cache_replied_media = AsyncMock()
        adapter._apply_telegram_group_observe_attribution = lambda event: event
        adapter._clean_bot_trigger_text = lambda text: text

        try:
            await adapter._handle_text_message(
                _make_text_update(), SimpleNamespace()
            )

            assert len(adapter._pending_text_batches) == 1
            batch_key, event = next(iter(adapter._pending_text_batches.items()))
            expected_session_key = build_session_key(
                event.source,
                group_sessions_per_user=adapter.config.extra.get(
                    "group_sessions_per_user", True
                ),
                thread_sessions_per_user=adapter.config.extra.get(
                    "thread_sessions_per_user", False
                ),
                profile=expected_profile,
            )
            assert event.source.profile == routed_profile
            assert batch_key == expected_session_key
        finally:
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_disconnected_adapter_drops_pending_media_group_flush_before_dispatch(self):
        """A pending media group should not dispatch after disconnect starts."""
        from plugins.platforms.telegram.adapter import TelegramAdapter

        adapter = _make_adapter()
        event = _make_event("album caption")
        event.media_urls = ["/tmp/photo.jpg"]
        event.media_types = ["image/jpeg"]

        with patch.object(TelegramAdapter, "MEDIA_GROUP_WAIT_SECONDS", 0.1):
            await adapter._queue_media_group_event("album-1", event)
            adapter._mark_disconnected()
            await asyncio.sleep(0.2)

        adapter.handle_message.assert_not_called()
        assert adapter._media_group_events == {}
        assert adapter._media_group_tasks == {}


    @pytest.mark.asyncio
    async def test_disconnect_cancels_all_pending_delivery_task_maps(self):
        """Photo/media/polling delayed tasks are awaited and queues are cleared."""
        adapter = _make_adapter()
        tasks = [asyncio.create_task(asyncio.sleep(0.2)) for _ in range(4)]
        adapter._pending_text_batches["text"] = _make_event("text")
        adapter._pending_text_batch_tasks["text"] = tasks[0]
        adapter._pending_photo_batches["photo"] = _make_event("photo")
        adapter._pending_photo_batch_tasks["photo"] = tasks[1]
        adapter._media_group_events["media"] = _make_event("media")
        adapter._media_group_tasks["media"] = tasks[2]
        adapter._polling_error_task = tasks[3]

        await adapter.disconnect()

        assert all(task.done() for task in tasks)
        assert adapter._pending_text_batches == {}
        assert adapter._pending_text_batch_tasks == {}
        assert adapter._pending_photo_batches == {}
        assert adapter._pending_photo_batch_tasks == {}
        assert adapter._media_group_events == {}
        assert adapter._media_group_tasks == {}
        assert adapter._polling_error_task is None
