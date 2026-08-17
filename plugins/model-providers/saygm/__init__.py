"""SayGM OpenAI-compatible Chat Completions provider profile.

The public model catalogue contains several protocol shapes. This profile
exposes only models advertising ``chat.completions``; Anthropic Messages and
OpenAI Responses models require separate transports.
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile, _profile_user_agent

# Offline fallback for the Chat Completions transport. Live discovery applies
# the same protocol filter, so models served only through Messages or Responses
# are not presented as usable here.
SAYGM_CC_MODELS = (
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "o3",
    "o4-mini",
)


class SayGMProfile(ProviderProfile):
    """SayGM — OpenAI-compatible chat surface, cc-specific reasoning quirks."""

    # Per-model completion-token caps. SayGM's default (mirroring upstream) is
    # 128k for the gpt-5.x family, but it caps o3/o4-mini at 100,003
    # completion tokens and 400s anything larger ("max_tokens is too large").
    # Hermes then misclassifies that 400 as a context-overflow and, on a tiny
    # conversation, bails with "Context length exceeded (N tokens)". Pin the
    # cap below SayGM's limit so the request goes through.
    _MODEL_MAX_TOKENS: dict[str, int] = {
        "o3": 100000,
        "o4-mini": 100000,
    }

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Return only currently available Chat Completions models."""
        import json
        import urllib.request

        from hermes_cli.urllib_security import open_credentialed_url

        url = (base_url or self.base_url).rstrip("/") + "/models"
        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _profile_user_agent())

        try:
            with open_credentialed_url(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            items = payload if isinstance(payload, list) else payload.get("data", [])
            return [
                item["id"]
                for item in items
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item.get("available") is True
                and isinstance(item.get("api_shapes"), list)
                and "chat.completions" in item["api_shapes"]
            ]
        except Exception:
            return None

    def get_max_tokens(self, model: str | None) -> int | None:
        cap = self._MODEL_MAX_TOKENS.get((model or "").strip().lower())
        if cap is not None:
            return cap
        return self.default_max_tokens

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Sol requires reasoning to be disabled when function tools are sent on
        # Chat Completions. Keep this model-specific: other SayGM chat models
        # either do not need the field or reject the "none" value.
        if (model or "").strip().lower() == "gpt-5.6-sol":
            top_level["reasoning_effort"] = "none"

        return extra_body, top_level


saygm = SayGMProfile(
    name="saygm",
    aliases=("saygm", "SayGM"),
    display_name="SayGM",
    description="SayGM — OpenAI-compatible chat models",
    signup_url="https://saygm.com",
    env_vars=("SAYGM_API_KEY",),
    base_url="https://api.saygm.com/v1",
    auth_type="api_key",
    # Frontier models cap output at 128k tokens; make that budget usable
    # instead of letting the endpoint truncate to its server default.
    default_max_tokens=128000,
    default_aux_model="gpt-5.4-mini",
    fallback_models=SAYGM_CC_MODELS,
)

register_provider(saygm)
