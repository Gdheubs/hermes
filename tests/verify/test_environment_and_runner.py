"""Tests for the verify environment manifest and the smoke runner."""

import http.server
import json
import os
import stat
import subprocess
import sys
import threading
import time
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.verify import runner
from agent.verify.environment import (
    load_manifest,
    load_or_detect,
    manifest_path,
    save_manifest,
)
from agent.verify.recipes import Recipe, detect_recipe
from agent.verify.runner import run_verify


class TestManifest:
    def test_roundtrip(self, tmp_path):
        recipe = Recipe(
            name="Next.js",
            kind="nextjs",
            bootstrap=["npm install"],
            build=["npm run build"],
            test=["npm test"],
            start="npm run dev",
            port=3000,
            readiness_path="/health",
        )
        path = save_manifest(tmp_path, recipe)
        assert path == manifest_path(tmp_path)
        payload = json.loads(path.read_text())
        assert payload["version"] == 1
        assert "updatedAt" in payload
        assert load_manifest(tmp_path) == recipe

    def test_missing_file(self, tmp_path):
        assert load_manifest(tmp_path) is None

    def test_malformed_json_tolerated(self, tmp_path):
        path = manifest_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{oops", encoding="utf-8")
        assert load_manifest(tmp_path) is None

    def test_non_dict_tolerated(self, tmp_path):
        path = manifest_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert load_manifest(tmp_path) is None

    def test_bare_recipe_shape_accepted(self, tmp_path):
        path = manifest_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"name": "Custom", "test": ["true"]}), encoding="utf-8")
        recipe = load_manifest(tmp_path)
        assert recipe.name == "Custom"
        assert recipe.test == ["true"]

    def test_manifest_wins_over_detection(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        save_manifest(tmp_path, Recipe(name="Custom", kind="custom", test=["true"]))
        recipe, source = load_or_detect(tmp_path)
        assert source == "manifest"
        assert recipe.name == "Custom"

    def test_detection_fallback(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        recipe, source = load_or_detect(tmp_path)
        assert source == "detected"
        assert recipe.kind == "go"


class TestRunner:
    def test_all_phases_pass(self, tmp_path):
        recipe = Recipe(name="x", bootstrap=["true"], build=["true"], test=["true"])
        result = run_verify(tmp_path, recipe, skip_start=True)
        assert result.ok
        assert [p.phase for p in result.phases] == ["bootstrap", "build", "test"]
        assert all(p.exit_code == 0 for p in result.phases)
        assert all(p.duration >= 0 for p in result.phases)

    def test_failure_stops_pipeline(self, tmp_path):
        recipe = Recipe(name="x", build=["false"], test=["true"])
        result = run_verify(tmp_path, recipe, skip_start=True)
        assert not result.ok
        assert len(result.phases) == 1
        assert result.phases[0].exit_code == 1

    def test_output_captured(self, tmp_path):
        recipe = Recipe(name="x", test=["echo hello-verify"])
        result = run_verify(tmp_path, recipe, skip_start=True)
        assert "hello-verify" in result.phases[0].output_tail

    def test_phase_selection(self, tmp_path):
        recipe = Recipe(name="x", bootstrap=["true"], build=["true"], test=["true"])
        result = run_verify(tmp_path, recipe, phases=("test",))
        assert [p.phase for p in result.phases] == ["test"]

    def test_phase_timeout(self, tmp_path):
        recipe = Recipe(name="x", test=["sleep 5"])
        result = run_verify(tmp_path, recipe, phase_timeout=0.3, skip_start=True)
        assert not result.ok
        assert result.phases[0].timed_out
        assert result.phases[0].exit_code is None

    def test_commands_run_in_project_root(self, tmp_path):
        (tmp_path / "marker.txt").write_text("here", encoding="utf-8")
        recipe = Recipe(name="x", test=["cat marker.txt"])
        result = run_verify(tmp_path, recipe, skip_start=True)
        assert result.ok

    def test_result_to_dict(self, tmp_path):
        recipe = Recipe(name="x", test=["true"])
        payload = run_verify(tmp_path, recipe, skip_start=True).to_dict()
        assert payload["ok"] is True
        assert payload["recipe"] == "x"
        assert payload["phases"][0]["command"] == "true"
        assert payload["readiness"] is None


class TestPythonIsolation:
    @pytest.mark.parametrize("kind", ["python", "django", "fastapi", "flask"])
    def test_established_python_recipe_kinds_use_isolation(self, tmp_path, monkeypatch, kind):
        calls = []

        def fake_environment(root):
            calls.append(root)
            return os.environ.copy()

        monkeypatch.setattr(runner, "_ensure_python_environment", fake_environment, raising=False)
        recipe = Recipe.from_dict({"name": "Saved Python", "kind": kind})
        assert recipe is not None

        result = run_verify(tmp_path, recipe, skip_start=True)

        assert result.ok
        assert calls == [tmp_path]

    def test_detected_python_recipe_uses_isolation(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
        recipe = detect_recipe(tmp_path)
        assert recipe is not None
        calls = []

        def fake_environment(root):
            calls.append(root)
            return os.environ.copy()

        monkeypatch.setattr(runner, "_ensure_python_environment", fake_environment)

        result = run_verify(tmp_path, recipe, skip_start=True, phases=("build",))

        assert result.ok
        assert recipe.kind == "flask"
        assert calls == [tmp_path]

    def test_all_command_phases_use_project_virtual_environment(self, tmp_path, monkeypatch):
        root = tmp_path / "project with spaces"
        root.mkdir()
        monkeypatch.setenv("PYTHONHOME", "/foreign/python/home")
        probe = (
            "python -c \"import os,sys; "
            "print('|'.join((sys.prefix, os.environ['VIRTUAL_ENV'], "
            "os.environ['PATH'].split(os.pathsep)[0], str(os.environ.get('PYTHONHOME')))))\""
        )
        recipe = Recipe(
            name="Python project",
            kind="python",
            bootstrap=[probe],
            build=[probe],
            test=[probe],
        )

        result = run_verify(root, recipe, skip_start=True)

        venv = root / ".hermes" / "verify-venv"
        scripts = runner._scripts_dir_for_venv(venv, os_name=os.name)
        assert result.ok
        assert [phase.phase for phase in result.phases] == ["bootstrap", "build", "test"]
        for phase in result.phases:
            prefix, virtual_env, path_head, pythonhome = phase.output_tail.strip().split("|")
            assert Path(prefix) == venv
            assert Path(virtual_env) == venv
            assert Path(path_head) == scripts
            assert pythonhome == "None"

    def test_start_phase_uses_project_virtual_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PYTHONHOME", "/foreign/python/home")
        port = _free_port()
        probe = (
            "import http.server,os,sys; "
            "print('|'.join((sys.prefix, os.environ['VIRTUAL_ENV'], "
            "os.environ['PATH'].split(os.pathsep)[0], str(os.environ.get('PYTHONHOME')))), "
            "flush=True); "
            f"http.server.ThreadingHTTPServer(('127.0.0.1', {port}), "
            "http.server.SimpleHTTPRequestHandler).serve_forever()"
        )
        recipe = Recipe(
            name="Python project",
            kind="python",
            start=f'python -u -c "{probe}"',
            port=port,
        )

        result = run_verify(tmp_path, recipe, phases=("start",), ready_timeout=15)

        assert result.ok
        assert result.readiness is not None
        prefix, virtual_env, path_head, pythonhome = (
            result.readiness.output_tail.splitlines()[0].split("|")
        )
        venv = tmp_path / ".hermes" / "verify-venv"
        assert Path(prefix) == venv
        assert Path(virtual_env) == venv
        assert Path(path_head) == runner._scripts_dir_for_venv(venv)
        assert pythonhome == "None"

    def test_non_python_recipe_preserves_environment_and_creates_no_venv(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("VERIFY_CALLER_MARKER", "preserved")
        recipe = Recipe(
            name="Go project",
            kind="go",
            test=['printf "%s\\n" "$VERIFY_CALLER_MARKER"'],
        )

        result = run_verify(tmp_path, recipe, skip_start=True)

        assert result.ok
        assert result.phases[0].output_tail.strip() == "preserved"
        assert not (tmp_path / ".hermes" / "verify-venv").exists()

    def test_existing_project_virtual_environment_is_reused(self, tmp_path, monkeypatch):
        recipe = Recipe(name="Python project", kind="python", test=["python -c \"pass\""])
        assert run_verify(tmp_path, recipe, skip_start=True).ok

        def fail_if_recreated(*args, **kwargs):
            raise AssertionError("valid project environment must be reused")

        monkeypatch.setattr(runner.venv.EnvBuilder, "create", fail_if_recreated)
        assert run_verify(tmp_path, recipe, skip_start=True).ok

    def test_invalid_existing_environment_fails_closed_before_bootstrap(
        self, tmp_path, monkeypatch
    ):
        venv = tmp_path / ".hermes" / "verify-venv"
        python = runner._python_for_venv(venv)
        python.parent.mkdir(parents=True)
        python.touch()
        (venv / "pyvenv.cfg").write_text("home = foreign\n", encoding="utf-8")
        monkeypatch.setattr(
            runner.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0,
                stdout=str(tmp_path / "foreign-environment") + "\n",
            ),
        )

        def fail_repair(*args, **kwargs):
            raise OSError("invalid environment could not be repaired")

        monkeypatch.setattr(runner.venv.EnvBuilder, "create", fail_repair)

        result = run_verify(
            tmp_path,
            Recipe(name="Python project", kind="python"),
            skip_start=True,
        )

        assert not result.ok
        assert result.phases[0].phase == "isolation"
        assert "invalid environment could not be repaired" in result.phases[0].output_tail

    def test_setup_failure_fails_closed_before_bootstrap(self, tmp_path, monkeypatch):
        marker = tmp_path / "bootstrap-ran"

        def fail_create(*args, **kwargs):
            raise OSError("ensurepip is unavailable")

        monkeypatch.setattr(runner.venv.EnvBuilder, "create", fail_create)
        recipe = Recipe(
            name="Python project",
            kind="python",
            bootstrap=[f'python -c "from pathlib import Path; Path(r\'{marker}\').touch()"'],
        )

        result = run_verify(tmp_path, recipe, skip_start=True)

        assert not result.ok
        assert not marker.exists()
        assert len(result.phases) == 1
        assert result.phases[0].phase == "isolation"
        assert "ensurepip is unavailable" in result.phases[0].output_tail
        assert ".hermes" in result.phases[0].output_tail
        assert "verify-venv" in result.phases[0].output_tail

    @pytest.mark.parametrize(
        ("os_name", "directory"),
        [("posix", "bin"), ("nt", "Scripts")],
    )
    def test_scripts_directory_is_cross_platform_and_space_safe(self, os_name, directory):
        venv = Path("project with spaces") / ".hermes" / "verify-venv"

        expected_separator = ";" if os_name == "nt" else ":"
        scripts = runner._scripts_dir_for_venv(venv, os_name=os_name)
        environment = runner._environment_for_venv(
            venv,
            {
                "PATH": expected_separator.join(("caller tools", "more tools")),
                "PYTHONHOME": "foreign",
            },
            os_name=os_name,
        )

        assert scripts == venv / directory
        assert environment["VIRTUAL_ENV"] == str(venv)
        assert environment["PATH"] == expected_separator.join(
            (str(venv / directory), "caller tools", "more tools")
        )
        assert "PYTHONHOME" not in environment

    def test_detected_uv_recipe_uses_one_project_environment(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
        (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        tools = tmp_path / "tools"
        tools.mkdir()
        uv = tools / "uv"
        uv.write_text(
            "#!/bin/sh\nprintf '%s' \"$UV_PROJECT_ENVIRONMENT\" > uv-environment.txt\n",
            encoding="utf-8",
        )
        uv.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
        recipe = detect_recipe(tmp_path)
        assert recipe is not None
        assert recipe.bootstrap == ["uv sync"]
        probe = (
            "python -c \"import os,sys; "
            "print(sys.prefix + '|' + os.environ['UV_PROJECT_ENVIRONMENT'])\""
        )
        recipe.build = [probe]
        recipe.test = [probe]

        result = run_verify(tmp_path, recipe, skip_start=True)

        expected = str((tmp_path / ".hermes" / "verify-venv").resolve())
        assert result.ok
        assert (tmp_path / "uv-environment.txt").read_text() == expected
        for phase in result.phases[1:]:
            assert phase.output_tail.strip() == f"{expected}|{expected}"

    @pytest.mark.parametrize("redirect", ["metadata", "cache"])
    def test_redirected_project_cache_is_rejected_without_touching_external_target(
        self, tmp_path, redirect
    ):
        root = tmp_path / "project"
        root.mkdir()
        external = tmp_path / "caller-environment"
        if redirect == "metadata":
            external.mkdir()
            (root / ".hermes").symlink_to(external, target_is_directory=True)
        else:
            (root / ".hermes").mkdir()
            venv.EnvBuilder(with_pip=True).create(external)
            (root / ".hermes" / "verify-venv").symlink_to(
                external, target_is_directory=True
            )
        marker = external / "caller-marker"
        marker.write_text("unchanged", encoding="utf-8")

        result = run_verify(
            root,
            Recipe(name="Python project", kind="python", test=["python -c \"pass\""]),
            skip_start=True,
        )

        assert not result.ok
        assert result.phases[0].phase == "isolation"
        assert "redirect" in result.phases[0].output_tail.lower()
        assert marker.read_text(encoding="utf-8") == "unchanged"

    def test_windows_reparse_point_is_classified_as_redirect(self, tmp_path):
        path = tmp_path / "junction"
        fake_stat = SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )

        assert runner._path_is_redirect(
            path,
            os_name="nt",
            lstat=lambda _path: fake_stat,
        )

    def test_invalid_cache_is_deleted_before_clean_recreation(self, tmp_path):
        recipe = Recipe(name="Python project", kind="python", test=["python -c \"pass\""])
        assert run_verify(tmp_path, recipe, skip_start=True).ok
        cache = tmp_path / ".hermes" / "verify-venv"
        python = runner._python_for_venv(cache)
        purelib = Path(
            subprocess.check_output(
                [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                text=True,
            ).strip()
        )
        stale = purelib / "hermes_stale_canary.py"
        stale.write_text("STALE = True\n", encoding="utf-8")
        (cache / "pyvenv.cfg").unlink()

        result = run_verify(tmp_path, recipe, skip_start=True)

        assert result.ok
        assert not stale.exists()

    def test_two_threads_serialize_complete_python_lifecycle(self, tmp_path, monkeypatch):
        recipe = Recipe(name="Python project", kind="python", test=["probe"])
        assert run_verify(
            tmp_path,
            Recipe(name="Python project", kind="python"),
            skip_start=True,
        ).ok
        state = {"active": 0, "maximum": 0}
        state_lock = threading.Lock()

        def slow_phase(phase, command, root, timeout, on_output=None, environment=None):
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.2)
            with state_lock:
                state["active"] -= 1
            return runner.PhaseResult(phase, command, 0, 0.2, "")

        monkeypatch.setattr(runner, "_run_phase_command", slow_phase)
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(run_verify(tmp_path, recipe, skip_start=True)))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert len(results) == 2
        assert all(result.ok for result in results)
        assert state["maximum"] == 1

    def test_second_process_waits_for_complete_python_lifecycle(self, tmp_path):
        recipe = Recipe(name="Python project", kind="python")
        assert run_verify(tmp_path, recipe, skip_start=True).ok
        repository = Path(__file__).resolve().parents[2]
        worker = (
            "import json,sys; from pathlib import Path; "
            "from agent.verify.recipes import Recipe; from agent.verify.runner import run_verify; "
            "r=run_verify(Path(sys.argv[1]), Recipe(name='p',kind='python',"
            "test=[\"python -c 'import time; time.sleep(0.7)'\"]), skip_start=True); "
            "print(json.dumps(r.to_dict())); raise SystemExit(0 if r.ok else 1)"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(repository)
        started = time.monotonic()
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", worker, str(tmp_path)],
                cwd=repository,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for _ in range(2)
        ]
        receipts = [process.communicate(timeout=20) for process in processes]
        elapsed = time.monotonic() - started

        assert [process.returncode for process in processes] == [0, 0], receipts
        assert elapsed >= 1.2, receipts

    def test_redirected_lock_target_is_rejected_without_touching_external_file(self, tmp_path):
        metadata = tmp_path / ".hermes"
        metadata.mkdir()
        external = tmp_path / "external-lock"
        external.write_text("unchanged", encoding="utf-8")
        (metadata / "verify.lock").symlink_to(external)

        result = run_verify(
            tmp_path,
            Recipe(name="Python project", kind="python", test=["python -c \"pass\""]),
            skip_start=True,
        )

        assert not result.ok
        assert result.phases[0].phase == "isolation"
        assert "redirect" in result.phases[0].output_tail.lower()
        assert external.read_text(encoding="utf-8") == "unchanged"

    def test_lock_timeout_fails_before_project_commands(self, tmp_path):
        entered = threading.Event()
        release = threading.Event()

        def hold_lock():
            with runner._project_python_lock(tmp_path.resolve(), 2):
                entered.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        assert entered.wait(timeout=5)
        marker = tmp_path / "command-ran"
        try:
            result = run_verify(
                tmp_path,
                Recipe(name="Python project", kind="python", test=[f"touch {marker}"]),
                phase_timeout=0.1,
                skip_start=True,
            )
        finally:
            release.set()
            holder.join(timeout=5)

        assert not result.ok
        assert result.phases[0].phase == "isolation"
        assert "timed out waiting" in result.phases[0].output_tail
        assert not marker.exists()

    def test_environment_manifest_is_preserved(self, tmp_path):
        metadata = tmp_path / ".hermes"
        metadata.mkdir()
        manifest = metadata / "environment.json"
        manifest.write_text('{"preserve": true}\n', encoding="utf-8")

        result = run_verify(
            tmp_path,
            Recipe(name="Python project", kind="python", test=["python -c \"pass\""]),
            skip_start=True,
        )

        assert result.ok
        assert manifest.read_text(encoding="utf-8") == '{"preserve": true}\n'


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestReadiness:
    def test_readiness_against_live_server(self, tmp_path):
        port = _free_port()
        recipe = Recipe(
            name="x",
            start=f"python3 -m http.server {port} --bind 127.0.0.1",
            port=port,
        )
        result = run_verify(tmp_path, recipe, phases=("start",), ready_timeout=15)
        assert result.readiness is not None
        assert result.readiness.ready
        assert result.readiness.status_code == 200
        assert result.readiness.url == f"http://127.0.0.1:{port}/"
        assert result.ok

    def test_readiness_timeout_when_nothing_listens(self, tmp_path):
        port = _free_port()
        recipe = Recipe(name="x", start="sleep 30", port=port)
        result = run_verify(tmp_path, recipe, phases=("start",), ready_timeout=1.5)
        assert result.readiness is not None
        assert not result.readiness.ready
        assert not result.ok

    def test_skip_start(self, tmp_path):
        recipe = Recipe(name="x", test=["true"], start="sleep 30", port=1)
        result = run_verify(tmp_path, recipe, skip_start=True)
        assert result.readiness is None
        assert result.ok

    def test_start_skipped_after_phase_failure(self, tmp_path):
        recipe = Recipe(name="x", test=["false"], start="sleep 30", port=1)
        result = run_verify(tmp_path, recipe, stop_on_failure=False)
        assert result.readiness is None
        assert not result.ok

    def test_port_override(self, tmp_path):
        port = _free_port()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.05)
        try:
            recipe = Recipe(name="x", start="sleep 30", port=1)
            result = run_verify(
                tmp_path, recipe, phases=("start",), ready_timeout=10, port_override=port
            )
            assert result.readiness.ready
            assert result.readiness.status_code == 204
        finally:
            server.shutdown()
            thread.join(timeout=5)
