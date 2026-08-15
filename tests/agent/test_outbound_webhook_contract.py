"""Strict Task 14 outbound configuration and signature contract."""

import hashlib
import hmac
import json

from agent import outbound_webhooks as ow


def _cfg(entry):
    return {"hooks": {"outbound": [entry]}}


def test_unknown_field_rejects_entire_target():
    assert ow.iter_configured_targets(
        _cfg({
            "url": "https://example.com/hook",
            "events": ["on_session_end"],
            "secrete": "typo",
        })
    ) == []


def test_inline_plaintext_secret_is_not_a_supported_field():
    assert ow.iter_configured_targets(
        _cfg({
            "url": "https://example.com/hook",
            "events": ["on_session_end"],
            "secret": "plaintext",
        })
    ) == []


def test_missing_explicit_secret_reference_fails_closed(monkeypatch):
    monkeypatch.delenv("MISSING_WR_SECRET", raising=False)
    assert ow.iter_configured_targets(
        _cfg({
            "url": "https://example.com/hook",
            "events": ["on_session_end"],
            "secret_ref": "MISSING_WR_SECRET",
        })
    ) == []


def test_v2_signature_binds_timestamp_and_body(monkeypatch):
    monkeypatch.setenv("WR_SECRET", "s3cret")
    target = ow.iter_configured_targets(
        _cfg({
            "url": "https://example.com/hook",
            "events": ["on_session_end"],
            "secret_ref": "WR_SECRET",
        })
    )[0]
    body = json.dumps({"schema_version": 1, "x": 1}).encode()
    delivery = ow._build_delivery("on_session_end", target, body, "d1")
    headers = delivery["headers"]
    timestamp = headers["X-Hermes-Timestamp"]
    expected = hmac.new(
        b"s3cret", timestamp.encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    assert headers["X-Hermes-Signature-V2"] == f"sha256={expected}"
    assert headers["X-Hermes-Schema-Version"] == "1"
