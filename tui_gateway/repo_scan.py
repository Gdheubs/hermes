"""Server-side git repo discovery for the Desktop Projects sidebar.

The desktop's native Electron walk (apps/desktop/electron/git-repo-scan.ts)
runs on the machine that hosts the Electron app. In remote mode the app is on
a different host than the backend files, so that walk never sees the repos the
backend is co-located with. This module is the backend's own bounded walk so a
remote-connected desktop still gets disk-scanned repos (populated into
``discovered_repos`` via ``projects.scan_repos`` / ``projects.record_repos``).

It mirrors the Electron walk's structural contract — repo detection by a
``.git`` dir with a readable ``HEAD``, bounded depth, hidden + junk dir
exclusion, empty-roots home-dir default — so local and remote discovery agree
on the set of repos. Exact concurrency and ordering are not preserved (they
never need to be).
"""

from __future__ import annotations

import os

DEFAULT_MAX_DEPTH = 3
JUNK_DIRS = frozenset(
    {"Applications", "Library", "node_modules", "site-packages", "vendor", "venv"}
)


def scan_repos(
    roots: list[str],
    *,
    max_depth: int | None = None,
    enabled: bool = True,
    exclude_paths: list[str] | None = None,
    home: str | None = None,
) -> list[dict]:
    """Walk ``roots`` for git repositories, bounded and junk-filtered.

    Empty ``roots`` preserves the historical home-directory scan. Returns a
    list of ``{"root": ..., "label": ...}`` (label = basename), deduplicated by
    normalized root. ``enabled=False`` returns ``[]`` without touching disk.
    """
    if not enabled:
        return []

    home_dir = home or os.path.expanduser("~")
    depth_cap = max_depth if isinstance(max_depth, int) and max_depth >= 0 else DEFAULT_MAX_DEPTH
    search_roots = [os.path.normpath(r) for r in roots] if roots else [home_dir]
    exclusions = [os.path.normpath(p) for p in (exclude_paths or []) if p]

    found: dict[str, dict] = {}

    def excluded(candidate: str) -> bool:
        return any(_is_within(candidate, exc) for exc in exclusions)

    def walk(dirpath: str, depth: int) -> None:
        if depth > depth_cap or excluded(dirpath):
            return

        try:
            entries = os.scandir(dirpath)
        except OSError:
            return

        git_found = False
        subdirs: list[str] = []

        with entries:
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue

                name = entry.name
                if is_dir and name == ".git":
                    git_found = True
                elif is_dir:
                    if name.startswith(".") or name in JUNK_DIRS:
                        continue
                    subdirs.append(entry.path)

        if git_found:
            # Only a repo when HEAD is readable.
            if os.access(os.path.join(dirpath, ".git", "HEAD"), os.R_OK):
                norm = os.path.normpath(dirpath)
                found.setdefault(
                    norm,
                    {"root": norm, "label": os.path.basename(norm) or norm},
                )
            return

        for subdir in subdirs:
            walk(subdir, depth + 1)

    for root in search_roots:
        walk(root, 0)

    return list(found.values())


def _is_within(candidate: str, parent: str) -> bool:
    """True when ``candidate`` equals ``parent`` or is nested under it."""
    try:
        rel = os.path.relpath(candidate, parent)
    except ValueError:  # different drive (Windows)
        return False
    return rel == "." or (rel != ".." and not rel.startswith(".." + os.sep) and not os.path.isabs(rel))
