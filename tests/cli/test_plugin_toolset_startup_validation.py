"""Regression coverage for plugin toolset validation during CLI startup."""

from __future__ import annotations

import json
import sys
import threading
import types
from types import SimpleNamespace


def test_untracked_discovery_serves_persisted_toolset_keys_without_waiting(
    tmp_path, monkeypatch
):
    """Direct prewarm keeps config resolution overlapped with discovery."""
    plugin_toolset = "cached_direct_prewarm_tools"
    hermes_home = tmp_path / "hermes-home"
    cache_dir = hermes_home / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "plugin_toolset_keys.json").write_text(
        json.dumps({"toolset_keys": [plugin_toolset], "portable_mcp": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import hermes_cli.plugins as plugins_mod

    manager = plugins_mod.PluginManager()
    discovery_entered = threading.Event()
    allow_discovery = threading.Event()
    keys_returned = threading.Event()
    observed = {}

    def paused_discovery():
        discovery_entered.set()
        if not allow_discovery.wait(timeout=5):
            raise RuntimeError("cached toolset lookup blocked plugin discovery")

    monkeypatch.setattr(manager, "_discover_and_load_inner", paused_discovery)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    monkeypatch.setattr(plugins_mod, "_background_discovery_thread", None)

    prewarm = threading.Thread(
        target=plugins_mod.discover_plugins,
        name="tool-prewarm",
        daemon=True,
    )

    def read_cached_keys():
        observed["keys"] = plugins_mod.get_plugin_toolset_keys_nowait()
        keys_returned.set()

    reader = threading.Thread(target=read_cached_keys, daemon=True)
    prewarm.start()
    assert discovery_entered.wait(timeout=5), "prewarm discovery did not start"
    reader.start()

    try:
        assert keys_returned.wait(timeout=2), "cached key lookup joined prewarm"
        assert observed["keys"] == {plugin_toolset}
    finally:
        allow_discovery.set()
        prewarm.join(timeout=5)
        reader.join(timeout=5)


def test_untracked_tool_prewarm_discovery_finishes_before_validation(
    monkeypatch,
):
    """Direct ``cli.py -w`` discovery must have one completion boundary.

    That path imports ``model_tools`` from an untracked ``tool-prewarm``
    thread. Model the resulting direct ``discover_plugins()`` call while a
    plugin is paused in registration, then validate on the main thread.
    """
    plugin_toolset = "direct_prewarm_tools"
    plugin_tool = "direct_prewarm_health"
    unknown_toolset = "genuinely_missing_toolset"

    import hermes_cli.plugins as plugins_mod
    from tools.registry import registry

    manager = plugins_mod.PluginManager()
    discovery_entered = threading.Event()
    validation_discovery_returned = threading.Event()
    registration_complete = threading.Event()
    observed = {}

    def load_prewarmed_plugin():
        discovery_entered.set()
        observed["validation_returned_before_registration"] = (
            validation_discovery_returned.wait(timeout=2.0)
        )
        registry.register(
            name=plugin_tool,
            toolset=plugin_toolset,
            schema={
                "name": plugin_tool,
                "description": "Read-only health check",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda _args, **_kwargs: '{"status":"ok"}',
        )
        manager._plugin_tool_names.add(plugin_tool)
        registration_complete.set()

    monkeypatch.setattr(manager, "_discover_and_load_inner", load_prewarmed_plugin)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    monkeypatch.setattr(plugins_mod, "_background_discovery_thread", None)

    validation_thread_id = threading.get_ident()
    original_discover = plugins_mod.discover_plugins

    def observe_validation_discovery(*args, **kwargs):
        result = original_discover(*args, **kwargs)
        if threading.get_ident() == validation_thread_id:
            validation_discovery_returned.set()
        return result

    monkeypatch.setattr(plugins_mod, "discover_plugins", observe_validation_discovery)

    prewarm = threading.Thread(
        target=plugins_mod.discover_plugins,
        name="tool-prewarm",
        daemon=True,
    )
    prewarm.start()
    assert discovery_entered.wait(timeout=5), "prewarm discovery did not start"

    try:
        import cli as cli_mod

        warnings = []
        monkeypatch.setattr(
            cli_mod.HermesCLI,
            "_console_print",
            lambda _self, message, *_args, **_kwargs: warnings.append(str(message)),
        )
        cli_mod.HermesCLI(
            toolsets=[plugin_toolset, unknown_toolset],
            compact=True,
            max_turns=1,
        )

        assert registration_complete.wait(timeout=5)
        assert observed["validation_returned_before_registration"] is False
        unknown_warnings = [msg for msg in warnings if "Unknown toolsets" in msg]
        assert len(unknown_warnings) == 1
        assert unknown_toolset in unknown_warnings[0]
        assert plugin_toolset not in unknown_warnings[0]
    finally:
        validation_discovery_returned.set()
        prewarm.join(timeout=5)
        registry.deregister(plugin_tool)


def test_completed_discovery_skips_redundant_join(monkeypatch):
    """A completed background discovery must not force a redundant join."""
    plugin_toolset = "completed_discovery_tools"
    plugin_tool = "completed_discovery_health"

    import hermes_cli.plugins as plugins_mod
    from tools.registry import registry

    manager = plugins_mod.PluginManager()
    manager._discovered = True
    registry.register(
        name=plugin_tool,
        toolset=plugin_toolset,
        schema={
            "name": plugin_tool,
            "description": "Read-only health check",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args, **_kwargs: '{"status":"ok"}',
    )
    manager._plugin_tool_names.add(plugin_tool)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    monkeypatch.setattr(plugins_mod, "_background_discovery_thread", None)

    def fail_on_join(*args, **kwargs):
        raise AssertionError("validation joined discovery although it had completed")

    monkeypatch.setattr(plugins_mod, "_join_background_discovery", fail_on_join)

    try:
        import cli as cli_mod

        warnings = []
        monkeypatch.setattr(
            cli_mod.HermesCLI,
            "_console_print",
            lambda _self, message, *_args, **_kwargs: warnings.append(str(message)),
        )
        cli_mod.HermesCLI(
            toolsets=[plugin_toolset],
            compact=True,
            max_turns=1,
        )
        assert not any("Unknown toolsets" in msg for msg in warnings)
    finally:
        registry.deregister(plugin_tool)


def test_config_selected_plugin_toolset_waits_for_discovery_before_warning(
    tmp_path, monkeypatch, caplog
):
    """A cached plugin key must be validated against completed discovery."""
    plugin_name = "startup-validation-plugin"
    plugin_toolset = "startup_validation_tools"
    plugin_tool = "startup_validation_health"
    unknown_toolset = "genuinely_unknown_startup_toolset"
    hermes_home = tmp_path / "hermes-home"
    plugin_dir = hermes_home / "plugins" / plugin_name
    cache_dir = hermes_home / "cache"
    plugin_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)

    discovery_entered = threading.Event()
    allow_registration = threading.Event()
    registration_complete = threading.Event()
    validation_joined_discovery = threading.Event()
    gate_module = types.ModuleType("_startup_validation_plugin_gate")
    gate_module.__dict__.update(
        discovery_entered=discovery_entered,
        allow_registration=allow_registration,
        registration_complete=registration_complete,
    )
    monkeypatch.setitem(sys.modules, gate_module.__name__, gate_module)

    (plugin_dir / "plugin.yaml").write_text(
        "\n".join([
            f"name: {plugin_name}",
            'version: "0.1.0"',
            "description: Startup validation test plugin",
            "",
        ]),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from _startup_validation_plugin_gate import (\n"
        "    allow_registration, discovery_entered, registration_complete,\n"
        ")\n\n"
        "def register(ctx):\n"
        "    discovery_entered.set()\n"
        "    if not allow_registration.wait(timeout=10):\n"
        "        raise RuntimeError('validation never reached plugin discovery')\n"
        "    ctx.register_tool(\n"
        f"        name={plugin_tool!r},\n"
        f"        toolset={plugin_toolset!r},\n"
        "        schema={\n"
        f"            'name': {plugin_tool!r},\n"
        "            'description': 'Read-only health check',\n"
        "            'parameters': {'type': 'object', 'properties': {}},\n"
        "        },\n"
        '        handler=lambda args, **kwargs: \'{"status": "ok"}\',\n'
        "    )\n"
        "    registration_complete.set()\n",
        encoding="utf-8",
    )
    config = {
        "plugins": {"enabled": [plugin_name]},
        "platform_toolsets": {"cli": [plugin_toolset]},
        "known_plugin_toolsets": {"cli": [plugin_toolset]},
    }
    (hermes_home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        f"    - {plugin_name}\n"
        "platform_toolsets:\n"
        "  cli:\n"
        f"    - {plugin_toolset}\n"
        "known_plugin_toolsets:\n"
        "  cli:\n"
        f"    - {plugin_toolset}\n",
        encoding="utf-8",
    )
    (cache_dir / "plugin_toolset_keys.json").write_text(
        json.dumps({"toolset_keys": [plugin_toolset], "portable_mcp": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import hermes_cli.mcp_startup as mcp_startup
    import hermes_cli.plugins as plugins_mod
    from hermes_cli.main import _prepare_agent_startup
    from hermes_cli.tools_config import _get_platform_tools
    from tools.registry import registry

    monkeypatch.setattr(plugins_mod, "_plugin_manager", None)
    monkeypatch.setattr(plugins_mod, "_background_discovery_thread", None)
    monkeypatch.setattr(
        mcp_startup, "start_background_mcp_discovery", lambda **_kw: None
    )

    try:
        _prepare_agent_startup(
            SimpleNamespace(
                command="chat",
                yolo=False,
                safe_mode=False,
                accept_hooks=False,
                tui=False,
            )
        )
        assert discovery_entered.wait(timeout=5), "plugin discovery did not start"

        selected = _get_platform_tools(config, "cli")
        assert plugin_toolset in selected
        assert not registration_complete.is_set()
        assert not any(
            "no valid toolsets configured" in record.getMessage()
            and plugin_toolset in record.getMessage()
            for record in caplog.records
        )

        original_join = plugins_mod._join_background_discovery

        def release_then_join_discovery(*args, **kwargs):
            validation_joined_discovery.set()
            allow_registration.set()
            return original_join(*args, **kwargs)

        monkeypatch.setattr(
            plugins_mod, "_join_background_discovery", release_then_join_discovery
        )

        import cli as cli_mod

        warnings = []
        monkeypatch.setattr(
            cli_mod.HermesCLI,
            "_console_print",
            lambda _self, message, *_args, **_kwargs: warnings.append(str(message)),
        )
        cli_mod.HermesCLI(
            toolsets=[plugin_toolset, unknown_toolset],
            compact=True,
            max_turns=1,
        )

        assert validation_joined_discovery.is_set()
        assert registration_complete.is_set()
        unknown_warnings = [msg for msg in warnings if "Unknown toolsets" in msg]
        assert len(unknown_warnings) == 1
        assert unknown_toolset in unknown_warnings[0]
        assert plugin_toolset not in unknown_warnings[0]
    finally:
        allow_registration.set()
        thread = plugins_mod._background_discovery_thread
        if thread is not None:
            thread.join(timeout=5)
        registry.deregister(plugin_tool)
