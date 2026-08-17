"""Focused tests for the isolated Lin-facing Hermes runtime API."""

import json
import time

from fastapi.testclient import TestClient

from lin_runtime.app import create_app


class FakeAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self._interrupt_requested = False

    def run_conversation(self, message, **kwargs):
        self.tool_progress_callback(
            "tool.started", "terminal", '{"command":"printf ok"}', {"command": "printf ok"}
        )
        self.tool_progress_callback(
            "tool.completed",
            "terminal",
            None,
            None,
            duration=0.01,
            is_error=False,
            result="ok",
        )
        return {"final_response": "ok"}


def test_runtime_requires_service_token():
    app = create_app(agent_factory=FakeAgent, service_token="secret")
    client = TestClient(app)

    response = client.post("/agent-runs", json={"prompt": "run"})

    assert response.status_code == 401


def test_runtime_emits_agent_and_tool_lifecycle_events():
    app = create_app(agent_factory=FakeAgent, service_token="secret")
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}

    created = client.post("/agent-runs", headers=headers, json={"prompt": "run"})
    assert created.status_code == 202
    run_id = created.json()["run_id"]

    deadline = time.time() + 2
    events = []
    while time.time() < deadline:
        response = client.get(
            f"/agent-runs/{run_id}/events", headers=headers, params={"after": len(events)}
        )
        assert response.status_code == 200
        for line in response.text.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        if any(event["type"] == "agent.completed" for event in events):
            break
        time.sleep(0.02)

    types = [event["type"] for event in events]
    assert types[0] == "agent.started"
    assert "tool.started" in types
    assert "tool.completed" in types
    assert types[-1] == "agent.completed"
    assert all(event["run_id"] == run_id for event in events)
    assert all("sequence" in event for event in events)


def test_runtime_cancel_marks_run_cancelled():
    class BlockingAgent(FakeAgent):
        def run_conversation(self, message, **kwargs):
            while not self._interrupt_requested:
                time.sleep(0.01)
            return {"final_response": "cancelled"}

    app = create_app(agent_factory=BlockingAgent, service_token="secret")
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}

    created = client.post("/agent-runs", headers=headers, json={"prompt": "run"})
    run_id = created.json()["run_id"]

    cancelled = client.post(f"/agent-runs/{run_id}/cancel", headers=headers)
    assert cancelled.status_code == 202

    deadline = time.time() + 2
    while time.time() < deadline:
        status = client.get(f"/agent-runs/{run_id}", headers=headers).json()
        if status["status"] == "cancelled":
            break
        time.sleep(0.02)

    assert status["status"] == "cancelled"
