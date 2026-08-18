from __future__ import annotations

from dataclasses import dataclass


class GroupError(Exception):
    code = "group_error"


class ValidationError(GroupError):
    code = "validation"


class NotFoundError(GroupError):
    code = "not_found"


class ConflictError(GroupError):
    code = "conflict"


class DeletedError(GroupError):
    code = "deleted"


class SchemaUnsupportedError(GroupError):
    code = "schema_unsupported"


@dataclass(frozen=True)
class BotMemberInput:
    bot_instance_id: str
    profile_name: str


@dataclass(frozen=True)
class GroupMember:
    bot_instance_id: str
    profile_name: str


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    color: str
    icon_kind: str
    icon_value: str
    leader_bot_instance_id: str
    revision: int
    created_at_ms: int
    updated_at_ms: int
    members: tuple[GroupMember, ...]


@dataclass(frozen=True)
class GroupMessage:
    id: str
    conversation_id: str
    sender_bot_instance_id: str | None
    sender_profile_name: str | None
    content: str
    created_at_ms: int
