"""Security and cache contracts for the NVIDIA speech model catalog."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from tools import nvidia_speech_catalog as catalog


def _payload():
    return {
        "schemaVersion": 1,
        "catalogId": "nvidia-nemotron-speech-cloud-models",
        "updatedAt": "2026-08-13T00:00:00Z",
        "defaults": {"asr": {"english": "nvidia/parakeet-ctc-1.1b-asr"}},
        "models": [
            {
                "id": "nvidia/parakeet-ctc-1.1b-asr",
                "displayName": "Parakeet CTC 1.1B English",
                "modality": "asr",
                "status": "active",
                "capabilities": {"languages": ["en-US"]},
                "selection": {"recommendedFor": ["English transcription"]},
                "cloud": {
                    "functionName": "ai-parakeet-ctc-1_1b-asr",
                    "functionId": "1598d209-5e27-4d3c-8079-4751568b1081",
                    "transport": "http",
                    "baseUrl": "https://1598d209-5e27-4d3c-8079-4751568b1081.invocation.api.nvcf.nvidia.com",
                    "requestStyle": "openai-audio",
                    "defaultLanguage": "en-US",
                },
            }
        ],
    }


@pytest.fixture(autouse=True)
def reset_catalog_cache():
    catalog._reset_nvidia_speech_catalog_cache_for_tests()
    yield
    catalog._reset_nvidia_speech_catalog_cache_for_tests()


def test_valid_catalog_resolves_trusted_http_model(monkeypatch):
    monkeypatch.setattr(
        catalog, "_fetch_catalog", lambda: catalog._validate_catalog(_payload())
    )

    resolved = catalog.resolve_nvidia_hosted_http_model(
        "nvidia/parakeet-ctc-1.1b-asr", modality="asr"
    )

    assert resolved is not None
    assert resolved.base_url.startswith("https://1598d209-")
    assert resolved.default_language == "en-US"


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://evil.example/speech",
        "http://1598d209-5e27-4d3c-8079-4751568b1081.invocation.api.nvcf.nvidia.com",
        "https://1598d209-5e27-4d3c-8079-4751568b1081.invocation.api.nvcf.nvidia.com.evil.example",
        "https://1598d209-5e27-4d3c-8079-4751568b1081.invocation.api.nvcf.nvidia.com/path",
    ],
)
def test_catalog_rejects_untrusted_credential_destination(bad_url):
    payload = deepcopy(_payload())
    payload["models"][0]["cloud"]["baseUrl"] = bad_url

    with pytest.raises(ValueError, match="untrusted HTTP baseUrl"):
        catalog._validate_catalog(payload)


def test_catalog_is_cached_for_process_ttl(monkeypatch):
    fetch = MagicMock(return_value=catalog._validate_catalog(_payload()))
    monkeypatch.setattr(catalog, "_fetch_catalog", fetch)

    first = catalog.load_nvidia_speech_catalog()
    second = catalog.load_nvidia_speech_catalog()

    assert first is second
    fetch.assert_called_once_with()


def test_invalid_catalog_falls_back_and_throttles_retry(monkeypatch):
    fetch = MagicMock(side_effect=ValueError("bad catalog"))
    monkeypatch.setattr(catalog, "_fetch_catalog", fetch)

    assert catalog.load_nvidia_speech_catalog() == {}
    assert catalog.load_nvidia_speech_catalog() == {}
    fetch.assert_called_once_with()


def test_fetch_uses_short_timeout_and_does_not_follow_redirects(monkeypatch):
    response = MagicMock()
    response.content = b"{}"
    response.json.return_value = _payload()
    get = MagicMock(return_value=response)
    monkeypatch.setattr(catalog.requests, "get", get)

    catalog._fetch_catalog()

    get.assert_called_once_with(
        catalog.NVIDIA_SPEECH_CATALOG_URL,
        timeout=(2, 3),
        allow_redirects=False,
        headers={"Accept": "application/json"},
    )
