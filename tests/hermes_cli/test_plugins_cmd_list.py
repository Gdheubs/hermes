import argparse
import json
from types import SimpleNamespace

from hermes_cli import plugins_cmd


def _args(**kwargs):
    defaults = {
        "enabled": False,
        "user": False,
        "no_bundled": False,
        "plain": False,
        "json": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_filter_plugin_entries_enabled_only():
    entries = [
        ("disk-cleanup", "2.0.0", "Bundled", "bundled", None, "disk-cleanup"),
        ("web-search-plus", "2.2.0", "Search", "git", None, "web-search-plus"),
        ("old-plugin", "1.0.0", "Old", "user", None, "old-plugin"),
    ]

    filtered = plugins_cmd._filter_plugin_entries(
        entries,
        _args(enabled=True),
        enabled={"disk-cleanup", "web-search-plus"},
        disabled={"old-plugin"},
    )

    assert [entry[0] for entry in filtered] == ["disk-cleanup", "web-search-plus"]


def test_cmd_list_plain_compact_output(monkeypatch, capsys):
    entries = [
        ("disk-cleanup", "2.0.0", "Bundled", "bundled", None, "disk-cleanup"),
        ("web-search-plus", "2.2.0", "Search", "git", None, "web-search-plus"),
    ]
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: entries)
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: {"web-search-plus"})
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: set())

    plugins_cmd.cmd_list(_args(plain=True, no_bundled=True))

    out = capsys.readouterr().out
    assert "web-search-plus" in out
    assert "enabled" in out
    assert "disk-cleanup" not in out
    assert "Search" not in out  # plain mode stays compact, no descriptions


def _colliding_entries():
    """Two bundled backends that share a manifest name across categories.

    Mirrors the real bundled tree: ``plugins/image_gen/deepinfra`` and
    ``plugins/video_gen/deepinfra`` both declare ``name: deepinfra``.
    """
    return [
        ("deepinfra", "1.0.0", "Image backend", "bundled", None, "image_gen/deepinfra"),
        ("deepinfra", "1.0.0", "Video backend", "bundled", None, "video_gen/deepinfra"),
        ("disk-cleanup", "2.0.0", "Cleanup", "bundled", None, "disk-cleanup"),
    ]


def _patch_discovery(monkeypatch, entries):
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: entries)
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: set())
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: set())


def test_cmd_list_plain_identifier_names_exactly_one_plugin(monkeypatch, capsys):
    """The identifier column is the argument for enable/disable, so it must
    name exactly one plugin — a manifest name shared by two categories does
    not, and has to fall back to the canonical key."""
    entries = _colliding_entries()
    _patch_discovery(monkeypatch, entries)

    plugins_cmd.cmd_list(_args(plain=True))

    printed = [
        line.split()[-1]
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert len(printed) == len(set(printed)), f"ambiguous rows: {printed}"
    # Unambiguous rows are untouched; only the colliding pair is qualified.
    assert "disk-cleanup" in printed
    assert {"image_gen/deepinfra", "video_gen/deepinfra"} <= set(printed)


def test_cmd_list_json_identifier_names_exactly_one_plugin(monkeypatch, capsys):
    """Same invariant for ``--json``, which scripts consume by ``name``."""
    entries = _colliding_entries()
    _patch_discovery(monkeypatch, entries)

    plugins_cmd.cmd_list(_args(json=True))

    payload = json.loads(capsys.readouterr().out)
    names = [row["name"] for row in payload]
    assert len(names) == len(set(names)), f"ambiguous rows: {names}"


def test_cmd_list_identifier_survives_filtering(monkeypatch, capsys):
    """A collision hidden by a filter is still a collision for anyone typing
    the identifier back into a command, so qualification must not depend on
    which rows survive ``--enabled``."""
    entries = _colliding_entries()
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: entries)
    monkeypatch.setattr(
        plugins_cmd, "_get_enabled_set", lambda: {"image_gen/deepinfra"}
    )
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: set())

    plugins_cmd.cmd_list(_args(plain=True, enabled=True))

    printed = [
        line.split()[-1]
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert printed == ["image_gen/deepinfra"]


def test_discover_all_plugins_includes_entrypoint_plugins(monkeypatch, tmp_path):
    bundled_dir = tmp_path / "bundled"
    user_dir = tmp_path / "user"
    bundled_dir.mkdir()
    user_dir.mkdir()

    dist = SimpleNamespace(
        version="0.1.0",
        metadata={"Summary": "Karpathy-style LLM Wikis for Hermes"},
    )
    entry_point = SimpleNamespace(
        name="wiki",
        value="adapters.hermes.cli_plugin",
        group="hermes_agent.plugins",
        dist=dist,
    )

    monkeypatch.setattr(plugins_cmd, "_plugins_dir", lambda: user_dir)
    monkeypatch.setattr(
        "hermes_cli.plugins.get_bundled_plugins_dir",
        lambda: bundled_dir,
    )
    monkeypatch.setattr(
        plugins_cmd.importlib.metadata,
        "entry_points",
        lambda: [entry_point],
    )

    entries = plugins_cmd._discover_all_plugins()

    assert entries == [
        (
            "wiki",
            "0.1.0",
            "Karpathy-style LLM Wikis for Hermes",
            "entrypoint",
            "adapters.hermes.cli_plugin",
            "wiki",
        )
    ]


def test_declared_capabilities_for_entrypoint_uses_distribution_metadata(
    monkeypatch, tmp_path
):
    bundled_dir = tmp_path / "bundled"
    user_dir = tmp_path / "user"
    bundled_dir.mkdir()
    user_dir.mkdir()
    plugin_ep = SimpleNamespace(
        name="thread-namer",
        value="thread_namer.plugin:register",
        group="hermes_agent.plugins",
        dist=SimpleNamespace(version="1.0", metadata={"Summary": ""}),
    )
    capability_ep = SimpleNamespace(
        name="thread-namer.gateway.platform_actions",
        value="thread_namer.plugin:register",
        group="hermes_agent.plugin_capabilities",
    )
    monkeypatch.setattr(plugins_cmd, "_plugins_dir", lambda: user_dir)
    monkeypatch.setattr(
        "hermes_cli.plugins.get_bundled_plugins_dir", lambda: bundled_dir
    )
    monkeypatch.setattr(
        plugins_cmd.importlib.metadata,
        "entry_points",
        lambda: [plugin_ep, capability_ep],
    )

    assert plugins_cmd._declared_capabilities_for_key("thread-namer") == [
        "gateway.platform_actions"
    ]


