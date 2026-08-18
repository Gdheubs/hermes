import json
from pathlib import Path

import pytest
import yaml

from tests.tools.conftest import register_all_web_providers

from tools.website_policy import WebsitePolicyError, check_website_access, load_website_blocklist


def test_load_website_blocklist_merges_config_and_shared_file(tmp_path):
    shared = tmp_path / "community-blocklist.txt"
    shared.write_text("# comment\nexample.org\nsub.bad.net\n", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "security": {
                    "website_blocklist": {
                        "enabled": True,
                        "domains": ["example.com", "https://www.evil.test/path"],
                        "shared_files": [str(shared)],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    policy = load_website_blocklist(config_path)

    assert policy["enabled"] is True
    assert {rule["pattern"] for rule in policy["rules"]} == {
        "example.com",
        "evil.test",
        "example.org",
        "sub.bad.net",
    }


def test_check_website_access_matches_parent_domain_subdomains(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "security": {
                    "website_blocklist": {
                        "enabled": True,
                        "domains": ["example.com"],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    blocked = check_website_access("https://docs.example.com/page", config_path=config_path)

    assert blocked is not None
    assert blocked["host"] == "docs.example.com"
    assert blocked["rule"] == "example.com"


def test_default_config_exposes_website_blocklist_shape():
    from hermes_cli.config import DEFAULT_CONFIG

    website_blocklist = DEFAULT_CONFIG["security"]["website_blocklist"]
    assert website_blocklist["enabled"] is False
    assert website_blocklist["domains"] == []
    assert website_blocklist["shared_files"] == []


def test_load_website_blocklist_wraps_shared_file_read_errors(tmp_path, monkeypatch):
    shared = tmp_path / "community-blocklist.txt"
    shared.write_text("example.org\n", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "security": {
                    "website_blocklist": {
                        "enabled": True,
                        "shared_files": [str(shared)],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    def failing_read_text(self, *args, **kwargs):
        raise PermissionError("no permission")

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    # Unreadable shared files are now warned and skipped (not raised),
    # so the blocklist loads successfully but without those rules.
    result = load_website_blocklist(config_path)
    assert result["enabled"] is True
    assert result["rules"] == []  # shared file rules skipped


def test_check_website_access_blocks_scheme_less_urls(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "security": {
                    "website_blocklist": {
                        "enabled": True,
                        "domains": ["blocked.test"],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    blocked = check_website_access("www.blocked.test/path", config_path=config_path)

    assert blocked is not None
    assert blocked["host"] == "www.blocked.test"
    assert blocked["rule"] == "blocked.test"


def test_browser_navigate_returns_policy_block(monkeypatch):
    from tools import browser_tool

    # Allow SSRF check to pass so the policy check is reached
    monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: True)
    monkeypatch.setattr(
        browser_tool,
        "check_website_access",
        lambda url: {
            "host": "blocked.test",
            "rule": "blocked.test",
            "source": "config",
            "message": "Blocked by website policy",
        },
    )
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *args, **kwargs: pytest.fail("browser command should not run for blocked URL"),
    )

    result = json.loads(browser_tool.browser_navigate("https://blocked.test"))

    assert result["success"] is False
    assert result["blocked_by_policy"]["rule"] == "blocked.test"


def test_browser_navigate_allows_when_shared_file_missing(monkeypatch, tmp_path):
    """Missing shared blocklist files are warned and skipped, not fatal."""

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "security": {
                    "website_blocklist": {
                        "enabled": True,
                        "shared_files": ["missing-blocklist.txt"],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # check_website_access should return None (allow) — missing file is skipped
    result = check_website_access("https://allowed.test", config_path=config_path)
    assert result is None


def test_browser_tool_fails_closed_when_policy_module_unavailable(monkeypatch):
    """If the website-policy module cannot be imported, navigation must be
    blocked (fail-closed), never allowed past a policy we could not load.

    Regression for the fail-open path that returned ``None`` (allow) when the
    policy module import raised — silently bypassing the website blocklist.
    """
    import builtins
    import importlib
    from tools import browser_tool

    real_import = builtins.__import__

    def _blocked_policy_import(name, *a, **kw):
        if name == "tools.website_policy":
            raise ImportError("simulated website_policy module failure")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked_policy_import)

    # Force the module to re-run its import guard with the policy import
    # blocked. The guard must install a FAIL-CLOSED fallback, not
    # ``lambda url: None``.
    importlib.reload(browser_tool)

    try:
        result = browser_tool.check_website_access("https://unknown.test")
    finally:
        # Restore the real import before anything else imports the module.
        monkeypatch.setattr(builtins, "__import__", real_import)
        importlib.reload(browser_tool)

    # Fail-closed: the result must be truthy (blocked), never None/allow.
    assert result is not None
    assert bool(result) is True
    assert result.get("rule") == "policy-unavailable"
    assert "unavailable" in result.get("message", "").lower()


def _reload_browser_tool_with_policy_import_blocked(monkeypatch):
    """Reload tools.browser_tool with the website_policy import raising, so its
    fail-closed fallback is installed. Returns the reloaded module and a
    callable that restores the real import + reloads. Callers must invoke the
    restore in a finally.
    """
    import builtins
    import importlib
    from tools import browser_tool

    real_import = builtins.__import__

    def _blocked_policy_import(name, *a, **kw):
        if name == "tools.website_policy":
            raise ImportError("simulated website_policy module failure")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked_policy_import)
    importlib.reload(browser_tool)

    def _restore():
        monkeypatch.setattr(builtins, "__import__", real_import)
        importlib.reload(browser_tool)

    return browser_tool, _restore


def test_both_pre_navigation_paths_fail_closed_when_policy_module_unavailable(monkeypatch):
    """Acceptance contract: with the website_policy import raised, BOTH
    pre-navigation policy call sites — ``evaluate_url_safety`` and
    ``browser_navigate`` — must return a blocked/error result for a normally
    denied host, never None/success.
    """
    import json
    from tools import browser_tool

    browser_tool, _restore = _reload_browser_tool_with_policy_import_blocked(monkeypatch)
    try:
        # Let the synthetic URL pass the SSRF / private-address pre-checks so
        # the website-policy check is what's exercised.
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: True)
        monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: True)

        # Pre-navigation path #1: evaluate_url_safety
        safety = browser_tool.evaluate_url_safety("https://blocked.test")
        assert safety is not None
        assert safety["success"] is False
        assert safety["blocked_by_policy"]["rule"] == "policy-unavailable"
        assert "unavailable" in safety["error"].lower()

        # Pre-navigation path #2: browser_navigate
        nav = json.loads(browser_tool.browser_navigate("https://blocked.test"))
        assert nav["success"] is False
        assert nav["blocked_by_policy"]["rule"] == "policy-unavailable"
        assert "unavailable" in nav["error"].lower()
    finally:
        _restore()


def test_check_website_access_fails_closed_on_malformed_config(tmp_path, monkeypatch):
    """Malformed config with default path must FAIL CLOSED (return a blocked
    result), never silently allow. Regression for the prior fail-open behavior
    that returned ``None`` (allow) on a config error — a broken blocklist must
    not disable web enforcement.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text("security: [oops\n", encoding="utf-8")

    # With explicit config_path (test mode), errors propagate
    with pytest.raises(WebsitePolicyError):
        check_website_access("https://example.com", config_path=config_path)

    # Simulate default path by pointing HERMES_HOME to tmp_path
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import website_policy
    website_policy.invalidate_cache()

    # With default path, errors are caught and FAIL CLOSED — blocked, not allowed
    result = check_website_access("https://example.com")
    assert result is not None
    assert bool(result) is True
    assert result["rule"] == "policy-unavailable"
    assert result["source"] == "website-policy-config-error"
    assert "blocked" in result["message"].lower()


def test_check_website_access_operator_disabled_is_visible_allow(tmp_path, monkeypatch, caplog):
    """Operator-disabled policy (enabled: false, valid config) is a distinct,
    explicit allow — NOT an error fallback. It returns None (allow) but is
    logged as an explicit operator setting, and the rule is NOT the
    fail-closed 'policy-unavailable' error.
    """
    import logging

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "security": {
                    "website_blocklist": {
                        "enabled": False,
                        "domains": ["blocked.test"],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import website_policy
    website_policy.invalidate_cache()

    with caplog.at_level(logging.INFO, logger="tools.website_policy"):
        result = check_website_access("https://blocked.test")

    # Operator-disabled is an explicit allow — never a blocked result
    assert result is None
    assert any("operator-disabled" in rec.message.lower() for rec in caplog.records)


class TestWebToolPolicy:
    """Tests that exercise web_extract_tool with website-policy gates.

    These tests need the bundled web providers to be registered in the
    agent.web_search_registry so the tool dispatchers can find an active
    provider.  Without registration, the tools return an error dict that
    lacks a ``results`` key, causing ``KeyError``.
    """

    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    @pytest.mark.asyncio
    async def test_web_extract_short_circuits_blocked_url(self, monkeypatch):
        from tools import web_tools
        from plugins.web.firecrawl import provider as firecrawl_provider

        # Allow test URLs past SSRF check so website policy is what gets tested
        async def _allow_ssrf(_url: str) -> bool:
            return True

        monkeypatch.setattr(web_tools, "async_is_safe_url", _allow_ssrf)
        # The per-URL website-policy gate moved into the firecrawl plugin's
        # extract() during the web-provider migration. Patch it at the new
        # location.
        monkeypatch.setattr(
            firecrawl_provider,
            "check_website_access",
            lambda url: {
                "host": "blocked.test",
                "rule": "blocked.test",
                "source": "config",
                "message": "Blocked by website policy",
            },
        )
        monkeypatch.setattr(
            firecrawl_provider,
            "_get_firecrawl_client",
            lambda: pytest.fail("firecrawl should not run for blocked URL"),
        )
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
        # Force the firecrawl plugin to be the active extract provider.
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fake-key")

        result = json.loads(await web_tools.web_extract_tool(["https://blocked.test"]))

        assert result["results"][0]["url"] == "https://blocked.test"
        assert "Blocked by website policy" in result["results"][0]["error"]

    @pytest.mark.asyncio
    async def test_web_extract_blocks_redirected_final_url(self, monkeypatch):
        from tools import web_tools
        from plugins.web.firecrawl import provider as firecrawl_provider

        # Allow test URLs past SSRF check so website policy is what gets tested
        async def _allow_ssrf(_url: str) -> bool:
            return True

        monkeypatch.setattr(web_tools, "async_is_safe_url", _allow_ssrf)
        monkeypatch.setattr(firecrawl_provider, "is_safe_url", lambda url: True)

        def fake_check(url):
            if url == "https://allowed.test":
                return None
            if url == "https://blocked.test/final":
                return {
                    "host": "blocked.test",
                    "rule": "blocked.test",
                    "source": "config",
                    "message": "Blocked by website policy",
                }
            pytest.fail(f"unexpected URL checked: {url}")

        class FakeFirecrawlClient:
            def scrape(self, url, formats):
                return {
                    "markdown": "secret content",
                    "metadata": {
                        "title": "Redirected",
                        "sourceURL": "https://blocked.test/final",
                    },
                }

        # After the web-provider migration, the per-URL gate + firecrawl client
        # live in the plugin. Patch both at the plugin location.
        monkeypatch.setattr(firecrawl_provider, "check_website_access", fake_check)
        monkeypatch.setattr(firecrawl_provider, "_get_firecrawl_client", lambda: FakeFirecrawlClient())
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fake-key")

        result = json.loads(await web_tools.web_extract_tool(["https://allowed.test"]))

        assert result["results"][0]["url"] == "https://blocked.test/final"
        assert result["results"][0]["content"] == ""
        assert result["results"][0]["blocked_by_policy"]["rule"] == "blocked.test"
