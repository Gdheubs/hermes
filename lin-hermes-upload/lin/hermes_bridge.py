"""Authenticated client for the isolated Hermes Runtime service."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterator

import requests


class HermesBridgeError(RuntimeError):
    """A controlled failure at the Lin-to-Hermes service boundary."""


@dataclass(frozen=True)
class HermesRuntimeConfig:
    base_url: str
    token: str
    connect_timeout: float = 5.0
    read_timeout: float = 90.0

    @classmethod
    def from_env(cls) -> "HermesRuntimeConfig":
        return cls(
            base_url=os.getenv("HERMES_RUNTIME_URL", "").rstrip("/"),
            token=os.getenv("HERMES_RUNTIME_TOKEN", ""),
            connect_timeout=float(os.getenv("HERMES_RUNTIME_CONNECT_TIMEOUT", "5")),
            read_timeout=float(os.getenv("HERMES_RUNTIME_READ_TIMEOUT", "90")),
        )


class HermesBridge:
    def __init__(self, config: HermesRuntimeConfig | None = None, session: requests.Session | None = None):
        self.config = config or HermesRuntimeConfig.from_env()
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        if not self.config.base_url or not self.config.token:
            raise HermesBridgeError("Hermes Runtime is not configured")
        return {
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            response = self.session.request(
                method,
                f"{self.config.base_url}{path}",
                headers=self._headers(),
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                **kwargs,
            )
        except requests.RequestException as exc:
            raise HermesBridgeError(f"Hermes Runtime unavailable: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:500]
            raise HermesBridgeError(f"Hermes Runtime returned {response.status_code}: {detail}")
        return response

    def start_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/agent-runs", json=payload).json()

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/agent-runs/{run_id}").json()

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/agent-runs/{run_id}/cancel").json()

    def stream_events(self, run_id: str, after: int = 0) -> Iterator[dict[str, Any]]:
        response = self._request(
            "GET",
            f"/agent-runs/{run_id}/events",
            params={"after": after},
            stream=True,
        )
        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if raw_line and raw_line.startswith("data: "):
                    yield json.loads(raw_line[6:])
        except (ValueError, requests.RequestException) as exc:
            raise HermesBridgeError(f"Hermes Runtime event stream failed: {exc}") from exc
        finally:
            response.close()


def restricted_event(event: dict[str, Any]) -> dict[str, Any]:
    """Keep only the browser-safe lifecycle projection from Hermes events."""
    allowed = {
        "schema_version", "run_id", "sequence", "timestamp", "type", "status",
        "entity", "tool_name", "duration_ms", "args_preview", "result_preview", "error",
    }
    return {key: value for key, value in event.items() if key in allowed}
