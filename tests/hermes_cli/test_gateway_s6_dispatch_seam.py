"""Seam tests for the R5 extraction of ``_dispatch_all_via_service_manager_if_s6``.

The function body moved byte-verbatim from ``hermes_cli.gateway`` into the
new ``hermes_cli.s6_dispatch`` module; ``hermes_cli.gateway`` now re-exports
it via an identity-preserving import shim. These tests pin the seam:

- object identity through the re-export (callers and monkeypatchers of
  ``hermes_cli.gateway._dispatch_all_via_service_manager_if_s6`` must keep
  hitting the canonical object);
- module-global patch authority on ``hermes_cli.gateway`` (the Defense-1
  contract exercised by ``test_gateway_restart_loop.py``);
- behavioral equivalence of the canonical and re-exported callables,
  including the in-body ``hermes_cli.service_manager`` import semantics
  that ``test_gateway_s6_dispatch.py`` relies on.
"""
from __future__ import annotations

import pytest


def test_reexport_object_identity() -> None:
    """The re-export must be the exact same object, both directions."""
    import hermes_cli.gateway as gw
    import hermes_cli.s6_dispatch as sd

    assert getattr(gw, "_dispatch_all_via_service_manager_if_s6") is getattr(
        sd, "_dispatch_all_via_service_manager_if_s6"
    )
    assert sd._dispatch_all_via_service_manager_if_s6.__module__ == "hermes_cli.s6_dispatch"
    assert gw._dispatch_all_via_service_manager_if_s6.__module__ == "hermes_cli.s6_dispatch"


def test_module_global_patch_authority_via_reexport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatching the name on ``hermes_cli.gateway`` must shadow the
    re-export for module-global callers (Defense-1 contract in
    ``test_gateway_restart_loop.py``), while the canonical module keeps the
    real object."""
    import hermes_cli.gateway as gw
    import hermes_cli.s6_dispatch as sd

    sentinel = object()
    monkeypatch.setattr(gw, "_dispatch_all_via_service_manager_if_s6", sentinel)
    assert gw._dispatch_all_via_service_manager_if_s6 is sentinel
    assert sd._dispatch_all_via_service_manager_if_s6 is not sentinel
    assert callable(sd._dispatch_all_via_service_manager_if_s6)


class _ListingRecorder:
    """Minimal stand-in for S6ServiceManager with a profile list."""

    kind = "s6"

    def __init__(self, profiles: list[str]) -> None:
        self.calls: list[tuple[str, str]] = []
        self._profiles = profiles

    def list_profile_gateways(self) -> list[str]:
        return list(self._profiles)

    def stop(self, name: str) -> None:
        self.calls.append(("stop", name))

    def restart(self, name: str) -> None:
        self.calls.append(("restart", name))


def _stub_s6(monkeypatch: pytest.MonkeyPatch, rec: _ListingRecorder) -> None:
    monkeypatch.setattr(
        "hermes_cli.service_manager.detect_service_manager", lambda: "s6",
    )
    monkeypatch.setattr(
        "hermes_cli.service_manager.get_service_manager", lambda: rec,
    )


@pytest.mark.parametrize("surface", ["gateway", "s6_dispatch"])
def test_behavioral_equivalence(
    surface: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Both access surfaces behave identically under the same patches."""
    import hermes_cli.gateway as gw
    import hermes_cli.s6_dispatch as sd

    fn = gw._dispatch_all_via_service_manager_if_s6 if surface == "gateway" else sd._dispatch_all_via_service_manager_if_s6
    assert fn is gw._dispatch_all_via_service_manager_if_s6 is sd._dispatch_all_via_service_manager_if_s6

    # Not on s6 -> False (caller falls through to host path).
    monkeypatch.setattr("hermes_cli.service_manager.detect_service_manager", lambda: "systemd")
    monkeypatch.setattr("hermes_cli.service_manager.get_service_manager", lambda: _ListingRecorder([]))
    assert fn("stop") is False
    assert fn("restart") is False

    # s6, no registered profiles -> True + message.
    _stub_s6(monkeypatch, _ListingRecorder([]))
    capsys.readouterr()
    assert fn("stop") is True
    out = capsys.readouterr().out
    assert "No profile gateways registered under s6" in out

    # s6 with profiles -> dispatches each service, returns True.
    rec = _ListingRecorder(["coder", "writer"])
    _stub_s6(monkeypatch, rec)
    capsys.readouterr()
    assert fn("restart") is True
    assert rec.calls == [("restart", "gateway-coder"), ("restart", "gateway-writer")]
    out = capsys.readouterr().out
    assert "Restarted 2 profile gateway(s)" in out

    # stop verb selects mgr.stop.
    rec = _ListingRecorder(["coder"])
    _stub_s6(monkeypatch, rec)
    capsys.readouterr()
    assert fn("stop") is True
    assert rec.calls == [("stop", "gateway-coder")]

    # Unsupported action -> False even under s6.
    _stub_s6(monkeypatch, _ListingRecorder(["coder"]))
    assert fn("start") is False
    assert fn("banana") is False


def test_partial_failure_reports_and_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure on one profile must not skip the others; the helper
    reports each failure and the success count (mirrors the canonical
    behavioral test in test_gateway_s6_dispatch.py, driven through the
    new module)."""
    import hermes_cli.s6_dispatch as sd

    class _FailOnWriter(_ListingRecorder):
        def stop(self, name: str) -> None:
            if name == "gateway-writer":
                raise RuntimeError("supervise FIFO permission denied")
            super().stop(name)

    rec = _FailOnWriter(["coder", "writer", "assistant"])
    _stub_s6(monkeypatch, rec)
    assert sd._dispatch_all_via_service_manager_if_s6("stop") is True
    assert ("stop", "gateway-coder") in rec.calls
    assert ("stop", "gateway-assistant") in rec.calls
    assert ("stop", "gateway-writer") not in rec.calls
    out = capsys.readouterr().out
    assert "Stopped 2 profile gateway(s)" in out
    assert "Could not stop gateway-writer" in out
    assert "supervise FIFO permission denied" in out
