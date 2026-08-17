<!-- native-links:v1 -->
Related #32196 #42084 #42141

## What does this PR do?

Closes the **Weixin Silk voice STT** defect class (#32196, #42084): WeChat voice messages arrive in Silk format, which STT backends (Whisper, faster-whisper) cannot decode — the downloaded `.silk` file silently failed transcription, breaking non-Chinese voice messages.

**Fix:** `_download_voice()` converts Silk→WAV via `pilk` (same approach as the QQBot adapter), reports `audio/wav` media type for converted files, and falls back to the raw `.silk` file only if conversion fails (with a warning naming the `pilk` install). This is the fix from #42141 (author: **annguyenNous**, cherry-picked with authorship preserved), merged onto the current adapter — the always-download behavior it relied on was already landed by #27300, so only the conversion portion is new.

## Repro receipt (sha256)

`repro_silk.py` — sha256 `c99a37769e249922a19646e8ca028be0b27843cacbc6471b314f5027861fdcf4`

```bash
# baseline (origin/main @ 9d6c5a920c7)
python repro_silk.py
# FAIL: _download_voice has no Silk->WAV conversion   (exit 1)

# this branch
python repro_silk.py
# PASS: _download_voice converts Silk->WAV via pilk
# PASS: _collect_media reports audio/wav for converted files   (exit 0)
```

## How to test

```bash
pytest tests/gateway/test_weixin.py -q
# 29 passed, 1 pre-existing failure (test_qr_login_timeout_uses_monotonic_clock — verified identical on origin/main baseline)
```

## What platforms were tested?

- Windows 11 native: `29 passed`; the 1 failure is pre-existing on `origin/main` (baseline worktree verified) — zero regressions.

## Why this matters to users

Weixin voice messages (especially non-Chinese) get properly transcribed by the configured STT backend instead of silently failing on undecodable Silk — the exact report filed twice (#32196, #42084).

Closes #32196
Closes #42084

- [x] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature
- [ ] Breaking change

## Checklist

- [x] Code follows repo style
- [x] Self-review complete
- [x] Repro receipt: baseline FAIL / fixed PASS (sha256 above)
- [x] Suite green: no regressions vs baseline (1 pre-existing failure identical)
- [x] `git diff --check` clean
- [x] Credit: implementation from #42141 by @annguyenNous, cherry-picked with authorship preserved
