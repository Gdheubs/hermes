# Fix: concurrent-instance memory clobber (last-writer-wins) — issue #85858

## Root cause
`MemoryStore` persists by rewriting the **whole file** from an in-memory entry
list (`save_to_disk`). That in-memory list is seeded from a snapshot taken at
provider init / `load_from_disk()`, and the background `sync_all` thread flushes
it to disk **without re-reading at flush time**. When two Hermes instances
share one profile, each holds its own aged snapshot; whichever `save_to_disk`
runs last rewrites the file from its stale list and silently discards the
other instance's concurrent entry. This is the same class that wiped
`MEMORY.md` under concurrent sessions (reported in #85858).

On Windows the file lock (`msvcrt.locking`) is a 1-byte **advisory** lock that
is process-dependent and unreliable under OneDrive-synced `AppData`, so it does
not reliably serialize two Hermes processes — making the clobber reachable in
practice. (The data-loss fix below removes the dependency on the lock entirely;
the lock patch hardens it as a complementary defense.)

## Fix — split into two independently-reviewable patches

### 1. `tools/memory_tool_85858_dataloss_fix.patch` (the real fix)
Make the write path merge + tombstone instead of blind overwrite:

- `save_to_disk(..., merge_live=True)` now unions live on-disk entries the
  instance doesn't already hold (content-dedup). Last-writer-*wins* becomes
  last-writer-*merges*. `add` opts in.
- **Resurrection safety via tombstones** (`__mem_tomb__:<sha1>` marker lines):
  `remove`/`replace`/`apply_batch` write a tombstone for the removed/replaced
  entry for **one round**. A concurrent sibling holding a stale snapshot that
  still contains the old entry skips it on its next merge instead of
  resurrecting it. Tombstones are never surfaced as entries and fade after one
  round (not re-persisted), so they don't grow the file or trip the drift
  guard (which now also strips tombstone lines before its round-trip check).
- `add` refuses to re-add a tombstoned entry.

### 2. `tools/memory_tool_85858_lock_hardening.patch` (complementary defense)
Windows `msvcrt.locking` now uses non-blocking `LK_NBLCK` with backoff retry +
a 30s timeout, so concurrent writers *queue* for the lock instead of racing,
and can never deadlock. This reduces contention further but is **not required**
for correctness — the merge/tombstone logic is the actual safety net.

## Verification
A faithful standalone model of the background-sync path (two `Provider`s sharing
one `MEMORY.md`, each flushing a stale in-memory snapshot) was used to exercise
the fix. **The model caught three bugs in the first draft** (now fixed):
1. computing `_tombstone_for(e)` over *parsed* entries tombstoned every entry;
2. not filtering the instance's own stale `entries` against tombstones;
3. the live-merge check compared entry *text* against the *marker* set, letting
   removed entries slip back in.

Running the **actual Hermes test suite** then caught a further **4th bug** (and
two test-harness bugs), all fixed:
4. `_detect_external_drift` compared the raw file (containing the tombstone
   line) against the tombstone-stripped round-trip → false "external drift" →
   writes refused. Fixed by comparing the tombstone-free view to its own
   round-trip.
5. `save_to_disk`'s non-merge branch dropped `extra_tombstones`, so tombstones
   never reached disk → resurrection. Fixed: both branches write them.
6. Test bugs: missing `target` arg on `remove`/`replace`; `"secret X" in final`
   matched the prefix of `"secret X'"`. Both fixed.

### In-repo test results (real suite, Hermes checkout)
- New `tests/tools/test_memory_concurrency_85858.py` (3 tests) + existing
  `tests/tools/test_memory_tool.py`: **40 passed**.
- Broader `tests/agent/test_memory_write_bridge.py`,
  `tests/agent/test_memory_provider.py`, `tests/agent/test_memory_async_sync.py`,
  `tests/tools/test_memory_tool_schema.py`,
  `tests/tools/test_memory_tool_import_fallback.py`: **71 passed**.
- Both split patches apply cleanly (no flags) in either order, and the combined
  result passes the full suite above.

### Standalone model results (all pass)
- Concurrent `add`s from two instances → **both preserved** (no loss).
- Instance A `remove`s X; instance B holds a stale snapshot with X and flushes
  → **X is not resurrected** (`['pre', 'from B']`).
- Instance A `replace`s X→X'; instance B holds stale X and flushes → **old X
  gone, X' kept** (`['pre', 'from B', "secret X'"]`).

## Scope / notes
- Only `add` uses `merge_live` (append is safe). `replace`/`remove` use
  tombstones, not merge, because merging there could resurrect removed content.
- The fix is format-compatible: tombstone lines are stripped on parse and by
  the drift guard, so older readers ignore them.
- The data-loss patch (#1) is the required fix; the lock patch (#2) is optional
  hardening and can be reviewed/merged independently.

## Apply
```bash
cd hermes-agent
git apply tools/memory_tool_85858_dataloss_fix.patch
git apply tools/memory_tool_85858_lock_hardening.patch
pytest tests/tools/test_memory_concurrency_85858.py
```
