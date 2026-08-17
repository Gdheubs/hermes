"""Unit tests for the Z.AI / GLM provider profile's reasoning wiring.

Z.AI's GLM-4.5-and-later chat models default to thinking-mode ON when the
request omits ``thinking``.  Before the profile emitted the parameter,
``reasoning_config = {"enabled": False}`` was a silent no-op on the direct
Z.AI route — users who turned thinking off kept burning thinking tokens on
every turn (the desktop "thinking reverts to medium" report).

GLM-5.2 additionally exposes a native ``reasoning_effort`` knob with seven
levels (max, xhigh, high, medium, low, minimal, none) on the OpenAI-
compatible ``/api/paas/v4`` endpoint; Hermes passes these through directly.

GLM-5.3 (2026-08-14) keeps those seven levels but always thinks: the
``thinking`` on/off toggle is a silent no-op on the Coding Plan endpoint
and a hard error (HTTP 400, code 1210) on the PaaS endpoint, so the profile
must never emit it for GLM >= 5.3.  ``{"enabled": False}`` maps to
``reasoning_effort: "minimal"`` — the lowest effort the model honors
(``"none"`` behaves like the server default, not like "off").

These tests pin the profile's wire-shape contract so Z.AI requests stay
correctly shaped without going live.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def zai_profile():
    """Resolve the registered Z.AI profile through the real discovery path."""
    # ``model_tools`` triggers plugin discovery on import, which is what
    # registers the Z.AI profile in the global provider registry.
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("zai")
    assert profile is not None, "zai provider profile must be registered"
    return profile


class TestZaiThinkingWireShape:
    """``build_api_kwargs_extras`` produces Z.AI's exact wire format."""

    def test_no_preference_omits_thinking(self, zai_profile):
        """No reasoning_config → omit ``thinking`` so the server default
        applies (matches prior behavior for users with no preference)."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config=None, model="glm-5"
        )
        assert extra_body == {}
        assert top_level == {}

    def test_enabled_sends_enabled_marker(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"}, model="glm-5"
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {}

    def test_explicitly_disabled_sends_disabled_marker(self, zai_profile):
        """``reasoning_config.enabled=False`` → ``thinking.type=disabled``.

        The crucial bit is that the parameter is *sent* at all — GLM defaults
        to thinking-on when ``thinking`` is absent, so an unsent disable
        burns thinking tokens forever.
        """
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="glm-5"
        )
        assert extra_body == {"thinking": {"type": "disabled"}}
        assert top_level == {}


class TestZaiGLM52ReasoningEffort:
    """GLM-5.2's native ``reasoning_effort`` knob (seven pass-through levels)."""

    def test_high_maps_to_high(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("effort", ["low", "medium", "minimal"])
    def test_lower_efforts_pass_through(self, zai_profile, effort):
        """GLM-5.2 supports low/medium/minimal natively — pass through
        unchanged instead of clamping up to high."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": effort}

    def test_xhigh_maps_to_xhigh(self, zai_profile):
        """xhigh is a native GLM-5.2 level — pass through, don't clamp to max."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "xhigh"}

    def test_max_maps_to_max(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "max"}

    def test_ultra_maps_to_max(self, zai_profile):
        """ultra is Hermes-only; GLM-5.2's top tier is max."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "ultra"},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "max"}

    def test_disabled_sends_no_effort(self, zai_profile):
        """Disabled reasoning still sends the thinking-off marker but never
        an effort level."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "high"},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "disabled"}}
        assert top_level == {}


    @pytest.mark.parametrize(
        "model",
        [
            "z-ai/glm-5.2",
            "glm-5-2",
            "glm-5p2",
            "accounts/fireworks/models/glm-5p2",
            "zai-org-glm-5-2",
        ],
    )
    def test_alias_spellings_recognized(self, zai_profile, model):
        _, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"},
            model=model,
        )
        assert top_level == {"reasoning_effort": "max"}

    @pytest.mark.parametrize(
        "model",
        ["glm-5.1", "glm-5", "glm-4.7", "glm-4-9b", "", None],
    )
    def test_non_glm_5_2_models_get_no_effort(self, zai_profile, model):
        _, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model=model,
        )
        assert top_level == {}


class TestZaiGLM53ReasoningEffort:
    """GLM-5.3: effort-only wiring, never a ``thinking`` toggle.

    Verified against the live Z.AI API on 2026-08-14: ``thinking:
    disabled`` is ignored on the Coding Plan endpoint and rejected with
    400/1210 on the PaaS endpoint; accepted effort levels are
    ``none, minimal, low, medium, high, xhigh, max`` (``light`` -> 1210).
    """

    @pytest.mark.parametrize(
        "effort", ["minimal", "low", "medium", "high", "xhigh", "max"]
    )
    def test_native_levels_pass_through(self, zai_profile, effort):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="glm-5.3",
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": effort}

    def test_disabled_maps_to_minimal(self, zai_profile):
        """GLM-5.3 always thinks — Hermes "off" becomes the real floor."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="glm-5.3",
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "minimal"}

    def test_disabled_with_effort_still_maps_to_minimal(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "high"},
            model="glm-5.3",
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "minimal"}

    def test_ultra_maps_to_max(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "ultra"},
            model="glm-5.3",
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "max"}

    def test_no_preference_omits_everything(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config=None,
            model="glm-5.3",
        )
        assert extra_body == {}
        assert top_level == {}

    @pytest.mark.parametrize(
        "model",
        [
            "glm-5.3",
            "glm-5-3",
            "glm-5p3",
            "z-ai/glm-5.3",
            "GLM-5.3",  # case-insensitive
            "glm-5.4",  # future versions take the same path
        ],
    )
    def test_alias_spellings_and_future_versions(self, zai_profile, model):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model=model,
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "high"}

    @pytest.mark.parametrize(
        "model",
        ["glm-5.2", "glm-5.1", "glm-5", "glm-4.7", "glm-4-9b"],
    )
    def test_older_models_keep_thinking_toggle(self, zai_profile, model):
        """Only >= 5.3 loses the toggle; 4.5–5.2 behavior is unchanged."""
        extra_body, _ = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model=model,
        )
        expected = {} if model in {"glm-4-9b"} else {"thinking": {"type": "disabled"}}
        assert extra_body == expected


class TestZaiModelGating:
    """GLM 4.5+ get thinking; earlier GLM models are left untouched."""

    @pytest.mark.parametrize(
        "model",
        [
            "glm-4.5",
            "glm-4.5-air",
            "glm-4.5-flash",
            "glm-4.6",
            "glm-5",
            "glm-5.2",
            "GLM-5",  # case-insensitive
        ],
    )
    def test_thinking_capable_models_emit_thinking(self, zai_profile, model):
        extra_body, _ = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model=model
        )
        assert extra_body == {"thinking": {"type": "disabled"}}


class TestZaiFullKwargsIntegration:
    """End-to-end: the transport's full kwargs carry the reasoning wiring."""


    def test_glm_5_2_effort_reaches_top_level(self, zai_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="glm-5.2",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=zai_profile,
            reasoning_config={"enabled": True, "effort": "max"},
            base_url="https://api.z.ai/api/paas/v4",
            provider_name="zai",
        )
        assert kwargs["reasoning_effort"] == "max"
        assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}

    def test_glm_5_3_effort_reaches_top_level_without_thinking(self, zai_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="glm-5.3",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=zai_profile,
            reasoning_config={"enabled": False},
            base_url="https://api.z.ai/api/coding/paas/v4",
            provider_name="zai",
        )
        assert kwargs["reasoning_effort"] == "minimal"
        assert "thinking" not in kwargs.get("extra_body", {})
