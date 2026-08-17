<!-- native-links:v1 -->
Related #11489 #11493 #17256

## What does this PR do?

Closes the **QQ Bot WebSocket session timeout / gateway URL fetch failure** defect class (#11489, #11493): the adapter fetched the gateway URL once with no retry and no fallback — a transient fetch failure killed the WebSocket session with no recovery.

**Fix:** retry the gateway URL fetch with backoff, and fall back to the last cached URL when retries fail, so a transient gateway outage doesn't kill the session. This is the fix from #17256 (author: **杜文持0668001110**, cherry-picked with authorship preserved), which was closed unmerged.

## How to test

```bash
pytest tests/gateway/test_qqbot.py -q
# 62 passed, 1 pre-existing failure (test_open_ws_honors_proxy_env — verified identical on origin/main baseline)
```

## What platforms were tested?

- Windows 11 native: `62 passed`; the 1 failure is pre-existing on `origin/main` (baseline worktree verified) — zero regressions.

## Why this matters to users

QQ Bot sessions survive transient gateway URL fetch failures instead of dying with a WebSocket timeout — the exact report filed twice (#11489, #11493).

Closes #11489
Closes #11493

- [x] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature
- [ ] Breaking change

## Checklist

- [x] Code follows repo style
- [x] Self-review complete
- [x] Tests added (retry-then-succeed, cached-fallback)
- [x] Suite green: no regressions vs baseline (1 pre-existing failure identical)
- [x] `git diff --check` clean
- [x] Credit: implementation from #17256 by @杜文持0668001110, cherry-picked with authorship preserved
