"""Callable-credential contract for the model-catalog probe helpers.

``resolve_runtime_provider()`` returns a **callable** token provider — not a
string — for the ``key_cmd`` and Entra ID auth paths, because gateways that
issue short-lived bearers (Databricks AI Gateway, internal OIDC brokers) go
stale mid-session and 401. Both wire clients invoke that callable per request.

The ``/models`` catalog helpers in ``hermes_cli.models`` build their HTTP
requests by hand rather than going through a wire client, so each one that
touches ``api_key`` has to mint it first. When they don't, a ``key_cmd``
provider breaks three different ways:

* ``_fetch_anthropic_models`` — ``.strip()`` / ``.startswith()`` on the
  provider object raises ``AttributeError``;
* ``probe_api_models`` — the provider object lands in the ``x-api-key`` /
  ``Authorization`` header value instead of a token;
* ``_custom_endpoint_fingerprint`` — ``"|".join()`` raises ``TypeError``, which
  propagates out of ``cached_fetch_api_models``.

These assert the behavior contract (a real token reaches the wire, and the
cache fingerprint stays stable across rotations), not the call shape.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _provider(token: str = "minted-token"):
    """A minimal stand-in for ``CommandTokenSource``: callable, not a str."""

    class _TokenProvider:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return token

    return _TokenProvider()


class TestMaterializedApiKey:
    def test_string_key_is_stripped(self):
        import hermes_cli.models as mod

        assert mod._materialized_api_key("  sk-key  ") == "sk-key"

    def test_none_becomes_empty(self):
        import hermes_cli.models as mod

        assert mod._materialized_api_key(None) == ""

    def test_callable_is_minted(self):
        import hermes_cli.models as mod

        tp = _provider("  minted  ")
        assert mod._materialized_api_key(tp) == "minted"
        assert tp.calls == 1, "the provider must be invoked, not stringified"

    def test_mint_failure_degrades_to_empty(self):
        """A probe is best-effort: callers treat '' as 'no catalog available'.

        Raising here would turn an unreachable catalog into a failed ``/model``
        switch, which is strictly worse than accepting the model unverified.
        """
        import hermes_cli.models as mod

        def _boom():
            raise RuntimeError("token command exited 1")

        assert mod._materialized_api_key(_boom) == ""

    def test_non_string_mint_result_is_rejected(self):
        import hermes_cli.models as mod

        assert mod._materialized_api_key(lambda: object()) == ""


class TestProbeSendsAMintedToken:
    """The token, not the provider object, must reach the request headers."""

    def _headers_for(self, api_key, **kwargs):
        import hermes_cli.models as mod

        captured: dict[str, dict[str, str]] = {}

        def _spy(req, **_kw):
            captured["headers"] = dict(req.headers)
            raise RuntimeError("stop after header build")

        with patch.object(mod, "_urlopen_model_catalog_request", _spy):
            mod.probe_api_models(api_key, "https://gw.example.invalid", **kwargs)
        return captured.get("headers") or {}

    def _auth_values(self, headers):
        return {
            k.lower(): v
            for k, v in headers.items()
            if k.lower() in ("x-api-key", "authorization")
        }

    def test_anthropic_mode_sends_minted_token_as_x_api_key(self):
        headers = self._headers_for(_provider("minted-token"), api_mode="anthropic_messages")
        auth = self._auth_values(headers)
        assert auth.get("x-api-key") == "minted-token"

    def test_openai_mode_sends_minted_token_as_bearer(self):
        headers = self._headers_for(_provider("minted-token"))
        auth = self._auth_values(headers)
        assert auth.get("authorization") == "Bearer minted-token"

    @pytest.mark.parametrize("api_mode", [None, "anthropic_messages"])
    def test_no_header_value_is_a_repr_of_the_provider(self, api_mode):
        """The regression guard: an object repr in an auth header is the bug."""
        headers = self._headers_for(_provider("minted-token"), api_mode=api_mode)
        for value in self._auth_values(headers).values():
            assert isinstance(value, str)
            assert "object at 0x" not in value

    def test_a_string_key_still_works(self):
        headers = self._headers_for("sk-static")
        assert self._auth_values(headers).get("authorization") == "Bearer sk-static"


class TestAnthropicCatalogAcceptsACallable:
    def test_callable_key_does_not_raise(self):
        """``_is_oauth_token`` calls ``.startswith`` — it needs a real str."""
        import hermes_cli.models as mod

        with patch.object(mod, "_urlopen_model_catalog_request", side_effect=OSError("offline")):
            # Returning None (unreachable) is fine; raising AttributeError is not.
            assert mod._fetch_anthropic_models(
                base_url="https://gw.example.invalid", api_key=_provider()
            ) is None

    def test_minted_token_reaches_the_x_api_key_header(self):
        import hermes_cli.models as mod

        captured: dict[str, dict[str, str]] = {}

        def _spy(req, **_kw):
            captured["headers"] = dict(req.headers)
            raise OSError("stop after header build")

        with patch.object(mod, "_urlopen_model_catalog_request", _spy):
            mod._fetch_anthropic_models(
                base_url="https://gw.example.invalid", api_key=_provider("minted-token")
            )

        values = {k.lower(): v for k, v in (captured.get("headers") or {}).items()}
        assert values.get("x-api-key") == "minted-token"


class TestFingerprintIsStableAcrossRotation:
    """A rotating bearer must not bust the catalog cache on every mint.

    Hashing the minted VALUE would change the fingerprint each rotation, so
    every catalog read would miss and re-probe — defeating the
    stale-while-revalidate tier the cache exists to provide. The endpoint URL
    in the cache key already scopes the entry.
    """

    def test_two_different_mints_share_one_fingerprint(self):
        import hermes_cli.models as mod

        first = mod._custom_endpoint_fingerprint(_provider("token-v1"), "anthropic_messages", None)
        second = mod._custom_endpoint_fingerprint(_provider("token-v2"), "anthropic_messages", None)
        assert first == second

    def test_callable_does_not_raise(self):
        import hermes_cli.models as mod

        assert isinstance(
            mod._custom_endpoint_fingerprint(_provider(), None, None), str
        )

    def test_api_mode_still_busts_the_fingerprint(self):
        """The stand-in must not flatten the other inputs into one bucket."""
        import hermes_cli.models as mod

        tp = _provider()
        assert mod._custom_endpoint_fingerprint(tp, "anthropic_messages", None) != \
            mod._custom_endpoint_fingerprint(tp, "chat_completions", None)

    def test_headers_still_bust_the_fingerprint(self):
        import hermes_cli.models as mod

        tp = _provider()
        assert mod._custom_endpoint_fingerprint(tp, None, {"X-Tenant": "a"}) != \
            mod._custom_endpoint_fingerprint(tp, None, {"X-Tenant": "b"})

    def test_a_callable_and_a_static_key_do_not_collide(self):
        import hermes_cli.models as mod

        assert mod._custom_endpoint_fingerprint(_provider(), None, None) != \
            mod._custom_endpoint_fingerprint("sk-static", None, None)

    def test_cached_fetch_api_models_survives_a_callable(self):
        """The TypeError propagated out of the cache wrapper before the fix."""
        import hermes_cli.models as mod

        with patch.object(mod, "_load_provider_models_cache", return_value={}), \
             patch.object(mod, "_save_provider_models_cache"), \
             patch.object(mod, "fetch_api_models", return_value=["m1"]) as live:
            out = mod.cached_fetch_api_models(_provider(), "https://gw.example.invalid/v1")
        assert out == ["m1"]
        live.assert_called_once()
