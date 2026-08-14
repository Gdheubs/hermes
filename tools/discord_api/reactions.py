"""Discord outbound reaction actions, aligned to REST v10 (Feature M3).

Discord Omniscience campaign (EPIC #79564), Phase 2A. A typed, validated
builder for Discord reaction REST calls so an agent can add/remove/list
reactions without hand-rolling request shapes.

Emoji forms supported:
  - Unicode emoji (a single grapheme cluster, e.g. "👍")
  - Custom guild emoji in Discord's ``name:id`` form (e.g. "hermes:123456")

The module is a pure request builder — it returns validated ``(method, path,
payload, query)`` descriptors for a transport layer to execute, so it is fully
unit-testable without live Discord and never makes an unbounded network call
itself.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional
from urllib.parse import quote

__all__ = [
    "ReactionError",
    "validate_emoji",
    "encode_emoji_path",
    "add_reaction_request",
    "remove_own_reaction_request",
    "remove_user_reaction_request",
    "remove_all_reactions_request",
    "list_reactions_request",
    "MAX_REACTION_PAGE",
]

# Discord's GET reaction listing default/max (REST v10 resources/emoji.mdx).
MAX_REACTION_PAGE = 100

_CUSTOM_EMOJI_RE = re.compile(r"^[A-Za-z0-9_]{2,32}:[0-9]{15,20}$")
# Reject forms that could smuggle a ping or whitespace into a URL.
_FORBIDDEN = re.compile(r"[\s/@#]|@everyone|@here")


class ReactionError(ValueError):
    """Raised for an invalid emoji or reaction request."""


def validate_emoji(emoji: str) -> str:
    """Validate and return the canonical emoji token (unicode or ``name:id``).

    A custom emoji must match Discord's ``name:id`` form (snowflake id). A
    unicode emoji is a grapheme cluster: every codepoint is a symbol or
    combining/ZWJ sequence with at least one symbol codepoint. Rejects
    surrounding whitespace, embedded pings, and mistaken plain-ASCII text.
    """
    if not isinstance(emoji, str) or not emoji:
        raise ReactionError("emoji must be a non-empty string")
    stripped = emoji.strip()
    if stripped != emoji:
        raise ReactionError("emoji must not have surrounding whitespace")
    if _FORBIDDEN.search(emoji):
        raise ReactionError(f"emoji {emoji!r} contains forbidden characters")
    if _CUSTOM_EMOJI_RE.match(emoji):
        return emoji
    # Unicode emoji cluster: no plain-ASCII letters/digits (which would be a
    # mistaken word or a malformed custom emoji), and at least one symbol
    # codepoint (So/Sm/Sc/Sk) among combining/ZWJ/emoji-modifier codepoints.
    codepoints = list(emoji)
    if any(c.isascii() and c.isalnum() for c in codepoints):
        raise ReactionError(
            f"{emoji!r} is not a recognized emoji; use a unicode emoji or custom 'name:id'"
        )
    if any(unicodedata.category(c).startswith("S") for c in codepoints):
        return emoji
    raise ReactionError(f"{emoji!r} is not a recognized emoji")


def encode_emoji_path(emoji: str) -> str:
    """Percent-encode an emoji for safe inclusion in a REST URL path."""
    return quote(validate_emoji(emoji), safe="")


def _base_path(channel_id: str, message_id: str) -> str:
    if not str(channel_id).isdigit():
        raise ReactionError(f"channel_id must be a snowflake, got {channel_id!r}")
    if not str(message_id).isdigit():
        raise ReactionError(f"message_id must be a snowflake, got {message_id!r}")
    return f"/channels/{channel_id}/messages/{message_id}"


def add_reaction_request(channel_id: str, message_id: str, emoji: str) -> dict:
    """Build the PUT to add the caller's own reaction to a message."""
    base = _base_path(channel_id, message_id)
    enc = encode_emoji_path(emoji)
    return {
        "method": "PUT",
        "path": f"{base}/reactions/{enc}/@me",
        "payload": None,
        "query": {},
    }


def remove_own_reaction_request(channel_id: str, message_id: str, emoji: str) -> dict:
    """Build the DELETE to remove the caller's own reaction."""
    base = _base_path(channel_id, message_id)
    enc = encode_emoji_path(emoji)
    return {
        "method": "DELETE",
        "path": f"{base}/reactions/{enc}/@me",
        "payload": None,
        "query": {},
    }


def remove_user_reaction_request(
    channel_id: str, message_id: str, emoji: str, user_id: str
) -> dict:
    """Build the DELETE to remove another user's reaction (requires perms)."""
    if not str(user_id).isdigit():
        raise ReactionError(f"user_id must be a snowflake, got {user_id!r}")
    base = _base_path(channel_id, message_id)
    enc = encode_emoji_path(emoji)
    return {
        "method": "DELETE",
        "path": f"{base}/reactions/{enc}/{user_id}",
        "payload": None,
        "query": {},
    }


def remove_all_reactions_request(channel_id: str, message_id: str) -> dict:
    """Build the DELETE that removes every reaction on a message."""
    base = _base_path(channel_id, message_id)
    return {
        "method": "DELETE",
        "path": f"{base}/reactions",
        "payload": None,
        "query": {},
    }


def list_reactions_request(
    channel_id: str, message_id: str, emoji: str, *, limit: int = 25
) -> dict:
    """Build the GET that lists users who reacted with ``emoji``.

    ``limit`` is clamped to the Discord max of 100.
    """
    base = _base_path(channel_id, message_id)
    enc = encode_emoji_path(emoji)
    n = max(1, min(int(limit), MAX_REACTION_PAGE))
    return {
        "method": "GET",
        "path": f"{base}/reactions/{enc}",
        "payload": None,
        "query": {"limit": str(n)},
    }
