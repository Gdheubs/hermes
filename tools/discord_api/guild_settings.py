"""Discord REST v10 guild-settings request builder (approved scalars only).

Pure request-builder: validates scalar guild settings per the REST v10
resources/guild.mdx contract and produces a PATCH /guilds/{guild_id}
request descriptor. No I/O is performed here.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict

__all__ = [
    "GuildSettingsError",
    "edit_guild_request",
    "NAME_MAX",
    "DESCRIPTION_MAX",
    "AFK_TIMEOUT_MIN",
    "AFK_TIMEOUT_MAX",
]

# ---------------------------------------------------------------------------
# Constants (Discord REST v10 resources/guild.mdx)
# ---------------------------------------------------------------------------

#: Maximum guild name length.
NAME_MAX = 100
#: Maximum guild description length.
DESCRIPTION_MAX = 1024
#: Guild verification level: 0 disabled .. 4 highest.
VERIFICATION_LEVEL_MIN, VERIFICATION_LEVEL_MAX = 0, 4
#: Default message notifications: 0 all messages, 1 only mentions.
DEFAULT_MESSAGE_NOTIFICATIONS_MIN, DEFAULT_MESSAGE_NOTIFICATIONS_MAX = 0, 1
#: Explicit content filter: 0 disabled, 1 members w/o roles, 2 all members.
EXPLICIT_CONTENT_FILTER_MIN, EXPLICIT_CONTENT_FILTER_MAX = 0, 2
#: NSFW level: 0 default .. 3 age restricted.
NSFW_LEVEL_MIN, NSFW_LEVEL_MAX = 0, 3
#: AFK timeout in seconds.
AFK_TIMEOUT_MIN, AFK_TIMEOUT_MAX = 60, 3600

#: Snowflakes are unsigned 64-bit integers.
_SNOWFLAKE_MAX = (1 << 64) - 1
_SNOWFLAKE_RE = re.compile(r"^[0-9]{1,20}$")


class GuildSettingsError(ValueError):
    """Raised when an invalid guild-settings field or value is supplied."""


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_snowflake(value: Any, field: str) -> Any:
    """Validate a Discord snowflake (int or decimal string) for ``field``."""
    if isinstance(value, bool):
        raise GuildSettingsError(f"{field!r} must be a snowflake, not a bool")
    if isinstance(value, int):
        if not 0 <= value <= _SNOWFLAKE_MAX:
            raise GuildSettingsError(
                f"{field!r} snowflake out of range: {value!r}"
            )
        return value
    if isinstance(value, str):
        if not _SNOWFLAKE_RE.match(value) or int(value) > _SNOWFLAKE_MAX:
            raise GuildSettingsError(
                f"{field!r} must be a decimal snowflake, got {value!r}"
            )
        return value
    raise GuildSettingsError(
        f"{field!r} must be a snowflake (int or decimal str), "
        f"got {type(value).__name__}"
    )


def _validate_str(
    value: Any, field: str, max_len: int, allow_none: bool = False
) -> Any:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise GuildSettingsError(f"{field!r} must be a string")
    if len(value) > max_len:
        raise GuildSettingsError(f"{field!r} exceeds {max_len} characters")
    return value


def _validate_int_range(value: Any, field: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GuildSettingsError(f"{field!r} must be an integer")
    if not lo <= value <= hi:
        raise GuildSettingsError(f"{field!r} must be between {lo} and {hi}")
    return value


def _validate_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise GuildSettingsError(f"{field!r} must be a boolean")
    return value


def _validate_optional_snowflake(value: Any, field: str) -> Any:
    if value is None:
        return None
    return _validate_snowflake(value, field)


# ---------------------------------------------------------------------------
# Approved scalar fields (REST v10 resources/guild.mdx, scalars only)
# ---------------------------------------------------------------------------

_FIELD_VALIDATORS: Dict[str, Callable[[Any], Any]] = {
    "name": lambda v: _validate_str(v, "name", NAME_MAX),
    "description": lambda v: _validate_str(
        v, "description", DESCRIPTION_MAX, allow_none=True
    ),
    "verification_level": lambda v: _validate_int_range(
        v, "verification_level", VERIFICATION_LEVEL_MIN, VERIFICATION_LEVEL_MAX
    ),
    "default_message_notifications": lambda v: _validate_int_range(
        v,
        "default_message_notifications",
        DEFAULT_MESSAGE_NOTIFICATIONS_MIN,
        DEFAULT_MESSAGE_NOTIFICATIONS_MAX,
    ),
    "explicit_content_filter": lambda v: _validate_int_range(
        v,
        "explicit_content_filter",
        EXPLICIT_CONTENT_FILTER_MIN,
        EXPLICIT_CONTENT_FILTER_MAX,
    ),
    "nsfw_level": lambda v: _validate_int_range(
        v, "nsfw_level", NSFW_LEVEL_MIN, NSFW_LEVEL_MAX
    ),
    "premium_progress_bar_enabled": lambda v: _validate_bool(
        v, "premium_progress_bar_enabled"
    ),
    "system_channel_id": lambda v: _validate_optional_snowflake(
        v, "system_channel_id"
    ),
    "rules_channel_id": lambda v: _validate_optional_snowflake(
        v, "rules_channel_id"
    ),
    "public_updates_channel_id": lambda v: _validate_optional_snowflake(
        v, "public_updates_channel_id"
    ),
    "afk_timeout": lambda v: _validate_int_range(
        v, "afk_timeout", AFK_TIMEOUT_MIN, AFK_TIMEOUT_MAX
    ),
}


def edit_guild_request(guild_id: Any, **fields: Any) -> Dict[str, Any]:
    """Build a validated PATCH /guilds/{guild_id} request descriptor.

    Only the approved scalar guild-settings keys (see ``_FIELD_VALIDATORS``)
    may be passed; anything else raises :class:`GuildSettingsError`. The
    returned descriptor contains only the fields that were provided.

    Returns:
        dict: ``{"method": "PATCH", "path": "/guilds/{guild_id}",
        "json": {validated provided fields}}``.
    """
    guild_id = _validate_snowflake(guild_id, "guild_id")
    payload: Dict[str, Any] = {}
    for key, value in fields.items():
        validator = _FIELD_VALIDATORS.get(key)
        if validator is None:
            raise GuildSettingsError(f"unsupported guild setting: {key!r}")
        payload[key] = validator(value)
    return {
        "method": "PATCH",
        "path": f"/guilds/{guild_id}",
        "json": payload,
    }
