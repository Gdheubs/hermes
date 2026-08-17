from types import SimpleNamespace
from unittest.mock import patch

from agent.tool_executor import _budget_for_agent


def test_budget_for_agent_loads_deferred_result_cap_from_config():
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(context_length=1_000_000)
    )
    config = {
        "tools": {
            "result_budget": {
                "deferred_result_size_chars": 20_000,
            }
        }
    }

    with patch("hermes_cli.config.load_config", return_value=config):
        budget = _budget_for_agent(agent)

    assert budget.deferred_result_size == 20_000


def test_budget_for_agent_ignores_infinity_without_disabling_context_scaling():
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(context_length=65_536),
    )
    config = {
        "tools": {
            "result_budget": {"deferred_result_size_chars": float("inf")}
        }
    }

    with patch("hermes_cli.config.load_config", return_value=config):
        budget = _budget_for_agent(agent)

    assert budget.deferred_result_size is None
    assert budget.default_result_size == int(65_536 * 0.6)
    assert budget.turn_budget == int(65_536 * 1.2)
