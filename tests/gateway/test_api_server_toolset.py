"""Tests for hermes-api-server toolset and API server tool availability."""
import json
from unittest.mock import patch, MagicMock


from toolsets import resolve_toolset, get_toolset, validate_toolset


class TestHermesApiServerToolset:
    """Tests for the hermes-api-server toolset definition."""


    def test_toolset_includes_web_tools(self):
        tools = resolve_toolset("hermes-api-server")
        assert "web_search" in tools
        assert "web_extract" in tools

    def test_toolset_includes_core_tools(self):
        tools = resolve_toolset("hermes-api-server")
        expected = [
            "terminal", "process",
            "read_file", "write_file", "patch", "search_files",
            "vision_analyze", "image_generate",
            "execute_code", "delegate_task",
            "todo", "memory", "session_search", "cronjob",
        ]
        for tool in expected:
            assert tool in tools, f"Missing expected tool: {tool}"

    def test_toolset_includes_browser_tools(self):
        tools = resolve_toolset("hermes-api-server")
        for tool in ["browser_navigate", "browser_snapshot", "browser_click",
                      "browser_type", "browser_scroll", "browser_back",
                      "browser_press"]:
            assert tool in tools, f"Missing browser tool: {tool}"


class TestApiServerPlatformConfig:

    def test_default_api_server_includes_terminal_toolset(self):
        """Regression #49622: desktop-only read_terminal is registered into the
        'terminal' toolset (ships in-repo), so resolve_toolset('terminal') grows
        to include it after discovery. read_terminal is NOT in the
        hermes-api-server composite, so the old all-tools subset test dropped
        'terminal' entirely. Its static membership (terminal, process) IS in the
        composite, so it must stay enabled."""
        from tools.registry import discover_builtin_tools
        from hermes_cli.tools_config import _get_platform_tools
        discover_builtin_tools()
        assert "terminal" in _get_platform_tools({}, "api_server")


class TestApiServerAdapterToolset:
    def test_message_response_projects_public_codex_commentary_for_display(self):
        """Codex commentary stays out of canonical content but survives hydration."""
        from gateway.platforms.api_server import APIServerAdapter

        payload = APIServerAdapter._message_response({
            "id": 7,
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "tc-1", "function": {"name": "terminal", "arguments": "{}"}}],
            "codex_message_items": [
                {
                    "type": "message",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "I'll inspect the repo first."}],
                },
                {
                    "type": "message",
                    "phase": "analysis",
                    "content": [{"type": "output_text", "text": "private scratchpad"}],
                },
            ],
        })

        assert payload["content"] == ""
        assert payload["display_content"] == "I'll inspect the repo first."
        assert "private scratchpad" not in payload["display_content"]
        assert "codex_message_items" not in payload

    def test_message_response_honors_commentary_visibility(self):
        from gateway.platforms.api_server import APIServerAdapter

        payload = APIServerAdapter._message_response({
            "role": "assistant",
            "content": "",
            "codex_message_items": [{
                "type": "message",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Public while enabled."}],
            }],
        }, show_commentary=False)

        assert "display_content" not in payload
        assert "codex_message_items" not in payload

    def test_message_response_forces_secret_redaction_at_projection_boundary(self):
        from gateway.platforms.api_server import APIServerAdapter

        secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
        with patch("agent.redact._REDACT_ENABLED", False):
            payload = APIServerAdapter._message_response({
                "role": "assistant",
                "content": "",
                "codex_message_items": json.dumps([{
                    "type": "message",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": f"Token: {secret}"}],
                }]),
            })

        assert secret not in payload["display_content"]
        assert "codex_message_items" not in payload

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_create_agent_reads_config_toolsets(self):
        """API server resolves toolsets from config like all other platforms."""
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())

        with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
             patch("gateway.run._resolve_gateway_model") as mock_model, \
             patch("gateway.run._load_gateway_config") as mock_config, \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_kwargs.return_value = {"api_key": "test-key", "base_url": None,
                                        "provider": None, "api_mode": None,
                                        "command": None, "args": []}
            mock_model.return_value = "test/model"
            # No platform_toolsets override — should fall back to hermes-api-server default
            mock_config.return_value = {}
            mock_agent_cls.return_value = MagicMock()

            adapter._create_agent()

            mock_agent_cls.assert_called_once()
            call_kwargs = mock_agent_cls.call_args
            toolsets = call_kwargs.kwargs.get("enabled_toolsets")
            assert isinstance(toolsets, list)
            assert len(toolsets) > 0
            assert call_kwargs.kwargs.get("platform") == "api_server"

