"""Contracts enforced by the canonical test runner's clean environment."""

import os


def test_canonical_runner_bypasses_loopback_proxies():
    """Local fixture servers must never inherit an OS-level proxy route."""
    no_proxy = {
        host.strip()
        for host in os.environ.get("NO_PROXY", "").split(",")
        if host.strip()
    }

    assert {"127.0.0.1", "localhost", "::1"} <= no_proxy
