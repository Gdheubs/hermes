"""``hermes watch`` subcommand — report filesystem changes as they happen.

Polls the watched trees and prints created / modified / deleted events. When
the optional ``watchdog`` package is importable the OS notification backend
(inotify, FSEvents, ReadDirectoryChangesW) is used instead, which is cheaper
on large trees; ``watchdog`` is not a Hermes dependency, so polling is the
path most installs take.

Usage:
  hermes watch <path> [<path> ...]
  hermes watch . --pattern "*.py" --command "pytest tests/"
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

try:  # optional accelerator, not a declared dependency
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _HAS_WATCHDOG = True
except ImportError:  # pragma: no cover - exercised by the polling path
    _HAS_WATCHDOG = False


# ── matching ─────────────────────────────────────────────────────────────────


def _matches(rel_path: str, pattern: str) -> bool:
    """True when *rel_path* matches *pattern*.

    A pattern is tried against the bare filename, the whole root-relative
    path, and that path with any leading directories absorbed. The last form
    is what makes the documented directory patterns (``__pycache__/*``,
    ``node_modules/*``) match at any depth rather than only at the root.
    """
    rel = rel_path.replace(os.sep, "/")
    return (
        fnmatch(rel.rsplit("/", 1)[-1], pattern)
        or fnmatch(rel, pattern)
        or fnmatch(rel, f"*/{pattern}")
    )


def _is_reported(rel_path: str, patterns: list[str], ignore: list[str]) -> bool:
    """Apply the ignore list, then the include list, to one path."""
    if any(_matches(rel_path, p) for p in ignore):
        return False
    if not patterns:
        return True
    return any(_matches(rel_path, p) for p in patterns)


# ── snapshot / diff (the whole detection rule, as pure functions) ────────────

# A file's identity for change detection. mtime alone is not enough: Linux
# inode timestamps are coarse, so two writes inside the same tick share an
# mtime. Size disambiguates the common case of an edit that changes length.
_Stamp = tuple[int, int]


def _snapshot(roots: list[str], recursive: bool = True) -> dict[str, _Stamp]:
    """Map every readable file under *roots* to its (mtime_ns, size) stamp.

    Keys are root-relative POSIX paths, prefixed with the root when more than
    one tree is watched, so ``src/a.py`` and ``tests/a.py`` stay distinct.
    """
    out: dict[str, _Stamp] = {}
    multi = len(roots) > 1
    for root in roots:
        prefix = f"{Path(root).name}/" if multi else ""
        for dirpath, dirnames, filenames in os.walk(root):
            if not recursive:
                dirnames[:] = []
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                try:
                    st = os.stat(full)
                except OSError:
                    # Vanished or unreadable between walk and stat — the next
                    # sweep reports it as deleted if it is really gone.
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                out[prefix + rel] = (st.st_mtime_ns, st.st_size)
    return out


def _diff(
    before: dict[str, _Stamp],
    after: dict[str, _Stamp],
    patterns: list[str],
    ignore: list[str],
) -> list[tuple[str, str]]:
    """Return ``[(kind, rel_path)]`` for the changes between two snapshots.

    Sorted by path so output is stable regardless of ``os.walk`` order.
    """
    events: list[tuple[str, str]] = []
    for rel in after.keys() - before.keys():
        events.append(("created", rel))
    for rel in before.keys() - after.keys():
        events.append(("deleted", rel))
    for rel in before.keys() & after.keys():
        if before[rel] != after[rel]:
            events.append(("modified", rel))
    return sorted(
        (e for e in events if _is_reported(e[1], patterns, ignore)),
        key=lambda e: (e[1], e[0]),
    )


# ── shared output / command plumbing ─────────────────────────────────────────


class _Reporter:
    """Numbers and prints events, and tracks whether a batch is outstanding."""

    def __init__(self) -> None:
        self.count = 0
        self._pending = False

    def emit(self, kind: str, rel_path: str) -> None:
        self.count += 1
        self._pending = True
        print(f"[{self.count:04d}] {kind:10s} {rel_path}", flush=True)

    def take_pending(self) -> bool:
        """True once per batch of events, consuming the flag."""
        pending, self._pending = self._pending, False
        return pending


def _run_command(command: str) -> int:
    """Run the ``--command`` shell string, returning its real exit code.

    ``os.system`` returns a wait status, not an exit code — propagating it
    turns a failing command's exit 1 into a wait status of 256, which the
    shell then sees as 0.
    """
    try:
        return subprocess.call(command, shell=True)
    except OSError as exc:
        print(f"watch: could not run command: {exc}", file=sys.stderr)
        return 1


def _install_signal_handlers(stop: threading.Event) -> None:
    """Ask the watcher to wind down on Ctrl+C / SIGTERM.

    Only the main thread may install handlers; embedded callers on a worker
    thread keep the default disposition and stop via *stop*.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def _handle(signum, frame):  # noqa: ARG001 - signal handler signature
        stop.set()
        print("\nStopping watcher...", flush=True)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):  # pragma: no cover - platform dependent
            logger.debug("watch: could not install handler for %s", sig)


def _resolve_roots(paths: list[Path]) -> list[str]:
    """Existing directories among *paths*, warning about the rest."""
    roots: list[str] = []
    for p in paths:
        resolved = p.resolve()
        if not resolved.exists():
            print(f"watch: skipping non-existent path: {p}", file=sys.stderr)
            continue
        roots.append(str(resolved))
    return roots


# ── backends ─────────────────────────────────────────────────────────────────


def run_polling(
    roots: list[str],
    patterns: list[str],
    ignore: list[str],
    interval: float,
    recursive: bool,
    reporter: _Reporter,
    stop: threading.Event,
    on_batch: Callable[[], None],
) -> None:
    """Sweep *roots* every *interval* seconds until *stop* is set."""
    previous = _snapshot(roots, recursive)
    print(
        f"Watching {len(roots)} path(s) [polling {interval}s] — Ctrl+C to stop",
        flush=True,
    )
    while not stop.wait(interval):
        current = _snapshot(roots, recursive)
        for kind, rel in _diff(previous, current, patterns, ignore):
            reporter.emit(kind, rel)
        previous = current
        on_batch()


def run_watchdog(
    roots: list[str],
    patterns: list[str],
    ignore: list[str],
    recursive: bool,
    reporter: _Reporter,
    stop: threading.Event,
    on_batch: Callable[[], None],
) -> None:
    """Sweep *roots* through watchdog's OS notification backend."""

    class _ChangeHandler(FileSystemEventHandler):  # type: ignore[misc]
        def __init__(self, root: str, prefix: str) -> None:
            super().__init__()
            self._root = root
            self._prefix = prefix

        def on_any_event(self, event) -> None:
            if event.is_directory:
                return
            path = getattr(event, "dest_path", None) or event.src_path
            rel = self._prefix + os.path.relpath(path, self._root).replace(os.sep, "/")
            if _is_reported(rel, patterns, ignore):
                reporter.emit(event.event_type, rel)

    observer = Observer()
    multi = len(roots) > 1
    for root in roots:
        prefix = f"{Path(root).name}/" if multi else ""
        observer.schedule(_ChangeHandler(root, prefix), root, recursive=recursive)

    observer.start()
    print(f"Watching {len(roots)} path(s) — Ctrl+C to stop", flush=True)
    try:
        # Events arrive on the observer thread; the command runs from here, so
        # a burst of writes triggers one run per tick rather than one per file.
        while not stop.wait(0.5):
            on_batch()
    finally:
        observer.stop()
        observer.join(timeout=3)


def watch(
    paths: list[Path],
    *,
    patterns: list[str] | None = None,
    ignore: list[str] | None = None,
    recursive: bool = True,
    interval: float = 1.0,
    command: str | None = None,
    stop: threading.Event | None = None,
) -> int:
    """Watch *paths*, then run *command* if anything changed."""
    roots = _resolve_roots(paths)
    if not roots:
        print("watch: no valid paths to watch", file=sys.stderr)
        return 1

    patterns = patterns or []
    ignore = ignore or []
    reporter = _Reporter()
    stop = stop if stop is not None else threading.Event()
    _install_signal_handlers(stop)

    # --command is a feedback loop: it runs each time a sweep turns up
    # something, on whichever backend is active. Running it once at shutdown
    # instead would mean `--command "pytest"` only fires after Ctrl+C, and
    # gating it on watchdog meant it did nothing at all on a normal install.
    last_rc = 0

    def _on_batch() -> None:
        nonlocal last_rc
        if command and reporter.take_pending():
            print(f"Running: {command}", flush=True)
            last_rc = _run_command(command)

    try:
        if _HAS_WATCHDOG:
            run_watchdog(roots, patterns, ignore, recursive, reporter, stop, _on_batch)
        else:
            run_polling(
                roots, patterns, ignore, interval, recursive, reporter, stop, _on_batch
            )
    except KeyboardInterrupt:  # pragma: no cover - handler normally wins
        pass

    print(f"Done. {reporter.count} events.", flush=True)
    return last_rc


# ── CLI entry point ──────────────────────────────────────────────────────────


def cmd_watch(args: argparse.Namespace) -> int:
    """Handler for ``hermes watch``."""
    return watch(
        [Path(p) for p in args.paths],
        patterns=args.pattern.split(",") if args.pattern else None,
        ignore=args.ignore.split(",") if args.ignore else None,
        recursive=args.recursive,
        interval=args.interval,
        command=args.run_command,
    )


# ── argparse builder ─────────────────────────────────────────────────────────


def build_watch_parser(subparsers, *, cmd_watch: Callable = cmd_watch) -> None:
    """Attach the ``watch`` subcommand to ``subparsers``."""
    watch_parser = subparsers.add_parser(
        "watch",
        help="Watch files/directories for changes",
        description=(
            "Watch one or more paths for filesystem events (created, modified, "
            "deleted) and print them as they happen. Sweeps by polling; uses "
            "inotify (Linux), FSEvents (macOS) or ReadDirectoryChangesW "
            "(Windows) instead when the optional ``watchdog`` package is "
            "installed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  hermes watch .                          Watch current directory
  hermes watch src/ tests/                Watch multiple directories
  hermes watch . --pattern "*.py"         Only show Python file changes
  hermes watch . --ignore "*.pyc,__pycache__/*"  Ignore patterns
  hermes watch . --command "pytest"       Run command after changes
  hermes watch . --interval 2.0           Polling interval
""",
    )
    watch_parser.add_argument(
        "paths", nargs="+", help="Files or directories to watch"
    )
    watch_parser.add_argument(
        "-p",
        "--pattern",
        default=None,
        help="Only show events matching these glob patterns (comma-separated)",
    )
    watch_parser.add_argument(
        "-i",
        "--ignore",
        default=None,
        help="Ignore events matching these glob patterns (comma-separated)",
    )
    watch_parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        default=True,
        help="Watch directories recursively (default: True)",
    )
    watch_parser.add_argument(
        "--no-recursive",
        action="store_false",
        dest="recursive",
        help="Do not watch directories recursively",
    )
    watch_parser.add_argument(
        "-c",
        "--command",
        # ``command`` is the subparser dest for the selected subcommand, so the
        # flag has to land somewhere else.
        dest="run_command",
        default=None,
        help="Shell command to run after changes are detected",
    )
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1.0)",
    )
    watch_parser.set_defaults(func=cmd_watch, command="watch")
