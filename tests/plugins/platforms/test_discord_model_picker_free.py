"""Unit tests for the Discord model-picker free-model ordering.

Regression: free models are appended to the END of curated provider lists
(union_with_portal_free_recommendations in hermes_cli/models.py), so a naive
``models[:25]`` slice in the Discord model dropdown silently dropped every
``:free`` model for providers with more than 25 curated entries (nous: 36,
openrouter: 34). Free-first ordering keeps them visible within Discord's
25-option select cap.
"""

from plugins.platforms.discord.adapter import (
    _DISCORD_SELECT_MAX_OPTIONS,
    _is_free_model_id,
    _ordered_discord_model_ids,
)


def test_free_models_ordered_first_within_cap():
    # Simulate nous: 30 paid + 6 free appended at the end.
    paid = [f"vendor/model-{i}" for i in range(30)]
    free = [
        "upstage/solar-pro4:free",
        "meituan/longcat-2.0:free",
        "poolside/laguna-s-2.1:free",
        "stepfun/step-3.7-flash:free",
        "tencent/hy3:free",
        "poolside/laguna-xs-2.1:free",
    ]
    ordered = _ordered_discord_model_ids(paid + free)

    # Every free model survives and lands at the front.
    assert ordered[: len(free)] == free
    # Within the 25-option Discord cap.
    assert len(ordered) <= _DISCORD_SELECT_MAX_OPTIONS
    # Paid tail is truncated but leading paid models still present, order kept.
    assert ordered[0].endswith(":free")
    assert "vendor/model-0" in ordered


def test_fewer_than_cap_returns_all_free_first_preserving_order():
    models = ["a/one", "b/two:free", "c/three", "d/four:free"]
    ordered = _ordered_discord_model_ids(models)
    assert ordered == ["b/two:free", "d/four:free", "a/one", "c/three"]


def test_empty_list_returns_empty():
    assert _ordered_discord_model_ids([]) == []


def test_no_free_models_unchanged_within_cap():
    many = [f"vendor/m{i}" for i in range(40)]
    ordered = _ordered_discord_model_ids(many)
    assert len(ordered) == _DISCORD_SELECT_MAX_OPTIONS
    assert ordered == many[:_DISCORD_SELECT_MAX_OPTIONS]


def test_discord_select_max_options_is_25():
    assert _DISCORD_SELECT_MAX_OPTIONS == 25


def test_is_free_model_id():
    assert _is_free_model_id("upstage/solar-pro4:free")
    assert _is_free_model_id("nvidia/nemotron-3-ultra-550b-a55b:free")
    assert not _is_free_model_id("openai/gpt-5.5")
    assert not _is_free_model_id("")
    assert not _is_free_model_id("vendor/model:paid-tier")
