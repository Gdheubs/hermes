"""Divergence guard for ``hermes update``.

Background
----------
``hermes_cli/update_cmd.py`` fetches ``origin/<branch>`` and then runs
``git merge --ff-only origin/<branch>``.  When that fails because local and
remote have DIVERGED, the current code falls through to an unconditional
``git reset --hard origin/<branch>`` (update_cmd.py:3983-3994).  That reset
silently deletes every local-only commit.  It has fired twice and destroyed
human-authored work both times.

This module is the preflight that replaces that fallback.  It compares the
local HEAD against the already-fetched ``origin/<branch>``:

* HEAD strictly behind origin  → ``PROCEED_FF``    (the ff-only merge is safe)
* origin has nothing new       → ``UP_TO_DATE``    (nothing for the updater to do)
* both sides have own commits  → ``ABORT_DIVERGED`` (a reset would destroy work)

On the abort path it creates ONE ref — ``rescue/pre-update-<UTC-timestamp>``
at the current HEAD — so the local commits stay reachable no matter what the
user does next, and then hands back a message with exact recovery commands.
HEAD, the index and the working tree are left exactly as they were.

Deliberately NOT reused: the ``_is_fork`` / upstream-sync machinery in
update_cmd.py.  That compares origin-vs-upstream on a different code path and
does nothing to protect local commits.  This guard keys off local-HEAD-vs-origin
and nothing else.

Every command this module runs is read-only except the single ``git branch``
that creates the rescue ref.  It never calls ``reset``, ``clean``, ``checkout``,
``stash drop`` or any other destructive operation.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Guard verdicts. The updater switches on these.
PROCEED_FF = "PROCEED_FF"
UP_TO_DATE = "UP_TO_DATE"
ABORT_DIVERGED = "ABORT_DIVERGED"

RESCUE_REF_PREFIX = "rescue/pre-update-"

# Commands this module is allowed to run. Anything that mutates history or the
# working tree is absent on purpose; `_run_git` refuses the rest at runtime so
# a future edit can't quietly reintroduce the reset.
_ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {"rev-parse", "rev-list", "merge-base", "branch", "for-each-ref", "status"}
)
_FORBIDDEN_GIT_SUBCOMMANDS = frozenset(
    {"reset", "clean", "checkout", "switch", "restore", "stash", "push", "merge",
     "rebase", "pull", "gc", "prune", "update-ref", "filter-branch", "am",
     "cherry-pick", "revert", "apply", "commit"}
)


class DestructiveGitCommand(RuntimeError):
    """Raised if this module is ever asked to run a history-destroying command."""


@dataclass
class GuardResult:
    """Verdict from :func:`check_divergence_and_guard`."""

    action: str
    rescue_ref: str | None = None
    local_only_count: int = 0
    remote_only_count: int = 0
    message: str = ""
    head_sha: str | None = None
    remote_sha: str | None = None
    # Populated when the guard wanted a rescue ref but `git branch` failed.
    rescue_error: str | None = None
    # Every git command the guard ran, for auditing/tests.
    commands_run: list[list[str]] = field(default_factory=list)

    @property
    def should_abort(self) -> bool:
        return self.action == ABORT_DIVERGED

    @property
    def may_proceed(self) -> bool:
        return self.action in (PROCEED_FF, UP_TO_DATE)


def _run_git(git_cmd, cwd, args, commands_run):
    """Run a read-only (or rescue-ref-creating) git command.

    Mirrors the ``subprocess.run(git_cmd + [...])`` idiom used throughout
    update_cmd.py. Raises :class:`DestructiveGitCommand` rather than running
    anything that could lose work.
    """
    subcommand = next((a for a in args if not a.startswith("-")), None)
    if subcommand in _FORBIDDEN_GIT_SUBCOMMANDS or subcommand not in _ALLOWED_GIT_SUBCOMMANDS:
        raise DestructiveGitCommand(
            f"divergence_guard refuses to run: git {' '.join(args)}"
        )
    # `git branch -D/-d/-m/-f` deletes or moves refs; only plain creation is ok.
    if subcommand == "branch" and any(
        a in ("-D", "-d", "--delete", "-m", "-M", "--move", "-f", "--force") for a in args
    ):
        raise DestructiveGitCommand(
            f"divergence_guard refuses to run: git {' '.join(args)}"
        )

    commands_run.append(list(args))
    return subprocess.run(
        list(git_cmd) + list(args),
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def _rev_parse(git_cmd, cwd, ref, commands_run) -> str | None:
    """Resolve ``ref`` to a SHA, or None if it doesn't exist."""
    result = _run_git(git_cmd, cwd, ["rev-parse", "--verify", f"{ref}^{{commit}}"], commands_run)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _count_ahead_behind(git_cmd, cwd, remote_ref, commands_run) -> tuple[int, int] | None:
    """Return ``(remote_only, local_only)`` commit counts, or None on error.

    ``remote_only`` is what origin has that we don't; ``local_only`` is what we
    have that origin doesn't — the commits a ``reset --hard`` would destroy.
    """
    result = _run_git(
        git_cmd, cwd,
        ["rev-list", "--left-right", "--count", f"{remote_ref}...HEAD"],
        commands_run,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _rescue_ref_name(now: datetime) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{RESCUE_REF_PREFIX}{stamp}"


def _create_rescue_ref(git_cmd, cwd, now, commands_run) -> tuple[str | None, str | None]:
    """Create ``rescue/pre-update-<UTC-timestamp>`` at HEAD.

    Returns ``(ref_name, error)``. Uses ``git branch <name> HEAD``, which only
    ever adds a ref — it cannot move HEAD or touch the working tree. If the
    name is already taken (two aborts inside the same second) a ``-2``, ``-3``
    … suffix is tried rather than overwriting the existing rescue point.
    """
    base = _rescue_ref_name(now)
    for attempt in range(1, 21):
        name = base if attempt == 1 else f"{base}-{attempt}"
        existing = _run_git(
            git_cmd, cwd, ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], commands_run
        )
        if existing.returncode == 0:
            continue
        result = _run_git(git_cmd, cwd, ["branch", name, "HEAD"], commands_run)
        if result.returncode == 0:
            return name, None
        stderr = result.stderr.strip()
        if "already exists" in stderr:
            continue
        return None, stderr or f"git branch exited {result.returncode}"
    return None, "could not find an unused rescue branch name"


def _abort_message(branch, rescue_ref, rescue_error, local_only, remote_only, head_sha) -> str:
    short = (head_sha or "unknown")[:10]
    lines = [
        f"✗ Update aborted: your local branch has diverged from origin/{branch}.",
        "",
        f"  Local-only commits (a reset would destroy these): {local_only}",
        f"  New commits on origin/{branch}: {remote_only}",
        f"  Current HEAD: {short}",
        "",
    ]
    if rescue_ref:
        lines += [
            "  Your work is safe. A rescue branch was created at your current HEAD:",
            f"      {rescue_ref}",
            "",
            "  Nothing else was changed. HEAD, the index and your working tree are",
            "  exactly as they were before this command ran.",
            "",
            "  See what is at risk:",
            f"      git log --oneline origin/{branch}..{rescue_ref}",
            f"      git diff origin/{branch}...{rescue_ref}",
            "",
            "  Then pick one:",
            "    1. Replay your commits on top of the update (keeps your work):",
            f"         git rebase origin/{branch}",
            "    2. Merge the update into your branch instead:",
            f"         git merge origin/{branch}",
            "    3. Deliberately drop your local commits:",
            f"         git reset --hard origin/{branch}",
            f"       Even then they stay reachable on {rescue_ref}.",
            "",
            f"  Re-run `hermes update` once local and origin/{branch} no longer diverge.",
        ]
    else:
        lines += [
            "  ⚠ The rescue branch could NOT be created"
            + (f": {rescue_error}" if rescue_error else "."),
            "  Nothing was changed, so your commits are still on HEAD. Save them now:",
            f"      git branch my-rescue {short}",
            "",
            "  Then see what is at risk:",
            f"      git log --oneline origin/{branch}..HEAD",
            "",
            f"  Re-run `hermes update` once local and origin/{branch} no longer diverge.",
        ]
    return "\n".join(lines)


def check_divergence_and_guard(git_cmd, cwd, branch, now=None) -> GuardResult:
    """Decide whether ``hermes update`` may safely fast-forward.

    Call this AFTER ``git fetch origin <branch>`` and INSTEAD OF the
    ``reset --hard`` fallback at update_cmd.py:3983.

    ``git_cmd`` is the git argv prefix used everywhere in update_cmd.py
    (e.g. ``["git"]``), ``cwd`` the repo root, ``branch`` the branch being
    updated. ``now`` is injectable for deterministic rescue-ref names in tests.

    Returns a :class:`GuardResult`. ``ABORT_DIVERGED`` means the caller must
    print ``result.message`` and stop — it must NOT reset.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    commands_run: list[list[str]] = []
    remote_ref = f"origin/{branch}"

    head_sha = _rev_parse(git_cmd, cwd, "HEAD", commands_run)
    remote_sha = _rev_parse(git_cmd, cwd, remote_ref, commands_run)

    # Can't establish the relationship → refuse to let a reset happen. The
    # guard's job is to be conservative; an unverifiable state is not a safe
    # state to destroy history in.
    if head_sha is None or remote_sha is None:
        missing = "HEAD" if head_sha is None else remote_ref
        return GuardResult(
            action=ABORT_DIVERGED,
            rescue_ref=None,
            local_only_count=-1,
            remote_only_count=-1,
            head_sha=head_sha,
            remote_sha=remote_sha,
            commands_run=commands_run,
            message=(
                f"✗ Update aborted: could not resolve {missing}.\n"
                "  Refusing to touch your history while the local/remote relationship\n"
                "  is unknown. Nothing was changed.\n"
                f"  Check the repo with: git status && git rev-parse HEAD {remote_ref}"
            ),
        )

    if head_sha == remote_sha:
        return GuardResult(
            action=UP_TO_DATE,
            rescue_ref=None,
            local_only_count=0,
            remote_only_count=0,
            head_sha=head_sha,
            remote_sha=remote_sha,
            commands_run=commands_run,
            message=f"Already up to date with origin/{branch}.",
        )

    counts = _count_ahead_behind(git_cmd, cwd, remote_ref, commands_run)
    if counts is None:
        return GuardResult(
            action=ABORT_DIVERGED,
            rescue_ref=None,
            local_only_count=-1,
            remote_only_count=-1,
            head_sha=head_sha,
            remote_sha=remote_sha,
            commands_run=commands_run,
            message=(
                f"✗ Update aborted: could not compare HEAD with origin/{branch}.\n"
                "  Refusing to touch your history while the local/remote relationship\n"
                "  is unknown. Nothing was changed.\n"
                f"  Check the repo with: git rev-list --left-right --count origin/{branch}...HEAD"
            ),
        )

    remote_only, local_only = counts

    if remote_only > 0 and local_only > 0:
        # The case the incident is about. Save HEAD before saying anything else.
        rescue_ref, rescue_error = _create_rescue_ref(git_cmd, cwd, now, commands_run)
        return GuardResult(
            action=ABORT_DIVERGED,
            rescue_ref=rescue_ref,
            local_only_count=local_only,
            remote_only_count=remote_only,
            head_sha=head_sha,
            remote_sha=remote_sha,
            rescue_error=rescue_error,
            commands_run=commands_run,
            message=_abort_message(
                branch, rescue_ref, rescue_error, local_only, remote_only, head_sha
            ),
        )

    if remote_only > 0:
        # Strictly behind: `merge --ff-only` succeeds and destroys nothing.
        return GuardResult(
            action=PROCEED_FF,
            rescue_ref=None,
            local_only_count=0,
            remote_only_count=remote_only,
            head_sha=head_sha,
            remote_sha=remote_sha,
            commands_run=commands_run,
            message=(
                f"Fast-forward is safe: {remote_only} new commit(s) on origin/{branch}, "
                "no local-only commits."
            ),
        )

    # remote_only == 0 with local_only > 0: we're strictly ahead. There is
    # nothing to pull, and `merge --ff-only` would be a no-op, so the updater
    # has no work to do and no reset can be triggered.
    return GuardResult(
        action=UP_TO_DATE,
        rescue_ref=None,
        local_only_count=local_only,
        remote_only_count=0,
        head_sha=head_sha,
        remote_sha=remote_sha,
        commands_run=commands_run,
        message=(
            f"No new commits on origin/{branch}; local is ahead by "
            f"{local_only} commit(s). Nothing to update."
        ),
    )
