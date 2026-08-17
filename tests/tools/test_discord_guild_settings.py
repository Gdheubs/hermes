"""Tests for tools/discord_api.guild_settings (feature A5)."""

import pytest

from tools.discord_api.guild_settings import (
    GuildSettingsError,
    edit_guild_request,
)

GUILD_ID = "123456789012345678"
CHANNEL_ID = "987654321098765432"


# --- allowed scalar edits ---------------------------------------------------


def test_allowed_scalar_edits_full_payload():
    req = edit_guild_request(
        GUILD_ID,
        name="Hermes HQ",
        description="A fine place",
        verification_level=4,
        default_message_notifications=1,
        explicit_content_filter=2,
        nsfw_level=3,
        premium_progress_bar_enabled=True,
        system_channel_id=CHANNEL_ID,
        rules_channel_id=CHANNEL_ID,
        public_updates_channel_id=CHANNEL_ID,
        afk_timeout=3600,
    )
    assert req["method"] == "PATCH"
    assert req["path"] == f"/guilds/{GUILD_ID}"
    assert req["json"] == {
        "name": "Hermes HQ",
        "description": "A fine place",
        "verification_level": 4,
        "default_message_notifications": 1,
        "explicit_content_filter": 2,
        "nsfw_level": 3,
        "premium_progress_bar_enabled": True,
        "system_channel_id": CHANNEL_ID,
        "rules_channel_id": CHANNEL_ID,
        "public_updates_channel_id": CHANNEL_ID,
        "afk_timeout": 3600,
    }


def test_minimum_allowed_values():
    req = edit_guild_request(
        GUILD_ID,
        verification_level=0,
        default_message_notifications=0,
        explicit_content_filter=0,
        nsfw_level=0,
        premium_progress_bar_enabled=False,
        afk_timeout=60,
        description=None,
        system_channel_id=None,
    )
    assert req["json"]["verification_level"] == 0
    assert req["json"]["default_message_notifications"] == 0
    assert req["json"]["explicit_content_filter"] == 0
    assert req["json"]["nsfw_level"] == 0
    assert req["json"]["premium_progress_bar_enabled"] is False
    assert req["json"]["afk_timeout"] == 60
    assert req["json"]["description"] is None
    assert req["json"]["system_channel_id"] is None


# --- disallowed keys --------------------------------------------------------


@pytest.mark.parametrize(
    "bad_key", ["widget_enabled", "system_channel_flags", "bogus_field"]
)
def test_disallowed_key_rejected(bad_key):
    with pytest.raises(GuildSettingsError):
        edit_guild_request(GUILD_ID, **{bad_key: True})


# --- name bound -------------------------------------------------------------


def test_name_max_length_ok():
    req = edit_guild_request(GUILD_ID, name="x" * 100)
    assert len(req["json"]["name"]) == 100


def test_name_too_long_rejected():
    with pytest.raises(GuildSettingsError):
        edit_guild_request(GUILD_ID, name="x" * 101)


def test_name_must_be_string():
    with pytest.raises(GuildSettingsError):
        edit_guild_request(GUILD_ID, name=123)


# --- description bound ------------------------------------------------------


def test_description_max_ok():
    req = edit_guild_request(GUILD_ID, description="x" * 1024)
    assert len(req["json"]["description"]) == 1024


def test_description_too_long_rejected():
    with pytest.raises(GuildSettingsError):
        edit_guild_request(GUILD_ID, description="x" * 1025)


# --- verification_level range -----------------------------------------------


@pytest.mark.parametrize("level", [-1, 5, 100])
def test_verification_level_out_of_range(level):
    with pytest.raises(GuildSettingsError):
        edit_guild_request(GUILD_ID, verification_level=level)


@pytest.mark.parametrize("bad", [True, "3", 3.5])
def test_verification_level_wrong_type(bad):
    with pytest.raises(GuildSettingsError):
        edit_guild_request(GUILD_ID, verification_level=bad)


# --- snowflake channel id validation ----------------------------------------


@pytest.mark.parametrize(
    "field",
    ["system_channel_id", "rules_channel_id", "public_updates_channel_id"],
)
@pytest.mark.parametrize(
    "bad",
    ["not-a-snowflake", "123abc", -5, 2**64, 1.5, True],
)
def test_channel_id_invalid_rejected(field, bad):
    with pytest.raises(GuildSettingsError):
        edit_guild_request(GUILD_ID, **{field: bad})


def test_channel_id_none_allowed():
    req = edit_guild_request(GUILD_ID, system_channel_id=None)
    assert req["json"]["system_channel_id"] is None


def test_channel_id_int_and_str_accepted():
    req_int = edit_guild_request(GUILD_ID, rules_channel_id=123456789012345678)
    assert req_int["json"]["rules_channel_id"] == 123456789012345678
    req_str = edit_guild_request(GUILD_ID, rules_channel_id="123456789012345678")
    assert req_str["json"]["rules_channel_id"] == "123456789012345678"


def test_invalid_guild_id_rejected():
    with pytest.raises(GuildSettingsError):
        edit_guild_request("guild-abc")


# --- afk_timeout range ------------------------------------------------------


@pytest.mark.parametrize("timeout", [59, 3601, 0, -1])
def test_afk_timeout_out_of_range(timeout):
    with pytest.raises(GuildSettingsError):
        edit_guild_request(GUILD_ID, afk_timeout=timeout)


# --- only provided fields ---------------------------------------------------


def test_only_provided_fields_in_payload():
    req = edit_guild_request(GUILD_ID, name="Renamed")
    assert req["json"] == {"name": "Renamed"}


def test_empty_fields_yields_empty_payload():
    req = edit_guild_request(GUILD_ID)
    assert req["json"] == {}
    assert req["path"] == f"/guilds/{GUILD_ID}"
