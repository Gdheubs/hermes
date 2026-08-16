"""At-rest content transforms: transform_message_store / transform_message_load.

The hooks fire on the sqlite boundary in :class:`hermes_state.SessionDB`. What
matters is that content round-trips exactly, that the stored form is whatever the
plugin produced, and that a failing callback raises instead of silently storing
or returning untransformed content.
"""

import base64
import os

import pytest

from hermes_cli import plugins
from hermes_state import SessionDB


def _wrap(content, session_id, role, **_kwargs):
    """Reversible non-deterministic transform: same input, different output."""
    if not isinstance(content, str):
        return None
    salt = base64.b64encode(os.urandom(6)).decode()
    return "W1:" + salt + ":" + base64.b64encode(content.encode()).decode()


def _unwrap(content, session_id, role, **_kwargs):
    if isinstance(content, str) and content.startswith("W1:"):
        return base64.b64decode(content.split(":", 2)[2]).decode()
    return None


@pytest.fixture
def registered():
    """Register the transform pair and restore the registry afterwards."""
    manager = plugins.get_plugin_manager()
    saved = {name: list(cbs) for name, cbs in manager._hooks.items()}
    manager._hooks.setdefault("transform_message_store", []).append(_wrap)
    manager._hooks.setdefault("transform_message_load", []).append(_unwrap)
    yield manager
    manager._hooks.clear()
    manager._hooks.update(saved)


def test_no_plugin_registered_leaves_content_untouched():
    """Inert until something registers, so existing callers are unaffected."""
    assert SessionDB._encode_content("plain") == "plain"
    assert SessionDB._decode_content("plain") == "plain"


def test_string_round_trip(registered):
    stored = SessionDB._encode_content("hello there", session_id="s1", role="user")
    assert stored.startswith("W1:")
    assert "hello there" not in stored
    assert (
        SessionDB._decode_content(stored, session_id="s1", role="user")
        == "hello there"
    )


def test_non_deterministic_transform_is_supported(registered):
    """Transforming the same input twice is allowed to produce different output."""
    first = SessionDB._encode_content("same", session_id="s1", role="user")
    second = SessionDB._encode_content("same", session_id="s1", role="user")
    assert first != second
    assert SessionDB._decode_content(first) == SessionDB._decode_content(second) == "same"


def test_structured_content_round_trips(registered):
    """Multimodal content is serialized before the store hook and rebuilt after load."""
    parts = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "x"}},
    ]
    stored = SessionDB._encode_content(parts, session_id="s2", role="assistant")
    assert "image_url" not in stored
    assert SessionDB._decode_content(stored, session_id="s2", role="assistant") == parts


def test_payload_carries_session_id_and_role(registered):
    """Per-session transforms are only possible if the session reaches the callback."""
    seen = []

    def observe(content, session_id, role, **_kwargs):
        seen.append((session_id, role))
        return None

    registered._hooks["transform_message_store"] = [observe]
    SessionDB._encode_content("x", session_id="s3", role="assistant")
    assert seen == [("s3", "assistant")]


def test_non_string_scalars_pass_through(registered):
    assert SessionDB._encode_content(None, session_id="s1") is None


def test_failing_store_callback_propagates(registered):
    """Swallowing here would write untransformed content to disk."""

    def boom(content, session_id, role, **_kwargs):
        raise RuntimeError("transform unavailable")

    registered._hooks["transform_message_store"] = [boom]
    with pytest.raises(RuntimeError, match="transform unavailable"):
        SessionDB._encode_content("value", session_id="s1", role="user")


def test_failing_load_callback_propagates(registered):
    """Swallowing here would hand the stored form back to the model."""

    def boom(content, session_id, role, **_kwargs):
        raise RuntimeError("transform unavailable")

    registered._hooks["transform_message_load"] = [boom]
    with pytest.raises(RuntimeError, match="transform unavailable"):
        SessionDB._decode_content("W1:whatever", session_id="s1", role="user")


def test_other_hooks_still_swallow_exceptions(registered):
    """strict is opt-in; every other hook keeps its isolate-the-plugin behaviour."""

    def boom(**_kwargs):
        raise RuntimeError("boom")

    registered._hooks["some_other_hook"] = [boom]
    assert plugins.invoke_hook("some_other_hook", content="x") == []
