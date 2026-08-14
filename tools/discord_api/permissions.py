"""Discord channel-permission overwrite REST request builders (API v10).

Pure request-builder helpers aligned with the Discord REST v10
``topics/permissions`` documentation. These functions never perform I/O;
they validate inputs and return request descriptors::

    {
        "method": "PUT",
        "path": "/channels/{channel_id}/permissions/{overwrite_id}",
        "payload": {"allow": ..., "deny": ..., "type": ...},
    }

Invalid inputs raise :class:`PermissionOverwriteError` (a ``ValueError``
subclass).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Union

__all__ = [
    "PermissionOverwriteError",
    "TYPE_MEMBER",
    "TYPE_ROLE",
    "set_channel_permission_request",
    "delete_channel_permission_request",
]

# Discord snowflakes are unsigned 63-bit integers (max 2^63 - 1).
MAX_SNOWFLAKE: int = (1 << 63) - 1

# Overwrite types: 0 = member, 1 = role (REST v10 permissions docs).
TYPE_MEMBER: int = 0
TYPE_ROLE: int = 1
_VALID_TYPES: tuple = (TYPE_MEMBER, TYPE_ROLE)

_SNOWFLAKE_RE = re.compile(r"^\d{1,20}$")


class PermissionOverwriteError(ValueError):
    """Raised when a permission-overwrite request cannot be built."""


def _validate_snowflake(value: Any, name: str) -> str:
    """Validate a Discord snowflake and return its canonical string form."""
    if isinstance(value, bool):
        raise PermissionOverwriteError(f"{name} must be a snowflake, got {value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        if not _SNOWFLAKE_RE.fullmatch(value):
            raise PermissionOverwriteError(
                f"{name} must be a snowflake (digits only), got {value!r}"
            )
        number = int(value)
    else:
        raise PermissionOverwriteError(f"{name} must be a snowflake, got {value!r}")
    if number < 0 or number > MAX_SNOWFLAKE:
        raise PermissionOverwriteError(
            f"{name} must be within [0, 2^63 - 1], got {value!r}"
        )
    return str(number)


def _validate_bitfield(value: Any, name: str) -> int:
    """Validate a permission bitfield (non-negative integer)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise PermissionOverwriteError(
            f"{name} must be an integer bitfield, got {value!r}"
        )
    if value < 0:
        raise PermissionOverwriteError(
            f"{name} must be a non-negative bitfield, got {value}"
        )
    return value


def _validate_type(type_: Any) -> int:
    if isinstance(type_, bool) or not isinstance(type_, int) or type_ not in _VALID_TYPES:
        raise PermissionOverwriteError(
            f"type_ must be 0 (member) or 1 (role), got {type_!r}"
        )
    return type_


def set_channel_permission_request(
    channel_id: Union[int, str],
    overwrite_id: Union[int, str],
    *,
    allow: int = 0,
    deny: int = 0,
    type_: int,
) -> Dict[str, Any]:
    """Build a request descriptor for PUT /channels/{channel}/permissions/{overwrite}.

    ``type_`` selects the overwrite target: ``0`` for a member, ``1`` for a
    role. ``allow``/``deny`` are non-negative permission bitfields.
    """
    channel = _validate_snowflake(channel_id, "channel_id")
    overwrite = _validate_snowflake(overwrite_id, "overwrite_id")
    allow_bits = _validate_bitfield(allow, "allow")
    deny_bits = _validate_bitfield(deny, "deny")
    overwrite_type = _validate_type(type_)
    return {
        "method": "PUT",
        "path": f"/channels/{channel}/permissions/{overwrite}",
        "payload": {
            "allow": allow_bits,
            "deny": deny_bits,
            "type": overwrite_type,
        },
    }


def delete_channel_permission_request(
    channel_id: Union[int, str],
    overwrite_id: Union[int, str],
) -> Dict[str, Any]:
    """Build a request descriptor for DELETE /channels/{channel}/permissions/{overwrite}."""
    channel = _validate_snowflake(channel_id, "channel_id")
    overwrite = _validate_snowflake(overwrite_id, "overwrite_id")
    return {
        "method": "DELETE",
        "path": f"/channels/{channel}/permissions/{overwrite}",
        "payload": None,
    }
