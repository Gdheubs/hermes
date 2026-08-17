"""#88463 — the named-custom model flow must probe with the entry's extra_headers.

Gateways fronting named custom providers (Bifrost, LiteLLM, …) often REQUIRE
their auth/routing headers on the /models probe itself; without them the
picker probe 401s and collapses to manual entry while chat works fine.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text("model:\n  default: m\n")


def _provider_info(**extra):
    info = {
        "name": "bifrost",
        "base_url": "http://bifrost.local/v1",
        "api_key": "k",
        "model": "",
    }
    info.update(extra)
    return info


def test_named_custom_probe_carries_extra_headers():
    captured = {}

    def _fake_fetch(api_key, base_url, **kwargs):
        captured.update(kwargs)
        return ["m1", "m2"]

    with (
        patch("hermes_cli.models.fetch_api_models", _fake_fetch),
        patch("hermes_cli.curses_ui.curses_radiolist", return_value=-1),
    ):
        from hermes_cli.model_setup_flows import _model_flow_named_custom

        _model_flow_named_custom(
            None, _provider_info(extra_headers={"x-bf-vk": "vk-1", "x-tenant": "acme"})
        )

    assert captured.get("headers") == {"x-bf-vk": "vk-1", "x-tenant": "acme"}


def test_named_custom_probe_without_extra_headers_sends_none():
    captured = {}

    def _fake_fetch(api_key, base_url, **kwargs):
        captured.update(kwargs)
        return ["m1"]

    with (
        patch("hermes_cli.models.fetch_api_models", _fake_fetch),
        patch("hermes_cli.curses_ui.curses_radiolist", return_value=-1),
    ):
        from hermes_cli.model_setup_flows import _model_flow_named_custom

        _model_flow_named_custom(None, _provider_info())

    assert "headers" not in captured
