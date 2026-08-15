"""compression.proactive_prune_* — config parse seam for the proactive prune.

Mirrors ``test_compression_max_attempts_config.py``: the three knobs are
parsed in ``agent_init`` with the same hardened semantics (booleans rejected,
fractional floats rejected — not truncated, integral floats and numeric
strings accepted) and attached to the built-in compressor.  Default is
0 / 8000 / 4096, i.e. the feature is OFF and behavior-neutral unless
``proactive_prune_tokens`` is set above 0.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from hermes_state import SessionDB
from agent.context_compressor import ContextCompressor
from run_agent import AIAgent


def _config(**prune_keys) -> dict:
    compression = {
        "enabled": True,
        "threshold": 0.50,
        "target_ratio": 0.20,
        "protect_first_n": 3,
        "protect_last_n": 20,
    }
    compression.update(prune_keys)
    return {
        "compression": compression,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }


def _make_agent(
    monkeypatch,
    tmp_path,
    *,
    model: str = "gpt-5.5",
    provider: str = "openai-codex",
    base_url: str = "https://chatgpt.com/backend-api/codex",
    **prune_keys,
):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: _config(**prune_keys))

    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: _config(**prune_keys))
    db = SessionDB(db_path=tmp_path / "state.db")
    with contextlib.redirect_stdout(io.StringIO()):
        agent = AIAgent(
            base_url=base_url,
            api_key="test-key",
            provider=provider,
            model=model,
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            session_db=db,
            session_id="proactive-prune-config-test",
        )
    return agent


class TestProactivePruneConfig:
    def test_default_is_disabled_when_unset(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path)
        cc = agent.context_compressor
        assert cc.proactive_prune_tokens == 0
        assert cc.proactive_prune_min_result_chars == 8000
        assert cc.proactive_prune_min_reclaim_tokens == 4096

    def test_custom_values_are_honored(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch,
            tmp_path,
            proactive_prune_tokens=48_000,
            proactive_prune_min_result_chars=12_000,
            proactive_prune_min_reclaim_tokens=8_192,
        )
        cc = agent.context_compressor
        assert cc.proactive_prune_tokens == 48_000
        assert cc.proactive_prune_min_result_chars == 12_000
        assert cc.proactive_prune_min_reclaim_tokens == 8_192

    def test_boolean_is_rejected_not_coerced(self, monkeypatch, tmp_path):
        # bool subclasses int: YAML `proactive_prune_tokens: true` must fall
        # back to disabled, never coerce to 1 token.
        agent = _make_agent(monkeypatch, tmp_path, proactive_prune_tokens=True)
        assert agent.context_compressor.proactive_prune_tokens == 0






class TestProactivePruneAutoDefault:
    """config sentinel -1 (unset) resolves a model-gated auto default:
    ON for large-window DeepSeek thinking sessions, OFF everywhere else.
    Explicit config (including 0 = off) stays authoritative."""

    def test_deepseek_unset_resolves_auto_on(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch, tmp_path,
            model="deepseek-v4-flash", provider="deepseek",
            base_url="https://api.deepseek.com/v1",
        )
        # 1M window // 8 = 125K.
        assert agent.context_compressor.proactive_prune_tokens == 125_000

    def test_deepseek_explicit_zero_stays_off(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch, tmp_path,
            model="deepseek-v4-flash", provider="deepseek",
            base_url="https://api.deepseek.com/v1",
            proactive_prune_tokens=0,
        )
        assert agent.context_compressor.proactive_prune_tokens == 0

    def test_non_deepseek_unset_stays_off(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path)
        assert agent.context_compressor.proactive_prune_tokens == 0

    def test_compressor_derives_auto_from_window(self, monkeypatch):
        from unittest.mock import patch

        from agent.context_compressor import ContextCompressor

        # Sentinel -1 is resolved inside the compressor against the resolved
        # context window (no extra resolver call at init).
        with patch("agent.context_compressor.get_model_context_length", return_value=2_000_000):
            cc = ContextCompressor("gemini-2.5-pro", proactive_prune_tokens=-1)
            assert cc.proactive_prune_tokens == 250_000
        with patch("agent.context_compressor.get_model_context_length", return_value=256_000):
            cc = ContextCompressor("gpt-4o", proactive_prune_tokens=-1)
            assert cc.proactive_prune_tokens == 0


def test_auto_prune_follows_model_switch(monkeypatch, tmp_path):
    agent = _make_agent(
        monkeypatch, tmp_path,
        model="deepseek-v4-flash", provider="deepseek",
        base_url="https://api.deepseek.com/v1",
    )
    cc = agent.context_compressor
    assert cc.proactive_prune_tokens == 125_000  # 1M window // 8
    cc.update_model("gpt-4o-mini", context_length=256_000)
    assert cc.proactive_prune_tokens == 0  # small window: auto prune off
    cc.update_model("gemini-2.5-pro", context_length=2_000_000)
    assert cc.proactive_prune_tokens == 250_000  # 2M window // 8
    # Explicit values are never re-derived.
    cc2 = ContextCompressor("gpt-4o", proactive_prune_tokens=4096)
    cc2.update_model("deepseek-v4-flash", context_length=1_000_000)
    assert cc2.proactive_prune_tokens == 4096
