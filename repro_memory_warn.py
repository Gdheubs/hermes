#!/usr/bin/env python3
"""Repro: memory tool has no soft-capacity warning at 90%.

Bug class: #60900 / #60902 / #60905 — a memory add that pushes the
store to >=90% of its char limit succeeds silently, giving the model no
signal to consolidate before hitting the hard cap (where adds fail).

On main: FAILS (no warning at 90%). With the fix: PASSES (warning +
recommendation at >=90%, nothing below).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import memory_tool  # noqa: E402
from tools.memory_tool import MemoryStore  # noqa: E402


def make_store():
    tmp = Path(tempfile.mkdtemp(prefix="hermes-repro-mem-"))
    memory_tool.get_memory_dir = lambda: tmp  # mirror the test fixture's isolation
    store = MemoryStore(memory_char_limit=500, user_char_limit=300)
    store.load_from_disk()
    return store


# Below threshold: no warning
r1 = make_store().add("memory", "x" * 449)
if "warning" in r1:
    print("FAIL: warning present below 90% threshold")
    sys.exit(1)

# At threshold: warning + recommendation
r2 = make_store().add("memory", "x" * 450)
if "warning" not in r2:
    print("FAIL: no warning at 90% capacity")
    sys.exit(1)
if "Memory is nearing capacity" not in r2["warning"] or "90%" not in r2["warning"]:
    print(f"FAIL: warning malformed: {r2['warning']!r}")
    sys.exit(1)
if "consolidate" not in r2.get("recommendation", "").lower():
    print("FAIL: no consolidate recommendation")
    sys.exit(1)

print("PASS: no warning below 90%, warning + recommendation at 90%")
sys.exit(0)
