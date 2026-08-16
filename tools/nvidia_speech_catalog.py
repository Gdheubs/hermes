"""Validated, cached access to NVIDIA's hosted speech model catalog."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

NVIDIA_SPEECH_CATALOG_URL = (
    "https://raw.githubusercontent.com/nvidia-riva/Nemotron-speech-skills/"
    "main/skills/nemotron-speech/references/speech-models.v1.json"
)
NVIDIA_SPEECH_CATALOG_ID = "nvidia-nemotron-speech-cloud-models"
NVIDIA_SPEECH_CATALOG_TTL_SECONDS = 60 * 60
NVIDIA_SPEECH_CATALOG_RETRY_SECONDS = 5 * 60
NVIDIA_SPEECH_CATALOG_MAX_BYTES = 512 * 1024

_VALID_MODALITIES = frozenset({"asr", "tts", "nmt"})
_VALID_STATUSES = frozenset({"active", "transitioning", "deprecated"})
_VALID_TRANSPORTS = frozenset({"http", "grpc"})

_catalog_cache: Optional[dict[str, Any]] = None
_catalog_cache_time = 0.0
_catalog_retry_after = 0.0
_catalog_lock = threading.Lock()


@dataclass(frozen=True)
class NvidiaHostedHttpModel:
    """Trusted HTTP routing fields selected from the validated catalog."""

    model_id: str
    base_url: str
    default_language: Optional[str]


def _is_valid_function_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def _is_trusted_invocation_url(value: Any, function_id: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    expected_host = f"{function_id.lower()}.invocation.api.nvcf.nvidia.com"
    return (
        parsed.scheme == "https"
        and parsed.hostname == expected_host
        and parsed.port is None
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def _validate_catalog(payload: Any) -> dict[str, Any]:
    """Validate the v1 contract and reject the whole catalog on drift."""
    if not isinstance(payload, dict):
        raise ValueError("catalog root must be an object")
    if payload.get("schemaVersion") != 1:
        raise ValueError("unsupported catalog schemaVersion")
    if payload.get("catalogId") != NVIDIA_SPEECH_CATALOG_ID:
        raise ValueError("unexpected catalogId")

    defaults = payload.get("defaults")
    models = payload.get("models")
    if not isinstance(defaults, dict) or not isinstance(models, list):
        raise ValueError("catalog defaults/models are required")
    if not models or len(models) > 256:
        raise ValueError("catalog models must contain 1..256 entries")

    ids: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("catalog model entries must be objects")
        model_id = model.get("id")
        modality = model.get("modality")
        status = model.get("status")
        if (
            not isinstance(model_id, str)
            or not model_id.startswith("nvidia/")
            or model_id in ids
        ):
            raise ValueError("catalog model ids must be unique nvidia/* strings")
        if modality not in _VALID_MODALITIES or status not in _VALID_STATUSES:
            raise ValueError(f"invalid modality/status for {model_id}")
        ids.add(model_id)

        cloud = model.get("cloud")
        if not isinstance(cloud, dict):
            raise ValueError(f"missing cloud metadata for {model_id}")
        function_id = cloud.get("functionId")
        transport = cloud.get("transport")
        if not _is_valid_function_id(function_id) or transport not in _VALID_TRANSPORTS:
            raise ValueError(f"invalid cloud routing for {model_id}")
        if transport == "http" and not _is_trusted_invocation_url(
            cloud.get("baseUrl"), function_id
        ):
            raise ValueError(f"untrusted HTTP baseUrl for {model_id}")
        if transport == "grpc" and cloud.get("server") != "grpc.nvcf.nvidia.com:443":
            raise ValueError(f"untrusted gRPC server for {model_id}")

    for modality, choices in defaults.items():
        if modality not in _VALID_MODALITIES or not isinstance(choices, dict):
            raise ValueError("invalid catalog defaults")
        for model_id in choices.values():
            if not isinstance(model_id, str) or model_id not in ids:
                raise ValueError("catalog default references an unknown model")
    return payload


def _fetch_catalog() -> dict[str, Any]:
    response = requests.get(
        NVIDIA_SPEECH_CATALOG_URL,
        timeout=(2, 3),
        allow_redirects=False,
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    if len(response.content) > NVIDIA_SPEECH_CATALOG_MAX_BYTES:
        raise ValueError("catalog response is too large")
    return _validate_catalog(response.json())


def load_nvidia_speech_catalog() -> dict[str, Any]:
    """Return a fresh cached catalog, or an empty dict for local fallback."""
    global _catalog_cache, _catalog_cache_time, _catalog_retry_after

    now = time.monotonic()
    if (
        _catalog_cache is not None
        and now - _catalog_cache_time < NVIDIA_SPEECH_CATALOG_TTL_SECONDS
    ):
        return _catalog_cache
    if now < _catalog_retry_after:
        return _catalog_cache or {}

    with _catalog_lock:
        now = time.monotonic()
        if (
            _catalog_cache is not None
            and now - _catalog_cache_time < NVIDIA_SPEECH_CATALOG_TTL_SECONDS
        ):
            return _catalog_cache
        if now < _catalog_retry_after:
            return _catalog_cache or {}
        try:
            catalog = _fetch_catalog()
        except Exception as exc:  # noqa: BLE001 - remote catalog is optional
            _catalog_retry_after = now + NVIDIA_SPEECH_CATALOG_RETRY_SECONDS
            logger.warning(
                "NVIDIA speech catalog unavailable; using bundled defaults: %s", exc
            )
            return _catalog_cache or {}
        _catalog_cache = catalog
        _catalog_cache_time = now
        _catalog_retry_after = 0.0
        return catalog


def resolve_nvidia_hosted_http_model(
    model_id: str, *, modality: str
) -> Optional[NvidiaHostedHttpModel]:
    """Resolve a trusted active/transitioning HTTP deployment by stable ID."""
    catalog = load_nvidia_speech_catalog()
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list):
        return None
    for model in models:
        if model.get("id") != model_id or model.get("modality") != modality:
            continue
        if model.get("status") not in {"active", "transitioning"}:
            return None
        cloud = model.get("cloud")
        if not isinstance(cloud, dict) or cloud.get("transport") != "http":
            return None
        language = cloud.get("defaultLanguage")
        return NvidiaHostedHttpModel(
            model_id=model_id,
            base_url=cloud["baseUrl"].rstrip("/"),
            default_language=language.strip()
            if isinstance(language, str) and language.strip()
            else None,
        )
    return None


def _reset_nvidia_speech_catalog_cache_for_tests() -> None:
    global _catalog_cache, _catalog_cache_time, _catalog_retry_after
    with _catalog_lock:
        _catalog_cache = None
        _catalog_cache_time = 0.0
        _catalog_retry_after = 0.0
