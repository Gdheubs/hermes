"""Cross-surface webhook validation (Task 19).

Starts the real webhook adapter's aiohttp listener via ``connect()`` on a
free port with an isolated profile home, then exercises the deployed surface
end-to-end over real HTTP:

- listener lifecycle (connect/disconnect + liveness endpoint)
- intake contract (202 accepted, unknown-route 404)
- same-route delivery retry suppression (idempotency)

This proves the surface an operator actually deploys, not a mocked handler.
Fan-out (cross-route execute), 409 conflict, and the /ready readiness endpoint
are covered by their own task PRs' focused tests and land with those PRs.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _github_sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    # Isolate the dynamic-routes home so the test never touches the real
    # operator's webhook_subscriptions.json.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": _free_port(),
                "routes": {
                    "pr": {
                        "secret": "e2e-secret",
                        "signature_mode": "github",
                        "events": ["push"],
                        "prompt": "PR {x}",
                        "deliver": "log",
                    }
                },
            },
        )
    )


class TestCrossSurfaceWebhook:
    @pytest.mark.asyncio
    async def test_listener_lifecycle_and_liveness(self, adapter):
        assert await adapter.connect() is True
        try:
            import aiohttp
            host = adapter._host
            port = adapter._port
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{host}:{port}/health") as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["status"] == "ok"
                    assert data["platform"] == "webhook"
                    # Never leaks the route secret.
                    assert "e2e-secret" not in json.dumps(data)
        finally:
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_intake_contract_over_real_http(self, adapter):
        assert await adapter.connect() is True
        try:
            import aiohttp
            host = adapter._host
            port = adapter._port
            base = f"http://{host}:{port}"
            body = json.dumps({"x": 1}).encode()
            sig = _github_sig(body, "e2e-secret")
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "e2e-delivery",
            }
            async with aiohttp.ClientSession() as session:
                # Accepted
                async with session.post(f"{base}/webhooks/pr", data=body, headers=headers) as resp:
                    assert resp.status == 202
                    data = await resp.json()
                    assert data["status"] == "accepted"
                # Unknown route -> 404
                async with session.post(f"{base}/webhooks/nope", data=body, headers=headers) as resp:
                    assert resp.status == 404
        finally:
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_same_route_retry_suppressed_over_real_http(self, adapter):
        assert await adapter.connect() is True
        try:
            import aiohttp
            host = adapter._host
            port = adapter._port
            base = f"http://{host}:{port}"
            body = json.dumps({"x": 1}).encode()
            sig = _github_sig(body, "e2e-secret")
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "e2e-retry",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/webhooks/pr", data=body, headers=headers) as resp:
                    assert resp.status == 202
                # Retry with the same delivery id -> duplicate (200), not re-run.
                async with session.post(f"{base}/webhooks/pr", data=body, headers=headers) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["status"] == "duplicate"
        finally:
            await adapter.disconnect()
