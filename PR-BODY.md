<!-- native-links:v1 -->
Related #21558 #21563 #22001 #22300

## What does this PR do?

Closes the **MCP broken approval tools** defect class (#21558, #21563, #22001): `permissions_list_open` / `permissions_respond` were exposed on the MCP bridge surface, but Hermes' gateway approval state is **process-local** — the bridge has no truthful IPC path for listing or resolving approvals. The tools silently reported empty/failed results, making the surface dishonest.

**Fix:** remove the two broken tools from the MCP surface and document the intentional non-exposure (the tools can return once a real gateway IPC path exists). This is the fix from #22300 (author: **LeonSGP43**, cherry-picked with authorship preserved), applied surgically on current main — the newer poll/baseline logic is untouched.

## Repro receipt (sha256)

`repro_mcp_approvals.py` — sha256 `421ab3a3c0253edeae54276b8eb9905e91d2a58ab32bf20e629ce28aaabfb22b`

```bash
# baseline (origin/main @ 9d6c5a920c7)
python repro_mcp_approvals.py
# FAIL: broken tool still exposed: permissions_list_open   (exit 1)

# this branch
python repro_mcp_approvals.py
# PASS: broken approval tools removed from MCP surface
# PASS: docstring documents the intentional non-exposure   (exit 0)
```

## How to test

```bash
pytest tests/test_mcp_serve.py -q
# 80 passed; the 2 TestEventBridgePollE2E failures are pre-existing ordering flakes
# (verified: the same 2 failures appear on the origin/main baseline worktree,
# and each passes in isolation on both)
```

## What platforms were tested?

- Windows 11 native: suite green minus the identical pre-existing flakes; `git diff --check` clean.

## Why this matters to users

MCP clients no longer see tools that silently fail — they get an honest surface with only the tools the bridge can actually serve. The report was filed three times (#21558, #21563, #22001).

Closes #21563
Closes #22001

- [x] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature
- [ ] Breaking change

## Checklist

- [x] Code follows repo style
- [x] Self-review complete
- [x] Tests updated (TestE2EPermissions removed with the tools)
- [x] Suite green: no regressions vs baseline (identical pre-existing flakes)
- [x] `git diff --check` clean
- [x] Repro receipt: baseline FAIL / fixed PASS (sha256 above)
- [x] Credit: implementation from #22300 by @LeonSGP43, cherry-picked with authorship preserved
