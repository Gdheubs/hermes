"""Notifier provider plugin discovery.

Scans two directories for notifier provider plugins:

1. Bundled providers: ``plugins/memory/<name>/`` (shipped with hermes-agent)
2. User-installed providers: ``$HERMES_HOME/plugins/<name>/``

Each subdirectory must contain ``__init__.py`` with a class implementing
the NotifierProvider ABC.  On name collisions, bundled providers take
precedence.

Only ONE provider can be active at a time, selected via
``notifier.provider`` in config.yaml.

Usage:
    from plugins.notifiers import discover_notifier_providers, load_notifier_provider

    available = discover_notifier_providers()   # [(name, desc, available), ...]
    provider = load_notifier_provider("mnemosyne")  # NotifierProvider instance
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)

_NOTIFIER_PLUGINS_DIR = Path(__file__).parent

# Synthetic parent package for user-installed providers, so they don't
# collide with bundled providers in sys.modules.
_USER_NAMESPACE = "_hermes_user_notifier"


def _register_synthetic_package(name: str, search_locations: List[str]) -> None:
    """Register an empty package shell in sys.modules.

    User-installed providers import as ``_hermes_user_notifier.<name>``, a
    dotted name whose parents exist nowhere on disk.  Unless those parents
    are present in ``sys.modules``, any relative import inside the plugin
    (``from . import config``) fails with
    ``ModuleNotFoundError: No module named '_hermes_user_notifier'`` — the
    same reason the loader already registers ``plugins`` and
    ``plugins.notifiers`` for bundled providers.
    """
    if name in sys.modules:
        return
    spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
    spec.submodule_search_locations = search_locations
    sys.modules[name] = importlib.util.module_from_spec(spec)


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def _get_user_plugins_dir() -> Optional[Path]:
    """Return ``$HERMES_HOME/plugins/`` or None if unavailable."""
    try:
        from hermes_constants import get_hermes_home
        d = get_hermes_home() / "plugins"
        return d if d.is_dir() else None
    except Exception:
        return None


def _is_memory_provider_dir(path: Path) -> bool:
    """Heuristic: does *path* look like a notifier provider plugin?

    Checks for ``register_notifier_provider`` or ``NotifierProvider`` in the
    ``__init__.py`` source.  Cheap text scan — no import needed.
    """
    init_file = path / "__init__.py"
    if not init_file.exists():
        return False
    try:
        source = init_file.read_text(errors="replace", encoding="utf-8")[:8192]
        return "register_notifier_provider" in source or "NotifierProvider" in source
    except Exception:
        return False


def _iter_provider_dirs() -> List[Tuple[str, Path]]:
    """Yield ``(name, path)`` for all discovered provider directories.

    Scans bundled first, then user-installed.  Bundled takes precedence
    on name collisions (first-seen wins via ``seen`` set).
    """
    seen: set = set()
    dirs: List[Tuple[str, Path]] = []

    # 1. Bundled providers (plugins/memory/<name>/)
    if _NOTIFIER_PLUGINS_DIR.is_dir():
        for child in sorted(_NOTIFIER_PLUGINS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            if not (child / "__init__.py").exists():
                continue
            seen.add(child.name)
            dirs.append((child.name, child))

    # 2. User-installed providers ($HERMES_HOME/plugins/<name>/)
    user_dir = _get_user_plugins_dir()
    if user_dir:
        for child in sorted(user_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            if child.name in seen:
                continue  # bundled takes precedence
            if not _is_memory_provider_dir(child):
                continue  # skip non-memory plugins
            dirs.append((child.name, child))

    return dirs


def find_provider_dir(name: str) -> Optional[Path]:
    """Resolve a provider name to its directory.

    Checks bundled first, then user-installed.
    """
    # Bundled
    bundled = _NOTIFIER_PLUGINS_DIR / name
    if bundled.is_dir() and (bundled / "__init__.py").exists():
        return bundled
    # User-installed
    user_dir = _get_user_plugins_dir()
    if user_dir:
        user = user_dir / name
        if user.is_dir() and _is_memory_provider_dir(user):
            return user
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_notifier_provider_names() -> List[str]:
    """Cheap name-only listing of discoverable notifier providers.

    Unlike :func:`discover_notifier_providers`, this does NOT import provider
    modules or run availability checks — it's a directory scan only, safe to
    call at module-import time (e.g. when building the dashboard config
    schema).
    """
    return sorted({name for name, _ in _iter_provider_dirs()})


def discover_notifier_providers() -> List[Tuple[str, str, bool]]:
    """Scan bundled and user-installed directories for available providers.

    Returns list of (name, description, is_available) tuples.
    Bundled providers take precedence on name collisions.
    """
    results = []

    for name, child in _iter_provider_dirs():
        # Read description from plugin.yaml if available
        desc = ""
        yaml_file = child / "plugin.yaml"
        if yaml_file.exists():
            try:
                import yaml
                with open(yaml_file, encoding="utf-8-sig") as f:
                    meta = yaml.safe_load(f) or {}
                desc = meta.get("description", "")
            except Exception:
                pass

        # Quick availability check — try loading and calling is_available()
        available = True
        try:
            provider = _load_provider_from_dir(child)
            if provider:
                available = provider.is_available()
            else:
                available = False
        except Exception:
            available = False

        results.append((name, desc, available))

    return results


def load_notifier_provider(name: str) -> Optional["NotifierProvider"]:
    """Load and return a NotifierProvider instance by name.

    Checks both bundled (``plugins/memory/<name>/``) and user-installed
    (``$HERMES_HOME/plugins/<name>/``) directories.  Bundled takes
    precedence on name collisions.

    Returns None if the provider is not found or fails to load.
    """
    provider_dir = find_provider_dir(name)
    if not provider_dir:
        logger.debug("Memory provider '%s' not found in bundled or user plugins", name)
        return None

    try:
        provider = _load_provider_from_dir(provider_dir)
        if provider:
            return provider
        logger.warning("Memory provider '%s' loaded but no provider instance found", name)
        return None
    except Exception as e:
        logger.warning("Failed to load notifier provider '%s': %s", name, e)
        return None


def _load_provider_from_dir(provider_dir: Path) -> Optional["NotifierProvider"]:
    """Import a provider module and extract the NotifierProvider instance.

    The module must have either:
    - A register(ctx) function (plugin-style) — we simulate a ctx
    - A top-level class that extends NotifierProvider — we instantiate it
    """
    name = provider_dir.name
    # Use a separate namespace for user-installed plugins so they don't
    # collide with bundled providers in sys.modules.
    _is_bundled = _NOTIFIER_PLUGINS_DIR in provider_dir.parents or provider_dir.parent == _NOTIFIER_PLUGINS_DIR
    module_name = f"plugins.notifiers.{name}" if _is_bundled else f"{_USER_NAMESPACE}.{name}"
    init_file = provider_dir / "__init__.py"

    if not init_file.exists():
        return None

    # Check if already loaded.  A synthetic package shell registered by
    # discover_plugin_cli_commands() for relative-import support has no
    # __file__; only reuse modules that were actually loaded from disk.
    cached = sys.modules.get(module_name)
    if cached is not None and getattr(cached, "__file__", None):
        mod = cached
    else:
        # Handle relative imports within the plugin
        # First ensure the parent packages are registered
        for parent in ("plugins", "plugins.notifiers"):
            if parent not in sys.modules:
                parent_path = Path(__file__).parent
                if parent == "plugins":
                    parent_path = parent_path.parent
                parent_init = parent_path / "__init__.py"
                if parent_init.exists():
                    spec = importlib.util.spec_from_file_location(
                        parent, str(parent_init),
                        submodule_search_locations=[str(parent_path)]
                    )
                    if spec:
                        parent_mod = importlib.util.module_from_spec(spec)
                        sys.modules[parent] = parent_mod
                        try:
                            spec.loader.exec_module(parent_mod)
                        except Exception:
                            pass

        # User-installed plugins need their synthetic parent registered the
        # same way, or relative imports inside the plugin cannot resolve.
        if not _is_bundled:
            _register_synthetic_package(_USER_NAMESPACE, [])

        # Now load the provider module
        spec = importlib.util.spec_from_file_location(
            module_name, str(init_file),
            submodule_search_locations=[str(provider_dir)]
        )
        if not spec:
            return None

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod

        # Register submodules so relative imports work
        # e.g., "from .store import MemoryStore" in holographic plugin
        for sub_file in provider_dir.glob("*.py"):
            if sub_file.name == "__init__.py":
                continue
            sub_name = sub_file.stem
            full_sub_name = f"{module_name}.{sub_name}"
            if full_sub_name not in sys.modules:
                sub_spec = importlib.util.spec_from_file_location(
                    full_sub_name, str(sub_file)
                )
                if sub_spec:
                    sub_mod = importlib.util.module_from_spec(sub_spec)
                    sys.modules[full_sub_name] = sub_mod
                    try:
                        sub_spec.loader.exec_module(sub_mod)
                    except Exception as e:
                        logger.debug("Failed to load submodule %s: %s", full_sub_name, e)

        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.debug("Failed to exec_module %s: %s", module_name, e)
            sys.modules.pop(module_name, None)
            return None

    # Try register(ctx) pattern first (how our plugins are written)
    if hasattr(mod, "register"):
        collector = _ProviderCollector()
        try:
            mod.register(collector)
            if collector.provider:
                return collector.provider
        except Exception as e:
            logger.debug("register() failed for %s: %s", name, e)

    # Fallback: find a NotifierProvider subclass and instantiate it
    from agent.notifier_provider import NotifierProvider
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name, None)
        if (isinstance(attr, type) and issubclass(attr, NotifierProvider)
                and attr is not NotifierProvider):
            try:
                return attr()
            except Exception:
                pass

    return None


class _ProviderCollector:
    """Fake plugin context that captures register_notifier_provider calls."""

    def __init__(self):
        self.provider = None

    def register_notifier_provider(self, provider):
        self.provider = provider

    # No-op for other registration methods
    def register_tool(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass

    def register_cli_command(self, *args, **kwargs):
        pass  # CLI registration happens via discover_plugin_cli_commands()


def _get_active_notifier_provider() -> Optional[str]:
    """Read the active notifier provider name from config.yaml.

    Returns the provider name (e.g. ``"honcho"``) or None if no
    external provider is configured.  Lightweight — only reads config,
    no plugin loading.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return cfg_get(config, "memory", "provider") or None
    except Exception:
        return None


