"""Contract tests for agent.tool_credentials API-key rotation.

Covers:
  ToolCredentialError + tool_error_from_exception — status extraction
    (getattr chains, regex fallback) and body/provider_id passthrough.
  run_with_key_rotation — single-shot passthrough conditions, candidate
    ordering/dedup, rotate-worthy vs non-rotate classification, and
    best-effort pool marking (status/reason persisted on the entries).

The pool is a REAL CredentialPool built in-test so the marking contract
(last_status / last_error_code / extra["failure_reason"]) is exercised
against actual pool bookkeeping; only load_pool() and
is_multiplex_active() are patched at their module boundaries.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from agent.tool_credentials import (
    ToolCredentialError,
    run_with_key_rotation,
    tool_error_from_exception,
)


def _entry(provider: str, label: str, key: str, *, priority: int = 0):
    """Build a real PooledCredential for the given provider/label/key."""
    from agent.credential_pool import PooledCredential

    return PooledCredential(
        provider=provider,
        id=f"id-{label}",
        label=label,
        auth_type="api_key",
        priority=priority,
        source="manual" if not label.startswith("env:") else label,
        access_token=key,
    )


def _make_pool(provider: str, entries):
    from agent.credential_pool import CredentialPool

    return CredentialPool(provider, entries)


def _install_pool(monkeypatch, pool):
    """Patch agent.credential_pool.load_pool to return ``pool``; return the Mock."""
    load_pool = Mock(return_value=pool)
    monkeypatch.setattr("agent.credential_pool.load_pool", load_pool)
    return load_pool


@pytest.fixture(autouse=True)
def _multiplex_off(monkeypatch):
    """Rotation tests assume the process is not a profile multiplexer."""
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: False)


def _billing_402(message: str) -> ToolCredentialError:
    return ToolCredentialError(message, status_code=402)


# ─── Passthrough (single-shot, no pool access) ───────────────────────────────


class TestPassthrough:
    def test_no_provider_id_passthrough(self, monkeypatch):
        """provider_id='' → fn(current_key) once, pool never touched."""
        load_pool = Mock()
        monkeypatch.setattr("agent.credential_pool.load_pool", load_pool)
        fn = Mock(return_value="ok")

        result = run_with_key_rotation("", fn, current_key="k")

        assert result == "ok"
        fn.assert_called_once_with("k")
        load_pool.assert_not_called()

    def test_multiplex_active_passthrough_never_touches_pool(self, monkeypatch):
        """Multiplex active → per-profile scope is authoritative; no pool read."""
        monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: True)
        load_pool = Mock()
        monkeypatch.setattr("agent.credential_pool.load_pool", load_pool)
        fn = Mock(return_value="ok")

        result = run_with_key_rotation("firecrawl", fn, current_key="k")

        assert result == "ok"
        fn.assert_called_once_with("k")
        load_pool.assert_not_called()

    def test_empty_pool_passthrough(self, monkeypatch):
        """Pool exists but holds no credentials → plain single call."""
        pool = _make_pool("firecrawl", [])
        load_pool = _install_pool(monkeypatch, pool)
        fn = Mock(return_value="ok")

        result = run_with_key_rotation("firecrawl", fn, current_key="k")

        assert result == "ok"
        fn.assert_called_once_with("k")
        load_pool.assert_called_once_with("firecrawl")

    def test_none_pool_passthrough(self, monkeypatch):
        """load_pool returns None → plain single call."""
        load_pool = _install_pool(monkeypatch, None)
        fn = Mock(return_value="ok")

        result = run_with_key_rotation("firecrawl", fn, current_key="k")

        assert result == "ok"
        fn.assert_called_once_with("k")
        load_pool.assert_called_once_with("firecrawl")


# ─── Rotation semantics ──────────────────────────────────────────────────────


class TestRotation:
    def test_402_on_current_key_rotates_and_marks_pool_entry(self, monkeypatch):
        """Billing failure on the current key → next pool key, entry marked."""
        pool = _make_pool(
            "firecrawl",
            [
                _entry("firecrawl", "env:FIRECRAWL_API_KEY", "key-a", priority=0),
                _entry("firecrawl", "manual:2", "key-b", priority=1),
            ],
        )
        load_pool = _install_pool(monkeypatch, pool)

        def fn(api_key: str):
            if api_key == "key-a":
                raise _billing_402("credits exhausted")
            return f"result-{api_key}"

        result = run_with_key_rotation("firecrawl", fn, current_key="key-a")

        assert result == "result-key-b"
        # Failed key exhausted with status + classifier verdict persisted;
        # the healthy fallback is untouched. (Marking swaps in a fresh
        # PooledCredential via dataclasses.replace — re-read from the pool.)
        entry_a, entry_b = pool.entries()
        assert entry_a.last_status == "exhausted"
        assert entry_a.last_error_code == 402
        assert entry_a.extra.get("failure_reason") == "billing"
        assert entry_b.last_status is None
        assert entry_b.last_error_code is None
        # load_pool is consulted exactly once per invocation.
        load_pool.assert_called_once_with("firecrawl")

    def test_400_reraises_immediately_without_marking(self, monkeypatch):
        """400 is a request problem, not a key problem — no rotation, no marking."""
        pool = _make_pool(
            "firecrawl",
            [
                _entry("firecrawl", "env:FIRECRAWL_API_KEY", "key-a", priority=0),
                _entry("firecrawl", "manual:2", "key-b", priority=1),
            ],
        )
        entry_a, _entry_b = pool.entries()
        _install_pool(monkeypatch, pool)
        fn = Mock(side_effect=ToolCredentialError("bad request", status_code=400))

        with pytest.raises(ToolCredentialError) as exc_info:
            run_with_key_rotation("firecrawl", fn, current_key="key-a")

        assert exc_info.value.status_code == 400
        fn.assert_called_once_with("key-a")
        assert entry_a.last_status is None
        assert entry_a.last_error_code is None

    def test_all_candidates_fail_reraises_last_and_never_marks_last_key(self, monkeypatch):
        """Every key fails 402 → last exception surfaces; all keys EXCEPT the
        final one are marked. The last-failed key has no untried alternative,
        so marking it would only cool it down and make the tool vanish for
        the TTL without enabling any rotation."""
        pool = _make_pool(
            "firecrawl",
            [
                _entry("firecrawl", "env:FIRECRAWL_API_KEY", "key-a", priority=0),
                _entry("firecrawl", "manual:2", "key-b", priority=1),
            ],
        )
        _install_pool(monkeypatch, pool)

        def fn(api_key: str):
            raise _billing_402(f"fail {api_key}")

        with pytest.raises(ToolCredentialError) as exc_info:
            run_with_key_rotation("firecrawl", fn, current_key="key-a")

        assert exc_info.value.status_code == 402
        assert "key-b" in str(exc_info.value)
        entry_a, entry_b = pool.entries()
        # key-a had an untried alternative (key-b) → marked exhausted.
        assert entry_a.last_status == "exhausted"
        assert entry_a.last_error_code == 402
        # key-b had none → NOT marked; it stays available for the next call.
        assert entry_b.last_status != "exhausted"

    def test_single_key_pool_429_surfaces_error_without_marking(self, monkeypatch):
        """Advisory regression guard: a user's ONLY firecrawl key (added via
        `hermes auth add firecrawl`, not in env) hits one transient 429.
        The error must surface, but the entry must NOT be marked exhausted —
        otherwise resolve_provider_secret() finds no key and the whole
        web toolset disappears for the cooldown TTL."""
        pool = _make_pool(
            "firecrawl",
            [_entry("firecrawl", "manual:1", "only-key", priority=0)],
        )
        _install_pool(monkeypatch, pool)
        fn = Mock(
            side_effect=lambda api_key: (_ for _ in ()).throw(
                ToolCredentialError(f"rate limited {api_key}", status_code=429)
            )
        )

        with pytest.raises(ToolCredentialError) as exc_info:
            run_with_key_rotation("firecrawl", fn, current_key="only-key")

        assert exc_info.value.status_code == 429
        # Exactly one attempt — no phantom retries.
        assert [call.args[0] for call in fn.call_args_list] == ["only-key"]
        # The lone key is still active: the tool stays available.
        (entry,) = pool.entries()
        assert entry.last_status != "exhausted"
        assert pool.peek() is not None

    def test_single_pool_entry_without_current_key_also_unmarked(self, monkeypatch):
        """current_key='' + one pool entry failing 429 → same guard: the
        lone entry is never cooled down by its own failure."""
        pool = _make_pool(
            "firecrawl",
            [_entry("firecrawl", "manual:1", "only-key", priority=0)],
        )
        _install_pool(monkeypatch, pool)
        fn = Mock(
            side_effect=lambda api_key: (_ for _ in ()).throw(
                ToolCredentialError(f"rate limited {api_key}", status_code=429)
            )
        )

        with pytest.raises(ToolCredentialError):
            run_with_key_rotation("firecrawl", fn, current_key="")

        assert [call.args[0] for call in fn.call_args_list] == ["only-key"]
        (entry,) = pool.entries()
        assert entry.last_status != "exhausted"

    def test_duplicate_key_values_never_called_twice_with_same_key(self, monkeypatch):
        """Pool entries sharing one key value → that key tried exactly once."""
        pool = _make_pool(
            "firecrawl",
            [
                _entry("firecrawl", "env:FIRECRAWL_API_KEY", "key-a", priority=0),
                _entry("firecrawl", "manual:1", "key-d", priority=1),
                _entry("firecrawl", "manual:2", "key-d", priority=2),
            ],
        )
        _install_pool(monkeypatch, pool)
        fn = Mock(side_effect=lambda api_key: (_ for _ in ()).throw(_billing_402(f"fail {api_key}")))

        with pytest.raises(ToolCredentialError):
            run_with_key_rotation("firecrawl", fn, current_key="key-a")

        # key-a once, then key-d once — never a second call with key-d.
        assert [call.args[0] for call in fn.call_args_list] == ["key-a", "key-d"]
        # key-a had an untried alternative → marked. key-d was the last
        # candidate → never marked (guard against cooling down a lone key).
        entry_a = pool.entries()[0]
        entry_d1, entry_d2 = pool.entries()[1:]
        assert entry_a.last_status == "exhausted"
        assert entry_d1.last_status != "exhausted"
        assert entry_d2.last_status != "exhausted"

    def test_pool_key_used_when_no_current_key(self, monkeypatch):
        """current_key='' → starts from the pool's first available entry."""
        pool = _make_pool(
            "firecrawl",
            [
                _entry("firecrawl", "manual:1", "key-a", priority=0),
                _entry("firecrawl", "manual:2", "key-b", priority=1),
            ],
        )
        _install_pool(monkeypatch, pool)
        fn = Mock(side_effect=lambda api_key: (_ for _ in ()).throw(_billing_402(f"fail {api_key}")))

        with pytest.raises(ToolCredentialError) as exc_info:
            run_with_key_rotation("firecrawl", fn, current_key="")

        assert "key-b" in str(exc_info.value)
        assert [call.args[0] for call in fn.call_args_list] == ["key-a", "key-b"]


# ─── tool_error_from_exception ───────────────────────────────────────────────


class TestToolErrorFromException:
    def test_status_code_attribute(self):
        err = tool_error_from_exception(_billing_402("billing"))
        assert err.status_code == 402

    def test_status_attribute(self):
        exc = Exception("boom")
        exc.status = 403
        assert tool_error_from_exception(exc).status_code == 403

    def test_response_status_code_attribute(self):
        exc = Exception("boom")
        exc.response = SimpleNamespace(status_code=429)
        assert tool_error_from_exception(exc).status_code == 429

    def test_response_status_attribute(self):
        exc = Exception("boom")
        exc.response = SimpleNamespace(status=503)
        assert tool_error_from_exception(exc).status_code == 503

    def test_string_status_code_coerced_to_int(self):
        exc = Exception("boom")
        exc.status_code = "418"
        assert tool_error_from_exception(exc).status_code == 418

    def test_regex_fallback_with_colon(self):
        exc = Exception("Error code: 402 Payment Required")
        assert tool_error_from_exception(exc).status_code == 402

    def test_regex_fallback_without_colon(self):
        exc = Exception("error code 429 rate limit")
        assert tool_error_from_exception(exc).status_code == 429

    def test_regex_fallback_when_attribute_unparseable(self):
        exc = Exception("Error code: 500 internal")
        exc.status_code = "not-a-number"
        assert tool_error_from_exception(exc).status_code == 500

    def test_no_status_anywhere(self):
        assert tool_error_from_exception(Exception("plain failure")).status_code is None

    def test_carries_body_and_provider_id(self):
        exc = Exception("boom")
        exc.body = {"detail": "nope"}
        err = tool_error_from_exception(exc, "firecrawl")
        assert err.body == {"detail": "nope"}
        assert err.provider_id == "firecrawl"
        assert str(err) == "boom"
