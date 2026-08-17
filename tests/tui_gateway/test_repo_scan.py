"""Invariants for the server-side git repo scan (tui_gateway.repo_scan).

The desktop's native Electron walk (apps/desktop/electron/git-repo-scan.ts)
never runs in remote mode, so the backend — which is co-located with the
actual files — needs its own bounded walk to populate discovered_repos. These
assert the structural contract (repo detection, bounded depth, junk/hidden
exclusion, home-dir default) without snapshotting exact results.
"""

from __future__ import annotations

import os

from tui_gateway import repo_scan


def _make_git_repo(root: str, name: str) -> str:
    repo = os.path.join(root, name)
    os.makedirs(os.path.join(repo, ".git"))
    with open(os.path.join(repo, ".git", "HEAD"), "w") as f:
        f.write("ref: refs/heads/main\n")
    return repo


def _make_plain_dir(root: str, name: str) -> str:
    path = os.path.join(root, name)
    os.makedirs(path)
    return path


def test_scans_nested_git_repos(tmp_path):
    repo_a = _make_git_repo(str(tmp_path), "alpha")
    repo_b = _make_git_repo(str(tmp_path), "beta")
    _make_plain_dir(str(tmp_path), "not-a-repo")

    found = repo_scan.scan_repos([str(tmp_path)])

    roots = {f["root"] for f in found}
    assert repo_a in roots
    assert repo_b in roots


def test_respects_max_depth(tmp_path):
    _make_git_repo(str(tmp_path), "top")
    deep = os.path.join(str(tmp_path), "one", "two", "three", "four")
    deep_repo = _make_git_repo(deep, "nested")

    shallow = repo_scan.scan_repos([str(tmp_path)], max_depth=2)
    shallow_roots = {f["root"] for f in shallow}
    assert deep_repo not in shallow_roots

    deep_scan = repo_scan.scan_repos([str(tmp_path)], max_depth=6)
    deep_roots = {f["root"] for f in deep_scan}
    assert deep_repo in deep_roots


def test_skips_junk_dirs(tmp_path):
    _make_git_repo(str(tmp_path), "real")
    # Junk and hidden dirs are never descended into, so a repo named as (or
    # nested under) a junk dir is not discovered.
    _make_git_repo(str(tmp_path), "node_modules")
    _make_git_repo(os.path.join(str(tmp_path), "real", "node_modules"), "inner")

    found = repo_scan.scan_repos([str(tmp_path)])

    roots = {f["root"] for f in found}
    assert os.path.join(str(tmp_path), "real") in roots
    assert os.path.join(str(tmp_path), "node_modules") not in roots
    assert os.path.join(str(tmp_path), "real", "node_modules", "inner") not in roots


def test_skips_hidden_dirs(tmp_path):
    _make_git_repo(str(tmp_path), "visible")
    _make_git_repo(str(tmp_path), ".hidden")
    _make_git_repo(os.path.join(str(tmp_path), "visible", ".config"), "inner")

    found = repo_scan.scan_repos([str(tmp_path)])

    roots = {f["root"] for f in found}
    assert os.path.join(str(tmp_path), "visible") in roots
    assert os.path.join(str(tmp_path), ".hidden") not in roots
    assert os.path.join(str(tmp_path), "visible", ".config", "inner") not in roots


def test_disabled_returns_empty(tmp_path):
    found = repo_scan.scan_repos([str(tmp_path)], enabled=False)
    assert found == []


def test_does_not_require_real_git(tmp_path):
    # A dir named .git without a readable HEAD is not a repo.
    fake = _make_plain_dir(str(tmp_path), "fake")
    os.makedirs(os.path.join(fake, ".git"))  # no HEAD file

    found = repo_scan.scan_repos([str(tmp_path)])
    roots = {f["root"] for f in found}
    assert fake not in roots
