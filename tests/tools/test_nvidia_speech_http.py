"""Tests for NVIDIA Nemotron Speech HTTP ASR and TTS providers."""

import subprocess

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def clean_nvidia_env(monkeypatch):
    for key in (
        "NVIDIA_API_KEY",
        "HERMES_SESSION_PLATFORM",
    ):
        monkeypatch.delenv(key, raising=False)
    # Existing provider tests exercise the bundled offline fallback. Catalog
    # behavior has focused tests below and never reaches the network here.
    monkeypatch.setattr(
        "tools.nvidia_speech_catalog.load_nvidia_speech_catalog", lambda: {}
    )


def _response(*, status=200, payload=None, content=b"", text=""):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload or {}
    response.content = content
    response.text = text
    return response


def test_asr_uses_tdt_and_forwards_word_boosting(tmp_path, monkeypatch):
    from tools.transcription_tools import (
        DEFAULT_NVIDIA_ASR_MODEL,
        _transcribe_nvidia,
    )

    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF-test")
    config = {
        "nvidia": {
            "boosted_words": ["Nemotron", "OpenClaw"],
            "boosted_words_score": 1.5,
            "custom_configuration": "key:value",
            "customizations": {"word_time_offsets": True},
        }
    }

    with patch(
        "tools.transcription_tools._resolve_provider_key",
        return_value="nvapi-profile-secret",
    ), patch(
        "tools.transcription_tools._load_stt_config", return_value=config
    ), patch("requests.post", return_value=_response(payload={"text": "hello"})) as post:
        result = _transcribe_nvidia(str(audio), DEFAULT_NVIDIA_ASR_MODEL)

    assert result == {
        "success": True,
        "transcript": "hello",
        "provider": "nvidia",
        "model": DEFAULT_NVIDIA_ASR_MODEL,
    }
    assert "d3fe9151-442b-4204-a70d-5fcc597fd610" in post.call_args.args[0]
    fields = post.call_args.kwargs["data"]
    assert ("boosted_lm_words", "Nemotron") in fields
    assert ("boosted_lm_words", "OpenClaw") in fields
    assert ("boosted_lm_score", "1.5") in fields
    assert ("custom_configuration", "key:value") in fields
    assert ("word_time_offsets", "true") in fields


def test_asr_falls_back_to_ctc_1_1b(tmp_path, monkeypatch):
    from tools.transcription_tools import (
        DEFAULT_NVIDIA_ASR_MODEL,
        FALLBACK_NVIDIA_ASR_MODEL,
        _transcribe_nvidia,
    )

    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF-test")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")

    with patch(
        "tools.transcription_tools._load_stt_config", return_value={"nvidia": {}}
    ), patch(
        "requests.post",
        side_effect=[
            _response(status=404, payload={"detail": "not ready"}),
            _response(payload={"text": "fallback transcript"}),
        ],
    ) as post:
        result = _transcribe_nvidia(str(audio), DEFAULT_NVIDIA_ASR_MODEL)

    assert result["success"] is True
    assert result["model"] == FALLBACK_NVIDIA_ASR_MODEL
    assert post.call_count == 2
    assert "1598d209-5e27-4d3c-8079-4751568b1081" in post.call_args.args[0]


def test_magpie_http_sends_customizations_and_writes_wav(tmp_path, monkeypatch):
    from tools.tts_tool import _generate_nvidia_tts

    output = tmp_path / "speech.wav"
    config = {
        "nvidia": {
            "voice": "Magpie-Multilingual.EN-US.Aria",
            "language": "en-US",
            "sample_rate_hz": 44100,
            "custom_dictionary": "Nemotron  pronunciation",
            "custom_configuration": {"key": "value"},
            "customizations": {"pace": "fast", "use_ssl": True},
        }
    }
    wav = b"RIFF-test-WAVE"

    with patch(
        "tools.tts_tool._resolve_provider_key",
        return_value="nvapi-profile-secret",
    ), patch(
        "requests.post", return_value=_response(content=wav)
    ) as post:
        result = _generate_nvidia_tts("<speak>Hello</speak>", str(output), config)

    assert result == str(output)
    assert output.read_bytes() == wav
    assert "877104f7-e885-42b9-8de8-f6e4c6303969" in post.call_args.args[0]
    fields = {name: value[1] for name, value in post.call_args.kwargs["files"]}
    assert fields["text"] == "<speak>Hello</speak>"
    assert fields["custom_dictionary"] == "Nemotron  pronunciation"
    assert fields["custom_configuration"] == "key:value"
    assert fields["pace"] == "fast"
    assert fields["use_ssl"] == "true"


def test_nvidia_tts_missing_key_is_actionable(tmp_path):
    from tools.tts_tool import _generate_nvidia_tts

    with patch("tools.tts_tool._resolve_provider_key", return_value=""), pytest.raises(
        ValueError, match="NVIDIA_API_KEY"
    ):
        _generate_nvidia_tts("Hello", str(tmp_path / "speech.wav"), {})


def test_asr_conversion_is_noninteractive_and_hides_windows_console(tmp_path):
    from hermes_cli._subprocess_compat import windows_hide_flags
    from tools.transcription_tools import (
        DEFAULT_NVIDIA_ASR_MODEL,
        _transcribe_nvidia,
    )

    audio = tmp_path / "sample.mp3"
    audio.write_bytes(b"fake-mp3")

    def fake_run(args, **kwargs):
        from pathlib import Path

        Path(args[-1]).write_bytes(b"RIFF-test")
        return MagicMock(returncode=0)

    with patch(
        "tools.transcription_tools._resolve_provider_key", return_value="nvapi-test"
    ), patch(
        "tools.transcription_tools._load_stt_config", return_value={"nvidia": {}}
    ), patch(
        "tools.transcription_tools._find_ffmpeg_binary", return_value="ffmpeg"
    ), patch(
        "tools.transcription_tools.subprocess.run", side_effect=fake_run
    ) as run, patch(
        "requests.post", return_value=_response(payload={"text": "hello"})
    ):
        result = _transcribe_nvidia(str(audio), DEFAULT_NVIDIA_ASR_MODEL)

    assert result["success"] is True
    assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL
    assert run.call_args.kwargs["creationflags"] == windows_hide_flags()


def test_asr_uses_catalog_http_fallback_when_tdt_is_grpc_only(tmp_path, monkeypatch):
    from tools.transcription_tools import DEFAULT_NVIDIA_ASR_MODEL, _transcribe_nvidia

    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF-test")
    catalog = {
        "models": [
            {
                "id": "nvidia/parakeet-tdt-default",
                "modality": "asr",
                "status": "transitioning",
                "cloud": {"transport": "grpc"},
            },
            {
                "id": "nvidia/parakeet-ctc-1.1b-asr",
                "modality": "asr",
                "status": "active",
                "cloud": {
                    "transport": "http",
                    "baseUrl": "https://catalog-ctc.invocation.api.nvcf.nvidia.com",
                    "defaultLanguage": "en-GB",
                },
            },
        ]
    }
    monkeypatch.setattr(
        "tools.nvidia_speech_catalog.load_nvidia_speech_catalog", lambda: catalog
    )

    with patch(
        "tools.transcription_tools._resolve_provider_key", return_value="nvapi-test"
    ), patch(
        "tools.transcription_tools._load_stt_config",
        return_value={"nvidia": {"language": ""}},
    ), patch(
        "requests.post", return_value=_response(payload={"text": "catalog transcript"})
    ) as post:
        result = _transcribe_nvidia(str(audio), DEFAULT_NVIDIA_ASR_MODEL)

    assert result["success"] is True
    assert result["model"] == "parakeet-ctc-1.1b-asr"
    assert post.call_count == 1
    assert post.call_args.args[0].startswith(
        "https://catalog-ctc.invocation.api.nvcf.nvidia.com/"
    )
    assert ("language", "en-GB") in post.call_args.kwargs["data"]


def test_asr_preserves_explicit_self_hosted_endpoint(tmp_path, monkeypatch):
    from tools.transcription_tools import DEFAULT_NVIDIA_ASR_MODEL, _transcribe_nvidia

    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF-test")
    load_catalog = MagicMock(side_effect=AssertionError("catalog should not be loaded"))
    monkeypatch.setattr(
        "tools.nvidia_speech_catalog.load_nvidia_speech_catalog", load_catalog
    )
    config = {
        "nvidia": {
            "tdt_base_url": "http://127.0.0.1:9000",
            "ctc_base_url": "http://127.0.0.1:9001",
        }
    }

    with patch(
        "tools.transcription_tools._resolve_provider_key", return_value="local-placeholder"
    ), patch(
        "tools.transcription_tools._load_stt_config", return_value=config
    ), patch(
        "requests.post", return_value=_response(payload={"text": "local transcript"})
    ) as post:
        result = _transcribe_nvidia(str(audio), DEFAULT_NVIDIA_ASR_MODEL)

    assert result["success"] is True
    assert post.call_args.args[0] == "http://127.0.0.1:9000/v1/audio/transcriptions"
    load_catalog.assert_not_called()


def test_magpie_uses_catalog_endpoint(tmp_path, monkeypatch):
    from tools.tts_tool import _generate_nvidia_tts

    catalog = {
        "models": [
            {
                "id": "nvidia/magpie-tts-multilingual",
                "modality": "tts",
                "status": "active",
                "cloud": {
                    "transport": "http",
                    "baseUrl": "https://catalog-magpie.invocation.api.nvcf.nvidia.com",
                    "defaultLanguage": "multi",
                },
            }
        ]
    }
    monkeypatch.setattr(
        "tools.nvidia_speech_catalog.load_nvidia_speech_catalog", lambda: catalog
    )
    output = tmp_path / "speech.wav"

    with patch(
        "tools.tts_tool._resolve_provider_key", return_value="nvapi-test"
    ), patch("requests.post", return_value=_response(content=b"RIFF-test")) as post:
        _generate_nvidia_tts("Hello", str(output), {"nvidia": {"language": ""}})

    assert post.call_args.args[0] == (
        "https://catalog-magpie.invocation.api.nvcf.nvidia.com/v1/audio/synthesize"
    )
    fields = {name: value[1] for name, value in post.call_args.kwargs["files"]}
    assert fields["language"] == "multi"
