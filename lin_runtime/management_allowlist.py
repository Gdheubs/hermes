"""Allowlist for Lin's internal Hermes management proxy.

The native Hermes dashboard remains the implementation. This module only
classifies routes that Lin may proxy; it does not duplicate dashboard logic.
"""

from __future__ import annotations

ALLOWED_PREFIXES = (
    "/api/config",
    "/api/model",
    "/api/skills",
    "/api/tools/toolsets",
    "/api/mcp",
    "/api/auth",
    "/assets/",
    "/fonts/",
    "/fonts-terminal/",
    "/favicon.ico",
)


def is_allowed_management_path(path: str) -> bool:
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in ALLOWED_PREFIXES)
