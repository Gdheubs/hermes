"""Tests for the ATEM dialect parser (Muse-Glimmer-30B's native tool-call markup).

Covers ``agent/atem_dialect.py`` in isolation: model-name detection and
parsing well-formed / malformed ``<atem:function_calls>`` markup out of raw
completion text.
"""

from __future__ import annotations

import json

import pytest

from agent.atem_dialect import extract_atem_tool_calls, is_muse_glimmer_model


class TestMuseGlimmerModelDetection:
    """is_muse_glimmer_model() must match across aggregator/path prefixes."""

    @pytest.mark.parametrize(
        "model",
        [
            "muse-glimmer-30b",
            "Muse-Glimmer-30B",
            "MUSE-GLIMMER-30B",
            "meta-models/Muse-Glimmer-30B",
            "openrouter/meta-models/muse-glimmer-30b",
            "museglimmer-30b",
        ],
    )
    def test_positive_matches(self, model):
        assert is_muse_glimmer_model(model) is True

    @pytest.mark.parametrize(
        "model",
        [
            "",
            None,
            "kimi-k2.6",
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.4",
            "google/gemini-3-flash-preview",
            "glimmer",
            "muse",
        ],
    )
    def test_negative_matches(self, model):
        assert is_muse_glimmer_model(model) is False


class TestExtractAtemToolCalls:
    def test_empty_text(self):
        calls, content, malformed = extract_atem_tool_calls("")
        assert calls == []
        assert content == ""
        assert malformed == []

    def test_none_like_text_returns_original(self):
        calls, content, malformed = extract_atem_tool_calls("   ")
        assert calls == []
        assert content == "   "
        assert malformed == []

    def test_plain_text_no_calls(self):
        calls, content, malformed = extract_atem_tool_calls("Sure, here's the answer.")
        assert calls == []
        assert content == "Sure, here's the answer."
        assert malformed == []

    def test_single_call(self):
        text = (
            "<atem:function_calls>\n"
            '<atem:invoke name="terminal">\n'
            '<atem:parameter name="command">ls logs/</atem:parameter>\n'
            "</atem:invoke>\n"
            "</atem:function_calls>"
        )
        calls, content, malformed = extract_atem_tool_calls(text)
        assert malformed == []
        assert content == ""
        assert len(calls) == 1
        call_id, name, arguments = calls[0]
        assert call_id == "atem_call_1"
        assert name == "terminal"
        assert json.loads(arguments) == {"command": "ls logs/"}

    def test_call_with_multiple_parameters_preserves_order(self):
        text = (
            "<atem:function_calls>\n"
            '<atem:invoke name="search">\n'
            '<atem:parameter name="query">weather</atem:parameter>\n'
            '<atem:parameter name="limit">5</atem:parameter>\n'
            "</atem:invoke>\n"
            "</atem:function_calls>"
        )
        calls, _content, malformed = extract_atem_tool_calls(text)
        assert malformed == []
        _, _, arguments = calls[0]
        assert list(json.loads(arguments).keys()) == ["query", "limit"]

    def test_argument_values_stay_strings(self):
        """No tool schema is available at parse time, so types aren't recovered."""
        text = (
            "<atem:function_calls>\n"
            '<atem:invoke name="toggle">\n'
            '<atem:parameter name="enabled">true</atem:parameter>\n'
            '<atem:parameter name="count">5</atem:parameter>\n'
            "</atem:invoke>\n"
            "</atem:function_calls>"
        )
        calls, _content, _malformed = extract_atem_tool_calls(text)
        arguments = json.loads(calls[0][2])
        assert arguments == {"enabled": "true", "count": "5"}
        assert isinstance(arguments["enabled"], str)
        assert isinstance(arguments["count"], str)

    def test_prose_around_a_call_is_preserved(self):
        text = (
            "Let me check that.\n"
            "<atem:function_calls>\n"
            '<atem:invoke name="terminal">\n'
            '<atem:parameter name="command">pwd</atem:parameter>\n'
            "</atem:invoke>\n"
            "</atem:function_calls>\n"
            "One moment."
        )
        calls, content, malformed = extract_atem_tool_calls(text)
        assert len(calls) == 1
        assert malformed == []
        assert content == "Let me check that.\n\nOne moment."

    def test_multiple_calls_in_document_order(self):
        text = (
            "<atem:function_calls>\n"
            '<atem:invoke name="first">\n'
            '<atem:parameter name="x">1</atem:parameter>\n'
            "</atem:invoke>\n"
            "</atem:function_calls>\n"
            "<atem:function_calls>\n"
            '<atem:invoke name="second">\n'
            '<atem:parameter name="y">2</atem:parameter>\n'
            "</atem:invoke>\n"
            "</atem:function_calls>"
        )
        calls, _content, malformed = extract_atem_tool_calls(text)
        assert malformed == []
        assert [name for _id, name, _args in calls] == ["first", "second"]
        assert [call_id for call_id, _name, _args in calls] == ["atem_call_1", "atem_call_2"]

    def test_call_with_no_parameters(self):
        text = (
            "<atem:function_calls>\n"
            '<atem:invoke name="list_files">\n'
            "</atem:invoke>\n"
            "</atem:function_calls>"
        )
        calls, _content, malformed = extract_atem_tool_calls(text)
        assert malformed == []
        assert json.loads(calls[0][2]) == {}

    def test_empty_block_is_reported_not_silent(self):
        text = "<atem:function_calls>\n</atem:function_calls>"
        calls, content, malformed = extract_atem_tool_calls(text)
        assert calls == []
        assert content == ""
        assert len(malformed) == 1
        assert "no <atem:invoke>" in malformed[0]

    def test_empty_invoke_name_is_reported(self):
        text = (
            "<atem:function_calls>\n"
            '<atem:invoke name="">\n'
            '<atem:parameter name="x">1</atem:parameter>\n'
            "</atem:invoke>\n"
            "</atem:function_calls>"
        )
        calls, _content, malformed = extract_atem_tool_calls(text)
        assert calls == []
        assert any("empty name attribute" in m for m in malformed)

    def test_unclosed_call_block_is_reported(self):
        """The likeliest truncation failure: an opened block never closed."""
        text = (
            "I'll check that.\n"
            "<atem:function_calls>\n"
            '<atem:invoke name="terminal">\n'
            '<atem:parameter name="command">ls'
        )
        calls, content, malformed = extract_atem_tool_calls(text)
        assert calls == []
        assert "never closed" in " ".join(malformed)
        assert "<atem:function_calls>" not in content

    def test_unterminated_parameter_is_reported_not_silently_dropped(self):
        text = (
            "<atem:function_calls>\n"
            '<atem:invoke name="terminal">\n'
            '<atem:parameter name="command">ls logs/\n'
            "</atem:invoke>\n"
            "</atem:function_calls>"
        )
        calls, _content, malformed = extract_atem_tool_calls(text)
        assert calls == []
        assert any("is not closed" in m for m in malformed)

    def test_loose_tag_shaped_text_outside_real_blocks_is_reported(self):
        text = "Here's a call: <atem:invoke name=\"x\"> but no block around it."
        calls, content, malformed = extract_atem_tool_calls(text)
        assert calls == []
        assert any("call-shaped but not a call" in m for m in malformed)
        assert "<atem:invoke" not in content

    def test_real_call_does_not_trigger_loose_tag_false_positive(self):
        """A correct call contains <atem:invoke>, which is itself loose-tag-shaped;
        it must not also be reported as a near miss."""
        text = (
            "<atem:function_calls>\n"
            '<atem:invoke name="terminal">\n'
            '<atem:parameter name="command">ls</atem:parameter>\n'
            "</atem:invoke>\n"
            "</atem:function_calls>"
        )
        _calls, _content, malformed = extract_atem_tool_calls(text)
        assert malformed == []

    def test_custom_call_id_prefix(self):
        text = (
            "<atem:function_calls>\n"
            '<atem:invoke name="terminal">\n'
            '<atem:parameter name="command">ls</atem:parameter>\n'
            "</atem:invoke>\n"
            "</atem:function_calls>"
        )
        calls, _content, _malformed = extract_atem_tool_calls(text, call_id_prefix="turn7")
        assert calls[0][0] == "turn7_1"
