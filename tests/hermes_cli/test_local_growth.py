"""In-session growth contracts (growth.py + the presets override seam).

The live half of the window ladder: grow before compress, overrides
persist across boots, physics re-checked every boot, growth state dies
with the model."""

from __future__ import annotations

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_overrides_roundtrip_and_clear(hermes_home):
    from hermes_cli.local_runtime.growth import (
        clear_window_override,
        load_window_overrides,
        save_window_override,
    )

    assert load_window_overrides() == {}
    save_window_override("model-a", 98304)
    save_window_override("model-b", 262144)
    assert load_window_overrides() == {"model-a": 98304, "model-b": 262144}
    clear_window_override("model-a")
    assert load_window_overrides() == {"model-b": 262144}
    # Clearing a missing key is a no-op, not an error.
    clear_window_override("never-existed")


def test_corrupt_overrides_read_as_empty(hermes_home):
    from hermes_cli.local_runtime.growth import (
        load_window_overrides,
        window_overrides_path,
    )

    path = window_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_window_overrides() == {}


def test_growth_declines_foreign_endpoints(hermes_home):
    """Only the server THIS process supervises grows — a detected external
    server or another process's endpoint returns None untouched."""
    from hermes_cli.local_runtime.growth import maybe_grow_window

    grown = maybe_grow_window(
        "some-model", base_url="http://127.0.0.1:9999/v1",
        session_tokens=100_000, current_window=65536)
    assert grown is None


def test_occupancy_confirmed_skips_gate_one():
    """The agent's compression gate IS the occupancy signal: when it fired,
    growth must not re-derive its own edge and hold. Decision-table check
    with a synthetic profile."""
    from hermes_cli.local_runtime.context_policy import growth_decision
    from hermes_cli.local_runtime.estimator import (
        HardwareBudget,
        LayerKind,
        ModelProfile,
    )

    gib = 1 << 30
    profile = ModelProfile(
        name="m", weights_bytes=2 * gib, embd_table_bytes=0,
        n_ctx_train=262144,
        layers=[(LayerKind.FULL, 4096)] * 16 + [(LayerKind.RECURRENT, 0)] * 48)
    budget = HardwareBudget(usable_vram_bytes=26 * gib,
                            total_device_bytes=32 * gib,
                            ram_available_bytes=64 * gib)

    # Hermes' threshold (e.g. 80% of window) can sit BELOW the ladder's 85%
    # occupancy gate: 78K of a 96K window is 81%.
    kwargs = dict(current_window=98304, session_tokens=78_000,
                  measured_decode_tok_s=None, server_idle=True)
    ungated = growth_decision(profile, budget, **kwargs)
    assert ungated.action == "hold", "sanity: below the ladder's own gate"

    confirmed = growth_decision(profile, budget, occupancy_confirmed=True, **kwargs)
    assert confirmed.action == "grow"
    assert confirmed.next_window and confirmed.next_window > 98304


def _stage_fake_gguf(mdir, name):
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{name}.gguf").write_bytes(b"GGUF" + b"\x00" * 64)


def _tiny_profile(model_id: str):
    from hermes_cli.local_runtime.estimator import LayerKind, ModelProfile

    gib = 1 << 30
    return ModelProfile(
        name=model_id, weights_bytes=2 * gib, embd_table_bytes=0,
        n_ctx_train=131072,
        layers=[(LayerKind.FULL, 512)] * 4)


def test_preset_restores_grown_window_capped_at_native(hermes_home, tmp_path, monkeypatch):
    """A persisted override lifts the preset window; an absurd override is
    capped at native. GGUF parsing is stubbed — the contract under test is
    the override plumbing, not the reader."""
    import hermes_cli.local_runtime.presets as presets_mod

    from hermes_cli.local_runtime.estimator import HardwareBudget
    from hermes_cli.local_runtime.growth import save_window_override

    mdir = tmp_path / "models"
    _stage_fake_gguf(mdir, "tiny-dense")
    monkeypatch.setattr(presets_mod, "read_gguf_header", lambda p: p)
    monkeypatch.setattr(presets_mod, "profile_from_gguf",
                        lambda h: _tiny_profile("tiny-dense"))

    gib = 1 << 30
    budget = HardwareBudget(usable_vram_bytes=24 * gib,
                            total_device_bytes=24 * gib,
                            ram_available_bytes=64 * gib)
    preset = tmp_path / "presets.ini"

    baseline = presets_mod.generate_presets(mdir, budget, preset)[0]
    assert baseline.window == 131072  # tiny model: native from the start

    # Override above native must cap at native, not exceed it.
    save_window_override("tiny-dense", 10_000_000)
    capped = presets_mod.generate_presets(mdir, budget, preset)[0]
    assert capped.window == 131072


def test_preset_ignores_override_below_launch_window(hermes_home, tmp_path, monkeypatch):
    """Overrides only ever RAISE the window (growth is monotone); a stale
    smaller override never shrinks a launch decision."""
    import hermes_cli.local_runtime.presets as presets_mod

    from hermes_cli.local_runtime.estimator import HardwareBudget
    from hermes_cli.local_runtime.growth import save_window_override

    mdir = tmp_path / "models"
    _stage_fake_gguf(mdir, "tiny-dense")
    monkeypatch.setattr(presets_mod, "read_gguf_header", lambda p: p)
    monkeypatch.setattr(presets_mod, "profile_from_gguf",
                        lambda h: _tiny_profile("tiny-dense"))
    save_window_override("tiny-dense", 65536)

    gib = 1 << 30
    budget = HardwareBudget(usable_vram_bytes=24 * gib,
                            total_device_bytes=24 * gib,
                            ram_available_bytes=64 * gib)
    entry = presets_mod.generate_presets(mdir, budget, tmp_path / "p.ini")[0]
    assert entry.window == 131072


def test_preset_restores_grown_window_midladder(hermes_home, tmp_path, monkeypatch):
    """The real growth shape: launch at a lower rung, override to a middle
    rung -> the preset window follows the override."""
    import hermes_cli.local_runtime.presets as presets_mod

    from hermes_cli.local_runtime.estimator import HardwareBudget, LayerKind, ModelProfile
    from hermes_cli.local_runtime.growth import save_window_override

    gib = 1 << 30
    # Expensive dense KV so the launch decision lands BELOW native on this
    # budget: 60 layers x 4 KiB/tok f16 -> q8 ~= 120 KiB/tok.
    profile = ModelProfile(
        name="big-dense", weights_bytes=20 * gib, embd_table_bytes=0,
        n_ctx_train=262144,
        layers=[(LayerKind.FULL, 4096)] * 60)
    mdir = tmp_path / "models"
    _stage_fake_gguf(mdir, "big-dense")
    monkeypatch.setattr(presets_mod, "read_gguf_header", lambda p: p)
    monkeypatch.setattr(presets_mod, "profile_from_gguf", lambda h: profile)

    budget = HardwareBudget(usable_vram_bytes=28 * gib,
                            total_device_bytes=32 * gib,
                            ram_available_bytes=128 * gib)
    baseline = presets_mod.generate_presets(mdir, budget, tmp_path / "a.ini")[0]
    assert baseline.window < 262144, "sanity: launch below native"

    grown = baseline.window * 2
    save_window_override("big-dense", grown)
    restored = presets_mod.generate_presets(mdir, budget, tmp_path / "b.ini")[0]
    assert restored.window >= grown, "override must lift the launch window"
