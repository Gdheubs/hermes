"""MCP tool-name filter normalization and matching helpers."""

import fnmatch
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _normalize_name_filter(value: Any, label: str) -> set[str]:
    """Normalize include/exclude config to a set of tool-name patterns.

    Entries may be exact tool names or fnmatch-style globs
    (``*_radar_*``, ``get_zones_*``). Matching happens in
    :func:`matches_name_filter`.
    """
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value}
    logger.warning("MCP config %s must be a string or list of strings; ignoring %r", label, value)
    return set()


def matches_name_filter(tool_name: str, patterns: set[str]) -> bool:
    """True if ``tool_name`` matches any entry in ``patterns``.

    Exact names match literally; entries containing fnmatch metacharacters
    (``*``, ``?``, ``[``) match as case-sensitive globs — the same pattern
    semantics as ``approvals.deny``. Exact membership is checked first so
    large literal lists stay O(1).
    """
    if not patterns:
        return False
    if tool_name in patterns:
        return True
    return any(
        fnmatch.fnmatchcase(tool_name, p)
        for p in patterns
        if "*" in p or "?" in p or "[" in p
    )
