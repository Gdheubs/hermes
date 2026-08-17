#!/usr/bin/env python3
"""Repro: Weixin Silk voice messages skip STT — transcription broken.

Bug class: #32196 / #42084 — WeChat voice messages arrive in Silk
format, which STT backends (Whisper, faster-whisper) cannot decode.
The adapter downloaded the .silk file and the central STT pipeline
silently failed to transcribe non-Chinese audio.

On main: FAILS (no Silk->WAV conversion). With the fix: PASSES
(pilk.silk_to_wav conversion with .wav media_type).
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gateway.platforms.weixin import WeixinAdapter  # noqa: E402

src = inspect.getsource(WeixinAdapter._download_voice)

if "pilk.silk_to_wav" not in src:
    print("FAIL: _download_voice has no Silk->WAV conversion")
    sys.exit(1)

if "audio/wav" not in inspect.getsource(WeixinAdapter._collect_media):
    print("FAIL: _collect_media has no audio/wav media_type fallback")
    sys.exit(1)

print("PASS: _download_voice converts Silk->WAV via pilk")
print("PASS: _collect_media reports audio/wav for converted files")
sys.exit(0)
