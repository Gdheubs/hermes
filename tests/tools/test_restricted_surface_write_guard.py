"""Tests for the sensitive-path write guard on messaging platforms.

Run with: python -m pytest tests/tools/test_restricted_surface_write_guard.py -v
"""

import contextlib
import unittest

from tools.approval import reset_current_session_key, set_current_session_key
from tools.file_tools import _check_sensitive_messaging_path, _session_platform


@contextlib.contextmanager
def _bind_key(key: str):
    token = set_current_session_key(key)
    try:
        yield
    finally:
        reset_current_session_key(token)


class TestSessionPlatform(unittest.TestCase):
    def test_gateway_key_parses_platform(self):
        with _bind_key("agent:main:telegram:dm:123"):
            self.assertEqual(_session_platform(), "telegram")

    def test_named_profile_gateway_key(self):
        with _bind_key("agent:cissp-coach:discord:g:9"):
            self.assertEqual(_session_platform(), "discord")

    def test_unregistered_platform_token_is_none(self):
        with _bind_key("agent:main:not_a_real_platform:dm:1"):
            self.assertIsNone(_session_platform())

    def test_local_platform_is_none(self):
        with _bind_key("agent:main:local:dm:1"):
            self.assertIsNone(_session_platform())

    def test_malformed_key_is_none(self):
        with _bind_key("agent:main"):
            self.assertIsNone(_session_platform())

    def test_cli_key_has_no_platform(self):
        with _bind_key("default"):
            self.assertIsNone(_session_platform())
        with _bind_key("20260811_093018_2cb019f0"):
            self.assertIsNone(_session_platform())

    def test_no_key_binds_none(self):
        self.assertIsNone(_session_platform())


class TestSensitiveMessagingPathWrite(unittest.TestCase):
    def test_telegram_cron_write_blocked(self):
        with _bind_key("agent:main:telegram:dm:1"):
            err = _check_sensitive_messaging_path("~/.hermes/cron/jobs.json")
            self.assertIsNotNone(err)
            self.assertIn("BLOCKED", err)
            self.assertIn("telegram", err)

    def test_telegram_scripts_write_blocked(self):
        with _bind_key("agent:main:telegram:dm:1"):
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.hermes/scripts/evil.sh")
            )

    def test_telegram_ssh_write_blocked(self):
        with _bind_key("agent:main:telegram:dm:1"):
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.ssh/id_ed25519")
            )

    def test_telegram_unrelated_paths_allowed(self):
        with _bind_key("agent:main:telegram:dm:1"):
            self.assertIsNone(_check_sensitive_messaging_path("/tmp/note.md"))
            # Non-execution hermes paths (logs, etc.) remain writable
            self.assertIsNone(
                _check_sensitive_messaging_path("~/.hermes/logs/app.log")
            )

    def test_cli_never_blocked(self):
        with _bind_key("default"):
            self.assertIsNone(
                _check_sensitive_messaging_path("~/.hermes/cron/jobs.json")
            )
        with _bind_key("20260811_093018_2cb019f0"):
            self.assertIsNone(
                _check_sensitive_messaging_path("~/.hermes/scripts/x.sh")
            )

    def test_unregistered_platform_not_blocked(self):
        with _bind_key("agent:main:not_a_real_platform:dm:1"):
            self.assertIsNone(
                _check_sensitive_messaging_path("~/.hermes/cron/jobs.json")
            )

    def test_local_platform_not_blocked(self):
        with _bind_key("agent:main:local:dm:1"):
            self.assertIsNone(
                _check_sensitive_messaging_path("~/.hermes/cron/jobs.json")
            )

    def test_other_gateway_platforms_blocked(self):
        with _bind_key("agent:main:slack:dm:9"):
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.hermes/cron/jobs.json")
            )
        with _bind_key("agent:main:whatsapp:dm:7"):
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.ssh/authorized_keys")
            )


if __name__ == "__main__":
    unittest.main()
