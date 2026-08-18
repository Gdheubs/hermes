"""Focused tests for the Lin Hermes bridge contract."""

from app.integrations.hermes_bridge import HermesBridge, HermesRuntimeConfig, restricted_event


class FakeResponse:
    def __init__(self, payload=None, body="", status_code=200):
        self._payload = payload
        self._body = body
        self.status_code = status_code
        self.text = body

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=True):
        return iter(self._body.splitlines())

    def close(self):
        pass


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def test_bridge_posts_run_with_authentication():
    session = FakeSession(FakeResponse({"run_id": "run_1", "status": "accepted"}))
    bridge = HermesBridge(HermesRuntimeConfig("http://hermes.test", "token"), session=session)
    result = bridge.start_run({"prompt": "run terminal"})

    assert result["run_id"] == "run_1"
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer token"


def test_bridge_restricts_event_payload():
    event = restricted_event(
        {
            "run_id": "run_1",
            "sequence": 1,
            "type": "tool.completed",
            "status": "success",
            "result_preview": "ok",
            "secret_internal_field": "must not pass",
        }
    )

    assert event["result_preview"] == "ok"
    assert "secret_internal_field" not in event


def test_bridge_parses_sse_events():
    body = (
        "event: agent_run\n"
        'data: {"run_id":"run_1","sequence":1,"type":"agent.started","status":"running"}\n\n'
        'data: {"run_id":"run_1","sequence":2,"type":"agent.completed","status":"success"}\n\n'
    )
    session = FakeSession(FakeResponse(body=body))
    bridge = HermesBridge(HermesRuntimeConfig("http://hermes.test", "token"), session=session)
    events = list(bridge.stream_events("run_1"))

    assert [event["type"] for event in events] == ["agent.started", "agent.completed"]
