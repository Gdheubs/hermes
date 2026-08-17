"""#88617 — the ws keepalive must be escapable on 0.0.0.0 binds.

The 20/20 uvicorn ws ping detects half-open tunnels on public binds, but a
deployment that must bind 0.0.0.0 (docker-bridge reverse proxy) while
serving only local clients gets the same hostile keepalive: a client event
loop stalled >20s during heavy streaming is declared dead and dropped
(1006). HERMES_DASHBOARD_WS_PING_OFF=1 disables the ping for that case;
loopback keeps its unconditional ping-off; public-tunnel defaults keep 20/20.
"""

import sys
from pathlib import Path

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

import pytest

from hermes_cli.web_server import _ws_keepalive_policy


class TestWsKeepalivePolicy:
    def test_loopback_never_pings(self, monkeypatch):
        monkeypatch.delenv("HERMES_DASHBOARD_WS_PING_OFF", raising=False)
        for host in ("127.0.0.1", "localhost", "::1"):
            assert _ws_keepalive_policy(host) == (None, None)

    def test_public_bind_defaults_to_2020(self, monkeypatch):
        monkeypatch.delenv("HERMES_DASHBOARD_WS_PING_OFF", raising=False)
        assert _ws_keepalive_policy("0.0.0.0") == (20.0, 20.0)
        assert _ws_keepalive_policy("example.com") == (20.0, 20.0)

    def test_escape_hatch_disables_ping_on_non_loopback(self, monkeypatch):
        # The reporter's deployment: bind 0.0.0.0 for the docker bridge,
        # serve only local clients.
        monkeypatch.setenv("HERMES_DASHBOARD_WS_PING_OFF", "1")
        assert _ws_keepalive_policy("0.0.0.0") == (None, None)

    def test_escape_hatch_truthy_set_matches_repo_convention(self, monkeypatch):
        for value in ("1", "true", "yes", "on"):
            monkeypatch.setenv("HERMES_DASHBOARD_WS_PING_OFF", value)
            assert _ws_keepalive_policy("0.0.0.0") == (None, None), value

    def test_escape_hatch_false_like_values_keep_the_ping(self, monkeypatch):
        for value in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("HERMES_DASHBOARD_WS_PING_OFF", value)
            assert _ws_keepalive_policy("0.0.0.0") == (20.0, 20.0), value
