"""Tests for providers config entry validation and normalization.

Covers Issue #9332: camelCase keys silently ignored, non-URL strings
accepted as base_url, and unknown keys go unreported.
"""

import logging

import pytest

from hermes_cli.config import (
    _PROVIDER_NORMALIZE_WARNED,
    _normalize_custom_provider_entry,
)


class TestNormalizeCustomProviderEntry:
    """Tests for _normalize_custom_provider_entry validation."""

    @pytest.fixture(autouse=True)
    def _reset_warn_cache(self):
        """The normalizer deduplicates its warnings via a process-lifetime
        cache; clear it around each test so warning assertions are independent
        of test order."""
        _PROVIDER_NORMALIZE_WARNED.clear()
        yield
        _PROVIDER_NORMALIZE_WARNED.clear()





    def test_unknown_keys_warned_once_per_signature(self, caplog):
        """Repeated normalization of the same entry (as happens on every
        picker/inventory load) must warn only once — otherwise the warning
        storms the log handler. Fix B."""
        entry = {
            "base_url": "https://api.example.com/v1",
            "api_key": "***",
            "unknownField": "value",
        }
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                _normalize_custom_provider_entry(
                    dict(entry), provider_key="test"
                )
        unknown_warnings = [
            r for r in caplog.records
            if "unknown config keys" in r.message.lower()
        ]
        assert len(unknown_warnings) == 1




    def test_env_var_placeholder_in_base_url_not_rejected(self):
        """A base_url that is an un-expanded ${ENV_VAR} placeholder must not be
        rejected as an invalid URL — it is expanded at runtime, so a caller
        reaching this normalizer with raw config would otherwise see the
        provider silently dropped. Regression test for #14457."""
        entry = {
            "name": "PROVIDER_A",
            "base_url": "${PROVIDER_A_BASE_URL}",
            "key_env": "PROVIDER_A_API_KEY",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="PROVIDER_A")
        assert result is not None
        assert result["base_url"] == "${PROVIDER_A_BASE_URL}"

    def test_max_output_tokens_is_supported_and_normalized(self, caplog):
        """A per-provider output cap must survive compatibility normalization.

        The foreground CLI reads this cap to constrain a single-slot local
        OpenAI-compatible server; treating it as unknown both emits a noisy
        warning and drops the cap from legacy-compatible provider entries.
        """
        entry = {
            "base_url": "http://127.0.0.1:18080/v1",
            "max_output_tokens": 1024,
        }
        with caplog.at_level(logging.WARNING):
            result = _normalize_custom_provider_entry(entry, provider_key="llamacpp")

        assert result is not None
        assert result["max_output_tokens"] == 1024
        assert "unknown config keys" not in caplog.text


