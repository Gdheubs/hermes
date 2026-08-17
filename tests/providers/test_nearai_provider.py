"""NEAR AI Cloud provider profile.

The generic suites in this directory enumerate every registered profile, so the
offline tests here cover only what is specific to nearai: the alias set, the
catalog wiring, and the ``_is_chat_model`` filter that keeps NEAR AI's non-chat
products (image generator, ASR, embedding, reranker, prompt filter) out of the
``hermes model`` picker.

Claims that can only be settled against the live catalog — is the curated list
still spelled correctly, is the aux model still multimodal — are asserted in the
``integration``-marked tests at the bottom. Those are excluded from the default
run by ``addopts = -m 'not integration'``; pin them with
``pytest -m integration tests/providers/test_nearai_provider.py``.

NB: a hand-installed copy of this plugin under ``~/.hermes/plugins`` shadows the
repo one in the live registry, so the filter tests import the profile straight
from its file rather than via ``get_provider_profile``.
"""

import importlib.util
from pathlib import Path

import pytest

from hermes_cli.provider_catalog import provider_catalog_by_slug
from providers import get_provider_profile

_PLUGIN = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "model-providers" / "nearai" / "__init__.py"
)


def _repo_module():
    """Load the repo's plugin file directly, bypassing the plugin registry."""
    spec = importlib.util.spec_from_file_location("_repo_nearai_under_test", _PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _profile():
    return get_provider_profile("nearai")


# ── Registration / wiring ────────────────────────────────────────────────


def test_registered_and_aliases_resolve():
    for alias in ("nearai", "near-ai", "near"):
        assert get_provider_profile(alias).name == "nearai"


def test_base_url_and_env_vars():
    p = _profile()
    assert p.base_url == "https://cloud-api.near.ai/v1"
    assert p.env_vars[0] == "NEAR_AI_API_KEY"
    assert p.get_hostname() == "cloud-api.near.ai"


def test_catalog_splits_base_url_off_the_credential_list():
    """``NEAR_AI_BASE_URL`` must not be prompted for as a credential.

    Guards the deliberate choice to keep the override in ``env_vars``: dropping
    it would silently disable the override, and moving it into
    ``api_key_env_vars`` would ask users for a second "key" they don't have.
    """
    d = provider_catalog_by_slug()["nearai"]
    assert d.label == "NEAR AI"
    assert d.tab == "keys"
    assert d.api_key_env_vars == ("NEAR_AI_API_KEY",)
    assert d.base_url_env_var == "NEAR_AI_BASE_URL"


# ── The non-chat filter ──────────────────────────────────────────────────
#
# Fixtures are trimmed copies of real /v1/models entries, so a catalog shape
# change shows up here rather than as a diffusion model in the picker.


def _entry(mid, inputs=("text",), outputs=("text",), ctx=131072):
    return {
        "id": mid,
        "input_modalities": list(inputs),
        "output_modalities": list(outputs),
        "context_length": ctx,
    }


@pytest.mark.parametrize(
    "entry",
    [
        _entry("google/gemini-2.5-flash", ("text", "image"), ctx=1000000),
        _entry("anthropic/claude-sonnet-5", ("text", "image"), ctx=1000000),
        _entry("openai/gpt-oss-120b"),
        _entry("qwen/qwen3-32b", ctx=128000),
    ],
    ids=lambda e: e["id"],
)
def test_chat_models_are_kept(entry):
    assert _repo_module()._is_chat_model(entry) is True


@pytest.mark.parametrize(
    "entry",
    [
        # image generator — text in, image out
        _entry("black-forest-labs/FLUX.2-klein-4B", outputs=("image",), ctx=128000),
        # ASR — audio in, no text input
        _entry("openai/whisper-large-v3", ("audio",), ctx=448),
        # embedding — text in, vector out
        _entry("Qwen/Qwen3-Embedding-0.6B", outputs=("embedding",), ctx=32768),
        # prompt filter — text/text but a 512-token window
        _entry("openai/privacy-filter", ctx=512),
        # reranker — indistinguishable by metadata, caught by id
        _entry("Qwen/Qwen3-Reranker-0.6B", ctx=40960),
    ],
    ids=lambda e: e["id"],
)
def test_non_chat_models_are_dropped(entry):
    assert _repo_module()._is_chat_model(entry) is False


def test_filter_tolerates_missing_metadata():
    """A catalog entry with no modality fields must not crash discovery."""
    assert _repo_module()._is_chat_model({"id": "x/y"}) is False


def test_curated_models_lead_the_picker():
    """``fallback_models`` is merged curated-first for nearai, so its order is
    the picker's front page — not a list that only shows up on fetch failure.
    """
    from hermes_cli.models import _LIVE_FIRST_PICKER_PROVIDERS

    assert "nearai" not in _LIVE_FIRST_PICKER_PROVIDERS
    assert _profile().fallback_models[0] == "anthropic/claude-sonnet-5"


# ── Live catalog (integration) ───────────────────────────────────────────


@pytest.mark.integration
def test_curated_ids_still_exist_upstream():
    """Every curated id must be live and spelled exactly — ids are
    case-sensitive and a retired one would sit dead at the top of the picker.
    """
    p = _profile()
    live = set(_repo_module().nearai.fetch_models() or ())
    assert live, "live catalog fetch returned nothing"
    assert not [m for m in p.fallback_models if m not in live]


@pytest.mark.integration
def test_aux_model_is_live_and_multimodal():
    """``resolve_provider_client`` fills an unset model from ``default_aux_model``
    on the vision path, so a text-only pick ships images to a blind model.
    """
    import json
    import urllib.request

    with urllib.request.urlopen(
        "https://cloud-api.near.ai/v1/models", timeout=20
    ) as resp:
        catalog = {m["id"]: m for m in json.loads(resp.read().decode())["data"]}

    aux = _profile().default_aux_model
    assert aux in catalog, f"{aux} is no longer in the catalog"
    assert "image" in catalog[aux]["input_modalities"]
    assert catalog[aux]["context_length"] >= 1_000_000


@pytest.mark.integration
def test_fetch_models_excludes_non_chat_products():
    live = _repo_module().nearai.fetch_models()
    assert live
    assert not [
        m
        for m in live
        if any(
            k in m.lower()
            for k in ("embedding", "reranker", "flux", "whisper", "privacy-filter")
        )
    ]
