"""Cron-safe Kanban todo/triage lifecycle transitions."""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _run_cli(*argv: str) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    kb_cli.build_parser(subparsers)
    args = parser.parse_args(["kanban", *argv])
    return kb_cli.kanban_command(args)


def test_idempotent_linkedin_cron_card_preserves_identity_comments_and_no_spawns(
    kanban_home, monkeypatch
):
    spawn_calls: list[str] = []
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="LinkedIn M/W/F weekly post",
            assignee="publisher",
            initial_status="todo",
            idempotency_key="linkedin-post:2026-W33",
        )
        comment_id = kb.add_comment(
            conn,
            task_id,
            author="linkedin-content-cron",
            body="Drafts for Monday, Wednesday, and Friday are queued.",
        )
        reused_id = kb.create_task(
            conn,
            title="LinkedIn M/W/F weekly post (retry)",
            assignee="publisher",
            initial_status="todo",
            idempotency_key="linkedin-post:2026-W33",
        )

        assert reused_id == task_id
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "todo"
        assert task.manual_hold is True
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

        first_tick = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawn_calls.append(task.id),
        )
        assert first_tick.promoted == 0
        assert first_tick.spawned == []
        assert spawn_calls == []
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "todo"

        transition = kb.transition_task_status(
            conn,
            task_id,
            target_status="triage",
            actor="linkedin-analytics-cron",
            reason="Friday analytics started",
        )
        assert transition.ok is True
        assert transition.changed is True
        assert transition.error is None

        # Retrying the transition is idempotent: no duplicate event or task.
        transition = kb.transition_task_status(
            conn,
            task_id,
            target_status="triage",
            actor="linkedin-analytics-cron",
            reason="Friday analytics started",
        )
        assert transition.ok is True
        assert transition.changed is False
        assert transition.error is None

        second_tick = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawn_calls.append(task.id),
        )
        assert second_tick.spawned == []
        assert spawn_calls == []
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "triage"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        comments = kb.list_comments(conn, task_id)
        assert [comment.id for comment in comments] == [comment_id]
        assert [comment.task_id for comment in comments] == [task_id]
        assert [comment.author for comment in comments] == [
            "linkedin-content-cron"
        ]
        assert [comment.body for comment in comments] == [
            "Drafts for Monday, Wednesday, and Friday are queued."
        ]

        transitions = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "status_transitioned"
        ]
        assert len(transitions) == 1
        assert transitions[0].payload == {
            "source_status": "todo",
            "target_status": "triage",
            "actor": "linkedin-analytics-cron",
            "reason": "Friday analytics started",
        }
        assert transitions[0].created_at > 0


def test_concurrent_idempotent_create_rechecks_after_write_lock(kanban_home):
    """A retry that loses the write race must reuse the committed winner."""
    key = "linkedin-post:company-page:2026-W33"
    lookup_seen = threading.Event()
    result: dict[str, str] = {}
    errors: list[BaseException] = []
    retry_thread: threading.Thread | None = None
    first_conn = kb.connect()

    def create_retry() -> None:
        try:
            with kb.connect_closing() as retry_conn:
                retry_conn.set_trace_callback(
                    lambda statement: lookup_seen.set()
                    if "SELECT id FROM tasks WHERE idempotency_key" in statement
                    else None
                )
                result["id"] = kb.create_task(
                    retry_conn,
                    title="LinkedIn M/W/F weekly post retry",
                    assignee="publisher",
                    initial_status="todo",
                    idempotency_key=key,
                )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    try:
        with kb.write_txn(first_conn):
            first_id = kb.create_task(
                first_conn,
                title="LinkedIn M/W/F weekly post",
                assignee="publisher",
                initial_status="todo",
                idempotency_key=key,
            )
            retry_thread = threading.Thread(target=create_retry)
            retry_thread.start()
            assert lookup_seen.wait(5), "retry never reached optimistic lookup"
            assert retry_thread.is_alive(), "retry should be waiting on write lock"
    finally:
        if retry_thread is not None:
            retry_thread.join(10)
        first_conn.close()

    assert retry_thread is not None
    assert not retry_thread.is_alive()
    assert errors == []
    assert result["id"] == first_id
    with kb.connect_closing() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
            (key,),
        ).fetchone()[0] == 1


def test_claim_and_transition_race_has_one_winner(kanban_home):
    """A card cannot become both claimed and triaged in the same race."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="race", assignee="publisher")

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def claim() -> None:
        try:
            with kb.connect_closing() as conn:
                barrier.wait()
                results["claimed"] = kb.claim_task(conn, task_id)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def transition() -> None:
        try:
            with kb.connect_closing() as conn:
                barrier.wait()
                results["transition"] = kb.transition_task_status(
                    conn,
                    task_id,
                    target_status="triage",
                    actor="linkedin-analytics-cron",
                )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    claim_thread = threading.Thread(target=claim)
    transition_thread = threading.Thread(target=transition)
    claim_thread.start()
    transition_thread.start()
    claim_thread.join(10)
    transition_thread.join(10)

    assert not claim_thread.is_alive()
    assert not transition_thread.is_alive()
    assert errors == []
    claimed = results["claimed"]
    transition_result = results["transition"]
    assert isinstance(transition_result, kb.StatusTransitionResult)
    assert bool(claimed) != transition_result.ok

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == ("running" if claimed else "triage")


def test_dependency_todo_keeps_legacy_auto_promotion(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent")
        child_id = kb.create_task(conn, title="child", parents=[parent_id])
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "todo"
        assert child.manual_hold is False

        assert kb.complete_task(conn, parent_id)
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "ready"


def test_transition_to_todo_is_a_manual_hold_until_promoted(
    kanban_home, monkeypatch
):
    spawn_calls: list[str] = []
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="stage release", assignee="publisher")
        transition = kb.transition_task_status(
            conn,
            task_id,
            target_status="todo",
            actor="release-cron",
            reason="wait for package assembly",
        )
        assert transition.ok is True
        assert transition.changed is True
        assert transition.error is None
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "todo"
        assert task.manual_hold is True

        held_tick = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawn_calls.append(task.id),
        )
        assert held_tick.promoted == 0
        assert spawn_calls == []

        promoted, promote_error = kb.promote_task(
            conn,
            task_id,
            actor="release-cron",
            reason="assembly can start",
        )
        assert promoted is True
        assert promote_error is None
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.manual_hold is False

        released_tick = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawn_calls.append(task.id),
        )
        assert [task_id] == spawn_calls
        assert [row[0] for row in released_tick.spawned] == [task_id]


def test_transition_refuses_active_or_terminal_tasks(kanban_home):
    with kb.connect() as conn:
        running_id = kb.create_task(conn, title="active", assignee="worker")
        assert kb.claim_task(conn, running_id) is not None
        transition = kb.transition_task_status(
            conn,
            running_id,
            target_status="triage",
            actor="cron",
        )
        assert transition.ok is False
        assert "running" in (transition.error or "")
        running = kb.get_task(conn, running_id)
        assert running is not None
        assert running.status == "running"

        done_id = kb.create_task(conn, title="finished")
        assert kb.complete_task(conn, done_id)
        transition = kb.transition_task_status(
            conn,
            done_id,
            target_status="todo",
            actor="cron",
        )
        assert transition.ok is False
        assert "done" in (transition.error or "")
        done = kb.get_task(conn, done_id)
        assert done is not None
        assert done.status == "done"


def test_cli_create_reuses_held_todo_then_transitions_to_triage(
    kanban_home, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_PROFILE", "linkedin-analytics-cron")

    create_args = (
        "create",
        "LinkedIn M/W/F weekly post",
        "--assignee",
        "publisher",
        "--initial-status",
        "todo",
        "--idempotency-key",
        "linkedin-post:2026-W33",
        "--json",
    )
    assert _run_cli(*create_args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "todo"

    assert _run_cli(*create_args) == 0
    reused = json.loads(capsys.readouterr().out)
    assert reused["id"] == first["id"]

    assert _run_cli(
        "transition",
        first["id"],
        "--to",
        "triage",
        "--reason",
        "Friday analytics started",
        "--json",
    ) == 0
    transitioned = json.loads(capsys.readouterr().out)
    assert transitioned == {
        "task_id": first["id"],
        "source_status": "todo",
        "target_status": "triage",
        "actor": "linkedin-analytics-cron",
        "reason": "Friday analytics started",
        "transitioned": True,
        "error": None,
    }

    assert _run_cli(
        "transition",
        first["id"],
        "--to",
        "triage",
        "--reason",
        "Friday analytics retry",
        "--json",
    ) == 0
    no_op = json.loads(capsys.readouterr().out)
    assert no_op == {
        "task_id": first["id"],
        "source_status": "triage",
        "target_status": "triage",
        "actor": "linkedin-analytics-cron",
        "reason": "Friday analytics retry",
        "transitioned": False,
        "error": None,
    }

    with kb.connect() as conn:
        task = kb.get_task(conn, first["id"])
        assert task is not None
        assert task.status == "triage"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
