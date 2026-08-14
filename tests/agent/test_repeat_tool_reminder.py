"""Unit tests for the advisory repeat-tool-reminder guard
(``agent/repeat_tool_reminder.py``).

Covers: argument canonicalization (deep key sort), thresholds and tone
escalation, reset on real user messages, include/exclude wildcards, chain
semantics (a different call resets the run), and the advisory contract
(never raises, never blocks).
"""

import threading

import pytest

from agent.repeat_tool_reminder import (
    DEFAULT_PREVIEW_CHARS,
    DEFAULT_THRESHOLDS,
    canonicalize_arguments,
    detailed_reminder,
    gentle_reminder,
    maybe_remind,
    reset,
    wildcard_to_regex,
)

DEFAULT_CFG = {
    "enabled": True,
    "thresholds": list(DEFAULT_THRESHOLDS),
    "include": [],
    "exclude": [],
    "arguments_preview_chars": DEFAULT_PREVIEW_CHARS,
}


def _agent():
    """A minimal stand-in for the AIAgent: chain state is an attribute."""
    return type("FakeAgent", (), {})()


# =========================================================================
# Canonicalization (deep key sort)
# =========================================================================


class TestCanonicalization:
    def test_property_order_is_irrelevant(self):
        a = {"z": 1, "a": {"d": 2, "c": {"q": 1, "p": 2}}, "m": [{"y": 1, "x": 2}]}
        b = {"a": {"c": {"p": 2, "q": 1}, "d": 2}, "m": [{"x": 2, "y": 1}], "z": 1}
        assert canonicalize_arguments(a) == canonicalize_arguments(b)

    def test_nested_lists_of_dicts_are_sorted(self):
        a = {"items": [{"b": 1, "a": 2}, {"d": 1, "c": 2}]}
        b = {"items": [{"a": 2, "b": 1}, {"c": 2, "d": 1}]}
        assert canonicalize_arguments(a) == canonicalize_arguments(b)

    def test_different_values_differ(self):
        assert canonicalize_arguments({"a": 1}) != canonicalize_arguments({"a": 2})

    def test_non_mapping_input_canonicalizes_to_empty_object(self):
        # Calls whose arguments could not be parsed as an object share the
        # "{}" identity — repeats of the same malformed call still accumulate.
        assert canonicalize_arguments(None) == canonicalize_arguments({})
        assert canonicalize_arguments("not a dict") == canonicalize_arguments({})

    def test_identity_through_the_chain(self):
        # Two calls that differ only in argument property order advance the
        # SAME chain run (the second call increments, it does not reset).
        agent = _agent()
        assert maybe_remind(agent, "terminal", {"cmd": "ls", "cwd": "/tmp"}, DEFAULT_CFG) is None
        assert maybe_remind(agent, "terminal", {"cwd": "/tmp", "cmd": "ls"}, DEFAULT_CFG) is None
        assert maybe_remind(agent, "terminal", {"cmd": "ls", "cwd": "/tmp"}, DEFAULT_CFG) is not None


# =========================================================================
# Thresholds and tone escalation
# =========================================================================


class TestThresholdsAndEscalation:
    def test_default_thresholds_escalate(self):
        agent = _agent()
        args = {"cmd": "ls"}
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None  # 1
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None  # 2
        third = maybe_remind(agent, "terminal", args, DEFAULT_CFG)         # 3 -> gentle
        assert third == gentle_reminder("terminal", 3)
        assert third.startswith("[reminder]")
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None  # 4
        fifth = maybe_remind(agent, "terminal", args, DEFAULT_CFG)         # 5 -> detailed
        assert fifth == detailed_reminder("terminal", 5, canonicalize_arguments(args))
        assert "consecutive_calls: 5" in fifth
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None  # 6
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None  # 7
        eighth = maybe_remind(agent, "terminal", args, DEFAULT_CFG)        # 8 -> detailed
        assert "consecutive_calls: 8" in eighth

    def test_custom_thresholds_first_is_gentle(self):
        cfg = {**DEFAULT_CFG, "thresholds": [2, 4]}
        agent = _agent()
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None  # 1
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) == gentle_reminder("terminal", 2)
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None  # 3
        detailed = maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg)      # 4
        assert "consecutive_calls: 4" in detailed
        assert detailed != gentle_reminder("terminal", 4)

    def test_detailed_reminder_names_tool_and_arguments(self):
        text = detailed_reminder("read_file", 5, '{"path":"a.txt"}')
        assert '"read_file"' in text
        assert "- arguments: {\"path\":\"a.txt\"}" in text
        assert text.startswith("[reminder] You have called")

    def test_detailed_preview_is_capped(self):
        args = {"payload": "x" * 200}
        canonical = canonicalize_arguments(args)
        text = detailed_reminder("write_file", 5, canonical, preview_chars=32)
        assert "(+%d more chars)" % (len(canonical) - 32) in text
        assert "payload" in text

    def test_preview_within_cap_is_verbatim(self):
        assert detailed_reminder("t", 5, '{"a":1}', preview_chars=50) == detailed_reminder(
            "t", 5, '{"a":1}', preview_chars=DEFAULT_PREVIEW_CHARS
        )

    def test_chain_resets_on_different_arguments(self):
        agent = _agent()
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is None  # 1
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is None  # 2
        assert maybe_remind(agent, "terminal", {"cmd": "pwd"}, DEFAULT_CFG) is None  # different -> 1
        assert maybe_remind(agent, "terminal", {"cmd": "pwd"}, DEFAULT_CFG) is None  # 2
        assert maybe_remind(agent, "terminal", {"cmd": "pwd"}, DEFAULT_CFG) is not None  # 3

    def test_chain_resets_on_different_tool(self):
        agent = _agent()
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is None  # 1
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is None  # 2
        assert maybe_remind(agent, "read_file", {"path": "a"}, DEFAULT_CFG) is None  # different -> 1
        assert maybe_remind(agent, "read_file", {"path": "a"}, DEFAULT_CFG) is None  # 2
        assert maybe_remind(agent, "read_file", {"path": "a"}, DEFAULT_CFG) is not None  # 3

    def test_chain_is_per_agent(self):
        a, b = _agent(), _agent()
        assert maybe_remind(a, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is None
        assert maybe_remind(a, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is None
        assert maybe_remind(b, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is None  # b starts at 1
        assert maybe_remind(a, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is not None  # a hits 3
        assert maybe_remind(b, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is None  # b at 2


# =========================================================================
# Reset on real user messages
# =========================================================================


class TestReset:
    def test_reset_clears_the_chain(self):
        agent = _agent()
        args = {"cmd": "ls"}
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is not None  # 3 -> gentle
        reset(agent)
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None  # back to 1
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None  # 2
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is not None  # 3 again

    def test_reset_is_idempotent_and_safe_on_plain_objects(self):
        agent = _agent()
        reset(agent)
        reset(agent)
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is None

    def test_mid_turn_user_correction_resets_the_chain(self):
        # Wiring check for conversation_loop._apply_active_turn_redirect: a
        # real user correction (steer/interrupt) must clear the chain so
        # repetition across user input is never counted as a loop.
        from types import SimpleNamespace

        from agent.conversation_loop import _apply_active_turn_redirect

        agent = SimpleNamespace(
            _strip_think_blocks=lambda text: text,
            _current_streamed_assistant_text="",
            _stream_needs_break=False,
        )
        # Advance the chain to the gentle threshold.
        args = {"cmd": "ls"}
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is not None

        _apply_active_turn_redirect(agent, [], "please stop and reconsider")

        # After the correction the chain restarts: two more identical calls
        # produce no reminder, the third hits the gentle threshold again.
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is None
        assert maybe_remind(agent, "terminal", args, DEFAULT_CFG) is not None


# =========================================================================
# include / exclude wildcards
# =========================================================================


class TestIncludeExclude:
    def test_include_limits_tracking(self):
        cfg = {**DEFAULT_CFG, "include": ["terminal*"]}
        agent = _agent()
        # read_file is transparent: it neither counts nor resets.
        assert maybe_remind(agent, "read_file", {"path": "a"}, cfg) is None
        assert maybe_remind(agent, "read_file", {"path": "a"}, cfg) is None
        assert maybe_remind(agent, "read_file", {"path": "a"}, cfg) is None
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None  # 1 (untracked calls ignored)
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None  # 2
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is not None  # 3

    def test_exclude_makes_tools_transparent(self):
        cfg = {**DEFAULT_CFG, "exclude": ["mcp_*"]}
        agent = _agent()
        assert maybe_remind(agent, "mcp_x", {"a": 1}, cfg) is None
        assert maybe_remind(agent, "mcp_x", {"a": 1}, cfg) is None
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None  # 1
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None  # 2
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is not None  # 3

    def test_exclude_wins_over_include(self):
        cfg = {**DEFAULT_CFG, "include": ["*"], "exclude": ["todo"]}
        agent = _agent()
        assert maybe_remind(agent, "todo", {"todos": []}, cfg) is None
        assert maybe_remind(agent, "todo", {"todos": []}, cfg) is None
        assert maybe_remind(agent, "todo", {"todos": []}, cfg) is None  # never counts
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None  # 1
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None  # 2
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is not None  # 3

    def test_wildcard_matches_any_run(self):
        assert wildcard_to_regex("terminal*").match("terminal")
        assert wildcard_to_regex("terminal*").match("terminal_tool")
        assert not wildcard_to_regex("terminal*").match("my_terminal")
        assert wildcard_to_regex("*_tool").match("read_tool")
        assert not wildcard_to_regex("*_tool").match("tool_read")
        assert wildcard_to_regex("*").match("anything")

    def test_literal_metacharacters_do_not_leak(self):
        # "a.b" must match literally, not as regex "any-char b".
        assert wildcard_to_regex("a.b").match("a.b")
        assert not wildcard_to_regex("a.b").match("axb")


# =========================================================================
# Config handling
# =========================================================================


class TestConfig:
    def test_disabled_returns_none(self):
        cfg = {**DEFAULT_CFG, "enabled": False}
        agent = _agent()
        for _ in range(5):
            assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None

    def test_empty_thresholds_disables_reminders(self):
        cfg = {**DEFAULT_CFG, "thresholds": []}
        agent = _agent()
        for _ in range(5):
            assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None

    def test_invalid_thresholds_are_filtered(self):
        # [3, 2, 2, 1, "x", True, 5] -> deduped/sorted valid integers [2, 3, 5].
        cfg = {**DEFAULT_CFG, "thresholds": [3, 2, 2, 1, "x", True, 5]}
        agent = _agent()
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None  # 1
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) == gentle_reminder("terminal", 2)
        third = maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg)         # 3 -> detailed
        assert "consecutive_calls: 3" in third
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg) is None  # 4
        fifth = maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg)         # 5 -> detailed
        assert "consecutive_calls: 5" in fifth

    def test_missing_section_falls_back_to_defaults(self):
        # An empty/absent section behaves exactly like the defaults.
        agent = _agent()
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, {}) is None
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, {}) is None
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, {}) is not None


# =========================================================================
# Advisory contract: never raises, never blocks
# =========================================================================


class TestAdvisoryContract:
    def test_broken_config_read_degrades_to_none(self, monkeypatch):
        import agent.repeat_tool_reminder as rtr

        def _boom():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(rtr, "_read_config", _boom)
        agent = _agent()
        # No exception, no reminder — the guard must never break the loop.
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}) is None

    def test_unserializable_args_never_raise(self):
        agent = _agent()
        cyclic = {}
        cyclic["self"] = cyclic  # json.dumps raises ValueError
        # Must not raise; degrades to no reminder.
        assert maybe_remind(agent, "terminal", cyclic, DEFAULT_CFG) is None
        # And the chain still works afterwards.
        assert maybe_remind(agent, "terminal", {"cmd": "ls"}, DEFAULT_CFG) is None

    def test_bad_config_shapes_never_raise(self):
        for bad in (None, "nope", 42, ["list"]):
            # Fresh agent per shape: bad configs fall back to defaults, and
            # the chain must still advance without raising.
            agent = _agent()
            assert maybe_remind(agent, "terminal", {"cmd": "ls"}, bad) is None

    def test_reminder_is_append_only_text(self):
        # The returned value is plain text meant for the result tail; it must
        # never look like a tool schema change or a blocking directive.
        text = detailed_reminder("terminal", 5, '{"cmd":"ls"}')
        assert text.startswith("[reminder]")
        assert "Do not call this tool with these exact arguments again" in text


# =========================================================================
# Thread safety (concurrent tool-execution workers)
# =========================================================================


class TestThreadSafety:
    def test_concurrent_identical_calls_advance_one_chain(self):
        agent = _agent()
        cfg = {**DEFAULT_CFG, "thresholds": [8, 10]}
        results = []
        errors = []

        def worker():
            try:
                results.append(maybe_remind(agent, "terminal", {"cmd": "ls"}, cfg))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        reminders = [r for r in results if r is not None]
        # Exactly two of the ten calls hit a threshold: count 8 (gentle) and
        # count 10 (detailed).
        assert len(reminders) == 2
        assert any("identical arguments 8 times" in r for r in reminders)
        assert any("consecutive_calls: 10" in r for r in reminders)
