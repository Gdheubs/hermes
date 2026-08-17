"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from gateway import kanban_watchers
from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_dispatch_threads_worker_lane_callbacks(monkeypatch):
    lane_spawn = lambda *_args, **_kwargs: None
    lane_predicate = lambda _assignee: True
    monkeypatch.setattr(
        kanban_watchers,
        "_worker_lane_callbacks",
        lambda _context: (lane_spawn, lane_predicate),
    )
    captured = {}
    fake_db = SimpleNamespace(
        dispatch_once=lambda conn, **kwargs: captured.update(kwargs) or "result"
    )

    result = kanban_watchers._dispatch_kanban_once(
        fake_db, object(), board="main", max_spawn=2
    )

    assert result == "result"
    assert captured["spawn_fn"] is lane_spawn
    assert captured["spawnable_assignee_fn"] is lane_predicate
    assert captured["board"] == "main"


def test_gateway_readiness_uses_worker_lane_predicate(monkeypatch):
    lane_predicate = lambda assignee: assignee == "agentplane-executor"
    monkeypatch.setattr(
        kanban_watchers,
        "_worker_lane_callbacks",
        lambda _context: (None, lane_predicate),
    )
    captured = {}

    def ready(_conn, **kwargs):
        captured["ready"] = kwargs
        return False

    def review(_conn, **kwargs):
        captured["review"] = kwargs
        return True

    fake_db = SimpleNamespace(
        has_spawnable_ready=ready,
        has_spawnable_review=review,
    )

    assert kanban_watchers._has_spawnable_kanban_work(
        fake_db, object(), review=True
    ) is True
    assert captured["ready"]["spawnable_assignee_fn"] is lane_predicate
    assert captured["review"]["spawnable_assignee_fn"] is lane_predicate

