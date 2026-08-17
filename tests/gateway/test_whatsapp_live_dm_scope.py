"""Regression tests for _live_dm_allow_from secret-scope fix.

Before the fix, _live_dm_allow_from read os.environ directly instead of
calling _get_wsecret(). For a secondary multiplex profile whose allowlist
lives only in its .env (visible via profile scope, NOT in process
os.environ), every DM was denied.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest


def _build_cloud_adapter(monkeypatch, env: dict[str, str]):
    from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter
    for var in (
        "WHATSAPP_CLOUD_ALLOW_FROM",
        "WHATSAPP_CLOUD_ALLOWED_USERS",
        "WHATSAPP_CLOUD_ALLOW_ALL_USERS",
        "WHATSAPP_CLOUD_DM_POLICY",
        "WHATSAPP_DM_POLICY",
        "GATEWAY_ALLOW_ALL_USERS",
        "WHATSAPP_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config = MagicMock()
    config.extra = {
        "phone_number_id": "1234567890",
        "access_token": "test-token",
    }
    return WhatsAppCloudAdapter(config)


def test_live_dm_allow_from_uses_scoped_secret_not_os_environ(monkeypatch):
    """_live_dm_allow_from must call _get_wsecret, not os.environ.get.

    Simulate secondary multiplex profile: env var is NOT in os.environ
    but _get_wsecret returns it from the profile scope.
    """
    import gateway.platforms.whatsapp_common as wc
    adapter = _build_cloud_adapter(monkeypatch, {
        "WHATSAPP_CLOUD_ALLOWED_USERS": "15551234567",
        "WHATSAPP_CLOUD_DM_POLICY": "allowlist",
    })
    assert "15551234567" in adapter._allow_from

    monkeypatch.delenv("WHATSAPP_CLOUD_ALLOWED_USERS", raising=False)

    with patch.object(wc, "_get_wsecret", return_value="15551234567,15559876543"):
        live = adapter._live_dm_allow_from()

    assert "15551234567" in live, (
        "_live_dm_allow_from must use _get_wsecret — secondary profile users denied"
    )
    assert "15559876543" in live


def test_live_dm_allow_from_returns_empty_when_key_removed(monkeypatch):
    """When key is removed (sole-entry revoke), must return set()."""
    import gateway.platforms.whatsapp_common as wc
    adapter = _build_cloud_adapter(monkeypatch, {
        "WHATSAPP_CLOUD_ALLOWED_USERS": "15551234567",
        "WHATSAPP_CLOUD_DM_POLICY": "allowlist",
    })
    monkeypatch.delenv("WHATSAPP_CLOUD_ALLOWED_USERS", raising=False)

    with patch.object(wc, "_get_wsecret", return_value=None):
        live = adapter._live_dm_allow_from()

    assert live == set()


def test_live_dm_allow_from_profile_scope_overrides_global_env(monkeypatch):
    """Profile-scoped allowlist must override global os.environ value."""
    import gateway.platforms.whatsapp_common as wc
    adapter = _build_cloud_adapter(monkeypatch, {
        "WHATSAPP_CLOUD_ALLOWED_USERS": "global_user1,global_user2",
        "WHATSAPP_CLOUD_DM_POLICY": "allowlist",
    })

    with patch.object(wc, "_get_wsecret", return_value="profile_user_only"):
        live = adapter._live_dm_allow_from()

    assert "profile_user_only" in live
    assert "global_user1" not in live, (
        "Profile-scoped allowlist must override global os.environ value"
    )
