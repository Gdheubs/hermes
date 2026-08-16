"""Pre-built Docker sandbox images ("sandbox builds").

Inspired by Cursor's Cloud Agent Builds (Aug 2026): instead of every fresh
sandbox container paying the clone/install cost at session start, Hermes can
bake a user-configured build command into a committed Docker image ahead of
time. Containers then boot from the prepared image, and the agent starts with
its tooling already installed.

Design properties (mirroring the upstream feature, adapted to a local-first
architecture):

- **Opt-in.** Nothing happens unless ``terminal.docker_build_command`` is set.
- **Fail-safe.** A failed build never replaces the active one — resolution
  only ever returns the latest *successful* build, so a broken install
  command degrades to the previous good image (or the raw base image).
- **Fingerprinted.** A build is keyed by (base image, build command). Change
  either and the stale build is ignored until a new one succeeds.
- **Staleness refresh.** When the active build is older than
  ``terminal.docker_build_refresh_hours`` (default 24, ``0`` disables), a
  background rebuild is kicked off at resolve time. The running session keeps
  the warm image; the *next* session picks up the refreshed one.
- **Observable.** Metadata + per-build logs live under
  ``<sandbox_dir>/builds/`` and are surfaced by ``hermes sandbox status``.

The build itself is intentionally simple: run the build command in a
container from the base image, ``docker commit`` the result, tag it
``hermes-sandbox-build:<fingerprint>``. Disk state only — running processes
do not survive the commit (same contract as Cursor's builds).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_IMAGE_REPO = "hermes-sandbox-build"
_BUILD_TIMEOUT_DEFAULT = 1800  # seconds; generous — installs can be slow
_LOCK = threading.Lock()
_refresh_started: set = set()  # fingerprints with an in-process background refresh


# ---------------------------------------------------------------------------
# Paths / metadata
# ---------------------------------------------------------------------------


def _builds_dir() -> Path:
    from tools.environments.base import get_sandbox_dir

    d = get_sandbox_dir() / "builds"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _metadata_path() -> Path:
    return _builds_dir() / "builds.json"


def _load_metadata() -> List[Dict[str, Any]]:
    path = _metadata_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("sandbox builds: unreadable metadata at %s", path, exc_info=True)
    return []


def _save_metadata(records: List[Dict[str, Any]]) -> None:
    path = _metadata_path()
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)
    os.replace(tmp, path)


def build_fingerprint(base_image: str, command: str) -> str:
    """Stable fingerprint for a (base image, build command) pair."""
    digest = hashlib.sha256(
        f"{base_image}\0{command}".encode("utf-8")
    ).hexdigest()
    return digest[:12]


def image_tag_for(fingerprint: str) -> str:
    return f"{_IMAGE_REPO}:{fingerprint}"


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def _docker_exe() -> str:
    try:
        from tools.environments.docker import find_docker

        return find_docker() or "docker"
    except Exception:
        return "docker"


def _image_exists(tag: str) -> bool:
    try:
        proc = subprocess.run(
            [_docker_exe(), "image", "inspect", tag],
            capture_output=True, timeout=30,
        )
        return proc.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Resolution (called from terminal_tool._create_environment)
# ---------------------------------------------------------------------------


def latest_successful(records: List[Dict[str, Any]], fingerprint: str) -> Optional[Dict[str, Any]]:
    hits = [
        r for r in records
        if r.get("fingerprint") == fingerprint and r.get("status") == "success"
    ]
    if not hits:
        return None
    return max(hits, key=lambda r: r.get("finished_at") or 0)


def resolve_image(base_image: str, container_config: Optional[Dict[str, Any]] = None) -> str:
    """Return the image a new Docker sandbox should boot from.

    When a successful sandbox build exists for the configured
    (base image, build command) pair, its committed tag is returned;
    otherwise *base_image* passes through unchanged. Never raises.
    """
    cc = container_config or {}
    command = str(cc.get("docker_build_command") or "").strip()
    if not command:
        return base_image
    try:
        fingerprint = build_fingerprint(base_image, command)
        record = latest_successful(_load_metadata(), fingerprint)
        if record is None:
            # No build yet — kick one off in the background so a future
            # session benefits; this session uses the base image (standard
            # startup flow, same as Cursor pre-first-build).
            _maybe_refresh_async(base_image, command, cc, reason="initial")
            return base_image
        tag = record.get("image_tag") or image_tag_for(fingerprint)
        if not _image_exists(tag):
            logger.info("sandbox builds: image %s missing, falling back to base", tag)
            _maybe_refresh_async(base_image, command, cc, reason="missing-image")
            return base_image
        _maybe_refresh_if_stale(base_image, command, cc, record)
        logger.info("sandbox builds: booting from prepared image %s", tag)
        return tag
    except Exception:
        logger.warning("sandbox builds: resolve failed, using base image", exc_info=True)
        return base_image


def _refresh_hours(cc: Dict[str, Any]) -> float:
    try:
        return max(float(cc.get("docker_build_refresh_hours", 24) or 0), 0.0)
    except (TypeError, ValueError):
        return 24.0


def _maybe_refresh_if_stale(
    base_image: str, command: str, cc: Dict[str, Any], record: Dict[str, Any],
) -> None:
    hours = _refresh_hours(cc)
    if hours <= 0:
        return
    finished_at = record.get("finished_at") or 0
    if (time.time() - finished_at) < hours * 3600:
        return
    _maybe_refresh_async(base_image, command, cc, reason="stale")


def _maybe_refresh_async(
    base_image: str, command: str, cc: Dict[str, Any], *, reason: str,
) -> None:
    """Start at most one background build per fingerprint per process."""
    fingerprint = build_fingerprint(base_image, command)
    with _LOCK:
        if fingerprint in _refresh_started:
            return
        _refresh_started.add(fingerprint)

    def _worker() -> None:
        try:
            logger.info("sandbox builds: background build (%s) for %s", reason, fingerprint)
            run_build(base_image, command, container_config=cc)
        except Exception:
            logger.warning("sandbox builds: background build failed", exc_info=True)

    threading.Thread(
        target=_worker, name=f"sandbox-build-{fingerprint}", daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Build execution
# ---------------------------------------------------------------------------


def run_build(
    base_image: str,
    command: str,
    *,
    container_config: Optional[Dict[str, Any]] = None,
    timeout: int = _BUILD_TIMEOUT_DEFAULT,
    stream_output: bool = False,
) -> Dict[str, Any]:
    """Run *command* in a container from *base_image* and commit the result.

    Returns the metadata record for the build (status ``success`` or
    ``failed``). A failed build leaves any previous successful build active.
    """
    cc = container_config or {}
    docker = _docker_exe()
    fingerprint = build_fingerprint(base_image, command)
    tag = image_tag_for(fingerprint)
    container_name = f"hermes-sandbox-build-{fingerprint}-{uuid.uuid4().hex[:8]}"
    log_path = _builds_dir() / f"build-{fingerprint}-{int(time.time())}.log"

    record: Dict[str, Any] = {
        "fingerprint": fingerprint,
        "base_image": base_image,
        "command": command,
        "image_tag": tag,
        "container_name": container_name,
        "started_at": time.time(),
        "finished_at": None,
        "status": "running",
        "log_path": str(log_path),
    }

    run_cmd = [
        docker, "run", "--name", container_name,
        # Deterministic entry: bash login shell so PATH setup in the image
        # (nvm, venvs) applies, mirroring how sandbox commands execute later.
        base_image, "bash", "-lc", command,
    ]
    shm = str(cc.get("docker_shm_size") or "").strip()
    if shm and shm != "0":
        run_cmd[2:2] = ["--shm-size", shm]

    status = "failed"
    exit_code: Optional[int] = None
    try:
        with open(log_path, "w", encoding="utf-8") as log_fh:
            log_fh.write(f"$ {' '.join(shlex.quote(c) for c in run_cmd)}\n")
            log_fh.flush()
            proc = subprocess.Popen(
                run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                log_fh.write(line)
                if stream_output:
                    print(line, end="")
            exit_code = proc.wait(timeout=timeout)
        if exit_code == 0:
            commit = subprocess.run(
                [docker, "commit",
                 "-c", 'CMD ["bash"]',
                 container_name, tag],
                capture_output=True, text=True, timeout=600,
            )
            if commit.returncode == 0:
                status = "success"
            else:
                with open(log_path, "a", encoding="utf-8") as log_fh:
                    log_fh.write(f"\ndocker commit failed: {commit.stderr}\n")
    except subprocess.TimeoutExpired:
        with open(log_path, "a", encoding="utf-8") as log_fh:
            log_fh.write(f"\nbuild timed out after {timeout}s\n")
        try:
            subprocess.run([docker, "kill", container_name], capture_output=True, timeout=60)
        except Exception:
            pass
    except Exception as exc:
        try:
            with open(log_path, "a", encoding="utf-8") as log_fh:
                log_fh.write(f"\nbuild error: {exc}\n")
        except Exception:
            pass
        logger.warning("sandbox builds: build execution failed", exc_info=True)
    finally:
        # Always remove the build container; the committed image carries the state.
        try:
            subprocess.run([docker, "rm", "-f", container_name], capture_output=True, timeout=60)
        except Exception:
            pass

    record["status"] = status
    record["exit_code"] = exit_code
    record["finished_at"] = time.time()

    with _LOCK:
        records = _load_metadata()
        records.append(record)
        # Bound growth: keep the newest 50 records.
        _save_metadata(records[-50:])

    if status == "success":
        logger.info("sandbox builds: build %s succeeded -> %s", fingerprint, tag)
        _prune_superseded_images(records, fingerprint, keep_tag=tag)
    else:
        logger.warning(
            "sandbox builds: build %s failed (exit=%s); previous build stays active. Log: %s",
            fingerprint, exit_code, log_path,
        )
    return record


def _prune_superseded_images(
    records: List[Dict[str, Any]], fingerprint: str, *, keep_tag: str,
) -> None:
    """Best-effort removal of images from other fingerprints (config changed)."""
    docker = _docker_exe()
    stale_tags = {
        r.get("image_tag") for r in records
        if r.get("status") == "success"
        and r.get("fingerprint") != fingerprint
        and r.get("image_tag")
    }
    for tag in stale_tags:
        try:
            subprocess.run([docker, "rmi", tag], capture_output=True, timeout=120)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI support
# ---------------------------------------------------------------------------


def clear_builds() -> int:
    """Remove all built images and metadata. Returns count of images removed."""
    docker = _docker_exe()
    records = _load_metadata()
    tags: set = {
        str(r.get("image_tag")) for r in records if r.get("image_tag")
    }
    removed = 0
    for tag in sorted(tags):
        try:
            proc = subprocess.run([docker, "rmi", tag], capture_output=True, timeout=120)
            if proc.returncode == 0:
                removed += 1
        except Exception:
            pass
    try:
        _metadata_path().unlink()
    except FileNotFoundError:
        pass
    return removed


def status_summary(base_image: str, command: str) -> Dict[str, Any]:
    """Structured status for ``hermes sandbox status``."""
    records = _load_metadata()
    result: Dict[str, Any] = {
        "configured": bool(command.strip()),
        "base_image": base_image,
        "command": command,
        "records": records[-10:],
        "active": None,
    }
    if command.strip():
        fingerprint = build_fingerprint(base_image, command)
        result["fingerprint"] = fingerprint
        active = latest_successful(records, fingerprint)
        if active and _image_exists(active.get("image_tag") or ""):
            result["active"] = active
    return result
