"""The built-in memory tool must only be advertised when the store can exist,
and its error must never invite a wrong config change (issues #60805, #32624).
"""

from unittest.mock import patch

import tools.memory_tool as mt


class TestCheckRequirements:
    def test_hidden_when_builtin_disabled(self):
        with patch("tools.memory_tool.builtin_memory_enabled", return_value=False):
            assert mt.check_memory_requirements() is False

    def test_visible_when_builtin_enabled(self):
        with patch("tools.memory_tool.builtin_memory_enabled", return_value=True):
            assert mt.check_memory_requirements() is True


class TestStoreNoneError:
    """Residual paths (skip_memory cron forks, races) get a state-aware error."""

    def test_provider_active_points_to_provider_tools(self):
        with patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"memory": {"provider": "memtensor"}},
        ):
            out = mt.memory_tool(store=None)
        assert "memtensor" in out
        assert "memos_search" in out
        assert "no config change is needed" in out

    def test_no_provider_is_non_actionable(self):
        with patch(
            "hermes_cli.config.load_config_readonly", return_value={"memory": {}}
        ):
            out = mt.memory_tool(store=None)
        assert "no config change is needed" in out
        assert "builtin_enabled" in out

    def test_never_invites_enabling(self):
        """The error must not suggest flipping config (that is the trap)."""
        with patch(
            "hermes_cli.config.load_config_readonly", return_value={"memory": {}}
        ):
            out = mt.memory_tool(store=None)
        assert "hermes config" not in out.lower()
        assert "re-enabl" not in out.lower()
