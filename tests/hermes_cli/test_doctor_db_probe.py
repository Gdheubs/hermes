"""Regression tests for the bounded state.db health probe used by doctor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import hermes_state


def _make_minimal_state_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        INSERT INTO sessions (id, source, started_at)
            VALUES ('session-1', 'test', 0.0);
        """
    )
    conn.commit()
    conn.close()


class _TracingConnection:
    def __init__(self, connection, statements, progress_handlers):
        self._connection = connection
        self._statements = statements
        self._progress_handlers = progress_handlers

    def execute(self, sql, *args, **kwargs):
        self._statements.append(str(sql))
        return self._connection.execute(sql, *args, **kwargs)

    def set_progress_handler(self, *args, **kwargs):
        self._progress_handlers.append(args)
        return self._connection.set_progress_handler(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _run_probe(path: Path, monkeypatch, *, deep: bool):
    statements = []
    progress_handlers = []
    real_connect = hermes_state.sqlite3.connect

    def traced_connect(*args, **kwargs):
        return _TracingConnection(
            real_connect(*args, **kwargs), statements, progress_handlers
        )

    monkeypatch.setattr(hermes_state.sqlite3, "connect", traced_connect)
    progress = []
    reason = hermes_state._db_opens_cleanly(
        path,
        deep=deep,
        progress_callback=progress.append,
    )
    return reason, statements, progress_handlers, progress


def test_quick_probe_skips_full_integrity_check_but_keeps_fts_write_probe(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.db"
    _make_minimal_state_db(path)

    reason, statements, progress_handlers, progress = _run_probe(
        path, monkeypatch, deep=False
    )

    assert reason is None
    assert not any("integrity_check" in statement.lower() for statement in statements)
    assert any("INSERT INTO messages" in statement for statement in statements)
    assert progress_handlers, "quick probes must install a SQLite progress deadline"
    assert progress


def test_deep_probe_is_explicit_and_retains_integrity_check(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _make_minimal_state_db(path)

    reason, statements, progress_handlers, progress = _run_probe(
        path, monkeypatch, deep=True
    )

    assert reason is None
    assert any("PRAGMA integrity_check" in statement for statement in statements)
    assert progress_handlers
    assert progress


def test_quick_and_deep_probes_honor_tiny_wall_clock_budgets(tmp_path):
    path = tmp_path / "state.db"
    _make_minimal_state_db(path)

    for deep in (False, True):
        reason = hermes_state._db_opens_cleanly(path, deep=deep, timeout=1e-9)
        mode = "deep" if deep else "quick"
        assert reason == f"{mode} state.db health probe timed out after 1e-09s"
