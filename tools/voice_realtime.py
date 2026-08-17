"""xAI Grok realtime (S2S WebSocket) session for voice mode.

Two brains (``voice.realtime.brain``):

* ``ears`` — input only. Server VAD + transcription; every utterance becomes
  a normal Hermes turn; replies speak via the regular TTS pipeline.
* ``supervisor`` — grok-voice converses instantly and delegates real work to
  Hermes through ``consult_hermes`` / ``steer_hermes``.

Config, tool schemas, and ``session.update`` live in
:mod:`tools.voice_realtime_config`. This module is the live session:
mic → socket → VAD events → optional supervisor playback.

Heavy deps (websockets, sounddevice, numpy, credentials) import lazily:
tools/*.py must stay cheap to import and voice is an optional extra.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import random
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, Optional, Tuple

from tools.voice_realtime_config import (
    CONSULT_TOOL_NAME,
    DEFAULT_REALTIME_MODEL,
    DEFAULT_SUPERVISOR_VOICE,
    FRAME_MS,
    FRAME_SAMPLES,
    INPUT_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
    REALTIME_URL,
    RECONNECT_DELAYS,
    STEER_TOOL_NAME,
    RealtimeConfig,
    RealtimeVoiceError,
    _ACK_PHRASES,
    build_session_update,
    check_realtime_requirements,
    load_realtime_config,
    realtime_voice_enabled,
)

logger = logging.getLogger(__name__)

_PREBUFFER_MAX_FRAMES = 20
_QUIET_WAIT_TIMEOUT_S = 20.0

def _default_connect(url: str, headers: Dict[str, str]):
    """Open a synchronous WebSocket connection (thread-based client)."""
    from websockets.sync.client import connect  # lazy: keep module import cheap

    return connect(url, additional_headers=headers, open_timeout=10, max_size=2**22)


class _SounddevicePlayoutSink:
    """Default speech sink: local speakers @ 24 kHz mono int16.

    Marks tools.voice_mode's audio-output refcount so the CLI's gate/idle/
    barge logic sees supervisor speech like any other playback. Non-local
    surfaces (Discord VC) inject their own sink and skip the refcount.
    """

    def __init__(self):
        import numpy as np  # lazy: optional voice extra
        import sounddevice as sd

        from tools.voice_mode import mark_audio_output_active

        self._np = np
        self._mark = mark_audio_output_active
        self._stream = sd.OutputStream(
            samplerate=OUTPUT_SAMPLE_RATE, channels=1, dtype="int16"
        )
        self._stream.start()

    def write(self, chunk: bytes) -> None:
        self._stream.write(self._np.frombuffer(chunk, dtype=self._np.int16))

    def set_active(self, active: bool) -> None:
        self._mark(active)

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


class _SounddeviceMic:
    """Default mic source: 16 kHz mono int16 InputStream → frame callback."""

    def __init__(self, on_frame: Callable[[bytes], None]):
        import sounddevice as sd  # lazy: optional voice extra

        def _callback(indata, frames, _time, status):
            if status:
                logger.debug("realtime mic status: %s", status)
            on_frame(bytes(indata.tobytes()))

        self._stream = sd.InputStream(
            samplerate=INPUT_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            callback=_callback,
        )
        self._stream.start()

    def close(self) -> None:
        try:
            self._stream.stop()
        finally:
            self._stream.close()


class RealtimeVoiceSession:
    """xAI realtime session: mic → server VAD → transcript / supervisor speech.

    Callbacks fire on session threads — keep them fast/thread-safe:
    * ``on_transcript(text)`` — finished utterance (armed + gate-open only)
    * ``on_speech_started()`` / ``on_speech_stopped()`` — server VAD edges;
      speech_started is the CLI's barge-in trigger
    * ``on_state(state, detail)`` — "connected" | "reconnecting" | "dead"
    * ``on_idle_pause()`` — idle timer fired; session disarmed itself first
    * ``input_gate()`` — False drops mic frames and suppresses speech events
    * ``activity_hold()`` — True while the user is correctly silent (agent
      busy / TTS live); idle timer pauses
    * ``on_function_call(name, call_id, args_json)`` — supervisor brain only
    * ``on_assistant_transcript(text)`` — supervisor speech transcript
    """

    def __init__(
        self,
        cfg: RealtimeConfig,
        *,
        on_transcript: Callable[[str], None],
        on_speech_started: Optional[Callable[[], None]] = None,
        on_speech_stopped: Optional[Callable[[], None]] = None,
        on_state: Optional[Callable[[str, str], None]] = None,
        on_idle_pause: Optional[Callable[[], None]] = None,
        input_gate: Optional[Callable[[], bool]] = None,
        activity_hold: Optional[Callable[[], bool]] = None,
        on_function_call: Optional[Callable[[str, str, str], None]] = None,
        on_assistant_transcript: Optional[Callable[[str], None]] = None,
        connect_fn: Optional[Callable[[str, Dict[str, str]], Any]] = None,
        mic_factory: Optional[Callable[[Callable[[bytes], None]], Any]] = None,
        playout_sink_factory: Optional[Callable[[], Any]] = None,
        require_local_audio: bool = True,
    ):
        self._cfg = cfg
        self._require_local_audio = require_local_audio
        self._on_transcript = on_transcript
        self._on_speech_started = on_speech_started
        self._on_speech_stopped = on_speech_stopped
        self._on_state = on_state
        self._on_idle_pause = on_idle_pause
        self._input_gate = input_gate
        self._activity_hold = activity_hold
        self._on_function_call = on_function_call
        self._on_assistant_transcript = on_assistant_transcript
        self._connect_fn = connect_fn or _default_connect
        self._mic_factory = mic_factory if mic_factory is not None else _SounddeviceMic
        self._playout_sink_factory = (
            playout_sink_factory if playout_sink_factory is not None
            else _SounddevicePlayoutSink
        )

        self._armed = threading.Event()
        self._stop = threading.Event()
        self._dead = threading.Event()
        self._connected_once = threading.Event()
        self._frames: "queue.Queue[bytes]" = queue.Queue(maxsize=100)
        self._prebuffer: deque = deque(maxlen=_PREBUFFER_MAX_FRAMES)
        self._ws: Any = None
        self._send_lock = threading.Lock()
        self._mic: Any = None
        self._net_thread: Optional[threading.Thread] = None
        self._pump_thread: Optional[threading.Thread] = None
        self._minimal_retry_done = False
        self._last_voice_activity = time.monotonic()
        self._current_rms = 0
        # Supervisor speech playback (lazy — never started in ears mode).
        self._playout_q: "queue.Queue[bytes]" = queue.Queue(maxsize=400)
        self._playout_thread: Optional[threading.Thread] = None
        self._playout_sink: Any = None
        self._playing = False
        self._active_response = False
        self._response_had_audio = False
        # Loud-barge (half-duplex supervisor): rolling speaker-bleed floor,
        # consecutive hot frames, open-mic window after a trigger, and a
        # short tail of gated frames replayed so the utterance start isn't
        # clipped.
        self._bleed_floor = 0.0
        self._barge_hot_frames = 0
        self._barge_until = 0.0
        self._gated_tail: deque = deque(maxlen=5)
        self._np: Any = None
        self._tool_results: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._tool_result_thread: Optional[threading.Thread] = None

    # -- public surface ----------------------------------------------------

    @property
    def alive(self) -> bool:
        return (
            self._net_thread is not None
            and self._net_thread.is_alive()
            and not self._dead.is_set()
            and not self._stop.is_set()
        )

    @property
    def connected(self) -> bool:
        return self.alive and self._ws is not None

    @property
    def current_rms(self) -> int:
        """Mic level for the CLI's audio meter (same contract as AudioRecorder)."""
        return self._current_rms

    def start(self) -> None:
        """Open the mic and start connecting (non-blocking; results via
        ``on_state``). Raises only on mic/requirements failure."""
        ok, detail = check_realtime_requirements(
            require_local_audio=self._require_local_audio
        )
        if not ok:
            raise RealtimeVoiceError(detail)
        try:
            self._mic = self._mic_factory(self._enqueue_frame)
        except Exception as exc:
            raise RealtimeVoiceError(f"microphone open failed: {exc}") from exc
        self._last_voice_activity = time.monotonic()
        self._net_thread = threading.Thread(
            target=self._net_loop, name="voice-rt-net", daemon=True
        )
        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="voice-rt-pump", daemon=True
        )
        self._net_thread.start()
        self._pump_thread.start()

    def stop(self) -> None:
        """Tear down mic, socket, and threads. Idempotent."""
        self._stop.set()
        self._armed.clear()
        self.clear_playout()
        mic, self._mic = self._mic, None
        if mic is not None:
            try:
                mic.close()
            except Exception:
                pass
        self._close_ws()
        for t in (self._net_thread, self._pump_thread, self._playout_thread, self._tool_result_thread):
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=3)

    def set_armed(self, armed: bool) -> None:
        """Arm/disarm transcript delivery; disarming clears the server buffer
        so a paused mic can't produce a stale utterance."""
        if armed:
            self._last_voice_activity = time.monotonic()
            self._armed.set()
        else:
            self._armed.clear()
            self._send_event({"type": "input_audio_buffer.clear"})
            self.clear_playout()

    @property
    def armed(self) -> bool:
        return self._armed.is_set()

    @property
    def speaking(self) -> bool:
        """True while supervisor speech is queued, playing, or still draining
        inside a buffering sink (``pending()`` — e.g. the Discord mixer)."""
        if self._playing or not self._playout_q.empty():
            return True
        pending = getattr(self._playout_sink, "pending", None)
        if pending is not None:
            try:
                return bool(pending())
            except Exception:
                return False
        return False

    @property
    def barge_active(self) -> bool:
        """True right after a loud-barge trigger — the gate lets mic frames
        through even though speech was just playing."""
        return time.monotonic() < self._barge_until

    def speak_verbatim(self, text: str, *, interruptible: bool = True) -> bool:
        """Inject exact text as spoken audio (xAI ``force_message``).
        Supervisor brain only — the ears brain never plays server audio."""
        text = (text or "").strip()
        if not text or not self._cfg.supervisor:
            return False
        return self._send_event({
            "type": "conversation.item.create",
            "item": {
                "type": "force_message",
                "role": "assistant",
                "interruptible": interruptible,
                "content": [{"type": "output_text", "text": text}],
            },
        })

    @property
    def last_response_had_audio(self) -> bool:
        """Whether the current/most recent response produced any speech."""
        return self._response_had_audio

    def speak_acknowledgment(self) -> None:
        """Instantly speak a rotating "on it" line (force_message, no model
        turn). Used when a consult arrived silently — the model skipped its
        mandated filler and the user must not get dead air."""
        self.speak_verbatim(random.choice(_ACK_PHRASES), interruptible=True)

    def send_function_output(self, call_id: str, output: str) -> None:
        """Return a tool result and ask for the follow-up response.

        ``response.create`` waits until current speech finishes (bounded) so
        the follow-up never talks over an in-flight answer. One worker thread
        serializes deliveries.
        """
        self._tool_results.put((call_id, output))
        if self._tool_result_thread is None or not self._tool_result_thread.is_alive():
            self._tool_result_thread = threading.Thread(
                target=self._tool_result_loop, name="voice-rt-tool-result", daemon=True
            )
            self._tool_result_thread.start()

    def _tool_result_loop(self) -> None:
        while not self._stop.is_set():
            try:
                call_id, output = self._tool_results.get(timeout=0.25)
            except queue.Empty:
                continue
            self._send_event({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            })
            deadline = time.monotonic() + _QUIET_WAIT_TIMEOUT_S
            while time.monotonic() < deadline and not self._stop.is_set():
                if not self.speaking and not self._active_response:
                    break
                time.sleep(0.2)
            if self._stop.is_set():
                return
            self._send_event({"type": "response.create"})

    def clear_playout(self) -> None:
        """Drop queued supervisor speech (barge-in / pause)."""
        try:
            while True:
                self._playout_q.get_nowait()
        except queue.Empty:
            pass
        # Buffering sinks (Discord mixer) hold already-written audio too.
        clear = getattr(self._playout_sink, "clear", None)
        if clear is not None:
            try:
                clear()
            except Exception:
                logger.debug("playout sink clear failed", exc_info=True)

    # -- internals ----------------------------------------------------------

    def _gate_open(self) -> bool:
        if self._input_gate is None:
            return True
        try:
            return bool(self._input_gate())
        except Exception:
            return True

    def _emit_state(self, state: str, detail: str = "") -> None:
        if self._on_state is not None:
            try:
                self._on_state(state, detail)
            except Exception:
                logger.debug("realtime on_state callback failed", exc_info=True)

    def _enqueue_frame(self, frame: bytes) -> None:
        try:
            self._frames.put_nowait(frame)
            return
        except queue.Full:
            pass
        try:
            self._frames.get_nowait()
        except queue.Empty:
            return
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            pass

    def _pump_loop(self) -> None:
        """Mic frame consumer: RMS meter, gating, loud-barge, idle pause."""
        while not self._stop.is_set():
            try:
                frame = self._frames.get(timeout=0.25)
            except queue.Empty:
                self._check_idle_pause()
                continue
            self._update_rms(frame)
            self._update_barge_detector()
            if not self._armed.is_set() or not self._gate_open():
                if self._playing:
                    # Keep a short tail so a barge doesn't clip the start
                    # of the user's utterance.
                    self._gated_tail.append(frame)
                self._check_idle_pause()
                continue
            self._check_idle_pause()
            while self._gated_tail:
                self._send_frame(self._gated_tail.popleft())
            self._send_frame(frame)

    def _send_frame(self, frame: bytes) -> None:
        ws = self._ws
        if ws is None:
            self._prebuffer.append(frame)
            return
        payload = json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(frame).decode("ascii"),
        })
        if not self._send_raw(payload):
            self._prebuffer.append(frame)

    def _update_barge_detector(self) -> None:
        """Loud-barge for half-duplex supervisor speech: the user talking
        clearly OVER the playback (RMS well above the tracked speaker-bleed
        floor) cuts playout and opens the mic. Bleed itself can't trigger —
        the floor is calibrated from it."""
        if not self._cfg.supervisor or self._cfg.full_duplex:
            return
        if not self._playing:
            self._barge_hot_frames = 0
            self._bleed_floor = 0.0  # recalibrate on the next playback
            return
        rms = float(self._current_rms)
        floor = self._bleed_floor
        if floor <= 0:
            self._bleed_floor = max(rms, 200.0)
            return
        # Track bleed: rise slowly (a shout must not become the floor),
        # fall quickly (quiet passages lower the trigger point).
        alpha = 0.05 if rms > floor else 0.3
        self._bleed_floor = floor + alpha * (rms - floor)
        if rms > max(self._bleed_floor, 200.0) * self._cfg.barge_multiplier:
            self._barge_hot_frames += 1
            if self._barge_hot_frames >= 2:  # ~200 ms sustained
                self._barge_hot_frames = 0
                self._barge_until = time.monotonic() + 4.0
                self.clear_playout()
                if self._active_response:
                    self._send_event({"type": "response.cancel"})
        else:
            self._barge_hot_frames = max(0, self._barge_hot_frames - 1)

    def _update_rms(self, frame: bytes) -> None:
        np = self._np
        if np is False:
            self._current_rms = 0
            return
        if np is None:
            try:
                import numpy as np  # lazy: optional voice extra
                self._np = np
            except Exception:
                self._np = False
                self._current_rms = 0
                return
        try:
            arr = np.frombuffer(frame, dtype=np.int16)
            if arr.size:
                self._current_rms = int(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
        except Exception:
            self._current_rms = 0

    def _check_idle_pause(self) -> None:
        idle_limit = self._cfg.idle_pause_seconds
        if idle_limit <= 0 or not self._armed.is_set():
            return
        if not self._gate_open():
            # Suppressed input (agent turn / TTS with barge off) is not idle.
            self._last_voice_activity = time.monotonic()
            return
        if self._activity_hold is not None:
            try:
                if self._activity_hold():
                    self._last_voice_activity = time.monotonic()
                    return
            except Exception:
                pass
        if time.monotonic() - self._last_voice_activity >= idle_limit:
            self.set_armed(False)
            if self._on_idle_pause is not None:
                try:
                    self._on_idle_pause()
                except Exception:
                    logger.debug("realtime on_idle_pause failed", exc_info=True)

    def _net_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                ws = self._open_session()
            except Exception as exc:
                if self._stop.is_set():
                    return
                if attempt >= len(RECONNECT_DELAYS):
                    logger.warning("realtime voice connection failed permanently: %s", exc)
                    self._dead.set()
                    self._emit_state("dead", str(exc))
                    return
                delay = RECONNECT_DELAYS[attempt]
                attempt += 1
                self._emit_state("reconnecting", f"retry in {delay:.0f}s: {exc}")
                if self._stop.wait(delay):
                    return
                continue

            attempt = 0
            try:
                self._recv_loop(ws)
            except Exception as exc:
                logger.debug("realtime recv loop ended: %s", exc)
            finally:
                self._ws = None
                try:
                    ws.close()
                except Exception:
                    pass
            if not self._stop.is_set():
                self._emit_state("reconnecting", "connection lost")

    def _open_session(self) -> Any:
        from tools.xai_http import resolve_xai_http_credentials  # lazy: heavy

        creds = resolve_xai_http_credentials()
        api_key = str(creds.get("api_key") or "").strip()
        if not api_key:
            raise RealtimeVoiceError("no xAI credentials available")
        url = f"{self._cfg.url}?model={self._cfg.model}"
        ws = self._connect_fn(url, {"Authorization": f"Bearer {api_key}"})
        with self._send_lock:
            ws.send(json.dumps(build_session_update(self._cfg)))
        # Flush disconnect-buffered audio before publishing the socket.
        while self._prebuffer:
            frame = self._prebuffer.popleft()
            with self._send_lock:
                ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(frame).decode("ascii"),
                }))
        self._ws = ws
        if not self._connected_once.is_set():
            self._connected_once.set()
        self._emit_state("connected", "")
        return ws

    def _recv_loop(self, ws: Any) -> None:
        while not self._stop.is_set():
            raw = ws.recv()
            if isinstance(raw, (bytes, bytearray, memoryview)):
                continue  # audio arrives as base64 JSON deltas, not binary frames
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict):
                self._handle_event(event)

    def _send_raw(self, payload: str) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            with self._send_lock:
                ws.send(payload)
            return True
        except Exception as exc:
            logger.debug("realtime send failed: %s", exc)
            return False

    def _send_event(self, event: Dict[str, Any]) -> bool:
        return self._send_raw(json.dumps(event))

    def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _mark_voice_activity(self) -> None:
        self._last_voice_activity = time.monotonic()

    # -- supervisor speech playback ------------------------------------------

    def _enqueue_playout(self, chunk: bytes) -> None:
        if self._playout_thread is None:
            self._playout_thread = threading.Thread(
                target=self._playout_loop, name="voice-rt-playout", daemon=True
            )
            self._playout_thread.start()
        try:
            self._playout_q.put_nowait(chunk)
        except queue.Full:
            pass  # sustained overrun — dropping late audio beats blocking recv

    def _playout_loop(self) -> None:
        """Feed queued PCM (24 kHz mono int16) into the playout sink."""
        try:
            sink = self._playout_sink_factory()
        except Exception as exc:
            logger.warning("supervisor playback unavailable: %s", exc)
            return
        self._playout_sink = sink
        set_active = getattr(sink, "set_active", None)
        pending = getattr(sink, "pending", None)
        marked = False

        def _mark(active: bool) -> None:
            nonlocal marked
            if marked == active:
                return
            marked = active
            self._playing = active
            if set_active is not None:
                try:
                    set_active(active)
                except Exception:
                    pass

        def _sink_pending() -> bool:
            if pending is None:
                return False
            try:
                return bool(pending())
            except Exception:
                return False

        try:
            while not self._stop.is_set():
                try:
                    chunk = self._playout_q.get(timeout=0.25)
                except queue.Empty:
                    # Buffering sinks are still audible after the queue
                    # drains — stay "playing" until they report empty.
                    if not _sink_pending():
                        _mark(False)
                    continue
                _mark(True)
                sink.write(chunk)
        except Exception as exc:
            logger.warning("supervisor playback stopped: %s", exc)
        finally:
            _mark(False)
            self._playout_sink = None
            try:
                sink.close()
            except Exception:
                pass

    def _handle_event(self, event: Dict[str, Any]) -> None:
        etype = str(event.get("type") or "")
        if etype == "input_audio_buffer.speech_started":
            self._mark_voice_activity()
            if self._cfg.supervisor:
                # Barge-in: the server interrupts its own response in VAD
                # mode; drop the locally queued remainder to match.
                self.clear_playout()
            if self._armed.is_set() and self._gate_open() and self._on_speech_started:
                try:
                    self._on_speech_started()
                except Exception:
                    logger.debug("realtime on_speech_started failed", exc_info=True)
        elif etype == "input_audio_buffer.speech_stopped":
            self._mark_voice_activity()
            if self._armed.is_set() and self._gate_open() and self._on_speech_stopped:
                try:
                    self._on_speech_stopped()
                except Exception:
                    logger.debug("realtime on_speech_stopped failed", exc_info=True)
        elif etype == "conversation.item.input_audio_transcription.completed":
            self._mark_voice_activity()
            transcript = str(event.get("transcript") or "").strip()
            if transcript and self._armed.is_set() and self._gate_open():
                try:
                    self._on_transcript(transcript)
                except Exception:
                    logger.warning("realtime transcript handler failed", exc_info=True)
        elif etype == "response.created":
            self._active_response = True
            self._response_had_audio = False
            if not self._cfg.supervisor:
                # Ears relay stays silent: cancel anything the server creates.
                self._send_event({"type": "response.cancel"})
        elif etype in ("response.done", "response.completed", "response.cancelled"):
            self._active_response = False
        elif etype in ("response.output_audio.delta", "response.audio.delta"):
            self._response_had_audio = True
            if self._cfg.supervisor and self._armed.is_set():
                b64 = event.get("delta") or event.get("audio") or ""
                if b64:
                    try:
                        self._enqueue_playout(base64.b64decode(b64))
                    except (ValueError, TypeError):
                        logger.debug("realtime: bad base64 audio delta")
        elif etype in (
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        ):
            transcript = str(event.get("transcript") or "").strip()
            if transcript and self._on_assistant_transcript:
                try:
                    self._on_assistant_transcript(transcript)
                except Exception:
                    logger.debug("realtime assistant transcript cb failed", exc_info=True)
        elif etype == "response.function_call_arguments.done":
            name = str(event.get("name") or "")
            call_id = str(event.get("call_id") or "")
            args = event.get("arguments")
            args_json = args if isinstance(args, str) else json.dumps(args or {})
            if self._on_function_call and name and call_id:
                try:
                    self._on_function_call(name, call_id, args_json)
                except Exception:
                    logger.warning("realtime function-call handler failed", exc_info=True)
        elif etype == "error":
            detail = event.get("error") or event.get("message") or event
            logger.warning("realtime voice server error: %s", detail)
            if not self._minimal_retry_done:
                # Full config may carry unsupported extras — downgrade once.
                self._minimal_retry_done = True
                self._send_event(build_session_update(self._cfg, minimal=True))


__all__ = [
    "CONSULT_TOOL_NAME",
    "DEFAULT_REALTIME_MODEL",
    "DEFAULT_SUPERVISOR_VOICE",
    "FRAME_MS",
    "FRAME_SAMPLES",
    "INPUT_SAMPLE_RATE",
    "OUTPUT_SAMPLE_RATE",
    "REALTIME_URL",
    "RealtimeConfig",
    "RealtimeVoiceError",
    "RealtimeVoiceSession",
    "build_session_update",
    "check_realtime_requirements",
    "load_realtime_config",
    "realtime_voice_enabled",
]
