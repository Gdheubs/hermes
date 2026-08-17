"""Regression tests for the CLI claim-lock ownership guard (#87671).

A delegate_task child can clear its ``HERMES_DELEGATED_CHILD_CONTEXT``
lineage marker in a fresh subprocess (``env -u … hermes kanban …``) and look
like an interactive user, defeating the env-marker CLI guard.  The claim-lock
ownership guard moves the mutation boundary to the DB: a task actively
claimed by a running worker may only be closed via CLI by a process that
presents that worker's ``HERMES_KANBAN_CLAIM_LOCK`` (dispatcher-spawned
workers carry it in env; child subprocess envs have every ``HERMES_KANBAN_*``
variable scrubbed by ``scrub_kanban_env``).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
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


@pytest.fixture
def no_claim_env(monkeypatch):
    """Simulate a fresh subprocess env: no kanban env vars, no lineage marker."""
    for var in (
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _make_running_task(kanban_home) -> tuple[str, str]:
    """Create a task claimed by a live worker; return (task_id, claim_lock)."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="parent", assignee="worker", workspace_kind="scratch"
        )
        claim = kb.claim_task(conn, tid)
        assert claim is not None
        lock = kb.get_task(conn, tid).claim_lock
        assert lock
    finally:
        conn.close()
    return tid, lock


def _run_kanban(*argv: str) -> int:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", *argv])
    return kc.kanban_command(args)


def _task_status(tid: str) -> str:
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    return task.status


def test_child_without_claim_lock_cannot_complete_running_task(kanban_home, no_claim_env):
    """The env -u bypass: a fresh process with no kanban env must not be able
    to complete a running task claimed by a live worker."""
    tid, _lock = _make_running_task(kanban_home)

    rc = _run_kanban("complete", tid, "--result", "escaped child")

    assert rc == 1
    assert _task_status(tid) == "running"


def test_worker_with_matching_claim_lock_can_complete(kanban_home, no_claim_env):
    """The real dispatcher worker carries HERMES_KANBAN_CLAIM_LOCK in env and
    stays able to close its own task."""
    tid, lock = _make_running_task(kanban_home)
    no_claim_env.setenv("HERMES_KANBAN_CLAIM_LOCK", lock)

    rc = _run_kanban("complete", tid, "--result", "real worker")

    assert rc == 0
    assert _task_status(tid) == "done"


def test_unclaimed_ready_task_can_be_completed(kanban_home, no_claim_env):
    """Manual CLI completion of an unclaimed task (human flow) is unchanged."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="ready task", assignee="alice", workspace_kind="scratch"
        )
    finally:
        conn.close()

    rc = _run_kanban("complete", tid, "--result", "manual")

    assert rc == 0
    assert _task_status(tid) == "done"


def test_wrong_claim_lock_is_rejected(kanban_home, no_claim_env):
    """A process presenting a claim lock that does not match the task's must
    be refused — a worker must not close another worker's task."""
    tid, _lock = _make_running_task(kanban_home)
    no_claim_env.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:9999")

    rc = _run_kanban("complete", tid, "--result", "wrong worker")

    assert rc == 1
    assert _task_status(tid) == "running"


def test_request_review_delegated_to_db_live_claim_guard(kanban_home, no_claim_env):
    """request-review is deliberately outside the CLI claim guard: the DB
    layer already refuses unowned requests on a live claim (the operator
    override is ``--force``, see kanban_db.request_review).  The guard must
    not add a second, env-based gate that would break the worker CLI flow."""
    tid, _lock = _make_running_task(kanban_home)

    rc = _run_kanban("request-review", tid, "--summary", "not the owner")

    # Not blocked by the CLI guard — refused by the DB live-claim guard.
    assert rc == 1
    assert _task_status(tid) == "running"


def test_block_and_reclaim_remain_human_recovery_paths(kanban_home, no_claim_env):
    """block (routes to human triage) and reclaim (human recovery flow) of a
    running task are deliberately outside the claim guard."""
    tid, _lock = _make_running_task(kanban_home)
    assert _run_kanban("block", tid, "stuck worker") == 0
    assert _task_status(tid) == "blocked"

    tid2, _lock2 = _make_running_task(kanban_home)
    assert _run_kanban("reclaim", tid2, "--reason", "stuck") == 0
    assert _task_status(tid2) == "ready"


def test_read_actions_unaffected(kanban_home, no_claim_env):
    tid, _lock = _make_running_task(kanban_home)
    assert _run_kanban("show", tid) == 0
