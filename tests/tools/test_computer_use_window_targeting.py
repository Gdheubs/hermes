"""Regression coverage for exact native-window targeting in computer_use."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.computer_use.backend import ActionResult, CaptureResult


def _window(
    window_id: int,
    *,
    pid: int = 73384,
    title: str = "Mission Control",
    on_screen: bool = True,
    width: float = 1906,
    height: float = 960,
    z_index: int = 45,
):
    return {
        "app_name": "Safari",
        "pid": pid,
        "window_id": window_id,
        "is_on_screen": on_screen,
        "title": title,
        "z_index": z_index,
        "layer": 0,
        "bounds": {"x": 1, "y": 30, "width": width, "height": height},
    }


def _backend_with_windows(windows):
    from tools.computer_use.cua_backend import CuaDriverBackend

    backend = CuaDriverBackend()
    session = MagicMock()
    session.capabilities_discovered = True
    session._has_tool.return_value = False
    session.supports_capability.return_value = False
    session.supports_input_property.return_value = False

    def call_tool(name, args, *unused_args, **unused_kwargs):
        if name == "list_windows":
            requested_pid = args.get("pid")
            listed = [w for w in windows if requested_pid is None or w["pid"] == requested_pid]
            return {
                "data": "",
                "images": [],
                "structuredContent": {"windows": listed},
                "isError": False,
            }
        if name == "get_window_state":
            return {
                "data": '0 elements\n- [0] AXWindow "Mission Control"',
                "images": [],
                "structuredContent": {"elements": []},
                "isError": False,
            }
        return {"data": "ok", "images": [], "structuredContent": {}, "isError": False}

    session.call_tool.side_effect = call_tool
    backend._session = session
    return backend


@pytest.fixture(autouse=True)
def _reset_tool_backend():
    from tools.computer_use.tool import reset_backend_for_tests

    reset_backend_for_tests()
    yield
    reset_backend_for_tests()


def test_capture_result_carries_exact_native_target_in_text_and_multimodal_responses():
    from tools.computer_use import tool as cu_tool

    cap = CaptureResult(
        mode="ax",
        width=100,
        height=100,
        app="Safari",
        window_title="Mission Control",
        pid=73384,
        window_id=754,
    )
    payload = json.loads(cu_tool._capture_response(cap))
    assert payload["pid"] == 73384
    assert payload["window_id"] == 754
    assert "pid=73384 window_id=754" in payload["summary"]

    cap.mode = "vision"
    cap.png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAADUlEQVR4nG"
        "NgGAUgAAABCAABgukLHQAAAABJRU5ErkJggg=="
    )
    with patch.object(cu_tool, "_should_route_through_aux_vision", return_value=False):
        response = cu_tool._capture_response(cap)
    assert response["meta"]["pid"] == 73384
    assert response["meta"]["window_id"] == 754
    assert "pid=73384 window_id=754" in response["text_summary"]


def test_auxiliary_vision_capture_response_carries_exact_native_target(tmp_path):
    from tools.computer_use import tool as cu_tool

    cap = CaptureResult(
        mode="vision",
        width=8,
        height=8,
        png_b64=(
            "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAADUlEQVR4nG"
            "NgGAUgAAABCAABgukLHQAAAABJRU5ErkJggg=="
        ),
        pid=73384,
        window_id=754,
    )
    fake_vision = MagicMock(return_value="<coro>")
    with patch.object(cu_tool, "_should_route_through_aux_vision", return_value=True), \
         patch("hermes_constants.get_hermes_dir", return_value=tmp_path), \
         patch("model_tools._run_async", return_value=json.dumps({"analysis": "synthetic"})), \
         patch("tools.vision_tools.vision_analyze_tool", new=fake_vision):
        response = cu_tool._capture_response(cap)

    payload = json.loads(response)
    assert payload["vision_analysis_routed_via"] == "auxiliary.vision"
    assert payload["pid"] == 73384
    assert payload["window_id"] == 754


def test_capture_after_uses_successful_action_result_target_over_sticky_target():
    from tools.computer_use import tool as cu_tool

    class CaptureTrackingBackend:
        _last_target = {"pid": 73384, "window_id": 754}
        _last_app = "Safari"

        def __init__(self):
            self.capture_calls = []

        def capture(self, **kwargs):
            self.capture_calls.append(kwargs)
            return CaptureResult(
                mode=kwargs["mode"],
                width=100,
                height=100,
                pid=kwargs.get("pid"),
                window_id=kwargs.get("window_id"),
            )

    backend = CaptureTrackingBackend()
    result = ActionResult(
        ok=True,
        action="key",
        meta={"pid": 80000, "window_id": 900},
    )

    cu_tool._maybe_follow_capture(backend, result, True)

    assert backend.capture_calls == [
        {"mode": cu_tool._capture_after_mode(), "pid": 80000, "window_id": 900}
    ]


def test_approval_summary_names_explicit_native_target():
    from tools.computer_use import tool as cu_tool

    summary = cu_tool._summarize_action(
        "key", {"keys": "cmd+t", "pid": 73384, "window_id": 754}
    )

    assert "pid=73384" in summary
    assert "window_id=754" in summary


def test_capture_dispatch_passes_only_target_keywords_actually_specified():
    from tools.computer_use import tool as cu_tool

    class PidAwareLegacyBackend:
        def __init__(self):
            self.pid = None

        def capture(self, mode="som", app=None, *, pid=None):
            self.pid = pid
            return CaptureResult(mode=mode, width=1, height=1)

    backend = PidAwareLegacyBackend()
    cu_tool._dispatch(
        backend,
        "capture",
        {"action": "capture", "mode": "ax", "pid": 73384},
    )

    assert backend.pid == 73384


def test_dispatch_omits_unspecified_target_keywords_for_legacy_backend():
    from tools.computer_use import tool as cu_tool

    class LegacyBackend:
        def key(self, keys, *, delivery_mode=None, bring_to_front=False):
            return ActionResult(ok=True, action="key", message=keys)

    payload = json.loads(
        cu_tool._dispatch(LegacyBackend(), "key", {"action": "key", "keys": "cmd+t"})
    )

    assert payload["ok"] is True


def test_public_dispatch_forwards_explicit_target_to_every_native_action():
    from tools.computer_use import tool as cu_tool

    class TrackingBackend:
        _last_target = None
        _last_app = None

        def __init__(self):
            self.calls = []

        def _record(self, name, **kwargs):
            self.calls.append((name, kwargs))
            return ActionResult(ok=True, action=name)

        def click(self, **kwargs):
            return self._record("click", **kwargs)

        def drag(self, **kwargs):
            return self._record("drag", **kwargs)

        def scroll(self, **kwargs):
            return self._record("scroll", **kwargs)

        def type_text(self, text, **kwargs):
            return self._record("type", text=text, **kwargs)

        def key(self, keys, **kwargs):
            return self._record("key", keys=keys, **kwargs)

        def set_value(self, value, element=None, **kwargs):
            return self._record("set_value", value=value, element=element, **kwargs)

        def focus_app(self, app=None, raise_window=False, **kwargs):
            return self._record(
                "focus_app", app=app, raise_window=raise_window, **kwargs
            )

    backend = TrackingBackend()
    target = {"pid": 73384, "window_id": 754}
    actions = [
        {"action": "click", "coordinate": [1, 2]},
        {
            "action": "double_click",
            "coordinate": [1, 2],
        },
        {"action": "right_click", "coordinate": [1, 2]},
        {"action": "middle_click", "coordinate": [1, 2]},
        {
            "action": "drag",
            "from_coordinate": [1, 2],
            "to_coordinate": [3, 4],
        },
        {"action": "scroll", "direction": "down"},
        {"action": "type", "text": "synthetic"},
        {"action": "key", "keys": "cmd+t"},
        {"action": "set_value", "element": 1, "value": "synthetic"},
        {"action": "focus_app"},
    ]

    for action in actions:
        result = cu_tool._dispatch(backend, action["action"], {**action, **target})
        assert "error" not in json.loads(result), result

    assert len(backend.calls) == len(actions)
    for _name, kwargs in backend.calls:
        assert kwargs["pid"] == 73384
        assert kwargs["window_id"] == 754


def test_app_capture_ignores_hidden_and_zero_size_technical_windows():
    technical = [
        _window(1331, on_screen=False, width=53, height=48, z_index=78),
        _window(1296, on_screen=False, width=0, height=0, z_index=75),
        _window(758, on_screen=False, width=699, height=337, z_index=46),
    ]
    backend = _backend_with_windows([*technical, _window(754)])

    cap = backend.capture(mode="ax", app="Safari")

    assert cap.pid == 73384
    assert cap.window_id == 754
    assert cap.error is None
    gws = [c for c in backend._session.call_tool.call_args_list if c.args[0] == "get_window_state"]
    assert gws[-1].args[1]["window_id"] == 754


def test_app_capture_with_two_visible_windows_fails_closed_and_lists_choices():
    backend = _backend_with_windows([
        _window(754, title="Mission Control", z_index=45),
        _window(900, title="Documentation", z_index=44),
    ])

    cap = backend.capture(mode="ax", app="Safari")

    assert cap.error == "ambiguous_window_target"
    assert {w["window_id"] for w in cap.available_windows} == {754, 900}
    assert backend._active_pid is None
    assert backend._active_window_id is None
    assert not any(
        c.args[0] == "get_window_state" for c in backend._session.call_tool.call_args_list
    )


def test_explicit_window_id_wins_and_infers_pid():
    backend = _backend_with_windows([
        _window(754, title="Mission Control"),
        _window(1331, on_screen=False, width=53, height=48, z_index=78),
    ])

    cap = backend.capture(mode="ax", app="Wrong App Name", window_id=754)

    assert cap.pid == 73384
    assert cap.window_id == 754
    assert cap.error is None


def test_explicit_hidden_window_id_still_wins_over_visibility_filter():
    backend = _backend_with_windows([
        _window(754, title="Mission Control"),
        _window(1331, on_screen=False, width=53, height=48, z_index=78),
    ])

    result = backend.key("cmd+t", window_id=1331)

    assert result.ok
    hotkey_calls = [
        call
        for call in backend._session.call_tool.call_args_list
        if call.args[0] == "hotkey"
    ]
    assert hotkey_calls[-1].args[1]["pid"] == 73384
    assert hotkey_calls[-1].args[1]["window_id"] == 1331


def test_stale_explicit_capture_target_fails_without_falling_back():
    backend = _backend_with_windows([_window(754)])

    cap = backend.capture(mode="ax", pid=73384, window_id=999999)

    assert cap.error == "stale_window_target"
    assert [w["window_id"] for w in cap.available_windows] == [754]
    assert backend._active_pid is None
    assert not any(
        c.args[0] == "get_window_state" for c in backend._session.call_tool.call_args_list
    )


def test_explicit_action_target_overrides_sticky_capture_target_and_reaches_driver():
    backend = _backend_with_windows([
        _window(754, title="Mission Control"),
        _window(900, title="Documentation", pid=80000),
    ])
    backend._active_pid = 73384
    backend._active_window_id = 754

    result = backend.key("cmd+t", pid=80000, window_id=900)

    assert result.ok
    hotkey_calls = [c for c in backend._session.call_tool.call_args_list if c.args[0] == "hotkey"]
    assert hotkey_calls[-1].args[1]["pid"] == 80000
    assert hotkey_calls[-1].args[1]["window_id"] == 900


def test_stale_explicit_action_target_fails_without_native_action_or_retarget():
    backend = _backend_with_windows([_window(754)])
    backend._active_pid = 73384
    backend._active_window_id = 754

    result = backend.key("cmd+t", pid=73384, window_id=999999)

    assert not result.ok
    assert result.code == "stale_window_target"
    assert result.meta["available_windows"][0]["window_id"] == 754
    assert not any(c.args[0] == "hotkey" for c in backend._session.call_tool.call_args_list)
    assert backend._active_window_id == 754


def test_pid_only_action_with_multiple_visible_windows_fails_closed():
    backend = _backend_with_windows([
        _window(754, title="Mission Control"),
        _window(900, title="Documentation"),
        _window(1331, on_screen=False, width=53, height=48),
    ])

    result = backend.key("cmd+t", pid=73384)

    assert not result.ok
    assert result.code == "ambiguous_window_target"
    assert {w["window_id"] for w in result.meta["available_windows"]} == {754, 900}
    assert not any(c.args[0] == "hotkey" for c in backend._session.call_tool.call_args_list)


def test_sticky_target_is_revalidated_and_closed_window_fails_closed():
    backend = _backend_with_windows([_window(900, title="Documentation")])
    backend._active_pid = 73384
    backend._active_window_id = 754

    result = backend.key("cmd+t")

    assert not result.ok
    assert result.code == "stale_window_target"
    assert not any(
        call.args[0] == "hotkey"
        for call in backend._session.call_tool.call_args_list
    )
    assert any(
        call.args[0] == "list_windows"
        for call in backend._session.call_tool.call_args_list
    )


def test_sticky_target_pair_mismatch_fails_closed_after_window_id_recycling():
    backend = _backend_with_windows([
        _window(754, pid=80000, title="Recycled Native ID"),
        _window(900, pid=73384, title="Sibling"),
    ])
    backend._active_pid = 73384
    backend._active_window_id = 754

    result = backend.key("cmd+t")

    assert not result.ok
    assert result.code == "stale_window_target"
    assert not any(
        call.args[0] == "hotkey"
        for call in backend._session.call_tool.call_args_list
    )


def test_focus_app_explicit_target_performs_one_exact_window_lookup():
    backend = _backend_with_windows([
        _window(754, title="Mission Control"),
        _window(900, title="Documentation"),
    ])

    result = backend.focus_app(pid=73384, window_id=754)

    assert result.ok
    list_calls = [
        call
        for call in backend._session.call_tool.call_args_list
        if call.args[0] == "list_windows"
    ]
    assert len(list_calls) == 1
    assert list_calls[0].args[1]["on_screen_only"] is False


def test_focus_app_with_multiple_visible_windows_fails_closed():
    backend = _backend_with_windows([
        _window(754, title="Mission Control"),
        _window(900, title="Documentation"),
        _window(1331, on_screen=False, width=53, height=48),
    ])

    result = backend.focus_app("Safari")

    assert not result.ok
    assert result.code == "ambiguous_window_target"
    assert {w["window_id"] for w in result.meta["available_windows"]} == {754, 900}
    assert backend._active_window_id is None


def test_captured_target_is_reused_exactly_when_followup_omits_target():
    backend = _backend_with_windows([_window(754)])
    cap = backend.capture(mode="ax", app="Safari")
    assert cap.window_id == 754

    result = backend.key("cmd+t")

    assert result.ok
    hotkey_calls = [c for c in backend._session.call_tool.call_args_list if c.args[0] == "hotkey"]
    assert hotkey_calls[-1].args[1]["pid"] == 73384
    assert hotkey_calls[-1].args[1]["window_id"] == 754
