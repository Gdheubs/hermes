"""Tests for Discord reaction request builders (tools/discord_api/reactions.py).

Feature M3 of the Discord Omniscience campaign (EPIC #79564).
"""

import pytest

from tools.discord_api.reactions import (
    MAX_REACTION_PAGE,
    ReactionError,
    add_reaction_request,
    encode_emoji_path,
    list_reactions_request,
    remove_all_reactions_request,
    remove_own_reaction_request,
    remove_user_reaction_request,
    validate_emoji,
)


# ── Emoji validation ─────────────────────────────────────────────────────────
def test_unicode_emoji_accepted():
    assert validate_emoji("\U0001f44d") == "\U0001f44d"  # thumbs up


def test_custom_emoji_accepted():
    assert validate_emoji("hermes:123456789012345678") == "hermes:123456789012345678"


def test_emoji_whitespace_rejected():
    with pytest.raises(ReactionError):
        validate_emoji("  ok")
    with pytest.raises(ReactionError):
        validate_emoji("\U0001f44d ")


def test_emoji_with_ping_smuggling_rejected():
    with pytest.raises(ReactionError):
        validate_emoji("<@123>")
    with pytest.raises(ReactionError):
        validate_emoji("x@everyone")


def test_plain_ascii_text_rejected():
    with pytest.raises(ReactionError):
        validate_emoji("OK")


def test_empty_emoji_rejected():
    with pytest.raises(ReactionError):
        validate_emoji("")


# ── URL encoding ─────────────────────────────────────────────────────────────
def test_custom_emoji_path_encoded():
    # Colon must be percent-encoded in the path.
    assert encode_emoji_path("hermes:123456789012345678") == "hermes%3A123456789012345678"


def test_unicode_emoji_path_encoded():
    assert encode_emoji_path("\U0001f44d") == "%F0%9F%91%8D"


# ── Request shapes ───────────────────────────────────────────────────────────
def test_add_reaction_request_shape():
    r = add_reaction_request("111", "222", "\U0001f44d")
    assert r["method"] == "PUT"
    assert r["path"].startswith("/channels/111/messages/222/reactions/")
    assert r["path"].endswith("/@me")
    assert r["payload"] is None


def test_remove_own_reaction_request_shape():
    r = remove_own_reaction_request("111", "222", "hermes:123456789012345678")
    assert r["method"] == "DELETE"
    assert "hermes%3A123456789012345678" in r["path"]
    assert r["path"].endswith("/@me")


def test_remove_user_reaction_request_shape():
    r = remove_user_reaction_request("111", "222", "\U0001f44d", "999")
    assert r["method"] == "DELETE"
    assert r["path"].endswith("/999")


def test_remove_all_reactions_shape():
    r = remove_all_reactions_request("111", "222")
    assert r["method"] == "DELETE"
    assert r["path"] == "/channels/111/messages/222/reactions"


def test_list_reactions_query_and_clamp():
    r = list_reactions_request("111", "222", "\U0001f44d", limit=10)
    assert r["method"] == "GET"
    assert r["query"] == {"limit": "10"}
    r2 = list_reactions_request("111", "222", "\U0001f44d", limit=9999)
    assert int(r2["query"]["limit"]) == MAX_REACTION_PAGE


def test_invalid_snowflake_rejected():
    with pytest.raises(ReactionError):
        add_reaction_request("not-a-snowflake", "222", "\U0001f44d")
    with pytest.raises(ReactionError):
        list_reactions_request("111", "x", "\U0001f44d")
    with pytest.raises(ReactionError):
        remove_user_reaction_request("111", "222", "\U0001f44d", "not-an-id")
