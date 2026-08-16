"""Regression tests for the concurrent-instance memory clobber (issue #85858).

Two MemoryStore instances sharing one profile (two processes / sessions)
must not lose each other's entries (last-writer-wins) and must not resurrect
removed/replaced entries. These tests guard the merge + tombstone fix in
tools/memory_tool.py (see tools/memory_tool_85858_fix.patch).

They drive the public save_to_disk(merge_live=True) path the way the
background-sync / second-instance race does: each store mutates its OWN
in-memory snapshot and then flushes, without re-reading between the two
flushes. Without the fix, save_to_disk(merge_live=True) does not exist
(TypeError) and the plain full-file rewrite would drop the sibling's entry.
"""

import pytest

from tools.memory_tool import MemoryStore


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    s = MemoryStore(memory_char_limit=5000, user_char_limit=3000)
    s.load_from_disk()
    return s


class TestConcurrentAddNoLoss:
    def test_two_stores_preserve_each_others_adds(self, tmp_path, monkeypatch):
        (tmp_path / "MEMORY.md").write_text("pre", encoding="utf-8")

        a = _store(tmp_path, monkeypatch)
        b = _store(tmp_path, monkeypatch)

        # Each store seeds its own in-memory snapshot, then flushes. The merge
        # in save_to_disk must keep BOTH writes on disk.
        a.memory_entries = ["pre", "from A"]
        b.memory_entries = ["pre", "from B"]

        a.save_to_disk("memory", merge_live=True)
        b.save_to_disk("memory", merge_live=True)

        final = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "from A" in final
        assert "from B" in final
        assert "pre" in final


class TestNoResurrection:
    def test_removed_entry_not_resurrected_by_sibling(self, tmp_path, monkeypatch):
        (tmp_path / "MEMORY.md").write_text("pre\n§\nsecret X", encoding="utf-8")
        a = _store(tmp_path, monkeypatch)
        b = _store(tmp_path, monkeypatch)

        a.remove("memory", "secret X")  # writes a tombstone for X this round
        # Sibling B still holds the stale snapshot containing X and flushes it.
        b.memory_entries = ["pre", "secret X", "from B"]
        b.save_to_disk("memory", merge_live=True)

        final = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "secret X" not in final
        assert "from B" in final
        assert "pre" in final

    def test_replaced_entry_old_version_not_resurrected(self, tmp_path, monkeypatch):
        (tmp_path / "MEMORY.md").write_text("pre\n§\nsecret X", encoding="utf-8")
        a = _store(tmp_path, monkeypatch)
        b = _store(tmp_path, monkeypatch)

        a.replace("memory", "secret X", "secret X'")  # tombstones X, writes X'
        b.memory_entries = ["pre", "secret X", "from B"]
        b.save_to_disk("memory", merge_live=True)

        final = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        entries = [e for e in final.split("\n§\n") if e.strip()]
        assert "secret X" not in entries        # old version gone (exact entry)
        assert "secret X'" in entries           # new version kept
        assert "from B" in entries
        assert "pre" in entries
