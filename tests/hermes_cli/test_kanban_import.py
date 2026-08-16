"""Behavior tests for importing Markdown task pools into native Kanban."""

from __future__ import annotations

import argparse
import threading
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kc
from hermes_cli.kanban_import import MarkdownAdapter, sync_import


@pytest.fixture
def import_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    profile = home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    source = tmp_path / "tasks"
    source.mkdir()
    return source


def _write(source: Path, name: str, metadata: dict, body: str = "work") -> Path:
    path = source / f"{name}.md"
    path.write_text(
        "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n" + body + "\n",
        encoding="utf-8",
    )
    return path


def _read(path: Path) -> dict:
    return next(MarkdownAdapter(path.parent).scan()).metadata


def test_dry_run_validates_without_mutating_source_or_schema(import_env):
    path = _write(import_env, "one", {
        "id": "ext-1", "title": "One", "status": "pending", "assignee": "worker",
    })
    original = path.read_text(encoding="utf-8")
    with kb.connect_closing() as conn:
        result = sync_import(
            conn, adapter=MarkdownAdapter(import_env), import_id="pool", dry_run=True,
        )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='task_imports'"
        ).fetchone() is None
        assert kb.list_tasks(conn) == []
    assert [(row.source_id, row.action) for row in result] == [("ext-1", "would_import")]
    assert path.read_text(encoding="utf-8") == original


def test_import_is_idempotent_and_mirrors_terminal_state(import_env):
    path = _write(import_env, "one", {
        "id": "ext-1", "title": "One", "status": "pending", "assignee": "worker",
        "priority": 7, "skills": ["github-code-review"],
    })
    adapter = MarkdownAdapter(import_env)
    with kb.connect_closing() as conn:
        first = sync_import(conn, adapter=adapter, import_id="pool")
        task_id = first[0].task_id
        assert first[0].action == "imported"
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.priority == 7
        assert task.skills == ["github-code-review"]
        assert _read(path)["status"] == "imported"

        second = sync_import(conn, adapter=adapter, import_id="pool")
        assert second[0].action == "unchanged"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

        conn.execute(
            "UPDATE tasks SET status='done', completed_at=1 WHERE id=?", (task_id,)
        )
        conn.commit()
        mirrored = sync_import(conn, adapter=adapter, import_id="pool")
        assert mirrored[0].action == "mirrored"
        metadata = _read(path)
        assert metadata["status"] == "done"
        assert metadata["hermes_kanban"]["task_id"] == task_id
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='import_mirrored'",
            (task_id,),
        ).fetchone()[0] == 1


def test_import_maps_dependency_graph(import_env):
    _write(import_env, "parent", {
        "id": "parent", "title": "Parent", "status": "pending", "assignee": "worker",
    })
    _write(import_env, "child", {
        "id": "child", "title": "Child", "status": "pending", "assignee": "worker",
        "depends_on": ["parent"],
    })
    with kb.connect_closing() as conn:
        result = sync_import(
            conn, adapter=MarkdownAdapter(import_env), import_id="pool",
        )
        ids = {row.source_id: row.task_id for row in result}
        child = kb.get_task(conn, ids["child"])
        assert child is not None and child.status == "todo"
        parent_ids = [
            row["parent_id"] for row in conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id=?", (child.id,)
            )
        ]
        assert parent_ids == [ids["parent"]]


def test_invalid_records_fail_closed_without_native_cards(import_env):
    _write(import_env, "unknown", {
        "id": "bad", "title": "Bad", "status": "pending", "assignee": "missing",
    })
    with kb.connect_closing() as conn:
        result = sync_import(
            conn, adapter=MarkdownAdapter(import_env), import_id="pool",
        )
        assert result[0].action == "error"
        assert "unknown or unassigned profile" in result[0].error
        assert kb.list_tasks(conn) == []


@pytest.mark.parametrize("metadata", [
    {"id": "x", "title": "X", "status": "pending", "assignee": "worker", "mystery": 1},
    {"id": "x", "title": "X", "status": "pending", "assignee": "worker", "workspace": "dir:relative"},
])
def test_unsupported_fields_and_relative_workspaces_fail_closed(import_env, metadata):
    _write(import_env, "bad", metadata)
    with kb.connect_closing() as conn:
        result = sync_import(
            conn, adapter=MarkdownAdapter(import_env), import_id="pool",
        )
        assert result[0].action == "error"
        assert kb.list_tasks(conn) == []


def test_source_cannot_become_runnable_after_import(import_env):
    path = _write(import_env, "one", {
        "id": "ext-1", "title": "One", "status": "pending", "assignee": "worker",
    })
    adapter = MarkdownAdapter(import_env)
    with kb.connect_closing() as conn:
        imported = sync_import(conn, adapter=adapter, import_id="pool")
        metadata = _read(path)
        metadata["status"] = "pending"
        _write(import_env, "one", metadata)
        conflict = sync_import(conn, adapter=adapter, import_id="pool")
        assert conflict[0].action == "conflict"
        assert conflict[0].task_id == imported[0].task_id
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_deleted_source_is_reported_without_archiving_native_task(import_env):
    path = _write(import_env, "one", {
        "id": "ext-1", "title": "One", "status": "pending", "assignee": "worker",
    })
    adapter = MarkdownAdapter(import_env)
    with kb.connect_closing() as conn:
        imported = sync_import(conn, adapter=adapter, import_id="pool")
        path.unlink()
        result = sync_import(conn, adapter=adapter, import_id="pool")
        assert result[0].action == "error"
        assert "deleted" in result[0].error
        assert kb.get_task(conn, imported[0].task_id).status == "ready"


def test_duplicate_source_ids_fail_closed(import_env):
    metadata = {
        "id": "same", "title": "One", "status": "pending", "assignee": "worker",
    }
    _write(import_env, "one", metadata)
    _write(import_env, "two", {**metadata, "title": "Two"})
    with kb.connect_closing() as conn:
        result = sync_import(
            conn, adapter=MarkdownAdapter(import_env), import_id="pool",
        )
        assert [(row.source_id, row.action) for row in result] == [("same", "error")]
        assert kb.list_tasks(conn) == []


def test_cli_import_json_exercises_public_surface(import_env):
    _write(import_env, "one", {
        "id": "ext-1", "title": "One", "status": "pending", "assignee": "worker",
    })
    output = kc.run_slash(
        f'import --adapter markdown --source "{import_env}" --id pool --dry-run --json'
    )
    assert '"source_id": "ext-1"' in output
    assert '"action": "would_import"' in output


def test_malformed_scalar_fields_fail_closed_without_raising(import_env):
    _write(import_env, "bad", {
        "id": "bad", "title": "Bad", "status": "pending", "assignee": "worker",
        "priority": [1],
    })
    with kb.connect_closing() as conn:
        result = sync_import(
            conn, adapter=MarkdownAdapter(import_env), import_id="pool",
        )
        assert result[0].action == "error"
        assert result[0].error == "bad.md: priority must be an integer"
        assert kb.list_tasks(conn) == []


def test_dry_run_and_real_import_reject_same_missing_ledger_dependency(import_env):
    _write(import_env, "parent", {
        "id": "parent", "title": "Parent", "status": "done", "assignee": "worker",
    })
    _write(import_env, "child", {
        "id": "child", "title": "Child", "status": "pending", "assignee": "worker",
        "depends_on": ["parent"],
    })
    with kb.connect_closing() as conn:
        dry = sync_import(
            conn, adapter=MarkdownAdapter(import_env), import_id="pool", dry_run=True,
        )
        real = sync_import(
            conn, adapter=MarkdownAdapter(import_env), import_id="pool",
        )
        expected = [("error", "dependency 'parent' was not imported")]
        assert [(row.action, row.error) for row in dry] == expected
        assert [(row.action, row.error) for row in real] == expected


def test_concurrent_import_ids_create_one_native_card(import_env):
    _write(import_env, "one", {
        "id": "ext-1", "title": "One", "status": "pending", "assignee": "worker",
    })
    barrier = threading.Barrier(2)

    class ConcurrentAdapter(MarkdownAdapter):
        def scan(self):
            tasks = list(super().scan())
            barrier.wait(timeout=5)
            return tasks

    outcomes = []

    def run(import_id):
        with kb.connect_closing() as conn:
            outcomes.extend(sync_import(
                conn, adapter=ConcurrentAdapter(import_env), import_id=import_id,
            ))

    threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert sorted(result.action for result in outcomes) == ["conflict", "imported"]


def test_later_import_id_rejects_existing_foreign_ownership(import_env):
    _write(import_env, "one", {
        "id": "ext-1", "title": "One", "status": "pending", "assignee": "worker",
    })
    with kb.connect_closing() as conn:
        imported = sync_import(
            conn, adapter=MarkdownAdapter(import_env), import_id="pool-a",
        )
        conflict = sync_import(
            conn, adapter=MarkdownAdapter(import_env), import_id="pool-b",
        )
        assert [(row.source_id, row.action, row.task_id) for row in conflict] == [
            ("ext-1", "conflict", imported[0].task_id),
        ]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_imports").fetchone()[0] == 1


def test_watch_mirrors_lifecycle_changes_without_manual_rerun(import_env, monkeypatch):
    path = _write(import_env, "one", {
        "id": "ext-1", "title": "One", "status": "pending", "assignee": "worker",
    })
    sleeps = 0

    def advance(_interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            with kb.connect_closing() as conn:
                conn.execute("UPDATE tasks SET status='done', completed_at=1")
                conn.commit()
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(kc.time, "sleep", advance)
    args = argparse.Namespace(
        source=str(import_env), assignee_map=None, adapter="markdown",
        import_id="pool", dry_run=False, watch=True, interval=0.01, json=False,
    )
    assert kc._cmd_import(args) == 0
    assert _read(path)["status"] == "done"


def test_cli_conflict_returns_nonzero(import_env):
    path = _write(import_env, "one", {
        "id": "ext-1", "title": "One", "status": "pending", "assignee": "worker",
    })
    with kb.connect_closing() as conn:
        sync_import(conn, adapter=MarkdownAdapter(import_env), import_id="pool")
    metadata = _read(path)
    metadata["status"] = "pending"
    _write(import_env, "one", metadata)
    args = argparse.Namespace(
        source=str(import_env), assignee_map=None, adapter="markdown",
        import_id="pool", dry_run=False, watch=False, interval=5.0, json=False,
    )
    assert kc._cmd_import(args) == 1
