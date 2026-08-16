from pathlib import Path

from hermes_cli.doctor import collect_source_tree_state


def test_collect_source_tree_state_empty_for_non_git_directory(tmp_path):
    assert collect_source_tree_state(tmp_path) == []


def test_collect_source_tree_state_reports_dirty_tracked_and_untracked(tmp_path):
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    tracked.write_text("two", encoding="utf-8")
    (tmp_path / "scratch.txt").write_text("scratch", encoding="utf-8")

    rows = collect_source_tree_state(tmp_path)
    assert any(level == "warn" and "local modifications" in text for level, text, _ in rows)
    assert any(level == "info" and "Untracked source files" in text and "scratch.txt" in detail for level, text, detail in rows)
