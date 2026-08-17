"""Tests for cron scheduler max_turns resolution from config.

Regression test for: TypeError: '<' not supported between instances
of 'int' and 'str' when agent.max_turns is a YAML string like "none".

The cron scheduler reads max_turns from config and passes it as
max_iterations to the agent loop. If the config value is the string
"none" (YAML parses unquoted `none` as the string "none", not None),
the `or` fallback chain kept the truthy string, and
`api_call_count < "none"` crashed at conversation_loop.py:1627.
"""
import pytest


def _resolve_max_iterations(_cfg):
    """Mirror of the scheduler's resolution logic for unit testing."""
    _mt_raw = _cfg.get("agent", {}).get("max_turns") or _cfg.get("max_turns")
    if isinstance(_mt_raw, str) and _mt_raw.lower() in ("none", "null", "unlimited", "infinite", "∞", "", "0", "-1"):
        return 500
    elif isinstance(_mt_raw, str) and _mt_raw.lstrip("-").isdigit():
        return int(_mt_raw)
    elif isinstance(_mt_raw, (int, float)) and _mt_raw in (0, -1):
        return 500
    elif _mt_raw is None:
        return 500
    elif isinstance(_mt_raw, (int, float)):
        return int(_mt_raw)
    else:
        return 500


class TestMaxTurnsResolution:
    """Verify that all valid max_turns spellings resolve to an int.

    The invariant: max_iterations must always be an int after resolution,
    never a string, so `api_call_count < max_iterations` never raises TypeError.
    """

    @pytest.mark.parametrize("value,expected", [
        # String spellings that mean "unlimited" → fallback to 500
        ("none", 500),
        ("null", 500),
        ("unlimited", 500),
        ("infinite", 500),
        ("∞", 500),
        ("", 500),
        # Numeric strings → int
        ("20", 20),
        ("100", 100),
        ("0", 500),  # 0 means unlimited, fallback
        ("-1", 500),  # -1 means unlimited, fallback
        # Integers → int
        (20, 20),
        (100, 100),
        # Floats → int
        (50.0, 50),
        # None → fallback
        (None, 500),
    ])
    def test_max_turns_always_resolves_to_int(self, value, expected):
        cfg = {"agent": {"max_turns": value}}
        result = _resolve_max_iterations(cfg)
        assert isinstance(result, int), f"Expected int, got {type(result)}"
        assert result == expected

    def test_top_level_max_turns_fallback(self):
        """If agent.max_turns is missing, top-level max_turns is used."""
        cfg = {"max_turns": "30"}
        result = _resolve_max_iterations(cfg)
        assert isinstance(result, int)
        assert result == 30

    def test_both_missing_uses_default(self):
        """If neither key exists, fall back to 500."""
        cfg = {}
        result = _resolve_max_iterations(cfg)
        assert isinstance(result, int)
        assert result == 500

    def test_string_none_does_not_crash_comparison(self):
        """The original bug: 'none' string caused TypeError in < comparison."""
        cfg = {"agent": {"max_turns": "none"}}
        result = _resolve_max_iterations(cfg)
        # This comparison must not raise
        assert 1 < result
        assert isinstance(result, int)

    def test_yaml_unquoted_none_is_string(self):
        """Verify that YAML parses unquoted 'none' as string, not None.

        This is the root cause: users write `max_turns: none` in YAML
        expecting Python None, but YAML 1.1 parses it as the string "none".
        """
        import yaml
        parsed = yaml.safe_load("max_turns: none")
        assert parsed["max_turns"] == "none"
        assert isinstance(parsed["max_turns"], str)

        # And our resolver handles it
        result = _resolve_max_iterations(parsed)
        assert isinstance(result, int)
