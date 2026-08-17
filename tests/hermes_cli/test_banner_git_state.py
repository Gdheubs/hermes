from unittest.mock import MagicMock, patch




def test_format_banner_version_label_on_upstream_main():
    from hermes_cli import banner

    with patch.object(
        banner,
        "get_git_banner_state",
        return_value={"upstream": "b2f477a3", "local": "b2f477a3", "ahead": 0},
    ):
        value = banner.format_banner_version_label()

    assert value.endswith("· upstream b2f477a3")
    assert "local" not in value


def test_get_git_banner_state_reads_origin_and_head(tmp_path):
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    results = {
        ("git", "rev-parse", "--short=8", "origin/main"): MagicMock(returncode=0, stdout="b2f477a3\n"),
        ("git", "rev-parse", "--short=8", "HEAD"): MagicMock(returncode=0, stdout="af8aad31\n"),
        ("git", "rev-list", "--count", "origin/main..HEAD"): MagicMock(returncode=0, stdout="3\n"),
    }

    def fake_run(cmd, **kwargs):
        key = tuple(cmd)
        if key not in results:
            raise AssertionError(f"unexpected command: {cmd}")
        return results[key]

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        state = banner.get_git_banner_state(repo_dir)

    assert state == {"upstream": "b2f477a3", "local": "af8aad31", "ahead": 3}


def test_check_via_local_git_ssh_fastpath_ahead_not_behind(tmp_path):
    """SSH fast path must not report an ahead (carried) HEAD as behind.

    A carried local commit means tip SHAs differ, but the fresh upstream tip
    is an ancestor of HEAD — that is "ahead", and reporting it as behind
    nudges the user into `hermes update`, which can wipe the carried work.
    """
    from unittest.mock import MagicMock

    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "git@github.com:NousResearch/hermes-agent.git"
        if args == ["rev-parse", "HEAD"]:
            return "b" * 40  # carried commit, differs from upstream tip
        raise AssertionError(f"unexpected git call: {args}")

    with (
        patch.object(banner, "_git_stdout", side_effect=fake_git_stdout),
        patch.object(banner, "_upstream_main_sha", return_value="a" * 40),
        # merge-base --is-ancestor exits 0: upstream tip IS an ancestor of HEAD
        patch.object(banner.subprocess, "run", return_value=MagicMock(returncode=0)),
    ):
        behind = banner._check_via_local_git(repo_dir)

    assert behind == 0


def test_check_via_local_git_ssh_fastpath_genuinely_behind(tmp_path):
    """SSH fast path reports the exact count (compare API) when behind."""
    from unittest.mock import MagicMock

    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "git@github.com:NousResearch/hermes-agent.git"
        if args == ["rev-parse", "HEAD"]:
            return "b" * 40
        raise AssertionError(f"unexpected git call: {args}")

    with (
        patch.object(banner, "_git_stdout", side_effect=fake_git_stdout),
        patch.object(banner, "_upstream_main_sha", return_value="a" * 40),
        # merge-base --is-ancestor exits 1: not an ancestor -> genuinely behind
        patch.object(banner.subprocess, "run", return_value=MagicMock(returncode=1)),
        patch.object(banner, "_github_compare_behind", return_value=3),
    ):
        behind = banner._check_via_local_git(repo_dir)

    assert behind == 3


def test_check_via_local_git_ssh_fastpath_offline_keeps_sentinel(tmp_path):
    """Behind + compare API unreachable = honest no-count sentinel, never 1."""
    from unittest.mock import MagicMock

    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "git@github.com:NousResearch/hermes-agent.git"
        if args == ["rev-parse", "HEAD"]:
            return "b" * 40
        raise AssertionError(f"unexpected git call: {args}")

    with (
        patch.object(banner, "_git_stdout", side_effect=fake_git_stdout),
        patch.object(banner, "_upstream_main_sha", return_value="a" * 40),
        patch.object(banner.subprocess, "run", return_value=MagicMock(returncode=1)),
        patch.object(banner, "_github_compare_behind", return_value=None),
    ):
        behind = banner._check_via_local_git(repo_dir)

    assert behind == banner.UPDATE_AVAILABLE_NO_COUNT


def test_check_via_local_git_full_clone_zero_count_diverged(tmp_path):
    """A diverged full clone with HEAD..origin/main == 0 is UPDATE_DIVERGED.

    Regression for #68484: a diverged feature branch can report a zero tip
    count (neither tip is an ancestor of the other), which the old code read
    as "up to date". The ancestry check must run for every successful
    full-clone count and return the named sentinel.
    """
    from unittest.mock import MagicMock

    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        raise AssertionError(f"unexpected git call: {args}")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "fetch"]:
            return MagicMock(returncode=0)
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return MagicMock(returncode=0, stdout="0\n")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(banner, "_git_stdout", side_effect=fake_git_stdout),
        patch.object(banner.subprocess, "run", side_effect=fake_run),
        # Neither tip is an ancestor of the other -> diverged.
        patch.object(banner, "_git_is_ancestor", return_value=False),
    ):
        behind = banner._check_via_local_git(repo_dir)

    assert behind == banner.UPDATE_DIVERGED


def test_check_via_local_git_full_clone_fast_forward_keeps_count(tmp_path):
    """A genuine fast-forward full clone keeps its exact behind count."""
    from unittest.mock import MagicMock

    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        raise AssertionError(f"unexpected git call: {args}")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "fetch"]:
            return MagicMock(returncode=0)
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return MagicMock(returncode=0, stdout="7\n")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    def fake_is_ancestor(maybe_ancestor, rev, repo_dir):
        # HEAD is an ancestor of origin/main -> fast-forward behind.
        return maybe_ancestor == "HEAD"

    with (
        patch.object(banner, "_git_stdout", side_effect=fake_git_stdout),
        patch.object(banner.subprocess, "run", side_effect=fake_run),
        patch.object(banner, "_git_is_ancestor", side_effect=fake_is_ancestor),
    ):
        behind = banner._check_via_local_git(repo_dir)

    assert behind == 7


def test_format_update_notice_diverged():
    """The diverged sentinel renders a non-fast-forward warning, not a count."""
    from hermes_cli import banner

    notice = banner._format_update_notice(banner.UPDATE_DIVERGED)

    assert "diverged" in notice
    assert "behind" not in notice
