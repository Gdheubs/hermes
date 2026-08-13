"""Local terminal session environment isolation regression tests."""

from __future__ import annotations

import json

from tools import terminal_tool


def _run(command: str, task_id: str, workdir: str) -> dict:
    return json.loads(
        terminal_tool.terminal_tool(
            command=command,
            task_id=task_id,
            workdir=workdir,
            timeout=15,
        )
    )


def test_local_parent_and_delegate_environment_snapshots_are_isolated(
    monkeypatch,
    tmp_path,
):
    """A child's exports must not enter the parent's persistent shell state."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)

    parent_id = "parent-session-env-isolation"
    child_id = "subagent-env-isolation"
    for task_id in (parent_id, child_id):
        terminal_tool.cleanup_vm(task_id)

    try:
        parent_export = _run(
            "export HERMES_PARENT_SENTINEL=parent",
            parent_id,
            str(tmp_path),
        )
        child_export = _run(
            "printf 'parent=%s\\n' \"${HERMES_PARENT_SENTINEL-unset}\"; "
            "export HERMES_CHILD_SENTINEL=child",
            child_id,
            str(tmp_path),
        )
        parent_check = _run(
            "printf 'parent=%s child=%s\\n' "
            "\"${HERMES_PARENT_SENTINEL-unset}\" "
            "\"${HERMES_CHILD_SENTINEL-unset}\"",
            parent_id,
            str(tmp_path),
        )

        assert parent_export["exit_code"] == 0
        assert child_export["exit_code"] == 0
        assert child_export["output"].strip() == "parent=unset"
        assert parent_check["exit_code"] == 0
        assert parent_check["output"].strip() == "parent=parent child=unset"
        assert terminal_tool.get_active_env(parent_id) is not terminal_tool.get_active_env(
            child_id
        )
    finally:
        for task_id in (parent_id, child_id):
            terminal_tool.cleanup_vm(task_id)
            terminal_tool.clear_task_env_overrides(task_id)
