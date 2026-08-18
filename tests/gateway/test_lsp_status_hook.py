"""Gateway lifecycle publication of the cross-process LSP status."""

from gateway.run import GatewayRunner


def test_runtime_status_update_publishes_lsp_snapshot(monkeypatch):
    calls = {"runtime": [], "lsp": 0}

    def fake_runtime_status(**kwargs):
        calls["runtime"].append(kwargs)

    def fake_lsp_status():
        calls["lsp"] += 1

    monkeypatch.setattr("gateway.status.write_runtime_status", fake_runtime_status)
    monkeypatch.setattr("agent.lsp.publish_service_status", fake_lsp_status)
    monkeypatch.setattr(GatewayRunner, "_active_work_count", lambda self: 0)

    runner = object.__new__(GatewayRunner)
    runner._restart_requested = False
    runner._update_runtime_status("running")

    assert calls["runtime"] == [
        {
            "gateway_state": "running",
            "exit_reason": None,
            "restart_requested": False,
            "active_agents": 0,
        }
    ]
    assert calls["lsp"] == 1
