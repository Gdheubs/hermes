"""Regression tests for list-typed delta.content / delta.reasoning_content
in the auxiliary-client _ChatStreamAccumulator and the reasoning stream
path in interruptible_streaming_api_call.

Some OpenAI-compatible providers (e.g. GLM via custom proxies) return
``delta.content`` or ``delta.reasoning_content`` as a **list** of
content-part dicts rather than a plain string:

    {"delta": {"content": [{"type": "text", "text": "Hello"}]}}

PR #63755 covers the ``delta.content`` path in
``interruptible_streaming_api_call``; these tests cover the **remaining**
sites not addressed by that PR:

  - ``_ChatStreamAccumulator.feed()`` in ``agent/auxiliary_client.py``
    (both content and reasoning paths)
  - The ``reasoning_text`` path in ``interruptible_streaming_api_call``
    (``delta.reasoning_content`` / ``delta.reasoning``)
"""

from types import SimpleNamespace

from agent.auxiliary_client import _ChatStreamAccumulator
from agent.message_content import flatten_message_text


def _chunk(content=None, reasoning=None, reasoning_content=None,
           finish_reason=None, model="m1", chunk_id="c1"):
    delta = SimpleNamespace(
        content=content,
        reasoning=reasoning,
        reasoning_content=reasoning_content,
        tool_calls=None,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(
        id=chunk_id, model=model, choices=[choice], usage=None,
    )


class TestFlattenMessageTextListContent:

    def test_str_passthrough(self):
        assert flatten_message_text("hello") == "hello"

    def test_none_returns_empty(self):
        assert flatten_message_text(None) == ""

    def test_list_of_text_parts(self):
        parts = [{"type": "text", "text": "Hello"}, {"type": "text", "text": "World"}]
        assert flatten_message_text(parts) == "Hello\nWorld"

    def test_list_of_bare_strings(self):
        assert flatten_message_text(["foo", "bar"]) == "foo\nbar"

    def test_list_of_bare_strings_explicit_empty_sep(self):
        assert flatten_message_text(["foo", "bar"], sep="") == "foobar"

    def test_list_of_bare_strings_split_fragment(self):
        assert flatten_message_text(["Hel", "lo"], sep="") == "Hello"

    def test_list_with_non_text_parts_skipped(self):
        parts = [
            {"type": "image_url", "image_url": {"url": "http://x"}},
            {"type": "text", "text": "visible"},
        ]
        assert flatten_message_text(parts) == "visible"

    def test_list_of_dicts_with_content_key(self):
        parts = [{"content": "via content key"}]
        assert flatten_message_text(parts) == "via content key"


class TestChatStreamAccumulatorListDelta:

    def test_str_content_accumulates_normally(self):
        acc = _ChatStreamAccumulator()
        acc.feed(_chunk(content="Hello "))
        acc.feed(_chunk(content="World", finish_reason="stop"))
        msg = acc.finish()
        assert msg.choices[0].message.content == "Hello World"

    def test_list_content_does_not_crash(self):
        acc = _ChatStreamAccumulator()
        acc.feed(_chunk(content=[{"type": "text", "text": "Hello "}]))
        acc.feed(_chunk(content=[{"type": "text", "text": "World"}], finish_reason="stop"))
        msg = acc.finish()
        assert msg.choices[0].message.content == "Hello World"

    def test_mixed_str_and_list_content(self):
        acc = _ChatStreamAccumulator()
        acc.feed(_chunk(content="Hello "))
        acc.feed(_chunk(content=[{"type": "text", "text": "World"}], finish_reason="stop"))
        msg = acc.finish()
        assert msg.choices[0].message.content == "Hello World"

    def test_multiple_parts_in_single_delta(self):
        acc = _ChatStreamAccumulator()
        acc.feed(_chunk(content=[
            {"type": "text", "text": "line1"},
            {"type": "text", "text": "line2"},
        ], finish_reason="stop"))
        msg = acc.finish()
        # With sep="" in the streaming accumulator, all intra-delta parts
        # concatenate like stream fragments — no implicit newline injection.
        assert msg.choices[0].message.content == "line1line2"

    def test_list_reasoning_does_not_crash(self):
        acc = _ChatStreamAccumulator()
        acc.feed(_chunk(reasoning=[{"type": "text", "text": "thinking..."}]))
        acc.feed(_chunk(content="answer", finish_reason="stop"))
        msg = acc.finish()
        assert msg.choices[0].message.reasoning == "thinking..."
        assert msg.choices[0].message.content == "answer"

    def test_list_reasoning_content_field(self):
        acc = _ChatStreamAccumulator()
        acc.feed(_chunk(reasoning_content=[{"type": "text", "text": "step 1"}]))
        acc.feed(_chunk(content="done", finish_reason="stop"))
        msg = acc.finish()
        assert msg.choices[0].message.reasoning == "step 1"

    def test_empty_list_content_yields_empty_string(self):
        acc = _ChatStreamAccumulator()
        acc.feed(_chunk(content=[]))
        acc.feed(_chunk(content="real text", finish_reason="stop"))
        msg = acc.finish()
        assert msg.choices[0].message.content == "real text"

    def test_empty_list_reasoning_is_dropped(self):
        acc = _ChatStreamAccumulator()
        acc.feed(_chunk(reasoning=[]))
        acc.feed(_chunk(content="answer", finish_reason="stop"))
        msg = acc.finish()
        assert msg.choices[0].message.reasoning is None

    def test_bare_string_split_fragment_in_single_delta(self):
        acc = _ChatStreamAccumulator()
        acc.feed(_chunk(content=["Hel", "lo "]))
        acc.feed(_chunk(content="World", finish_reason="stop"))
        msg = acc.finish()
        assert msg.choices[0].message.content == "Hello World"

    def test_bare_string_split_fragment_in_reasoning(self):
        acc = _ChatStreamAccumulator()
        acc.feed(_chunk(reasoning_content=["step", " 1"]))
        acc.feed(_chunk(content="done", finish_reason="stop"))
        msg = acc.finish()
        assert msg.choices[0].message.reasoning == "step 1"
