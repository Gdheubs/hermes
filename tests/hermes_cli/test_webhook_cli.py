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


class TestManageCommands:
    """Task 15: show / update / enable / disable / rotate-secret / --replace."""

    def _mk(self, **kw):
        return _make_args(**kw)

    def test_subscribe_conflict_requires_replace(self, capsys):
        webhook_command(self._mk(webhook_action="subscribe", name="dup", secret="s1"))
        capsys.readouterr()
        webhook_command(self._mk(webhook_action="subscribe", name="dup", secret="s2"))
        out = capsys.readouterr().out
        assert "already exists" in out.lower()
        # Without --replace, the route is untouched.
        assert _load_subscriptions()["dup"]["secret"] == "s1"

    def test_subscribe_replace_overwrites(self, capsys):
        webhook_command(self._mk(webhook_action="subscribe", name="dup", secret="s1"))
        capsys.readouterr()
        webhook_command(self._mk(
            webhook_action="subscribe", name="dup", secret="s2", replace=True
        ))
        capsys.readouterr()
        assert _load_subscriptions()["dup"]["secret"] == "s2"

    def test_show_masks_secret(self, capsys):
        webhook_command(self._mk(
            webhook_action="subscribe", name="shown",
            secret="a-very-long-secret-value", description="desc",
        ))
        capsys.readouterr()
        webhook_command(self._mk(webhook_action="show", name="shown"))
        out = capsys.readouterr().out
        assert "a-very-long-secret-value" not in out
        assert "a-ve" in out  # masked head

    def test_show_json(self, capsys):
        webhook_command(self._mk(webhook_action="subscribe", name="js", secret="xyz123456"))
        capsys.readouterr()
        webhook_command(self._mk(webhook_action="show", name="js", json=True))
        out = capsys.readouterr().out
        import json as _json
        data = _json.loads(out)
        assert data["name"] == "js"
        assert "xyz123456" not in out

    def test_disable_and_enable(self, capsys):
        webhook_command(self._mk(webhook_action="subscribe", name="toggle"))
        capsys.readouterr()
        webhook_command(self._mk(webhook_action="disable", name="toggle"))
        capsys.readouterr()
        assert _load_subscriptions()["toggle"]["enabled"] is False
        webhook_command(self._mk(webhook_action="enable", name="toggle"))
        capsys.readouterr()
        assert _load_subscriptions()["toggle"]["enabled"] is True

    def test_update_patches_fields(self):
        webhook_command(self._mk(webhook_action="subscribe", name="upd", prompt="old"))
        webhook_command(self._mk(webhook_action="update", name="upd", prompt="new", events="a,b"))
        route = _load_subscriptions()["upd"]
        assert route["prompt"] == "new"
        assert route["events"] == ["a", "b"]

    def test_rotate_secret_shows_once(self, capsys):
        webhook_command(self._mk(webhook_action="subscribe", name="rot", secret="oldsecret"))
        capsys.readouterr()
        webhook_command(self._mk(webhook_action="rotate-secret", name="rot"))
        out = capsys.readouterr().out
        assert "New secret" in out
        new_secret = _load_subscriptions()["rot"]["secret"]
        assert new_secret != "oldsecret"
        assert len(new_secret) > 20

    def test_list_json_is_parseable_and_secret_safe(self, capsys):
        webhook_command(self._mk(webhook_action="subscribe", name="lj", secret="topsecret123"))
        capsys.readouterr()
        webhook_command(self._mk(webhook_action="list", json=True))
        out = capsys.readouterr().out
        import json as _json
        data = _json.loads(out)
        assert any(r["name"] == "lj" for r in data)
        assert "topsecret123" not in out

