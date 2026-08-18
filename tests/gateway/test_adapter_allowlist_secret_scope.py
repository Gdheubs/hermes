"""Allowlist / gate reads must honor the active profile secret scope.

These adapter-level reads sit in front of gateway authz. A bare
``os.getenv`` under ``gateway.multiplex_profiles`` would borrow the
default profile's allowlist (or allow-all flag) for a secondary
profile. Same shape as the Matrix recovery-key fix (#69090) and the
Slack app-token pattern (#59739).
"""
from types import SimpleNamespace

import pytest

from agent import secret_scope as ss
from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.signal import SignalAdapter
from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin
from gateway.platforms.whatsapp_common import _get_wsecret as whatsapp_secret
from gateway.session import SessionSource
from plugins.platforms.email.adapter import EmailAdapter
from plugins.platforms.email.adapter import _get_secret as email_secret
from plugins.platforms.matrix.adapter import MatrixAdapter
from plugins.platforms.matrix.adapter import _apply_yaml_config
from plugins.platforms.matrix.adapter import _startup_env_secret as matrix_secret
from gateway.platforms.signal import _startup_env_secret as signal_secret


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def _signal_event(sender: str) -> MessageEvent:
    return MessageEvent(
        text="hi",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=SimpleNamespace(value="signal"),
            chat_id=sender,
            user_id=sender,
        ),
    )


class TestMatrixYamlAllowlistsSurviveScopedMiss:
    def test_yaml_hook_seeds_extra_and_skips_env_under_multiplex(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@default:example.org")
        monkeypatch.setenv("MATRIX_ALLOWED_ROOMS", "!default:example.org")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"SOME_OTHER_KEY": "x"})
        try:
            seeded = _apply_yaml_config(
                {},
                {
                    "allowed_users": ["@operator:example.org"],
                    "allowed_rooms": ["!private:example.org"],
                },
            )
            assert seeded["allowed_users"] == "@operator:example.org"
            assert seeded["allowed_rooms"] == "!private:example.org"
            import os
            assert os.environ["MATRIX_ALLOWED_USERS"] == "@default:example.org"
            assert os.environ["MATRIX_ALLOWED_ROOMS"] == "!default:example.org"
        finally:
            ss.reset_secret_scope(token)

    def test_adapter_uses_yaml_extra_not_default_profile_env(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@default:example.org")
        monkeypatch.setenv("MATRIX_ALLOWED_ROOMS", "!default:example.org")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"SOME_OTHER_KEY": "x"})
        try:
            seeded = _apply_yaml_config(
                {},
                {
                    "allowed_users": ["@operator:example.org"],
                    "allowed_rooms": ["!private:example.org"],
                },
            )
            config = PlatformConfig(enabled=True)
            config.extra = {
                "homeserver": "https://example.org",
                **seeded,
            }
            adapter = MatrixAdapter(config)
            assert adapter._allowed_user_ids == {"@operator:example.org"}
            assert adapter._allowed_rooms == {"!private:example.org"}
        finally:
            ss.reset_secret_scope(token)

    def test_scoped_env_value_wins_when_extra_absent(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@default:example.org")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"MATRIX_ALLOWED_USERS": "@second:example.org"})
        try:
            assert matrix_secret("MATRIX_ALLOWED_USERS") == "@second:example.org"
            config = PlatformConfig(enabled=True)
            config.extra = {"homeserver": "https://example.org"}
            adapter = MatrixAdapter(config)
            assert adapter._allowed_user_ids == {"@second:example.org"}
        finally:
            ss.reset_secret_scope(token)

    def test_unscoped_startup_still_reads_environ(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@default:example.org")
        assert matrix_secret("MATRIX_ALLOWED_USERS") == "@default:example.org"


class TestSignalReactionsFailClosedOnScopedMiss:
    def test_helper_scoped_miss_is_empty_not_star(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "+155****0001")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"SOME_OTHER_KEY": "x"})
        try:
            assert signal_secret("SIGNAL_ALLOWED_USERS", "") == ""
        finally:
            ss.reset_secret_scope(token)

    def test_adapter_reactions_closed_when_profile_has_no_allowlist(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "+155****0001")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"SOME_OTHER_KEY": "x"})
        try:
            config = PlatformConfig(enabled=True)
            config.extra = {"http_url": "http://localhost:8080", "account": "+155****4567"}
            adapter = SignalAdapter(config)
            assert adapter.dm_allow_from == set()
            event = _signal_event("+155****9999")
            assert adapter._reactions_enabled(event) is False
        finally:
            ss.reset_secret_scope(token)

    def test_explicit_star_still_opens_reactions(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "*")
        config = PlatformConfig(enabled=True)
        config.extra = {"http_url": "http://localhost:8080", "account": "+155****4567"}
        adapter = SignalAdapter(config)
        assert "*" in adapter.dm_allow_from
        assert adapter._reactions_enabled(_signal_event("+155****9999")) is True

    def test_scoped_profile_allowlist_is_used(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "+155****0001")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"SIGNAL_ALLOWED_USERS": "+155****0002"})
        try:
            config = PlatformConfig(enabled=True)
            config.extra = {"http_url": "http://localhost:8080", "account": "+155****4567"}
            adapter = SignalAdapter(config)
            assert adapter.dm_allow_from == {"+155****0002"}
            assert adapter._reactions_enabled(_signal_event("+155****0002")) is True
            assert adapter._reactions_enabled(_signal_event("+155****0001")) is False
        finally:
            ss.reset_secret_scope(token)


class TestEmailAndWhatsAppAdapterGates:
    def test_email_allow_all_does_not_borrow_environ(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
        monkeypatch.delenv("EMAIL_ALLOW_ALL_USERS", raising=False)
        monkeypatch.delenv("EMAIL_ALLOWED_USERS", raising=False)
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"EMAIL_ADDRESS": "b@example.com"})
        try:
            assert email_secret("GATEWAY_ALLOW_ALL_USERS", "") == ""
            config = PlatformConfig(enabled=True)
            adapter = EmailAdapter(config)
            assert adapter._allow_all_senders() is False
            assert adapter._allowlist_in_effect() is False
        finally:
            ss.reset_secret_scope(token)

    def test_whatsapp_open_dm_does_not_borrow_environ(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"WHATSAPP_ALLOWED_USERS": "x"})
        try:
            assert (whatsapp_secret("GATEWAY_ALLOW_ALL_USERS", default="") or "") == ""

            class _Host(WhatsAppBehaviorMixin):
                name = "whatsapp"

            assert _Host()._open_dm_opted_in() is False
        finally:
            ss.reset_secret_scope(token)
