#!/usr/bin/env python3
"""Repro: WhatsApp bridge only checks `node` on PATH, misses usable macOS runtimes.

Bug class: #2975 / #2976 — check_whatsapp_requirements() only ran `node
--version` from PATH. On macOS systems without a standalone Node install
but with VS Code present (which bundles a usable Node runtime), the
WhatsApp bridge refused to start.

On main: FAILS (no PATH node -> requirements unmet, even with VS Code).
With the fix: PASSES (VS Code bundled runtime is a fallback candidate).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plugins.platforms.whatsapp.adapter import _resolve_node_command  # noqa: E402

resolved = _resolve_node_command()
print(f"resolved node command: {resolved[0] if resolved else None!r}")
if resolved is None:
    print("FAIL: no node command resolved (PATH node missing, no fallback)")
    sys.exit(1)

# The resolved command must actually run
import subprocess
r = subprocess.run([resolved[0], "--version"], capture_output=True, text=True, timeout=5)
if r.returncode != 0:
    print(f"FAIL: resolved command does not run (exit {r.returncode})")
    sys.exit(1)

print(f"PASS: node command resolves and runs (exit 0): {r.stdout.strip()}")
sys.exit(0)
