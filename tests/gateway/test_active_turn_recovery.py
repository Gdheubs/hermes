"""Regression tests for exact durable active-turn restart recovery.

A long-running gateway turn can outlive the legacy 120-second
``updated_at`` crash heuristic.  These tests require an exact persisted
marker, compare-and-swap cleanup, and promotion into the existing
``resume_pending`` recovery path after an unclean exit.
"""

import asyncio
import errno
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, SessionStore


ACTIVE_TURN_MAX_AGE_SECONDS = 60 * 60


def _make_source(chat_id: str = "active-turn-chat") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        user_id="user-1",
        chat_type="channel",
        thread_id="thread-1",
    )


def _make_store(tmp_path) -> SessionStore:
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    # Exercise the legacy JSON fallback deterministically.  ``_save_entry``
    # must still persist correctly when state.db is unavailable.
    store._db = None
    store._db_exact_authority_marker_path().unlink(missing_ok=True)
    return store


def _make_db_store(tmp_path) -> SessionStore:
    from hermes_state import SessionDB

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    if store._db is not None:
        store._db.close()
    store._db = SessionDB(db_path=tmp_path / "state.db")
    return store


def _close_store_db(store: SessionStore) -> None:
    db = store._db
    assert db is not None
    db.close()


def _entry_for(store: SessionStore, source: SessionSource) -> SessionEntry:
    key = store._generate_session_key(source)
    with store._lock:
        store._ensure_loaded_locked()
        return store._entries[key]


def test_active_turn_fields_round_trip_and_legacy_payload_defaults(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)

    token = store.mark_turn_active(entry.session_key)
    assert token

    payload = _entry_for(store, source).to_dict()
    assert payload["active_turn_token"] == token
    assert payload["active_turn_started_at"] is not None

    restored = SessionEntry.from_dict(payload)
    assert restored.active_turn_token == token
    assert restored.active_turn_started_at is not None

    payload.pop("active_turn_token")
    payload.pop("active_turn_started_at")
    legacy = SessionEntry.from_dict(payload)
    assert legacy.active_turn_token is None
    assert legacy.active_turn_started_at is None

    payload["active_turn_token"] = {"invalid": "not-a-token"}
    payload["active_turn_started_at"] = datetime.now().isoformat()
    corrupt = SessionEntry.from_dict(payload)
    assert corrupt.active_turn_token is None
    assert corrupt.active_turn_started_at is None


def test_mark_refreshes_updated_at_for_legacy_upgrade_fallback(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    old_updated_at = datetime.now() - timedelta(hours=2)
    with store._lock:
        store._entries[entry.session_key].updated_at = old_updated_at

    token = store.mark_turn_active(entry.session_key)

    assert token is not None
    assert _entry_for(store, source).updated_at > old_updated_at


def test_active_turn_clear_is_compare_and_swap(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)

    first = store.mark_turn_active(entry.session_key)
    second = store.mark_turn_active(entry.session_key)
    assert first is not None
    assert second is not None
    assert first != second

    assert store.clear_turn_active(entry.session_key, first) is False
    assert _entry_for(store, source).active_turn_token == second

    assert store.clear_turn_active(entry.session_key, second) is True
    current = _entry_for(store, source)
    assert current.active_turn_token is None
    assert current.active_turn_started_at is None


def test_mark_and_clear_use_single_entry_persistence(tmp_path):
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    real_save_entry = store._save_entry
    store._save_entry = MagicMock(wraps=real_save_entry)

    token = store.mark_turn_active(entry.session_key)
    assert token is not None
    store._save_entry.assert_called_once_with(
        entry.session_key,
        entry_data=store._entries[entry.session_key].to_dict(),
        lock_held=True,
        require_authoritative=False,
    )

    store._save_entry.reset_mock()
    assert store.clear_turn_active(entry.session_key, token) is True
    store._save_entry.assert_called_once_with(
        entry.session_key,
        entry_data=store._entries[entry.session_key].to_dict(),
        lock_held=True,
        require_authoritative=False,
    )


def test_failed_mark_persistence_does_not_leak_marker_into_later_save(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    real_save_entry = store._save_entry
    store._save_entry = MagicMock(side_effect=OSError("disk unavailable"))

    with pytest.raises(OSError, match="disk unavailable"):
        store.mark_turn_active(entry.session_key)

    current = _entry_for(store, source)
    assert current.active_turn_token is None
    assert current.active_turn_started_at is None

    # A later unrelated save must not make the failed marker durable.
    store._save_entry = real_save_entry
    with store._lock:
        store._entries[entry.session_key].updated_at = datetime.now()
        store._save()

    reloaded = _make_store(tmp_path)
    assert reloaded.recover_interrupted_turns() == 0
    assert _entry_for(reloaded, source).active_turn_token is None


def test_failed_clear_persistence_keeps_token_retryable_and_durable_clear_wins(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)
    assert token is not None

    real_save_entry = store._save_entry
    store._save_entry = MagicMock(side_effect=OSError("disk unavailable"))

    with pytest.raises(OSError, match="disk unavailable"):
        store.clear_turn_active(entry.session_key, token)

    current = _entry_for(store, source)
    assert current.active_turn_token == token
    assert current.active_turn_started_at is not None

    store._save_entry = real_save_entry
    assert store.clear_turn_active(entry.session_key, token) is True

    reloaded = _make_store(tmp_path)
    persisted = _entry_for(reloaded, source)
    assert persisted.active_turn_token is None
    assert persisted.active_turn_started_at is None


def test_state_db_failure_atomic_marker_round_trip(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source("state-db-active-turn")
    entry = store.get_or_create_session(source)
    db = store._db
    assert db is not None
    db.save_gateway_routing_entry = MagicMock(
        side_effect=OSError("fast state.db write blocked")
    )
    db.replace_gateway_routing_entries = MagicMock(
        side_effect=OSError("full state.db write blocked")
    )

    with pytest.raises(
        RuntimeError, match="authoritative state.db routing save failed"
    ):
        store.mark_turn_active(entry.session_key)
    assert _entry_for(store, source).active_turn_token is None
    _close_store_db(store)

    json_only = _make_store(tmp_path / "sessions")
    assert _entry_for(json_only, source).active_turn_token is None

    reloaded = _make_db_store(tmp_path)
    reloaded_entry = _entry_for(reloaded, source)
    assert reloaded_entry.active_turn_token is None
    token = reloaded.mark_turn_active(reloaded_entry.session_key)
    assert token is not None

    db = reloaded._db
    assert db is not None
    db.save_gateway_routing_entry = MagicMock(
        side_effect=OSError("fast state.db write blocked")
    )
    db.replace_gateway_routing_entries = MagicMock(
        side_effect=OSError("full state.db write blocked")
    )
    with pytest.raises(
        RuntimeError, match="authoritative state.db routing save failed"
    ):
        reloaded.clear_turn_active(reloaded_entry.session_key, token)
    assert _entry_for(reloaded, source).active_turn_token == token
    _close_store_db(reloaded)

    final = _make_db_store(tmp_path)
    persisted = _entry_for(final, source)
    assert persisted.active_turn_token == token
    assert final.clear_turn_active(persisted.session_key, token) is True
    _close_store_db(final)


def test_state_db_commit_survives_legacy_mirror_failure(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source("state-db-mirror-failure")
    entry = store.get_or_create_session(source)
    db = store._db
    assert db is not None
    db.save_gateway_routing_entry = MagicMock(
        side_effect=OSError("fast upsert unavailable")
    )
    store._save_sessions_json = MagicMock(
        side_effect=OSError("legacy mirror unavailable")
    )

    token = store.mark_turn_active(entry.session_key)
    assert token is not None
    _close_store_db(store)

    reloaded = _make_db_store(tmp_path)
    recovered = _entry_for(reloaded, source)
    assert recovered.active_turn_token == token
    _close_store_db(reloaded)


def test_state_db_mirror_never_replays_stale_active_marker_on_json_fallback(
    tmp_path,
):
    store = _make_db_store(tmp_path)
    source = _make_source("stale-db-mirror")
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)
    assert token

    # A structural save can capture the active marker in the legacy mirror.
    # The later exact clear takes the state.db fast path, so the mirror is now
    # deliberately stale but explicitly tagged as non-authoritative for exact
    # recovery state.
    with store._lock:
        store._save()
    assert store.clear_turn_active(entry.session_key, token) is True
    _close_store_db(store)

    fallback = SessionStore(
        sessions_dir=tmp_path / "sessions",
        config=GatewayConfig(),
    )
    fallback._db = None
    fallback_entry = _entry_for(fallback, source)
    assert fallback_entry.active_turn_token is None
    assert fallback.recover_interrupted_turns(max_age_seconds=3600) == 0


def test_exact_file_publication_fsyncs_parent_directory(tmp_path):
    json_store = _make_store(tmp_path / "json-sessions")
    json_entry = json_store.get_or_create_session(_make_source("json-dir-fsync"))
    json_store._fsync_sessions_dir = MagicMock()
    assert json_store.mark_turn_active(json_entry.session_key)
    json_store._fsync_sessions_dir.assert_called_once_with()

    db_store = _make_db_store(tmp_path / "db-case")
    db_entry = db_store.get_or_create_session(_make_source("sidecar-dir-fsync"))
    db_store._db_exact_authority_marker_path().unlink(missing_ok=True)
    db_store._fsync_sessions_dir = MagicMock()
    assert db_store.mark_turn_active(db_entry.session_key)
    db_store._fsync_sessions_dir.assert_called_once_with()
    _close_store_db(db_store)


def test_exact_file_publication_rejects_non_atomic_replace_fallback(tmp_path):
    store = _make_store(tmp_path / "json-sessions")
    source = _make_source("strict-publication")
    entry = store.get_or_create_session(source)

    with patch(
        "gateway.session.os.replace",
        side_effect=OSError(errno.EXDEV, "cross-device rename"),
    ):
        with pytest.raises(OSError) as exc_info:
            store.mark_turn_active(entry.session_key)

    assert exc_info.value.errno == errno.EXDEV
    assert _entry_for(store, source).active_turn_token is None


def test_authority_sidecar_rejects_non_atomic_replace_fallback(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source("strict-sidecar-publication")
    entry = store.get_or_create_session(source)
    store._db_exact_authority_marker_path().unlink(missing_ok=True)
    db = store._db
    assert db is not None
    db.save_gateway_routing_entry = MagicMock()

    with patch(
        "gateway.session.os.replace",
        side_effect=OSError(errno.EBUSY, "busy mount"),
    ):
        with pytest.raises(OSError) as exc_info:
            store.mark_turn_active(entry.session_key)

    assert exc_info.value.errno == errno.EBUSY
    db.save_gateway_routing_entry.assert_not_called()
    assert _entry_for(store, source).active_turn_token is None
    _close_store_db(store)


def test_exact_file_publication_rejects_symlink_target(tmp_path):
    sessions_dir = tmp_path / "sessions"
    store = _make_store(sessions_dir)
    source = _make_source("strict-symlink-publication")
    entry = store.get_or_create_session(source)
    external = tmp_path / "external.json"
    external.write_text("external remains unchanged", encoding="utf-8")
    sessions_file = sessions_dir / "sessions.json"
    sessions_file.unlink()
    sessions_file.symlink_to(external)

    with pytest.raises(OSError, match="refusing symlink target"):
        store.mark_turn_active(entry.session_key)

    assert external.read_text(encoding="utf-8") == "external remains unchanged"
    assert _entry_for(store, source).active_turn_token is None


def test_existing_sidecar_symlink_cannot_authorize_exact_db_transition(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source("symlink-sidecar")
    entry = store.get_or_create_session(source)
    marker = store._db_exact_authority_marker_path()
    marker.unlink(missing_ok=True)
    external = tmp_path / "external-authority-marker"
    external.write_text("state.db\n", encoding="utf-8")
    marker.symlink_to(external)
    db = store._db
    assert db is not None
    db.save_gateway_routing_entry = MagicMock()

    with pytest.raises(OSError, match="refusing unsafe exact-authority marker"):
        store.mark_turn_active(entry.session_key)

    db.save_gateway_routing_entry.assert_not_called()
    assert _entry_for(store, source).active_turn_token is None
    assert marker.is_symlink()
    _close_store_db(store)


def test_dangling_sidecar_symlink_keeps_json_exact_state_untrusted(tmp_path):
    sessions_dir = tmp_path / "sessions"
    json_store = _make_store(sessions_dir)
    source = _make_source("dangling-sidecar")
    entry = json_store.get_or_create_session(source)
    assert json_store.mark_turn_active(entry.session_key)

    marker = json_store._db_exact_authority_marker_path()
    external = tmp_path / "removed-authority-marker"
    external.write_text("state.db\n", encoding="utf-8")
    marker.symlink_to(external)
    external.unlink()
    assert marker.is_symlink() and not marker.exists()

    fallback = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    fallback._db = None
    assert _entry_for(fallback, source).active_turn_token is None
    assert fallback.recover_interrupted_turns(max_age_seconds=3600) == 0


def test_sidecar_directory_fsync_failure_blocks_then_retries_exact_transition(
    tmp_path,
):
    store = _make_db_store(tmp_path)
    source = _make_source("authority-dir-fsync-retry")
    entry = store.get_or_create_session(source)
    store._db_exact_authority_marker_path().unlink(missing_ok=True)
    store._fsync_sessions_dir = MagicMock(
        side_effect=[OSError("directory fsync failed"), None]
    )
    db = store._db
    assert db is not None
    real_save = db.save_gateway_routing_entry
    db.save_gateway_routing_entry = MagicMock(wraps=real_save)

    with pytest.raises(OSError, match="directory fsync failed"):
        store.mark_turn_active(entry.session_key)
    db.save_gateway_routing_entry.assert_not_called()
    assert _entry_for(store, source).active_turn_token is None

    token = store.mark_turn_active(entry.session_key)
    assert token
    db.save_gateway_routing_entry.assert_called_once()
    assert store._fsync_sessions_dir.call_count == 2
    _close_store_db(store)


def test_authority_sidecar_failure_prevents_exact_db_transition(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source("authority-sidecar-failure")
    entry = store.get_or_create_session(source)
    store._db_exact_authority_marker_path().unlink(missing_ok=True)
    db = store._db
    assert db is not None
    db.save_gateway_routing_entry = MagicMock()
    store._ensure_db_exact_authority_marker = MagicMock(
        side_effect=OSError("sidecar unavailable")
    )

    with pytest.raises(OSError, match="sidecar unavailable"):
        store.mark_turn_active(entry.session_key)

    db.save_gateway_routing_entry.assert_not_called()
    assert _entry_for(store, source).active_turn_token is None
    _close_store_db(store)


def test_db_load_failure_cannot_prune_or_relabel_json_authority(tmp_path):
    sessions_dir = tmp_path / "sessions"
    json_store = _make_store(sessions_dir)
    source = _make_source("json-db-load-failure")
    entry = json_store.get_or_create_session(source)
    token = json_store.mark_turn_active(entry.session_key)
    assert token

    db_store = _make_db_store(tmp_path)
    db = db_store._db
    assert db is not None
    db.load_gateway_routing_entries = MagicMock(
        side_effect=OSError("DB routing load failed")
    )
    db.get_session = MagicMock(return_value={"end_reason": "completed"})
    db.replace_gateway_routing_entries = MagicMock(
        side_effect=OSError("DB replacement failed")
    )

    loaded = _entry_for(db_store, source)
    assert loaded.active_turn_token == token
    assert db_store._loaded is True
    db.get_session.assert_not_called()
    db.replace_gateway_routing_entries.assert_not_called()
    assert not db_store._db_exact_authority_marker_path().exists()

    # Even a later ordinary save must preserve JSON authority if the DB still
    # cannot accept the snapshot.
    with db_store._lock:
        db_store._save()
    db.replace_gateway_routing_entries.assert_called_once()
    persisted = json.loads((sessions_dir / "sessions.json").read_text())
    assert (
        persisted[SessionStore._JSON_EXACT_AUTHORITY_KEY]
        == SessionStore._JSON_EXACT_AUTHORITY_JSON
    )
    assert persisted[entry.session_key]["active_turn_token"] == token

    # A later exact transition remains on strict JSON authority instead of
    # publishing a DB sidecar before a DB write that may fail.
    db.save_gateway_routing_entry = MagicMock(
        side_effect=OSError("DB upsert failed")
    )
    replacement_token = db_store.mark_turn_active(entry.session_key)
    assert replacement_token and replacement_token != token
    db.save_gateway_routing_entry.assert_not_called()
    assert not db_store._db_exact_authority_marker_path().exists()
    _close_store_db(db_store)

    fallback = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    fallback._db = None
    assert _entry_for(fallback, source).active_turn_token == replacement_token
    assert fallback.recover_interrupted_turns(max_age_seconds=3600) == 1


def test_json_authoritative_final_clear_commits_before_any_db_handoff(tmp_path):
    sessions_dir = tmp_path / "sessions"
    json_store = _make_store(sessions_dir)
    source = _make_source("json-final-clear")
    entry = json_store.get_or_create_session(source)
    token = json_store.mark_turn_active(entry.session_key)
    assert token

    db_store = _make_db_store(tmp_path)
    loaded = _entry_for(db_store, source)
    assert loaded.active_turn_token == token
    assert db_store._json_exact_state_is_authoritative is True
    db = db_store._db
    assert db is not None
    db.save_gateway_routing_entry = MagicMock(
        side_effect=OSError("DB upsert failed")
    )
    db.replace_gateway_routing_entries = MagicMock(
        side_effect=OSError("DB replacement failed")
    )

    assert db_store.clear_turn_active(entry.session_key, token) is True
    db.save_gateway_routing_entry.assert_not_called()
    db.replace_gateway_routing_entries.assert_not_called()
    assert not db_store._db_exact_authority_marker_path().exists()
    _close_store_db(db_store)

    fallback = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    fallback._db = None
    assert _entry_for(fallback, source).active_turn_token is None
    assert fallback.recover_interrupted_turns(max_age_seconds=3600) == 0


def test_db_adoption_failure_preserves_json_exact_authority(tmp_path):
    sessions_dir = tmp_path / "sessions"
    json_store = _make_store(sessions_dir)
    source = _make_source("json-to-db-failure")
    entry = json_store.get_or_create_session(source)
    token = json_store.mark_turn_active(entry.session_key)
    assert token

    db_store = _make_db_store(tmp_path)
    db = db_store._db
    assert db is not None
    db.replace_gateway_routing_entries = MagicMock(
        side_effect=OSError("DB adoption failed")
    )
    loaded = _entry_for(db_store, source)
    assert loaded.active_turn_token == token
    db.replace_gateway_routing_entries.assert_not_called()
    assert db_store._json_exact_state_is_authoritative is True
    assert not db_store._db_exact_authority_marker_path().exists()
    _close_store_db(db_store)

    fallback = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    fallback._db = None
    recovered = _entry_for(fallback, source)
    assert recovered.active_turn_token == token
    assert fallback.recover_interrupted_turns(max_age_seconds=3600) == 1


def test_db_adoption_failure_cannot_prune_json_authority_first(tmp_path):
    sessions_dir = tmp_path / "sessions"
    json_store = _make_store(sessions_dir)
    source = _make_source("json-to-db-stale-prune-failure")
    entry = json_store.get_or_create_session(source)
    token = json_store.mark_turn_active(entry.session_key)
    assert token

    db_store = _make_db_store(tmp_path)
    db = db_store._db
    assert db is not None
    db.get_session = MagicMock(return_value={"end_reason": "completed"})
    db.replace_gateway_routing_entries = MagicMock(
        side_effect=OSError("DB adoption failed before prune")
    )
    loaded = _entry_for(db_store, source)
    assert loaded.active_turn_token == token
    db.replace_gateway_routing_entries.assert_not_called()
    db.get_session.assert_not_called()
    assert db_store._loaded is True
    assert db_store._json_exact_state_is_authoritative is True
    assert not db_store._db_exact_authority_marker_path().exists()
    _close_store_db(db_store)

    fallback = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    fallback._db = None
    recovered = _entry_for(fallback, source)
    assert recovered.active_turn_token == token
    assert fallback.recover_interrupted_turns(max_age_seconds=3600) == 1


def test_db_adoption_sidecar_failure_retries_same_process(tmp_path):
    sessions_dir = tmp_path / "sessions"
    json_store = _make_store(sessions_dir)
    source = _make_source("json-to-db-sidecar-retry")
    entry = json_store.get_or_create_session(source)

    db_store = _make_db_store(tmp_path)
    real_ensure = db_store._ensure_db_exact_authority_marker
    attempts = 0

    def flaky_ensure():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("sidecar unavailable")
        real_ensure()

    db_store._ensure_db_exact_authority_marker = flaky_ensure
    with pytest.raises(OSError, match="sidecar unavailable"):
        _entry_for(db_store, source)
    assert db_store._loaded is False
    assert not db_store._db_exact_authority_marker_path().exists()

    migrated = _entry_for(db_store, source)
    assert migrated.session_id == entry.session_id
    assert migrated.active_turn_token is None
    assert attempts == 2
    assert db_store._loaded is True
    assert db_store._db_exact_authority_marker_path().exists()
    _close_store_db(db_store)


def test_db_adoption_sidecar_invalidates_preexisting_json_exact_marker(tmp_path):
    sessions_dir = tmp_path / "sessions"
    json_store = _make_store(sessions_dir)
    source = _make_source("json-to-db-migration")
    entry = json_store.get_or_create_session(source)
    token = json_store.mark_turn_active(entry.session_key)
    assert token

    db_store = _make_db_store(tmp_path)
    migrated = _entry_for(db_store, source)
    assert migrated.active_turn_token == token
    assert db_store.clear_turn_active(migrated.session_key, token) is True
    _close_store_db(db_store)

    fallback = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    fallback._db = None
    recovered = _entry_for(fallback, source)
    assert recovered.active_turn_token is None
    assert fallback.recover_interrupted_turns(max_age_seconds=3600) == 0


def test_exact_old_active_turn_recovers_even_when_updated_at_is_stale(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)

    with store._lock:
        current = store._entries[entry.session_key]
        current.updated_at = datetime.now() - timedelta(hours=2)
        current.active_turn_started_at = datetime.now() - timedelta(minutes=10)
        store._save()

    # Prove the marker survives a fresh SessionStore and is not relying on the
    # in-memory object that wrote it.
    reloaded = _make_store(tmp_path)
    assert reloaded.recover_interrupted_turns(
        max_age_seconds=ACTIVE_TURN_MAX_AGE_SECONDS
    ) == 1

    recovered = _entry_for(reloaded, source)
    assert recovered.resume_pending is True
    assert recovered.resume_reason == "active_turn_interrupted"
    assert recovered.last_resume_marked_at is not None
    assert recovered.last_resume_marked_at > datetime.now() - timedelta(seconds=5)
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None
    assert token


def test_suspended_active_turn_is_cleared_without_resume(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)

    with store._lock:
        store._entries[entry.session_key].suspended = True

    assert store.recover_interrupted_turns() == 0
    recovered = _entry_for(store, source)
    assert recovered.suspended is True
    assert recovered.resume_pending is False
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


def test_exact_active_marker_supersedes_existing_timeout_reason(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    original_mark = datetime.now() - timedelta(minutes=2)

    with store._lock:
        current = store._entries[entry.session_key]
        current.resume_pending = True
        current.resume_reason = "shutdown_timeout"
        current.last_resume_marked_at = original_mark

    assert store.recover_interrupted_turns() == 1
    recovered = _entry_for(store, source)
    assert recovered.resume_pending is True
    assert recovered.resume_reason == "active_turn_interrupted"
    assert recovered.last_resume_marked_at > original_mark
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


def test_ancient_active_marker_is_cleared_without_auto_resume(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)

    with store._lock:
        current = store._entries[entry.session_key]
        current.active_turn_started_at = datetime.now() - timedelta(hours=2)

    assert store.recover_interrupted_turns(
        max_age_seconds=ACTIVE_TURN_MAX_AGE_SECONDS
    ) == 0
    recovered = _entry_for(store, source)
    assert recovered.resume_pending is False
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


def test_clean_startup_discards_orphan_markers_without_resuming(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)

    assert store.discard_active_turn_markers() == 1

    recovered = _entry_for(store, source)
    assert recovered.resume_pending is False
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


def test_recovery_db_failure_publishes_no_in_memory_resume(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)
    store._db.replace_gateway_routing_entries = MagicMock(
        side_effect=OSError("db write blocked")
    )

    with pytest.raises(
        RuntimeError, match="authoritative state.db routing save failed"
    ):
        store.recover_interrupted_turns()

    assert entry.resume_pending is False
    assert entry.resume_reason is None
    assert entry.active_turn_token == token


def test_clean_discard_db_failure_keeps_live_marker_retryable(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)
    store._db.replace_gateway_routing_entries = MagicMock(
        side_effect=OSError("db write blocked")
    )

    with pytest.raises(
        RuntimeError, match="authoritative state.db routing save failed"
    ):
        store.discard_active_turn_markers()

    assert entry.active_turn_token == token


@pytest.mark.asyncio
async def test_clean_shutdown_marker_is_not_consumed_when_discard_fails(tmp_path):
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("clean", encoding="utf-8")
    runner = object.__new__(GatewayRunner)
    async_store = MagicMock()
    async_store.discard_active_turn_markers = AsyncMock(
        side_effect=OSError("state store unavailable")
    )

    with patch.object(
        GatewayRunner,
        "async_session_store",
        new_callable=PropertyMock,
        return_value=async_store,
    ):
        with pytest.raises(OSError, match="state store unavailable"):
            await runner._consume_clean_shutdown_marker(marker)

    assert marker.exists()


@pytest.mark.asyncio
async def test_clean_shutdown_marker_is_unlinked_after_durable_discard(tmp_path):
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("clean", encoding="utf-8")
    runner = object.__new__(GatewayRunner)
    async_store = MagicMock()
    async_store.discard_active_turn_markers = AsyncMock(return_value=2)

    with patch.object(
        GatewayRunner,
        "async_session_store",
        new_callable=PropertyMock,
        return_value=async_store,
    ):
        discarded = await runner._consume_clean_shutdown_marker(marker)

    assert discarded == 2
    assert not marker.exists()


@pytest.mark.asyncio
async def test_runner_active_turn_carrier_clears_the_exact_resolved_key():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    mark_active = AsyncMock(return_value="token-1")
    clear_active = AsyncMock(return_value=True)
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            mark_turn_active=mark_active,
            clear_turn_active=clear_active,
        ),
    )
    event = SimpleNamespace()

    await runner._mark_durable_active_turn(
        cast(Any, event), "resolved-session-key"
    )

    assert event._gateway_active_turn_session_key == "resolved-session-key"
    assert event._gateway_active_turn_token == "token-1"

    await runner._clear_durable_active_turn(cast(Any, event))

    clear_active.assert_awaited_once_with("resolved-session-key", "token-1")
    assert not hasattr(event, "_gateway_active_turn_session_key")
    assert not hasattr(event, "_gateway_active_turn_token")


@pytest.mark.asyncio
async def test_runner_active_turn_clear_is_best_effort():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    clear_active = AsyncMock(
        side_effect=[OSError("disk unavailable"), True]
    )
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            clear_turn_active=clear_active,
        ),
    )
    event = SimpleNamespace(
        _gateway_active_turn_session_key="resolved-session-key",
        _gateway_active_turn_token="token-1",
    )

    await runner._clear_durable_active_turn(cast(Any, event))

    assert clear_active.await_count == 2
    assert not hasattr(event, "_gateway_active_turn_session_key")
    assert not hasattr(event, "_gateway_active_turn_token")


@pytest.mark.asyncio
async def test_runner_active_turn_clear_stops_after_bounded_retries():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    clear_active = AsyncMock(side_effect=OSError("disk unavailable"))
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            clear_turn_active=clear_active,
        ),
    )
    event = SimpleNamespace(
        _gateway_active_turn_session_key="resolved-session-key",
        _gateway_active_turn_token="token-1",
    )

    assert await runner._clear_durable_active_turn(cast(Any, event)) is False

    assert clear_active.await_count == 3
    assert not hasattr(event, "_gateway_active_turn_session_key")
    assert not hasattr(event, "_gateway_active_turn_token")


@pytest.mark.asyncio
async def test_unclean_recovery_promotes_exact_markers_before_legacy_fallback(
    monkeypatch,
):
    runner = object.__new__(GatewayRunner)
    calls: list[str] = []

    monkeypatch.delenv("HERMES_AGENT_TIMEOUT", raising=False)

    async def _recover(*, max_age_seconds):
        assert max_age_seconds == ACTIVE_TURN_MAX_AGE_SECONDS
        calls.append("exact")
        return 1

    async def _fallback(*, max_age_seconds):
        assert max_age_seconds == 120
        calls.append("fallback")
        return 2

    runner.session_store = MagicMock()
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            recover_interrupted_turns=_recover,
            suspend_recently_active=_fallback,
        ),
    )

    assert await runner._recover_unclean_sessions() == (1, 2)
    assert calls == ["exact", "fallback"]


@pytest.mark.asyncio
async def test_real_startup_promotes_and_dispatches_exact_active_turn_once(tmp_path):
    config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(enabled=True, token="test-token")
        },
        sessions_dir=tmp_path / "sessions",
        loop_watchdog=False,
    )
    runner = GatewayRunner(config)
    runner._previous_shutdown_clean = False
    source = _make_source("startup-integration")
    entry = runner.session_store.get_or_create_session(source)
    runner.session_store.mark_turn_active(entry.session_key)
    with runner.session_store._lock:
        current = runner.session_store._entries[entry.session_key]
        current.updated_at = datetime.now() - timedelta(hours=2)
        current.active_turn_started_at = datetime.now() - timedelta(minutes=10)
        runner.session_store._save()

    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter(config.platforms[Platform.DISCORD])
    adapter.gateway_runner = runner
    runner._create_adapter = MagicMock(return_value=adapter)
    runner._connect_adapter_with_timeout = AsyncMock(return_value=True)
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._suspend_stuck_loop_sessions = MagicMock(return_value=0)
    runner._update_runtime_status = MagicMock()
    runner._update_platform_runtime_status = MagicMock()
    runner._sync_voice_mode_state_to_adapter = MagicMock()
    runner._send_update_notification = AsyncMock(return_value=True)
    runner._send_restart_notification = AsyncMock()
    runner._redeliver_pending_obligations = AsyncMock(return_value=0)
    runner.hooks = MagicMock(loaded_hooks=[])
    runner.hooks.emit = AsyncMock()

    agent_runs: list[str] = []

    async def _fake_run(event, event_source, quick_key, run_generation):
        agent_runs.append(quick_key)
        return "RESUMED OK"

    runner._handle_message_with_agent = _fake_run
    runner._post_turn_goal_continuation = AsyncMock()
    adapter.send = AsyncMock()
    adapter._keep_typing = AsyncMock()
    adapter._stop_typing_refresh = AsyncMock()
    adapter._run_processing_hook = AsyncMock()

    real_create_task = asyncio.create_task

    def create_only_resume_task(coro, *args, **kwargs):
        name = getattr(getattr(coro, "cr_code", None), "co_name", "")
        if name in {
            "_run_startup_resume_event",
            "_process_message_background",
            "_drain_pending_messages",
        }:
            return real_create_task(coro, *args, **kwargs)
        coro.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    with patch("gateway.status.write_runtime_status"), patch(
        "hermes_cli.plugins.discover_plugins"
    ), patch("hermes_cli.config.load_config", return_value={}), patch(
        "agent.shell_hooks.register_from_config"
    ), patch(
        "tools.process_registry.process_registry.recover_from_checkpoint",
        return_value=0,
    ), patch(
        "gateway.channel_directory.build_channel_directory",
        new=AsyncMock(return_value={"platforms": {}}),
    ), patch("gateway.run.asyncio.create_task", side_effect=create_only_resume_task):
        assert await runner.start() is True

    for _ in range(50):
        if (
            agent_runs
            and entry.session_key not in runner._running_agents
            and entry.session_key not in adapter._pending_messages
        ):
            break
        await asyncio.sleep(0.02)

    recovered = _entry_for(runner.session_store, source)
    assert agent_runs == [entry.session_key]
    assert recovered.resume_reason == "active_turn_interrupted"
    assert recovered.active_turn_token is None
    assert entry.session_key not in runner._running_agents
    assert entry.session_key not in adapter._pending_messages
