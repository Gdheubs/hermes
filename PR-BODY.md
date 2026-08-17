<!-- native-links:v1 -->
Related #24277 #24278 #24309

## What does this PR do?

Closes the **Windows MSYS special-symbol display** defect class (#24277, #24278): decorative Unicode (┊, ⚡, ❯, spinner braille) renders as `?` question marks in MSYS/mingw terminals and the Windows console. 

**Fix:** new `hermes_cli/display_compat.py` — `terminal_prefers_ascii()` detects MSYS/mingw (or `HERMES_FORCE_ASCII_DISPLAY`), and display paths (skin tool prefix, tool emoji, waiting/thinking faces, spinner frames, tool messages) downgrade to ASCII when the terminal needs it. This is the fix from #24309 (author: **LeonSGP43**, cherry-picked with authorship preserved), merged onto the current display module (which gained the redaction + tool-classification imports since the PR was written).

## How to test

```bash
pytest tests/agent/test_display.py tests/hermes_cli/test_skin_engine.py tests/cli/test_cprint_bg_thread.py -q
# 47 passed
```

The ascii-fallback tests (`HERMES_FORCE_ASCII_DISPLAY=1` → spinner `- \ | /`, tool message `*` prefix) verify the fix path; the skin-prefix tests pin Unicode rendering via an autouse fixture (the preference is True on MSYS hosts, so the Unicode assertions must opt out).

## What platforms were tested?

- Windows 11 native (MSYS): `47 passed`, `git diff --check` clean.

## Why this matters to users

Windows/MSYS users see readable ASCII symbols instead of `?` question marks — the exact report filed twice (#24277, #24278).

Closes #24277
Closes #24278

- [x] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature
- [ ] Breaking change

## Checklist

- [x] Code follows repo style
- [x] Self-review complete
- [x] Tests added (ascii fallback + Unicode pinning fixture)
- [x] Suite green: `47 passed` on current main
- [x] `git diff --check` clean
- [x] Credit: implementation from #24309 by @LeonSGP43, cherry-picked with authorship preserved
