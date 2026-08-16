"""Contract tests for Exa pool-backed key rotation.

Patches at the SDK boundary (``exa_py.Exa``) and the credential-pool
boundary (``agent.credential_pool.load_pool``) only — the provider's
``search()``/``extract()`` and the ``run_with_key_rotation`` wrapper run
for real.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import plugins.web.exa.provider as exa_provider


@pytest.fixture(autouse=True)
def _reset_exa_clients():
    exa_provider._reset_client_for_tests()
    yield
    exa_provider._reset_client_for_tests()


@pytest.fixture(autouse=True)
def _no_lazy_install(monkeypatch):
    """Never trigger a lazy pip install in tests (exa-py pin may differ)."""
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **k: None)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)


def _make_pool(entries):
    from agent.credential_pool import CredentialPool, PooledCredential

    creds = [
        PooledCredential(
            provider="exa",
            id=f"id-{label}",
            label=label,
            auth_type="api_key",
            priority=priority,
            source="manual" if not label.startswith("env:") else label,
            access_token=key,
        )
        for priority, (label, key) in enumerate(entries)
    ]
    return CredentialPool("exa", creds)


class TestExaSearchRotation:
    def test_402_rotates_to_pool_key(self, monkeypatch):
        """Billing failure on key-a → search retried with pool key-b."""
        from agent.tool_credentials import ToolCredentialError
        from plugins.web.exa.provider import ExaWebSearchProvider

        pool = _make_pool([("env:EXA_API_KEY", "key-a"), ("manual:2", "key-b")])
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda pid: pool)

        constructed = []
        # MagicMock: _get_exa_client sets client.headers["x-exa-integration"].
        client_a, client_b = MagicMock(), MagicMock()

        def _make_client(api_key):
            constructed.append(api_key)
            return client_a if len(constructed) == 1 else client_b

        client_a.search.side_effect = ToolCredentialError(
            "Error code: 402 credits exhausted", status_code=402
        )
        client_b.search.return_value = SimpleNamespace(
            results=[
                SimpleNamespace(
                    url="https://example.com", title="T", highlights=["a highlight"]
                )
            ]
        )

        with patch.dict("os.environ", {"EXA_API_KEY": "key-a"}), \
             patch("exa_py.Exa", Mock(side_effect=_make_client)):
            result = ExaWebSearchProvider().search("query", limit=3)

        assert result["success"] is True
        assert len(result["data"]["web"]) == 1
        assert result["data"]["web"][0]["title"] == "T"
        assert result["data"]["web"][0]["description"] == "a highlight"
        assert constructed == ["key-a", "key-b"]
        client_a.search.assert_called_once()
        client_b.search.assert_called_once()
        # Marking swaps in a fresh PooledCredential — re-read from the pool.
        entry_a = pool.entries()[0]
        assert entry_a.last_status == "exhausted"
        assert entry_a.last_error_code == 402
        assert entry_a.extra.get("failure_reason") == "billing"

    def test_500_does_not_rotate(self, monkeypatch):
        """5xx is not a per-key problem — single attempt, no pool marking."""
        from agent.tool_credentials import ToolCredentialError
        from plugins.web.exa.provider import ExaWebSearchProvider

        pool = _make_pool([("env:EXA_API_KEY", "key-a"), ("manual:2", "key-b")])
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda pid: pool)

        client_a = MagicMock()
        client_a.search.side_effect = ToolCredentialError(
            "Error code: 500 internal", status_code=500
        )
        with patch.dict("os.environ", {"EXA_API_KEY": "key-a"}), \
             patch("exa_py.Exa", Mock(return_value=client_a)):
            result = ExaWebSearchProvider().search("query", limit=3)

        assert result["success"] is False
        assert "500" in result["error"]
        client_a.search.assert_called_once()
        entry_a = pool.entries()[0]
        assert entry_a.last_status is None
        assert entry_a.last_error_code is None
