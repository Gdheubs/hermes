"""Durable, profile-scoped Hermes execution read contract.

The execution ledger is the authoritative local record for externally
observable execution lifecycle, decisions, ordered events, and terminal
effect receipts.  It deliberately does not infer effect success from chat
text, session history, process-local run state, or Kanban summaries.

All public reads use SQLite ``mode=ro`` and ``PRAGMA query_only``.  Schema
creation, migration, recovery, and publication are explicit write operations.
Terminal state, its ordered event, and any evidence-backed receipt are written
in one ``BEGIN IMMEDIATE`` transaction; the events table is therefore the
transactional publication outbox for the read API.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, cast

from hermes_cli.sqlite_safe_read import connect_tracked
from hermes_cli.sqlite_util import write_txn
from hermes_constants import get_hermes_home

CONTRACT_VERSION = "hermes.execution.read.v1"
API_VERSION = "v1"
STORE_SCHEMA_VERSION = 1
EVENT_RETENTION_SECONDS = 30 * 24 * 60 * 60
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
PROFILE_ANCHOR_VERSION = 1
PROFILE_ANCHOR_FILENAME = ".execution-contract-profile-instance.json"
_PROFILE_ANCHOR_LOCK_FILENAME = ".execution-contract-profile-instance.lock"
_PROFILE_ANCHOR_MAX_BYTES = 4096
_PROFILE_ANCHOR_WAIT_ATTEMPTS = 200
_PROFILE_ANCHOR_WAIT_SECONDS = 0.01

EXECUTION_STATES = frozenset(
    {
        "accepted",
        "queued",
        "running",
        "awaiting_decision",
        "cancellation_requested",
        "terminal_succeeded",
        "terminal_failed",
        "terminal_cancelled",
        "terminal_partial",
        "terminal_ambiguous",
    }
)
TERMINAL_EXECUTION_STATES = frozenset(
    {
        "terminal_succeeded",
        "terminal_failed",
        "terminal_cancelled",
        "terminal_partial",
        "terminal_ambiguous",
    }
)
DECISION_STATES = frozenset({"pending", "resolved", "expired", "superseded"})
RECEIPT_OUTCOMES = frozenset(
    {"succeeded", "failed", "cancelled", "partial", "ambiguous"}
)
EVENT_TYPES = frozenset(
    {
        "execution.created",
        "execution.transitioned",
        "execution.recovered",
        "decision.requested",
        "decision.resolved",
        "decision.expired",
        "decision.superseded",
        "effect.evidence_recorded",
        "receipt.published",
    }
)
RECEIPT_STATES = frozenset(
    {"not_applicable", "pending_evidence", "unproven", "published"}
)

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "accepted": frozenset(
        EXECUTION_STATES - {"accepted"}
    ),
    "queued": frozenset(
        EXECUTION_STATES - {"accepted", "queued"}
    ),
    "running": frozenset(
        {
            "awaiting_decision",
            "cancellation_requested",
            *TERMINAL_EXECUTION_STATES,
        }
    ),
    "awaiting_decision": frozenset(
        {"running", "cancellation_requested", *TERMINAL_EXECUTION_STATES}
    ),
    "cancellation_requested": frozenset(
        {
            "terminal_cancelled",
            "terminal_failed",
            "terminal_partial",
            "terminal_ambiguous",
        }
    ),
    "terminal_ambiguous": frozenset(
        {
            "terminal_succeeded",
            "terminal_failed",
            "terminal_cancelled",
            "terminal_partial",
        }
    ),
    "terminal_succeeded": frozenset(),
    "terminal_failed": frozenset(),
    "terminal_cancelled": frozenset(),
    "terminal_partial": frozenset(),
}

_ID_RE = re.compile(r"^(exe|dec|rcp|evt)_([0-9a-f]{12})_([0-9a-f]{32})$")
_EVIDENCE_ID_RE = re.compile(r"^evd_([0-9a-f]{12})_([0-9a-f]{32})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_INSTANCE_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ExecutionContractError(RuntimeError):
    """Base class for closed-contract failures."""


class ContractValidationError(ExecutionContractError):
    """Caller input does not satisfy the public contract."""


class ContractNotFoundError(ExecutionContractError):
    """A correctly scoped contract object does not exist."""


class ContractForbiddenError(ExecutionContractError):
    """An identifier belongs to a different profile authority."""


class ContractUnauthorizedError(ExecutionContractError):
    """The request did not present valid credentials for this contract."""


class ContractMethodNotAllowedError(ExecutionContractError):
    """The contract namespace does not support the requested HTTP method."""


class ContractConflictError(ExecutionContractError):
    """A revision, lifecycle, or immutable binding conflicts."""


class ContractCursorGoneError(ExecutionContractError):
    """The requested event cursor predates retained history."""

    def __init__(self, minimum_available: int, high_water: int) -> None:
        self.minimum_available = minimum_available
        self.high_water = high_water
        super().__init__(
            f"event cursor was pruned; minimum_available={minimum_available}"
        )


class ContractRateLimitedError(ExecutionContractError):
    """The durable ledger is temporarily busy."""


class ContractUnavailableError(ExecutionContractError):
    """The durable ledger cannot currently provide trustworthy data."""


class UnsupportedContractVersionError(ExecutionContractError):
    """The caller or store requested an unsupported contract version."""


class ContractDataError(ExecutionContractError):
    """Persisted data violates a closed contract invariant."""


@dataclass(frozen=True)
class AuthorityIdentity:
    profile_id: str
    profile_name: str
    authority_id: str
    executor_id: str
    profile_key: str

    def public(self) -> dict[str, str]:
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "authority_id": self.authority_id,
            "executor_id": self.executor_id,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Optional[datetime] = None) -> str:
    current = value or _utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ContractValidationError("timestamps must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ContractDataError(f"{field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractDataError(f"{field} is malformed") from exc
    return parsed.astimezone(timezone.utc)


def _normalize_profile_name(profile_name: str) -> str:
    clean = str(profile_name or "default").strip()
    if not clean or len(clean) > 128 or _CONTROL_RE.search(clean):
        raise ContractValidationError("profile_name is invalid")
    return clean


def _profile_anchor_path(profile_home: Path) -> Path:
    return Path(profile_home) / PROFILE_ANCHOR_FILENAME


def _profile_anchor_exists(profile_home: Path) -> bool:
    return os.path.lexists(_profile_anchor_path(profile_home))


def _read_profile_anchor(profile_home: Path) -> str:
    """Read and validate the durable profile-instance anchor without mutation."""

    path = _profile_anchor_path(profile_home)
    descriptor: Optional[int] = None
    try:
        path_metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ContractDataError("profile instance anchor is missing") from exc
    except OSError as exc:
        raise ContractDataError("profile instance anchor is unavailable") from exc
    if not stat.S_ISREG(path_metadata.st_mode) or stat.S_ISLNK(path_metadata.st_mode):
        raise ContractDataError("profile instance anchor is not a regular file")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ContractDataError("profile instance anchor is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (path_metadata.st_dev, path_metadata.st_ino)
        != (metadata.st_dev, metadata.st_ino)
    ):
        os.close(descriptor)
        raise ContractDataError("profile instance anchor changed while opening")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        os.close(descriptor)
        raise ContractDataError("profile instance anchor permissions are unsafe")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        os.close(descriptor)
        raise ContractDataError("profile instance anchor owner is unsafe")
    if metadata.st_size <= 0 or metadata.st_size > _PROFILE_ANCHOR_MAX_BYTES:
        os.close(descriptor)
        raise ContractDataError("profile instance anchor is corrupt")
    try:
        raw = os.read(descriptor, _PROFILE_ANCHOR_MAX_BYTES + 1)
        os.close(descriptor)
        descriptor = None
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ContractDataError("profile instance anchor is corrupt") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "instance_id"}:
        raise ContractDataError("profile instance anchor is corrupt")
    version = payload.get("version")
    instance_id = payload.get("instance_id")
    if (
        isinstance(version, bool)
        or version != PROFILE_ANCHOR_VERSION
        or not isinstance(instance_id, str)
        or _PROFILE_INSTANCE_RE.fullmatch(instance_id) is None
    ):
        raise ContractDataError("profile instance anchor is corrupt")
    return instance_id


def _fsync_profile_home(profile_home: Path) -> None:
    """Durably publish profile metadata on platforms supporting directory fsync."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(profile_home, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("profile instance anchor write made no progress")
        offset += written


def _restrict_open_file(descriptor: int, path: Path) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    else:
        os.chmod(path, 0o600)


def _create_profile_anchor(profile_home: Path) -> str:
    """Create the anchor once through an owner-only lock and atomic rename."""

    anchor_path = _profile_anchor_path(profile_home)
    if _profile_anchor_exists(profile_home):
        return _read_profile_anchor(profile_home)
    lock_path = Path(profile_home) / _PROFILE_ANCHOR_LOCK_FILENAME
    open_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    lock_descriptor: Optional[int] = None
    try:
        lock_descriptor = os.open(lock_path, open_flags, 0o600)
        _restrict_open_file(lock_descriptor, lock_path)
        os.fsync(lock_descriptor)
    except FileExistsError:
        for _attempt in range(_PROFILE_ANCHOR_WAIT_ATTEMPTS):
            if _profile_anchor_exists(profile_home):
                return _read_profile_anchor(profile_home)
            if not os.path.lexists(lock_path):
                return _create_profile_anchor(profile_home)
            time.sleep(_PROFILE_ANCHOR_WAIT_SECONDS)
        raise ContractUnavailableError("profile instance anchor initialization is busy")
    except OSError as exc:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        with suppress(OSError):
            lock_path.unlink()
        raise ContractUnavailableError(
            "profile instance anchor cannot be initialized"
        ) from exc

    temp_path = Path(profile_home) / (
        f".{PROFILE_ANCHOR_FILENAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    temp_descriptor: Optional[int] = None
    try:
        if _profile_anchor_exists(profile_home):
            return _read_profile_anchor(profile_home)
        instance_id = secrets.token_hex(32)
        encoded = (
            json.dumps(
                {"version": PROFILE_ANCHOR_VERSION, "instance_id": instance_id},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        temp_descriptor = os.open(temp_path, open_flags, 0o600)
        _restrict_open_file(temp_descriptor, temp_path)
        _write_all(temp_descriptor, encoded)
        os.fsync(temp_descriptor)
        os.close(temp_descriptor)
        temp_descriptor = None
        if _profile_anchor_exists(profile_home):
            return _read_profile_anchor(profile_home)
        os.replace(temp_path, anchor_path)
        _fsync_profile_home(profile_home)
        return _read_profile_anchor(profile_home)
    except ExecutionContractError:
        raise
    except OSError as exc:
        raise ContractUnavailableError(
            "profile instance anchor cannot be initialized"
        ) from exc
    finally:
        if temp_descriptor is not None:
            os.close(temp_descriptor)
        with suppress(OSError):
            temp_path.unlink()
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        with suppress(OSError):
            lock_path.unlink()
        with suppress(OSError):
            _fsync_profile_home(profile_home)


def authority_identity(
    profile_home: Path,
    profile_name: Optional[str] = None,
) -> AuthorityIdentity:
    """Derive authority from the durable anchor, never routing or a home path."""

    canonical_home = Path(profile_home).expanduser().resolve(strict=False)
    anchor_id = _read_profile_anchor(canonical_home)
    fingerprint = hashlib.sha256(
        b"hermes-execution-profile-anchor-v1\x00" + bytes.fromhex(anchor_id)
    ).hexdigest()
    profile_key = fingerprint[:12]
    clean = _normalize_profile_name(profile_name or canonical_home.name or "profile")
    instance = fingerprint[:24]
    return AuthorityIdentity(
        profile_id=f"hermes-profile-instance:{instance}",
        profile_name=clean,
        authority_id=f"hermes-execution-authority:{instance}",
        executor_id=f"hermes-agent-executor:{instance}",
        profile_key=profile_key,
    )


def validate_contract_version(value: Optional[str]) -> None:
    if value in (None, "", CONTRACT_VERSION):
        return
    raise UnsupportedContractVersionError(
        f"unsupported execution contract version: {value}"
    )


def canonical_digest(value: Mapping[str, Any] | list[Any] | str) -> str:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    else:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_digest(value: Optional[str], *, field: str, required: bool) -> Optional[str]:
    if value in (None, "") and not required:
        return None
    clean = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(clean):
        raise ContractValidationError(f"{field} must be a lowercase SHA-256 digest")
    return clean


def _validated_ref(
    value: Optional[str],
    *,
    field: str,
    required: bool = False,
    max_length: int = 512,
) -> Optional[str]:
    if value in (None, "") and not required:
        return None
    clean = str(value or "").strip()
    if not clean or len(clean) > max_length or _CONTROL_RE.search(clean):
        raise ContractValidationError(f"{field} is invalid")
    return clean


def _validated_choices(choices: list[str] | tuple[str, ...]) -> list[str]:
    if not isinstance(choices, (list, tuple)) or not choices:
        raise ContractValidationError("allowed_choices must be a non-empty array")
    if len(choices) > 32:
        raise ContractValidationError("allowed_choices cannot exceed 32 items")
    out: list[str] = []
    for raw in choices:
        choice = _validated_ref(raw, field="allowed_choice", required=True, max_length=64)
        assert choice is not None
        if choice in out:
            raise ContractValidationError("allowed_choices must be unique")
        out.append(choice)
    return out


def _validated_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("limit must be an integer") from exc
    if value < 1 or value > MAX_PAGE_SIZE:
        raise ContractValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return value


def _validated_cursor(after: int) -> int:
    try:
        value = int(after)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("after must be an integer") from exc
    if value < 0:
        raise ContractValidationError("after must be non-negative")
    return value


def _validated_snapshot(value: Optional[int]) -> Optional[int]:
    if value in (None, ""):
        return None
    return _validated_cursor(cast(int, value))


class ExecutionContractStore:
    """Profile-local durable authority store and read projection."""

    def __init__(
        self,
        *,
        database_path: Optional[Path] = None,
        profile_home: Optional[Path] = None,
        profile_name: Optional[str] = None,
        runtime_instance_id: Optional[str] = None,
    ) -> None:
        active_home = Path(
            profile_home
            or (database_path.parent if database_path is not None else get_hermes_home())
        ).expanduser().resolve(strict=False)
        self.profile_home = active_home
        self.database_path = database_path or (active_home / "execution_contract.sqlite3")
        self.profile_name = _normalize_profile_name(
            profile_name or active_home.name or "profile"
        )
        self._authority: Optional[AuthorityIdentity] = None
        self.runtime_instance_id = runtime_instance_id or uuid.uuid4().hex

    @property
    def authority(self) -> AuthorityIdentity:
        if self._authority is None:
            self._authority = authority_identity(
                self.profile_home,
                self.profile_name,
            )
        return self._authority

    @property
    def profile_anchor_path(self) -> Path:
        return _profile_anchor_path(self.profile_home)

    # ------------------------------------------------------------------
    # Connection and schema lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        self.profile_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.profile_home.chmod(0o700)
        except OSError:
            pass
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.database_path.parent.chmod(0o700)
        except OSError:
            pass
        ledger_exists = self.database_path.exists()
        if ledger_exists and not _profile_anchor_exists(self.profile_home):
            raise ContractDataError(
                "existing execution ledger is missing its profile instance anchor"
            )
        if not _profile_anchor_exists(self.profile_home):
            _create_profile_anchor(self.profile_home)
        self._authority = authority_identity(self.profile_home, self.profile_name)
        connection = self._connect_write()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                self._create_schema_v1(connection)
            elif version != STORE_SCHEMA_VERSION:
                raise UnsupportedContractVersionError(
                    f"unsupported execution store schema version: {version}"
                )
            self._validate_schema_metadata(connection)
        finally:
            connection.close()
        self._tighten_permissions()

    def _connect_write(self) -> sqlite3.Connection:
        try:
            connection = connect_tracked(self.database_path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            from hermes_state import apply_wal_with_fallback

            apply_wal_with_fallback(
                connection,
                db_label="execution_contract.sqlite3",
            )
            return connection
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise ContractRateLimitedError("execution ledger is busy") from exc
            raise ContractUnavailableError("execution ledger is unavailable") from exc
        except ExecutionContractError:
            raise
        except Exception as exc:
            raise ContractUnavailableError("execution ledger is unavailable") from exc

    @contextmanager
    def _read_connection(self) -> Iterator[Optional[sqlite3.Connection]]:
        if not self.database_path.exists():
            yield None
            return
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = connect_tracked(
                uri,
                tracking_path=self.database_path,
                uri=True,
                timeout=1.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("BEGIN")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != STORE_SCHEMA_VERSION:
                raise UnsupportedContractVersionError(
                    f"unsupported execution store schema version: {version}"
                )
            self._validate_schema_metadata(connection)
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.close()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise ContractRateLimitedError("execution ledger is busy") from exc
            raise ContractUnavailableError("execution ledger is unavailable") from exc
        except ExecutionContractError:
            if connection is not None:
                connection.close()
            raise
        except Exception as exc:
            if connection is not None:
                connection.close()
            raise ContractUnavailableError("execution ledger is unavailable") from exc
        try:
            yield connection
        finally:
            with suppress(sqlite3.Error):
                connection.rollback()
            connection.close()

    def _tighten_permissions(self) -> None:
        for candidate in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            try:
                if candidate.exists():
                    candidate.chmod(0o600)
            except OSError:
                pass

    def _create_schema_v1(self, connection: sqlite3.Connection) -> None:
        quoted_profile_id = connection.execute(
            "SELECT quote(?)",
            (self.authority.profile_id,),
        ).fetchone()[0]
        quoted_authority_id = connection.execute(
            "SELECT quote(?)",
            (self.authority.authority_id,),
        ).fetchone()[0]
        quoted_executor_id = connection.execute(
            "SELECT quote(?)",
            (self.authority.executor_id,),
        ).fetchone()[0]
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS execution_contract_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS executions (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL UNIQUE,
                profile_id TEXT NOT NULL,
                authority_id TEXT NOT NULL,
                executor_id TEXT NOT NULL,
                source_run_id TEXT UNIQUE,
                work_ref TEXT,
                proposal_ref TEXT,
                effect_id TEXT,
                lifecycle TEXT NOT NULL,
                receipt_state TEXT NOT NULL,
                receipt_id TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                terminal_at TEXT,
                recovery_ref TEXT,
                revision INTEGER NOT NULL,
                runtime_instance_id TEXT NOT NULL,
                CHECK (lifecycle IN (
                    'accepted', 'queued', 'running', 'awaiting_decision',
                    'cancellation_requested', 'terminal_succeeded',
                    'terminal_failed', 'terminal_cancelled', 'terminal_partial',
                    'terminal_ambiguous'
                )),
                CHECK (receipt_state IN (
                    'not_applicable', 'pending_evidence', 'unproven', 'published'
                )),
                CHECK (revision >= 1)
            );

            CREATE TABLE IF NOT EXISTS decisions (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL UNIQUE,
                execution_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                authority_id TEXT NOT NULL,
                effect_id TEXT NOT NULL,
                proposal_ref TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                candidate_digest TEXT,
                policy_digest TEXT,
                allowed_choices_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL,
                choice TEXT,
                resolution_evidence_digest TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                revision INTEGER NOT NULL,
                FOREIGN KEY (execution_id) REFERENCES executions(execution_id),
                UNIQUE (execution_id, request_digest),
                CHECK (state IN ('pending', 'resolved', 'expired', 'superseded')),
                CHECK (revision >= 1)
            );

            CREATE TABLE IF NOT EXISTS effect_evidence (
                evidence_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL UNIQUE,
                profile_id TEXT NOT NULL,
                authority_id TEXT NOT NULL,
                executor_id TEXT NOT NULL,
                effect_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                subject_digest TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                decision_id TEXT,
                recovery_ref TEXT,
                reconciliation_ref TEXT,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY (execution_id) REFERENCES executions(execution_id),
                FOREIGN KEY (decision_id) REFERENCES decisions(decision_id),
                CHECK (outcome IN ('succeeded', 'failed', 'cancelled', 'partial', 'ambiguous'))
            );

            CREATE TABLE IF NOT EXISTS receipts (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id TEXT NOT NULL UNIQUE,
                execution_id TEXT NOT NULL UNIQUE,
                effect_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                authority_id TEXT NOT NULL,
                executor_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                subject_digest TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                terminal_at TEXT NOT NULL,
                decision_id TEXT,
                recovery_ref TEXT,
                reconciliation_ref TEXT,
                revision INTEGER NOT NULL,
                FOREIGN KEY (execution_id) REFERENCES executions(execution_id),
                FOREIGN KEY (decision_id) REFERENCES decisions(decision_id),
                CHECK (outcome IN ('succeeded', 'failed', 'cancelled', 'partial', 'ambiguous')),
                CHECK (revision = 1)
            );

            CREATE TABLE IF NOT EXISTS execution_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                execution_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                authority_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_lifecycle TEXT,
                to_lifecycle TEXT,
                decision_id TEXT,
                receipt_id TEXT,
                reason_code TEXT,
                occurred_at TEXT NOT NULL,
                revision INTEGER NOT NULL,
                FOREIGN KEY (execution_id) REFERENCES executions(execution_id),
                FOREIGN KEY (decision_id) REFERENCES decisions(decision_id),
                FOREIGN KEY (receipt_id) REFERENCES receipts(receipt_id),
                CHECK (event_type IN (
                    'execution.created', 'execution.transitioned',
                    'execution.recovered', 'decision.requested',
                    'decision.resolved', 'decision.expired',
                    'decision.superseded', 'effect.evidence_recorded',
                    'receipt.published'
                )),
                CHECK (revision >= 1)
            );

            CREATE INDEX IF NOT EXISTS execution_events_execution_sequence
                ON execution_events(execution_id, sequence);
            CREATE INDEX IF NOT EXISTS decisions_state_ordinal
                ON decisions(state, ordinal);
            CREATE INDEX IF NOT EXISTS executions_lifecycle_ordinal
                ON executions(lifecycle, ordinal);

            INSERT OR IGNORE INTO execution_contract_metadata(key, value)
                VALUES ('contract_version', 'hermes.execution.read.v1');
            INSERT OR IGNORE INTO execution_contract_metadata(key, value)
                VALUES ('events_pruned_through', '0');
            INSERT OR IGNORE INTO execution_contract_metadata(key, value)
                VALUES ('event_retention_seconds', '2592000');
            INSERT OR IGNORE INTO execution_contract_metadata(key, value)
                VALUES ('profile_id', {quoted_profile_id});
            INSERT OR IGNORE INTO execution_contract_metadata(key, value)
                VALUES ('authority_id', {quoted_authority_id});
            INSERT OR IGNORE INTO execution_contract_metadata(key, value)
                VALUES ('executor_id', {quoted_executor_id});

            PRAGMA user_version=1;
            COMMIT;
            """
        )

    def _validate_schema_metadata(self, connection: sqlite3.Connection) -> None:
        try:
            rows = connection.execute(
                "SELECT key, value FROM execution_contract_metadata WHERE key IN "
                "('contract_version', 'profile_id', 'authority_id', 'executor_id')"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ContractDataError("execution ledger schema is incomplete") from exc
        metadata = {str(row[0]): str(row[1]) for row in rows}
        actual = metadata.get("contract_version")
        if actual != CONTRACT_VERSION:
            raise UnsupportedContractVersionError(
                f"unsupported stored execution contract version: {actual}"
            )
        expected_authority = {
            "profile_id": self.authority.profile_id,
            "authority_id": self.authority.authority_id,
            "executor_id": self.authority.executor_id,
        }
        if any(metadata.get(key) != value for key, value in expected_authority.items()):
            raise ContractDataError(
                "execution ledger authority metadata does not match the active profile"
            )

    # ------------------------------------------------------------------
    # Durable mutation hooks (not public HTTP mutation routes)
    # ------------------------------------------------------------------

    def create_execution(
        self,
        *,
        lifecycle: str = "accepted",
        source_run_id: Optional[str] = None,
        work_ref: Optional[str] = None,
        proposal_ref: Optional[str] = None,
        effect_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        self.initialize()
        if lifecycle not in {"accepted", "queued", "running"}:
            raise ContractValidationError("initial lifecycle is invalid")
        source_run_id = _validated_ref(source_run_id, field="source_run_id")
        work_ref = _validated_ref(work_ref, field="work_ref")
        proposal_ref = _validated_ref(proposal_ref, field="proposal_ref")
        effect_id = _validated_ref(effect_id, field="effect_id")
        if effect_id and not (work_ref and proposal_ref):
            raise ContractValidationError(
                "effect_id requires exact work_ref and proposal_ref bindings"
            )
        instant = _timestamp(now)
        execution_id = self._new_id("exe")
        receipt_state = "pending_evidence" if effect_id else "not_applicable"
        connection = self._connect_write()
        try:
            with write_txn(connection):
                if source_run_id:
                    existing = connection.execute(
                        "SELECT * FROM executions WHERE source_run_id=?",
                        (source_run_id,),
                    ).fetchone()
                    if existing is not None:
                        expected = (work_ref, proposal_ref, effect_id)
                        actual = (
                            existing["work_ref"],
                            existing["proposal_ref"],
                            existing["effect_id"],
                        )
                        if expected != actual:
                            raise ContractConflictError(
                                "source_run_id already has different immutable bindings"
                            )
                        return self._execution_from_row(existing)
                connection.execute(
                    """
                    INSERT INTO executions(
                        execution_id, profile_id, authority_id, executor_id,
                        source_run_id, work_ref, proposal_ref, effect_id,
                        lifecycle, receipt_state, created_at, started_at,
                        updated_at, revision, runtime_instance_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        execution_id,
                        self.authority.profile_id,
                        self.authority.authority_id,
                        self.authority.executor_id,
                        source_run_id,
                        work_ref,
                        proposal_ref,
                        effect_id,
                        lifecycle,
                        receipt_state,
                        instant,
                        instant if lifecycle == "running" else None,
                        instant,
                        self.runtime_instance_id,
                    ),
                )
                self._append_event_in_txn(
                    connection,
                    execution_id=execution_id,
                    event_type="execution.created",
                    to_lifecycle=lifecycle,
                    occurred_at=instant,
                    revision=1,
                )
                row = self._execution_row(connection, execution_id)
                assert row is not None
                return self._execution_from_row(row)
        except sqlite3.IntegrityError as exc:
            raise ContractConflictError("execution identity conflicts") from exc
        finally:
            connection.close()

    def transition_execution(
        self,
        execution_id: str,
        lifecycle: str,
        *,
        expected_revision: Optional[int] = None,
        reason_code: Optional[str] = None,
        recovery_ref: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        self.initialize()
        self._assert_identifier_scope(execution_id, "exe")
        if lifecycle not in EXECUTION_STATES:
            raise ContractValidationError("unknown execution lifecycle")
        reason_code = _validated_ref(reason_code, field="reason_code", max_length=128)
        recovery_ref = _validated_ref(recovery_ref, field="recovery_ref")
        instant = _timestamp(now)
        connection = self._connect_write()
        try:
            with write_txn(connection):
                row = self._execution_row(connection, execution_id)
                if row is None:
                    raise ContractNotFoundError("execution not found")
                current = str(row["lifecycle"])
                revision = int(row["revision"])
                if expected_revision is not None and int(expected_revision) != revision:
                    raise ContractConflictError("execution revision conflict")
                if lifecycle == current:
                    return self._execution_from_row(row)
                if lifecycle not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
                    raise ContractConflictError(
                        f"invalid execution transition: {current} -> {lifecycle}"
                    )

                evidence = connection.execute(
                    "SELECT * FROM effect_evidence WHERE execution_id=?",
                    (execution_id,),
                ).fetchone()
                requested_terminal = lifecycle in TERMINAL_EXECUTION_STATES
                actual_lifecycle = lifecycle
                receipt_state = str(row["receipt_state"])
                receipt_id: Optional[str] = row["receipt_id"]
                event_reason = reason_code

                if requested_terminal and row["effect_id"]:
                    if evidence is None:
                        actual_lifecycle = "terminal_ambiguous"
                        receipt_state = "unproven"
                        event_reason = event_reason or "effect_evidence_missing"
                    else:
                        self._evidence_from_row(evidence, connection=connection)
                        actual_lifecycle = f"terminal_{evidence['outcome']}"
                        receipt_id = self._publish_receipt_in_txn(
                            connection,
                            row=row,
                            evidence=evidence,
                            terminal_at=instant,
                        )
                        receipt_state = "published"

                if current == "terminal_ambiguous" and actual_lifecycle != current:
                    if evidence is None or not recovery_ref:
                        raise ContractConflictError(
                            "terminal ambiguity requires evidence and recovery_ref to reconcile"
                        )

                pending_decisions = connection.execute(
                    "SELECT * FROM decisions WHERE execution_id=? AND state='pending' "
                    "ORDER BY ordinal ASC",
                    (execution_id,),
                ).fetchall()
                closes_decisions = (
                    actual_lifecycle in TERMINAL_EXECUTION_STATES
                    or actual_lifecycle == "cancellation_requested"
                )
                if closes_decisions:
                    for decision in pending_decisions:
                        connection.execute(
                            """
                            UPDATE decisions
                            SET state='superseded', updated_at=?, revision=revision+1
                            WHERE decision_id=? AND state='pending'
                            """,
                            (instant, decision["decision_id"]),
                        )

                new_revision = revision + 1
                started_at = row["started_at"]
                if started_at is None and actual_lifecycle == "running":
                    started_at = instant
                terminal_at = (
                    instant if actual_lifecycle in TERMINAL_EXECUTION_STATES else None
                )
                connection.execute(
                    """
                    UPDATE executions
                    SET lifecycle=?, receipt_state=?, receipt_id=?, started_at=?,
                        updated_at=?, terminal_at=?, recovery_ref=?, revision=?,
                        runtime_instance_id=?
                    WHERE execution_id=? AND revision=?
                    """,
                    (
                        actual_lifecycle,
                        receipt_state,
                        receipt_id,
                        started_at,
                        instant,
                        terminal_at,
                        recovery_ref or row["recovery_ref"],
                        new_revision,
                        self.runtime_instance_id,
                        execution_id,
                        revision,
                    ),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ContractConflictError("execution revision conflict")
                self._append_event_in_txn(
                    connection,
                    execution_id=execution_id,
                    event_type=(
                        "execution.recovered" if recovery_ref else "execution.transitioned"
                    ),
                    from_lifecycle=current,
                    to_lifecycle=actual_lifecycle,
                    receipt_id=receipt_id,
                    reason_code=event_reason,
                    occurred_at=instant,
                    revision=new_revision,
                )
                for decision in pending_decisions:
                    if closes_decisions:
                        self._append_event_in_txn(
                            connection,
                            execution_id=execution_id,
                            event_type="decision.superseded",
                            from_lifecycle=actual_lifecycle,
                            to_lifecycle=actual_lifecycle,
                            decision_id=decision["decision_id"],
                            reason_code=(
                                "execution_terminal"
                                if actual_lifecycle in TERMINAL_EXECUTION_STATES
                                else "execution_cancellation_requested"
                            ),
                            occurred_at=instant,
                            revision=new_revision,
                        )
                if receipt_id:
                    self._append_event_in_txn(
                        connection,
                        execution_id=execution_id,
                        event_type="receipt.published",
                        to_lifecycle=actual_lifecycle,
                        receipt_id=receipt_id,
                        occurred_at=instant,
                        revision=new_revision,
                    )
                updated = self._execution_row(connection, execution_id)
                assert updated is not None
                return self._execution_from_row(updated)
        finally:
            connection.close()

    def recover_orphaned_executions(
        self,
        *,
        recovery_ref: str,
        now: Optional[datetime] = None,
    ) -> list[str]:
        """Mark prior-runtime nonterminal rows ambiguous after restart."""

        self.initialize()
        recovery_ref = cast(
            str,
            _validated_ref(
                recovery_ref,
                field="recovery_ref",
                required=True,
            ),
        )
        instant = _timestamp(now)
        recovered: list[str] = []
        connection = self._connect_write()
        try:
            with write_txn(connection):
                rows = connection.execute(
                    """
                    SELECT * FROM executions
                    WHERE lifecycle NOT LIKE 'terminal_%'
                      AND runtime_instance_id != ?
                    ORDER BY ordinal ASC
                    """,
                    (self.runtime_instance_id,),
                ).fetchall()
                for row in rows:
                    revision = int(row["revision"]) + 1
                    receipt_state = (
                        "unproven" if row["effect_id"] else "not_applicable"
                    )
                    pending_decisions = connection.execute(
                        "SELECT * FROM decisions WHERE execution_id=? AND state='pending' "
                        "ORDER BY ordinal ASC",
                        (row["execution_id"],),
                    ).fetchall()
                    for decision in pending_decisions:
                        connection.execute(
                            """
                            UPDATE decisions
                            SET state='superseded', updated_at=?, revision=revision+1
                            WHERE decision_id=? AND state='pending'
                            """,
                            (instant, decision["decision_id"]),
                        )
                    connection.execute(
                        """
                        UPDATE executions
                        SET lifecycle='terminal_ambiguous', receipt_state=?,
                            updated_at=?, terminal_at=?, recovery_ref=?, revision=?,
                            runtime_instance_id=?
                        WHERE execution_id=?
                        """,
                        (
                            receipt_state,
                            instant,
                            instant,
                            recovery_ref,
                            revision,
                            self.runtime_instance_id,
                            row["execution_id"],
                        ),
                    )
                    self._append_event_in_txn(
                        connection,
                        execution_id=row["execution_id"],
                        event_type="execution.recovered",
                        from_lifecycle=row["lifecycle"],
                        to_lifecycle="terminal_ambiguous",
                        reason_code="runtime_restarted",
                        occurred_at=instant,
                        revision=revision,
                    )
                    for decision in pending_decisions:
                        self._append_event_in_txn(
                            connection,
                            execution_id=row["execution_id"],
                            event_type="decision.superseded",
                            from_lifecycle="terminal_ambiguous",
                            to_lifecycle="terminal_ambiguous",
                            decision_id=decision["decision_id"],
                            reason_code="runtime_restarted",
                            occurred_at=instant,
                            revision=revision,
                        )
                    recovered.append(str(row["execution_id"]))
            return recovered
        finally:
            connection.close()

    def create_decision(
        self,
        *,
        execution_id: str,
        effect_id: str,
        proposal_ref: str,
        request_digest: str,
        allowed_choices: list[str] | tuple[str, ...],
        expires_at: datetime,
        candidate_digest: Optional[str] = None,
        policy_digest: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        self.initialize()
        self._assert_identifier_scope(execution_id, "exe")
        effect_id = cast(
            str,
            _validated_ref(effect_id, field="effect_id", required=True),
        )
        proposal_ref = cast(
            str,
            _validated_ref(
                proposal_ref,
                field="proposal_ref",
                required=True,
            ),
        )
        request_digest = cast(
            str,
            _validated_digest(
                request_digest,
                field="request_digest",
                required=True,
            ),
        )
        candidate_digest = _validated_digest(
            candidate_digest,
            field="candidate_digest",
            required=False,
        )
        policy_digest = _validated_digest(
            policy_digest,
            field="policy_digest",
            required=False,
        )
        choices = _validated_choices(allowed_choices)
        instant_dt = now or _utc_now()
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ContractValidationError("expires_at must be timezone-aware")
        if instant_dt.tzinfo is None or instant_dt.utcoffset() is None:
            raise ContractValidationError("now must be timezone-aware")
        instant = _timestamp(instant_dt)
        expiry = _timestamp(expires_at)
        decision_id = self._new_id("dec")
        connection = self._connect_write()
        try:
            with write_txn(connection):
                self._after_write_lock_acquired("decision", execution_id)
                execution = self._execution_row(connection, execution_id)
                if execution is None:
                    raise ContractNotFoundError("execution not found")
                current_lifecycle = str(execution["lifecycle"])
                if execution["effect_id"] != effect_id:
                    raise ContractConflictError("decision effect binding mismatch")
                if execution["proposal_ref"] != proposal_ref:
                    raise ContractConflictError("decision proposal binding mismatch")
                existing = connection.execute(
                    "SELECT * FROM decisions WHERE execution_id=? AND request_digest=?",
                    (execution_id, request_digest),
                ).fetchone()
                if existing is not None:
                    immutable = (
                        effect_id,
                        proposal_ref,
                        candidate_digest,
                        policy_digest,
                        json.dumps(choices, separators=(",", ":")),
                        expiry,
                    )
                    actual = (
                        existing["effect_id"],
                        existing["proposal_ref"],
                        existing["candidate_digest"],
                        existing["policy_digest"],
                        existing["allowed_choices_json"],
                        existing["expires_at"],
                    )
                    if immutable != actual:
                        raise ContractConflictError(
                            "decision request digest has conflicting bindings"
                        )
                    return self._decision_from_row(existing)
                if expires_at.astimezone(timezone.utc) <= instant_dt.astimezone(
                    timezone.utc
                ):
                    raise ContractValidationError("expires_at must be in the future")
                published_effect = connection.execute(
                    "SELECT 1 FROM effect_evidence WHERE execution_id=? "
                    "UNION ALL SELECT 1 FROM receipts WHERE execution_id=? LIMIT 1",
                    (execution_id, execution_id),
                ).fetchone()
                if published_effect is not None:
                    raise ContractConflictError(
                        "execution effect evidence already exists; no new decision is allowed"
                    )
                pending = connection.execute(
                    "SELECT decision_id FROM decisions "
                    "WHERE execution_id=? AND state='pending' LIMIT 1",
                    (execution_id,),
                ).fetchone()
                if pending is not None:
                    raise ContractConflictError(
                        "execution already has a pending decision"
                    )
                if "awaiting_decision" not in _ALLOWED_TRANSITIONS.get(
                    current_lifecycle,
                    frozenset(),
                ):
                    raise ContractConflictError(
                        f"execution cannot request a decision from {current_lifecycle}"
                    )
                connection.execute(
                    """
                    INSERT INTO decisions(
                        decision_id, execution_id, profile_id, authority_id,
                        effect_id, proposal_ref, request_digest, candidate_digest,
                        policy_digest, allowed_choices_json, expires_at, state,
                        created_at, updated_at, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, 1)
                    """,
                    (
                        decision_id,
                        execution_id,
                        self.authority.profile_id,
                        self.authority.authority_id,
                        effect_id,
                        proposal_ref,
                        request_digest,
                        candidate_digest,
                        policy_digest,
                        json.dumps(choices, separators=(",", ":")),
                        expiry,
                        instant,
                        instant,
                    ),
                )
                revision = int(execution["revision"]) + 1
                connection.execute(
                    """
                    UPDATE executions
                    SET lifecycle='awaiting_decision', updated_at=?, revision=?
                    WHERE execution_id=? AND revision=? AND lifecycle=?
                    """,
                    (
                        instant,
                        revision,
                        execution_id,
                        execution["revision"],
                        current_lifecycle,
                    ),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ContractConflictError("execution decision transition conflict")
                self._append_event_in_txn(
                    connection,
                    execution_id=execution_id,
                    event_type="decision.requested",
                    from_lifecycle=current_lifecycle,
                    to_lifecycle="awaiting_decision",
                    decision_id=decision_id,
                    occurred_at=instant,
                    revision=revision,
                )
                row = connection.execute(
                    "SELECT * FROM decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                assert row is not None
                return self._decision_from_row(row)
        finally:
            connection.close()

    def resolve_decision(
        self,
        decision_id: str,
        *,
        choice: str,
        resolution_evidence_digest: str,
        expected_revision: int = 1,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        self.initialize()
        self._assert_identifier_scope(decision_id, "dec")
        choice = cast(
            str,
            _validated_ref(
                choice,
                field="choice",
                required=True,
                max_length=64,
            ),
        )
        evidence_digest = cast(
            str,
            _validated_digest(
                resolution_evidence_digest,
                field="resolution_evidence_digest",
                required=True,
            ),
        )
        instant = _timestamp(now)
        connection = self._connect_write()
        try:
            with write_txn(connection):
                row = connection.execute(
                    "SELECT * FROM decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                if row is None:
                    raise ContractNotFoundError("decision not found")
                if int(row["revision"]) != int(expected_revision):
                    if (
                        row["state"] == "resolved"
                        and row["choice"] == choice
                        and row["resolution_evidence_digest"] == evidence_digest
                    ):
                        return self._decision_from_row(row)
                    raise ContractConflictError("decision revision conflict")
                if row["state"] != "pending":
                    raise ContractConflictError("decision is not pending")
                if _parse_timestamp(row["expires_at"], field="expires_at") <= _parse_timestamp(
                    instant,
                    field="now",
                ):
                    raise ContractConflictError("decision expired")
                choices = json.loads(row["allowed_choices_json"])
                if choice not in choices:
                    raise ContractValidationError("choice is not allowed")
                execution = self._execution_row(connection, row["execution_id"])
                if execution is None:
                    raise ContractDataError("decision execution is missing")
                current_lifecycle = str(execution["lifecycle"])
                if "running" not in _ALLOWED_TRANSITIONS.get(
                    current_lifecycle,
                    frozenset(),
                ):
                    raise ContractConflictError(
                        f"decision cannot resolve execution from {current_lifecycle}"
                    )
                connection.execute(
                    """
                    UPDATE decisions
                    SET state='resolved', choice=?, resolution_evidence_digest=?,
                        updated_at=?, resolved_at=?, revision=revision+1
                    WHERE decision_id=? AND revision=?
                    """,
                    (choice, evidence_digest, instant, instant, decision_id, expected_revision),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ContractConflictError("decision revision conflict")
                execution_revision = int(execution["revision"]) + 1
                connection.execute(
                    """
                    UPDATE executions SET lifecycle='running', updated_at=?, revision=?
                    WHERE execution_id=? AND revision=? AND lifecycle=?
                    """,
                    (
                        instant,
                        execution_revision,
                        row["execution_id"],
                        execution["revision"],
                        current_lifecycle,
                    ),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ContractConflictError("execution decision transition conflict")
                self._append_event_in_txn(
                    connection,
                    execution_id=row["execution_id"],
                    event_type="decision.resolved",
                    from_lifecycle=current_lifecycle,
                    to_lifecycle="running",
                    decision_id=decision_id,
                    occurred_at=instant,
                    revision=execution_revision,
                )
                updated = connection.execute(
                    "SELECT * FROM decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                assert updated is not None
                return self._decision_from_row(updated)
        finally:
            connection.close()

    def expire_decisions(self, *, now: Optional[datetime] = None) -> list[str]:
        self.initialize()
        instant = _timestamp(now)
        expired: list[str] = []
        connection = self._connect_write()
        try:
            with write_txn(connection):
                rows = connection.execute(
                    "SELECT * FROM decisions WHERE state='pending' AND expires_at <= ? "
                    "ORDER BY ordinal ASC",
                    (instant,),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        UPDATE decisions SET state='expired', updated_at=?,
                            revision=revision+1 WHERE decision_id=?
                        """,
                        (instant, row["decision_id"]),
                    )
                    execution = self._execution_row(connection, row["execution_id"])
                    assert execution is not None
                    if execution["lifecycle"] in TERMINAL_EXECUTION_STATES:
                        revision = int(execution["revision"])
                        self._append_event_in_txn(
                            connection,
                            execution_id=row["execution_id"],
                            event_type="decision.expired",
                            from_lifecycle=execution["lifecycle"],
                            to_lifecycle=execution["lifecycle"],
                            decision_id=row["decision_id"],
                            reason_code="decision_expired_after_terminal",
                            occurred_at=instant,
                            revision=revision,
                        )
                        expired.append(str(row["decision_id"]))
                        continue
                    revision = int(execution["revision"]) + 1
                    connection.execute(
                        """
                        UPDATE executions
                        SET lifecycle='terminal_ambiguous', receipt_state='unproven',
                            updated_at=?, terminal_at=?, revision=?
                        WHERE execution_id=?
                        """,
                        (instant, instant, revision, row["execution_id"]),
                    )
                    self._append_event_in_txn(
                        connection,
                        execution_id=row["execution_id"],
                        event_type="decision.expired",
                        from_lifecycle=execution["lifecycle"],
                        to_lifecycle="terminal_ambiguous",
                        decision_id=row["decision_id"],
                        reason_code="decision_expired",
                        occurred_at=instant,
                        revision=revision,
                    )
                    expired.append(str(row["decision_id"]))
            return expired
        finally:
            connection.close()

    def supersede_decision(
        self,
        decision_id: str,
        *,
        reason_code: str,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        self.initialize()
        self._assert_identifier_scope(decision_id, "dec")
        reason_code = cast(
            str,
            _validated_ref(
                reason_code,
                field="reason_code",
                required=True,
                max_length=128,
            ),
        )
        instant = _timestamp(now)
        connection = self._connect_write()
        try:
            with write_txn(connection):
                row = connection.execute(
                    "SELECT * FROM decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                if row is None:
                    raise ContractNotFoundError("decision not found")
                if row["state"] != "pending":
                    raise ContractConflictError("decision is not pending")
                execution = self._execution_row(connection, row["execution_id"])
                if execution is None:
                    raise ContractDataError("decision execution is missing")
                current_lifecycle = str(execution["lifecycle"])
                if "running" not in _ALLOWED_TRANSITIONS.get(
                    current_lifecycle,
                    frozenset(),
                ):
                    raise ContractConflictError(
                        f"decision cannot be superseded from {current_lifecycle}"
                    )
                connection.execute(
                    """
                    UPDATE decisions SET state='superseded', updated_at=?,
                        revision=revision+1 WHERE decision_id=?
                    """,
                    (instant, decision_id),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ContractConflictError("decision supersession conflict")
                revision = int(execution["revision"]) + 1
                connection.execute(
                    """
                    UPDATE executions SET lifecycle='running', updated_at=?, revision=?
                    WHERE execution_id=? AND revision=? AND lifecycle=?
                    """,
                    (
                        instant,
                        revision,
                        row["execution_id"],
                        execution["revision"],
                        current_lifecycle,
                    ),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ContractConflictError("execution decision transition conflict")
                self._append_event_in_txn(
                    connection,
                    execution_id=row["execution_id"],
                    event_type="decision.superseded",
                    from_lifecycle=current_lifecycle,
                    to_lifecycle="running",
                    decision_id=decision_id,
                    reason_code=reason_code,
                    occurred_at=instant,
                    revision=revision,
                )
                updated = connection.execute(
                    "SELECT * FROM decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                assert updated is not None
                return self._decision_from_row(updated)
        finally:
            connection.close()

    def record_effect_evidence(
        self,
        *,
        execution_id: str,
        effect_id: str,
        outcome: str,
        subject_digest: str,
        evidence_digest: str,
        result_digest: str,
        decision_id: Optional[str] = None,
        recovery_ref: Optional[str] = None,
        reconciliation_ref: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Record closed executor evidence; never infer it from generic output."""

        self.initialize()
        self._assert_identifier_scope(execution_id, "exe")
        if outcome not in RECEIPT_OUTCOMES:
            raise ContractValidationError("unknown effect outcome")
        effect_id = cast(
            str,
            _validated_ref(effect_id, field="effect_id", required=True),
        )
        subject_digest = cast(
            str,
            _validated_digest(
                subject_digest,
                field="subject_digest",
                required=True,
            ),
        )
        evidence_digest = cast(
            str,
            _validated_digest(
                evidence_digest,
                field="evidence_digest",
                required=True,
            ),
        )
        result_digest = cast(
            str,
            _validated_digest(
                result_digest,
                field="result_digest",
                required=True,
            ),
        )
        if decision_id:
            self._assert_identifier_scope(decision_id, "dec")
        recovery_ref = _validated_ref(recovery_ref, field="recovery_ref")
        reconciliation_ref = _validated_ref(
            reconciliation_ref,
            field="reconciliation_ref",
        )
        instant = _timestamp(now)
        evidence_id = f"evd_{self.authority.profile_key}_{uuid.uuid4().hex}"
        connection = self._connect_write()
        try:
            with write_txn(connection):
                self._after_write_lock_acquired("evidence", execution_id)
                execution = self._execution_row(connection, execution_id)
                if execution is None:
                    raise ContractNotFoundError("execution not found")
                if execution["effect_id"] != effect_id:
                    raise ContractConflictError("effect evidence binding mismatch")
                if (
                    execution["lifecycle"] == "terminal_ambiguous"
                    and execution["receipt_state"] == "unproven"
                ):
                    raise ContractConflictError(
                        "late ambiguous evidence requires the reconciliation path"
                    )
                latest_decision = connection.execute(
                    "SELECT * FROM decisions WHERE execution_id=? "
                    "ORDER BY ordinal DESC LIMIT 1",
                    (execution_id,),
                ).fetchone()
                if latest_decision is not None:
                    if latest_decision["state"] == "pending":
                        raise ContractConflictError(
                            "newer pending decision blocks effect evidence"
                        )
                    if latest_decision["state"] != "resolved":
                        raise ContractConflictError(
                            "latest decision does not authorize effect evidence"
                        )
                    if not decision_id:
                        raise ContractConflictError(
                            "effect evidence requires its exact decision binding to the "
                            "latest resolved decision"
                        )
                    if latest_decision["decision_id"] != decision_id:
                        raise ContractConflictError(
                            "effect evidence is not bound to the latest decision"
                        )
                    if (
                        latest_decision["execution_id"] != execution_id
                        or latest_decision["effect_id"] != effect_id
                        or latest_decision["proposal_ref"]
                        != execution["proposal_ref"]
                    ):
                        raise ContractConflictError("decision evidence binding mismatch")
                elif decision_id:
                    raise ContractConflictError(
                        "effect evidence decision is not bound to the execution"
                    )
                existing = connection.execute(
                    "SELECT * FROM effect_evidence WHERE execution_id=?",
                    (execution_id,),
                ).fetchone()
                immutable = (
                    effect_id,
                    outcome,
                    subject_digest,
                    evidence_digest,
                    result_digest,
                    decision_id,
                    recovery_ref,
                    reconciliation_ref,
                )
                if existing is not None:
                    actual = tuple(
                        existing[key]
                        for key in (
                            "effect_id",
                            "outcome",
                            "subject_digest",
                            "evidence_digest",
                            "result_digest",
                            "decision_id",
                            "recovery_ref",
                            "reconciliation_ref",
                        )
                    )
                    if immutable != actual:
                        raise ContractConflictError("conflicting effect evidence")
                    return self._evidence_from_row(existing)
                connection.execute(
                    """
                    INSERT INTO effect_evidence(
                        evidence_id, execution_id, profile_id, authority_id,
                        executor_id, effect_id, outcome, subject_digest,
                        evidence_digest, result_digest, decision_id, recovery_ref,
                        reconciliation_ref, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_id,
                        execution_id,
                        self.authority.profile_id,
                        self.authority.authority_id,
                        self.authority.executor_id,
                        effect_id,
                        outcome,
                        subject_digest,
                        evidence_digest,
                        result_digest,
                        decision_id,
                        recovery_ref,
                        reconciliation_ref,
                        instant,
                    ),
                )
                self._append_event_in_txn(
                    connection,
                    execution_id=execution_id,
                    event_type="effect.evidence_recorded",
                    decision_id=decision_id,
                    occurred_at=instant,
                    revision=int(execution["revision"]),
                )
                inserted = connection.execute(
                    "SELECT * FROM effect_evidence WHERE evidence_id=?",
                    (evidence_id,),
                ).fetchone()
                assert inserted is not None
                return self._evidence_from_row(inserted)
        finally:
            connection.close()

    def reconcile_ambiguous_evidence(
        self,
        *,
        execution_id: str,
        effect_id: str,
        outcome: str,
        subject_digest: str,
        evidence_digest: str,
        result_digest: str,
        reconciliation_ref: str,
        decision_id: Optional[str] = None,
        recovery_ref: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Publish late authoritative ambiguous evidence in one transaction.

        This path never upgrades an ambiguous execution to success. It exists
        only for an executor that can later prove that the terminal outcome
        itself was ambiguous; generic process, chat, session, or Kanban state
        is not accepted as evidence.
        """

        self.initialize()
        self._assert_identifier_scope(execution_id, "exe")
        if outcome != "ambiguous":
            raise ContractValidationError(
                "late reconciliation only accepts authoritative ambiguous evidence"
            )
        effect_id = cast(
            str,
            _validated_ref(effect_id, field="effect_id", required=True),
        )
        subject_digest = cast(
            str,
            _validated_digest(subject_digest, field="subject_digest", required=True),
        )
        evidence_digest = cast(
            str,
            _validated_digest(
                evidence_digest,
                field="evidence_digest",
                required=True,
            ),
        )
        result_digest = cast(
            str,
            _validated_digest(result_digest, field="result_digest", required=True),
        )
        if decision_id:
            self._assert_identifier_scope(decision_id, "dec")
        recovery_ref = _validated_ref(recovery_ref, field="recovery_ref")
        reconciliation_ref = cast(
            str,
            _validated_ref(
                reconciliation_ref,
                field="reconciliation_ref",
                required=True,
            ),
        )
        instant = _timestamp(now)
        evidence_id = f"evd_{self.authority.profile_key}_{uuid.uuid4().hex}"
        connection = self._connect_write()
        try:
            with write_txn(connection):
                execution = self._execution_row(connection, execution_id)
                if execution is None:
                    raise ContractNotFoundError("execution not found")
                if execution["effect_id"] != effect_id:
                    raise ContractConflictError("effect evidence binding mismatch")
                actual_recovery_ref = recovery_ref or execution["recovery_ref"]
                if (
                    execution["lifecycle"] != "terminal_ambiguous"
                    or execution["receipt_state"] != "unproven"
                    or execution["receipt_id"] is not None
                ):
                    existing_receipt = connection.execute(
                        "SELECT * FROM receipts WHERE execution_id=?",
                        (execution_id,),
                    ).fetchone()
                    if existing_receipt is not None:
                        immutable = (
                            effect_id,
                            outcome,
                            subject_digest,
                            evidence_digest,
                            result_digest,
                            decision_id,
                            actual_recovery_ref,
                            reconciliation_ref,
                        )
                        actual = (
                            existing_receipt["effect_id"],
                            existing_receipt["outcome"],
                            existing_receipt["subject_digest"],
                            existing_receipt["evidence_digest"],
                            existing_receipt["result_digest"],
                            existing_receipt["decision_id"],
                            existing_receipt["recovery_ref"],
                            existing_receipt["reconciliation_ref"],
                        )
                        if immutable == actual:
                            return self._receipt_from_row(
                                existing_receipt,
                                connection=connection,
                            )
                    raise ContractConflictError(
                        "execution is not eligible for late ambiguous reconciliation"
                    )

                latest_decision = connection.execute(
                    "SELECT * FROM decisions WHERE execution_id=? "
                    "ORDER BY ordinal DESC LIMIT 1",
                    (execution_id,),
                ).fetchone()
                if latest_decision is not None:
                    if latest_decision["state"] == "pending":
                        raise ContractConflictError(
                            "newer pending decision blocks effect evidence"
                        )
                    if (
                        latest_decision["state"] != "resolved"
                        or latest_decision["decision_id"] != decision_id
                        or latest_decision["effect_id"] != effect_id
                        or latest_decision["proposal_ref"]
                        != execution["proposal_ref"]
                    ):
                        raise ContractConflictError(
                            "late evidence requires the latest resolved decision"
                        )
                elif decision_id:
                    raise ContractConflictError(
                        "effect evidence decision is not bound to the execution"
                    )

                existing_evidence = connection.execute(
                    "SELECT * FROM effect_evidence WHERE execution_id=?",
                    (execution_id,),
                ).fetchone()
                if existing_evidence is not None:
                    raise ContractDataError(
                        "late effect evidence exists without its transactional receipt"
                    )

                connection.execute(
                    """
                    INSERT INTO effect_evidence(
                        evidence_id, execution_id, profile_id, authority_id,
                        executor_id, effect_id, outcome, subject_digest,
                        evidence_digest, result_digest, decision_id, recovery_ref,
                        reconciliation_ref, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_id,
                        execution_id,
                        self.authority.profile_id,
                        self.authority.authority_id,
                        self.authority.executor_id,
                        effect_id,
                        outcome,
                        subject_digest,
                        evidence_digest,
                        result_digest,
                        decision_id,
                        actual_recovery_ref,
                        reconciliation_ref,
                        instant,
                    ),
                )
                evidence = connection.execute(
                    "SELECT * FROM effect_evidence WHERE evidence_id=?",
                    (evidence_id,),
                ).fetchone()
                assert evidence is not None
                receipt_id = self._publish_receipt_in_txn(
                    connection,
                    row=execution,
                    evidence=evidence,
                    terminal_at=instant,
                )
                revision = int(execution["revision"]) + 1
                connection.execute(
                    """
                    UPDATE executions
                    SET receipt_state='published', receipt_id=?, updated_at=?,
                        recovery_ref=?, revision=?, runtime_instance_id=?
                    WHERE execution_id=? AND revision=?
                    """,
                    (
                        receipt_id,
                        instant,
                        actual_recovery_ref,
                        revision,
                        self.runtime_instance_id,
                        execution_id,
                        execution["revision"],
                    ),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ContractConflictError("execution reconciliation conflict")
                self._append_event_in_txn(
                    connection,
                    execution_id=execution_id,
                    event_type="effect.evidence_recorded",
                    from_lifecycle="terminal_ambiguous",
                    to_lifecycle="terminal_ambiguous",
                    decision_id=decision_id,
                    reason_code="late_ambiguous_evidence",
                    occurred_at=instant,
                    revision=revision,
                )
                self._append_event_in_txn(
                    connection,
                    execution_id=execution_id,
                    event_type="receipt.published",
                    from_lifecycle="terminal_ambiguous",
                    to_lifecycle="terminal_ambiguous",
                    decision_id=decision_id,
                    receipt_id=receipt_id,
                    reason_code="late_ambiguous_reconciliation",
                    occurred_at=instant,
                    revision=revision,
                )
                receipt = connection.execute(
                    "SELECT * FROM receipts WHERE receipt_id=?",
                    (receipt_id,),
                ).fetchone()
                assert receipt is not None
                return self._receipt_from_row(receipt, connection=connection)
        finally:
            connection.close()

    def prune_events(
        self,
        *,
        older_than: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> int:
        """Prune terminal history while persisting an explicit cursor watermark."""

        self.initialize()
        current = now or _utc_now()
        cutoff = older_than or (
            current - timedelta(seconds=EVENT_RETENTION_SECONDS)
        )
        cutoff_text = _timestamp(cutoff)
        connection = self._connect_write()
        try:
            with write_txn(connection):
                self._after_write_lock_acquired("prune", "")
                try:
                    prior = int(
                        connection.execute(
                            "SELECT value FROM execution_contract_metadata "
                            "WHERE key='events_pruned_through'"
                        ).fetchone()[0]
                    )
                except (TypeError, ValueError, sqlite3.Error) as exc:
                    raise ContractDataError(
                        "event retention watermark is corrupt"
                    ) from exc
                sequence_row = connection.execute(
                    "SELECT seq FROM sqlite_sequence "
                    "WHERE name='execution_events'"
                ).fetchone()
                allocated_high_water = int(sequence_row[0]) if sequence_row else 0
                current_high_water = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM execution_events"
                    ).fetchone()[0]
                )
                if (
                    prior < 0
                    or allocated_high_water < 0
                    or current_high_water < 0
                    or prior > allocated_high_water
                    or current_high_water > allocated_high_water
                ):
                    raise ContractDataError("event high-water metadata is corrupt")
                self._validate_global_event_continuity(
                    connection,
                    pruned_through=prior,
                    snapshot_high_water=allocated_high_water,
                )
                rows = connection.execute(
                    """
                    SELECT ev.sequence, ev.occurred_at, ex.lifecycle
                    FROM execution_events ev
                    JOIN executions ex ON ex.execution_id = ev.execution_id
                    WHERE ev.sequence > ?
                    ORDER BY ev.sequence ASC
                    """,
                    (prior,),
                ).fetchall()
                through = allocated_high_water
                for row in rows:
                    if not (
                        str(row["occurred_at"]) < cutoff_text
                        and str(row["lifecycle"]).startswith("terminal_")
                    ):
                        through = int(row["sequence"]) - 1
                        break
                if through <= prior:
                    return 0
                deleted = connection.execute(
                    "DELETE FROM execution_events WHERE sequence <= ?",
                    (through,),
                ).rowcount
                expected_deleted = through - prior
                if int(deleted or 0) != expected_deleted:
                    raise ContractDataError("event pruning row count is inconsistent")
                connection.execute(
                    "UPDATE execution_contract_metadata SET value=? "
                    "WHERE key='events_pruned_through'",
                    (str(through),),
                )
                return int(deleted or 0)
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Side-effect-free public reads
    # ------------------------------------------------------------------

    def list_executions(
        self,
        *,
        after: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
        lifecycle: Optional[str] = None,
        snapshot_high_water: Optional[int] = None,
    ) -> dict[str, Any]:
        after = _validated_cursor(after)
        limit = _validated_limit(limit)
        snapshot_high_water = _validated_snapshot(snapshot_high_water)
        if lifecycle is not None and lifecycle not in EXECUTION_STATES:
            raise ContractValidationError("unknown execution lifecycle")
        with self._read_connection() as connection:
            if connection is None:
                if after > 0 or snapshot_high_water not in (None, 0):
                    raise ContractConflictError("execution cursor is ahead of high-water")
                return self._collection_page([], after, 0, 0, False, 0)
            pinned = self._pin_collection_snapshot(
                connection,
                table="executions",
                after=after,
                requested=snapshot_high_water,
            )
            params: list[Any] = [after, pinned]
            where = "ordinal > ? AND ordinal <= ?"
            if lifecycle:
                where += " AND lifecycle = ?"
                params.append(lifecycle)
            params.append(limit + 1)
            rows = connection.execute(
                f"SELECT * FROM executions WHERE {where} ORDER BY ordinal ASC LIMIT ?",
                params,
            ).fetchall()
            return self._rows_page(
                rows,
                after=after,
                limit=limit,
                decoder=self._execution_from_row,
                connection=connection,
                snapshot_high_water=pinned,
            )

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        self._assert_identifier_scope(execution_id, "exe")
        with self._read_connection() as connection:
            if connection is None:
                raise ContractNotFoundError("execution not found")
            row = self._execution_row(connection, execution_id)
            if row is None:
                raise ContractNotFoundError("execution not found")
            return self._execution_from_row(row, connection=connection)

    def get_execution_by_source_run_id(self, source_run_id: str) -> dict[str, Any]:
        source_run_id = cast(
            str,
            _validated_ref(
                source_run_id,
                field="source_run_id",
                required=True,
            ),
        )
        with self._read_connection() as connection:
            if connection is None:
                raise ContractNotFoundError("execution not found")
            row = connection.execute(
                "SELECT * FROM executions WHERE source_run_id=?",
                (source_run_id,),
            ).fetchone()
            if row is None:
                raise ContractNotFoundError("execution not found")
            return self._execution_from_row(row, connection=connection)

    def list_decisions(
        self,
        *,
        after: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
        state: Optional[str] = None,
        snapshot_high_water: Optional[int] = None,
    ) -> dict[str, Any]:
        after = _validated_cursor(after)
        limit = _validated_limit(limit)
        snapshot_high_water = _validated_snapshot(snapshot_high_water)
        if state is not None and state not in DECISION_STATES:
            raise ContractValidationError("unknown decision state")
        with self._read_connection() as connection:
            if connection is None:
                if after > 0 or snapshot_high_water not in (None, 0):
                    raise ContractConflictError("decision cursor is ahead of high-water")
                return self._collection_page([], after, 0, 0, False, 0)
            pinned = self._pin_collection_snapshot(
                connection,
                table="decisions",
                after=after,
                requested=snapshot_high_water,
            )
            params: list[Any] = [after, pinned]
            where = "ordinal > ? AND ordinal <= ?"
            if state:
                where += " AND state = ?"
                params.append(state)
            params.append(limit + 1)
            rows = connection.execute(
                f"SELECT * FROM decisions WHERE {where} ORDER BY ordinal ASC LIMIT ?",
                params,
            ).fetchall()
            return self._rows_page(
                rows,
                after=after,
                limit=limit,
                decoder=self._decision_from_row,
                connection=connection,
                snapshot_high_water=pinned,
            )

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        self._assert_identifier_scope(decision_id, "dec")
        with self._read_connection() as connection:
            if connection is None:
                raise ContractNotFoundError("decision not found")
            row = connection.execute(
                "SELECT * FROM decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise ContractNotFoundError("decision not found")
            return self._decision_from_row(row, connection=connection)

    def list_receipts(
        self,
        *,
        after: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
        outcome: Optional[str] = None,
        snapshot_high_water: Optional[int] = None,
    ) -> dict[str, Any]:
        after = _validated_cursor(after)
        limit = _validated_limit(limit)
        snapshot_high_water = _validated_snapshot(snapshot_high_water)
        if outcome is not None and outcome not in RECEIPT_OUTCOMES:
            raise ContractValidationError("unknown receipt outcome")
        with self._read_connection() as connection:
            if connection is None:
                if after > 0 or snapshot_high_water not in (None, 0):
                    raise ContractConflictError("receipt cursor is ahead of high-water")
                return self._collection_page([], after, 0, 0, False, 0)
            pinned = self._pin_collection_snapshot(
                connection,
                table="receipts",
                after=after,
                requested=snapshot_high_water,
            )
            params: list[Any] = [after, pinned]
            where = "ordinal > ? AND ordinal <= ?"
            if outcome:
                where += " AND outcome = ?"
                params.append(outcome)
            params.append(limit + 1)
            rows = connection.execute(
                f"SELECT * FROM receipts WHERE {where} ORDER BY ordinal ASC LIMIT ?",
                params,
            ).fetchall()
            return self._rows_page(
                rows,
                after=after,
                limit=limit,
                decoder=self._receipt_from_row,
                connection=connection,
                snapshot_high_water=pinned,
            )

    def get_receipt(self, receipt_id: str) -> dict[str, Any]:
        self._assert_identifier_scope(receipt_id, "rcp")
        with self._read_connection() as connection:
            if connection is None:
                raise ContractNotFoundError("receipt not found")
            row = connection.execute(
                "SELECT * FROM receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise ContractNotFoundError("receipt not found")
            return self._receipt_from_row(row, connection=connection)

    def list_events(
        self,
        *,
        after: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
        execution_id: Optional[str] = None,
        snapshot_high_water: Optional[int] = None,
    ) -> dict[str, Any]:
        after = _validated_cursor(after)
        limit = _validated_limit(limit)
        snapshot_high_water = _validated_snapshot(snapshot_high_water)
        if execution_id:
            self._assert_identifier_scope(execution_id, "exe")
        with self._read_connection() as connection:
            if connection is None:
                if after > 0 or snapshot_high_water not in (None, 0):
                    raise ContractConflictError(
                        "event cursor is ahead of high-water"
                    )
                return self._event_page([], after, 0, 1, False, 0, 0)
            sequence_row = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='execution_events'"
            ).fetchone()
            allocated_high_water = int(sequence_row[0]) if sequence_row else 0
            current_high_water = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM execution_events"
                ).fetchone()[0]
            )
            try:
                pruned_through = int(
                    connection.execute(
                        "SELECT value FROM execution_contract_metadata "
                        "WHERE key='events_pruned_through'"
                    ).fetchone()[0]
                )
            except (TypeError, ValueError, sqlite3.Error) as exc:
                raise ContractDataError("event retention watermark is corrupt") from exc
            if (
                allocated_high_water < 0
                or current_high_water < 0
                or current_high_water > allocated_high_water
                or pruned_through < 0
                or pruned_through > allocated_high_water
            ):
                raise ContractDataError("event high-water metadata is corrupt")
            available_high_water = allocated_high_water
            high_water = (
                available_high_water
                if snapshot_high_water is None
                else snapshot_high_water
            )
            if high_water > available_high_water:
                raise ContractConflictError("event snapshot is ahead of high-water")
            if after > high_water:
                raise ContractConflictError("event cursor is ahead of snapshot")
            minimum_available_row = connection.execute(
                "SELECT MIN(sequence) FROM execution_events"
            ).fetchone()
            minimum_available = (
                int(minimum_available_row[0])
                if minimum_available_row and minimum_available_row[0] is not None
                else pruned_through + 1
            )
            if after < pruned_through:
                raise ContractCursorGoneError(minimum_available, high_water)
            self._after_snapshot_pinned("events", high_water)
            self._validate_global_event_continuity(
                connection,
                pruned_through=pruned_through,
                snapshot_high_water=high_water,
            )
            params: list[Any] = [after, high_water]
            where = "sequence > ? AND sequence <= ?"
            if execution_id:
                where += " AND execution_id = ?"
                params.append(execution_id)
            params.append(limit + 1)
            rows = connection.execute(
                f"SELECT * FROM execution_events WHERE {where} "
                "ORDER BY sequence ASC LIMIT ?",
                params,
            ).fetchall()
            visible = rows[:limit]
            has_more = len(rows) > limit
            cursor = (
                int(visible[-1]["sequence"])
                if has_more and visible
                else high_water
            )
            return self._event_page(
                [self._event_from_row(row, connection=connection) for row in visible],
                cursor,
                high_water,
                minimum_available,
                has_more,
                pruned_through,
                high_water,
            )

    # ------------------------------------------------------------------
    # Internal serialization and invariants
    # ------------------------------------------------------------------

    def _new_id(self, kind: str) -> str:
        return f"{kind}_{self.authority.profile_key}_{uuid.uuid4().hex}"

    def _assert_identifier_scope(self, identifier: str, expected_kind: str) -> None:
        match = _ID_RE.fullmatch(str(identifier or ""))
        if match is None or match.group(1) != expected_kind:
            raise ContractValidationError(f"invalid {expected_kind} identifier")
        if match.group(2) != self.authority.profile_key:
            raise ContractForbiddenError("identifier belongs to another profile")

    @staticmethod
    def _execution_row(
        connection: sqlite3.Connection,
        execution_id: str,
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()

    def _append_event_in_txn(
        self,
        connection: sqlite3.Connection,
        *,
        execution_id: str,
        event_type: str,
        occurred_at: str,
        revision: int,
        from_lifecycle: Optional[str] = None,
        to_lifecycle: Optional[str] = None,
        decision_id: Optional[str] = None,
        receipt_id: Optional[str] = None,
        reason_code: Optional[str] = None,
    ) -> int:
        if event_type not in EVENT_TYPES:
            raise ContractValidationError("unknown event type")
        event_id = self._new_id("evt")
        cursor = connection.execute(
            """
            INSERT INTO execution_events(
                event_id, execution_id, profile_id, authority_id, event_type,
                from_lifecycle, to_lifecycle, decision_id, receipt_id,
                reason_code, occurred_at, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                execution_id,
                self.authority.profile_id,
                self.authority.authority_id,
                event_type,
                from_lifecycle,
                to_lifecycle,
                decision_id,
                receipt_id,
                reason_code,
                occurred_at,
                revision,
            ),
        )
        if cursor.lastrowid is None:
            raise ContractDataError("event append did not allocate a sequence")
        return int(cursor.lastrowid)

    def _publish_receipt_in_txn(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        evidence: sqlite3.Row,
        terminal_at: str,
    ) -> str:
        existing = connection.execute(
            "SELECT * FROM receipts WHERE execution_id=?",
            (row["execution_id"],),
        ).fetchone()
        if existing is not None:
            immutable = (
                evidence["effect_id"],
                evidence["outcome"],
                evidence["subject_digest"],
                evidence["evidence_digest"],
                evidence["result_digest"],
                evidence["decision_id"],
                evidence["recovery_ref"],
                evidence["reconciliation_ref"],
            )
            actual = tuple(
                existing[key]
                for key in (
                    "effect_id",
                    "outcome",
                    "subject_digest",
                    "evidence_digest",
                    "result_digest",
                    "decision_id",
                    "recovery_ref",
                    "reconciliation_ref",
                )
            )
            if immutable != actual:
                raise ContractConflictError("conflicting terminal receipt")
            return str(existing["receipt_id"])
        receipt_id = self._new_id("rcp")
        connection.execute(
            """
            INSERT INTO receipts(
                receipt_id, execution_id, effect_id, profile_id, authority_id,
                executor_id, outcome, subject_digest, evidence_digest,
                result_digest, terminal_at, decision_id, recovery_ref,
                reconciliation_ref, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                receipt_id,
                row["execution_id"],
                evidence["effect_id"],
                self.authority.profile_id,
                self.authority.authority_id,
                self.authority.executor_id,
                evidence["outcome"],
                evidence["subject_digest"],
                evidence["evidence_digest"],
                evidence["result_digest"],
                terminal_at,
                evidence["decision_id"],
                evidence["recovery_ref"],
                evidence["reconciliation_ref"],
            ),
        )
        return receipt_id

    def _assert_row_authority(
        self,
        row: sqlite3.Row,
        *,
        require_executor: bool,
    ) -> None:
        if (
            row["profile_id"] != self.authority.profile_id
            or row["authority_id"] != self.authority.authority_id
            or (
                require_executor
                and row["executor_id"] != self.authority.executor_id
            )
        ):
            raise ContractDataError(
                "persisted row authority does not match the active profile"
            )

    def _persisted_identifier(
        self,
        value: Any,
        kind: str,
        *,
        nullable: bool = False,
    ) -> Optional[str]:
        if value is None and nullable:
            return None
        try:
            self._assert_identifier_scope(str(value or ""), kind)
        except (ContractValidationError, ContractForbiddenError) as exc:
            raise ContractDataError(f"persisted {kind} identifier is invalid") from exc
        return str(value)

    @staticmethod
    def _persisted_ref(
        value: Any,
        *,
        field: str,
        required: bool = False,
        max_length: int = 512,
    ) -> Optional[str]:
        try:
            return _validated_ref(
                value,
                field=field,
                required=required,
                max_length=max_length,
            )
        except ContractValidationError as exc:
            raise ContractDataError(f"persisted {field} is invalid") from exc

    @staticmethod
    def _persisted_digest(
        value: Any,
        *,
        field: str,
        required: bool,
    ) -> Optional[str]:
        try:
            return _validated_digest(value, field=field, required=required)
        except ContractValidationError as exc:
            raise ContractDataError(f"persisted {field} is invalid") from exc

    @staticmethod
    def _persisted_revision(value: Any, *, field: str = "revision") -> int:
        if isinstance(value, bool):
            raise ContractDataError(f"persisted {field} is invalid")
        try:
            revision = int(value)
        except (TypeError, ValueError) as exc:
            raise ContractDataError(f"persisted {field} is invalid") from exc
        if revision < 1:
            raise ContractDataError(f"persisted {field} is invalid")
        return revision

    @staticmethod
    def _ordered_timestamps(
        *,
        created: datetime,
        updated: datetime,
        started: Optional[datetime] = None,
        terminal: Optional[datetime] = None,
    ) -> None:
        if updated < created:
            raise ContractDataError("persisted timestamp ordering is invalid")
        if started is not None and (started < created or started > updated):
            raise ContractDataError("persisted timestamp ordering is invalid")
        if terminal is not None and (terminal < created or terminal > updated):
            raise ContractDataError("persisted timestamp ordering is invalid")

    def _execution_from_row(
        self,
        row: sqlite3.Row,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        self._assert_row_authority(row, require_executor=True)
        execution_id = cast(str, self._persisted_identifier(row["execution_id"], "exe"))
        lifecycle = str(row["lifecycle"])
        receipt_state = str(row["receipt_state"])
        if lifecycle not in EXECUTION_STATES or receipt_state not in RECEIPT_STATES:
            raise ContractDataError("execution row contains an unknown state")
        source_run_id = self._persisted_ref(row["source_run_id"], field="source_run_id")
        work_ref = self._persisted_ref(row["work_ref"], field="work_ref")
        proposal_ref = self._persisted_ref(row["proposal_ref"], field="proposal_ref")
        effect_id = self._persisted_ref(row["effect_id"], field="effect_id")
        recovery_ref = self._persisted_ref(row["recovery_ref"], field="recovery_ref")
        self._persisted_ref(
            row["runtime_instance_id"],
            field="runtime_instance_id",
            required=True,
        )
        revision = self._persisted_revision(row["revision"])
        created = _parse_timestamp(row["created_at"], field="created_at")
        updated = _parse_timestamp(row["updated_at"], field="updated_at")
        started = (
            _parse_timestamp(row["started_at"], field="started_at")
            if row["started_at"] is not None
            else None
        )
        terminal = (
            _parse_timestamp(row["terminal_at"], field="terminal_at")
            if row["terminal_at"] is not None
            else None
        )
        self._ordered_timestamps(
            created=created,
            updated=updated,
            started=started,
            terminal=terminal,
        )
        is_terminal = lifecycle in TERMINAL_EXECUTION_STATES
        if is_terminal != (terminal is not None):
            raise ContractDataError("execution terminal timestamp invariant failed")
        if effect_id and not (work_ref and proposal_ref):
            raise ContractDataError("execution effect binding is incomplete")
        receipt_id = self._persisted_identifier(
            row["receipt_id"],
            "rcp",
            nullable=True,
        )
        if effect_id is None:
            if receipt_state != "not_applicable" or receipt_id is not None:
                raise ContractDataError("effect-free execution has receipt state")
        elif receipt_state == "not_applicable":
            raise ContractDataError("effect execution has non-applicable receipt state")
        elif receipt_state == "pending_evidence":
            if is_terminal or receipt_id is not None:
                raise ContractDataError("pending evidence execution is terminal")
        elif receipt_state == "unproven":
            if lifecycle != "terminal_ambiguous" or receipt_id is not None:
                raise ContractDataError("unproven execution is not terminal ambiguous")
        elif receipt_state == "published":
            if not is_terminal or receipt_id is None:
                raise ContractDataError("published execution is missing its receipt")
        if effect_id and is_terminal and lifecycle != "terminal_ambiguous" and (
            receipt_state != "published"
        ):
            raise ContractDataError("proven terminal execution is missing its receipt")
        if connection is not None and receipt_id is not None:
            receipt = connection.execute(
                "SELECT * FROM receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if receipt is None or (
                receipt["execution_id"] != execution_id
                or receipt["effect_id"] != effect_id
            ):
                raise ContractDataError("execution receipt binding is invalid")
            self._receipt_from_row(receipt, connection=connection)
        if connection is not None and effect_id is not None:
            evidence = connection.execute(
                "SELECT * FROM effect_evidence WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if evidence is not None:
                self._evidence_from_row(evidence, connection=connection)
        freshness = "terminal" if lifecycle in TERMINAL_EXECUTION_STATES else (
            "live"
            if row["runtime_instance_id"] == self.runtime_instance_id
            else "stale"
        )
        return {
            "contract_version": CONTRACT_VERSION,
            "execution_id": execution_id,
            "profile_id": row["profile_id"],
            "authority_id": row["authority_id"],
            "executor_id": row["executor_id"],
            "source_run_id": source_run_id,
            "work_ref": work_ref,
            "proposal_ref": proposal_ref,
            "effect_id": effect_id,
            "lifecycle": lifecycle,
            "freshness": freshness,
            "receipt_state": receipt_state,
            "receipt_id": receipt_id,
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "terminal_at": row["terminal_at"],
            "recovery_ref": recovery_ref,
            "revision": revision,
        }

    def _decision_from_row(
        self,
        row: sqlite3.Row,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        self._assert_row_authority(row, require_executor=False)
        decision_id = cast(str, self._persisted_identifier(row["decision_id"], "dec"))
        execution_id = cast(str, self._persisted_identifier(row["execution_id"], "exe"))
        state = str(row["state"])
        if state not in DECISION_STATES:
            raise ContractDataError("decision row contains an unknown state")
        try:
            choices = json.loads(row["allowed_choices_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractDataError("decision choices are malformed") from exc
        try:
            choices = _validated_choices(choices)
        except ContractValidationError as exc:
            raise ContractDataError("decision choices are malformed") from exc
        effect_id = cast(
            str,
            self._persisted_ref(row["effect_id"], field="effect_id", required=True),
        )
        proposal_ref = cast(
            str,
            self._persisted_ref(
                row["proposal_ref"],
                field="proposal_ref",
                required=True,
            ),
        )
        request_digest = cast(
            str,
            self._persisted_digest(
                row["request_digest"],
                field="request_digest",
                required=True,
            ),
        )
        candidate_digest = self._persisted_digest(
            row["candidate_digest"],
            field="candidate_digest",
            required=False,
        )
        policy_digest = self._persisted_digest(
            row["policy_digest"],
            field="policy_digest",
            required=False,
        )
        resolution_digest = self._persisted_digest(
            row["resolution_evidence_digest"],
            field="resolution_evidence_digest",
            required=state == "resolved",
        )
        revision = self._persisted_revision(row["revision"])
        created = _parse_timestamp(row["created_at"], field="created_at")
        updated = _parse_timestamp(row["updated_at"], field="updated_at")
        expires = _parse_timestamp(row["expires_at"], field="expires_at")
        resolved = (
            _parse_timestamp(row["resolved_at"], field="resolved_at")
            if row["resolved_at"] is not None
            else None
        )
        if updated < created or expires <= created:
            raise ContractDataError("decision timestamp ordering is invalid")
        if resolved is not None and (resolved < created or resolved > updated):
            raise ContractDataError("decision timestamp ordering is invalid")
        choice = row["choice"]
        if state == "resolved":
            if choice not in choices or resolution_digest is None or resolved is None:
                raise ContractDataError("resolved decision evidence is incomplete")
            if revision < 2:
                raise ContractDataError("resolved decision revision is invalid")
        elif choice is not None or resolution_digest is not None or resolved is not None:
            raise ContractDataError("unresolved decision contains resolution evidence")
        elif state != "pending" and revision < 2:
            raise ContractDataError("closed decision revision is invalid")
        if connection is not None:
            execution = self._execution_row(connection, execution_id)
            if execution is None:
                raise ContractDataError("decision execution is missing")
            self._assert_row_authority(execution, require_executor=True)
            self._execution_from_row(execution)
            if (
                execution["effect_id"] != effect_id
                or execution["proposal_ref"] != proposal_ref
            ):
                raise ContractDataError("decision execution binding is invalid")
            if state == "pending" and execution["lifecycle"] != "awaiting_decision":
                raise ContractDataError("pending decision execution state is invalid")
        return {
            "contract_version": CONTRACT_VERSION,
            "decision_id": decision_id,
            "execution_id": execution_id,
            "profile_id": row["profile_id"],
            "authority_id": row["authority_id"],
            "effect_id": effect_id,
            "proposal_ref": proposal_ref,
            "request_digest": request_digest,
            "candidate_digest": candidate_digest,
            "policy_digest": policy_digest,
            "allowed_choices": choices,
            "expires_at": row["expires_at"],
            "state": state,
            "choice": choice,
            "resolution_evidence_digest": resolution_digest,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"],
            "revision": revision,
        }

    def _receipt_from_row(
        self,
        row: sqlite3.Row,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        self._assert_row_authority(row, require_executor=True)
        receipt_id = cast(str, self._persisted_identifier(row["receipt_id"], "rcp"))
        execution_id = cast(str, self._persisted_identifier(row["execution_id"], "exe"))
        outcome = str(row["outcome"])
        if outcome not in RECEIPT_OUTCOMES:
            raise ContractDataError("receipt row contains an unknown outcome")
        effect_id = cast(
            str,
            self._persisted_ref(row["effect_id"], field="effect_id", required=True),
        )
        subject_digest = cast(
            str,
            self._persisted_digest(
                row["subject_digest"],
                field="subject_digest",
                required=True,
            ),
        )
        evidence_digest = cast(
            str,
            self._persisted_digest(
                row["evidence_digest"],
                field="evidence_digest",
                required=True,
            ),
        )
        result_digest = cast(
            str,
            self._persisted_digest(
                row["result_digest"],
                field="result_digest",
                required=True,
            ),
        )
        decision_id = self._persisted_identifier(
            row["decision_id"],
            "dec",
            nullable=True,
        )
        recovery_ref = self._persisted_ref(row["recovery_ref"], field="recovery_ref")
        reconciliation_ref = self._persisted_ref(
            row["reconciliation_ref"],
            field="reconciliation_ref",
        )
        terminal = _parse_timestamp(row["terminal_at"], field="terminal_at")
        revision = self._persisted_revision(row["revision"])
        if revision != 1:
            raise ContractDataError("receipt revision must be immutable at one")
        if connection is not None:
            execution = self._execution_row(connection, execution_id)
            if execution is None:
                raise ContractDataError("receipt execution is missing")
            self._assert_row_authority(execution, require_executor=True)
            self._execution_from_row(execution)
            expected_lifecycle = f"terminal_{outcome}"
            if (
                execution["effect_id"] != effect_id
                or execution["receipt_id"] != receipt_id
                or execution["receipt_state"] != "published"
                or execution["lifecycle"] != expected_lifecycle
            ):
                raise ContractDataError("receipt execution invariant failed")
            execution_terminal = _parse_timestamp(
                execution["terminal_at"],
                field="execution_terminal_at",
            )
            evidence = connection.execute(
                "SELECT * FROM effect_evidence WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if evidence is None:
                raise ContractDataError("receipt effect evidence is missing")
            self._evidence_from_row(evidence, connection=connection)
            evidence_recorded = _parse_timestamp(
                evidence["recorded_at"],
                field="evidence_recorded_at",
            )
            if evidence_recorded > execution_terminal:
                if (
                    reconciliation_ref is None
                    or outcome != "ambiguous"
                    or execution["lifecycle"] != "terminal_ambiguous"
                    or terminal != evidence_recorded
                ):
                    raise ContractDataError(
                        "reconciled receipt terminal timestamp must equal evidence recorded timestamp"
                    )
            elif terminal != execution_terminal:
                raise ContractDataError(
                    "receipt terminal timestamp must equal execution terminal timestamp"
                )
            for field, expected in (
                ("effect_id", effect_id),
                ("outcome", outcome),
                ("subject_digest", subject_digest),
                ("evidence_digest", evidence_digest),
                ("result_digest", result_digest),
                ("decision_id", decision_id),
                ("recovery_ref", recovery_ref),
                ("reconciliation_ref", reconciliation_ref),
            ):
                if evidence[field] != expected:
                    raise ContractDataError("receipt evidence binding is invalid")
            if decision_id is not None:
                decision = connection.execute(
                    "SELECT * FROM decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                if decision is None or (
                    decision["execution_id"] != execution_id
                    or decision["effect_id"] != effect_id
                    or decision["state"] != "resolved"
                ):
                    raise ContractDataError("receipt decision binding is invalid")
        return {
            "contract_version": CONTRACT_VERSION,
            "receipt_id": receipt_id,
            "execution_id": execution_id,
            "effect_id": effect_id,
            "profile_id": row["profile_id"],
            "authority_id": row["authority_id"],
            "executor_id": row["executor_id"],
            "outcome": outcome,
            "subject_digest": subject_digest,
            "evidence_digest": evidence_digest,
            "result_digest": result_digest,
            "terminal_at": row["terminal_at"],
            "decision_id": decision_id,
            "recovery_ref": recovery_ref,
            "reconciliation_ref": reconciliation_ref,
            "revision": revision,
        }

    def _event_from_row(
        self,
        row: sqlite3.Row,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        self._assert_row_authority(row, require_executor=False)
        event_id = cast(str, self._persisted_identifier(row["event_id"], "evt"))
        execution_id = cast(str, self._persisted_identifier(row["execution_id"], "exe"))
        event_type = str(row["event_type"])
        if event_type not in EVENT_TYPES:
            raise ContractDataError("event row contains an unknown type")
        try:
            sequence = int(row["sequence"])
        except (TypeError, ValueError) as exc:
            raise ContractDataError("event sequence is invalid") from exc
        if sequence < 1:
            raise ContractDataError("event sequence is invalid")
        from_lifecycle = row["from_lifecycle"]
        to_lifecycle = row["to_lifecycle"]
        if from_lifecycle is not None and from_lifecycle not in EXECUTION_STATES:
            raise ContractDataError("event from lifecycle is invalid")
        if to_lifecycle is not None and to_lifecycle not in EXECUTION_STATES:
            raise ContractDataError("event to lifecycle is invalid")
        decision_id = self._persisted_identifier(
            row["decision_id"],
            "dec",
            nullable=True,
        )
        receipt_id = self._persisted_identifier(
            row["receipt_id"],
            "rcp",
            nullable=True,
        )
        reason_code = self._persisted_ref(
            row["reason_code"],
            field="reason_code",
            max_length=128,
        )
        _parse_timestamp(row["occurred_at"], field="occurred_at")
        revision = self._persisted_revision(row["revision"])
        if connection is not None:
            execution = self._execution_row(connection, execution_id)
            if execution is None:
                raise ContractDataError("event execution is missing")
            self._assert_row_authority(execution, require_executor=True)
            self._execution_from_row(execution)
            if decision_id is not None:
                decision = connection.execute(
                    "SELECT * FROM decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                if decision is None or decision["execution_id"] != execution_id:
                    raise ContractDataError("event decision binding is invalid")
                self._decision_from_row(decision)
            if receipt_id is not None:
                receipt = connection.execute(
                    "SELECT * FROM receipts WHERE receipt_id=?",
                    (receipt_id,),
                ).fetchone()
                if receipt is None or receipt["execution_id"] != execution_id:
                    raise ContractDataError("event receipt binding is invalid")
                self._receipt_from_row(receipt, connection=connection)
        return {
            "contract_version": CONTRACT_VERSION,
            "sequence": sequence,
            "event_id": event_id,
            "execution_id": execution_id,
            "profile_id": row["profile_id"],
            "authority_id": row["authority_id"],
            "event_type": event_type,
            "from_lifecycle": from_lifecycle,
            "to_lifecycle": to_lifecycle,
            "decision_id": decision_id,
            "receipt_id": receipt_id,
            "reason_code": reason_code,
            "occurred_at": row["occurred_at"],
            "revision": revision,
        }

    def _evidence_from_row(
        self,
        row: sqlite3.Row,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        self._assert_row_authority(row, require_executor=True)
        evidence_match = _EVIDENCE_ID_RE.fullmatch(str(row["evidence_id"] or ""))
        if (
            evidence_match is None
            or evidence_match.group(1) != self.authority.profile_key
        ):
            raise ContractDataError("persisted evidence identifier is invalid")
        execution_id = cast(str, self._persisted_identifier(row["execution_id"], "exe"))
        effect_id = cast(
            str,
            self._persisted_ref(row["effect_id"], field="effect_id", required=True),
        )
        outcome = str(row["outcome"])
        if outcome not in RECEIPT_OUTCOMES:
            raise ContractDataError("effect evidence outcome is invalid")
        digests = {
            field: cast(
                str,
                self._persisted_digest(row[field], field=field, required=True),
            )
            for field in ("subject_digest", "evidence_digest", "result_digest")
        }
        decision_id = self._persisted_identifier(
            row["decision_id"],
            "dec",
            nullable=True,
        )
        recovery_ref = self._persisted_ref(row["recovery_ref"], field="recovery_ref")
        reconciliation_ref = self._persisted_ref(
            row["reconciliation_ref"],
            field="reconciliation_ref",
        )
        _parse_timestamp(row["recorded_at"], field="recorded_at")
        if connection is not None:
            execution = self._execution_row(connection, execution_id)
            if execution is None or execution["effect_id"] != effect_id:
                raise ContractDataError("effect evidence execution binding is invalid")
            latest_decision = connection.execute(
                "SELECT * FROM decisions WHERE execution_id=? "
                "ORDER BY ordinal DESC LIMIT 1",
                (execution_id,),
            ).fetchone()
            if latest_decision is None:
                if decision_id is not None:
                    raise ContractDataError("effect evidence decision is unbound")
            elif (
                latest_decision["decision_id"] != decision_id
                or latest_decision["state"] != "resolved"
                or latest_decision["effect_id"] != effect_id
            ):
                raise ContractDataError("effect evidence decision binding is invalid")
        return {
            "evidence_id": row["evidence_id"],
            "execution_id": execution_id,
            "effect_id": effect_id,
            "outcome": outcome,
            "subject_digest": digests["subject_digest"],
            "evidence_digest": digests["evidence_digest"],
            "result_digest": digests["result_digest"],
            "decision_id": decision_id,
            "recovery_ref": recovery_ref,
            "reconciliation_ref": reconciliation_ref,
            "recorded_at": row["recorded_at"],
        }

    def _rows_page(
        self,
        rows: list[sqlite3.Row],
        *,
        after: int,
        limit: int,
        decoder,
        connection: sqlite3.Connection,
        snapshot_high_water: int,
    ) -> dict[str, Any]:
        visible = rows[:limit]
        has_more = len(rows) > limit
        cursor = (
            int(visible[-1]["ordinal"])
            if has_more and visible
            else snapshot_high_water
        )
        return self._collection_page(
            [decoder(row, connection=connection) for row in visible],
            cursor,
            snapshot_high_water,
            1 if snapshot_high_water else 0,
            has_more,
            snapshot_high_water,
        )

    def _pin_collection_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        after: int,
        requested: Optional[int],
    ) -> int:
        available = int(
            connection.execute(
                f"SELECT COALESCE(MAX(ordinal), 0) FROM {table}"
            ).fetchone()[0]
        )
        pinned = available if requested is None else requested
        if pinned > available:
            raise ContractConflictError(f"{table} snapshot is ahead of high-water")
        if after > pinned:
            raise ContractConflictError(f"{table} cursor is ahead of snapshot")
        self._after_snapshot_pinned(table, pinned)
        return pinned

    def _after_snapshot_pinned(self, _collection: str, _high_water: int) -> None:
        """Test seam called inside the pinned SQLite read transaction."""

    def _after_write_lock_acquired(self, _operation: str, _execution_id: str) -> None:
        """Test seam called only after BEGIN IMMEDIATE owns the writer lock."""

    @staticmethod
    def _validate_global_event_continuity(
        connection: sqlite3.Connection,
        *,
        pruned_through: int,
        snapshot_high_water: int,
    ) -> None:
        """Prove the complete unfiltered global interval for this snapshot."""

        if snapshot_high_water < pruned_through:
            raise ContractDataError("event snapshot predates the retention watermark")
        expected_count = snapshot_high_water - pruned_through
        row = connection.execute(
            "SELECT COUNT(*), MIN(sequence), MAX(sequence) "
            "FROM execution_events WHERE sequence > ? AND sequence <= ?",
            (pruned_through, snapshot_high_water),
        ).fetchone()
        count = int(row[0])
        minimum = int(row[1]) if row[1] is not None else None
        maximum = int(row[2]) if row[2] is not None else None
        if expected_count == 0:
            if count != 0 or minimum is not None or maximum is not None:
                raise ContractDataError("global event sequence continuity failed")
            return
        if (
            count != expected_count
            or minimum != pruned_through + 1
            or maximum != snapshot_high_water
        ):
            raise ContractDataError("global event sequence gap detected")

    @staticmethod
    def _collection_page(
        items: list[dict[str, Any]],
        cursor: int,
        high_water: int,
        minimum_available: int,
        has_more: bool,
        snapshot_high_water: int,
    ) -> dict[str, Any]:
        return {
            "items": items,
            "page": {
                "cursor": cursor,
                "high_water": high_water,
                "snapshot_high_water": snapshot_high_water,
                "minimum_available": minimum_available,
                "has_more": has_more,
                "completeness": "complete",
            },
        }

    @staticmethod
    def _event_page(
        events: list[dict[str, Any]],
        cursor: int,
        high_water: int,
        minimum_available: int,
        has_more: bool,
        pruned_through: int,
        snapshot_high_water: int,
    ) -> dict[str, Any]:
        return {
            "items": events,
            "page": {
                "cursor": cursor,
                "high_water": high_water,
                "snapshot_high_water": snapshot_high_water,
                "minimum_available": minimum_available,
                "has_more": has_more,
                "completeness": "complete",
                "pruned_through": pruned_through,
                "retention_seconds": EVENT_RETENTION_SECONDS,
            },
        }


def contract_capabilities(
    profile_home: Path,
    profile_name: Optional[str] = None,
) -> dict[str, Any]:
    authority = authority_identity(profile_home, profile_name)
    return {
        "contract_version": CONTRACT_VERSION,
        "api_version": API_VERSION,
        "authority": authority.public(),
        "read_scope": "execution:read",
        "mutation_scopes": [
            "execution:start",
            "decision:resolve",
            "execution:steer",
            "execution:stop",
        ],
        "event_retention_seconds": EVENT_RETENTION_SECONDS,
        "max_page_size": MAX_PAGE_SIZE,
        "features": {
            "durable_executions": True,
            "durable_pending_decisions": True,
            "ordered_restart_safe_events": True,
            "evidence_gated_terminal_receipts": True,
            "transactional_event_outbox": True,
            "profile_scoped_authority": True,
            "webauthn_decisions": False,
            "action_dispatch": False,
        },
    }


def contract_schema() -> dict[str, Any]:
    """Load the packaged closed JSON Schema for capability negotiation."""

    resource = importlib.resources.files("hermes_cli.execution_contract_schemas").joinpath(
        "hermes.execution.read.v1.schema.json"
    )
    with resource.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ContractDataError("packaged execution contract schema is malformed")
    return value
