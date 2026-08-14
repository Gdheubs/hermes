"""The llamacpp provider row in the model picker payload (Rollout 4).

Contract: staged local GGUFs appear as a selectable provider row in
build_models_payload — the same payload /api/model/options and the desktop
picker consume — whenever models are staged, without any credential."""

from __future__ import annotations

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _stage(home, *names):
    mdir = home / "models"
    mdir.mkdir(exist_ok=True)
    for name in names:
        (mdir / f"{name}.gguf").write_bytes(b"GGUF" + b"\x00" * 64)


def test_no_staged_models_no_row(hermes_home):
    from hermes_cli.inventory import _local_runtime_row, load_picker_context

    assert _local_runtime_row(load_picker_context()) is None


def test_staged_models_make_a_selectable_row(hermes_home):
    from hermes_cli.inventory import _local_runtime_row, load_picker_context

    _stage(hermes_home, "Qwen3-4B-Instruct-2507-UD-Q8_K_XL", "Some-Other-Model")
    row = _local_runtime_row(load_picker_context())
    assert row is not None
    assert row["slug"] == "llamacpp"
    assert row["authenticated"] is True
    assert "Qwen3-4B-Instruct-2507-UD-Q8_K_XL" in row["models"]
    assert row["total_models"] == 2


def test_row_marks_current_when_config_points_at_llamacpp(hermes_home):
    from hermes_cli.inventory import _local_runtime_row, load_picker_context

    _stage(hermes_home, "M")
    ctx = load_picker_context().with_overrides(current_provider="llamacpp")
    row = _local_runtime_row(ctx)
    assert row is not None and row["is_current"] is True


def test_full_payload_includes_local_row(hermes_home):
    """Through the REAL payload builder — the shape the desktop picker eats."""
    from hermes_cli.inventory import build_models_payload, load_picker_context

    _stage(hermes_home, "Local-Model-X")
    payload = build_models_payload(
        load_picker_context(),
        probe_custom_providers=False,
        probe_current_custom_provider=False,
    )
    slugs = [p["slug"] for p in payload["providers"]]
    assert "llamacpp" in slugs
    row = payload["providers"][slugs.index("llamacpp")]
    assert row["models"] == ["Local-Model-X"]
