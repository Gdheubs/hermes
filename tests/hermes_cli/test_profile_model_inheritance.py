"""Opt-in inheritance of the two primary-routing scalars (``model.default`` +
``model.provider``) from the default root config into a NAMED profile.

Behavioral contract (see ``website/docs/user-guide/profiles.md`` §"Inherit the
root model"):

* A named profile inherits ONLY ``model.default`` and ``model.provider`` from
  ``<root>/config.yaml`` and ONLY when its own config sets
  ``model.inherit_root_primary: true``.
* Default-off and the default/root profile preserve full isolation.
* Root-config create/change/delete is observed WITHOUT restarting a
  long-lived process (cache invalidation).
* A missing/malformed root config fails safe (keeps the profile's own values,
  never broadens inheritance) with a diagnostic.

All model ids/providers here are generic placeholders — the feature must never
pin a real model or provider.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from hermes_cli import config as cfgmod


ROOT_MODEL = "root-model-alpha"
ROOT_PROVIDER = "provider-root"
LOCAL_MODEL = "local-model-beta"
LOCAL_PROVIDER = "provider-local"


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _bump_mtime(path: Path) -> None:
    """Force a stat change even if the write landed in the same mtime tick."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000))


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Point HERMES_HOME at ``<root>/profiles/coder`` so ``get_hermes_home()``
    is the named profile and ``get_default_hermes_root()`` is ``<root>``."""
    root = tmp_path / "root"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    # Defeat any cross-test cache/memo carryover.
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()
    cfgmod._PROFILE_OPT_IN_CACHE.clear()
    yield {
        "root": root,
        "root_cfg": root / "config.yaml",
        "profile": profile,
        "profile_cfg": profile / "config.yaml",
    }
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()
    cfgmod._PROFILE_OPT_IN_CACHE.clear()


def _leaf_paths(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_leaf_paths(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = d
    return out


# ── Core opt-in ────────────────────────────────────────────────────────────

def test_opted_in_named_profile_inherits_root_primary(profile_env):
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    cfg = cfgmod.load_config()

    assert cfg["model"]["default"] == ROOT_MODEL
    assert cfg["model"]["provider"] == ROOT_PROVIDER


def test_default_off_preserves_isolation(profile_env):
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    cfg = cfgmod.load_config()

    assert cfg["model"]["default"] == LOCAL_MODEL
    assert cfg["model"]["provider"] == LOCAL_PROVIDER


def test_default_profile_never_inherits_from_itself(tmp_path, monkeypatch):
    """When HERMES_HOME IS the root, the flag must be a no-op (no self-read)."""
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    cfgmod._LOAD_CONFIG_CACHE.clear()
    _write_yaml(root / "config.yaml", {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER,
                                                 "inherit_root_primary": True}})

    # The root config path resolves to the current config path → helper opts out.
    assert cfgmod._root_config_path_for_inheritance(root / "config.yaml") is None
    cfg = cfgmod.load_config()
    assert cfg["model"]["default"] == ROOT_MODEL  # its own value, not an inherited copy
    cfgmod._LOAD_CONFIG_CACHE.clear()


def test_flag_present_model_fields_omitted_still_inherits(profile_env):
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}, "toolsets": ["hermes-cli"]})

    cfg = cfgmod.load_config()

    assert cfg["model"]["default"] == ROOT_MODEL
    assert cfg["model"]["provider"] == ROOT_PROVIDER


# ── Migration-safe rule ────────────────────────────────────────────────────

def test_legacy_local_fields_root_wins(profile_env):
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"],
                {"model": {"inherit_root_primary": True, "default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    cfg = cfgmod.load_config()

    assert cfg["model"]["default"] == ROOT_MODEL
    assert cfg["model"]["provider"] == ROOT_PROVIDER


def test_root_missing_one_field_falls_back_to_local(profile_env):
    # Root sets only provider; profile keeps a local default.
    _write_yaml(profile_env["root_cfg"], {"model": {"provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"],
                {"model": {"inherit_root_primary": True, "default": LOCAL_MODEL}})

    cfg = cfgmod.load_config()

    assert cfg["model"]["provider"] == ROOT_PROVIDER   # inherited
    assert cfg["model"]["default"] == LOCAL_MODEL       # migration-safe fallback


# ── Isolation: everything else stays profile-local/default ──────────────────

def test_only_two_model_fields_are_inherited(profile_env):
    root_cfg = {
        "model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER,
                  "base_url": "https://root.example/v1", "api_key": "root-secret"},
        "providers": {"rootprov": {"api_key": "root-key"}},
        "toolsets": ["root-tool"],
        "terminal": {"backend": "docker"},
        "aliases": {"r": "root"},
    }
    _write_yaml(profile_env["root_cfg"], root_cfg)

    base_profile = {"model": {"provider": LOCAL_PROVIDER, "base_url": "https://local.example/v1"}}
    # Baseline: flag OFF.
    _write_yaml(profile_env["profile_cfg"], base_profile)
    off = cfgmod.load_config()
    cfgmod._LOAD_CONFIG_CACHE.clear()
    # Opted-in: identical profile + flag ON.
    on_profile = {"model": dict(base_profile["model"], inherit_root_primary=True)}
    _write_yaml(profile_env["profile_cfg"], on_profile)
    on = cfgmod.load_config()

    off_leaves = _leaf_paths(off)
    on_leaves = _leaf_paths(on)
    changed = {k for k in set(off_leaves) | set(on_leaves) if off_leaves.get(k) != on_leaves.get(k)}
    changed.discard("model.inherit_root_primary")  # the flag itself differs by construction
    assert changed == {"model.default", "model.provider"}, changed

    # Spot-check specific non-inherited leaves.
    assert on["model"].get("base_url") == "https://local.example/v1"  # NOT root's
    assert "api_key" not in on["model"] or on["model"].get("api_key") != "root-secret"
    assert on.get("providers", {}).get("rootprov") is None
    assert "root-tool" not in (on.get("toolsets") or [])


def test_no_non_primary_state_is_inherited(profile_env):
    """Regression: a rich root config must NOT leak ANY non-primary state into
    an opted-in profile — not credentials, providers, aliases, delegation
    routing, model.base_url/api_key, terminal, tools, memory, skills, sessions,
    cron, logs, or gateway state. Only model.default + model.provider cross."""
    root_cfg = {
        "model": {
            "default": ROOT_MODEL, "provider": ROOT_PROVIDER,
            "base_url": "https://root.example/v1", "api_key": "root-secret",
            "delegation": {"default": "root-deleg-model", "provider": "root-deleg-prov"},
        },
        "providers": {"rootprov": {"api_key": "root-key", "base_url": "https://rp.example"}},
        "aliases": {"r": "root"},
        "terminal": {"backend": "docker"},
        "toolsets": ["root-tool"],
        "memory": {"enabled": True, "path": "/root/mem"},
        "skills": {"official": ["root-skill"]},
        "sessions": {"retention_days": 99},
        "cron": {"model": "root-cron-model"},
        "logs": {"level": "root-debug"},
        "gateway": {"port": 61234},
    }
    _write_yaml(profile_env["root_cfg"], root_cfg)
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    on = cfgmod.load_config()

    # The two inherited scalars cross.
    assert on["model"]["default"] == ROOT_MODEL
    assert on["model"]["provider"] == ROOT_PROVIDER
    # Nothing else does. Delegation stays default/local (never root's).
    assert on["model"].get("delegation", {}).get("provider") != "root-deleg-prov"
    assert on["model"].get("base_url") != "https://root.example/v1"
    assert on["model"].get("api_key") != "root-secret"
    # Whole non-model sections never take root's values.
    assert on.get("providers", {}).get("rootprov") is None
    assert on.get("aliases", {}).get("r") != "root"
    assert on.get("terminal", {}).get("backend") != "docker"
    assert "root-tool" not in (on.get("toolsets") or [])
    assert on.get("memory", {}).get("path") != "/root/mem"
    assert "root-skill" not in (on.get("skills", {}).get("official") or [])
    assert on.get("sessions", {}).get("retention_days") != 99
    assert on.get("cron", {}).get("model") != "root-cron-model"
    assert on.get("logs", {}).get("level") != "root-debug"
    assert on.get("gateway", {}).get("port") != 61234


# ── Fail-safe ───────────────────────────────────────────────────────────────

def test_malformed_root_fails_safe(profile_env, caplog):
    profile_env["root_cfg"].parent.mkdir(parents=True, exist_ok=True)
    profile_env["root_cfg"].write_text("model: {default: [unbalanced\n", encoding="utf-8")
    _write_yaml(profile_env["profile_cfg"],
                {"model": {"inherit_root_primary": True, "default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    with caplog.at_level("WARNING"):
        cfg = cfgmod.load_config()

    assert cfg["model"]["default"] == LOCAL_MODEL   # unchanged — no broadening
    assert cfg["model"]["provider"] == LOCAL_PROVIDER
    assert any("root" in r.message.lower() for r in caplog.records)


def test_missing_root_fails_safe(profile_env):
    # No root config.yaml at all.
    _write_yaml(profile_env["profile_cfg"],
                {"model": {"inherit_root_primary": True, "default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    cfg = cfgmod.load_config()

    assert cfg["model"]["default"] == LOCAL_MODEL
    assert cfg["model"]["provider"] == LOCAL_PROVIDER


# ── Cache: parent-aware invalidation in a long-lived process ────────────────

def test_root_change_invalidates_cached_profile_result(profile_env):
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    first = cfgmod.load_config()
    assert first["model"]["default"] == ROOT_MODEL

    # Root-only edit (profile file untouched) — must be observed with NO restart
    # and NO cache clear.
    _write_yaml(profile_env["root_cfg"], {"model": {"default": "root-model-changed", "provider": ROOT_PROVIDER}})
    _bump_mtime(profile_env["root_cfg"])

    second = cfgmod.load_config()
    assert second["model"]["default"] == "root-model-changed"


def test_root_create_and_delete_invalidate(profile_env):
    _write_yaml(profile_env["profile_cfg"],
                {"model": {"inherit_root_primary": True, "default": LOCAL_MODEL}})

    # No root yet → keeps local.
    assert cfgmod.load_config()["model"]["default"] == LOCAL_MODEL

    # Create root → inherited.
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _bump_mtime(profile_env["root_cfg"])
    assert cfgmod.load_config()["model"]["default"] == ROOT_MODEL

    # Delete root → back to local (no stale inherited value).
    profile_env["root_cfg"].unlink()
    assert cfgmod.load_config()["model"]["default"] == LOCAL_MODEL


# ── Cache: a NON-opted profile stays isolated from root churn ───────────────

def test_non_opted_profile_cache_key_excludes_root_stat(profile_env):
    """A non-opted named profile must NOT fold the root config's stat into its
    load-cache signature. Otherwise every root create/change/delete would
    needlessly invalidate a profile whose effective config never depends on root
    (issue #43713 isolation)."""
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    cfgmod.load_config()

    cached = cfgmod._LOAD_CONFIG_CACHE[str(profile_env["profile_cfg"])]
    # cache tuple: (user_mtime, user_size, managed_mtime, managed_size,
    #               root_mtime, root_size, cfg, env_snap)
    assert cached[4:6] == (0, 0)  # root stat NOT folded into a non-opted key


def test_non_opted_profile_isolated_from_root_churn_long_lived(profile_env):
    """Long-lived negative regression: within ONE process (no cache clear, no
    restart), a root-config change must NOT invalidate a non-opted profile's
    cached result — its cache signature is untouched and its model stays local.

    The opted-in counterpart (``test_root_change_invalidates_cached_profile_result``)
    guarantees the inverse, so the two together pin the exact boundary."""
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    first = cfgmod.load_config()
    assert first["model"]["default"] == LOCAL_MODEL
    path_key = str(profile_env["profile_cfg"])
    sig_before = cfgmod._LOAD_CONFIG_CACHE[path_key][:6]

    # Root-only edit (profile file untouched); same process, NO cache clear.
    _write_yaml(profile_env["root_cfg"], {"model": {"default": "root-model-changed", "provider": ROOT_PROVIDER}})
    _bump_mtime(profile_env["root_cfg"])

    second = cfgmod.load_config()
    sig_after = cfgmod._LOAD_CONFIG_CACHE[path_key][:6]

    assert sig_before == sig_after                    # root churn did NOT perturb the key
    assert second["model"]["default"] == LOCAL_MODEL  # still fully isolated


# ── Direct helper unit ──────────────────────────────────────────────────────

def test_helper_is_noop_without_flag(profile_env):
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    cfg = {"model": {"default": LOCAL_MODEL}}
    out = cfgmod.apply_root_primary_model_inheritance(cfg, config_path=profile_env["profile_cfg"])
    assert out["model"]["default"] == LOCAL_MODEL


def test_helper_overlays_two_fields_when_opted_in(profile_env):
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    cfg = {"model": {"inherit_root_primary": True}}
    out = cfgmod.apply_root_primary_model_inheritance(cfg, config_path=profile_env["profile_cfg"])
    assert out["model"]["default"] == ROOT_MODEL
    assert out["model"]["provider"] == ROOT_PROVIDER


# ── Consumer parity: `profile list` (profiles._read_config_model) ───────────

def test_profile_list_reflects_inherited_model(tmp_path, monkeypatch):
    """The former raw bypass now agrees with the canonical resolver: an opted-in
    profile's listed model/provider reflects the inherited root values."""
    from hermes_cli import profiles as profmod

    root = tmp_path / "root"
    coder = root / "profiles" / "coder"
    plain = root / "profiles" / "plain"
    coder.mkdir(parents=True, exist_ok=True)
    plain.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))  # listing runs from the root install
    cfgmod._LOAD_CONFIG_CACHE.clear()

    _write_yaml(root / "config.yaml", {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(coder / "config.yaml", {"model": {"inherit_root_primary": True}})
    _write_yaml(plain / "config.yaml", {"model": {"default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})

    assert profmod._read_config_model(coder) == (ROOT_MODEL, ROOT_PROVIDER)   # inherited
    assert profmod._read_config_model(plain) == (LOCAL_MODEL, LOCAL_PROVIDER)  # isolated
    cfgmod._LOAD_CONFIG_CACHE.clear()


# ── Cache: opt-in flag discovery must not re-parse the profile on cache hits ─
#
# The load-config cache exists to avoid re-parsing config.yaml on every call.
# An opted-in named profile still has to learn the opt-in flag BEFORE the
# cache-hit check (the flag decides whether the root stat is folded into the
# signature). Discovering it must NOT cost an uncached profile YAML parse on a
# cache hit — otherwise the cache is defeated for every opted-in profile. These
# tests count the on-disk flag reads across cold load / cache hit / profile flag
# change / root change (the four scenarios the fix must prove).

def _spy_on_disk_flag_reads(monkeypatch):
    """Count calls to the on-disk opt-in probe (the only 'extra parse solely to
    discover the flag'). Returns a mutable counter dict."""
    calls = {"n": 0}
    orig = cfgmod._profile_opts_into_root_primary_on_disk

    def _counting(config_path):
        calls["n"] += 1
        return orig(config_path)

    monkeypatch.setattr(cfgmod, "_profile_opts_into_root_primary_on_disk", _counting)
    return calls


def test_opted_in_cache_hit_avoids_extra_flag_parse(profile_env, monkeypatch):
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})
    calls = _spy_on_disk_flag_reads(monkeypatch)

    first = cfgmod.load_config()             # cold load
    assert first["model"]["default"] == ROOT_MODEL
    assert calls["n"] == 1                    # one parse to discover the flag

    second = cfgmod.load_config()            # cache hit — nothing changed
    assert second["model"]["default"] == ROOT_MODEL
    assert calls["n"] == 1                    # NO extra parse on the cache hit


def test_opted_in_flag_toggle_reparses_without_stale_opt_in(profile_env, monkeypatch):
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})
    calls = _spy_on_disk_flag_reads(monkeypatch)

    assert cfgmod.load_config()["model"]["default"] == ROOT_MODEL
    cfgmod.load_config()                      # cache hit
    assert calls["n"] == 1

    # Toggle the flag OFF (and keep a local default). Changing the profile file
    # changes its (mtime, size) → the opt-in must be re-derived, not stale.
    _write_yaml(profile_env["profile_cfg"],
                {"model": {"inherit_root_primary": False, "default": LOCAL_MODEL, "provider": LOCAL_PROVIDER}})
    _bump_mtime(profile_env["profile_cfg"])

    cfg = cfgmod.load_config()
    assert calls["n"] == 2                     # re-parsed after the profile changed
    assert cfg["model"]["default"] == LOCAL_MODEL   # no stale opt-in → local wins now


def test_root_change_needs_no_flag_reparse_yet_reloads(profile_env, monkeypatch):
    _write_yaml(profile_env["root_cfg"], {"model": {"default": ROOT_MODEL, "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})
    calls = _spy_on_disk_flag_reads(monkeypatch)

    assert cfgmod.load_config()["model"]["default"] == ROOT_MODEL
    assert calls["n"] == 1

    # Root-only edit (profile file untouched). The opt-in is unchanged, so it
    # must NOT be re-parsed — but dynamic root-stat invalidation must still fire.
    _write_yaml(profile_env["root_cfg"], {"model": {"default": "root-model-changed", "provider": ROOT_PROVIDER}})
    _bump_mtime(profile_env["root_cfg"])

    cfg = cfgmod.load_config()
    assert calls["n"] == 1                     # profile flag NOT re-parsed on a root-only change
    assert cfg["model"]["default"] == "root-model-changed"   # dynamic root invalidation preserved


# ── Env expansion of inherited primary values (parity / consistency / security)

def test_resolver_expands_inherited_env_ref(profile_env, monkeypatch):
    """The single shared resolver every consumer routes through must return
    ENV-EXPANDED inherited values, so CLI / gateway / doctor / profile-list /
    cron all agree regardless of when they expand."""
    monkeypatch.setenv("HERMES_TEST_RESOLVER_MODEL", "resolver-expanded-model")
    _write_yaml(profile_env["root_cfg"],
                {"model": {"default": "${HERMES_TEST_RESOLVER_MODEL}", "provider": ROOT_PROVIDER}})
    cfg = {"model": {"inherit_root_primary": True}}

    out = cfgmod.apply_root_primary_model_inheritance(cfg, config_path=profile_env["profile_cfg"])

    assert out["model"]["default"] == "resolver-expanded-model"
    assert out["model"]["provider"] == ROOT_PROVIDER


def test_inherited_env_ref_consistent_normal_load_vs_cron(profile_env, monkeypatch):
    """``load_config`` (normal load) and ``build_cron_effective_config`` (a
    canonical consumer) must resolve an inherited ``${VAR}`` to the SAME value."""
    monkeypatch.setenv("HERMES_TEST_CONSUMER_MODEL", "consumer-parity-model")
    _write_yaml(profile_env["root_cfg"],
                {"model": {"default": "${HERMES_TEST_CONSUMER_MODEL}", "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    from_load = cfgmod.load_config()["model"]["default"]
    from_cron = cfgmod.build_cron_effective_config(profile_env["profile_cfg"])["model"]["default"]

    assert from_load == from_cron == "consumer-parity-model"


def test_inherited_env_ref_missing_stays_verbatim_like_local(profile_env, monkeypatch):
    """A missing ``${VAR}`` in an inherited value is kept verbatim — identical to
    how ``_expand_env_vars`` treats a missing ref in a profile-local value."""
    monkeypatch.delenv("HERMES_TEST_MISSING_MODEL", raising=False)

    # Local baseline: a non-opted profile with the same missing ref.
    _write_yaml(profile_env["profile_cfg"], {"model": {"default": "${HERMES_TEST_MISSING_MODEL}"}})
    local_default = cfgmod.load_config()["model"]["default"]
    cfgmod._LOAD_CONFIG_CACHE.clear()

    # Inherited: opted-in profile, root supplies the missing ref.
    _write_yaml(profile_env["root_cfg"],
                {"model": {"default": "${HERMES_TEST_MISSING_MODEL}", "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})
    _bump_mtime(profile_env["profile_cfg"])
    inherited_default = cfgmod.load_config()["model"]["default"]

    assert inherited_default == local_default == "${HERMES_TEST_MISSING_MODEL}"


def test_only_primary_root_env_refs_are_expanded_or_merged(profile_env, monkeypatch):
    """Security/isolation: only ``model.default`` + ``model.provider`` are read,
    expanded and merged from root. A ``${VAR}`` in ANY non-primary root field is
    never expanded and never crosses into the profile."""
    monkeypatch.setenv("HERMES_TEST_PRIMARY", "primary-expanded")
    monkeypatch.setenv("HERMES_TEST_SECRET", "secret-must-not-leak")
    _write_yaml(profile_env["root_cfg"], {
        "model": {
            "default": "${HERMES_TEST_PRIMARY}", "provider": ROOT_PROVIDER,
            "api_key": "${HERMES_TEST_SECRET}",
        },
        "providers": {"rootprov": {"api_key": "${HERMES_TEST_SECRET}"}},
    })
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    cfg = cfgmod.load_config()

    assert cfg["model"]["default"] == "primary-expanded"       # primary IS expanded
    # Root's non-primary fields never merge in — neither raw nor expanded.
    assert cfg["model"].get("api_key") not in ("secret-must-not-leak", "${HERMES_TEST_SECRET}")
    assert cfg.get("providers", {}).get("rootprov") is None
    # The secret's expansion appears NOWHERE (root non-primary never expanded).
    assert all(v != "secret-must-not-leak" for v in _leaf_paths(cfg).values())


def test_inherited_env_ref_rotation_invalidates_cache(profile_env, monkeypatch):
    """Matching existing env semantics (#58514): an in-process rotation of a
    ``${VAR}`` referenced by the inherited root value invalidates the cached
    profile result WITHOUT a file change or cache clear."""
    monkeypatch.setenv("HERMES_TEST_ROT_MODEL", "rotation-value-A")
    _write_yaml(profile_env["root_cfg"],
                {"model": {"default": "${HERMES_TEST_ROT_MODEL}", "provider": ROOT_PROVIDER}})
    _write_yaml(profile_env["profile_cfg"], {"model": {"inherit_root_primary": True}})

    first = cfgmod.load_config()
    assert first["model"]["default"] == "rotation-value-A"

    monkeypatch.setenv("HERMES_TEST_ROT_MODEL", "rotation-value-B")  # rotate in-process
    second = cfgmod.load_config()
    assert second["model"]["default"] == "rotation-value-B"
