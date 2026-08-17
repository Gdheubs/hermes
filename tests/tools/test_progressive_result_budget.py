from unittest.mock import patch

from tools.budget_config import BudgetConfig, budget_for_context_window


def test_deferred_result_size_is_opt_in():
    assert BudgetConfig().deferred_result_size is None


@patch("tools.tool_search.is_deferrable_tool_name", return_value=True)
@patch("tools.registry.registry")
def test_deferred_tool_uses_smaller_opt_in_cap(mock_registry, _mock_deferred):
    mock_registry.get_max_result_size.return_value = 100_000
    cfg = BudgetConfig(deferred_result_size=20_000)
    assert cfg.resolve_threshold("mcp__agentmemory__memory_sessions") == 20_000


@patch("tools.tool_search.is_deferrable_tool_name", return_value=True)
@patch("tools.registry.registry")
def test_exact_override_wins_over_deferred_cap(mock_registry, _mock_deferred):
    mock_registry.get_max_result_size.return_value = 100_000
    cfg = BudgetConfig(
        deferred_result_size=20_000,
        tool_overrides={"mcp__agentmemory__memory_sessions": 30_000},
    )
    assert cfg.resolve_threshold("mcp__agentmemory__memory_sessions") == 30_000


@patch("tools.tool_search.is_deferrable_tool_name", return_value=True)
@patch("tools.registry.registry")
def test_deferred_cap_constrains_registry_infinity(mock_registry, _mock_deferred):
    mock_registry.get_max_result_size.return_value = float("inf")
    cfg = BudgetConfig(deferred_result_size=20_000)
    assert cfg.resolve_threshold("mcp__example__unbounded") == 20_000


@patch("tools.tool_search.is_deferrable_tool_name", return_value=False)
@patch("tools.registry.registry")
def test_core_tool_is_unchanged_by_deferred_cap(mock_registry, _mock_deferred):
    mock_registry.get_max_result_size.return_value = 100_000
    cfg = BudgetConfig(deferred_result_size=20_000)
    assert cfg.resolve_threshold("terminal") == 100_000


def test_user_config_applies_deferred_cap():
    cfg = budget_for_context_window(
        1_000_000,
        result_budget_config={"deferred_result_size_chars": 20_000},
    )
    assert cfg.deferred_result_size == 20_000


def test_invalid_deferred_cap_is_ignored():
    cfg = budget_for_context_window(
        1_000_000,
        result_budget_config={"deferred_result_size_chars": "not-an-int"},
    )
    assert cfg.deferred_result_size is None


def test_infinite_deferred_cap_is_ignored_without_losing_context_scaling():
    cfg = budget_for_context_window(
        65_536,
        result_budget_config={"deferred_result_size_chars": float("inf")},
    )

    assert cfg.deferred_result_size is None
    assert cfg.default_result_size == int(65_536 * 0.6)
    assert cfg.turn_budget == int(65_536 * 1.2)
