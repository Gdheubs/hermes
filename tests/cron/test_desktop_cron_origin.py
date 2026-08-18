"""Desktop/TUI/CLI cron origin: stamp session_id so attach_to_session can mirror.

Desktop does not set HERMES_SESSION_PLATFORM / CHAT_ID. Without a fallback,
deliver=origin from a Desktop chat writes only a cron_* session the operator
never opens. Capture HERMES_SESSION_SOURCE + HERMES_SESSION_ID as origin so
mirror_to_session can append to that transcript. No gateway adapter is used.
"""

from unittest.mock import patch

from gateway.mirror import _find_session_id
from tools.cronjob_tools import _origin_from_env


def _session_env(env: dict):
    return patch(
        "gateway.session_context.get_session_env",
        side_effect=lambda name, default="": env.get(name, default),
    )


class TestDesktopOriginCapture:
    def test_desktop_session_id_becomes_origin(self):
        env = {
            "HERMES_SESSION_SOURCE": "desktop",
            "HERMES_SESSION_ID": "20260816_180049_da67a1",
        }
        with _session_env(env):
            origin = _origin_from_env()
        assert origin is not None
        assert origin["platform"] == "desktop"
        assert origin["chat_id"] == "20260816_180049_da67a1"
        assert origin["thread_id"] is None

    def test_tui_same_shape(self):
        env = {
            "HERMES_SESSION_SOURCE": "tui",
            "HERMES_SESSION_ID": "20260816_120000_abc123",
        }
        with _session_env(env):
            origin = _origin_from_env()
        assert origin is not None
        assert origin["platform"] == "tui"
        assert origin["chat_id"] == "20260816_120000_abc123"

    def test_messaging_platform_still_wins(self):
        env = {
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "-1004301769804",
            "HERMES_SESSION_SOURCE": "desktop",
            "HERMES_SESSION_ID": "should-not-win",
        }
        with _session_env(env):
            origin = _origin_from_env()
        assert origin is not None
        assert origin["platform"] == "telegram"
        assert origin["chat_id"] == "-1004301769804"

    def test_source_without_session_id_is_none(self):
        env = {"HERMES_SESSION_SOURCE": "desktop"}
        with _session_env(env):
            assert _origin_from_env() is None


class TestDesktopMirrorLookup:
    def test_find_session_id_returns_chat_id_for_desktop(self):
        assert (
            _find_session_id("desktop", "20260816_180049_da67a1")
            == "20260816_180049_da67a1"
        )
