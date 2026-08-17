"""Tests for tools.tool_result_profiles (tool-aware relevance filtering).

Covers:
1. Defaults: DEFAULT_PROFILES mirrors DEFAULT_CONFIG; fallback when config
   read fails.
2. Config resolution: user overrides, disabled, unknown mode -> full,
   malformed section fallback.
3. Mode behavior:
   - bounded_matches (search_files): verbose ``matches`` array trimming,
     densified ``matches_text`` trimming, hint-suffix preservation,
     small-result passthrough, error-JSON passthrough.
   - tail_or_head (read_file): large pages keep head + tail, small pages
     pass through, JSON error results pass through.
   - summary (patch/write_file): compact envelope kept, verbose keys
     dropped, no-op when nothing would be dropped, non-JSON passthrough.
   - smart_tail (terminal): output field trimmed, metadata intact,
     small output no-op, non-JSON passthrough.
4. Fail-open contract: unknown tools, disabled config, non-string content,
   and malformed input never raise and never mutate the original.
5. Config escapes: per-tool ``middle_summary_lines`` for the densified text
   form (trimmed by lines, counted as lines) and ``keep_keys``/``deny_keys``
   for the summary envelope.
6. Executor ordering: the filter runs before the size caps/persistence, so
   persistence always receives the already-filtered result.
"""

from __future__ import annotations

import json

from unittest.mock import MagicMock, patch

import pytest

from tools import tool_result_profiles as trp


@pytest.fixture(autouse=True)
def _reset_profiles_cache():
    """get_tool_result_profiles() memoizes for the process lifetime, so each
    test must start from a clean cache to observe the config it patches in."""
    trp._reset_tool_result_profiles_cache()
    yield
    trp._reset_tool_result_profiles_cache()


def _cfg_with(tool_profiles: dict) -> dict:
    return {"tool_result_profiles": tool_profiles}


def _default_cfg() -> dict:
    """Full config dict (root key included) with a bounded search profile."""
    return {
        "tool_result_profiles": {
            "enabled": True,
            "tools": {
                "search_files": {
                    "mode": "bounded_matches",
                    "first_matches": 2,
                    "last_matches": 2,
                },
            },
        },
    }


class TestDefaults:
    def test_default_profiles_mirror_default_config(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert "tool_result_profiles" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["tool_result_profiles"] == trp.DEFAULT_PROFILES

    def test_get_profiles_returns_defaults_when_load_config_raises(self):
        def _boom():
            raise RuntimeError("boom")

        with patch("hermes_cli.config.load_config", side_effect=_boom):
            profiles = trp.get_tool_result_profiles()
        assert profiles["enabled"] is True
        assert profiles["tools"]["search_files"]["mode"] == "bounded_matches"


class TestConfigResolution:
    def test_user_config_overrides_mode(self):
        cfg = _cfg_with({"enabled": True, "tools": {
            "search_files": {"mode": "bounded_matches", "first_matches": 1, "last_matches": 1},
        }})
        with patch("hermes_cli.config.load_config", return_value=cfg):
            profiles = trp.get_tool_result_profiles()
        assert profiles["tools"]["search_files"]["first_matches"] == 1
        assert profiles["tools"]["search_files"]["last_matches"] == 1

    def test_disabled_passes_everything_through(self):
        cfg = _cfg_with({"enabled": False, "tools": {"search_files": {"mode": "bounded_matches"}}})
        content = json.dumps({
            "total_count": 20,
            "matches": [{"path": f"f{i}.py", "line": i, "content": "x"} for i in range(20)],
        })
        with patch("hermes_cli.config.load_config", return_value=cfg):
            out = trp.apply_tool_result_filter("search_files", content)
        assert out == content

    def test_unknown_mode_coerces_to_full(self):
        cfg = _cfg_with({"enabled": True, "tools": {"search_files": {"mode": "teleport"}}})
        content = "plain text that must pass through unchanged"
        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert trp.apply_tool_result_filter("search_files", content) == content

    def test_section_not_a_dict_falls_back_to_defaults(self):
        cfg = {"tool_result_profiles": "nonsense"}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            profiles = trp.get_tool_result_profiles()
        assert profiles["enabled"] is True
        assert profiles["tools"]["search_files"]["mode"] == "bounded_matches"

    def test_unknown_tool_passes_through(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            assert trp.apply_tool_result_filter("some_other_tool", "anything") == "anything"

    def test_non_string_content_passes_through(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            assert trp.apply_tool_result_filter("search_files", None) is None
            assert trp.apply_tool_result_filter("search_files", {"a": 1}) == {"a": 1}


class TestBoundedMatches:
    def _search_content(self, n: int, densify: bool = False) -> str:
        if densify:
            data = {
                "total_count": n,
                "matches_format": "path-grouped",
                "matches_text": "\n".join(
                    ["src/a.py", *[f"  {i}: line {i}" for i in range(n)]]
                ),
            }
        else:
            data = {
                "total_count": n,
                "matches": [
                    {"path": "src/a.py", "line": i, "content": f"line {i}"}
                    for i in range(n)
                ],
            }
        return json.dumps(data, ensure_ascii=False)

    def test_verbose_matches_array_is_bounded(self):
        content = self._search_content(20)
        with patch("hermes_cli.config.load_config", return_value=_default_cfg()):
            out = trp.apply_tool_result_filter("search_files", content)
        data = json.loads(out)
        assert len(data["matches"]) == 4  # first 2 + last 2
        assert data["matches"][0]["line"] == 0
        assert data["matches"][-1]["line"] == 19
        assert data["truncated"] is True
        assert data["_relevance"]["omitted"] == 16
        assert data["total_count"] == 20

    def test_densified_matches_text_is_bounded_with_note(self):
        content = self._search_content(12, densify=True)
        with patch("hermes_cli.config.load_config", return_value=_default_cfg()):
            out = trp.apply_tool_result_filter("search_files", content)
        data = json.loads(out)
        text = data["matches_text"]
        assert text.startswith("src/a.py")
        assert "9 additional lines omitted" in text
        assert "  11: line 11" in text  # last match kept
        assert data["truncated"] is True
        assert data["_relevance"]["omitted_lines"] == 9

    def test_densified_note_uses_middle_summary_lines_template(self):
        cfg = _cfg_with({"enabled": True, "tools": {
            "search_files": {
                "mode": "bounded_matches",
                "first_matches": 2,
                "last_matches": 2,
                "middle_summary_lines": "{omitted} lines hidden — page with offset",
            },
        }})
        content = self._search_content(12, densify=True)
        with patch("hermes_cli.config.load_config", return_value=cfg):
            out = trp.apply_tool_result_filter("search_files", content)
        data = json.loads(out)
        assert "9 lines hidden — page with offset" in data["matches_text"]

    def test_custom_tool_without_defaults_uses_provided_lines_template(self):
        cfg = _cfg_with({"enabled": True, "tools": {
            "my_search": {
                "mode": "bounded_matches",
                "first_matches": 2,
                "last_matches": 2,
                "middle_summary_lines": "{omitted} lines hidden — page with offset",
            },
        }})
        content = json.dumps({"matches_text": "\n".join(f"l{i}" for i in range(12))})
        with patch("hermes_cli.config.load_config", return_value=cfg):
            out = trp.apply_tool_result_filter("my_search", content)
        data = json.loads(out)
        assert "8 lines hidden — page with offset" in data["matches_text"]

    def test_small_result_unchanged(self):
        content = self._search_content(3)
        with patch("hermes_cli.config.load_config", return_value=_default_cfg()):
            assert trp.apply_tool_result_filter("search_files", content) == content

    def test_hint_suffix_is_preserved(self):
        content = self._search_content(20) + "\n\n[Hint: Results truncated. Use offset=10 to see more.]"
        with patch("hermes_cli.config.load_config", return_value=_default_cfg()):
            out = trp.apply_tool_result_filter("search_files", content)
        assert out.endswith("[Hint: Results truncated. Use offset=10 to see more.]")
        json.loads(out.split("\n\n[Hint:")[0])  # JSON part still parseable

    def test_error_json_passes_through(self):
        content = json.dumps({"error": "Path not found: /nope"})
        with patch("hermes_cli.config.load_config", return_value=_default_cfg()):
            assert trp.apply_tool_result_filter("search_files", content) == content

    def test_non_json_passes_through(self):
        content = "plain text that is not JSON"
        with patch("hermes_cli.config.load_config", return_value=_default_cfg()):
            assert trp.apply_tool_result_filter("search_files", content) == content

    def test_zero_keep_config_is_noop(self):
        cfg = _cfg_with({"enabled": True, "tools": {
            "search_files": {"mode": "bounded_matches", "first_matches": 0, "last_matches": 0},
        }})
        content = self._search_content(20)
        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert trp.apply_tool_result_filter("search_files", content) == content


class TestTailOrHead:
    def _big_page(self, n: int = 500) -> str:
        return "\n".join(f"{i}|line of content {i}" for i in range(1, n + 1))

    def _cfg(self, **overrides) -> dict:
        base = {
            "enabled": True,
            "tools": {
                "read_file": {
                    "mode": "tail_or_head",
                    "head_lines": 5,
                    "tail_lines": 5,
                    "full_if_under_chars": 4000,
                    **overrides,
                },
            },
        }
        return _cfg_with(base)

    def test_large_page_keeps_head_and_tail(self):
        content = self._big_page(500)
        with patch("hermes_cli.config.load_config", return_value=self._cfg()):
            out = trp.apply_tool_result_filter("read_file", content)
        lines = out.split("\n")
        assert lines[0] == "1|line of content 1"
        assert lines[-1] == "500|line of content 500"
        assert "490 lines omitted" in out
        assert len(lines) == 11  # 5 head + note + 5 tail

    def test_small_read_passes_through(self):
        content = self._big_page(20)
        assert len(content) < 4000
        with patch("hermes_cli.config.load_config", return_value=self._cfg()):
            assert trp.apply_tool_result_filter("read_file", content) == content

    def test_json_error_result_passes_through(self):
        content = json.dumps({"success": False, "note": "not a regular file"})
        with patch("hermes_cli.config.load_config", return_value=self._cfg(full_if_under_chars=0)):
            assert trp.apply_tool_result_filter("read_file", content) == content


class TestSummary:
    def _cfg(self) -> dict:
        return _cfg_with({"enabled": True, "tools": {
            "patch": {"mode": "summary"},
            "write_file": {"mode": "summary"},
        }})

    def test_dropped_keys_are_removed(self):
        content = json.dumps({
            "success": True,
            "files_modified": ["src/a.py"],
            "resolved_path": "/work/src/a.py",
            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
        })
        with patch("hermes_cli.config.load_config", return_value=self._cfg()):
            out = trp.apply_tool_result_filter("patch", content)
        data = json.loads(out)
        assert data["success"] is True
        assert data["files_modified"] == ["src/a.py"]
        assert "diff" not in data
        assert data["_relevance"]["dropped_keys"] == ["diff"]

    def test_already_compact_json_unchanged(self):
        content = json.dumps({"success": True, "files_modified": ["src/a.py"]})
        with patch("hermes_cli.config.load_config", return_value=self._cfg()):
            assert trp.apply_tool_result_filter("write_file", content) == content

    def test_non_json_passes_through(self):
        content = "Some plain text confirmation"
        with patch("hermes_cli.config.load_config", return_value=self._cfg()):
            assert trp.apply_tool_result_filter("write_file", content) == content

    def test_keep_keys_preserve_new_fields(self):
        cfg = _cfg_with({"enabled": True, "tools": {
            "patch": {
                "mode": "summary",
                "keep_keys": ["conflicts"],
                "deny_keys": [],
            },
        }})
        content = json.dumps({
            "success": True,
            "files_modified": ["src/a.py"],
            "conflicts": ["src/b.py"],
            "diff": "--- a\n+++ b\n",
        })
        with patch("hermes_cli.config.load_config", return_value=cfg):
            out = trp.apply_tool_result_filter("patch", content)
        data = json.loads(out)
        assert data["success"] is True
        assert data["conflicts"] == ["src/b.py"]  # new field surfaced via config
        assert "diff" not in data
        assert data["_relevance"]["dropped_keys"] == ["diff"]

    def test_deny_keys_drop_default_kept_keys(self):
        cfg = _cfg_with({"enabled": True, "tools": {
            "patch": {
                "mode": "summary",
                "keep_keys": [],
                "deny_keys": ["status", "files_modified"],
            },
        }})
        content = json.dumps({
            "success": True,
            "status": "applied",
            "files_modified": ["src/a.py"],
            "diff": "--- a\n+++ b\n",
        })
        with patch("hermes_cli.config.load_config", return_value=cfg):
            out = trp.apply_tool_result_filter("patch", content)
        data = json.loads(out)
        assert data["success"] is True
        assert "status" not in data
        assert "files_modified" not in data
        assert data["_relevance"]["dropped_keys"] == ["diff", "files_modified", "status"]

    def test_garbage_keep_deny_entries_are_ignored(self):
        cfg = _cfg_with({"enabled": True, "tools": {
            "write_file": {
                "mode": "summary",
                "keep_keys": "not-a-list",
                "deny_keys": [123, None],
            },
        }})
        content = json.dumps({"success": True, "diff": "--- a\n+++ b\n"})
        with patch("hermes_cli.config.load_config", return_value=cfg):
            out = trp.apply_tool_result_filter("write_file", content)
        data = json.loads(out)
        assert data["success"] is True
        assert "diff" not in data  # built-in defaults still apply


class TestSmartTail:
    def _cfg(self) -> dict:
        return _cfg_with({"enabled": True, "tools": {
            "terminal": {"mode": "smart_tail", "head_lines": 2, "tail_lines": 2},
        }})

    def _terminal_content(self, n: int = 100, exit_code: int = 0) -> str:
        return json.dumps({
            "output": "\n".join(f"out line {i}" for i in range(n)),
            "exit_code": exit_code,
            "output_total_chars": 10_000,
        }, ensure_ascii=False)

    def test_output_field_is_trimmed_metadata_kept(self):
        content = self._terminal_content(100)
        with patch("hermes_cli.config.load_config", return_value=self._cfg()):
            out = trp.apply_tool_result_filter("terminal", content)
        data = json.loads(out)
        lines = data["output"].split("\n")
        assert lines[0] == "out line 0"
        assert lines[-1] == "out line 99"
        assert "96 lines of output omitted" in data["output"]
        assert data["exit_code"] == 0
        assert data["output_total_chars"] == 10_000
        assert data["relevance_note"] == "96 lines trimmed from output"

    def test_small_output_unchanged(self):
        content = self._terminal_content(3)
        with patch("hermes_cli.config.load_config", return_value=self._cfg()):
            assert trp.apply_tool_result_filter("terminal", content) == content

    def test_non_json_output_passes_through(self):
        content = "just raw output text"
        with patch("hermes_cli.config.load_config", return_value=self._cfg()):
            assert trp.apply_tool_result_filter("terminal", content) == content

    def test_error_exit_code_still_trimmed(self):
        content = self._terminal_content(50, exit_code=1)
        with patch("hermes_cli.config.load_config", return_value=self._cfg()):
            out = trp.apply_tool_result_filter("terminal", content)
        data = json.loads(out)
        assert data["exit_code"] == 1
        assert "46 lines of output omitted" in data["output"]


class TestFailOpen:
    @pytest.mark.parametrize("mode", ["bounded_matches", "tail_or_head", "summary", "smart_tail"])
    def test_never_raises_on_garbage_input(self, mode):
        cfg = _cfg_with({"enabled": True, "tools": {"t": {"mode": mode}}})
        garbage = ["", "{", "not json at all", "[", "\x00\x01\x02"]
        with patch("hermes_cli.config.load_config", return_value=cfg):
            for g in garbage:
                assert trp.apply_tool_result_filter("t", g) == g

    def test_config_read_failure_still_filters_with_defaults(self):
        def _boom():
            raise RuntimeError("boom")

        content = json.dumps({
            "total_count": 20,
            "matches": [{"path": "a.py", "line": i, "content": "x"} for i in range(20)],
        })
        with patch("hermes_cli.config.load_config", side_effect=_boom):
            out = trp.apply_tool_result_filter("search_files", content)
        data = json.loads(out)
        assert len(data["matches"]) == 10  # defaults: first 5 + last 5


# ---------------------------------------------------------------------------
# Executor wiring: the filter must actually run between tool dispatch and
# context injection, on the real sequential executor surface.
# ---------------------------------------------------------------------------
def _make_executor_agent():
    from pathlib import Path
    import tempfile
    from run_agent import AIAgent

    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-test-home-"))
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=[{
                "type": "function",
                "function": {
                    "name": "search_files",
                    "description": "search",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


class TestExecutorWiring:
    """The sequential executor is the real injection surface — verify the
    filter is applied to results before they become tool messages."""

    def _big_search_result(self) -> str:
        return json.dumps({
            "total_count": 20,
            "matches": [
                {"path": "src/a.py", "line": i, "content": f"line {i}"}
                for i in range(20)
            ],
        }, ensure_ascii=False)

    def _run_sequential(self, agent, function_name, result, profiles_cfg, persist_hook=None):
        from types import SimpleNamespace

        tool_call = SimpleNamespace(
            id="call_1",
            type="function",
            function=SimpleNamespace(name=function_name, arguments="{}"),
        )
        messages: list = []
        assistant_message = SimpleNamespace(content="", tool_calls=[tool_call])

        def _fake_persist(**kwargs):
            if persist_hook is not None:
                persist_hook(kwargs["content"])
            return kwargs["content"]

        with (
            patch("run_agent.handle_function_call", return_value=result),
            patch(
                "agent.tool_executor.maybe_persist_tool_result",
                side_effect=_fake_persist,
            ),
            patch("hermes_cli.config.load_config", return_value=profiles_cfg),
        ):
            agent._execute_tool_calls_sequential(
                assistant_message, messages, "task-1"
            )
        return messages

    def test_search_result_is_bounded_before_context_injection(self):
        agent = _make_executor_agent()
        cfg = {"tool_result_profiles": {"enabled": True, "tools": {
            "search_files": {"mode": "bounded_matches", "first_matches": 2, "last_matches": 2},
        }}}
        messages = self._run_sequential(
            agent, "search_files", self._big_search_result(), cfg
        )
        assert messages and messages[-1]["role"] == "tool"
        data = json.loads(messages[-1]["content"])
        assert len(data["matches"]) == 4
        assert data["matches"][0]["line"] == 0
        assert data["matches"][-1]["line"] == 19
        assert data["truncated"] is True

    def test_filter_runs_before_size_caps_and_persistence(self):
        """The relevance filter must shrink the result BEFORE persistence
        sees it — otherwise an oversized result is replaced by a preview
        blob (no longer JSON) and the filter fails open, i.e. it is a no-op
        exactly for the results it exists for."""
        agent = _make_executor_agent()
        cfg = {"tool_result_profiles": {"enabled": True, "tools": {
            "search_files": {"mode": "bounded_matches", "first_matches": 2, "last_matches": 2},
        }}}
        seen_by_persist: list = []

        def _capture(content: str) -> None:
            seen_by_persist.append(content)

        self._run_sequential(
            agent, "search_files", self._big_search_result(), cfg, persist_hook=_capture
        )
        assert len(seen_by_persist) == 1
        data = json.loads(seen_by_persist[0])
        assert len(data["matches"]) == 4  # persistence received the filtered form

    def test_filter_skipped_when_disabled(self):
        agent = _make_executor_agent()
        cfg = {"tool_result_profiles": {"enabled": False, "tools": {
            "search_files": {"mode": "bounded_matches", "first_matches": 2, "last_matches": 2},
        }}}
        messages = self._run_sequential(
            agent, "search_files", self._big_search_result(), cfg
        )
        data = json.loads(messages[-1]["content"])
        assert len(data["matches"]) == 20  # untouched