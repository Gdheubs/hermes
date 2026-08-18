"""SQLite persistence for durable bot groups."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import (
    ConflictError,
    Group,
    GroupMember,
    GroupMessage,
    SchemaUnsupportedError,
    ValidationError,
)

SCHEMA_VERSION = 1

V1_TABLE_SHAPES = {
    "bot_instances": (
        ("id", "TEXT", 0, 1),
        ("profile_name", "TEXT", 1, 0),
        ("created_at_ms", "INTEGER", 1, 0),
        ("updated_at_ms", "INTEGER", 1, 0),
    ),
    "bot_groups": (
        ("id", "TEXT", 0, 1),
        ("name", "TEXT", 1, 0),
        ("color", "TEXT", 1, 0),
        ("icon_kind", "TEXT", 1, 0),
        ("icon_value", "TEXT", 1, 0),
        ("leader_bot_instance_id", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("created_at_ms", "INTEGER", 1, 0),
        ("updated_at_ms", "INTEGER", 1, 0),
        ("deleted_at_ms", "INTEGER", 0, 0),
    ),
    "bot_group_members": (
        ("id", "TEXT", 0, 1),
        ("group_id", "TEXT", 1, 0),
        ("bot_instance_id", "TEXT", 1, 0),
        ("membership_epoch", "INTEGER", 1, 0),
        ("display_order", "INTEGER", 1, 0),
        ("added_at_ms", "INTEGER", 1, 0),
        ("removed_at_ms", "INTEGER", 0, 0),
    ),
    "bot_group_conversations": (
        ("id", "TEXT", 0, 1),
        ("group_id", "TEXT", 1, 0),
        ("created_at_ms", "INTEGER", 1, 0),
    ),
    "bot_group_messages": (
        ("id", "TEXT", 0, 1),
        ("conversation_id", "TEXT", 1, 0),
        ("sender_bot_instance_id", "TEXT", 0, 0),
        ("content", "TEXT", 1, 0),
        ("created_at_ms", "INTEGER", 1, 0),
    ),
    "bot_group_audit_events": (
        ("id", "TEXT", 0, 1),
        ("group_id", "TEXT", 1, 0),
        ("event_type", "TEXT", 1, 0),
        ("payload_json", "TEXT", 1, 0),
        ("created_at_ms", "INTEGER", 1, 0),
    ),
    "idempotency_keys": (
        ("scope", "TEXT", 1, 1),
        ("key", "TEXT", 1, 2),
        ("request_hash", "TEXT", 1, 0),
        ("response_json", "TEXT", 1, 0),
        ("created_at_ms", "INTEGER", 1, 0),
    ),
}

V1_FOREIGN_KEYS = {
    "bot_groups": {("leader_bot_instance_id", "bot_instances", "id")},
    "bot_group_members": {
        ("group_id", "bot_groups", "id"),
        ("bot_instance_id", "bot_instances", "id"),
    },
    "bot_group_conversations": {("group_id", "bot_groups", "id")},
    "bot_group_messages": {
        ("conversation_id", "bot_group_conversations", "id"),
        ("sender_bot_instance_id", "bot_instances", "id"),
    },
    "bot_group_audit_events": {("group_id", "bot_groups", "id")},
}

PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def utc_ms() -> int:
    """Return the current UTC timestamp in milliseconds."""
    return time.time_ns() // 1_000_000


class GroupStore:
    """Persist bot-group state and idempotent mutation receipts."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            # Some SQLite hosts do not support WAL. The transaction still works.
            pass
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            transaction_started = False
            try:
                connection.execute("BEGIN IMMEDIATE")
                transaction_started = True
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, applied_at_ms INTEGER NOT NULL)"
                )
                versions = [
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                if any(
                    not isinstance(version, int)
                    or version < 0
                    or version > SCHEMA_VERSION
                    for version in versions
                ):
                    raise SchemaUnsupportedError("database schema is unsupported")

                if not versions or versions == [0]:
                    self._create_v1(connection)
                    connection.execute("DELETE FROM schema_migrations WHERE version=0")
                    connection.execute(
                        "INSERT OR REPLACE INTO schema_migrations VALUES (?, ?)",
                        (SCHEMA_VERSION, utc_ms()),
                    )
                elif versions == [SCHEMA_VERSION]:
                    self._validate_v1(connection)
                else:
                    raise SchemaUnsupportedError("database schema is unsupported")

                connection.execute("COMMIT")
                transaction_started = False
            except Exception:
                if transaction_started:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _create_v1(connection: sqlite3.Connection) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS bot_instances ("
            "id TEXT PRIMARY KEY, profile_name TEXT NOT NULL, "
            "created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS bot_groups ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL, "
            "icon_kind TEXT NOT NULL, icon_value TEXT NOT NULL, "
            "leader_bot_instance_id TEXT NOT NULL REFERENCES bot_instances(id), "
            "revision INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, "
            "updated_at_ms INTEGER NOT NULL, deleted_at_ms INTEGER)",
            "CREATE TABLE IF NOT EXISTS bot_group_members ("
            "id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES bot_groups(id), "
            "bot_instance_id TEXT NOT NULL REFERENCES bot_instances(id), "
            "membership_epoch INTEGER NOT NULL, display_order INTEGER NOT NULL, "
            "added_at_ms INTEGER NOT NULL, removed_at_ms INTEGER)",
            "CREATE UNIQUE INDEX IF NOT EXISTS bot_group_members_active_unique "
            "ON bot_group_members(group_id, bot_instance_id) "
            "WHERE removed_at_ms IS NULL",
            "CREATE TABLE IF NOT EXISTS bot_group_conversations ("
            "id TEXT PRIMARY KEY, group_id TEXT NOT NULL UNIQUE REFERENCES bot_groups(id), "
            "created_at_ms INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS bot_group_messages ("
            "id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL "
            "REFERENCES bot_group_conversations(id), sender_bot_instance_id TEXT "
            "REFERENCES bot_instances(id), content TEXT NOT NULL, "
            "created_at_ms INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS bot_group_audit_events ("
            "id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES bot_groups(id), "
            "event_type TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "created_at_ms INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS idempotency_keys ("
            "scope TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, "
            "response_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL, "
            "PRIMARY KEY(scope, key))",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _validate_v1(connection: sqlite3.Connection) -> None:
        present_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not V1_TABLE_SHAPES.keys() <= present_tables:
            raise SchemaUnsupportedError("database schema is unsupported")

        for table, expected_shape in V1_TABLE_SHAPES.items():
            actual_shape = tuple(
                (row["name"], row["type"].upper(), row["notnull"], row["pk"])
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual_shape != expected_shape:
                raise SchemaUnsupportedError("database schema is unsupported")

            actual_foreign_keys = {
                (row["from"], row["table"], row["to"])
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            if actual_foreign_keys != V1_FOREIGN_KEYS.get(table, set()):
                raise SchemaUnsupportedError("database schema is unsupported")

        conversation_indexes = connection.execute(
            "PRAGMA index_list(bot_group_conversations)"
        ).fetchall()
        if not any(
            row["unique"]
            and tuple(
                column["name"]
                for column in connection.execute(f"PRAGMA index_info({row['name']})")
            )
            == ("group_id",)
            for row in conversation_indexes
        ):
            raise SchemaUnsupportedError("database schema is unsupported")

        active_index = next(
            (
                row
                for row in connection.execute("PRAGMA index_list(bot_group_members)")
                if row["name"] == "bot_group_members_active_unique"
            ),
            None,
        )
        if active_index is None or not active_index["unique"] or not active_index["partial"]:
            raise SchemaUnsupportedError("database schema is unsupported")
        active_index_columns = tuple(
            row["name"]
            for row in connection.execute(
                "PRAGMA index_info(bot_group_members_active_unique)"
            )
        )
        active_index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("bot_group_members_active_unique",),
        ).fetchone()["sql"]
        normalized_active_index_sql = " ".join(active_index_sql.upper().split()).rstrip(";")
        expected_active_index_sql = (
            "CREATE UNIQUE INDEX BOT_GROUP_MEMBERS_ACTIVE_UNIQUE "
            "ON BOT_GROUP_MEMBERS(GROUP_ID, BOT_INSTANCE_ID) "
            "WHERE REMOVED_AT_MS IS NULL"
        )
        if (
            active_index_columns != ("group_id", "bot_instance_id")
            or normalized_active_index_sql != expected_active_index_sql
        ):
            raise SchemaUnsupportedError("database schema is unsupported")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        transaction_started = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            yield connection
            connection.execute("COMMIT")
            transaction_started = False
        except Exception:
            if transaction_started:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_idempotency(
        scope: object, key: object, request_hash: object
    ) -> None:
        values = (scope, key, request_hash)
        if values == (None, None, None):
            return
        if all(isinstance(value, str) and value.strip() for value in values):
            return
        raise ValidationError(
            "idempotency scope, key, and request hash must be supplied together "
            "as nonblank strings"
        )

    def _group(self, connection: sqlite3.Connection, group_id: str) -> Group | None:
        row = connection.execute(
            "SELECT * FROM bot_groups WHERE id=? AND deleted_at_ms IS NULL", (group_id,)
        ).fetchone()
        if row is None:
            return None

        members = connection.execute(
            "SELECT i.id, i.profile_name FROM bot_group_members m "
            "JOIN bot_instances i ON i.id=m.bot_instance_id "
            "WHERE m.group_id=? AND m.removed_at_ms IS NULL "
            "ORDER BY m.display_order",
            (group_id,),
        ).fetchall()
        return Group(
            row["id"],
            row["name"],
            row["color"],
            row["icon_kind"],
            row["icon_value"],
            row["leader_bot_instance_id"],
            row["revision"],
            row["created_at_ms"],
            row["updated_at_ms"],
            tuple(GroupMember(member["id"], member["profile_name"]) for member in members),
        )

    @staticmethod
    def _pack(group: Group) -> dict[str, Any]:
        return {
            "group": {
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
                    [member.bot_instance_id, member.profile_name]
                    for member in group.members
                ],
            }
        }

    @staticmethod
    def _unpack(payload: dict[str, Any]) -> Group:
        group = payload["group"]
        return Group(
            group["id"],
            group["name"],
            group["color"],
            group["icon_kind"],
            group["icon_value"],
            group["leader_bot_instance_id"],
            group["revision"],
            group["created_at_ms"],
            group["updated_at_ms"],
            tuple(GroupMember(*member) for member in group["members"]),
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> GroupMessage:
        return GroupMessage(
            row["id"],
            row["conversation_id"],
            row["sender_bot_instance_id"],
            row["sender_profile_name"],
            row["content"],
            row["created_at_ms"],
        )

    @staticmethod
    def _pack_message(message: GroupMessage) -> dict[str, Any]:
        return {
            "message": {
                "id": message.id,
                "conversation_id": message.conversation_id,
                "sender_bot_instance_id": message.sender_bot_instance_id,
                "sender_profile_name": message.sender_profile_name,
                "content": message.content,
                "created_at_ms": message.created_at_ms,
            }
        }

    @staticmethod
    def _unpack_message(payload: dict[str, Any]) -> GroupMessage:
        message = payload["message"]
        return GroupMessage(
            message["id"],
            message["conversation_id"],
            message["sender_bot_instance_id"],
            message["sender_profile_name"],
            message["content"],
            message["created_at_ms"],
        )

    @staticmethod
    def _replay(
        connection: sqlite3.Connection, scope: str | None, key: str | None, request_hash: str | None
    ) -> dict[str, Any] | None:
        if scope is None:
            return None
        row = connection.execute(
            "SELECT request_hash, response_json FROM idempotency_keys "
            "WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ConflictError("idempotency key was used for a different request")
        return json.loads(row["response_json"])

    @staticmethod
    def _receipt(
        connection: sqlite3.Connection,
        scope: str | None,
        key: str | None,
        request_hash: str | None,
        response: dict[str, Any],
    ) -> None:
        if scope is None:
            return
        connection.execute(
            "INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?)",
            (
                scope,
                key,
                request_hash,
                json.dumps(response, sort_keys=True, separators=(",", ":")),
                utc_ms(),
            ),
        )

    @staticmethod
    def _members(
        connection: sqlite3.Connection, members: Sequence[GroupMember], now: int
    ) -> None:
        for member in members:
            connection.execute(
                "INSERT INTO bot_instances VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET profile_name=excluded.profile_name, "
                "updated_at_ms=excluded.updated_at_ms",
                (member.bot_instance_id, member.profile_name, now, now),
            )

    @staticmethod
    def _assert_leader_is_member(
        members: Sequence[GroupMember], leader_bot_instance_id: str
    ) -> None:
        if leader_bot_instance_id not in {
            member.bot_instance_id for member in members
        }:
            raise ValidationError("leader must be an active member")

    def create_group(
        self,
        *,
        group_id: str,
        name: str,
        color: str,
        icon_kind: str,
        icon_value: str,
        members: Sequence[GroupMember],
        leader_bot_instance_id: str,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> Group:
        self._validate_idempotency(idempotency_scope, idempotency_key, request_hash)
        self._assert_leader_is_member(members, leader_bot_instance_id)

        with self.transaction() as connection:
            replay = self._replay(
                connection, idempotency_scope, idempotency_key, request_hash
            )
            if replay is not None:
                return self._unpack(replay)

            now = utc_ms()
            self._members(connection, members, now)
            connection.execute(
                "INSERT INTO bot_groups VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)",
                (
                    group_id,
                    name,
                    color,
                    icon_kind,
                    icon_value,
                    leader_bot_instance_id,
                    now,
                    now,
                ),
            )
            for display_order, member in enumerate(members):
                connection.execute(
                    "INSERT INTO bot_group_members VALUES (?, ?, ?, 1, ?, ?, NULL)",
                    (
                        str(uuid.uuid4()),
                        group_id,
                        member.bot_instance_id,
                        display_order,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO bot_group_conversations VALUES (?, ?, ?)",
                (str(uuid.uuid4()), group_id, now),
            )
            connection.execute(
                "INSERT INTO bot_group_audit_events VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), group_id, "group_created", "{}", now),
            )
            group = self._group(connection, group_id)
            assert group is not None
            self._receipt(
                connection,
                idempotency_scope,
                idempotency_key,
                request_hash,
                self._pack(group),
            )
            return group

    def update_metadata(
        self,
        *,
        group_id: str,
        expected_revision: int,
        name: str,
        color: str,
        icon_kind: str,
        icon_value: str,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> Group | None:
        self._validate_idempotency(idempotency_scope, idempotency_key, request_hash)
        return self._mutate_metadata(
            group_id,
            expected_revision,
            name,
            color,
            icon_kind,
            icon_value,
            idempotency_scope,
            idempotency_key,
            request_hash,
        )

    def _mutate_metadata(
        self,
        group_id: str,
        expected_revision: int,
        name: str,
        color: str,
        icon_kind: str,
        icon_value: str,
        idempotency_scope: str | None,
        idempotency_key: str | None,
        request_hash: str | None,
    ) -> Group | None:
        with self.transaction() as connection:
            replay = self._replay(
                connection, idempotency_scope, idempotency_key, request_hash
            )
            if replay is not None:
                return self._unpack(replay)

            now = utc_ms()
            result = connection.execute(
                "UPDATE bot_groups SET name=?, color=?, icon_kind=?, icon_value=?, "
                "revision=revision+1, updated_at_ms=? "
                "WHERE id=? AND deleted_at_ms IS NULL AND revision=?",
                (name, color, icon_kind, icon_value, now, group_id, expected_revision),
            )
            if result.rowcount != 1:
                return None

            connection.execute(
                "INSERT INTO bot_group_audit_events VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), group_id, "metadata_updated", "{}", now),
            )
            group = self._group(connection, group_id)
            assert group is not None
            self._receipt(
                connection,
                idempotency_scope,
                idempotency_key,
                request_hash,
                self._pack(group),
            )
            return group

    def replace_membership(
        self,
        *,
        group_id: str,
        expected_revision: int,
        members: Sequence[GroupMember],
        leader_bot_instance_id: str,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> Group | None:
        self._validate_idempotency(idempotency_scope, idempotency_key, request_hash)
        self._assert_leader_is_member(members, leader_bot_instance_id)

        with self.transaction() as connection:
            replay = self._replay(
                connection, idempotency_scope, idempotency_key, request_hash
            )
            if replay is not None:
                return self._unpack(replay)

            # This guard must precede every related-row write. Returning None from
            # this transaction commits, so a stale request cannot have side effects.
            current = connection.execute(
                "SELECT revision FROM bot_groups "
                "WHERE id=? AND deleted_at_ms IS NULL",
                (group_id,),
            ).fetchone()
            if current is None or current["revision"] != expected_revision:
                return None

            now = utc_ms()
            self._members(connection, members, now)
            result = connection.execute(
                "UPDATE bot_groups SET leader_bot_instance_id=?, revision=revision+1, "
                "updated_at_ms=? WHERE id=? AND deleted_at_ms IS NULL AND revision=?",
                (leader_bot_instance_id, now, group_id, expected_revision),
            )
            if result.rowcount != 1:
                raise RuntimeError("membership revision changed during immediate transaction")

            epoch = expected_revision + 1
            connection.execute(
                "UPDATE bot_group_members SET removed_at_ms=? "
                "WHERE group_id=? AND removed_at_ms IS NULL",
                (now, group_id),
            )
            for display_order, member in enumerate(members):
                connection.execute(
                    "INSERT INTO bot_group_members VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (
                        str(uuid.uuid4()),
                        group_id,
                        member.bot_instance_id,
                        epoch,
                        display_order,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO bot_group_audit_events VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), group_id, "membership_replaced", "{}", now),
            )
            group = self._group(connection, group_id)
            assert group is not None
            self._receipt(
                connection,
                idempotency_scope,
                idempotency_key,
                request_hash,
                self._pack(group),
            )
            return group

    def delete_group(
        self,
        *,
        group_id: str,
        expected_revision: int,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> bool:
        self._validate_idempotency(idempotency_scope, idempotency_key, request_hash)

        with self.transaction() as connection:
            replay = self._replay(
                connection, idempotency_scope, idempotency_key, request_hash
            )
            if replay is not None:
                return bool(replay["deleted"])

            now = utc_ms()
            result = connection.execute(
                "UPDATE bot_groups SET deleted_at_ms=?, revision=revision+1, "
                "updated_at_ms=? WHERE id=? AND deleted_at_ms IS NULL AND revision=?",
                (now, now, group_id, expected_revision),
            )
            if result.rowcount != 1:
                return False

            connection.execute(
                "INSERT INTO bot_group_audit_events VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), group_id, "group_deleted", "{}", now),
            )
            self._receipt(
                connection,
                idempotency_scope,
                idempotency_key,
                request_hash,
                {"deleted": True},
            )
            return True

    def load_idempotency(
        self, *, scope: str, key: str
    ) -> tuple[str, dict[str, Any]] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_hash, response_json FROM idempotency_keys "
                "WHERE scope=? AND key=?",
                (scope, key),
            ).fetchone()
        if row is None:
            return None
        return row["request_hash"], json.loads(row["response_json"])

    def save_idempotency(self, **_: object) -> None:
        raise RuntimeError("use atomic mutator receipt")

    def group_state(self, group_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT deleted_at_ms FROM bot_groups WHERE id=?", (group_id,)
            ).fetchone()
        if row is None:
            return "missing"
        return "deleted" if row["deleted_at_ms"] is not None else "active"

    def get_group(self, group_id: str) -> Group | None:
        with self._connect() as connection:
            return self._group(connection, group_id)

    def list_groups(self) -> list[Group]:
        with self._connect() as connection:
            group_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM bot_groups WHERE deleted_at_ms IS NULL "
                    "ORDER BY created_at_ms, id"
                )
            ]
            return [
                group
                for group_id in group_ids
                if (group := self._group(connection, group_id)) is not None
            ]

    def list_messages(self, group_id: str) -> list[GroupMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT m.id, m.conversation_id, m.sender_bot_instance_id, "
                "i.profile_name AS sender_profile_name, m.content, m.created_at_ms "
                "FROM bot_group_messages m "
                "JOIN bot_group_conversations c ON c.id=m.conversation_id "
                "LEFT JOIN bot_instances i ON i.id=m.sender_bot_instance_id "
                "WHERE c.group_id=? ORDER BY m.created_at_ms, m.id",
                (group_id,),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def append_message(
        self,
        *,
        group_id: str,
        sender_bot_instance_id: str | None,
        content: str,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> GroupMessage | None:
        self._validate_idempotency(idempotency_scope, idempotency_key, request_hash)
        with self.transaction() as connection:
            replay = self._replay(
                connection, idempotency_scope, idempotency_key, request_hash
            )
            if replay is not None:
                return self._unpack_message(replay)

            conversation = connection.execute(
                "SELECT c.id FROM bot_group_conversations c "
                "JOIN bot_groups g ON g.id=c.group_id "
                "WHERE c.group_id=? AND g.deleted_at_ms IS NULL",
                (group_id,),
            ).fetchone()
            if conversation is None:
                return None

            sender_profile_name: str | None = None
            if sender_bot_instance_id is not None:
                sender = connection.execute(
                    "SELECT i.profile_name FROM bot_group_members m "
                    "JOIN bot_instances i ON i.id=m.bot_instance_id "
                    "WHERE m.group_id=? AND m.bot_instance_id=? "
                    "AND m.removed_at_ms IS NULL",
                    (group_id, sender_bot_instance_id),
                ).fetchone()
                if sender is None:
                    raise ValidationError("message sender must be an active group member")
                sender_profile_name = sender["profile_name"]

            message = GroupMessage(
                str(uuid.uuid4()),
                conversation["id"],
                sender_bot_instance_id,
                sender_profile_name,
                content,
                utc_ms(),
            )
            connection.execute(
                "INSERT INTO bot_group_messages VALUES (?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.conversation_id,
                    message.sender_bot_instance_id,
                    message.content,
                    message.created_at_ms,
                ),
            )
            self._receipt(
                connection,
                idempotency_scope,
                idempotency_key,
                request_hash,
                self._pack_message(message),
            )
            return message

    def reconcile_bot_instances(
        self, bots: Sequence[tuple[str, str | None]]
    ) -> list[dict[str, str | bool]]:
        """Return stable instance IDs while separating copied profile metadata."""
        profile_names = [profile_name for profile_name, _ in bots]
        if len(profile_names) != len(set(profile_names)):
            raise ValidationError("bot profile names must be distinct")
        if any(not PROFILE_NAME_RE.fullmatch(name) for name in profile_names):
            raise ValidationError("bot profile name is invalid")

        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT id, profile_name FROM bot_instances"
            ).fetchall()
            by_id = {row["id"]: row["profile_name"] for row in rows}
            by_profile = {row["profile_name"]: row["id"] for row in rows}
            incoming_names = set(profile_names)
            assignments: dict[str, str] = {}
            claimed_ids: set[str] = set()

            # Recorded profile ownership wins over copied UI metadata.
            for profile_name, _ in bots:
                known_id = by_profile.get(profile_name)
                if known_id is not None:
                    assignments[profile_name] = known_id
                    claimed_ids.add(known_id)

            # A supplied ID may establish a bot or rename its absent owner. If
            # the recorded owner is also present, this row is a clone.
            for profile_name, supplied_id in bots:
                if profile_name in assignments or not self._is_uuid(supplied_id):
                    continue
                assert isinstance(supplied_id, str)
                recorded_owner = by_id.get(supplied_id)
                if supplied_id in claimed_ids:
                    continue
                if recorded_owner is None or recorded_owner not in incoming_names:
                    assignments[profile_name] = supplied_id
                    claimed_ids.add(supplied_id)

            for profile_name, _ in bots:
                if profile_name in assignments:
                    continue
                instance_id = str(uuid.uuid4())
                while instance_id in by_id or instance_id in claimed_ids:
                    instance_id = str(uuid.uuid4())
                assignments[profile_name] = instance_id
                claimed_ids.add(instance_id)

            now = utc_ms()
            for profile_name, instance_id in assignments.items():
                connection.execute(
                    "INSERT INTO bot_instances VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "profile_name=excluded.profile_name, updated_at_ms=excluded.updated_at_ms",
                    (instance_id, profile_name, now, now),
                )

            supplied_by_profile = dict(bots)
            return [
                {
                    "profile_name": profile_name,
                    "instance_id": assignments[profile_name],
                    "changed": supplied_by_profile[profile_name]
                    != assignments[profile_name],
                }
                for profile_name in profile_names
            ]

    @staticmethod
    def _is_uuid(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return str(uuid.UUID(value)) == value.lower()
        except ValueError:
            return False

    def count_rows(self, table: str) -> int:
        allowed_tables = {
            "bot_instances",
            "bot_groups",
            "bot_group_members",
            "bot_group_conversations",
            "bot_group_messages",
            "bot_group_audit_events",
            "idempotency_keys",
            "schema_migrations",
        }
        if table not in allowed_tables:
            raise ValueError("unknown table")
        with self._connect() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
