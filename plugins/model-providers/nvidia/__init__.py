"""NVIDIA NIM provider profile."""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


def _is_deepseek_v4_flash(model: str | None) -> bool:
    """True when the model is a DeepSeek V4 flash model served via NIM.

    NVIDIA NIM hosts ``deepseek-ai/deepseek-v4-flash-<date>`` model IDs.
    Only this family gets the DeepSeek-specific thinking wire format on the
    NVIDIA route; every other NVIDIA model (nemotron, minimax, llama, ...)
    keeps the generic OpenAI-compatible behavior.
    """
    m = (model or "").strip().lower()
    return "deepseek-v4-flash" in m


class NvidiaProfile(ProviderProfile):
    """NVIDIA NIM — OpenAI-compatible, with DeepSeek V4 flash passthrough.

    DeepSeek V4 models default to thinking-mode ON when ``extra_body.thinking``
    is absent, then enforce the ``reasoning_content``-must-be-echoed-back
    contract on subsequent tool-call turns (HTTP 400 otherwise). The official
    DeepSeek profile emits the DeepSeek wire shape to avoid that trap; NIM
    fronts the same DeepSeek models, so the NVIDIA route must do the same for
    deepseek-v4-flash. Non-DeepSeek NVIDIA models must NOT receive the
    DeepSeek-specific fields (they would reject or ignore them).
    """

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None, **context: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not _is_deepseek_v4_flash(model):
            return {}, {}

        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Mirror the official DeepSeek profile
        # (plugins/model-providers/deepseek/__init__.py): thinking is sent
        # explicitly (never left to the server default) and effort maps to
        # top-level reasoning_effort.
        enabled = True
        if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is False:
            enabled = False

        extra_body["thinking"] = {"type": "enabled" if enabled else "disabled"}

        if not enabled:
            return extra_body, top_level

        if isinstance(reasoning_config, dict):
            effort = (reasoning_config.get("effort") or "").strip().lower()
            if effort in {"xhigh", "max"}:
                top_level["reasoning_effort"] = "max"
            elif effort in {"low", "medium", "high"}:
                top_level["reasoning_effort"] = effort

        return extra_body, top_level


nvidia = NvidiaProfile(
    name="nvidia",
    aliases=("nvidia-nim",),
    env_vars=("NVIDIA_API_KEY",),
    display_name="NVIDIA NIM",
    description="NVIDIA NIM — accelerated inference",
    signup_url="https://build.nvidia.com/",
    fallback_models=(
        "nvidia/llama-3.1-nemotron-70b-instruct",
        "nvidia/llama-3.3-70b-instruct",
    ),
    base_url="https://integrate.api.nvidia.com/v1",
    default_max_tokens=16384,
)

register_provider(nvidia)
