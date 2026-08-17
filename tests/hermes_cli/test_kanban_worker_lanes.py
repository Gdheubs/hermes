"""Native dispatcher coverage for plugin-registered worker lanes."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import profiles


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_plugin_lane_is_spawnable_in_ready_and_review(kanban_home, monkeypatch):
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: False)
    lane = lambda assignee: str(assignee).startswith("agentplane-")

    with kb.connect_closing() as conn:
        ready_id = kb.create_task(
            conn,
            title="ready external work",
            assignee="agentplane-executor",
        )
        review_id = kb.create_task(
            conn,
            title="review external work",
            assignee="agentplane-reviewer",
        )
        conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?",
            (review_id,),
        )
        conn.commit()

        assert kb.has_spawnable_ready(
            conn, spawnable_assignee_fn=lane
        ) is True
        assert kb.has_spawnable_review(
            conn, spawnable_assignee_fn=lane
        ) is True

        result = kb.dispatch_once(
            conn,
            spawnable_assignee_fn=lane,
            dry_run=True,
            max_spawn=2,
        )

    assert {item[0] for item in result.spawned} == {ready_id, review_id}
    assert result.skipped_nonspawnable == []


def test_plugin_lane_spawn_receives_claimed_task_context(kanban_home, monkeypatch):
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: False)
    captured = {}

    def spawn(task, workspace, *, board=None):
        captured.update(task=task, workspace=workspace, board=board)
        return None

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="external semantic episode",
            assignee="agentplane-executor",
            metadata={"agentplane": {"task_id": "20260817-ABC"}},
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn,
            spawnable_assignee_fn=lambda assignee: assignee == "agentplane-executor",
        )

    task = captured["task"]
    assert result.spawned[0][0] == task_id
    assert task.id == task_id
    assert task.assignee == "agentplane-executor"
    assert task.current_run_id is not None
    assert task.claim_lock
    assert task.metadata == {"agentplane": {"task_id": "20260817-ABC"}}
    assert captured["workspace"]


def test_unknown_non_profile_lane_remains_nonspawnable(kanban_home, monkeypatch):
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: False)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="unknown lane",
            assignee="unregistered-runner",
        )
        result = kb.dispatch_once(
            conn,
            spawnable_assignee_fn=lambda _assignee: False,
            dry_run=True,
        )

    assert result.spawned == []
    assert result.skipped_nonspawnable == [task_id]
