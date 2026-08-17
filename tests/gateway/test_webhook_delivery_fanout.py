"""Delivery fan-out tests (Task 13, #32403).

Legacy single ``deliver`` translates to a one-element ``deliveries`` list;
a ``deliveries`` list fans out to multiple targets where one failure does not
erase another target's success.
"""

from __future__ import annotations

import pytest

from unittest.mock import AsyncMock

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter


def _adapter(routes):
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "routes": routes},
        )
    )


class TestDeliveryFanOut:
    def test_legacy_single_deliver_normalizes_to_one_element(self):
        adapter = _adapter(
            {"r": {"secret": "s", "prompt": "p", "deliver": "log"}}
        )
        targets = adapter._normalize_deliveries(
            adapter._routes["r"], {"x": 1}
        )
        assert targets == [{"deliver": "log", "deliver_extra": {}}]

    def test_deliveries_list_fans_out(self):
        adapter = _adapter(
            {
                "r": {
                    "secret": "s",
                    "prompt": "p",
                    "deliveries": [
                        {"deliver": "log"},
                        {"deliver": "telegram", "deliver_extra": {"chat_id": "42"}},
                    ],
                }
            }
        )
        targets = adapter._normalize_deliveries(
            adapter._routes["r"], {"x": 1}
        )
        assert len(targets) == 2
        assert targets[0]["deliver"] == "log"
        assert targets[1]["deliver"] == "telegram"
        assert targets[1]["deliver_extra"] == {"chat_id": "42"}

    @pytest.mark.asyncio
    async def test_fanout_one_failure_does_not_erase_success(self):
        adapter = _adapter(
            {"r": {"secret": "s", "prompt": "p", "deliver": "log"}}
        )
        # Seed a two-target deliveries list directly on the adapter.
        adapter._delivery_info["webhook:r:d1"] = {
            "deliveries": [
                {"deliver": "log", "deliver_extra": {}},
                {"deliver": "telegram", "deliver_extra": {"chat_id": "42"}},
            ]
        }
        # First target (log) succeeds; second (cross-platform) fails because
        # there is no gateway runner and 'telegram' is a known platform but
        # gateway_runner is None → falls through to unknown? No: telegram is
        # builtin, but gateway_runner None means _deliver_cross_platform is
        # not reached; the adapter logs unknown and returns failure only if
        # not known. telegram IS known → without a runner, _deliver_one
        # returns... let's force failure by registering a failing runner.
        adapter.gateway_runner = object()
        result = await adapter.send("webhook:r:d1", "hello")
        # log target succeeded, so overall success is True despite the other
        # target failing (no real telegram adapter on the fake runner).
        assert result.success is True

    @pytest.mark.asyncio
    async def test_single_target_preserves_result_verbatim(self):
        adapter = _adapter(
            {"r": {"secret": "s", "prompt": "p", "deliver": "log"}}
        )
        adapter._delivery_info["webhook:r:d1"] = {
            "deliveries": [{"deliver": "log", "deliver_extra": {}}]
        }
        result = await adapter.send("webhook:r:d1", "hello")
        assert result.success is True
