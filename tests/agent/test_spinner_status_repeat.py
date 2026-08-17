"""Regression test: KawaiiSpinner must not stack repeated status frames.

Bug #70031: on Windows + interface=cli + streaming=false, mid-turn status
frames repeated without clearing, filling output with stacked frames. Root
cause: KawaiiSpinner clears the previous frame with a bare carriage return
(\\r); when stdout is wrapped by prompt_toolkit's patch_stdout (or any proxy
that injects newlines around flush()), the \\r overwrite never lands on the
same line, so every animation tick is emitted on its own line.

This test proves the spinner suppresses its \\r animation when stdout is a
proxy that does not honor \\r in-place, writing the status only once instead of
stacking frames. It is platform-agnostic (no Windows needed) by simulating a
stdout whose \\r does not return to column 0.
"""

import threading
import time

import pytest

from agent.display import KawaiiSpinner


class _NoRewriteStdout:
    """Simulates a proxy stdout where \\r does NOT return to column 0.

    Real terminals honor ``\\r`` as 'go to column 0'; prompt_toolkit's
    StdoutProxy and some Windows consoles do not, so each write becomes its
    own line. We record writes as separate lines to model that.
    """

    def __init__(self):
        self.lines = []
        self._buf = ""

    def isatty(self):
        return True

    def write(self, text):
        # Emulate a stream that does NOT collapse \\r into the same line:
        # every chunk is appended as a new logical line.
        self.lines.append(text)
        return len(text)

    def flush(self):
        pass


class _PatchStdoutProxyLike:
    """Mimics prompt_toolkit's StdoutProxy (sets _patch_stdout)."""

    _patch_stdout = object()

    def isatty(self):
        return True

    def write(self, text):
        return len(text)

    def flush(self):
        pass


def _run_spinner_for(spinner, seconds=0.4):
    spinner.start()
    time.sleep(seconds)
    spinner.stop("done")
    # Let the animation thread finish.
    time.sleep(0.1)


def test_spinner_does_not_stack_frames_on_non_rewriting_stdout():
    """With a stdout that ignores \\r, the spinner must write only ONCE."""
    out = _NoRewriteStdout()
    sp = KawaiiSpinner(message="thinking", print_fn=lambda t: out.write(t))
    # Force the captured _out to our simulated stream so _is_tty/_is_proxy
    # inspect the right object.
    sp._out = out
    _run_spinner_for(sp)
    # Under a non-rewriting stream the spinner should NOT emit a stream of
    # distinct animation frames. It writes the single "[tool] message" line
    # (non-TTY-ish path) and the final "done" line — never a stack of frames.
    frame_like = [
        ln for ln in out.lines if "thinking" in ln and ln.strip().startswith("[tool]")
    ]
    assert len(frame_like) <= 1, (
        f"status stacked into {len(frame_like)} frames: {out.lines}"
    )


def test_spinner_treats_patch_stdout_proxy_as_suppressed():
    """A proxy-like stdout (has _patch_stdout) suppresses \\r animation.

    The animation loop must not run its per-tick \\r frames under a proxy;
    only the final stop() clear is allowed (a single harmless \\r, not a
    stack). We assert no \\r is written *during the run* (the stacking bug).
    """
    out = _PatchStdoutProxyLike()
    recorded = []
    sp = KawaiiSpinner(message="thinking", print_fn=lambda t: recorded.append(t))
    sp._out = out
    assert sp._is_patch_stdout_proxy() is True
    # Run, capturing only in-run writes (stop()'s single clear is excluded).
    sp.start()
    time.sleep(0.4)
    in_run = list(recorded)
    sp.stop("done")
    time.sleep(0.1)
    assert all("\r" not in line for line in in_run), (
        f"\\r frames leaked during run: {in_run}"
    )
