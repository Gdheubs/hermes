"""Tests for tools.discord_api.permissions request builders (feature A2)."""

import pytest

from tools.discord_api.permissions import (
    PermissionOverwriteError,
    delete_channel_permission_request,
    set_channel_permission_request,
)

CHANNEL = "123456789012345678"
OVERWRITE = "987654321098765432"


class TestPermissionOverwriteError:
    def test_is_valueerror_subclass(self):
        assert issubclass(PermissionOverwriteError, ValueError)


class TestSetChannelPermissionRequest:
    def test_payload_allow_deny_type(self):
        req = set_channel_permission_request(CHANNEL, OVERWRITE, allow=1024, deny=8, type_=1)
        assert req["method"] == "PUT"
        assert req["path"] == f"/channels/{CHANNEL}/permissions/{OVERWRITE}"
        assert req["payload"] == {"allow": 1024, "deny": 8, "type": 1}

    def test_defaults_allow_deny_zero(self):
        req = set_channel_permission_request(CHANNEL, OVERWRITE, type_=0)
        assert req["payload"] == {"allow": 0, "deny": 0, "type": 0}

    def test_member_type(self):
        req = set_channel_permission_request(CHANNEL, OVERWRITE, type_=0)
        assert req["payload"]["type"] == 0

    def test_role_type(self):
        req = set_channel_permission_request(CHANNEL, OVERWRITE, type_=1)
        assert req["payload"]["type"] == 1

    def test_accepts_int_snowflakes(self):
        req = set_channel_permission_request(123456789012345678, 987654321098765432, type_=1)
        assert req["path"] == "/channels/123456789012345678/permissions/987654321098765432"

    @pytest.mark.parametrize("bad", [2, -1, "1", 1.0, True, None])
    def test_invalid_type_rejected(self, bad):
        with pytest.raises(PermissionOverwriteError):
            set_channel_permission_request(CHANNEL, OVERWRITE, type_=bad)

    @pytest.mark.parametrize("bad", [-1, -1024])
    def test_negative_allow_rejected(self, bad):
        with pytest.raises(PermissionOverwriteError):
            set_channel_permission_request(CHANNEL, OVERWRITE, allow=bad, type_=0)

    @pytest.mark.parametrize("bad", [-1, -1024])
    def test_negative_deny_rejected(self, bad):
        with pytest.raises(PermissionOverwriteError):
            set_channel_permission_request(CHANNEL, OVERWRITE, deny=bad, type_=0)

    def test_non_int_allow_rejected(self):
        with pytest.raises(PermissionOverwriteError):
            set_channel_permission_request(CHANNEL, OVERWRITE, allow="8", type_=0)

    def test_bool_allow_rejected(self):
        with pytest.raises(PermissionOverwriteError):
            set_channel_permission_request(CHANNEL, OVERWRITE, allow=True, type_=0)

    @pytest.mark.parametrize(
        "bad", ["", "abc", "-1", "12x", "1.5", 1.5, True, None, 2**63]
    )
    def test_invalid_snowflake_rejected(self, bad):
        with pytest.raises(PermissionOverwriteError):
            set_channel_permission_request(bad, OVERWRITE, type_=0)
        with pytest.raises(PermissionOverwriteError):
            set_channel_permission_request(CHANNEL, bad, type_=0)


class TestDeleteChannelPermissionRequest:
    def test_shape(self):
        req = delete_channel_permission_request(CHANNEL, OVERWRITE)
        assert req["method"] == "DELETE"
        assert req["path"] == f"/channels/{CHANNEL}/permissions/{OVERWRITE}"
        assert req["payload"] is None

    def test_invalid_channel_rejected(self):
        with pytest.raises(PermissionOverwriteError):
            delete_channel_permission_request("", OVERWRITE)

    def test_invalid_overwrite_rejected(self):
        with pytest.raises(PermissionOverwriteError):
            delete_channel_permission_request(CHANNEL, -5)
