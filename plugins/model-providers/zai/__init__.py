"""ZAI / GLM provider profile.

Z.AI's GLM-4.5-and-later chat models default to thinking-mode ON when the
request omits ``thinking``.  Hermes' ``reasoning_config = {"enabled": False}``
was previously a silent no-op on this route — the base profile emits nothing,
so users who turned thinking off (desktop toggle, ``/reasoning none``,
``reasoning_effort: none``/``false`` in config.yaml) kept burning thinking
tokens on every turn.

:meth:`ZaiProfile.build_api_kwargs_extras` translates the Hermes reasoning
config into the wire shape Z.AI's OpenAI-compat endpoint expects:

    {"extra_body": {"thinking": {"type": "enabled" | "disabled"}}}

When no reasoning preference is set (``reasoning_config is None``) the field
is omitted so the server default applies, matching prior behavior.  GLM
models before 4.5 (e.g. ``glm-4-9b``) don't accept ``thinking`` and are left
untouched.

GLM-5.2 additionally exposes a native ``reasoning_effort`` knob with seven
levels — ``max``, ``xhigh``, ``high``, ``medium``, ``low``, ``minimal``,
``none`` — on the OpenAI-compatible endpoint (per Z.AI / BigModel docs).
Hermes passes these through directly so the user's effort preference
reaches the model unmodified.

GLM-5.3 (released 2026-08-14) keeps the same seven ``reasoning_effort``
levels but drops the on/off toggle semantics: the model *always* thinks.
``thinking: {"type": "disabled"}`` is silently ignored on the Coding Plan
endpoint (``/api/coding/paas/v4`` — the response still carries a full
reasoning trace) and rejected with HTTP 400 (error 1210) on the standard
PaaS endpoint (``/api/paas/v4``).  The profile therefore never emits the
``thinking`` toggle for GLM >= 5.3 and instead maps Hermes'
``{"enabled": False}`` onto ``reasoning_effort: "minimal"`` — the lowest
effort the model actually honors (``"none"`` is accepted but behaves like
the server default, not like "off").  All other Hermes effort levels pass
through unchanged except ``ultra``, which maps to ``max``.

Level acceptance verified against the live API on 2026-08-14: both glm-5.2
and glm-5.3 accept ``none, minimal, low, medium, high, xhigh, max``
(``light`` is rejected with error 1210 listing exactly those seven).
"""

from __future__ import annotations

import re
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

_GLM_VERSION_RE = re.compile(r"^glm-(\d+)(?:\.(\d+))?")

# Finds a GLM version anywhere in the model id (vendor prefixes included):
# matches ``glm-5.3``, ``glm-5-3``, ``glm-5p3``, ``z-ai/glm-5.3`` etc.
_GLM_VERSION_SEARCH_RE = re.compile(r"glm-(\d+)(?:[.\-p](\d+))?")


def _model_supports_thinking(model: str | None) -> bool:
    """GLM thinking-capable model families: glm-4.5 and later (4.5, 4.6, 5…)."""
    m = (model or "").strip().lower()
    match = _GLM_VERSION_RE.match(m)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return (major, minor) >= (4, 5)


def _is_glm_5_2(model: str | None) -> bool:
    """Detect GLM-5.2 across the alias spellings providers use.

    Covers the canonical ``glm-5.2`` plus the ``glm-5-2`` / ``glm-5p2``
    variants seen on relays (Fireworks ``glm-5p2``, etc.) and any
    vendor-prefixed form (``z-ai/glm-5.2``, ``zai-org-glm-5-2``).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    return any(token in m for token in ("glm-5.2", "glm-5-2", "glm-5p2"))


def _is_glm_5_3_plus(model: str | None) -> bool:
    """Detect GLM-5.3 and later across alias spellings and vendor prefixes.

    Matches ``glm-5.3`` / ``glm-5-3`` / ``glm-5p3`` / ``z-ai/glm-5.3`` and any
    future ``glm-<major>.<minor>`` >= 5.3.  Excludes 5.2 (handled separately).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    for match in _GLM_VERSION_SEARCH_RE.finditer(m):
        major = int(match.group(1))
        minor = int(match.group(2) or 0)
        if (major, minor) >= (5, 3):
            return True
    return False


def _glm_5_3_reasoning_effort(reasoning_config: dict | None) -> str | None:
    """Map Hermes reasoning config onto GLM-5.3's ``reasoning_effort``.

    GLM-5.3 accepts ``none, minimal, low, medium, high, xhigh, max`` and
    always thinks — there is no off switch.  Mapping:

    * ``enabled: False`` (Hermes ``/reasoning none``) -> ``"minimal"``,
      the lowest effort GLM-5.3 actually honors (``none`` behaves like the
      server default, not like "off").
    * ``ultra`` -> ``max`` (GLM's top tier).
    * every other Hermes level passes through unchanged.
    * no preference -> ``None`` (field omitted, server default = max).
    """
    if not isinstance(reasoning_config, dict):
        return None

    if reasoning_config.get("enabled") is False:
        # GLM-5.3 cannot disable thinking; "minimal" is the real floor.
        return "minimal"

    effort = (reasoning_config.get("effort") or "").strip().lower()
    if not effort or effort == "none":
        return None

    if effort == "ultra":
        return "max"

    # minimal, low, medium, high, xhigh, max — all native GLM-5.3 levels
    return effort


def _glm_5_2_reasoning_effort(reasoning_config: dict | None) -> str | None:
    """Map Hermes reasoning effort onto GLM-5.2's native ``reasoning_effort``.

    GLM-5.2 supports seven effort levels on the OpenAI-compatible endpoint
    (per Z.AI / BigModel docs): ``max``, ``xhigh``, ``high``, ``medium``,
    ``low``, ``minimal``, ``none``.  Hermes' effort scale maps directly onto
    these — every level is passed through unchanged except ``ultra`` (which
    GLM does not expose, so it maps to ``max``).

    When reasoning is explicitly disabled, or no effort preference is
    supplied, the server default is left untouched.
    """
    if not isinstance(reasoning_config, dict):
        return None
    if reasoning_config.get("enabled") is False:
        return None

    effort = (reasoning_config.get("effort") or "").strip().lower()
    if not effort or effort == "none":
        return None

    # ultra is Hermes-only; GLM's top tier is "max"
    if effort == "ultra":
        return "max"

    # minimal, low, medium, high, xhigh, max — all native GLM-5.2 levels
    return effort


class ZaiProfile(ProviderProfile):
    """Z.AI / GLM — 5.3+: reasoning_effort only; <=5.2: thinking toggle + effort."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # GLM >= 5.3: never emit the thinking toggle (no-op on the Coding
        # Plan endpoint, hard error 1210 on the PaaS endpoint).  Effort only.
        if _is_glm_5_3_plus(model):
            effort = _glm_5_3_reasoning_effort(reasoning_config)
            if effort is not None:
                top_level["reasoning_effort"] = effort
            return extra_body, top_level

        if not _model_supports_thinking(model) and not _is_glm_5_2(model):
            return extra_body, top_level

        # Only emit when the user expressed a preference; omitting the field
        # keeps the server default (enabled) exactly as before.
        if isinstance(reasoning_config, dict):
            enabled = reasoning_config.get("enabled") is not False
            extra_body["thinking"] = {"type": "enabled" if enabled else "disabled"}

        if _is_glm_5_2(model):
            effort = _glm_5_2_reasoning_effort(reasoning_config)
            if effort is not None:
                top_level["reasoning_effort"] = effort

        return extra_body, top_level


zai = ZaiProfile(
    name="zai",
    aliases=("glm", "z-ai", "z.ai", "zhipu"),
    env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
    display_name="Z.AI (GLM)",
    description="Z.AI / GLM — Zhipu AI models",
    signup_url="https://z.ai/",
    fallback_models=(
        "glm-5.3",
        "glm-5.2",
        "glm-5",
        "glm-4-9b",
    ),
    base_url="https://api.z.ai/api/paas/v4",
    default_aux_model="glm-4.5-flash",
)

register_provider(zai)
