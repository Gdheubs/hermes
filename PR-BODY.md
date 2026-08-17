<!-- native-links:v1 -->
Related #60900 #60902 #60905 #60908 #60909

## What does this PR do?

Closes the **memory soft-capacity warning** defect class (#60900, #60902, #60905): a memory add that pushes the store to ≥90% of its char limit succeeds **silently**, giving the model no signal to consolidate before hitting the hard cap (where adds fail and the consolidation loop starts).

**Fix:** `_success_response()` now auto-attaches a soft-capacity warning + consolidation recommendation when `current / limit >= 0.9` — covering every successful write path (add, replace, remove, apply_batch) uniformly. Also adds the structured `compact()` directive response from #60909.

This is the class fix from #60908 (author: **giggling-ginger**) plus the polish from #60909 (author: **HEBEI77**), both cherry-picked with authorship preserved; the conflict merge kept main's all-or-nothing semantics docstring and added the uniform threshold check.

## Repro receipt (sha256)

`repro_memory_warn.py` — sha256 `bec18717ee0c45191c92d3473444f42eb068c12743840229ce200e87eaa47a9c`

```bash
# baseline (origin/main @ 9d6c5a920c7)
python repro_memory_warn.py
# FAIL: no warning at 90% capacity   (exit 1)

# this branch
python repro_memory_warn.py
# PASS: no warning below 90%, warning + recommendation at 90%   (exit 0)
```

## How to test

```bash
pytest tests/tools/test_memory_tool.py -q
# 39 passed
```

## What platforms were tested?

- Windows 11 native: `39 passed`, `git diff --check` clean.

## Why this matters to users

The model gets an early, actionable signal to consolidate memory instead of silently running into the hard cap — the exact request filed three times (#60900, #60902, #60905).

Closes #60900
Closes #60902
Closes #60905

- [x] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature
- [ ] Breaking change

## Checklist

- [x] Code follows repo style
- [x] Self-review complete
- [x] Tests added (soft-capacity threshold)
- [x] Suite green: `39 passed` on current main
- [x] `git diff --check` clean
- [x] Repro receipt: baseline FAIL / fixed PASS (sha256 above)
- [x] Credit: implementation from #60908 by @giggling-ginger + #60909 by @HEBEI77, cherry-picked with authorship preserved
