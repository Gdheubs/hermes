"""``hermes watch`` — change detection, filtering, and the CLI surface.

The detection rule lives in two pure functions (``_snapshot`` / ``_diff``), so
these tests assert the events that come out of real files on disk rather than
that a loop returned 0.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli.main import _BUILTIN_SUBCOMMANDS
from hermes_cli.subcommands import watch as watch_mod
from hermes_cli.subcommands.watch import (
    _diff,
    _is_reported,
    _snapshot,
    build_watch_parser,
    watch,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _events(root: Path, mutate, patterns=None, ignore=None, recursive=True):
    """Snapshot, apply *mutate*, snapshot again, and return the diff."""
    roots = [str(root)]
    before = _snapshot(roots, recursive)
    mutate()
    after = _snapshot(roots, recursive)
    return _diff(before, after, patterns or [], ignore or [])


class TestDetection:
    def test_reports_created_modified_and_deleted(self, tmp_path):
        _write(tmp_path / "kept.txt", "same")
        _write(tmp_path / "edited.txt", "v1")
        _write(tmp_path / "gone.txt", "bye")

        def _mutate():
            _write(tmp_path / "fresh.txt", "new")
            _write(tmp_path / "edited.txt", "v2-longer")
            (tmp_path / "gone.txt").unlink()

        assert _events(tmp_path, _mutate) == [
            ("modified", "edited.txt"),
            ("created", "fresh.txt"),
            ("deleted", "gone.txt"),
        ]

    def test_an_untouched_tree_produces_no_events(self, tmp_path):
        _write(tmp_path / "a.txt", "x")
        _write(tmp_path / "sub" / "b.txt", "y")
        assert _events(tmp_path, lambda: None) == []

    def test_an_mtime_only_change_is_detected(self, tmp_path):
        f = _write(tmp_path / "a.txt", "aaaa")
        before = _snapshot([str(tmp_path)])
        bumped = before["a.txt"][0] + 1_000_000
        os.utime(f, ns=(bumped, bumped))
        after = _snapshot([str(tmp_path)])
        assert _diff(before, after, [], []) == [("modified", "a.txt")]

    def test_a_size_only_change_is_detected(self, tmp_path):
        """Size is in the stamp because inode mtimes are coarse.

        On Linux a rewrite inside the same clock tick can carry the previous
        mtime; comparing size as well catches the common length-changing edit.
        """
        f = _write(tmp_path / "a.txt", "aaaa")
        before = _snapshot([str(tmp_path)])
        f.write_text("aaaaaaaa", encoding="utf-8")
        os.utime(f, ns=(before["a.txt"][0], before["a.txt"][0]))  # freeze mtime
        after = _snapshot([str(tmp_path)])
        assert _diff(before, after, [], []) == [("modified", "a.txt")]

    def test_nested_files_are_found_and_named_by_relative_path(self, tmp_path):
        events = _events(tmp_path, lambda: _write(tmp_path / "src" / "deep" / "m.py", "x"))
        assert events == [("created", "src/deep/m.py")]

    def test_no_recursive_stops_at_the_top_level(self, tmp_path):
        _write(tmp_path / "top.txt", "x")

        def _mutate():
            _write(tmp_path / "top.txt", "xx")
            _write(tmp_path / "sub" / "deep.txt", "y")

        assert _events(tmp_path, _mutate, recursive=False) == [("modified", "top.txt")]

    def test_an_unstattable_entry_does_not_break_the_sweep(self, tmp_path):
        """os.walk lists the name, stat then fails — the sweep must continue.

        A dangling symlink is the case that survives on every platform; the
        same guard covers a file deleted between the walk and the stat.
        """
        try:
            (tmp_path / "dangling").symlink_to(tmp_path / "no-such-target")
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this host")
        _write(tmp_path / "real.txt", "x")

        snap = _snapshot([str(tmp_path)])

        assert set(snap) == {"real.txt"}


class TestMultipleRoots:
    def test_changes_in_every_root_are_reported(self, tmp_path):
        """Watching two trees must not silently monitor only the first."""
        src, tests = tmp_path / "src", tmp_path / "tests"
        _write(src / "a.py", "x")
        _write(tests / "b.py", "y")
        roots = [str(src), str(tests)]

        before = _snapshot(roots)
        _write(src / "new_src.py", "1")
        _write(tests / "new_test.py", "2")
        after = _snapshot(roots)

        assert _diff(before, after, [], []) == [
            ("created", "src/new_src.py"),
            ("created", "tests/new_test.py"),
        ]

    def test_same_named_files_in_two_roots_stay_distinct(self, tmp_path):
        src, tests = tmp_path / "src", tmp_path / "tests"
        _write(src / "a.py", "x")
        _write(tests / "a.py", "y")
        roots = [str(src), str(tests)]

        before = _snapshot(roots)
        _write(tests / "a.py", "y-changed")
        after = _snapshot(roots)

        assert _diff(before, after, [], []) == [("modified", "tests/a.py")]


class TestFiltering:
    @pytest.mark.parametrize(
        "rel,patterns,ignore,expected",
        [
            ("main.py", ["*.py"], [], True),
            ("notes.txt", ["*.py"], [], False),
            ("src/deep/main.py", ["*.py"], [], True),
            ("main.py", [], ["*.pyc"], True),
            ("main.pyc", [], ["*.pyc"], False),
            # The directory patterns the --help epilog and the skill advertise.
            ("pkg/__pycache__/m.cpython-311.pyc", [], ["__pycache__/*"], False),
            ("web/node_modules/lib/x.js", [], ["node_modules/*"], False),
            ("__pycache__/m.pyc", [], ["__pycache__/*"], False),
            ("src/node_modules_notes.md", [], ["node_modules/*"], True),
            # Ignore wins over an include that also matches.
            ("build/out.py", ["*.py"], ["build/*"], False),
        ],
    )
    def test_include_and_ignore_patterns(self, rel, patterns, ignore, expected):
        assert _is_reported(rel, patterns, ignore) is expected

    def test_filtering_applies_to_real_events(self, tmp_path):
        def _mutate():
            _write(tmp_path / "a.py", "x")
            _write(tmp_path / "b.txt", "x")
            _write(tmp_path / "pkg" / "__pycache__" / "a.pyc", "x")

        events = _events(tmp_path, _mutate, patterns=["*.py"], ignore=["__pycache__/*"])
        assert events == [("created", "a.py")]


class TestWatchLoop:
    def test_a_change_during_the_loop_is_printed(self, tmp_path, capsys):
        _write(tmp_path / "a.txt", "v1")
        stop = threading.Event()

        def _edit_then_stop():
            time.sleep(0.15)
            _write(tmp_path / "a.txt", "v2-longer")
            time.sleep(0.15)
            stop.set()

        t = threading.Thread(target=_edit_then_stop, daemon=True)
        t.start()
        rc = watch([tmp_path], interval=0.05, stop=stop)
        t.join(timeout=2)

        out = capsys.readouterr().out
        assert rc == 0
        assert "modified   a.txt" in out, out
        assert "Done. 1 events." in out, out

    def test_no_valid_paths_exits_one_without_looping(self, tmp_path, capsys):
        missing = tmp_path / "nope"
        # No stop event is set: reaching the loop at all would hang the test.
        assert watch([missing], interval=0.01) == 1
        assert "no valid paths" in capsys.readouterr().err

    def test_one_missing_path_does_not_disable_the_others(self, tmp_path, capsys):
        real = tmp_path / "real"
        real.mkdir()
        stop = threading.Event()
        stop.set()
        assert watch([real, tmp_path / "nope"], interval=0.01, stop=stop) == 0
        assert "skipping non-existent path" in capsys.readouterr().err


class TestCommand:
    def test_the_command_runs_while_watching_not_at_shutdown(
        self, tmp_path, monkeypatch, capsys
    ):
        """`--command "pytest"` is a feedback loop, so it fires per batch.

        It was also wired to the watchdog backend only, and watchdog is not a
        Hermes dependency — so on a normal install the flag was accepted,
        documented, and silently did nothing.
        """
        _write(tmp_path / "a.txt", "v1")
        ran: list[str] = []
        seen_at_run: list[int] = []

        def _fake(cmd):
            ran.append(cmd)
            # Proof this happened mid-watch: the loop is still running and the
            # shutdown line has not been printed yet.
            seen_at_run.append(len(ran))
            return 0

        monkeypatch.setattr(watch_mod, "_run_command", _fake)
        stop = threading.Event()

        def _edit_twice_then_stop():
            time.sleep(0.15)
            _write(tmp_path / "a.txt", "v2-longer")
            time.sleep(0.25)
            _write(tmp_path / "b.txt", "new file")
            time.sleep(0.25)
            stop.set()

        t = threading.Thread(target=_edit_twice_then_stop, daemon=True)
        t.start()
        rc = watch([tmp_path], interval=0.05, command="echo hi", stop=stop)
        t.join(timeout=3)

        out = capsys.readouterr().out
        assert ran == ["echo hi", "echo hi"], (
            f"expected one run per change batch, got {len(ran)}\n{out}"
        )
        assert seen_at_run == [1, 2]
        assert rc == 0

    def test_a_quiet_sweep_does_not_rerun_the_command(self, tmp_path, monkeypatch):
        """Idle ticks must not re-fire the command."""
        _write(tmp_path / "a.txt", "v1")
        ran: list[str] = []
        monkeypatch.setattr(watch_mod, "_run_command", lambda c: ran.append(c) or 0)
        stop = threading.Event()

        def _edit_then_idle():
            time.sleep(0.15)
            _write(tmp_path / "a.txt", "v2-longer")
            time.sleep(0.5)  # many quiet sweeps at interval=0.05
            stop.set()

        t = threading.Thread(target=_edit_then_idle, daemon=True)
        t.start()
        watch([tmp_path], interval=0.05, command="echo hi", stop=stop)
        t.join(timeout=3)

        assert ran == ["echo hi"]

    def test_no_change_means_no_command(self, tmp_path, monkeypatch):
        ran: list[str] = []
        monkeypatch.setattr(watch_mod, "_run_command", lambda c: ran.append(c) or 0)
        stop = threading.Event()
        stop.set()
        assert watch([tmp_path], interval=0.01, command="echo hi", stop=stop) == 0
        assert ran == []

    def test_the_last_command_failure_becomes_the_exit_code(
        self, tmp_path, monkeypatch
    ):
        _write(tmp_path / "a.txt", "v1")
        monkeypatch.setattr(watch_mod, "_run_command", lambda c: 3)
        stop = threading.Event()

        def _edit_then_stop():
            time.sleep(0.15)
            _write(tmp_path / "a.txt", "v2-longer")
            time.sleep(0.15)
            stop.set()

        t = threading.Thread(target=_edit_then_stop, daemon=True)
        t.start()
        rc = watch([tmp_path], interval=0.05, command="false", stop=stop)
        t.join(timeout=3)

        assert rc == 3

    def test_a_failing_command_reports_its_real_exit_code(self):
        """os.system returns a wait status: exit 1 arrives as 256.

        sys.exit(256) is truncated to 0, so the old path reported success for
        a failed command.
        """
        assert watch_mod._run_command(f'"{sys.executable}" -c "raise SystemExit(3)"') == 3
        assert watch_mod._run_command(f'"{sys.executable}" -c "pass"') == 0

    def test_the_shell_never_sees_a_wait_status(self):
        raw = os.system(f'"{sys.executable}" -c "raise SystemExit(1)"')
        assert raw != 1, "precondition: os.system is expected to return a wait status"
        assert watch_mod._run_command(f'"{sys.executable}" -c "raise SystemExit(1)"') == 1


class TestCliSurface:
    def _parser(self):
        p = argparse.ArgumentParser(prog="hermes")
        sub = p.add_subparsers(dest="command")
        build_watch_parser(sub)
        return p

    def test_defaults(self):
        args = self._parser().parse_args(["watch", "."])
        assert args.paths == ["."]
        assert args.command == "watch"
        assert args.recursive is True
        assert args.interval == 1.0
        assert args.pattern is None and args.ignore is None

    def test_command_flag_does_not_shadow_the_subcommand_dest(self):
        args = self._parser().parse_args(["watch", ".", "--command", "echo hi"])
        assert args.run_command == "echo hi"
        assert args.command == "watch", (
            "--command overwrote the subparser dest, so dispatch would look "
            "for a subcommand named after the user's shell string"
        )

    def test_flags_are_parsed(self):
        args = self._parser().parse_args(
            ["watch", "src", "tests", "-p", "*.py,*.js", "-i", "*.pyc", "--no-recursive"]
        )
        assert args.paths == ["src", "tests"]
        assert args.pattern == "*.py,*.js"
        assert args.ignore == "*.pyc"
        assert args.recursive is False

    def test_watch_is_a_known_builtin_subcommand(self):
        """Absent from this set, `hermes watch` pays for plugin discovery.

        _plugin_cli_discovery_needed() treats an unlisted first token as a
        possible plugin command and imports every bundled plugin module.
        """
        assert "watch" in _BUILTIN_SUBCOMMANDS

    def test_hermes_watch_help_works_end_to_end(self):
        """The parser is reachable through the real CLI, not just in-process."""
        proc = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "watch", "--help"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=Path(__file__).resolve().parents[2],
        )
        assert proc.returncode == 0, proc.stderr
        assert "--interval" in proc.stdout
