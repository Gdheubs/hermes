"""Private FastAPI surface for the Hermes Bot Mode desktop plugin."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
import sys

from fastapi import APIRouter, Body, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

# Hermes loads dashboard/plugin_api.py as a standalone module via
# importlib.util.spec_from_file_location(), while tests and normal Python users
# may import it as dashboard.plugin_api. Keep exactly one bot_groups module
# identity in either mode so dataclass/isinstance/error checks stay coherent.
if __package__:
    from .bot_groups import (
        BotMemberInput,
        ConflictError,
        DeletedError,
        Group,
        GroupError,
        GroupMessage,
        GroupService,
        GroupStore,
        NotFoundError,
        ValidationError,
    )
    from .bot_groups.store import SCHEMA_VERSION
else:
    _DASHBOARD_DIR = Path(__file__).resolve().parent
    if str(_DASHBOARD_DIR) not in sys.path:
        sys.path.insert(0, str(_DASHBOARD_DIR))

    from bot_groups import (
        BotMemberInput,
        ConflictError,
        DeletedError,
        Group,
        GroupError,
        GroupMessage,
        GroupService,
        GroupStore,
        NotFoundError,
        ValidationError,
    )
    from bot_groups.store import SCHEMA_VERSION


class MemberPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_instance_id: str
    profile_name: str


class CreateGroupPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    color: str
    icon_kind: str
    icon_value: str
    members: list[MemberPayload]
    leader_bot_instance_id: str
    idempotency_key: str | None = None


class UpdateGroupPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int
    name: str
    color: str
    icon_kind: str
    icon_value: str
    idempotency_key: str | None = None


class ReplaceMembershipPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int
    members: list[MemberPayload]
    leader_bot_instance_id: str
    idempotency_key: str | None = None


class DeleteGroupPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int
    idempotency_key: str | None = None


class BotIdentityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_name: str
    instance_id: str | None = None


class ReconcileBotsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bots: list[BotIdentityPayload]


class GroupMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    sender_bot_instance_id: str | None = None
    idempotency_key: str | None = None


def default_group_db_path() -> Path:
    """Return one installation-wide database path, independent of active profile."""
    from hermes_constants import get_default_hermes_root

    data_dir = get_default_hermes_root() / "plugins" / "hermes-bots"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "bot-groups.sqlite3"


@lru_cache(maxsize=1)
def get_group_service() -> GroupService:
    return GroupService(GroupStore(default_group_db_path()))


def _member_inputs(members: list[MemberPayload]) -> list[BotMemberInput]:
    return [
        BotMemberInput(
            bot_instance_id=member.bot_instance_id,
            profile_name=member.profile_name,
        )
        for member in members
    ]


def _group_payload(group: Group) -> dict:
    return {
        "id": group.id,
        "name": group.name,
        "color": group.color,
        "icon_kind": group.icon_kind,
        "icon_value": group.icon_value,
        "leader_bot_instance_id": group.leader_bot_instance_id,
        "revision": group.revision,
        "created_at_ms": group.created_at_ms,
        "updated_at_ms": group.updated_at_ms,
        "members": [
            {
                "bot_instance_id": member.bot_instance_id,
                "profile_name": member.profile_name,
            }
            for member in group.members
        ],
    }


def _message_payload(message: GroupMessage) -> dict:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "sender_bot_instance_id": message.sender_bot_instance_id,
        "sender_profile_name": message.sender_profile_name,
        "content": message.content,
        "created_at_ms": message.created_at_ms,
    }


def _idempotency_key(header_key: str | None, body_key: str | None) -> str | None:
    if header_key and body_key and header_key != body_key:
        raise ValidationError("idempotency key header and body must match")
    return header_key or body_key


def _http_error(error: GroupError) -> HTTPException:
    if isinstance(error, ConflictError):
        status_code = status.HTTP_409_CONFLICT
    elif isinstance(error, DeletedError):
        status_code = status.HTTP_410_GONE
    elif isinstance(error, NotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, ValidationError):
        status_code = status.HTTP_400_BAD_REQUEST
    else:
        status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": str(error)},
    )


def create_router(
    service_provider: Callable[[], GroupService] = get_group_service,
) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict:
        service_provider()
        return {"status": "ok", "schema_version": SCHEMA_VERSION}

    @router.get("/groups")
    def list_groups() -> list[dict]:
        return [_group_payload(group) for group in service_provider().store.list_groups()]

    @router.post("/bots/reconcile")
    def reconcile_bots(payload: ReconcileBotsPayload) -> dict:
        try:
            bots = service_provider().store.reconcile_bot_instances(
                [(bot.profile_name, bot.instance_id) for bot in payload.bots]
            )
        except GroupError as error:
            raise _http_error(error) from error
        return {"bots": bots}

    @router.post("/groups", status_code=status.HTTP_201_CREATED)
    def create_group(
        payload: CreateGroupPayload,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        try:
            idempotency_key = _idempotency_key(
                idempotency_key, payload.idempotency_key
            )
            group = service_provider().create_group(
                name=payload.name,
                color=payload.color,
                icon_kind=payload.icon_kind,
                icon_value=payload.icon_value,
                members=_member_inputs(payload.members),
                leader_bot_instance_id=payload.leader_bot_instance_id,
                idempotency_scope="api:create-group" if idempotency_key else None,
                idempotency_key=idempotency_key,
            )
        except GroupError as error:
            raise _http_error(error) from error
        return _group_payload(group)

    @router.get("/groups/{group_id}")
    def get_group(group_id: str) -> dict:
        service = service_provider()
        group = service.store.get_group(group_id)
        if group is not None:
            return _group_payload(group)
        state = service.store.group_state(group_id)
        error: GroupError
        if state == "deleted":
            error = DeletedError("group is deleted")
        else:
            error = NotFoundError("group was not found")
        raise _http_error(error)

    @router.get("/groups/{group_id}/messages")
    def list_group_messages(group_id: str) -> list[dict]:
        try:
            messages = service_provider().list_messages(group_id)
        except GroupError as error:
            raise _http_error(error) from error
        return [_message_payload(message) for message in messages]

    @router.post(
        "/groups/{group_id}/messages", status_code=status.HTTP_201_CREATED
    )
    def append_group_message(
        group_id: str,
        payload: GroupMessagePayload,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        try:
            idempotency_key = _idempotency_key(
                idempotency_key, payload.idempotency_key
            )
            message = service_provider().append_message(
                group_id,
                content=payload.content,
                sender_bot_instance_id=payload.sender_bot_instance_id,
                idempotency_scope=(
                    f"api:group:{group_id}:message" if idempotency_key else None
                ),
                idempotency_key=idempotency_key,
            )
        except GroupError as error:
            raise _http_error(error) from error
        return _message_payload(message)

    @router.patch("/groups/{group_id}")
    def update_group(
        group_id: str,
        payload: UpdateGroupPayload,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        try:
            idempotency_key = _idempotency_key(
                idempotency_key, payload.idempotency_key
            )
            group = service_provider().update_metadata(
                group_id,
                expected_revision=payload.expected_revision,
                name=payload.name,
                color=payload.color,
                icon_kind=payload.icon_kind,
                icon_value=payload.icon_value,
                idempotency_scope=(
                    f"api:group:{group_id}:metadata" if idempotency_key else None
                ),
                idempotency_key=idempotency_key,
            )
        except GroupError as error:
            raise _http_error(error) from error
        return _group_payload(group)

    @router.put("/groups/{group_id}/membership")
    def replace_membership(
        group_id: str,
        payload: ReplaceMembershipPayload,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        try:
            idempotency_key = _idempotency_key(
                idempotency_key, payload.idempotency_key
            )
            group = service_provider().replace_membership(
                group_id,
                expected_revision=payload.expected_revision,
                members=_member_inputs(payload.members),
                leader_bot_instance_id=payload.leader_bot_instance_id,
                idempotency_scope=(
                    f"api:group:{group_id}:membership" if idempotency_key else None
                ),
                idempotency_key=idempotency_key,
            )
        except GroupError as error:
            raise _http_error(error) from error
        return _group_payload(group)

    @router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_group(
        group_id: str,
        payload: DeleteGroupPayload = Body(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        try:
            idempotency_key = _idempotency_key(
                idempotency_key, payload.idempotency_key
            )
            service_provider().delete_group(
                group_id,
                expected_revision=payload.expected_revision,
                idempotency_scope=(
                    f"api:group:{group_id}:delete" if idempotency_key else None
                ),
                idempotency_key=idempotency_key,
            )
        except GroupError as error:
            raise _http_error(error) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


router = create_router()
