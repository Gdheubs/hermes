"""Callback SSRF-guard and envelope tests (Task 13, #4386/#73828)."""

from __future__ import annotations

import json

import pytest

from gateway.platforms import webhook_callbacks as wc


class TestValidateCallbackUrl:
    def test_public_https_ok(self):
        ok, _ = wc.validate_callback_url("https://example.com/done")
        assert ok is True

    def test_private_host_blocked(self):
        ok, reason = wc.validate_callback_url("http://10.0.0.5/cb")
        assert ok is False
        assert "SSRF" in reason

    def test_loopback_blocked(self):
        ok, reason = wc.validate_callback_url("http://127.0.0.1/cb")
        assert ok is False
        assert "SSRF" in reason

    def test_metadata_blocked(self):
        ok, _ = wc.validate_callback_url("http://169.254.169.254/latest/meta-data")
        assert ok is False

    def test_non_http_blocked(self):
        ok, reason = wc.validate_callback_url("file:///etc/passwd")
        assert ok is False
        assert "http(s)" in reason

    def test_missing_host_blocked(self):
        ok, _ = wc.validate_callback_url("https://")
        assert ok is False


class TestEnvelope:
    def test_envelope_shape(self):
        env = wc.build_callback_envelope(
            execution_id="e1", event_id="ev1", status="completed",
            output="done", error=None, attempt=1,
        )
        assert env["execution_id"] == "e1"
        assert env["status"] == "completed"
        assert env["attempt"] == 1
        assert env["error"] is None

    def test_envelope_serializes(self):
        env = wc.build_callback_envelope(
            execution_id="e1", event_id="ev1", status="failed",
            output=None, error="boom", attempt=2,
        )
        json.dumps(env)  # must not raise


class TestSigning:
    def test_signature_is_sha256(self):
        body = b'{"x":1}'
        sig = wc._sign(body, "secret")
        assert sig.startswith("sha256=")


class TestDeliverRefusesPrivate:
    def test_deliver_refuses_private(self):
        assert wc.deliver_callback("http://127.0.0.1:9/cb", None, {}) is False
