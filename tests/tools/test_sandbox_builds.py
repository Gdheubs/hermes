"""Tests for tools.sandbox_builds (pre-built Docker sandbox images)."""

import json
import time
from unittest import mock

import pytest

import tools.sandbox_builds as sb


@pytest.fixture(autouse=True)
def _isolated_builds_dir(tmp_path, monkeypatch):
    """Point the builds dir at a temp location and reset per-process state."""
    builds = tmp_path / "builds"
    builds.mkdir()
    monkeypatch.setattr(sb, "_builds_dir", lambda: builds)
    monkeypatch.setattr(sb, "_refresh_started", set())
    yield builds


def _record(fingerprint, status="success", finished_at=None, image_tag=None):
    return {
        "fingerprint": fingerprint,
        "status": status,
        "finished_at": finished_at if finished_at is not None else time.time(),
        "image_tag": image_tag or sb.image_tag_for(fingerprint),
        "base_image": "base:img",
        "command": "true",
    }


class TestFingerprint:
    def test_stable(self):
        assert sb.build_fingerprint("img", "cmd") == sb.build_fingerprint("img", "cmd")

    def test_varies_with_image_and_command(self):
        base = sb.build_fingerprint("img", "cmd")
        assert sb.build_fingerprint("img2", "cmd") != base
        assert sb.build_fingerprint("img", "cmd2") != base

    def test_no_separator_collision(self):
        # ("ab", "c") must not fingerprint like ("a", "bc")
        assert sb.build_fingerprint("ab", "c") != sb.build_fingerprint("a", "bc")


class TestLatestSuccessful:
    def test_none_when_empty(self):
        assert sb.latest_successful([], "f1") is None

    def test_ignores_failed_and_other_fingerprints(self):
        records = [
            _record("f1", status="failed"),
            _record("f2", status="success"),
        ]
        assert sb.latest_successful(records, "f1") is None

    def test_picks_newest_success(self):
        old = _record("f1", finished_at=100)
        new = _record("f1", finished_at=200)
        assert sb.latest_successful([old, new], "f1") is new


class TestResolveImage:
    def test_passthrough_without_command(self):
        assert sb.resolve_image("base:img", {}) == "base:img"
        assert sb.resolve_image("base:img", None) == "base:img"
        assert sb.resolve_image("base:img", {"docker_build_command": "  "}) == "base:img"

    def test_no_build_yet_returns_base_and_schedules(self, monkeypatch):
        started = []
        monkeypatch.setattr(
            sb, "_maybe_refresh_async",
            lambda *a, **k: started.append(k.get("reason")),
        )
        cc = {"docker_build_command": "pip install x"}
        assert sb.resolve_image("base:img", cc) == "base:img"
        assert started == ["initial"]

    def test_successful_build_returns_tag(self, monkeypatch):
        cc = {"docker_build_command": "pip install x", "docker_build_refresh_hours": 0}
        fp = sb.build_fingerprint("base:img", "pip install x")
        sb._save_metadata([_record(fp)])
        monkeypatch.setattr(sb, "_image_exists", lambda tag: True)
        assert sb.resolve_image("base:img", cc) == sb.image_tag_for(fp)

    def test_missing_image_falls_back(self, monkeypatch):
        cc = {"docker_build_command": "pip install x"}
        fp = sb.build_fingerprint("base:img", "pip install x")
        sb._save_metadata([_record(fp)])
        monkeypatch.setattr(sb, "_image_exists", lambda tag: False)
        scheduled = []
        monkeypatch.setattr(
            sb, "_maybe_refresh_async",
            lambda *a, **k: scheduled.append(k.get("reason")),
        )
        assert sb.resolve_image("base:img", cc) == "base:img"
        assert scheduled == ["missing-image"]

    def test_failed_build_never_used(self, monkeypatch):
        cc = {"docker_build_command": "pip install x"}
        fp = sb.build_fingerprint("base:img", "pip install x")
        sb._save_metadata([_record(fp, status="failed")])
        monkeypatch.setattr(sb, "_maybe_refresh_async", lambda *a, **k: None)
        assert sb.resolve_image("base:img", cc) == "base:img"

    def test_config_change_ignores_old_build(self, monkeypatch):
        old_fp = sb.build_fingerprint("base:img", "old command")
        sb._save_metadata([_record(old_fp)])
        monkeypatch.setattr(sb, "_image_exists", lambda tag: True)
        monkeypatch.setattr(sb, "_maybe_refresh_async", lambda *a, **k: None)
        cc = {"docker_build_command": "new command"}
        assert sb.resolve_image("base:img", cc) == "base:img"

    def test_stale_build_triggers_refresh_but_keeps_image(self, monkeypatch):
        cc = {"docker_build_command": "pip install x", "docker_build_refresh_hours": 1}
        fp = sb.build_fingerprint("base:img", "pip install x")
        sb._save_metadata([_record(fp, finished_at=time.time() - 7200)])
        monkeypatch.setattr(sb, "_image_exists", lambda tag: True)
        scheduled = []
        monkeypatch.setattr(
            sb, "_maybe_refresh_async",
            lambda *a, **k: scheduled.append(k.get("reason")),
        )
        assert sb.resolve_image("base:img", cc) == sb.image_tag_for(fp)
        assert scheduled == ["stale"]

    def test_refresh_zero_disables_staleness(self, monkeypatch):
        cc = {"docker_build_command": "pip install x", "docker_build_refresh_hours": 0}
        fp = sb.build_fingerprint("base:img", "pip install x")
        sb._save_metadata([_record(fp, finished_at=0)])  # ancient
        monkeypatch.setattr(sb, "_image_exists", lambda tag: True)
        scheduled = []
        monkeypatch.setattr(
            sb, "_maybe_refresh_async",
            lambda *a, **k: scheduled.append(k.get("reason")),
        )
        assert sb.resolve_image("base:img", cc) == sb.image_tag_for(fp)
        assert scheduled == []

    def test_resolve_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            sb, "_load_metadata", mock.Mock(side_effect=RuntimeError("boom"))
        )
        cc = {"docker_build_command": "pip install x"}
        assert sb.resolve_image("base:img", cc) == "base:img"


class TestRefreshDedup:
    def test_one_background_build_per_fingerprint(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sb, "run_build", lambda *a, **k: calls.append(a))
        threads = []

        class _FakeThread:
            def __init__(self, target=None, **kwargs):
                self._target = target
                threads.append(self)

            def start(self):
                self._target()

        monkeypatch.setattr(sb.threading, "Thread", _FakeThread)
        cc = {"docker_build_command": "x"}
        sb._maybe_refresh_async("img", "x", cc, reason="initial")
        sb._maybe_refresh_async("img", "x", cc, reason="initial")
        assert len(calls) == 1


class TestMetadataRoundTrip:
    def test_save_and_load(self, _isolated_builds_dir):
        records = [_record("f1"), _record("f2", status="failed")]
        sb._save_metadata(records)
        loaded = sb._load_metadata()
        assert [r["fingerprint"] for r in loaded] == ["f1", "f2"]

    def test_corrupt_metadata_returns_empty(self, _isolated_builds_dir):
        (_isolated_builds_dir / "builds.json").write_text("{not json", encoding="utf-8")
        assert sb._load_metadata() == []


class TestRunBuild:
    def _mock_popen(self, exit_code=0, output="ok\n"):
        proc = mock.Mock()
        proc.stdout = iter(output.splitlines(keepends=True))
        proc.wait = mock.Mock(return_value=exit_code)
        return proc

    def test_success_commits_and_records(self, monkeypatch):
        monkeypatch.setattr(sb, "_docker_exe", lambda: "docker")
        popen = mock.Mock(return_value=self._mock_popen(0))
        monkeypatch.setattr(sb.subprocess, "Popen", popen)
        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return mock.Mock(returncode=0, stderr="")

        monkeypatch.setattr(sb.subprocess, "run", fake_run)
        record = sb.run_build("base:img", "true")
        assert record["status"] == "success"
        assert any(c[:2] == ["docker", "commit"] for c in run_calls)
        # rm -f cleanup always happens
        assert any(c[:3] == ["docker", "rm", "-f"] for c in run_calls)
        stored = sb._load_metadata()
        assert stored[-1]["status"] == "success"

    def test_failed_command_records_failure_and_skips_commit(self, monkeypatch):
        monkeypatch.setattr(sb, "_docker_exe", lambda: "docker")
        popen = mock.Mock(return_value=self._mock_popen(3, "boom\n"))
        monkeypatch.setattr(sb.subprocess, "Popen", popen)
        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return mock.Mock(returncode=0, stderr="")

        monkeypatch.setattr(sb.subprocess, "run", fake_run)
        record = sb.run_build("base:img", "false")
        assert record["status"] == "failed"
        assert record["exit_code"] == 3
        assert not any(c[:2] == ["docker", "commit"] for c in run_calls)
        # A prior success for the same fingerprint stays active.
        fp = record["fingerprint"]
        sb._save_metadata(sb._load_metadata() + [_record(fp, finished_at=1)])
        active = sb.latest_successful(sb._load_metadata(), fp)
        assert active is not None and active["status"] == "success"


class TestStatusSummary:
    def test_unconfigured(self):
        info = sb.status_summary("base:img", "")
        assert info["configured"] is False
        assert info["active"] is None

    def test_active_build_reported(self, monkeypatch):
        fp = sb.build_fingerprint("base:img", "cmd")
        sb._save_metadata([_record(fp)])
        monkeypatch.setattr(sb, "_image_exists", lambda tag: True)
        info = sb.status_summary("base:img", "cmd")
        assert info["configured"] is True
        assert info["active"]["fingerprint"] == fp


class TestCreateEnvironmentIntegration:
    def test_docker_env_uses_resolved_image(self, monkeypatch):
        """_create_environment routes the image through sandbox build resolution."""
        import tools.terminal_tool as tt

        captured = {}

        class FakeDockerEnv:
            def __init__(self, image=None, **kwargs):
                captured["image"] = image

        monkeypatch.setattr(tt, "_DockerEnvironment", FakeDockerEnv)
        monkeypatch.setattr(tt, "_maybe_reap_docker_orphans", lambda cc: None)
        monkeypatch.setattr(sb, "_image_exists", lambda tag: True)
        monkeypatch.setattr(sb, "_maybe_refresh_async", lambda *a, **k: None)
        fp = sb.build_fingerprint("base:img", "pip install x")
        sb._save_metadata([_record(fp)])
        tt._create_environment(
            "docker", "base:img", "/root", 60,
            container_config={
                "docker_build_command": "pip install x",
                "docker_build_refresh_hours": 0,
            },
        )
        assert captured["image"] == sb.image_tag_for(fp)

    def test_docker_env_unchanged_without_command(self, monkeypatch):
        import tools.terminal_tool as tt

        captured = {}

        class FakeDockerEnv:
            def __init__(self, image=None, **kwargs):
                captured["image"] = image

        monkeypatch.setattr(tt, "_DockerEnvironment", FakeDockerEnv)
        monkeypatch.setattr(tt, "_maybe_reap_docker_orphans", lambda cc: None)
        tt._create_environment("docker", "base:img", "/root", 60, container_config={})
        assert captured["image"] == "base:img"
