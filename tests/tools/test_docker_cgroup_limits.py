"""Tests for cgroup resource-limit gating in the docker backend.

On hosts where the cgroup v2 cpu/memory/pids controllers are not delegated
(e.g. unprivileged Proxmox LXCs), passing ``--cpus``/``--memory``/``--pids-limit``
to ``docker run`` fails every container start with OCI runtime error / exit 126.
``_cgroup_limits_available`` probes once and the resource flags are gated on it,
so the sandbox degrades gracefully instead of failing.
"""
import subprocess

import pytest

import tools.environments.docker as docker_env


@pytest.fixture(autouse=True)
def _reset_cgroup_cache():
    """The probe result is cached in a module-level global; reset per test."""
    docker_env._cgroup_limits_ok = None
    yield
    docker_env._cgroup_limits_ok = None


def test_pids_limit_not_in_base_security_args():
    """``--pids-limit`` must NOT be hardcoded in the static security args.

    It requires the pids cgroup controller and is gated on the probe instead.
    """
    assert "--pids-limit" not in docker_env._BASE_SECURITY_ARGS


def test_probe_returns_true_when_container_starts(monkeypatch):
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    captured = {}

    def _run(cmd, *a, **k):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", _run)
    assert docker_env._cgroup_limits_available("hermes-agent:latest") is True
    # Probes all three controllers together against the real sandbox image.
    assert "--cpus" in captured["cmd"]
    assert "--memory" in captured["cmd"]
    assert "--pids-limit" in captured["cmd"]
    assert "hermes-agent:latest" in captured["cmd"]


def test_probe_result_is_cached(monkeypatch):
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    calls = []

    def _run(cmd, *a, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", _run)
    docker_env._cgroup_limits_available("img")
    docker_env._cgroup_limits_available("img")
    docker_env._cgroup_limits_available("img")
    assert len(calls) == 1  # probe runs once, then cached


def _docker_available_but_cgroup_fails(calls):
    """Mock docker: ``docker version`` succeeds (Docker present) but the
    cgroup probe (docker run with --cpus/--memory/--pids-limit) fails with
    exit 126 (OCI runtime error — controllers not delegated)."""
    def _run(cmd, *a, **k):
        calls.append(cmd)
        if cmd and cmd[0].endswith("version") or (len(cmd) > 1 and cmd[1] == "version"):
            return subprocess.CompletedProcess(cmd, 0, stdout="docker version...", stderr="")
        return subprocess.CompletedProcess(
            cmd, 126, stdout="", stderr="OCI runtime error: cgroup delegation"
        )
    return _run


def test_fails_closed_when_limits_requested_but_cgroup_unavailable(monkeypatch):
    """Requesting cpu/memory limits when the cgroup probe fails must raise an
    isolation-unavailable error (fail-closed), never silently run the
    container without the requested resource limits."""
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    calls = []
    monkeypatch.setattr(docker_env.subprocess, "run", _docker_available_but_cgroup_fails(calls))

    assert docker_env._cgroup_limits_available("img") is False

    with pytest.raises(RuntimeError, match="isolation unavailable"):
        docker_env.DockerEnvironment(
            image="img",
            cpu=2,  # explicitly requested resource limit
            cwd="/root",
        )


def test_no_raise_when_no_limits_requested_and_cgroup_unavailable(monkeypatch):
    """When the caller requests no cpu/memory limits, a failed cgroup probe
    must NOT raise — there is nothing to drop, so normal operation continues."""
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    calls = []
    monkeypatch.setattr(docker_env.subprocess, "run", _docker_available_but_cgroup_fails(calls))

    assert docker_env._cgroup_limits_available("img") is False

    # No cpu/memory requested -> should construct without raising.
    env = docker_env.DockerEnvironment(
        image="img",
        cpu=0,
        memory=0,
        cwd="/root",
    )
    assert env is not None


def test_raises_when_memory_requested_and_cgroup_unavailable(monkeypatch):
    """Same fail-closed guarantee when it is the memory limit that is
    requested (not just cpu)."""
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    calls = []
    monkeypatch.setattr(docker_env.subprocess, "run", _docker_available_but_cgroup_fails(calls))

    assert docker_env._cgroup_limits_available("img") is False

    with pytest.raises(RuntimeError, match="isolation unavailable"):
        docker_env.DockerEnvironment(
            image="img",
            memory=512,  # explicitly requested memory limit
            cwd="/root",
        )


