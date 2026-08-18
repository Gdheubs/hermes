"""Tests for the v38 config migration: memory.memory_enabled → memory.builtin_enabled.

The legacy key conflated "is the memory subsystem on?" with "is the built-in
MEMORY.md/USER.md file store on?" — which made `builtin_enabled: false` +
`provider: X` (the recommended provider combo) read as "memory disabled" and
prompted agents to "fix" a correct configuration.
"""

from unittest.mock import patch


def _run_migration(raw_config):
    """Apply _migrate_to_38 against raw_config, returning (config, results)."""
    from hermes_cli import config_migrations as cm

    results = {"config_added": [], "warnings": []}

    class _FakeCfg:
        def __init__(self, cfg):
            self._cfg = cfg

        def read_raw_config(self):
            return self._cfg

        def _persist_migration(self, cfg):
            self._cfg = cfg

    fake = _FakeCfg(raw_config)
    with patch.object(cm, "_cfg", return_value=fake):
        cm._migrate_to_38(results, quiet=True)
    return fake._cfg, results


class TestMigrateTo38:
    def test_renames_legacy_key(self):
        cfg, results = _run_migration({"memory": {"memory_enabled": False}})
        assert "memory_enabled" not in cfg["memory"]
        assert cfg["memory"]["builtin_enabled"] is False
        assert results["config_added"]

    def test_preserves_value(self):
        cfg, _ = _run_migration({"memory": {"memory_enabled": True}})
        assert cfg["memory"]["builtin_enabled"] is True

    def test_keeps_sibling_keys(self):
        cfg, _ = _run_migration(
            {
                "memory": {
                    "memory_enabled": False,
                    "user_profile_enabled": True,
                    "provider": "memtensor",
                }
            }
        )
        assert cfg["memory"]["user_profile_enabled"] is True
        assert cfg["memory"]["provider"] == "memtensor"

    def test_noop_without_legacy_key(self):
        cfg, results = _run_migration({"memory": {"builtin_enabled": False}})
        assert cfg["memory"]["builtin_enabled"] is False
        assert not results["config_added"]

    def test_noop_without_memory_section(self):
        cfg, results = _run_migration({"model": {"default": "x"}})
        assert "memory" not in cfg
        assert not results["config_added"]

    def test_both_keys_canonical_wins(self):
        cfg, results = _run_migration(
            {"memory": {"memory_enabled": True, "builtin_enabled": False}}
        )
        assert "memory_enabled" not in cfg["memory"]
        assert cfg["memory"]["builtin_enabled"] is False
        assert results["warnings"]


def test_registry_contains_v38():
    from hermes_cli import config_migrations as cm

    assert (38, cm._migrate_to_38) in cm.MIGRATIONS


class TestBuiltinMemoryEnabledHelper:
    """The alias helper must read RAW config so defaults never mask user intent."""

    def _effective(self, raw_mem):
        raw = {"memory": raw_mem} if raw_mem is not None else {}
        with patch(
            "hermes_cli.config.read_raw_config_readonly", return_value=raw
        ):
            from tools.memory_tool import builtin_memory_enabled

            return builtin_memory_enabled()

    def test_legacy_false_respected(self):
        assert self._effective({"memory_enabled": False}) is False

    def test_legacy_true(self):
        assert self._effective({"memory_enabled": True}) is True

    def test_canonical_wins_over_legacy(self):
        assert self._effective({"builtin_enabled": False, "memory_enabled": True}) is False

    def test_user_profile_alone_enables_store(self):
        assert self._effective({"user_profile_enabled": True}) is True

    def test_user_profile_false_alone_keeps_store_on(self):
        """user_profile_enabled-only config must NOT silently disable the
        built-in store: pre-v38 the merged memory_enabled: true default kept it
        on, and an absent builtin/legacy key still defaults to True."""
        assert self._effective({"user_profile_enabled": False}) is True

    def test_user_profile_true_keeps_store_on_despite_builtin_false(self):
        """builtin_enabled: false + user_profile_enabled: true still creates
        the store (user profile), so the tool stays advertised."""
        assert self._effective({"builtin_enabled": False, "user_profile_enabled": True}) is True

    def test_default_when_unset(self):
        assert self._effective(None) is True
