"""Readiness/liveness lifecycle tests (Task 17).

The webhook adapter must expose:
- ``/health`` — liveness only (process up, serving HTTP), always 200 when the
  handler is reachable.
- ``/ready`` — readiness (listener bound, routes loaded), 200 when ready and
  503 with a ``problems`` list when not. Never leaks secrets or route-config
  detail.
"""

from __future__ import annotations

import pytest

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


def _adapter(routes=None):
    extra = {"host": "127.0.0.1", "port": 0}
    if routes is not None:
        extra["routes"] = routes
    return WebhookAdapter(
        PlatformConfig(enabled=True, extra=extra)
    )


class TestReadinessLifecycle:
    @pytest.mark.asyncio
    async def test_health_is_liveness_only(self):
        adapter = _adapter()
        app = web.Application()
        app.router.add_get("/health", adapter._handle_health)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_ready_reports_not_ready_before_start(self):
        # Before connect(), the listener is not started.
        adapter = _adapter(routes={"a": {"secret": "x", "prompt": "p"}})
        app = web.Application()
        app.router.add_get("/ready", adapter._handle_ready)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/ready")
            assert resp.status == 503
            data = await resp.json()
            assert data["status"] == "not_ready"
            assert "listener not started" in data["problems"]

    @pytest.mark.asyncio
    async def test_ready_never_leaks_secret(self):
        adapter = _adapter(routes={"a": {"secret": "super-secret-value", "prompt": "p"}})
        app = web.Application()
        app.router.add_get("/ready", adapter._handle_ready)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/ready")
            body = await resp.text()
        assert "super-secret-value" not in body
        assert "prompt" not in body
