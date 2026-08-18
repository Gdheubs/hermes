"""Validation and domain-error mapping for bot-group operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .models import (
    BotMemberInput,
    ConflictError,
    DeletedError,
    Group,
    GroupMember,
    GroupMessage,
    NotFoundError,
    ValidationError,
)
from .store import GroupStore

_COLORS = frozenset({"blue", "green", "yellow", "orange", "red", "purple", "pink", "gray"})
_ICON_KINDS = frozenset({"emoji", "codicon"})


class GroupService:
    """Apply public validation before delegating durable work to ``GroupStore``."""

    def __init__(self, store: GroupStore) -> None:
        self.store = store

    @staticmethod
    def _hash(payload: dict[str, Any]) -> str:
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(serialized.encode()).hexdigest()

    @classmethod
    def _idempotency_hash(
        cls, scope: str | None, payload: dict[str, Any]
    ) -> str | None:
        """Only provide a hash when the optional idempotency tuple is enabled."""
        return cls._hash(payload) if scope is not None else None

    @staticmethod
    def _validate_idempotency(scope: object, key: object) -> None:
        if (scope is None) != (key is None):
            raise ValidationError("idempotency scope and key must be supplied together")
        if scope is not None and (
            not isinstance(scope, str)
            or not scope.strip()
            or not isinstance(key, str)
            or not key.strip()
        ):
            raise ValidationError("idempotency scope and key must be nonblank strings")

    def _raise_mutation_failure(self, group_id: str) -> None:
        state = self.store.group_state(group_id)
        if state == "deleted":
            raise DeletedError("group is deleted")
        if state == "missing":
            raise NotFoundError("group was not found")
        raise ConflictError("group revision is stale")

    @staticmethod
    def _name(value: object) -> str:
        if not isinstance(value, str):
            raise ValidationError("name must be nonblank and at most 120 characters")
        name = value.strip()
        if not name or len(name) > 120:
            raise ValidationError("name must be nonblank and at most 120 characters")
        return name

    @staticmethod
    def _message_content(value: object) -> str:
        if not isinstance(value, str):
            raise ValidationError("message content must be a nonblank string")
        content = value.strip()
        if not content or len(content) > 32_768:
            raise ValidationError("message content must be between 1 and 32768 characters")
        return content

    @staticmethod
    def _icon(
        color: object, icon_kind: object, icon_value: object
    ) -> tuple[str, str, str]:
        if (
            not isinstance(color, str)
            or not isinstance(icon_kind, str)
            or color not in _COLORS
            or icon_kind not in _ICON_KINDS
            or not isinstance(icon_value, str)
            or not icon_value
            or len(icon_value) > 64
        ):
            raise ValidationError("invalid icon")
        return color, icon_kind, icon_value

    @staticmethod
    def _members(members: object) -> tuple[GroupMember, ...]:
        if not isinstance(members, list) or not members:
            raise ValidationError("at least one member is required")

        normalized: list[GroupMember] = []
        seen_ids: set[str] = set()
        for member in members:
            if (
                not isinstance(member, BotMemberInput)
                or not isinstance(member.bot_instance_id, str)
                or not member.bot_instance_id
                or not isinstance(member.profile_name, str)
                or not member.profile_name.strip()
                or member.bot_instance_id in seen_ids
            ):
                raise ValidationError("members must be distinct valid bot instances")
            seen_ids.add(member.bot_instance_id)
            normalized.append(
                GroupMember(member.bot_instance_id, member.profile_name.strip())
            )
        return tuple(normalized)

    @staticmethod
    def _require_member_leader(
        members: tuple[GroupMember, ...], leader_bot_instance_id: str
    ) -> None:
        if leader_bot_instance_id not in {member.bot_instance_id for member in members}:
            raise ValidationError("leader must be an active member")

    def create_group(
        self,
        *,
        name: object,
        color: object,
        icon_kind: object,
        icon_value: object,
        members: object,
        leader_bot_instance_id: str,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
    ) -> Group:
        normalized_name = self._name(name)
        normalized_color, normalized_icon_kind, normalized_icon_value = self._icon(
            color, icon_kind, icon_value
        )
        normalized_members = self._members(members)
        self._require_member_leader(normalized_members, leader_bot_instance_id)
        self._validate_idempotency(idempotency_scope, idempotency_key)
        request_hash = self._idempotency_hash(
            idempotency_scope,
            {
                "operation": "create_group",
                "name": normalized_name,
                "color": normalized_color,
                "icon_kind": normalized_icon_kind,
                "icon_value": normalized_icon_value,
                "members": [
                    (member.bot_instance_id, member.profile_name)
                    for member in normalized_members
                ],
                "leader_bot_instance_id": leader_bot_instance_id,
            },
        )
        return self.store.create_group(
            group_id=str(uuid.uuid4()),
            name=normalized_name,
            color=normalized_color,
            icon_kind=normalized_icon_kind,
            icon_value=normalized_icon_value,
            members=normalized_members,
            leader_bot_instance_id=leader_bot_instance_id,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    def update_metadata(
        self,
        group_id: str,
        *,
        expected_revision: int,
        name: object,
        color: object,
        icon_kind: object,
        icon_value: object,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
    ) -> Group:
        normalized_name = self._name(name)
        normalized_color, normalized_icon_kind, normalized_icon_value = self._icon(
            color, icon_kind, icon_value
        )
        self._validate_idempotency(idempotency_scope, idempotency_key)
        request_hash = self._idempotency_hash(
            idempotency_scope,
            {
                "operation": "metadata",
                "group_id": group_id,
                "expected_revision": expected_revision,
                "name": normalized_name,
                "color": normalized_color,
                "icon_kind": normalized_icon_kind,
                "icon_value": normalized_icon_value,
            },
        )
        group = self.store.update_metadata(
            group_id=group_id,
            expected_revision=expected_revision,
            name=normalized_name,
            color=normalized_color,
            icon_kind=normalized_icon_kind,
            icon_value=normalized_icon_value,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if group is None:
            self._raise_mutation_failure(group_id)
        assert group is not None
        return group

    def replace_membership(
        self,
        group_id: str,
        *,
        expected_revision: int,
        members: object,
        leader_bot_instance_id: str,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
    ) -> Group:
        normalized_members = self._members(members)
        self._require_member_leader(normalized_members, leader_bot_instance_id)
        self._validate_idempotency(idempotency_scope, idempotency_key)
        request_hash = self._idempotency_hash(
            idempotency_scope,
            {
                "operation": "membership",
                "group_id": group_id,
                "expected_revision": expected_revision,
                "members": [
                    (member.bot_instance_id, member.profile_name)
                    for member in normalized_members
                ],
                "leader_bot_instance_id": leader_bot_instance_id,
            },
        )
        group = self.store.replace_membership(
            group_id=group_id,
            expected_revision=expected_revision,
            members=normalized_members,
            leader_bot_instance_id=leader_bot_instance_id,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if group is None:
            self._raise_mutation_failure(group_id)
        assert group is not None
        return group

    def delete_group(
        self,
        group_id: str,
        *,
        expected_revision: int,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self._validate_idempotency(idempotency_scope, idempotency_key)
        request_hash = self._idempotency_hash(
            idempotency_scope,
            {
                "operation": "delete_group",
                "group_id": group_id,
                "expected_revision": expected_revision,
            },
        )
        deleted = self.store.delete_group(
            group_id=group_id,
            expected_revision=expected_revision,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if not deleted:
            self._raise_mutation_failure(group_id)

    def list_messages(self, group_id: str) -> list[GroupMessage]:
        state = self.store.group_state(group_id)
        if state == "deleted":
            raise DeletedError("group is deleted")
        if state == "missing":
            raise NotFoundError("group was not found")
        return self.store.list_messages(group_id)

    def append_message(
        self,
        group_id: str,
        *,
        content: object,
        sender_bot_instance_id: str | None = None,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
    ) -> GroupMessage:
        normalized_content = self._message_content(content)
        if sender_bot_instance_id is not None and (
            not isinstance(sender_bot_instance_id, str) or not sender_bot_instance_id.strip()
        ):
            raise ValidationError("message sender must be a valid bot instance id")
        self._validate_idempotency(idempotency_scope, idempotency_key)
        request_hash = self._idempotency_hash(
            idempotency_scope,
            {
                "operation": "append_message",
                "group_id": group_id,
                "sender_bot_instance_id": sender_bot_instance_id,
                "content": normalized_content,
            },
        )
        message = self.store.append_message(
            group_id=group_id,
            sender_bot_instance_id=sender_bot_instance_id,
            content=normalized_content,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if message is None:
            self._raise_mutation_failure(group_id)
        assert message is not None
        return message
