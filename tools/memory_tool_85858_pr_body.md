# Fix concurrent-instance memory clobber (last-writer-wins) — #85858

## Problem
`MemoryStore` persists by rewriting the **entire `MEMORY.md`/user file** from an
in-memory entry list (`save_to_disk`). That list is a snapshot seeded at
`load_from_disk()`, and the background `sync_all` flush writes it **without
re-reading at flush time**. When two Hermes instances share one profile, each
holds its own aged snapshot; whichever `save_to_disk` runs last rewrites the file
from its stale list and silently discards the other instance's concurrent entry.
This is the same class that wiped `MEMORY.md` under concurrent sessions
(reported in #85858).

On Windows the file lock (`msvcrt.locking`) is a 1-byte advisory lock that is
process-dependent and unreliable under OneDrive-synced `AppData`, so it does not
reliably serialize two processes — making the clobber reachable in practice.

## Fix (split into two independently-reviewable patches)

**1. `memory_tool_85858_dataloss_fix.patch` (required)** — merge + tombstone
instead of blind overwrite:
- `save_to_disk(..., merge_live=True)` unions live on-disk entries the instance
  doesn't already hold (content-dedup). Last-writer-*wins* →
  last-writer-*merges*. `add` opts in.
- **Resurrection safety via tombstones** (`__mem_tomb__:<sha1>` marker lines):
  `remove`/`replace`/`apply_batch` write a tombstone for the removed/replaced
  entry for **one round**, so a sibling holding a stale snapshot skips it
  instead of resurrecting it. Tombstones are never surfaced as entries and fade
  after one round. The drift guard now strips tombstone lines before its
  round-trip check.
- `add` refuses to re-add a tombstoned entry.

**2. `memory_tool_85858_lock_hardening.patch` (optional hardening)** — Windows
`msvcrt.locking` now uses non-blocking `LK_NBLCK` with backoff retry + a 30s
timeout, so concurrent writers *queue* instead of racing and can't deadlock.
This is a complementary defense; the merge/tombstone logic is the real safety
net, so this patch is safe to merge independently (or skip).

## Verification
New `tests/tools/test_memory_concurrency_85858.py` (3 tests) exercises two
`MemoryStore` instances sharing one profile: concurrent `add`s both survive;
a `remove` then a sibling's stale flush does **not** resurrect the entry; a
`replace` then a sibling's stale flush keeps the new version and drops the old.

- New test + `tests/tools/test_memory_tool.py`: **40 passed**.
- Broader `tests/agent/test_memory_*.py` + schema/import tests: **71 passed**.
- Both patches apply cleanly (no flags) in either order.

Running the real suite caught a bug the standalone model missed: the drift guard
compared the raw file (with the tombstone line) against the tombstone-stripped
round-trip and falsely refused writes; fixed. The standalone model had already
caught three earlier bugs (tombstoning every entry; not filtering stale
in-memory entries; text-vs-marker mismatch in the merge). All fixed.

Standalone model also passes: concurrent adds both preserved; `remove` + stale
sibling flush → no resurrection (`['pre','from B']`); `replace` X→X' + stale
sibling flush → old gone, X' kept (`['pre','from B',"secret X'"]`).

## Scope
- Only `add` uses `merge_live` (append is safe). `replace`/`remove` use
  tombstones, not merge, because merging there could resurrect removed content.
- Format-compatible: tombstone lines are stripped on parse and by the drift
  guard, so older readers ignore them.
- Patch #1 is the required fix; patch #2 is optional hardening.

## How to apply
```bash
git apply tools/memory_tool_85858_dataloss_fix.patch
git apply tools/memory_tool_85858_lock_hardening.patch
pytest tests/tools/test_memory_concurrency_85858.py
```
