"""Profile-isolation regression tests for the gateway runtime-resolution memo.

The per-message memo in gateway/run.py (_memoized_resolve_runtime) caches
resolve_runtime_provider() results keyed on config/auth file signatures plus
a TTL. A multiplex gateway resolves multiple profiles' agents in the same OS
process (the desktop tui_gateway switches profiles per request via
set_hermes_home_override), so the key must carry the profile identity:
without hermes_home, two profiles whose config.yaml/auth.json share the same
(mtime_ns, size) — exactly what `hermes profile create --clone-all` produces
via mtime-preserving shutil.copy2 — would resolve to the same memo slot and
one profile would receive the other's cached api_key/base_url for up to the
300s TTL. Same profile-boundary fix as #78185 applies to agent/moa_loop.py's
sibling cache.
"""

from pathlib import Path

import pytest

from hermes_constants import get_hermes_home

# The two_profiles fixture lives in the sibling profile-isolation suite.
from tests.test_profile_isolation_runtime import two_profiles  # noqa: F401


def _under_override(home: Path, fn):
    """Run ``fn`` with the profile override set to ``home`` and reset after."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(home))
    try:
        return fn()
    finally:
        reset_hermes_home_override(token)


class TestGatewayRuntimeMemoProfileIsolation:
    """gateway/run.py's runtime memo must not leak credentials across profiles."""

    def test_memo_does_not_leak_credentials_across_profiles(
        self, two_profiles, monkeypatch
    ):
        prof_a, prof_b = two_profiles
        import gateway.run as gateway_run
        import hermes_cli.runtime_provider as rp_mod

        with gateway_run._runtime_resolve_memo_lock:
            gateway_run._runtime_resolve_memo.clear()

        def fake_resolve(*, requested=None, target_model=None, **_kw):
            # A realistic resolver reads the active profile's own config for
            # credentials — simulated here by keying off the live override.
            home = str(get_hermes_home())
            return {
                "api_key": f"secret-for-{Path(home).name}",
                "base_url": f"https://{Path(home).name}.example.com/v1",
                "provider": requested or "openai",
                "requested_provider": requested,
                "api_mode": "chat_completions",
                "command": None,
                "args": [],
                "credential_pool": None,
            }

        monkeypatch.setattr(rp_mod, "resolve_runtime_provider", fake_resolve)

        resolved_a = _under_override(
            prof_a, lambda: gateway_run._memoized_resolve_runtime()
        )
        resolved_b = _under_override(
            prof_b, lambda: gateway_run._memoized_resolve_runtime()
        )

        assert resolved_a["api_key"] == f"secret-for-{prof_a.name}"
        assert resolved_b["api_key"] == f"secret-for-{prof_b.name}", (
            "profile B's runtime resolution must not receive profile A's "
            "cached credentials for the same memo key"
        )
        assert resolved_a["base_url"] != resolved_b["base_url"]

    def test_signature_includes_hermes_home(self, two_profiles):
        """The memo signature must differ between profiles even when the
        config/auth files are byte-identical (cloned profiles share
        mtime_ns/size via mtime-preserving copy2)."""
        import gateway.run as gateway_run

        prof_a, prof_b = two_profiles
        sig_a = _under_override(prof_a, gateway_run._runtime_resolve_memo_signature)
        sig_b = _under_override(prof_b, gateway_run._runtime_resolve_memo_signature)
        assert sig_a != sig_b
        assert sig_a[0] == str(prof_a)
        assert sig_b[0] == str(prof_b)
