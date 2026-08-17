"""
Seam tests for the api_server idempotency R1 extraction.

The idempotency cluster (``_IdempotencyCache``, ``_idem_cache``,
``_make_request_fingerprint``) was extracted byte-verbatim from
``gateway/platforms/api_server.py`` lines 1210-1261 (pin
ee4bb75b532e932a1055d9a710802a7435163b6a) into
``gateway/platforms/api_server_idempotency.py`` and is re-exported through
``gateway.platforms.api_server`` so the four internal call sites (chat
completions and responses handlers) keep resolving the same objects.

These tests pin:
- object identity across the seam (the re-export is not a copy),
- the behavioral contract of the cache (caching, inflight dedup, TTL, LRU),
- the fingerprint contract (deterministic, subset-sensitive, order-independent),
- the module-level singleton used by the production handlers.
"""

import asyncio
import hashlib
import time

import pytest

import gateway.platforms.api_server as api_server
import gateway.platforms.api_server_idempotency as api_server_idempotency
from gateway.platforms.api_server import (
    _IdempotencyCache,
    _idem_cache,
    _make_request_fingerprint,
)

_REE_EXPORTED_NAMES = ("_IdempotencyCache", "_idem_cache", "_make_request_fingerprint")


# ---------------------------------------------------------------------------
# Seam: object identity across the re-export
# ---------------------------------------------------------------------------


class TestSeamObjectIdentity:

    def test_all_reexported_names_are_the_same_objects(self):
        for name in _REE_EXPORTED_NAMES:
            assert getattr(api_server, name) is getattr(api_server_idempotency, name), name

    def test_module_globals_bind_to_the_new_modules_objects(self):
        for name in _REE_EXPORTED_NAMES:
            assert api_server.__dict__[name] is api_server_idempotency.__dict__[name], name

    def test_from_import_via_api_server_is_identical(self):
        from gateway.platforms.api_server import (  # noqa: PLC0415
            _IdempotencyCache as A,
            _idem_cache as B,
            _make_request_fingerprint as C,
        )

        assert A is api_server_idempotency._IdempotencyCache
        assert B is api_server_idempotency._idem_cache
        assert C is api_server_idempotency._make_request_fingerprint

    def test_reexported_objects_are_defined_in_the_new_module(self):
        assert api_server._make_request_fingerprint.__module__ == "gateway.platforms.api_server_idempotency"
        assert api_server._IdempotencyCache.__module__ == "gateway.platforms.api_server_idempotency"
        assert type(api_server._idem_cache).__module__ == "gateway.platforms.api_server_idempotency"


# ---------------------------------------------------------------------------
# _make_request_fingerprint behavioral contract
# ---------------------------------------------------------------------------


class TestMakeRequestFingerprint:

    KEYS = ["model", "messages", "tools"]

    def test_deterministic_and_matches_direct_sha256_of_repr_subset(self):
        body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "tools": []}
        f1 = _make_request_fingerprint(body, self.KEYS)
        f2 = _make_request_fingerprint(dict(body), self.KEYS)
        assert f1 == f2
        assert len(f1) == 64
        expected = hashlib.sha256(
            repr({"model": "gpt-4o", "messages": body["messages"], "tools": []}).encode("utf-8")
        ).hexdigest()
        assert f1 == expected

    def test_subset_sensitive_to_fingerprinted_keys(self):
        body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "tools": []}
        f1 = _make_request_fingerprint(body, self.KEYS)
        assert _make_request_fingerprint({**body, "model": "gpt-4o-mini"}, self.KEYS) != f1

    def test_non_fingerprinted_keys_are_ignored(self):
        body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "tools": []}
        f1 = _make_request_fingerprint(body, self.KEYS)
        assert _make_request_fingerprint({**body, "extra": "changed"}, self.KEYS) == f1

    def test_dict_order_independent(self):
        f1 = _make_request_fingerprint({"model": "m", "messages": "a", "tools": "t"}, self.KEYS)
        f2 = _make_request_fingerprint({"tools": "t", "model": "m", "messages": "a"}, self.KEYS)
        assert f1 == f2

    def test_missing_keys_become_none(self):
        f = _make_request_fingerprint({"model": "m"}, ["model", "tools"])
        expected = hashlib.sha256(repr({"model": "m", "tools": None}).encode("utf-8")).hexdigest()
        assert f == expected


# ---------------------------------------------------------------------------
# _IdempotencyCache behavioral contract
# ---------------------------------------------------------------------------


class TestIdempotencyCacheBehavior:

    @pytest.mark.asyncio
    async def test_get_or_set_serves_cached_result_without_recompute(self):
        cache = _IdempotencyCache()
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            return ("response", {"total_tokens": 1})

        first = await cache.get_or_set("key", "fp", compute)
        second = await cache.get_or_set("key", "fp", compute)
        assert first == second == ("response", {"total_tokens": 1})
        assert calls == 1

    @pytest.mark.asyncio
    async def test_concurrent_same_key_and_fingerprint_runs_once(self):
        # Mirror of test_api_server.py::TestIdempotencyCache — pins the
        # inflight-dedup path exercised by the chat-completions handler.
        cache = _IdempotencyCache()
        gate = asyncio.Event()
        started = asyncio.Event()
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            started.set()
            await gate.wait()
            return ("response", {"total_tokens": 1})

        first = asyncio.create_task(cache.get_or_set("idem-key", "fp-1", compute))
        second = asyncio.create_task(cache.get_or_set("idem-key", "fp-1", compute))

        await started.wait()
        assert calls == 1

        gate.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result == second_result == ("response", {"total_tokens": 1})

    @pytest.mark.asyncio
    async def test_fingerprint_change_recomputes(self):
        cache = _IdempotencyCache()
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            return ("response", {})

        await cache.get_or_set("key", "fp-1", compute)
        await cache.get_or_set("key", "fp-2", compute)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_ttl_expiry_recomputes(self):
        cache = _IdempotencyCache(ttl_seconds=1)
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            return ("response", {})

        await cache.get_or_set("key", "fp", compute)
        assert calls == 1
        time.sleep(1.2)
        await cache.get_or_set("key", "fp", compute)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_max_items_evicts_oldest_entry(self):
        cache = _IdempotencyCache(max_items=2)

        async def compute(v):
            return (v, {})

        for i in range(3):
            await cache.get_or_set(f"key-{i}", f"fp-{i}", lambda v=i: compute(v))

        assert len(cache._store) == 2
        assert "key-0" not in cache._store
        assert "key-1" in cache._store
        assert "key-2" in cache._store


# ---------------------------------------------------------------------------
# Module-level singleton used by the production handlers
# ---------------------------------------------------------------------------


class TestGlobalSingleton:

    def test_singleton_is_an_instance_of_the_reexported_class(self):
        assert isinstance(_idem_cache, _IdempotencyCache)
        assert isinstance(api_server._idem_cache, api_server._IdempotencyCache)

    @pytest.mark.asyncio
    async def test_singleton_serves_cache_like_the_production_path(self):
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            return ("result", {"usage": 1})

        key = "seam-global-key"
        try:
            first = await _idem_cache.get_or_set(key, "fp", compute)
            second = await _idem_cache.get_or_set(key, "fp", compute)
            assert first == second == ("result", {"usage": 1})
            assert calls == 1
        finally:
            _idem_cache._store.pop(key, None)
