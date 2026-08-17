"""Gateway-side fixed action-buttons primitive.

Separate from ``clarify_gateway`` because this flow is intentionally *not*
open-ended: the user must choose one of the predefined actions, and the
Telegram callback payload resolves directly to the full selected text.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class _ActionButtonsEntry:
    action_id: str
    session_key: str
    question: str
    choices: List[str]
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[str] = None


_lock = threading.RLock()
_entries: Dict[str, _ActionButtonsEntry] = {}
_session_index: Dict[str, List[str]] = {}


def register(action_id: str, session_key: str, question: str, choices: List[str]) -> _ActionButtonsEntry:
    entry = _ActionButtonsEntry(
        action_id=action_id,
        session_key=session_key,
        question=question,
        choices=list(choices or []),
    )
    with _lock:
        _entries[action_id] = entry
        _session_index.setdefault(session_key, []).append(action_id)
    return entry


def get_entry(action_id: str) -> Optional[_ActionButtonsEntry]:
    """Return a pending entry without resolving it."""
    with _lock:
        return _entries.get(action_id)


def wait_for_response(action_id: str, timeout: float) -> Optional[str]:
    with _lock:
        entry = _entries.get(action_id)
    if entry is None:
        return None

    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # pragma: no cover
        touch_activity_if_due = None

    deadline = time.monotonic() + max(timeout, 0.0)
    activity_state = {"last_touch": time.monotonic(), "start": time.monotonic()}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if entry.event.wait(timeout=min(1.0, remaining)):
            break
        if touch_activity_if_due is not None:
            touch_activity_if_due(activity_state, "waiting for user action-button response")

    with _lock:
        _entries.pop(action_id, None)
        ids = _session_index.get(entry.session_key)
        if ids and action_id in ids:
            ids.remove(action_id)
            if not ids:
                _session_index.pop(entry.session_key, None)
    return entry.response


def resolve(action_id: str, response: str) -> bool:
    with _lock:
        entry = _entries.get(action_id)
        if entry is None:
            return False
    entry.response = str(response) if response is not None else ""
    entry.event.set()
    return True


def resolve_gateway_action_buttons(action_id: str, response: str) -> bool:
    """Backward-compatible explicit gateway resolver name."""
    return resolve(action_id, response)


def cancel(action_id: str) -> bool:
    """Cancel one pending action-button prompt and unblock waiters."""
    with _lock:
        entry = _entries.pop(action_id, None)
        if entry is None:
            return False
        ids = _session_index.get(entry.session_key)
        if ids and action_id in ids:
            ids.remove(action_id)
            if not ids:
                _session_index.pop(entry.session_key, None)
    entry.response = ""
    entry.event.set()
    return True


def clear_session(session_key: str) -> int:
    with _lock:
        ids = list(_session_index.pop(session_key, []) or [])
        entries = [_entries.pop(aid, None) for aid in ids]
    cancelled = 0
    for entry in entries:
        if entry is None:
            continue
        entry.response = ""
        entry.event.set()
        cancelled += 1
    return cancelled


def get_action_buttons_timeout() -> int:
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        agent_cfg = cfg.get("agent", {}) or {}
        return int(agent_cfg.get("clarify_timeout", 600))
    except Exception:
        return 600
