"""Behavior contracts for operator-approved delegation route aliases."""

import json
import threading
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _schema_parent_agent, delegate_task
from tools.registry import registry


def _parent(provider="nous", model="hermes-4-405b"):
    parent = MagicMock()
    parent.provider = provider
    parent.model = model
    parent.base_url = "https://parent.example/v1"
    parent.api_key = "parent-secret"
    parent.api_mode = "chat_completions"
    parent.platform = "cli"
    parent.enabled_toolsets = ["delegation"]
    parent.disabled_toolsets = []
    parent.valid_tool_names = {"delegate_task"}
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent.provider_require_parameters = False
    parent.provider_data_collection = ""
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.session_id = "parent-session"
    return parent


def _runtime(provider, model):
    return {
        "provider": provider,
        "model": model,
        "base_url": f"https://{provider}.example/v1",
        "api_key": f"{provider}-operator-secret",
        "api_mode": "chat_completions",
        "request_overrides": {},
    }


def _completed_child(model):
    child = MagicMock()
    child.model = model
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "api_calls": 1,
        "messages": [],
    }
    return child


def test_schema_describes_effective_route_aliases_and_security_boundary():
    cfg = {
        "provider": "openrouter",
        "model": "global-worker",
        "routes": {
            "grok": {"provider": "openrouter", "model": "x-ai/grok-4.6"},
            "unsafe": {
                "provider": "openrouter",
                "model": "hidden",
                "api_key": "must-not-appear",
            },
        },
    }
    parent = _parent()
    token = _schema_parent_agent.set(parent)
    try:
        with patch("tools.delegate_tool._load_config", return_value=cfg):
            schema = registry.get_definitions({"delegate_task"})[0]["function"]
    finally:
        _schema_parent_agent.reset(token)

    text = schema["description"]
    props = schema["parameters"]["properties"]
    assert "provider 'openrouter', model 'global-worker'" in text
    assert "do not inspect or modify config" in text.lower()
    assert "per-call raw routing is unsupported" in text
    assert props["route"]["enum"] == ["grok"]
    assert props["tasks"]["items"]["properties"]["route"]["enum"] == ["grok"]
    assert "must-not-appear" not in json.dumps(schema)
    for forbidden in ("provider", "model", "base_url", "api_key"):
        assert forbidden not in props
        assert forbidden not in props["tasks"]["items"]["properties"]


def test_schema_config_only_distinguishes_configured_and_inherited_parts():
    cfg = {"model": "configured-worker", "routes": {}}
    with patch("tools.delegate_tool._load_config", return_value=cfg):
        schema = registry.get_definitions({"delegate_task"})[0]["function"]

    text = schema["description"]
    assert "inherited provider, model 'configured-worker'" in text
    assert "identity is not available" in text
    assert "nous" not in text


def test_unknown_alias_fails_before_any_child_is_built_and_lists_safe_names():
    cfg = {
        "routes": {
            "grok": {"provider": "openrouter", "model": "x-ai/grok-4.6"},
            "unsafe": {"provider": "openrouter", "model": "x", "api_key": "secret"},
        }
    }
    with (
        patch("tools.delegate_tool._load_config", return_value=cfg),
        patch("tools.delegate_tool._build_child_preserving_parent_tools") as build,
    ):
        result = json.loads(delegate_task(goal="research", route="missing", parent_agent=_parent()))

    assert "Unknown or disallowed" in result["error"]
    assert "Safe route aliases: grok" in result["error"]
    assert "unsafe" not in result["error"]
    build.assert_not_called()


def test_known_alias_uses_operator_credentials_not_parent_or_model_secrets():
    cfg = {
        "routes": {
            "grok": {
                "provider": "openrouter",
                "model": "x-ai/grok-4.6",
                "reasoning_effort": "high",
                "max_output_tokens": 12000,
                "request_overrides": {"temperature": 0},
            }
        }
    }
    parent = _parent()
    with (
        patch("tools.delegate_tool._load_config", return_value=cfg),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_runtime("openrouter", "x-ai/grok-4.6")),
        patch("run_agent.AIAgent", return_value=_completed_child("x-ai/grok-4.6")) as agent_cls,
    ):
        delegate_task(goal="research", route="grok", parent_agent=parent)

    kwargs = agent_cls.call_args.kwargs
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "x-ai/grok-4.6"
    assert kwargs["api_key"] == "openrouter-operator-secret"
    assert kwargs["api_key"] != parent.api_key
    assert kwargs["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert kwargs["max_tokens"] == 12000
    assert kwargs["request_overrides"] == {"temperature": 0}


def test_alias_rejects_invalid_reasoning_before_child_construction():
    cfg = {
        "routes": {
            "broken": {
                "provider": "openrouter",
                "model": "x-ai/grok-4.6",
                "reasoning_effort": "impossibly-deep",
            }
        }
    }
    with (
        patch("tools.delegate_tool._load_config", return_value=cfg),
        patch("tools.delegate_tool._build_child_preserving_parent_tools") as build,
    ):
        result = json.loads(delegate_task(goal="research", route="broken", parent_agent=_parent()))

    assert "reasoning_effort" in result["error"]
    build.assert_not_called()


def test_alias_rejects_credential_fields_nested_in_request_overrides():
    cfg = {
        "routes": {
            "unsafe": {
                "provider": "openrouter",
                "model": "x-ai/grok-4.6",
                "request_overrides": {
                    "headers": {"Authorization": "Bearer route-secret"}
                },
            }
        }
    }
    with (
        patch("tools.delegate_tool._load_config", return_value=cfg),
        patch("tools.delegate_tool._build_child_preserving_parent_tools") as build,
    ):
        result = json.loads(delegate_task(goal="research", route="unsafe", parent_agent=_parent()))

    assert "request_overrides" in result["error"]
    assert "authorization" in result["error"].lower()
    build.assert_not_called()


def test_alias_rejects_non_inference_request_override_fields():
    cfg = {
        "routes": {
            "unsafe": {
                "provider": "openrouter",
                "model": "x-ai/grok-4.6",
                "request_overrides": {"extra_body": {"trace": "private"}},
            }
        }
    }

    with patch("tools.delegate_tool._load_config", return_value=cfg):
        result = json.loads(delegate_task(goal="research", route="unsafe", parent_agent=_parent()))

    assert "request_overrides" in result["error"]
    assert "extra_body" in result["error"]


def test_alias_rejects_credential_bearing_base_url():
    cfg = {
        "routes": {
            "unsafe": {
                "provider": "custom:gateway",
                "model": "worker",
                "base_url": "https://user:password@gateway.example/v1?token=secret",
            }
        }
    }

    with patch("tools.delegate_tool._load_config", return_value=cfg):
        result = json.loads(delegate_task(goal="research", route="unsafe", parent_agent=_parent()))

    assert "base_url" in result["error"]
    assert "credentials" in result["error"]


def test_batch_routes_resolve_independently_with_task_precedence():
    cfg = {
        "provider": "nous",
        "model": "global-worker",
        "routes": {
            "fast": {"provider": "openrouter", "model": "fast-model"},
            "careful": {"provider": "anthropic", "model": "careful-model"},
        },
    }

    def resolve(requested, target_model):
        return _runtime(requested, target_model)

    built = []

    def capture_build(**kwargs):
        built.append(kwargs)
        return _completed_child(kwargs["model"])

    tasks = [
        {"goal": "Investigate the first module carefully"},
        {"goal": "Investigate the second module carefully", "route": "careful"},
        {"goal": "Investigate the third module carefully", "route": "fast"},
    ]
    with (
        patch("tools.delegate_tool._load_config", return_value=cfg),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolve),
        patch("tools.delegate_tool._build_child_preserving_parent_tools", side_effect=capture_build),
        patch("tools.delegate_tool._run_single_child", side_effect=lambda task_index, goal, child, parent_agent, **kw: {
            "task_index": task_index,
            "status": "completed",
            "summary": "ok",
            "api_calls": 1,
            "duration_seconds": 0,
        }),
    ):
        delegate_task(tasks=tasks, route="fast", parent_agent=_parent())

    assert [(item["override_provider"], item["model"]) for item in built] == [
        ("openrouter", "fast-model"),
        ("anthropic", "careful-model"),
        ("openrouter", "fast-model"),
    ]
