"""Bounded read_file behavior for cloud-backed or wedged filesystems."""

import json
import threading
import time
from unittest.mock import MagicMock

from tools import file_tools
from tools.file_operations import ReadResult


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


def test_late_read_does_not_publish_bookkeeping(tmp_path, monkeypatch):
    path = tmp_path / "cloud.md"
    path.write_text("local placeholder")
    release = threading.Event()
    read_returned = threading.Event()
    record_read = MagicMock()

    class _WedgedOps:
        def read_file(self, *_args, **_kwargs):
            release.wait()
            read_returned.set()
            return ReadResult(content="late content", total_lines=1, file_size=12)

    task_id = "late-read-task"
    with file_tools._read_tracker_lock:
        file_tools._read_tracker.pop(task_id, None)

    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task_id: _WedgedOps())
    monkeypatch.setattr(file_tools, "_file_ops_uses_host_paths", lambda _ops: True)
    monkeypatch.setattr(file_tools, "_resolve_read_file_timeout", lambda: 0.05)
    monkeypatch.setattr(file_tools.file_state, "record_read", record_read)

    try:
        payload = json.loads(file_tools.read_file_tool(str(path), task_id=task_id))
        assert payload["error_type"] == "tool_timeout"
        release.set()
        assert read_returned.wait(timeout=1)
        time.sleep(0.05)

        with file_tools._read_tracker_lock:
            task_data = file_tools._read_tracker[task_id]
            assert task_data["last_key"] is None
            assert task_data["consecutive"] == 0
            assert task_data["dedup"] == {}
            assert task_data["read_timestamps"] == {}
        record_read.assert_not_called()
    finally:
        release.set()
        with file_tools._read_tracker_lock:
            file_tools._read_tracker.pop(task_id, None)
