"""Bounded process-memory telemetry and fail-closed restart guard.

The guard is deliberately local-only: it writes redacted counters beneath the
active HERMES_HOME and never exports telemetry.  It monitors the Hermes Python
process separately from descendant tool processes so a healthy 300 MB backend
is not confused with a multi-GB model download.
"""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_MIB = 1024 * 1024

# Canonical descendant-tree memory thresholds and hard-limit confirmation
# count, shared with tools.process_registry so per-process caps cannot drift
# from the gateway guard (single source of truth).
DESCENDANT_WARN_RSS_MB = 8192
DESCENDANT_HARD_RSS_MB = 24576
HARD_LIMIT_CONFIRMATIONS = 2


@dataclass(frozen=True)
class ResourceGuardSettings:
    enabled: bool = True
    poll_seconds: float = 15.0
    telemetry_enabled: bool = False
    telemetry_seconds: float = 60.0
    warn_rss_mb: int = 2048
    snapshot_rss_mb: int = 4096
    hard_rss_mb: int = 8192
    descendant_warn_rss_mb: int = DESCENDANT_WARN_RSS_MB
    descendant_hard_rss_mb: int = DESCENDANT_HARD_RSS_MB
    hard_limit_confirmations: int = HARD_LIMIT_CONFIRMATIONS
    snapshot_cooldown_seconds: float = 300.0


def _positive_float(value: Any, default: float, *, floor: float = 1.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return max(floor, float(value))
    except (TypeError, ValueError):
        return default


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def load_resource_guard_settings() -> ResourceGuardSettings:
    """Read resource_guard config with safe defaults and ordered thresholds."""
    raw: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        candidate = config.get("resource_guard") if isinstance(config, dict) else None
        if isinstance(candidate, dict):
            raw = candidate
    except Exception:
        pass

    defaults = ResourceGuardSettings()
    warn = _positive_int(raw.get("warn_rss_mb"), defaults.warn_rss_mb)
    snapshot = max(
        warn,
        _positive_int(raw.get("snapshot_rss_mb"), defaults.snapshot_rss_mb),
    )
    hard = max(
        snapshot,
        _positive_int(raw.get("hard_rss_mb"), defaults.hard_rss_mb),
    )
    descendant_warn = _positive_int(
        raw.get("descendant_warn_rss_mb"), defaults.descendant_warn_rss_mb
    )
    descendant_hard = max(
        descendant_warn,
        _positive_int(
            raw.get("descendant_hard_rss_mb"), defaults.descendant_hard_rss_mb
        ),
    )
    return ResourceGuardSettings(
        enabled=raw.get("enabled", defaults.enabled) is not False,
        telemetry_enabled=(
            raw.get("telemetry_enabled", defaults.telemetry_enabled) is True
        ),
        poll_seconds=_positive_float(
            raw.get("poll_seconds"), defaults.poll_seconds, floor=2.0
        ),
        telemetry_seconds=_positive_float(
            raw.get("telemetry_seconds"), defaults.telemetry_seconds, floor=5.0
        ),
        warn_rss_mb=warn,
        snapshot_rss_mb=snapshot,
        hard_rss_mb=hard,
        descendant_warn_rss_mb=descendant_warn,
        descendant_hard_rss_mb=descendant_hard,
        hard_limit_confirmations=_positive_int(
            raw.get("hard_limit_confirmations"),
            defaults.hard_limit_confirmations,
        ),
        snapshot_cooldown_seconds=_positive_float(
            raw.get("snapshot_cooldown_seconds"),
            defaults.snapshot_cooldown_seconds,
            floor=10.0,
        ),
    )


def collect_process_memory_snapshot(
    metrics_fn: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Collect redacted process and descendant counters using psutil."""
    import psutil

    proc = psutil.Process(os.getpid())
    parent_rss = int(proc.memory_info().rss)
    children = []
    descendant_rss = 0
    try:
        descendants = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        descendants = []
    for child in descendants:
        try:
            rss = int(child.memory_info().rss)
            descendant_rss += rss
            children.append({"pid": child.pid, "rss_bytes": rss, "name": child.name()})
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    children.sort(key=lambda row: row["rss_bytes"], reverse=True)

    snapshot: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "rss_bytes": parent_rss,
        "descendant_rss_bytes": descendant_rss,
        "descendant_count": len(children),
        "largest_descendants": children[:10],
        "thread_count": threading.active_count(),
    }
    try:
        vm = psutil.virtual_memory()
        snapshot["host_available_bytes"] = int(vm.available)
        snapshot["host_memory_percent"] = float(vm.percent)
    except Exception:
        pass
    if metrics_fn is not None:
        try:
            extra = metrics_fn()
            if isinstance(extra, dict):
                snapshot["hermes"] = extra
        except Exception as exc:
            snapshot["metrics_error"] = type(exc).__name__
    return snapshot


def _append_telemetry(snapshot: dict[str, Any]) -> None:
    log_dir = get_hermes_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "memory-telemetry.jsonl"
    try:
        if path.exists() and path.stat().st_size > 10 * 1024 * 1024:
            rotated = path.with_suffix(".jsonl.1")
            try:
                rotated.unlink(missing_ok=True)
            except OSError:
                pass
            path.replace(rotated)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
    except OSError:
        logger.debug("Could not append memory telemetry", exc_info=True)


def _write_evidence_snapshot(snapshot: dict[str, Any], reason: str) -> Path | None:
    evidence_dir = get_hermes_home() / "logs" / "memory-snapshots"
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = f"{stamp}-pid{os.getpid()}-{reason}"
        json_path = evidence_dir / f"{stem}.json"
        tmp_path = evidence_dir / f".{stem}.tmp"
        tmp_path.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, json_path)
        stack_path = evidence_dir / f"{stem}-threads.log"
        with stack_path.open("w", encoding="utf-8") as handle:
            faulthandler.dump_traceback(file=handle, all_threads=True)
        return json_path
    except OSError:
        logger.exception("Could not write memory evidence snapshot")
        return None


class ProcessMemoryGuard:
    """Daemon monitor that captures evidence and requests a graceful restart."""

    def __init__(
        self,
        *,
        component: str,
        metrics_fn: Callable[[], dict[str, Any]] | None = None,
        on_hard_limit: Callable[[dict[str, Any]], None] | None = None,
        settings: ResourceGuardSettings | None = None,
    ) -> None:
        self.component = component
        self.metrics_fn = metrics_fn
        self.on_hard_limit = on_hard_limit
        self.settings = settings or load_resource_guard_settings()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned = False
        self._descendant_warned = False
        self._hard_fired = False
        self._hard_violations = 0
        self._last_snapshot_at = 0.0
        self._last_telemetry_at = 0.0

    def start(self) -> "ProcessMemoryGuard":
        if not self.settings.enabled or self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"hermes-memory-guard-{self.component}",
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(2.0, self.settings.poll_seconds))

    def _run(self) -> None:
        while not self._stop.wait(self.settings.poll_seconds):
            try:
                self._sample()
            except Exception:
                logger.exception("Resource guard sample failed for %s", self.component)

    def _sample(self) -> dict[str, Any]:
        snapshot = collect_process_memory_snapshot(self.metrics_fn)
        snapshot["component"] = self.component
        now = time.monotonic()
        rss_mb = snapshot["rss_bytes"] / _MIB
        descendant_mb = snapshot["descendant_rss_bytes"] / _MIB

        if (
            self.settings.telemetry_enabled
            and now - self._last_telemetry_at >= self.settings.telemetry_seconds
        ):
            _append_telemetry(snapshot)
            self._last_telemetry_at = now

        if rss_mb >= self.settings.warn_rss_mb and not self._warned:
            logger.warning(
                "Hermes memory guard: component=%s rss_mb=%.1f warn_mb=%d "
                "descendant_rss_mb=%.1f",
                self.component,
                rss_mb,
                self.settings.warn_rss_mb,
                descendant_mb,
            )
            self._warned = True
        elif rss_mb < self.settings.warn_rss_mb * 0.8:
            self._warned = False

        if (
            descendant_mb >= self.settings.descendant_warn_rss_mb
            and not self._descendant_warned
        ):
            logger.warning(
                "Hermes descendant memory warning: component=%s rss_mb=%.1f "
                "warn_mb=%d count=%d",
                self.component,
                descendant_mb,
                self.settings.descendant_warn_rss_mb,
                snapshot["descendant_count"],
            )
            self._descendant_warned = True
        elif descendant_mb < self.settings.descendant_warn_rss_mb * 0.8:
            self._descendant_warned = False

        snapshot_due = rss_mb >= self.settings.snapshot_rss_mb and (
            now - self._last_snapshot_at >= self.settings.snapshot_cooldown_seconds
        )
        if snapshot_due:
            _write_evidence_snapshot(snapshot, "snapshot")
            self._last_snapshot_at = now

        hard_parent = rss_mb >= self.settings.hard_rss_mb
        hard_descendants = descendant_mb >= self.settings.descendant_hard_rss_mb
        if hard_parent or hard_descendants:
            self._hard_violations += 1
        else:
            self._hard_violations = 0
        if (
            self._hard_violations >= self.settings.hard_limit_confirmations
            and not self._hard_fired
        ):
            reason = "hard-parent" if hard_parent else "hard-descendants"
            evidence = _write_evidence_snapshot(snapshot, reason)
            logger.error(
                "Hermes memory guard hard limit: component=%s rss_mb=%.1f "
                "descendant_rss_mb=%.1f evidence=%s; refusing new work and "
                "requesting graceful restart",
                self.component,
                rss_mb,
                descendant_mb,
                evidence,
            )
            self._hard_fired = True
            if self.on_hard_limit is not None:
                self.on_hard_limit(snapshot)
        return snapshot


__all__ = [
    "DESCENDANT_WARN_RSS_MB",
    "DESCENDANT_HARD_RSS_MB",
    "HARD_LIMIT_CONFIRMATIONS",
    "ProcessMemoryGuard",
    "ResourceGuardSettings",
    "collect_process_memory_snapshot",
    "load_resource_guard_settings",
]
