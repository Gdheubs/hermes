"""The qwen preserve_thinking enforcement."""

from unittest.mock import patch

from agent.chat_completion_helpers import resolve_qwen_preserve_thinking


class _Qwen:
    model = "qwen3.8-max"
    _base_url_lower = "https://portal.qwen.ai/v1"


class _Other:
    model = "gpt-5.6"
    _base_url_lower = ""


def test_enforced_by_default():
    with patch("hermes_cli.config.load_config_readonly", return_value={}):
        assert resolve_qwen_preserve_thinking(_Qwen()) is True


def test_never_for_non_qwen():
    with patch("hermes_cli.config.load_config_readonly", return_value={}):
        assert resolve_qwen_preserve_thinking(_Other()) is False


def test_config_can_opt_out():
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"HERMES_QWEN_PRESERVE_THINKING": False}):
        assert resolve_qwen_preserve_thinking(_Qwen()) is False
