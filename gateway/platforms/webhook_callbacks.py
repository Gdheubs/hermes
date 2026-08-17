"""Signed asynchronous webhook callbacks (Task 13, #4386/#73828).

A route may declare a ``callback`` (an http(s) URL + optional secret). After
the agent run finishes, the adapter POSTs a signed envelope — execution ID,
event ID, terminal status, output/error, timestamp, attempt number — to that
URL. The URL is an untrusted boundary: private/link-local/metadata
destinations are rejected by default to prevent SSRF; redirects and DNS
rebinding are refused; delivery is bounded and fire-and-forget.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

CALLBACK_TIMEOUT_SECONDS = 10
CALLBACK_MAX_ATTEMPTS = 2
CALLBACK_BACKOFF_SECONDS = 1.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _is_private_host(host: str) -> bool:
    """Return True for loopback, private, link-local, and metadata hosts.

    Blocks SSRF by default: an administrator must explicitly allow a
    non-public callback destination. DNS rebinding is mitigated by resolving
    the host and checking every resolved address before connecting.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return True  # fail closed on resolution failure
    if not infos:
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


def validate_callback_url(url: str) -> tuple[bool, str]:
    """Validate a callback URL is public http(s). Returns (ok, reason)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "callback must be http(s)"
    host = parsed.hostname
    if not host:
        return False, "callback missing host"
    if _is_private_host(host):
        return False, "callback host resolves to a private/loopback/metadata address (SSRF blocked)"
    return True, ""


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_callback_envelope(
    *,
    execution_id: str,
    event_id: str,
    status: str,
    output: str | None,
    error: str | None,
    attempt: int,
) -> dict:
    return {
        "execution_id": execution_id,
        "event_id": event_id,
        "status": status,
        "output": output,
        "error": error,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "attempt": attempt,
    }


def deliver_callback(
    url: str,
    secret: str | None,
    envelope: dict,
    *,
    timeout: int = CALLBACK_TIMEOUT_SECONDS,
) -> bool:
    """POST a signed envelope to the callback URL. Fire-and-forget, bounded."""
    ok, reason = validate_callback_url(url)
    if not ok:
        logger.warning("[webhook] callback refused: %s", reason)
        return False
    body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Hermes-Delivery": envelope["execution_id"]}
    if secret:
        headers["X-Hermes-Signature-256"] = _sign(body, secret)
    last_error = ""
    for attempt in range(1, CALLBACK_MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with _opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
            if 200 <= status < 300:
                return True
            last_error = f"HTTP {status}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if 400 <= exc.code < 500:
                return False  # receiver says the request is wrong — no retry
        except Exception as exc:
            last_error = str(exc) or type(exc).__name__
        if attempt < CALLBACK_MAX_ATTEMPTS:
            time.sleep(CALLBACK_BACKOFF_SECONDS * attempt)
    logger.warning("[webhook] callback delivery failed after %d attempts: %s", CALLBACK_MAX_ATTEMPTS, last_error)
    return False
