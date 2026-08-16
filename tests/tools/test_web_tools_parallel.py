"""Contract tests for Parallel.ai pool-backed key rotation.

Patches at the SDK boundary (fake ``parallel`` module exposing
``Parallel`` / ``AsyncParallel``) and the credential-pool boundary
(``agent.credential_pool.load_pool``) only — the provider's ``search()``,
the async ``extract()``, and the inline async rotation twin
(``_extract_with_key_rotation``) all run for real.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import plugins.web.parallel.provider as parallel_provider


# ─── Fake Parallel SDK ────────────────────────────────────────────────────────
# parallel-web is not installed in the test venv; register a minimal fake so
# the provider's lazy ``from parallel import Parallel`` resolves. Behaviors
# are keyed by API key so the rotation wrapper sees a real per-key client.


def _ok_search(*args, **kwargs):
    return SimpleNamespace(
        results=[
            SimpleNamespace(
                url="https://example.com", title="T", excerpts=["hello"]
            )
        ]
    )


def _ok_extract(*args, **kwargs):
    return SimpleNamespace(
        results=[
            SimpleNamespace(
                url="https://example.com",
                title="T",
                excerpts=["hello"],
                full_content="full",
            )
        ],
        errors=[],
    )


class FakeParallel:
    instances = []
    behaviors = {}

    def __init__(self, api_key):
        self.api_key = api_key
        self.beta = Mock()
        self.beta.search.side_effect = FakeParallel.behaviors.get(api_key, _ok_search)
        FakeParallel.instances.append(self)


class FakeAsyncParallel:
    instances = []
    behaviors = {}

    def __init__(self, api_key):
        self.api_key = api_key
        self.beta = Mock()
        self.beta.extract = AsyncMock(
            side_effect=FakeAsyncParallel.behaviors.get(api_key, _ok_extract)
        )
        FakeAsyncParallel.instances.append(self)


@pytest.fixture(autouse=True)
def _fake_parallel_sdk(monkeypatch):
    module = types.ModuleType("parallel")
    module.Parallel = FakeParallel
    module.AsyncParallel = FakeAsyncParallel
    monkeypatch.setitem(sys.modules, "parallel", module)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **k: None)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    parallel_provider._reset_clients_for_tests()
    FakeParallel.instances = []
    FakeAsyncParallel.instances = []
    FakeParallel.behaviors = {}
    FakeAsyncParallel.behaviors = {}
    yield
    parallel_provider._reset_clients_for_tests()
    FakeParallel.instances = []
    FakeAsyncParallel.instances = []


def _make_pool(entries):
    from agent.credential_pool import CredentialPool, PooledCredential

    creds = [
        PooledCredential(
            provider="parallel",
            id=f"id-{label}",
            label=label,
            auth_type="api_key",
            priority=priority,
            source="manual" if not label.startswith("env:") else label,
            access_token=key,
        )
        for priority, (label, key) in enumerate(entries)
    ]
    return CredentialPool("parallel", creds)


class TestParallelSearchRotation:
    def test_429_rotates_to_pool_key(self, monkeypatch):
        """Rate limit on key-a → sync search retried with pool key-b."""
        from agent.tool_credentials import ToolCredentialError
        from plugins.web.parallel.provider import ParallelWebSearchProvider

        pool = _make_pool([("env:PARALLEL_API_KEY", "key-a"), ("manual:2", "key-b")])
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda pid: pool)

        FakeParallel.behaviors = {
            "key-a": ToolCredentialError("Error code: 429 rate limit", status_code=429),
        }
        with patch.dict("os.environ", {"PARALLEL_API_KEY": "key-a"}):
            result = ParallelWebSearchProvider().search("query", limit=3)

        assert [c.api_key for c in FakeParallel.instances] == ["key-a", "key-b"]
        # First attempt failed, second succeeded with the pool key.
        assert result["success"] is True
        assert len(result["data"]["web"]) == 1
        assert result["data"]["web"][0]["title"] == "T"
        assert FakeParallel.instances[0].beta.search.call_count == 1
        assert FakeParallel.instances[1].beta.search.call_count == 1
        # Marking swaps in a fresh PooledCredential — re-read from the pool.
        entry_a = pool.entries()[0]
        assert entry_a.last_status == "exhausted"
        assert entry_a.last_error_code == 429
        assert entry_a.extra.get("failure_reason") == "rate_limit"


class TestParallelExtractRotation:
    def test_async_extract_429_rotates_via_async_twin(self, monkeypatch):
        """Async extract mirrors rotation: 429 on key-a → key-b succeeds."""
        from agent.tool_credentials import ToolCredentialError
        from plugins.web.parallel.provider import ParallelWebSearchProvider

        pool = _make_pool([("env:PARALLEL_API_KEY", "key-a"), ("manual:2", "key-b")])
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda pid: pool)

        FakeAsyncParallel.behaviors = {
            "key-a": ToolCredentialError("Error code: 429 rate limit", status_code=429),
        }
        with patch.dict("os.environ", {"PARALLEL_API_KEY": "key-a"}):
            result = asyncio.run(
                ParallelWebSearchProvider().extract(["https://example.com"])
            )

        assert [c.api_key for c in FakeAsyncParallel.instances] == ["key-a", "key-b"]
        assert len(result) == 1
        assert result[0]["content"] == "full"
        assert result[0]["url"] == "https://example.com"
        FakeAsyncParallel.instances[0].beta.extract.assert_awaited_once()
        FakeAsyncParallel.instances[1].beta.extract.assert_awaited_once()
        entry_a = pool.entries()[0]
        assert entry_a.last_status == "exhausted"
        assert entry_a.last_error_code == 429
        assert entry_a.extra.get("failure_reason") == "rate_limit"

    def test_async_extract_single_shot_without_pool(self, monkeypatch):
        """No pool credentials → passthrough: one attempt with the current key."""
        from plugins.web.parallel.provider import ParallelWebSearchProvider

        pool = _make_pool([])
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda pid: pool)

        with patch.dict("os.environ", {"PARALLEL_API_KEY": "key-a"}):
            result = asyncio.run(
                ParallelWebSearchProvider().extract(["https://example.com"])
            )

        assert [c.api_key for c in FakeAsyncParallel.instances] == ["key-a"]
        assert len(result) == 1
        assert result[0]["content"] == "full"
