"""Regression coverage for macOS launchd gateway fleet restarts."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import hermes_cli.gateway as gateway
import hermes_cli.update_cmd as update_cmd


def test_get_service_pids_includes_every_loaded_launchd_gateway(monkeypatch):
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway, "is_macos", lambda: True)
    monkeypatch.setattr(
        gateway,
        "_expected_launchd_gateway_labels",
        lambda: {"ai.hermes.gateway", "ai.hermes.gateway-roz-ops"},
    )

    monkeypatch.setattr(gateway.os, "getuid", lambda: 501)
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        target = cmd[-1]
        calls.append(target)
        if target == "gui/501/ai.hermes.gateway":
            return SimpleNamespace(returncode=0, stdout="pid = 101\n")
        if target == "user/501/ai.hermes.gateway":
            return SimpleNamespace(returncode=0, stdout="pid = 303\n")
        if target == "user/501/ai.hermes.gateway-roz-ops":
            return SimpleNamespace(returncode=0, stdout="pid = 202\n")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)

    assert gateway._get_service_pids() == {101, 202, 303}
    assert "gui/501/ai.hermes.gateway" in calls
    assert "user/501/ai.hermes.gateway" in calls
    assert "user/501/ai.hermes.gateway-roz-ops" in calls
    assert all("fleet-update-watch" not in target for target in calls)


def test_launchd_domain_is_cached_per_gateway_label(monkeypatch):
    gateway._resolved_launchd_domain = None
    monkeypatch.setattr(gateway.os, "getuid", lambda: 501)
    calls: list[str] = []

    def fake_run(cmd, check=False, **kwargs):
        target = cmd[-1]
        calls.append(target)
        if target in {
            "gui/501/ai.hermes.gateway",
            "user/501/ai.hermes.gateway-roz-ops",
        }:
            return SimpleNamespace(returncode=0, stdout="")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)

    assert gateway._launchd_domain("ai.hermes.gateway") == "gui/501"
    assert gateway._launchd_domain("ai.hermes.gateway-roz-ops") == "user/501"
    assert "gui/501/ai.hermes.gateway-roz-ops" in calls
    assert "user/501/ai.hermes.gateway-roz-ops" in calls


def test_launchd_restart_uses_the_requested_labels_pid(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_list_launchd_gateway_services",
        lambda: [
            ("ai.hermes.gateway", "gui/501", 101),
            ("ai.hermes.gateway-roz-ops", "user/501", 202),
        ],
    )
    monkeypatch.setattr(gateway, "_launchd_domain", lambda label=None: "gui/501")
    requested: list[tuple[int, float]] = []
    monkeypatch.setattr(gateway, "_get_restart_drain_timeout", lambda: 42.0)
    monkeypatch.setattr(
        gateway,
        "_graceful_restart_via_sigusr1",
        lambda pid, drain_timeout: requested.append((pid, drain_timeout)) or True,
    )
    monkeypatch.setattr(
        gateway,
        "_request_gateway_self_restart",
        lambda pid: (_ for _ in ()).throw(
            AssertionError("explicit fleet restart must use the drain-aware path")
        ),
    )

    gateway.launchd_restart(
        "ai.hermes.gateway-roz-ops", domain="user/501", pid=202
    )

    assert requested == [(202, 42.0)]


def test_restart_launchd_gateway_fleet_continues_after_one_failure(monkeypatch):
    restart_fleet = getattr(update_cmd, "_restart_launchd_gateway_fleet", None)
    assert callable(restart_fleet), "macOS update path needs a launchd fleet helper"

    services = [
        ("ai.hermes.gateway", "gui/501", 101),
        ("ai.hermes.gateway", "user/501", 303),
        ("ai.hermes.gateway-roz-ops", "gui/501", 202),
    ]
    attempted: list[tuple[str, str, int | None]] = []

    monkeypatch.setattr(gateway, "get_launchd_gateway_services", lambda: services)

    def fake_restart(label: str, *, domain: str, pid: int | None):
        attempted.append((label, domain, pid))
        if label == "ai.hermes.gateway" and domain == "user/501":
            raise subprocess.TimeoutExpired(["launchctl", "kickstart", label], 5)

    monkeypatch.setattr(gateway, "launchd_restart", fake_restart)

    restarted: list[str] = []
    failed: list[str] = []
    restart_fleet(restarted, failed)

    assert attempted == services
    assert restarted == [
        "gui/501/ai.hermes.gateway",
        "gui/501/ai.hermes.gateway-roz-ops",
    ]
    assert failed == ["user/501/ai.hermes.gateway"]


def test_restart_launchd_gateway_fleet_skips_when_none_are_loaded(monkeypatch):
    restart_fleet = getattr(update_cmd, "_restart_launchd_gateway_fleet", None)
    assert callable(restart_fleet), "macOS update path needs a launchd fleet helper"

    monkeypatch.setattr(gateway, "get_launchd_gateway_services", lambda: [])
    monkeypatch.setattr(
        gateway,
        "launchd_restart",
        lambda label=None, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not restart")
        ),
    )

    restarted: list[str] = []
    failed: list[str] = []
    restart_fleet(restarted, failed)

    assert restarted == []
    assert failed == []
