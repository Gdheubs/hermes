"""Regression coverage for browser cleanup in multiplexed gateways."""

import time

from agent.secret_scope import (
    get_secret,
    reset_secret_scope,
    set_multiplex_active,
    set_secret_scope,
)
from tools import browser_tool


def test_inactive_cleanup_reinstalls_session_secret_scope(monkeypatch):
    task_id = "profile-session"
    seen = []

    monkeypatch.setattr(browser_tool, "_session_last_activity", {})
    monkeypatch.setattr(browser_tool, "_session_secret_scopes", {}, raising=False)
    monkeypatch.setattr(browser_tool, "BROWSER_SESSION_INACTIVITY_TIMEOUT", 30)

    def scoped_cleanup(cleanup_task_id):
        seen.append((cleanup_task_id, get_secret("CAMOFOX_URL")))

    monkeypatch.setattr(browser_tool, "_cleanup_single_browser_session", scoped_cleanup)
    set_multiplex_active(True)
    token = set_secret_scope({"CAMOFOX_URL": "http://profile-camofox"})
    try:
        browser_tool._update_session_activity(task_id)
    finally:
        reset_secret_scope(token)

    browser_tool._session_last_activity[task_id] = time.time() - 31
    try:
        browser_tool._cleanup_inactive_browser_sessions()
    finally:
        set_multiplex_active(False)

    assert seen == [(task_id, "http://profile-camofox")]
    assert task_id not in browser_tool._session_last_activity
    assert task_id not in browser_tool._session_secret_scopes


def test_shutdown_cleanup_reinstalls_each_session_secret_scope(monkeypatch):
    seen = []
    monkeypatch.setattr(browser_tool, "_active_sessions", {"a": {}, "b": {}})
    monkeypatch.setattr(
        browser_tool,
        "_session_secret_scopes",
        {
            "a": {"CAMOFOX_URL": "http://profile-a"},
            "b": {"CAMOFOX_URL": "http://profile-b"},
        },
    )
    monkeypatch.setattr(
        browser_tool,
        "_cleanup_single_browser_session",
        lambda task_id: seen.append((task_id, get_secret("CAMOFOX_URL"))),
    )

    set_multiplex_active(True)
    try:
        browser_tool.cleanup_all_browsers()
    finally:
        set_multiplex_active(False)

    assert seen == [
        ("a", "http://profile-a"),
        ("b", "http://profile-b"),
    ]


def test_direct_cleanup_reinstalls_each_exact_session_scope(monkeypatch):
    task_id = "agent-close-session"
    sidecar_id = f"{task_id}{browser_tool._LOCAL_SUFFIX}"
    seen = []
    monkeypatch.setattr(
        browser_tool,
        "_active_sessions",
        {task_id: {}, sidecar_id: {}},
    )
    monkeypatch.setattr(
        browser_tool,
        "_session_secret_scopes",
        {
            task_id: {"CAMOFOX_URL": "http://profile-close"},
            sidecar_id: {"CAMOFOX_URL": "http://profile-sidecar"},
        },
    )
    monkeypatch.setattr(
        browser_tool,
        "_cleanup_single_browser_session",
        lambda cleanup_task_id: seen.append(
            (cleanup_task_id, get_secret("CAMOFOX_URL"))
        ),
    )

    set_multiplex_active(True)
    try:
        browser_tool.cleanup_browser(task_id)
    finally:
        set_multiplex_active(False)

    assert seen == [
        (task_id, "http://profile-close"),
        (sidecar_id, "http://profile-sidecar"),
    ]


def test_unscoped_activity_never_reuses_a_stale_profile_scope(monkeypatch):
    task_id = "reused-session"
    monkeypatch.setattr(browser_tool, "_session_last_activity", {})
    monkeypatch.setattr(
        browser_tool,
        "_session_secret_scopes",
        {task_id: {"CAMOFOX_URL": "http://stale-profile"}},
    )

    browser_tool._update_session_activity(task_id)

    assert browser_tool._session_secret_scopes[task_id] is None
