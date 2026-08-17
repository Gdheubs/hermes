#!/usr/bin/env python3
"""Repro: MCP surface exposes broken approval tools.

Bug class: #21558 / #21563 / #22001 — permissions_list_open /
permissions_respond are exposed on the MCP bridge surface but the
gateway approval state is process-local with no truthful IPC path, so
the tools silently report empty/failed results.

On main: FAILS (broken tools still exposed). With the fix: PASSES
(tools removed; surface lists only honest tools).
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_serve  # noqa: E402

src = inspect.getsource(mcp_serve)

# The bridge must not advertise tools it cannot truthfully serve
for tool in ("permissions_list_open", "permissions_respond"):
    if tool in src:
        print(f"FAIL: broken tool still exposed: {tool}")
        sys.exit(1)

# The docstring must not list them either
doc = inspect.getdoc(mcp_serve.create_mcp_server) or ""
if "permissions_list_open" in doc or "permissions_respond" in doc:
    print("FAIL: docstring still advertises broken tools")
    sys.exit(1)

print("PASS: broken approval tools removed from MCP surface")
print("PASS: docstring documents the intentional non-exposure")
sys.exit(0)
