"""Bounded read_file behavior for cloud-backed or wedged filesystems."""

import json
import threading
import time

from tools import file_tools


def test_read_file_timeout_returns_actionable_structured_error(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def _wedged_read(*_args, **_kwargs):
        started.set()
        release.wait()
        return "late result"

    monkeypatch.setattr(file_tools, "_read_file_tool_impl", _wedged_read)
    monkeypatch.setattr(file_tools, "_resolve_read_file_timeout", lambda: 0.05)

    before = time.monotonic()
    try:
        payload = json.loads(file_tools.read_file_tool("/cloud/project/file.md"))
    finally:
        release.set()

    assert started.is_set()
    assert time.monotonic() - before < 1.0
    assert payload["error_type"] == "tool_timeout"
    assert payload["timeout_seconds"] == 0.05
    assert payload["path"] == "/cloud/project/file.md"
    assert "local clone/direct source" in payload["error"]
    assert "instead of retrying the same read" in payload["error"]


def test_read_file_timeout_can_be_disabled(monkeypatch):
    monkeypatch.setattr(file_tools, "_resolve_read_file_timeout", lambda: None)
    monkeypatch.setattr(
        file_tools,
        "_read_file_tool_impl",
        lambda path, offset, limit, task_id: f"{path}|{offset}|{limit}|{task_id}",
    )

    assert file_tools.read_file_tool("notes.md", 3, 7, "task-1") == (
        "notes.md|3|7|task-1"
    )
