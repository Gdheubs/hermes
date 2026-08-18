"""``hermes update`` must flag a Desktop app that was NOT rebuilt.

Regression test for #88251: a failed desktop pack is non-fatal, but the
success banner used to be the last thing the user saw — implying the
Electron app shipped with the update while it silently stayed on the
previous build. ``_rebuild_desktop_after_update`` now returns ``False``
only when a rebuild was attempted and failed, so both update paths (zip
and git) can append a prominent stale-app warning after the summary.
"""

from pathlib import Path

import pytest

import hermes_cli.update_cmd as update_cmd
from hermes_cli.update_cmd import _rebuild_desktop_after_update


class _Result:
    def __init__(self, returncode: int, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture()
def desktop_env(tmp_path, monkeypatch):
    """A desktop dir that looks installed and a faked CLI main module."""
    desktop_dir = tmp_path / "apps" / "desktop"
    desktop_dir.mkdir(parents=True)
    (desktop_dir / "package.json").write_text("{}", encoding="utf-8")

    calls = {"builds": 0, "build_needed": True}

    class _FakeMain:
        PROJECT_ROOT = tmp_path

        @staticmethod
        def _resolve_node_runtime_npm():
            return "/fake/npm"

        @staticmethod
        def _desktop_build_needed(*_a, **_kw):
            return calls["build_needed"]

        @staticmethod
        def _run_logged_subprocess(cmd, cwd=None, env=None):
            calls["builds"] += 1
            return _Result(1, stdout="Error: [stage-native-deps] boom")

    monkeypatch.setattr(update_cmd, "_m", lambda: _FakeMain)
    monkeypatch.setattr(
        "hermes_constants.with_hermes_node_path", lambda: {}, raising=False
    )
    monkeypatch.setattr(
        "hermes_constants.display_hermes_home", lambda: str(tmp_path), raising=False
    )
    return desktop_dir, calls


def _run(desktop_dir):
    return _rebuild_desktop_after_update(
        desktop_dir, had_desktop_app_before_update=True
    )


def test_failed_rebuild_returns_false_and_keeps_the_non_fatal_hint(
    desktop_env, capsys
):
    desktop_dir, calls = desktop_env
    # Both the initial build and the retry fail (returncode=1 every time).
    assert _run(desktop_dir) is False
    assert calls["builds"] == 2
    out = capsys.readouterr().out
    assert "Desktop build failed (non-fatal" in out
    assert "stage-native-deps" in out


def test_successful_rebuild_returns_true(desktop_env, monkeypatch, capsys):
    desktop_dir, _calls = desktop_env
    # `_m()` yields the fake main class itself; patch the build result on it.
    builds = []
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        staticmethod(
            lambda cmd, cwd=None, env=None: builds.append(cmd) or _Result(0)
        ),
    )
    assert _run(desktop_dir) is True
    assert len(builds) == 1
    assert "Desktop app up to date" in capsys.readouterr().out


def test_up_to_date_desktop_returns_true_without_spawning(desktop_env):
    desktop_dir, calls = desktop_env
    calls["build_needed"] = False
    assert _run(desktop_dir) is True
    assert calls["builds"] == 0


def test_desktop_never_installed_returns_true(tmp_path, monkeypatch):
    # No package.json → nothing to rebuild; must not spawn anything either.
    spawned = []
    monkeypatch.setattr(
        update_cmd,
        "_m",
        lambda: type(
            "_M",
            (),
            {
                "PROJECT_ROOT": tmp_path,
                "_resolve_node_runtime_npm": staticmethod(lambda: "/fake/npm"),
                "_run_logged_subprocess": staticmethod(
                    lambda *a, **k: spawned.append(1) or _Result(0)
                ),
            },
        ),
    )
    missing = tmp_path / "apps" / "desktop"
    missing.mkdir(parents=True)
    assert _run(missing) is True
    assert spawned == []
