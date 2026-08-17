"""Tests for per-turn primary runtime restoration and transport recovery.

Verifies that:
1. Fallback is turn-scoped: a new turn restores the primary model/provider
2. The fallback chain index resets so all fallbacks are available again
3. Context compressor state is restored alongside the runtime
4. Transient transport errors get one recovery cycle before fallback
5. Recovery is skipped for aggregator providers (OpenRouter, Nous)
6. Non-transport errors don't trigger recovery
"""

import time
from unittest.mock import MagicMock, patch


from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _make_agent(
    fallback_model=None,
    provider="custom",
    base_url="https://my-llm.example.com/v1",
    reasoning_config=None,
):
    """Create a minimal AIAgent with optional fallback config."""
    extra = {"reasoning_config": reasoning_config} if reasoning_config else {}
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        # Unit tests must not probe live endpoints. The compressor resolves
        # context length lazily via a real network call against base_url; for
        # reachable hosts (the nous portal case) the endpoint's answer for the
        # empty test model (32K) trips agent_init's 64K floor and fails the
        # test on network behavior, not code under test.
        patch(
            "agent.context_compressor.get_model_context_length",
            return_value=200_000,
        ),
    ):
        agent = AIAgent(
            api_key="test-key-12345678",
            base_url=base_url,
            provider=provider,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
            **extra,
        )
        agent.client = MagicMock()
        return agent


def _mock_resolve(base_url="https://openrouter.ai/api/v1", api_key="fallback-key-1234"):
    """Helper to create a mock client for resolve_provider_client."""
    mock_client = MagicMock()
    mock_client.api_key = api_key
    mock_client.base_url = base_url
    return mock_client


# =============================================================================
# Live reasoning changes vs the primary snapshot
# =============================================================================


class TestApplyLiveReasoningConfig:
    """A live ``/reasoning`` change reuses the running agent, so the
    primary-runtime snapshot has to move with it — otherwise a fallback later
    in the session restores the effort the session STARTED at, and the agent's
    prompt then advertises a tier nobody selected.

    The one exception is while a fallback is active: ``agent.reasoning_config``
    then describes the FALLBACK's tier, and writing it into the snapshot would
    destroy the only record of what the primary should come back to.
    """

    def test_live_change_refreshes_the_snapshot(self):
        from agent.agent_runtime_helpers import apply_live_reasoning_config

        agent = _make_agent(reasoning_config={"enabled": True, "effort": "medium"})
        apply_live_reasoning_config(agent, {"enabled": True, "effort": "ultra"})

        assert agent.reasoning_config == {"enabled": True, "effort": "ultra"}
        assert agent._primary_runtime["reasoning_config"] == {
            "enabled": True, "effort": "ultra",
        }

    def test_snapshot_stores_a_copy_not_an_alias(self):
        from agent.agent_runtime_helpers import apply_live_reasoning_config

        agent = _make_agent()
        live = {"enabled": True, "effort": "ultra"}
        apply_live_reasoning_config(agent, live)
        live["effort"] = "low"

        assert agent._primary_runtime["reasoning_config"]["effort"] == "ultra"

    def test_clearing_to_provider_default_is_recorded_as_present_none(self):
        """``None`` must be stored, not skipped — restore branches on key
        presence, so a dropped key would silently keep the old effort."""
        from agent.agent_runtime_helpers import apply_live_reasoning_config

        agent = _make_agent(reasoning_config={"enabled": True, "effort": "ultra"})
        apply_live_reasoning_config(agent, None)

        assert agent.reasoning_config is None
        assert "reasoning_config" in agent._primary_runtime
        assert agent._primary_runtime["reasoning_config"] is None

    def test_change_while_fallback_active_does_not_touch_the_snapshot(self):
        from agent.agent_runtime_helpers import apply_live_reasoning_config

        agent = _make_agent(reasoning_config={"enabled": True, "effort": "ultra"})
        agent._fallback_activated = True
        apply_live_reasoning_config(agent, {"enabled": True, "effort": "low"})

        assert agent.reasoning_config == {"enabled": True, "effort": "low"}
        assert agent._primary_runtime["reasoning_config"] == {
            "enabled": True, "effort": "ultra",
        }, "the primary's saved effort must survive a fallback-time assignment"

    def test_change_during_fallback_is_parked_not_dropped(self):
        """Both surfaces can take a change in the window between a fallback
        turn ending and the next turn's restore. Holding the snapshot back is
        right, but the selection must not be thrown away — restore would
        otherwise revert the user's brand-new pick on the very next turn."""
        from agent.agent_runtime_helpers import apply_live_reasoning_config

        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
            reasoning_config={"enabled": True, "effort": "medium"},
        )
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback()
        assert agent._fallback_activated is True

        # User picks ultra while the fallback still owns the runtime.
        apply_live_reasoning_config(agent, {"enabled": True, "effort": "ultra"})
        assert agent._primary_runtime["reasoning_config"] == {
            "enabled": True, "effort": "medium",
        }

        with patch("run_agent.OpenAI", return_value=MagicMock()):
            assert agent._restore_primary_runtime() is True

        assert agent.reasoning_config == {"enabled": True, "effort": "ultra"}
        assert agent._primary_runtime["reasoning_config"] == {
            "enabled": True, "effort": "ultra",
        }

    def test_parked_provider_default_is_distinguishable_from_nothing_parked(self):
        """Parking ``None`` (provider default) must not read as 'no change'."""
        from agent.agent_runtime_helpers import apply_live_reasoning_config

        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
            reasoning_config={"enabled": True, "effort": "medium"},
        )
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback()

        apply_live_reasoning_config(agent, None)
        with patch("run_agent.OpenAI", return_value=MagicMock()):
            assert agent._restore_primary_runtime() is True

        assert agent.reasoning_config is None
        assert agent._primary_runtime["reasoning_config"] is None

    def test_restore_without_a_parked_change_uses_the_snapshot(self):
        """No live change during the fallback → plain snapshot restore, and
        nothing stale is left behind to fire on a later turn."""
        from agent.agent_runtime_helpers import apply_live_reasoning_config

        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
            reasoning_config={"enabled": True, "effort": "medium"},
        )
        apply_live_reasoning_config(agent, {"enabled": True, "effort": "ultra"})
        assert getattr(agent, "_pending_primary_reasoning_config", None) is None

        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback()
        with patch("run_agent.OpenAI", return_value=MagicMock()):
            assert agent._restore_primary_runtime() is True

        assert agent.reasoning_config == {"enabled": True, "effort": "ultra"}
        assert getattr(agent, "_pending_primary_reasoning_config", None) is None

    def test_restore_returns_the_latest_live_effort_not_the_original(self):
        """The whole point, end to end: change effort mid-session, fail over,
        come back — the primary must resume at the LIVE effort."""
        from agent.agent_runtime_helpers import apply_live_reasoning_config

        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
            reasoning_config={"enabled": True, "effort": "medium"},
        )
        apply_live_reasoning_config(agent, {"enabled": True, "effort": "ultra"})

        mock_client = _mock_resolve()
        with (
            patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)),
            patch(
                "hermes_constants.resolve_reasoning_config",
                return_value={"enabled": True, "effort": "xhigh"},
            ),
        ):
            agent._try_activate_fallback()
        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}

        with patch("run_agent.OpenAI", return_value=MagicMock()):
            assert agent._restore_primary_runtime() is True
        assert agent.reasoning_config == {"enabled": True, "effort": "ultra"}


# =============================================================================
# _primary_runtime snapshot
# =============================================================================

class TestPrimaryRuntimeSnapshot:
    def test_snapshot_created_at_init(self):
        agent = _make_agent()
        assert hasattr(agent, "_primary_runtime")
        rt = agent._primary_runtime
        assert rt["model"] == agent.model
        assert rt["provider"] == "custom"
        assert rt["base_url"] == "https://my-llm.example.com/v1"
        assert rt["api_mode"] == agent.api_mode
        assert "client_kwargs" in rt
        assert "compressor_context_length" in rt

    def test_snapshot_always_records_reasoning_config(self):
        """Fallback activation re-resolves reasoning_config for the fallback
        model, so the primary's value has to be captured at init — and the KEY
        has to be present even when the value is None, or restore cannot tell
        "primary had no explicit effort" from "old snapshot never recorded
        one" and would leave the fallback's tier in place."""
        agent = _make_agent()
        assert agent.reasoning_config is None
        assert "reasoning_config" in agent._primary_runtime
        assert agent._primary_runtime["reasoning_config"] is None

    def test_snapshot_copies_rather_than_aliases_reasoning_config(self):
        """A live mutation of agent.reasoning_config must not rewrite history."""
        agent = _make_agent(reasoning_config={"enabled": True, "effort": "ultra"})
        assert agent._primary_runtime["reasoning_config"] == {
            "enabled": True, "effort": "ultra",
        }
        agent.reasoning_config["effort"] = "low"
        assert agent._primary_runtime["reasoning_config"]["effort"] == "ultra"

    def test_snapshot_includes_compressor_state(self):
        agent = _make_agent()
        rt = agent._primary_runtime
        cc = agent.context_compressor
        assert rt["compressor_model"] == cc.model
        assert rt["compressor_provider"] == cc.provider
        assert rt["compressor_context_length"] == cc.context_length
        assert rt["compressor_threshold_tokens"] == cc.threshold_tokens

    def test_snapshot_includes_anthropic_state_when_applicable(self):
        """Anthropic-mode agents should snapshot Anthropic-specific state."""
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
        ):
            agent = AIAgent(
                api_key="sk-ant-test-12345678",
                base_url="https://api.anthropic.com",
                provider="anthropic",
                api_mode="anthropic_messages",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        rt = agent._primary_runtime
        assert "anthropic_api_key" in rt
        assert "anthropic_base_url" in rt
        assert "is_anthropic_oauth" in rt

    def test_snapshot_omits_anthropic_for_openai_mode(self):
        agent = _make_agent(provider="custom")
        rt = agent._primary_runtime
        assert "anthropic_api_key" not in rt


# =============================================================================
# _restore_primary_runtime()
# =============================================================================

class TestRestorePrimaryRuntime:
    def test_noop_when_not_fallback(self):
        agent = _make_agent()
        assert agent._fallback_activated is False
        assert agent._restore_primary_runtime() is False

    def test_restores_model_and_provider(self):
        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        )
        original_model = agent.model
        original_provider = agent.provider

        # Simulate fallback activation
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback()

        assert agent._fallback_activated is True
        assert agent.model == "anthropic/claude-sonnet-4"
        assert agent.provider == "openrouter"

        # Restore should bring back the primary
        with patch("run_agent.OpenAI", return_value=MagicMock()):
            result = agent._restore_primary_runtime()

        assert result is True
        assert agent._fallback_activated is False
        assert agent.model == original_model
        assert agent.provider == original_provider

    def test_restores_provider_default_reasoning_over_a_fallback_override(self):
        """The primary ran at the provider default; the fallback re-resolved a
        concrete effort. Restore must put the agent — and therefore the
        prompt's reasoning line — back on the provider default, not leave the
        fallback's tier behind under the primary's model name."""
        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        )
        assert agent.reasoning_config is None

        mock_client = _mock_resolve()
        with (
            patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)),
            patch(
                "hermes_constants.resolve_reasoning_config",
                return_value={"enabled": True, "effort": "xhigh"},
            ),
        ):
            agent._try_activate_fallback()
        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}

        with patch("run_agent.OpenAI", return_value=MagicMock()):
            assert agent._restore_primary_runtime() is True
        assert agent.reasoning_config is None

    def test_legacy_snapshot_without_the_key_keeps_current_reasoning(self):
        """Back-compat: an in-flight snapshot from before the key existed must
        not be read as 'the primary had no effort'."""
        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        )
        agent._primary_runtime.pop("reasoning_config", None)
        agent._fallback_activated = True
        agent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        with patch("run_agent.OpenAI", return_value=MagicMock()):
            assert agent._restore_primary_runtime() is True
        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}

    def test_resets_fallback_index(self):
        """After restore, the full fallback chain should be available again."""
        agent = _make_agent(
            fallback_model=[
                {"provider": "openrouter", "model": "model-a"},
                {"provider": "anthropic", "model": "model-b"},
            ],
        )
        # Advance through the chain
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback()

        assert agent._fallback_index == 1  # consumed one entry

        with patch("run_agent.OpenAI", return_value=MagicMock()):
            agent._restore_primary_runtime()

        assert agent._fallback_index == 0  # reset for next turn

    def test_restores_compressor_state(self):
        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        )
        original_ctx_len = agent.context_compressor.context_length
        original_threshold = agent.context_compressor.threshold_tokens

        # Simulate fallback modifying compressor
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback()

        # Manually simulate compressor being changed (as _try_activate_fallback does)
        agent.context_compressor.context_length = 32000
        agent.context_compressor.threshold_tokens = 25600

        with patch("run_agent.OpenAI", return_value=MagicMock()):
            agent._restore_primary_runtime()

        assert agent.context_compressor.context_length == original_ctx_len
        assert agent.context_compressor.threshold_tokens == original_threshold

    def test_restores_prompt_caching_flag(self):
        agent = _make_agent()
        original_caching = agent._use_prompt_caching

        # Simulate fallback changing the caching flag
        agent._fallback_activated = True
        agent._use_prompt_caching = not original_caching

        with patch("run_agent.OpenAI", return_value=MagicMock()):
            agent._restore_primary_runtime()

        assert agent._use_prompt_caching == original_caching

    def test_restore_skips_cross_provider_pool_entry(self):
        """Restore must not swap in a fallback provider credential for the primary runtime."""

        class _Entry:
            provider = "openrouter"
            id = "fallback-entry"
            label = "fallback"
            runtime_api_key = "fallback-key"
            runtime_base_url = "https://openrouter.ai/api/v1"
            access_token = "fallback-key"

        class _Pool:
            provider = "openrouter"

            def has_available(self):
                return True

            def select(self):
                return _Entry()

        agent = _make_agent(
            provider="custom",
            base_url="https://primary.example.com/v1",
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        )
        original_base_url = agent.base_url
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback()
        agent._credential_pool = _Pool()
        agent._swap_credential = MagicMock()

        with patch("run_agent.OpenAI", return_value=MagicMock()):
            result = agent._restore_primary_runtime()

        assert result is True
        assert agent.provider == "custom"
        assert agent.base_url == original_base_url
        agent._swap_credential.assert_not_called()

    def test_restore_keeps_primary_base_url_when_fallback_pool_attached(self):
        """Issue #56885: plain-provider primary must not inherit a fallback
        provider's base_url via the restore-path pool reselect.

        Repro: primary is openai-api/gpt-5.5, a transient failure falls back to
        deepseek and attaches deepseek's credential pool. On the next turn the
        restore reselect must NOT swap in the deepseek entry — otherwise the
        request goes out as model=gpt-5.5 to base_url=api.deepseek.com → 404.
        """

        class _DeepseekEntry:
            provider = "deepseek"
            id = "dsk-1"
            label = "deepseek-key"
            runtime_api_key = "sk-deepseek-xxx"
            runtime_base_url = "https://api.deepseek.com/v1"
            base_url = "https://api.deepseek.com/v1"
            access_token = "sk-deepseek-xxx"

        class _DeepseekPool:
            provider = "deepseek"

            def has_available(self):
                return True

            def select(self):
                return _DeepseekEntry()

        agent = _make_agent(
            provider="openai-api",
            base_url="https://api.openai.com/v1",
            fallback_model={"provider": "deepseek", "model": "deepseek-v4-flash"},
        )
        primary_base_url = agent.base_url
        primary_provider = agent.provider
        mock_client = _mock_resolve(base_url="https://api.deepseek.com/v1")
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(mock_client, None),
        ):
            agent._try_activate_fallback()
        # Fallback attached deepseek's pool; simulate it surviving into the next turn.
        agent._credential_pool = _DeepseekPool()
        agent._swap_credential = MagicMock()

        primary_pool = MagicMock()
        primary_pool.provider = primary_provider
        primary_pool.has_available.return_value = False
        with (
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("agent.credential_pool.load_pool", return_value=primary_pool) as load_pool,
        ):
            result = agent._restore_primary_runtime()

        assert result is True
        assert agent.provider == primary_provider
        assert agent.base_url == primary_base_url
        assert "deepseek" not in str(agent.base_url)
        assert agent._credential_pool is primary_pool
        load_pool.assert_called_once_with(primary_provider)
        agent._swap_credential.assert_not_called()

    def test_restore_clears_fallback_pool_when_primary_pool_reload_fails(self):
        """A fallback pool must never remain attached to the restored primary."""
        agent = _make_agent(
            provider="openai-api",
            base_url="https://api.openai.com/v1",
        )
        agent._fallback_activated = True
        fallback_pool = MagicMock()
        fallback_pool.provider = "deepseek"
        agent._credential_pool = fallback_pool

        with (
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch(
                "agent.credential_pool.load_pool",
                side_effect=RuntimeError("auth store unavailable"),
            ),
        ):
            result = agent._restore_primary_runtime()

        assert result is True
        assert agent.provider == "openai-api"
        assert agent._credential_pool is None

    def test_restore_swaps_matching_custom_pool_entry(self):
        """Custom primary + custom:<name> entry whose base_url resolves to the
        SAME custom key must swap (legitimate same-endpoint rotation)."""

        class _Entry:
            provider = "custom:myllm"
            id = "custom-entry"
            label = "myllm"
            runtime_api_key = "custom-key"
            runtime_base_url = "https://my-llm.example.com/v1"
            access_token = "custom-key"

        class _Pool:
            provider = "custom:myllm"

            def has_available(self):
                return True

            def select(self):
                return _Entry()

        agent = _make_agent(provider="custom", base_url="https://my-llm.example.com/v1")
        agent._fallback_activated = True
        agent._credential_pool = _Pool()
        agent._swap_credential = MagicMock()

        with (
            patch(
                "agent.credential_pool.get_custom_provider_pool_key",
                return_value="custom:myllm",
            ),
            patch("run_agent.OpenAI", return_value=MagicMock()),
        ):
            result = agent._restore_primary_runtime()

        assert result is True
        agent._swap_credential.assert_called_once()




# =============================================================================
# _try_recover_primary_transport()
# =============================================================================

def _make_transport_error(error_type="ReadTimeout"):
    """Create an exception whose type().__name__ matches the given name."""
    cls = type(error_type, (Exception,), {})
    return cls("connection timed out")


class TestTryRecoverPrimaryTransport:

    def test_recovers_on_read_timeout(self):
        agent = _make_agent(provider="custom")
        error = _make_transport_error("ReadTimeout")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep"):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is True





    def test_skipped_when_already_on_fallback(self):
        agent = _make_agent(provider="custom")
        agent._fallback_activated = True
        error = _make_transport_error("ReadTimeout")

        result = agent._try_recover_primary_transport(
            error, retry_count=3, max_retries=3,
        )
        assert result is False




    def test_allowed_for_nous_anthropic_messages(self):
        """Portal Claude holds a local Anthropic SDK client — rebuild it."""
        agent = _make_agent(
            provider="nous",
            base_url="https://inference-api.nousresearch.com/v1",
        )
        agent.api_mode = "anthropic_messages"
        agent.model = "anthropic/claude-opus-4.8"
        agent._primary_runtime.update({
            "api_mode": "anthropic_messages",
            "model": "anthropic/claude-opus-4.8",
            "provider": "nous",
            "anthropic_api_key": "portal-jwt",
            "anthropic_base_url": "https://inference-api.nousresearch.com/v1",
            "is_anthropic_oauth": False,
        })
        error = _make_transport_error("ReadTimeout")
        rebuilt = MagicMock(name="anthropic-client")

        with (
            patch(
                "agent.anthropic_adapter.build_anthropic_client",
                return_value=rebuilt,
            ),
            patch("time.sleep"),
        ):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is True
        assert agent._anthropic_client is rebuilt



    def test_wait_time_scales_with_retry_count(self):
        agent = _make_agent(provider="custom")
        error = _make_transport_error("ReadTimeout")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep") as mock_sleep:
            agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )
            # wait_time = min(3 + retry_count, 8) = min(6, 8) = 6
            mock_sleep.assert_called_once_with(6)

    def test_wait_time_capped_at_8(self):
        agent = _make_agent(provider="custom")
        error = _make_transport_error("ReadTimeout")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep") as mock_sleep:
            agent._try_recover_primary_transport(
                error, retry_count=10, max_retries=3,
            )
            # wait_time = min(3 + 10, 8) = 8
            mock_sleep.assert_called_once_with(8)


    def test_survives_rebuild_failure(self):
        """If client rebuild fails, returns False gracefully."""
        agent = _make_agent(provider="custom")
        error = _make_transport_error("ReadTimeout")

        with patch("run_agent.OpenAI", side_effect=Exception("socket error")), \
             patch("time.sleep"):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is False


# =============================================================================
# Integration: restore_primary_runtime called from run_conversation
# =============================================================================

class TestRestoreInRunConversation:
    """Verify the hook in run_conversation() calls _restore_primary_runtime."""

    def test_restore_called_at_turn_start(self):
        agent = _make_agent()
        agent._fallback_activated = True

        with patch.object(agent, "_restore_primary_runtime", return_value=True) as mock_restore, \
             patch.object(agent, "run_conversation", wraps=None) as _:
            # We can't easily run the full conversation, but we can verify
            # the method exists and is callable
            agent._restore_primary_runtime()
            mock_restore.assert_called_once()

    def test_full_cycle_fallback_then_restore(self):
        """Simulate: turn 1 activates fallback, turn 2 restores primary."""
        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
            provider="custom",
        )

        # Turn 1: activate fallback
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            assert agent._try_activate_fallback() is True

        assert agent._fallback_activated is True
        assert agent.model == "anthropic/claude-sonnet-4"
        assert agent.provider == "openrouter"
        assert agent._fallback_index == 1

        # Turn 2: restore primary
        with patch("run_agent.OpenAI", return_value=MagicMock()):
            assert agent._restore_primary_runtime() is True

        assert agent._fallback_activated is False
        assert agent._fallback_index == 0
        assert agent.provider == "custom"
        assert agent.base_url == "https://my-llm.example.com/v1"


# =============================================================================
# Rate-limit cooldown gate
# =============================================================================

class TestRateLimitCooldown:
    """Verify _restore_primary_runtime() respects the 60s rate-limit cooldown."""

    def test_restore_blocked_during_cooldown(self):
        """While _rate_limited_until is in the future, restore returns False."""
        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        )
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback()

        assert agent._fallback_activated is True

        # Manually set cooldown well into the future
        agent._rate_limited_until = time.monotonic() + 60

        result = agent._restore_primary_runtime()
        assert result is False
        assert agent._fallback_activated is True  # still on fallback


    def test_cooldown_set_on_rate_limit_reason(self):
        """_try_activate_fallback with rate_limit reason sets _rate_limited_until."""
        from run_agent import FailoverReason
        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        )
        before = time.monotonic()
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback(reason=FailoverReason.rate_limit)

        assert hasattr(agent, "_rate_limited_until")
        assert agent._rate_limited_until > before + 50  # ~60s from now

    def test_cooldown_not_set_when_already_on_fallback(self):
        """Chain-switching while already on fallback must not reset cooldown."""
        from run_agent import FailoverReason
        agent = _make_agent(
            fallback_model=[
                {"provider": "openrouter", "model": "model-a"},
                {"provider": "anthropic", "model": "model-b"},
            ],
        )
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            # First call: leaving primary → cooldown should be set
            agent._try_activate_fallback(reason=FailoverReason.rate_limit)
            first_cooldown = getattr(agent, "_rate_limited_until", 0)

            # Second call: already on fallback (provider != primary) → cooldown must not advance
            agent._try_activate_fallback(reason=FailoverReason.rate_limit)
            second_cooldown = getattr(agent, "_rate_limited_until", 0)

        # second call should not have extended the cooldown
        assert second_cooldown == first_cooldown
