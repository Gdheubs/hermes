"""Audio bridges between Discord voice channels and the xAI realtime session.

The realtime backend (:mod:`tools.voice_realtime`) is transport-agnostic: it
takes a ``mic_factory`` (frames in) and a ``playout_sink_factory`` (speech
out). This module supplies the Discord implementations:

* :class:`DiscordMicBridge` — fed continuously by the adapter's voice-receive
  drain (48 kHz stereo, Discord-native), downsamples to the session's 16 kHz
  mono input format.
* :class:`MixerPlayoutSink` — receives the supervisor's 24 kHz mono speech,
  upsamples to 48 kHz stereo, and streams it through the guild's continuous
  :class:`~voice_mixer.VoiceMixer` so it ducks the ambient bed and mixes over
  it like any other speech.

Sample-rate notes: 48 kHz / 16 kHz is an exact 3:1 decimation and
24 kHz → 48 kHz an exact 1:2 repeat, so both conversions are simple integer
resamples — no scipy dependency. The 6-sample group mean in the downsampler
doubles as the mono mixdown and a crude anti-alias filter, which is plenty
for speech feeding a VAD + ASR pipeline.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Tuple

try:
    from .voice_mixer import StreamSpeechChild, _require_numpy
except ImportError:
    from voice_mixer import StreamSpeechChild, _require_numpy

# 3 stereo int16 frames (L,R × 3) collapse into one mono output sample.
_DOWNSAMPLE_GROUP_BYTES = 12


def downsample_48k_stereo_to_16k_mono(pcm: bytes) -> Tuple[bytes, bytes]:
    """Return ``(mono_16k_pcm, remainder)`` for Discord-native input PCM.

    ``remainder`` is the trailing sub-group slice (< 12 bytes) the caller
    must prepend to the next chunk so decimation stays sample-aligned
    across drains.
    """
    usable = len(pcm) - (len(pcm) % _DOWNSAMPLE_GROUP_BYTES)
    if usable <= 0:
        return b"", pcm
    remainder = pcm[usable:]
    np = _require_numpy()
    arr = np.frombuffer(pcm[:usable], dtype=np.int16).astype(np.float32)
    out = arr.reshape(-1, 6).mean(axis=1)
    return out.astype(np.int16).tobytes(), remainder


def upsample_24k_mono_to_48k_stereo(pcm: bytes) -> bytes:
    """Convert realtime speech PCM to Discord-native playback PCM."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return b""
    np = _require_numpy()
    arr = np.frombuffer(pcm[:usable], dtype=np.int16)
    # ×2 for 24 kHz → 48 kHz, ×2 again for mono → interleaved stereo:
    # each input sample becomes (L,L),(R,R) of two consecutive 48 kHz frames.
    return np.repeat(arr, 4).tobytes()


class DiscordMicBridge:
    """The realtime session's "microphone", fed by the VC receive drain.

    The adapter calls :meth:`feed` with each user's freshly drained
    48 kHz stereo PCM (already allowlist-filtered). A per-user carry keeps
    the 3:1 decimation aligned between drains. Simultaneous speakers are
    forwarded in drain order — the server's VAD/ASR sees one interleaved
    conversation, which matches how a speakerphone would behave.
    """

    def __init__(self, on_frame: Callable[[bytes], None]):
        self._on_frame = on_frame
        self._carry: Dict[int, bytes] = {}
        self._closed = False

    def feed(self, user_id: int, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        data = self._carry.pop(user_id, b"") + pcm
        frame, remainder = downsample_48k_stereo_to_16k_mono(data)
        if remainder:
            self._carry[user_id] = remainder
        if frame:
            self._on_frame(frame)

    def close(self) -> None:
        self._closed = True
        self._carry.clear()


class MixerPlayoutSink:
    """Realtime playout sink that streams speech through the guild mixer.

    Implements the optional sink extensions the session honors:
    ``clear()`` drops buffered audio on barge-in and ``pending()`` reports
    whether previously written audio is still audibly draining (so the
    session's ``speaking`` state covers the mixer's buffer, not just its
    own queue).

    ``mixer_getter`` is resolved on every write — the mixer is installed
    per-connection and may be replaced across VC reconnects.
    """

    def __init__(self, mixer_getter: Callable[[], Optional[object]], *, gain: float = 1.0):
        self._mixer_getter = mixer_getter
        self._gain = float(gain)
        self._child: Optional[StreamSpeechChild] = None

    def write(self, chunk: bytes) -> None:
        mixer = self._mixer_getter()
        if mixer is None:
            return  # not connected — drop rather than buffer unboundedly
        child = self._child
        if child is None or child.finished:
            child = StreamSpeechChild("realtime-speech", gain=self._gain)
            self._child = child
        child.feed(upsample_24k_mono_to_48k_stereo(chunk))
        # Idempotent; also re-attaches after a stop_speech() detached it.
        mixer.attach_speech_stream(child)

    def clear(self) -> None:
        child = self._child
        if child is not None:
            child.clear()

    def pending(self) -> bool:
        child = self._child
        return bool(child is not None and child.buffered_bytes > 0)

    def set_active(self, active: bool) -> None:
        # Ducking is audibility-driven inside the mixer; nothing to mark.
        pass

    def close(self) -> None:
        child, self._child = self._child, None
        if child is not None:
            child.end()


__all__ = [
    "DiscordMicBridge",
    "MixerPlayoutSink",
    "downsample_48k_stereo_to_16k_mono",
    "upsample_24k_mono_to_48k_stereo",
]
