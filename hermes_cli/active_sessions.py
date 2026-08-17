"""Cross-process active chat session leases.

The session database records persisted conversations.  This module records
currently open chat surfaces, including idle CLI/TUI sessions that have not
written a transcript row yet.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _get_terminal_surface_id() -> str:
    """Return a best-effort identifier for the current terminal surface.

    Used to prevent multiple Hermes CLI/TUI instances from sharing one TTY,
    which causes stacked prompt_toolkit status frames on Windows.
    """
    try:
        if sys.platform == "win32":
            import ctypes

            title_buf = ctypes.create_unicode_buffer(1024)
            length = ctypes.windll.kernel32.GetConsoleTitleW(title_buf, 1024)
            if length:
                # Windows Terminal tabs share an HWND and console title, so
                # same-titled tabs collide on the basic key. Prefer the
                # per-tab WT_SESSION GUID when present, else fall back to the
                # console window handle, then the bare title. Each tab gets a
                # distinct key without relying on unstable titles.
                wt_session = os.environ.get("WT_SESSION")
                if wt_session:
                    return f"win32-console:{title_buf.value}:wt:{wt_session}"
                try:
                    kernel32 = ctypes.windll.kernel32
                    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
                    console_hwnd = kernel32.GetConsoleWindow()
                    if console_hwnd:
                        return f"win32-console:{title_buf.value}:hwnd:{console_hwnd}"
                except Exception:
                    pass
                return f"win32-console:{title_buf.value}"
            logger.warning(
                "Terminal surface detection failed (empty console title); "
                "same-surface duplicate-session guard is ineffective this launch"
            )
            return f"win32-pid:{os.getpid()}"
        fd = sys.stdout.fileno()
        return f"tty:{os.ttyname(fd)}"
    except Exception:
        logger.warning(
            "Terminal surface detection raised; same-surface "
            "duplicate-session guard is ineffective this launch",
            exc_info=True,
        )
        return f"pid:{os.getpid()}"


def coerce_max_concurrent_sessions(value: Any, key: str = "max_concurrent_sessions") -> Optional[int]:
    """Return a positive integer cap, or None when disabled/invalid."""
    if value is None:
        return None
    if isinstance(value, bool):
        logger.warning(
            "Ignoring invalid %s=%r (expected a positive integer; 0/null disables)",
            key,
            value,
        )
        return None
    try:
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(value)
            parsed = int(value)
        elif isinstance(value, str):
            parsed = int(value.strip(), 10)
        else:
            parsed = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid %s=%r (expected a positive integer; 0/null disables)",
            key,
            value,
        )
        return None
    if parsed <= 0:
        return None
    return parsed


def resolve_max_concurrent_sessions(config: Any) -> Optional[int]:
    """Resolve top-level max_concurrent_sessions with gateway.* fallback."""
    raw: Any = None
    key = "max_concurrent_sessions"
    if isinstance(config, dict):
        if "max_concurrent_sessions" in config:
            raw = config.get("max_concurrent_sessions")
        else:
            gateway_cfg = config.get("gateway")
            if isinstance(gateway_cfg, dict):
                raw = gateway_cfg.get("max_concurrent_sessions")
                key = "gateway.max_concurrent_sessions"
    else:
        raw = getattr(config, "max_concurrent_sessions", None)
    return coerce_max_concurrent_sessions(raw, key=key)


def format_age(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if not minutes else f"{hours}h{minutes}m"


def summarize_holders(entries: list[dict[str, Any]]) -> str:
    """Compact "who is holding the slots" phrase, e.g. ``desktop x4, cli``."""
    if not entries:
        return ""
    counts: dict[str, int] = {}
    for entry in entries:
        surface = str(entry.get("surface") or "unknown")
        counts[surface] = counts.get(surface, 0) + 1
    held = ", ".join(
        f"{surface} x{n}" if n > 1 else surface
        for surface, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    started = [t for t in (_optional_float(e.get("started_at")) for e in entries) if t]
    if started:
        held += f", oldest {format_age(time.time() - min(started))} ago"
    return held


def active_session_limit_message(
    active_count: int,
    max_sessions: int,
    entries: Optional[list[dict[str, Any]]] = None,
) -> str:
    # Name the holders: the slots are shared across CLI, desktop/TUI and the
    # messaging gateway, so the surface that gets rejected is usually NOT the
    # one squatting on them (idle desktop chats starving a Discord bot, say).
    # Without this the message is unactionable and the only way to find out is
    # reading runtime/active_sessions.json by hand.
    held = summarize_holders(entries or [])
    detail = f" Held by: {held}." if held else ""
    return (
        f"Hermes is at the active session limit ({active_count}/{max_sessions})."
        f"{detail} Try again when another session finishes."
    )


def _state_dir() -> Path:
    return Path(get_hermes_home()) / "runtime"


def _state_path() -> Path:
    return _state_dir() / "active_sessions.json"


def _lock_path() -> Path:
    return _state_dir() / "active_sessions.lock"


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        if os.name == "nt":
            try:
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            except Exception as exc:
                self._fh.close()
                self._fh = None
                raise RuntimeError("active session file lock unavailable") from exc
        else:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            except Exception as exc:
                self._fh.close()
                self._fh = None
                raise RuntimeError("active session file lock unavailable") from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is None:
            return
        if os.name == "nt":
            try:
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        else:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            self._fh.close()
        finally:
            self._fh = None


def _read_entries(path: Path) -> list[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except Exception:
        logger.warning("Ignoring corrupt active session registry at %s", path)
        return []
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _write_entries(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"entries": entries}, fh, sort_keys=True)
    os.replace(tmp, path)


def _process_start_time(pid: int) -> Optional[float]:
    # Pair pid with process create_time when psutil can read it, so a recycled
    # pid does not keep a stale lease alive indefinitely.
    try:
        import psutil  # type: ignore

        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_pid_exists():
    """Lazy import of the authoritative cross-platform PID-existence check.

    active_sessions no longer does ``from gateway.status import _pid_exists``
    per-call.  The binding is resolved once at first use and reused, so the
    import machinery (and its failure modes: circular import, syntax error,
    missing transitive dep, scaffold race) is exercised at most once per
    process instead of on every ``_pid_alive`` call.

    If the import fails, ``_PID_EXISTS`` is never bound and ``_pid_alive``
    treats the PID as *unknown* — it fails closed (reports alive) so
    ``_prune_dead`` spares every entry rather than wiping the registry.
    """
    global _PID_EXISTS
    from gateway.status import _pid_exists as _PID_EXISTS  # noqa: WPS436


def _pid_alive(pid: Any, process_start_time: Any = None) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        _resolve_pid_exists()
    except Exception:
        # Checker unavailable: we cannot prove the PID dead. Report alive
        # so _prune_dead spares entries instead of wiping the registry on
        # an import failure in gateway.status.
        return True
    try:
        exists = bool(_PID_EXISTS(pid_int))
    except Exception:
        return False
    if not exists:
        return False
    expected_start = _optional_float(process_start_time)
    if expected_start is None:
        return True
    current_start = _process_start_time(pid_int)
    if current_start is None:
        return True
    return abs(current_start - expected_start) < 0.001


def _prune_dead(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if _pid_alive(entry.get("pid"), entry.get("process_start_time"))
    ]


@dataclass
class ActiveSessionLease:
    lease_id: str
    session_id: str
    surface: str
    enabled: bool = True
    released: bool = False
    # Registry paths pinned at acquisition time. A lease acquired under the
    # root ``HERMES_HOME`` must release against the same registry even when
    # ``release()`` runs inside a profile home override (native multiplex
    # routes turns under ``_profile_runtime_scope``), otherwise the root
    # entry survives until process exit and the session cap fills with
    # phantom leases (#85431).
    state_path: Optional[Path] = None
    lock_path: Optional[Path] = None

    def release(self) -> None:
        if self.released or not self.enabled:
            return
        release_active_session(self)


def _normalize_surface(terminal_surface: str) -> str:
    """Strip unstable per-pseudoconsole HWND suffixes for collision checks.

    On Windows ConPTY, older code paths included ``:hwnd:<handle>`` in the
    surface ID. Each ConPTY instance gets a unique HWND, so two Hermes
    processes in the same terminal tab compute different surface IDs and
    bypass the duplicate-session check. Normalizing by removing the HWND
    suffix makes old and new entries collide correctly.

    Windows Terminal entries carry a ``:wt:<guid>`` per-tab key instead; those
    are left untouched by :func:`_surfaces_collide` so two same-titled WT tabs
    (each with its own GUID) never falsely collide.
    """
    return re.sub(r":hwnd:[0-9a-fA-F]+$", "", terminal_surface)


def _surfaces_collide(a: str, b: str) -> bool:
    """True when two terminal-surface ids denote the same surface.

    Exact match always collides. Otherwise HWND suffixes are stripped on both
    sides so legacy ``:hwnd:`` entries still collide with new-format ids — but
    only when neither side carries a ``:wt:`` tab GUID (a WT GUID is already
    tab-unique; stripping around it would merge distinct tabs of the same
    title).
    """
    if a == b:
        return True
    if ":wt:" in a or ":wt:" in b:
        return False
    return _normalize_surface(a) == _normalize_surface(b)


def try_acquire_active_session(
    *,
    session_id: str,
    surface: str,
    config: Any,
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[Optional[ActiveSessionLease], Optional[str]]:
    """Acquire an active-session slot.

    Returns ``(lease, None)`` on success.  When the cap is disabled, the lease is
    a lightweight no-op that still records the surface in the registry so that
    concurrent CLI/TUI instances on the same terminal surface can be detected
    and refused (prevents stacked prompt_toolkit status frames).
    """
    max_sessions = resolve_max_concurrent_sessions(config)
    lease_id = uuid.uuid4().hex
    terminal_surface = _get_terminal_surface_id()
    now = time.time()
    entry = {
        "lease_id": lease_id,
        "session_id": str(session_id),
        "surface": str(surface),
        "terminal_surface": terminal_surface,
        "pid": os.getpid(),
        "process_start_time": _process_start_time(os.getpid()),
        "started_at": now,
        "updated_at": now,
    }
    if metadata:
        entry["metadata"] = {
            str(k): v for k, v in metadata.items() if isinstance(k, str)
        }

    state_path = _state_path()
    with _FileLock(_lock_path()):
        raw_entries = _read_entries(state_path)
        entries = _prune_dead(raw_entries)
        pruned = len(raw_entries) - len(entries)
        if pruned:
            logger.info("Pruned %d stale active session lease(s)", pruned)

        same_surface_holder = next(
            (
                e
                for e in entries
                if _surfaces_collide(e.get("terminal_surface", ""), terminal_surface)
                and e.get("pid") != os.getpid()
            ),
            None,
        )
        if same_surface_holder:
            _write_entries(state_path, entries)
            return None, (
                "Another Hermes session is already using this terminal surface. "
                "Close the other session first, or switch to a different terminal."
            )

        if max_sessions is not None and len(entries) >= max_sessions:
            _write_entries(state_path, entries)
            logger.info(
                "Active session limit reached: active=%d max=%d surface=%s",
                len(entries),
                max_sessions,
                surface,
            )
            return None, active_session_limit_message(
                len(entries), max_sessions, entries
            )
        entries.append(entry)
        _write_entries(state_path, entries)

    return ActiveSessionLease(
        lease_id=lease_id,
        session_id=str(session_id),
        surface=str(surface),
        enabled=max_sessions is not None,
        state_path=state_path,
        lock_path=_lock_path(),
    ), None


def release_active_session(lease: ActiveSessionLease) -> None:
    # Prefer the registry the lease was acquired against: the caller may be
    # running under a profile HERMES_HOME override (#85431).
    state_path = lease.state_path or _state_path()
    try:
        with _FileLock(lease.lock_path or _lock_path()):
            entries = _prune_dead(_read_entries(state_path))
            kept = [
                entry
                for entry in entries
                if str(entry.get("lease_id") or "") != lease.lease_id
            ]
            if len(kept) != len(entries):
                _write_entries(state_path, kept)
    finally:
        lease.released = True


def transfer_active_session(
    lease: ActiveSessionLease,
    *,
    session_id: str,
    metadata: Optional[dict[str, Any]] = None,
) -> bool:
    """Move an existing lease to a new session id without dropping the slot."""
    new_session_id = str(session_id or "")
    if not new_session_id:
        return False
    if lease.released:
        return False
    if not lease.enabled:
        lease.session_id = new_session_id
        return True

    state_path = lease.state_path or _state_path()
    with _FileLock(lease.lock_path or _lock_path()):
        entries = _prune_dead(_read_entries(state_path))
        updated = False
        for entry in entries:
            if str(entry.get("lease_id") or "") != lease.lease_id:
                continue
            entry["session_id"] = new_session_id
            entry["updated_at"] = time.time()
            if metadata:
                entry["metadata"] = {
                    str(k): v for k, v in metadata.items() if isinstance(k, str)
                }
            updated = True
            break
        if updated:
            _write_entries(state_path, entries)
            lease.session_id = new_session_id
        return updated


def release_orphaned_leases(live_lease_ids: set[str]) -> int:
    """Drop this process's registry entries that no live session owns.

    ``_prune_dead`` only reclaims leases whose owning process died. A server
    that runs for days (``hermes dashboard`` / ``serve``) never trips that
    check, so a lease whose session skipped teardown is held until restart.
    The owning process is the only authority on which of its own leases are
    real, so it drops the rest itself — exact, with no heartbeat write on the
    turn path and no staleness threshold to tune.
    """
    pid = os.getpid()
    state_path = _state_path()
    # With the cap disabled the registry is never written, so don't take a lock
    # (or create its file) on the idle-reaper tick for the majority of installs.
    if not state_path.exists():
        return 0
    with _FileLock(_lock_path()):
        entries = _prune_dead(_read_entries(state_path))
        kept = [
            entry
            for entry in entries
            if entry.get("pid") != pid
            or str(entry.get("lease_id") or "") in live_lease_ids
        ]
        dropped = len(entries) - len(kept)
        if dropped:
            _write_entries(state_path, kept)
    return dropped


def active_session_registry_snapshot() -> list[dict[str, Any]]:
    """Return the pruned active-session registry for diagnostics/tests."""
    state_path = _state_path()
    with _FileLock(_lock_path()):
        entries = _prune_dead(_read_entries(state_path))
        _write_entries(state_path, entries)
        return entries
