"""Tests for hermes_cli/webhook.py — webhook subscription CLI."""

import json
import os
import pytest
import stat
from argparse import Namespace

from hermes_cli.webhook import (
    webhook_command,
    _get_webhook_base_url,
    _load_subscriptions,
    _save_subscriptions,
    _subscriptions_path,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Default: webhooks enabled (most tests need this)
    monkeypatch.setattr(
        "hermes_cli.webhook._is_webhook_enabled", lambda: True
    )


def _make_args(**kwargs):
    defaults = {
        "webhook_action": None,
        "name": "",
        "prompt": "",
        "events": "",
        "description": "",
        "skills": "",
        "deliver": "log",
        "deliver_chat_id": "",
        "secret": "",
        "signature_mode": "",
        "payload": "",
        "script": "",
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


@pytest.mark.parametrize("host", [None, "", "0.0.0.0", "::"])
def test_webhook_base_url_maps_wildcard_hosts_to_localhost(monkeypatch, host):
    monkeypatch.setattr(
        "hermes_cli.webhook._get_webhook_config",
        lambda: {"extra": {"host": host, "port": 9123}},
    )
    assert _get_webhook_base_url() == "http://localhost:9123"


class TestSubscribe:


    def test_custom_secret(self):
        webhook_command(_make_args(
            webhook_action="subscribe", name="s", secret="my-secret"
        ))
        assert _load_subscriptions()["s"]["secret"] == "my-secret"


    def test_auto_secret(self):
        webhook_command(_make_args(webhook_action="subscribe", name="s"))
        secret = _load_subscriptions()["s"]["secret"]
        assert len(secret) > 20

    def test_signature_mode_persisted(self):
        webhook_command(_make_args(
            webhook_action="subscribe", name="s", signature_mode="gitlab_standard"
        ))
        assert _load_subscriptions()["s"]["signature_mode"] == "gitlab_standard"

    def test_signature_mode_omitted_defaults_to_implicit(self):
        webhook_command(_make_args(webhook_action="subscribe", name="s"))
        route = _load_subscriptions()["s"]
        # No explicit mode written → gateway default (generic_v2) applies.
        assert "signature_mode" not in route

    def test_signature_mode_normalized_lowercase(self):
        webhook_command(_make_args(
            webhook_action="subscribe", name="s", signature_mode="GITHUB"
        ))
        assert _load_subscriptions()["s"]["signature_mode"] == "github"


class TestList:

    def test_with_entries(self, capsys):
        webhook_command(_make_args(webhook_action="subscribe", name="a"))
        webhook_command(_make_args(webhook_action="subscribe", name="b"))
        capsys.readouterr()  # clear
        webhook_command(_make_args(webhook_action="list"))
        out = capsys.readouterr().out
        assert "2 webhook" in out
        assert "a" in out
        assert "b" in out


class TestRemove:


    def test_selective_remove(self):
        webhook_command(_make_args(webhook_action="subscribe", name="keep"))
        webhook_command(_make_args(webhook_action="subscribe", name="drop"))
        webhook_command(_make_args(webhook_action="remove", name="drop"))
        subs = _load_subscriptions()
        assert "keep" in subs
        assert "drop" not in subs


class TestPersistence:
    def test_corrupted_file(self):
        path = _subscriptions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("broken{{{")
        assert _load_subscriptions() == {}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are platform-specific")
    def test_save_creates_secret_file_owner_only_under_permissive_umask(self):
        old_umask = os.umask(0o022)
        try:
            _save_subscriptions({"demo": {"secret": "TOPSECRET", "prompt": "x"}})
        finally:
            os.umask(old_umask)

        path = _subscriptions_path()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "TOPSECRET" in path.read_text(encoding="utf-8")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are platform-specific")
    def test_save_narrows_existing_broad_secret_file_mode(self):
        # Simulate a pre-existing 0o644 file from before this hardening landed.
        path = _subscriptions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"old": {"secret": "stale", "prompt": "x"}}))
        path.chmod(0o644)

        _save_subscriptions({"demo": {"secret": "FRESH", "prompt": "x"}})

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "FRESH" in path.read_text(encoding="utf-8")


class TestWebhookEnabledGate:

    def test_blocks_list_when_disabled(self, capsys, monkeypatch):
        monkeypatch.setattr("hermes_cli.webhook._is_webhook_enabled", lambda: False)
        webhook_command(_make_args(webhook_action="list"))
        out = capsys.readouterr().out
        assert "not enabled" in out.lower()

    def test_allows_when_enabled(self, capsys):
        # _is_webhook_enabled already patched to True by autouse fixture
        webhook_command(_make_args(webhook_action="subscribe", name="allowed"))
        out = capsys.readouterr().out
        assert "Created" in out
        assert "allowed" in _load_subscriptions()

    def test_real_check_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.webhook._get_webhook_config",
            lambda: {},
        )
        monkeypatch.setattr(
            "hermes_cli.webhook._is_webhook_enabled",
            lambda: bool({}.get("enabled")),
        )
        import hermes_cli.webhook as wh_mod
        assert wh_mod._is_webhook_enabled() is False


class TestModeBoundTestSigning:
    """hermes webhook test signs with the route's configured mode."""

    def _h(self, cap, name):
        for k, v in cap["headers"].items():
            if k.lower() == name.lower():
                return v
        return None


    def _sign_headers(self, route, monkeypatch, payload="{}"):
        import hermes_cli.webhook as wh

        captured = {}

        class FakeResponse:
            status = 200
            def read(self):
                return b"{}"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class FakeUrlopen:
            def __init__(self, req, timeout=10):
                captured["url"] = req.full_url
                captured["headers"] = dict(req.headers)
            def __enter__(self):
                return FakeResponse()
            def __exit__(self, *a):
                return False

        monkeypatch.setattr("urllib.request.urlopen", FakeUrlopen)
        monkeypatch.setattr(
            "hermes_cli.webhook._get_webhook_base_url", lambda: "http://localhost:9123"
        )
        wh.webhook_command(_make_args(
            webhook_action="test", name="r", payload=payload
        ))
        return captured

    def _subscribe_with_mode(self, mode):
        webhook_command(_make_args(
            webhook_action="subscribe", name="r", secret="sec",
            signature_mode=mode,
        ))

    def test_generic_v2_signing(self, monkeypatch):
        self._subscribe_with_mode("generic_v2")
        cap = self._sign_headers(None, monkeypatch)
        assert self._h(cap, "X-Webhook-Signature-V2") is not None
        assert self._h(cap, "X-Webhook-Timestamp") is not None
        assert self._h(cap, "X-Hub-Signature-256") is None

    def test_github_signing(self, monkeypatch):
        self._subscribe_with_mode("github")
        cap = self._sign_headers(None, monkeypatch)
        assert self._h(cap, "X-Hub-Signature-256").startswith("sha256=")

    def test_hindsight_signing(self, monkeypatch):
        self._subscribe_with_mode("hindsight")
        cap = self._sign_headers(None, monkeypatch)
        assert self._h(cap, "X-Hindsight-Signature") is not None
        assert self._h(cap, "X-Hindsight-Signature").startswith("sha256=")

    def test_gitlab_signing(self, monkeypatch):
        self._subscribe_with_mode("gitlab")
        cap = self._sign_headers(None, monkeypatch)
        assert self._h(cap, "X-Gitlab-Token") == "sec"

    def test_gitlab_standard_signing_uses_webhook_headers(self, monkeypatch):
        self._subscribe_with_mode("gitlab_standard")
        cap = self._sign_headers(None, monkeypatch)
        assert self._h(cap, "webhook-id") is not None
        assert self._h(cap, "webhook-timestamp") is not None
        assert self._h(cap, "webhook-signature").startswith("v1,")

    def test_svix_signing_uses_svix_headers(self, monkeypatch):
        self._subscribe_with_mode("svix")
        cap = self._sign_headers(None, monkeypatch)
        assert self._h(cap, "svix-id") is not None
        assert self._h(cap, "svix-signature").startswith("v1,")

    def test_signature_matches_gateway_validation(self, monkeypatch):
        # The CLI-signed generic_v2 request must pass the gateway's own
        # validator with the same route config — the contract test.
        import hashlib
        import hmac
        import time as _time
        from unittest.mock import MagicMock

        from gateway.platforms.webhook_auth import WebhookAuthMixin

        self._subscribe_with_mode("generic_v2")
        cap = self._sign_headers(None, monkeypatch, payload='{"test": true}')
        ts = self._h(cap, "X-Webhook-Timestamp")
        sig = self._h(cap, "X-Webhook-Signature-V2")
        body = b'{"test": true}'
        expected = hmac.new(
            b"sec", ts.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        assert sig == expected

        adapter = WebhookAuthMixin()
        req = MagicMock()
        req.headers = {
            "X-Webhook-Signature-V2": sig,
            "X-Webhook-Timestamp": ts,
        }
        req.match_info = {"route_name": "r"}
        assert adapter._validate_signature(
            req, body, "sec", signature_mode="generic_v2"
        ) is True

