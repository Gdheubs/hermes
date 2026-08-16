#!/usr/bin/env python3
"""Compare hermes's static context-window catalog against the live catalogs
hermes itself resolves from at runtime: models.dev (``limit.context``, the
primary source) and OpenRouter (``context_length``). Warnings (yellow,
non-gating) on drift — the signal that a ``agent/model_metadata.py`` entry
needs a deliberate update.

The 5% tolerance absorbs the 2^20 rounding convention (1,000,000 vs
1,048,576); a real provider bump (e.g. 1M -> 2M) crosses it and warns.
"""

import json
import sys
import urllib.request

TOLERANCE = 0.05
_UA = {"User-Agent": "hermes-agent context-window-check/1.0", "Accept": "application/json"}


def _fetch(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.load(resp)
    except Exception as exc:
        print(f"::notice::context-window check: {url} fetch failed ({exc}); skipping that source")
        return None


def main() -> int:
    models_dev = _fetch("https://models.dev/models.json")
    openrouter = _fetch("https://openrouter.ai/api/v1/models")
    if models_dev is None and openrouter is None:
        return 0  # both sources unreachable — never gate on transient network

    from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS
    from hermes_cli.models import OPENROUTER_MODELS, _PROVIDER_MODELS

    or_live = {m["id"]: m.get("context_length") for m in (openrouter or {}).get("data", [])}
    # Every provider/model id hermes can send: OpenRouter list + per-provider maps.
    provider_model_ids = list(dict.fromkeys(
        [pm for pm, _ in OPENROUTER_MODELS]
        + [pm for models in _PROVIDER_MODELS.values() for pm in models]
    ))

    checked = 0
    warnings = 0
    for provider_model in provider_model_ids:
        _provider, _, model = provider_model.partition("/")
        hermes_v = DEFAULT_CONTEXT_LENGTHS.get(model)
        if not hermes_v:
            continue  # not a static entry (runtime-resolved — not stale-prone)
        live = None
        source = None
        md_entry = (models_dev or {}).get(provider_model)
        if isinstance(md_entry, dict):
            live = (md_entry.get("limit") or {}).get("context")
            source = "models.dev"
        if live is None:
            live = or_live.get(provider_model)
            source = "openrouter"
        if not live:
            continue
        if abs(live - hermes_v) / hermes_v > TOLERANCE:
            print(
                f"::warning::context-window drift: {provider_model} "
                f"hermes={hermes_v} {source}={live} — "
                "verify/update agent/model_metadata.py"
            )
            warnings += 1
        checked += 1
    print(f"checked {checked} static entries against models.dev/openrouter; {warnings} drift warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
