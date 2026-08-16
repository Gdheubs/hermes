from hermes_cli.oneshot import _normalize_toolsets, _validate_explicit_toolsets
import model_tools


def test_none_toolset_is_an_explicit_empty_tool_list():
    toolsets, error = _validate_explicit_toolsets("none")
    assert error is None
    assert toolsets == []


def test_explicit_empty_tool_list_survives_agent_construction_normalization():
    assert _normalize_toolsets([]) == []
    assert _normalize_toolsets(None) is None


def test_explicit_empty_tool_list_is_not_widened_by_kanban_context(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "tool-free-proof")
    monkeypatch.setattr(model_tools, "_is_delegated_child_context", lambda: False)
    monkeypatch.setattr(model_tools, "_is_dispatcher_owned_worker", lambda: True)
    model_tools._clear_tool_defs_cache()
    try:
        assert model_tools.get_tool_definitions(enabled_toolsets=[], quiet_mode=True) == []
    finally:
        model_tools._clear_tool_defs_cache()
