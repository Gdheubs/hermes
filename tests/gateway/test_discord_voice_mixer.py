"""Tests for the Discord continuous voice mixer (ambient + ducked speech)
and the verbal-ack-before-tool-calls hook.

The mixer (plugins/platforms/discord/voice_mixer.py) is pure-PCM and has no
discord.py dependency, so its core is tested directly.  The adapter
integration (install on join, play routing, ack) is tested with the standard
``object.__new__(DiscordAdapter)`` helper used elsewhere in the voice suite.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# numpy ships only in the optional "voice" extra (not [all,dev]); the mixer
# math needs it, so skip this whole module when it isn't installed.
np = pytest.importorskip("numpy")

# voice_mixer lives inside the discord plugin package dir; import by path the
# same way the adapter does.
_DISCORD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugins", "platforms", "discord",
)
if _DISCORD_DIR not in sys.path:
    sys.path.insert(0, _DISCORD_DIR)

import voice_mixer as vm  # noqa: E402


# =====================================================================
# Pure mixer unit tests
# =====================================================================

class TestStreamingMixerChild:
    def test_split_24k_mono_input_converts_to_discord_frame(self):
        child = vm.StreamingMixerChild("stream", max_buffer_bytes=vm.FRAME_SIZE * 2)
        mono = np.arange(480, dtype=np.int16).tobytes()
        child.write(mono[:101])
        child.write(mono[101:])
        child.finish()

        frame = child.read_frame()
        assert frame is not None
        expected = np.repeat(np.repeat(np.arange(480, dtype=np.int16), 2), 2)
        np.testing.assert_array_equal(frame.astype(np.int16), expected)
        assert child.read_frame() is None

    def test_underrun_returns_silence_until_finished(self):
        child = vm.StreamingMixerChild("stream")
        frame = child.read_frame()
        assert frame is not None
        assert np.count_nonzero(frame) == 0
        assert not child.finished

        child.finish()
        assert child.read_frame() is None
        assert child.finished

    def test_finish_drains_partial_frame_with_padding(self):
        child = vm.StreamingMixerChild("stream")
        child.write(np.array([123], dtype=np.int16).tobytes())
        child.finish()
        frame = child.read_frame()
        assert frame is not None
        assert frame[0] == frame[1] == frame[2] == frame[3] == 123
        assert np.count_nonzero(frame[4:]) == 0
        assert child.read_frame() is None

    def test_abort_drops_buffered_audio(self):
        child = vm.StreamingMixerChild("stream")
        child.write(np.arange(480, dtype=np.int16).tobytes())
        child.abort()
        assert child.read_frame() is None
        assert child.finished

    def test_buffer_is_bounded(self):
        child = vm.StreamingMixerChild("stream", max_buffer_bytes=vm.FRAME_SIZE)
        child.write(np.arange(480, dtype=np.int16).tobytes())
        with pytest.raises(BufferError):
            child.write(np.array([1], dtype=np.int16).tobytes())


class TestVoiceMixerCore:
    def test_frame_geometry_matches_discord(self):
        # 20ms @ 48kHz stereo s16 == 3840 bytes (discord.opus.Encoder.FRAME_SIZE)
        assert vm.FRAME_SIZE == 3840
        assert vm.SAMPLES_PER_FRAME == 960
        assert len(vm.SILENCE_FRAME) == vm.FRAME_SIZE

    def test_empty_mixer_returns_silence_frames(self):
        mx = vm.VoiceMixer()
        for _ in range(5):
            frame = mx.read()
            assert len(frame) == vm.FRAME_SIZE
            assert frame == vm.SILENCE_FRAME

    def test_is_opus_false(self):
        # discord.py sends raw PCM when is_opus() is False.
        assert vm.VoiceMixer().is_opus() is False

    def test_ambient_loops_and_is_quiet(self):
        mx = vm.VoiceMixer(ambient_gain=0.2)
        amb = vm.synth_ambient_pcm(seconds=0.5)
        assert len(amb) % vm.FRAME_SIZE == 0  # frame-aligned for seamless loop
        mx.set_ambient(amb)
        peaks = [int(np.max(np.abs(np.frombuffer(mx.read(), dtype=np.int16))))
                 for _ in range(100)]  # 2s >> 0.5s loop
        # Produces audio after the fade-in and stays under the configured gain.
        assert any(p > 0 for p in peaks[10:])
        assert max(peaks) < int(32767 * 0.5)


# =====================================================================
# Adapter integration
# =====================================================================

def _make_adapter(fx_cfg=None):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import Platform, PlatformConfig
    config = PlatformConfig(enabled=True, extra={})
    config.token = "fake-token"
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = config
    adapter._client = MagicMock()
    adapter._voice_clients = {}
    adapter._voice_locks = {}
    adapter._voice_text_channels = {111: 111}
    adapter._voice_sources = {}
    adapter._voice_timeout_tasks = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._voice_mixers = {}
    adapter._reset_voice_timeout = MagicMock()
    adapter._ambient_pcm_cache = None
    adapter._voice_fx_cfg = fx_cfg if fx_cfg is not None else {
        "enabled": True, "ambient_enabled": True, "ambient_path": "",
        "ambient_gain": 0.18, "duck_gain": 0.06, "speech_gain": 1.0,
        "ack_enabled": True, "ack_phrases": ["One moment."],
    }
    return adapter


class TestStreamingMixerLifecycle:
    def test_drained_callback_fires_once_on_natural_drain(self):
        events = []
        mx = vm.VoiceMixer()
        child = mx.begin_streaming_speech(on_drained=lambda c: events.append("drained"))
        child.write(np.arange(480, dtype=np.int16).tobytes())
        child.finish()
        for _ in range(100):
            mx.read()
            if not mx.speech_active:
                break
        assert events == ["drained"]

    def test_drained_callback_fires_on_abort(self):
        events = []
        mx = vm.VoiceMixer()
        child = mx.begin_streaming_speech(on_drained=lambda c: events.append("drained"))
        child.write(np.arange(480, dtype=np.int16).tobytes())
        child.abort()
        assert events == ["drained"]

    def test_abort_one_streaming_child_keeps_sibling_speech_active(self):
        mx = vm.VoiceMixer()
        a = mx.begin_streaming_speech()
        b = mx.begin_streaming_speech()
        b.write(np.arange(960, dtype=np.int16).tobytes())
        a.abort()
        assert mx.speech_active is True, "sibling speech must stay active"
        assert mx.read() != vm.SILENCE_FRAME, "sibling audio must still play"
        b.abort()
        assert mx.speech_active is False

    def test_stop_speech_aborts_streaming_children(self):
        mx = vm.VoiceMixer()
        child = mx.begin_streaming_speech()
        child.write(np.arange(960, dtype=np.int16).tobytes())
        mx.stop_speech()
        assert mx.speech_active is False
        # A late producer write must not resurrect audio into the mixer.
        child.write(np.arange(960, dtype=np.int16).tobytes())
        assert mx.read() == vm.SILENCE_FRAME


class TestVoiceMixerActive:
    def test_streaming_child_plays_before_finish(self):
        mx = vm.VoiceMixer()
        child = mx.begin_streaming_speech()
        child.write(np.arange(480, dtype=np.int16).tobytes())
        frame = mx.read()
        assert frame != vm.SILENCE_FRAME
        assert mx.speech_active

    def test_streaming_child_underrun_keeps_mixer_alive(self):
        mx = vm.VoiceMixer()
        child = mx.begin_streaming_speech()
        # Empty but open: silence frames, mixer must not stop the stream.
        assert mx.read() == vm.SILENCE_FRAME
        # One full 20ms-worth of input (480 mono samples) converts to a full
        # Discord frame, so the next read emits audio.
        child.write(np.arange(480, dtype=np.int16).tobytes())
        assert mx.read() != vm.SILENCE_FRAME

    def test_streaming_child_finish_releases_duck(self):
        mx = vm.VoiceMixer()
        child = mx.begin_streaming_speech()
        child.write(np.arange(480, dtype=np.int16).tobytes())
        child.finish()
        drained = 0
        while mx.speech_active and drained < 100:
            mx.read()
            drained += 1
        assert not mx.speech_active

    def test_abort_streaming_child_stops_speech_immediately(self):
        mx = vm.VoiceMixer()
        child = mx.begin_streaming_speech()
        child.write(np.arange(960, dtype=np.int16).tobytes())
        child.abort()
        assert not mx.speech_active

    def test_false_when_attr_missing(self):
        # Defensive getattr path (object.__new__ helper that forgot the attr).
        from plugins.platforms.discord.adapter import DiscordAdapter
        from gateway.config import Platform
        bare = object.__new__(DiscordAdapter)
        bare.platform = Platform.DISCORD
        assert bare.voice_mixer_active(111) is False


class TestPlayInVoiceChannelMixerPath:
    @pytest.mark.asyncio
    async def test_routes_through_mixer_when_present(self):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_connected.return_value = True
        adapter._voice_clients[111] = vc

        # speech_active returns True once (so play_speech is observed) then
        # False so the wait loop exits promptly.
        class _Mixer:
            def __init__(self):
                self._polls = 0
                self.play_speech = MagicMock()

            @property
            def speech_active(self):
                self._polls += 1
                return self._polls <= 1

        mixer = _Mixer()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        fake_pcm = b"\x00" * vm.FRAME_SIZE
        with patch.object(vm, "decode_to_pcm", return_value=fake_pcm):
            ok = await adapter.play_in_voice_channel(111, "/tmp/x.mp3")
        assert ok is True
        mixer.play_speech.assert_called_once()
        adapter._reset_voice_timeout.assert_called_once_with(111)
        # Legacy path must NOT have been used.
        vc.play.assert_not_called()


class TestLeadSilence:
    """Warm-up lead silence prepended to speech so the first word isn't clipped
    (issue #66827)."""

    def test_bytes_empty_when_unset(self):
        adapter = _make_adapter()  # default cfg has no lead_silence_ms
        assert adapter._lead_silence_bytes() == b""


    def test_bytes_length_matches_ms(self):
        adapter = _make_adapter({"lead_silence_ms": 200})
        lead = adapter._lead_silence_bytes()
        assert lead == b"\x00" * (vm.BYTES_PER_MS * 200)
        assert len(lead) == 200 * 192  # 48kHz stereo s16 -> 192 bytes/ms


class TestPlayAckInVoice:
    @pytest.mark.asyncio
    async def test_noop_when_ack_disabled(self):
        adapter = _make_adapter({"ack_enabled": False})
        adapter._voice_mixers[111] = MagicMock()
        assert await adapter.play_ack_in_voice(111) is False


class TestStreamingTTSContract:
    @pytest.mark.asyncio
    async def test_supports_false_without_mixer(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter()
        assert adapter.supports_streaming_tts("111", AudioFormat()) is False

    @pytest.mark.asyncio
    async def test_supports_true_when_connected_flag_on(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter({"streaming_tts": True})
        vc = MagicMock()
        vc.is_connected.return_value = True
        adapter._voice_clients[111] = vc
        adapter._voice_mixers[111] = MagicMock()
        assert adapter.supports_streaming_tts("111", AudioFormat()) is True

    @pytest.mark.asyncio
    async def test_supports_false_on_wrong_format(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter({"streaming_tts": True})
        vc = MagicMock()
        vc.is_connected.return_value = True
        adapter._voice_clients[111] = vc
        adapter._voice_mixers[111] = MagicMock()
        assert adapter.supports_streaming_tts(
            "111", AudioFormat(sample_rate=44100, channels=2, sample_width=2)
        ) is False

    @pytest.mark.asyncio
    async def test_begin_returns_handle_and_warms_child_with_lead_silence(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter({"lead_silence_ms": 200})
        mixer = MagicMock()
        child = MagicMock()
        mixer.begin_streaming_speech.return_value = child
        adapter._voice_mixers[111] = mixer
        receiver = MagicMock()
        receiver.paused = False
        adapter._voice_receivers[111] = receiver
        fmt = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
        handle = await adapter.begin_streaming_tts("111", fmt)
        assert handle is not None
        assert handle.audio_format == fmt
        mixer.begin_streaming_speech.assert_called_once()
        # 200 ms of 24 kHz mono s16le lead silence == 200 * 48 bytes.
        written = child.write.call_args[0][0]
        assert len(written) == 200 * 48
        assert written == b"\x00" * (200 * 48)
        adapter._voice_receivers[111].pause.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_forwards_chunks_and_marks_audible(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter()
        mixer = MagicMock()
        child = MagicMock()
        mixer.begin_streaming_speech.return_value = child
        adapter._voice_mixers[111] = mixer
        handle = await adapter.begin_streaming_tts("111", AudioFormat())
        assert handle is not None
        await adapter.write_streaming_tts(handle, b"\x01\x02")
        child.write.assert_called_once_with(b"\x01\x02")
        assert handle.audible is True

    @pytest.mark.asyncio
    async def test_finish_without_audio_keeps_handle_not_audible(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter()
        mixer = MagicMock()
        child = MagicMock()
        mixer.begin_streaming_speech.return_value = child
        adapter._voice_mixers[111] = mixer
        handle = await adapter.begin_streaming_tts("111", AudioFormat())
        assert handle is not None
        await adapter.finish_streaming_tts(handle)
        assert handle.audible is False, (
            "never-audible stream must stay not-audible so whole-file fallback "
            "is not suppressed"
        )

    @pytest.mark.asyncio
    async def test_finish_does_not_resume_receiver_before_drain(self):
        from gateway.platforms.base import AudioFormat

        adapter = _make_adapter()
        mixer = vm.VoiceMixer()
        adapter._voice_mixers[111] = mixer
        receiver = MagicMock()
        receiver.paused = False
        adapter._voice_receivers[111] = receiver
        handle = await adapter.begin_streaming_tts("111", AudioFormat())
        assert handle is not None
        await adapter.write_streaming_tts(
            handle, np.arange(480, dtype=np.int16).tobytes()
        )
        await adapter.finish_streaming_tts(handle)
        assert receiver.resume.call_count == 0, (
            "receiver must stay paused until buffered speech drains"
        )
        # Drain the mixer on the sender thread: buffered audio plays, then the
        # drained callback fires and resumes the receiver.
        for _ in range(200):
            mixer.read()
            if receiver.resume.call_count:
                break
        assert receiver.resume.call_count == 1

    @pytest.mark.asyncio
    async def test_receiver_already_paused_is_not_resumed_by_handle(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter()
        mixer = MagicMock()
        child = MagicMock()
        mixer.begin_streaming_speech.return_value = child
        receiver = MagicMock()
        receiver.paused = True  # paused by someone else (e.g. another handle)
        adapter._voice_mixers[111] = mixer
        adapter._voice_receivers[111] = receiver
        handle = await adapter.begin_streaming_tts("111", AudioFormat())
        assert handle is not None
        receiver.pause.assert_not_called()
        await adapter.abort_streaming_tts(handle)
        receiver.resume.assert_not_called()

    @pytest.mark.asyncio
    async def test_begin_rolls_back_child_when_receiver_pause_fails(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter()
        mixer = MagicMock()
        child = MagicMock()
        mixer.begin_streaming_speech.return_value = child
        receiver = MagicMock()
        receiver.paused = False
        receiver.pause.side_effect = RuntimeError("boom")
        adapter._voice_mixers[111] = mixer
        adapter._voice_receivers[111] = receiver
        handle = await adapter.begin_streaming_tts("111", AudioFormat())
        assert handle is None
        child.abort.assert_called_once()
        receiver.resume.assert_not_called()  # we never paused it

    @pytest.mark.asyncio
    async def test_finish_after_abort_is_noop(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter()
        mixer = MagicMock()
        child = MagicMock()
        mixer.begin_streaming_speech.return_value = child
        adapter._voice_mixers[111] = mixer
        handle = await adapter.begin_streaming_tts("111", AudioFormat())
        assert handle is not None
        await adapter.abort_streaming_tts(handle)
        await adapter.finish_streaming_tts(handle)
        assert child.finish.call_count == 0

    @pytest.mark.asyncio
    async def test_abort_aborts_child_and_resumes_receiver(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter()
        mixer = MagicMock()
        child = MagicMock()
        mixer.begin_streaming_speech.return_value = child
        receiver = MagicMock()
        receiver.paused = False
        adapter._voice_mixers[111] = mixer
        adapter._voice_receivers[111] = receiver
        handle = await adapter.begin_streaming_tts("111", AudioFormat())
        assert handle is not None
        await adapter.abort_streaming_tts(handle, "boom")
        child.abort.assert_called_once()
        receiver.resume.assert_called_once()
        assert handle.aborted is True

    @pytest.mark.asyncio
    async def test_abort_is_idempotent(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter()
        mixer = MagicMock()
        child = MagicMock()
        mixer.begin_streaming_speech.return_value = child
        adapter._voice_mixers[111] = mixer
        handle = await adapter.begin_streaming_tts("111", AudioFormat())
        assert handle is not None
        await adapter.abort_streaming_tts(handle)
        await adapter.abort_streaming_tts(handle)
        assert child.abort.call_count == 1
        assert handle.aborted is True

    @pytest.mark.asyncio
    async def test_no_streaming_when_mixer_absent(self):
        from gateway.platforms.base import AudioFormat
        adapter = _make_adapter()
        handle = await adapter.begin_streaming_tts("111", AudioFormat())
        assert handle is None


