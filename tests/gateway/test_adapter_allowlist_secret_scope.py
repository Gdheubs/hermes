"""Allowlist / gate env reads must honor the active profile secret scope.

These adapter-level reads sit in front of gateway authz. A bare
``os.getenv`` under ``gateway.multiplex_profiles`` would borrow the
default profile's allowlist (or allow-all flag) for a secondary
profile. Same shape as the Matrix recovery-key fix (#69090) and the
Slack app-token pattern (#59739).
"""
import pytest

from agent import secret_scope as ss
from plugins.platforms.matrix.adapter import _startup_env_secret as matrix_secret
from gateway.platforms.signal import _startup_env_secret as signal_secret
from plugins.platforms.email.adapter import _get_secret as email_secret
from gateway.platforms.whatsapp_common import _get_wsecret as whatsapp_secret


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


class TestMatrixAllowedUsersScope:
    def test_scoped_secondary_uses_own_allowlist(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@default:example.org")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"MATRIX_ALLOWED_USERS": "@second:example.org"})
        try:
            assert matrix_secret("MATRIX_ALLOWED_USERS") == "@second:example.org"
        finally:
            ss.reset_secret_scope(token)

    def test_scoped_missing_does_not_borrow_environ(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@default:example.org")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"SOME_OTHER_KEY": "x"})
        try:
            assert matrix_secret("MATRIX_ALLOWED_USERS") == ""
        finally:
            ss.reset_secret_scope(token)


class TestSignalAllowedUsersScope:
    def test_scoped_secondary_uses_own_allowlist(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "+15550001")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"SIGNAL_ALLOWED_USERS": "+15550002"})
        try:
            assert signal_secret("SIGNAL_ALLOWED_USERS", "*") == "+15550002"
        finally:
            ss.reset_secret_scope(token)

    def test_scoped_missing_does_not_borrow_environ(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "+15550001")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"SOME_OTHER_KEY": "x"})
        try:
            assert signal_secret("SIGNAL_ALLOWED_USERS", "*") == "*"
        finally:
            ss.reset_secret_scope(token)


class TestEmailGatewayGatesScope:
    def test_scoped_missing_allow_all_does_not_borrow_environ(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"EMAIL_ADDRESS": "b@example.com"})
        try:
            assert email_secret("GATEWAY_ALLOW_ALL_USERS", "") == ""
        finally:
            ss.reset_secret_scope(token)


class TestWhatsAppGatewayAllowAllScope:
    def test_scoped_missing_does_not_borrow_environ(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"WHATSAPP_ALLOWED_USERS": "x"})
        try:
            assert (whatsapp_secret("GATEWAY_ALLOW_ALL_USERS", default="") or "") == ""
        finally:
            ss.reset_secret_scope(token)
