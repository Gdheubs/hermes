"""CPU profiling support for Hermes CLI entrypoints.

The profiler is intentionally process-scoped: the CLI enables it once after
argument parsing and writes the profile at interpreter shutdown. This covers
long-running commands such as ``gateway run`` and regular short commands
without forcing every command handler through a wrapper.
"""

from __future__ import annotations

import atexit
import cProfile
import logging
import os
import pstats
from pathlib import Path

logger = logging.getLogger(__name__)

_profiler: cProfile.Profile | None = None
_profile_path: Path | None = None


def enable_cpu_profile(path: str | os.PathLike[str] | None) -> Path | None:
    """Start process-wide CPU profiling and dump pstats data on exit.

    Returns the resolved profile path when profiling was enabled. Repeated calls
    are idempotent so fast-path/full-parser handoffs and tests cannot register
    duplicate atexit writers.
    """

    global _profiler, _profile_path
    if not path:
        return None
    target = Path(path).expanduser().resolve()
    if _profiler is not None:
        return _profile_path

    target.parent.mkdir(parents=True, exist_ok=True)
    profiler = cProfile.Profile()
    profiler.enable()
    _profiler = profiler
    _profile_path = target
    atexit.register(_write_cpu_profile)
    os.environ["HERMES_CPU_PROFILE"] = str(target)
    logger.info("CPU profiling enabled: %s", target)
    return target


def _write_cpu_profile() -> None:
    global _profiler
    profiler = _profiler
    target = _profile_path
    if profiler is None or target is None:
        return
    try:
        profiler.disable()
        target.parent.mkdir(parents=True, exist_ok=True)
        profiler.dump_stats(str(target))
        # Emit a tiny human-readable companion file so users can inspect the
        # hottest functions without remembering the pstats incantation.
        txt = target.with_suffix(target.suffix + ".txt")
        with txt.open("w", encoding="utf-8") as fh:
            stats = pstats.Stats(profiler, stream=fh).sort_stats("cumulative")
            stats.print_stats(80)
        logger.info("CPU profile written: %s", target)
    except Exception:
        logger.exception("Failed to write CPU profile: %s", target)
    finally:
        _profiler = None
