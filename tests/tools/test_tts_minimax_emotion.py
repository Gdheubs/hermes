"""MiniMax TTS emotion payload tests.

The t2a_v2 ``voice_setting`` previously always carried an ``emotion`` field,
defaulting to ``"neutral"``. MiniMax documents that when the field is omitted
the model "automatically selects the most natural emotion based on text", so
the hardcoded default silently disabled that inference for every request and
flattened delivery. ``_minimax_voice_setting`` now includes the field only
when a config sets an explicit value; an empty value or ``"auto"`` also omits
it, so a config can state the intent explicitly.

The wire-payload tests drive the real ``_generate_minimax_tts`` with a
capture-then-raise ``requests.post`` stub. On a tree without the fix the
omission test fails on the payload assertion (``emotion: neutral`` present)
rather than erroring at collection, while the explicit-value cases pass by
design. The helper is looked up per test rather than imported
at module top, so collection succeeds on an unfixed tree and the helper tests
fail at runtime instead of blocking the whole file.
"""

import pytest

import requests

from tools import tts_tool
from tools.tts_tool import DEFAULT_MINIMAX_VOICE_ID, _generate_minimax_tts


FAKE_CREDENTIAL = "FAKE_MINIMAX_CREDENTIAL"


@pytest.fixture(autouse=True)
def _fake_minimax_credentials(monkeypatch):
    monkeypatch.setattr(
        "tools.tts_tool.get_env_value",
        lambda name, default=None: (
            {"MINIMAX_API_KEY": FAKE_CREDENTIAL}.get(name, default)
        ),
    )


class TestVoiceSetting:
    def test_unset_emotion_is_omitted(self):
        setting = tts_tool._minimax_voice_setting({"voice_id": "v1"})
        assert "emotion" not in setting
        assert setting["voice_id"] == "v1"

    @pytest.mark.parametrize("value", ["", None, "auto", "  Auto "])
    def test_empty_and_auto_omit(self, value):
        assert "emotion" not in tts_tool._minimax_voice_setting({"emotion": value})

    def test_explicit_emotion_is_sent_case_folded(self):
        assert tts_tool._minimax_voice_setting({"emotion": "Happy"})["emotion"] == "happy"

    def test_voice_knobs_pass_through(self):
        setting = tts_tool._minimax_voice_setting(
            {"voice_id": "v", "speed": 1.2, "vol": 0.8, "pitch": -2},
        )
        assert (setting["voice_id"], setting["speed"],
                setting["vol"], setting["pitch"]) == ("v", 1.2, 0.8, -2)

    def test_defaults_when_config_empty(self):
        setting = tts_tool._minimax_voice_setting({})
        assert setting["voice_id"] == DEFAULT_MINIMAX_VOICE_ID
        assert setting["speed"] == 1.0
        assert setting["vol"] == 1.0
        assert setting["pitch"] == 0


class _RequestCaptured(Exception):
    """Raised by the stub once the outgoing payload has been recorded."""


def _capture_post(captured):
    def fake_post(url, json=None, **kwargs):
        captured["url"] = url
        captured["payload"] = json
        raise _RequestCaptured()
    return fake_post


class TestWirePayload:
    def test_payload_omits_emotion_when_unset(self, monkeypatch, tmp_path):
        """The generator must not invent an emotion the config never set."""
        captured = {}
        monkeypatch.setattr(requests, "post", _capture_post(captured))

        cfg = {"minimax": {"model": "speech-2.8-turbo", "voice_id": "v1"}}
        with pytest.raises(_RequestCaptured):
            _generate_minimax_tts("hello", str(tmp_path / "clip.mp3"), cfg)

        voice_setting = captured["payload"]["voice_setting"]
        assert "emotion" not in voice_setting
        assert voice_setting["voice_id"] == "v1"

    @pytest.mark.parametrize("configured", ["calm", "neutral"])
    def test_payload_carries_configured_emotion(self, monkeypatch, tmp_path,
                                                configured):
        """Explicit values reach the wire unchanged.

        "neutral" is pinned by name because it is the documented migration
        path back to the pre-fix delivery; a future enum validator must not
        be able to drop it while these tests stay green.
        """
        captured = {}
        monkeypatch.setattr(requests, "post", _capture_post(captured))

        cfg = {"minimax": {"model": "speech-2.8-turbo", "voice_id": "v1",
                           "emotion": configured}}
        with pytest.raises(_RequestCaptured):
            _generate_minimax_tts("hello", str(tmp_path / "clip.mp3"), cfg)

        assert captured["payload"]["voice_setting"]["emotion"] == configured
