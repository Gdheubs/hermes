"""Cross-process publication of the LSP service's live status.

``LSPService`` runs inside whichever process embeds the agent (CLI,
gateway, TUI backend, ...).  ``hermes lsp status`` is normally invoked as
its *own*, separate process — calling :func:`agent.lsp.get_service` there
would spin up a brand-new, disconnected service with zero real clients
rather than showing what the actual running process is doing.  Instead,
the owning process publishes its :meth:`agent.lsp.manager.LSPService.get_status`
snapshot here on every meaningful lifecycle change, and the CLI just reads
the file.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger("agent.lsp.status")

_STATUS_FILENAME = "lsp-status.json"


def status_path() -> Path:
    """Return the path to the cross-process LSP status snapshot."""
    return get_hermes_home() / "runtime" / _STATUS_FILENAME


def write_lsp_status(payload: Dict[str, Any]) -> None:
    """Atomically persist ``payload`` (an ``LSPService.get_status()`` snapshot).

    Best-effort — a write failure (read-only HERMES_HOME, disk full) must
    never break the LSP service itself.
    """
    record = dict(payload)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        atomic_json_write(status_path(), record, indent=None, separators=(",", ":"))
    except Exception as e:  # noqa: BLE001
        logger.debug("failed to publish LSP status snapshot: %s", e)


def read_lsp_status() -> Optional[Dict[str, Any]]:
    """Read the persisted cross-process LSP status snapshot, or ``None``."""
    try:
        raw = status_path().read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["status_path", "write_lsp_status", "read_lsp_status"]
