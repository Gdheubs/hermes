"""NEAR AI Cloud provider profile.

NEAR AI Cloud is an OpenAI-compatible inference gateway that runs models
inside TEEs for verifiable private inference, fronting both frontier
(Anthropic, OpenAI, Gemini) and open (Qwen, GLM, DeepSeek, Kimi) models.
The live catalog at ``https://cloud-api.near.ai/v1/models`` is public (no
key required) and follows the standard ``{"data": [{"id": ...}]}`` shape,
but it is a *product* catalog rather than a chat catalog: alongside 42 chat
models it lists an image generator, an ASR model, an embedding model, a
reranker, and a 512-token prompt filter. The base ``fetch_models`` keeps only
``m["id"]`` and so cannot tell them apart, which puts a diffusion model in the
``hermes model`` picker — hence the override below.
"""

import json
import logging
import urllib.request

from providers import register_provider
from providers.base import ProviderProfile, _profile_user_agent

logger = logging.getLogger(__name__)

_MODELS_URL = "https://cloud-api.near.ai/v1/models"

# Below this, an entry is a utility rather than something you can hold a
# conversation with (openai/privacy-filter is 512, whisper-large-v3 is 448).
# The smallest real chat model in the catalog has 16384.
_MIN_CHAT_CONTEXT = 8192


def _is_chat_model(m: dict) -> bool:
    """Whether a catalog entry is usable as a chat model.

    Driven by the catalog's own per-model metadata rather than an id denylist,
    so new non-chat products are excluded the day they appear:
    ``input_modalities`` drops audio-only ASR, ``output_modalities`` drops the
    image generator and the embedding model, and the context floor drops the
    prompt filter.
    """
    inputs = m.get("input_modalities") or []
    outputs = m.get("output_modalities") or []
    if "text" not in inputs or "text" not in outputs:
        return False
    if (m.get("context_length") or 0) < _MIN_CHAT_CONTEXT:
        return False
    # ponytail: rerankers are the one product the metadata cannot distinguish —
    # they advertise text→text like a chat model and carry a normal context
    # window, so only the id gives them away. Widen if NEAR AI ships another
    # scoring model that this misses.
    return "reranker" not in m.get("id", "").lower()


def _fetch_chat_models(timeout: float = 8.0) -> list[str] | None:
    """Fetch the public catalog and keep only the chat models."""
    try:
        from hermes_cli.urllib_security import open_credentialed_url

        req = urllib.request.Request(_MODELS_URL)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _profile_user_agent())
        with open_credentialed_url(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("data", []) if isinstance(data, dict) else data
        return [
            m["id"]
            for m in items
            if isinstance(m, dict) and "id" in m and _is_chat_model(m)
        ]
    except Exception as exc:
        logger.debug("fetch_models(nearai): %s", exc)
        return None


class NearAIProfile(ProviderProfile):
    """NEAR AI Cloud — filters the product catalog down to chat models."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        return _fetch_chat_models(timeout=timeout)


nearai = NearAIProfile(
    name="nearai",
    aliases=("near-ai", "near"),
    display_name="NEAR AI",
    description="NEAR AI Cloud — verifiable private inference (TEE), frontier + open models",
    signup_url="https://cloud.near.ai",
    env_vars=("NEAR_AI_API_KEY", "NEAR_AI_BASE_URL"),
    base_url="https://cloud-api.near.ai/v1",
    auth_type="api_key",
    # Catalog spans providers with different output ceilings — let each
    # upstream apply its own cap rather than flattening them here.
    default_max_tokens=None,
    # API-shape flag, not a per-model capability: the gateway is OpenAI Chat
    # Completions, so it accepts images inside tool-result messages. Whether a
    # given catalog model can actually see them stays a model-level question,
    # resolved by agent/image_routing.py from the model catalog or an explicit
    # ``providers.nearai.models.<model>.supports_vision`` override.
    supports_vision=True,
    # Resolved against this provider's own catalog (see
    # auxiliary_client._get_aux_model_for_provider) — the id is sent to
    # cloud-api.near.ai, so no separate OpenAI key is involved.
    #
    # Hard constraints, both from how the field is actually consumed:
    #   - multimodal: resolve_provider_client() fills an unset model from this
    #     field on the vision path too (is_vision=True), so a text-only pick
    #     ships the image to a model that cannot see it. Rules out the cheaper
    #     openai/gpt-5-mini ($0.25/M), which reports input_modalities=["text"].
    #   - 1M context: context compaction feeds a whole conversation through
    #     this model, and a mid-tier ceiling turns compaction into truncation.
    #
    # Eight catalog ids clear both bars, and this is NOT the cheapest —
    # openai/gpt-4.1-nano beats it on every measurable axis ($0.10 vs $0.30/M
    # in, $0.40 vs $2.50/M out, 16384 vs 8192 max output, same 1M context).
    # The pick is a deliberate quality-over-cost call on faithfulness when
    # compacting a long conversation, the one axis the catalog does not expose:
    # a bad compaction silently drops session state instead of merely looking
    # sloppy, and it runs a handful of times per session rather than per turn.
    # ai-gateway, the other multi-upstream gateway here, defaults to a Gemini
    # flash for the same reason. Switch to gpt-4.1-nano if the cost or the
    # 8192-token output ceiling shows up in practice.
    default_aux_model="google/gemini-2.5-flash",
    # Not fallback-only despite the field name: nearai is absent from
    # models.py::_LIVE_FIRST_PICKER_PROVIDERS, so these are merged *ahead* of
    # the live catalog and lead the `hermes model` picker even on a successful
    # fetch. That is the intent — the live catalog's own order buries the
    # agentic models — so this list must stay a hand-picked front page, ordered
    # best-first, not a grab bag.
    fallback_models=(
        "anthropic/claude-sonnet-5",
        "openai/gpt-5.4",
        "google/gemini-3.5-flash",
        "moonshotai/kimi-k2.6",
        "z-ai/glm-5.2",
        "qwen/qwen3.7-max",
        "deepseek-ai/DeepSeek-V4-Flash",
    ),
)

register_provider(nearai)
