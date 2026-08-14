"""Bootstrap for the managed runtime: config -> installed binaries ->
running supervised server.

One public call, ``ensure_local_runtime(config)``, safe to call at any
session start:
- disabled or already-running (state file answers /health) -> no-op
- enabled -> install binaries if missing (idempotent), spawn supervisor

Kept import-light: callers gate on config before importing this module so
sessions with local_runtime disabled never pay the import.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_SUPERVISOR = None  # process-wide singleton; one router per Hermes process


def _detect_gpu_vendor() -> str | None:
    """Best-effort GPU vendor for backend selection. NVIDIA via nvidia-smi;
    anything else defers to select_backend's fallback ladder."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return "nvidia " + out.stdout.strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def models_dir() -> Path:
    return get_hermes_home() / "models"


def assets_dir() -> Path:
    """Non-model companion files (mmproj vision projectors, spec-decode
    draft models). A subdirectory so the router's model listing — and our
    staged_models() — never mistakes an asset for a servable model."""
    return models_dir() / "assets"


def staged_models() -> "list[Path]":
    """Servable staged models: first-parts of split GGUFs count once,
    continuation parts and assets/ don't count at all."""
    import re

    part = re.compile(r"-(\d{5})-of-\d{5}\.gguf$")
    out = []
    for p in sorted(models_dir().glob("*.gguf")):
        m = part.search(p.name)
        if m and m.group(1) != "00001":
            continue
        out.append(p)
    return out


def staged_model_ids() -> "list[str]":
    import re

    return [re.sub(r"-\d{5}-of-\d{5}$", "", p.stem) for p in staged_models()]


def refresh_local_runtime() -> bool:
    """Restart the managed server so it rescans the models directory.

    The router's model list is SPAWN-ONLY: a GGUF added after start is
    invisible to GET /models and 400s on completion, so anything that
    changes the staged set while the server runs must bounce it. Only
    touches a server THIS process supervises; returns False when there is
    nothing to refresh (next boot scans fresh anyway)."""
    global _SUPERVISOR
    if _SUPERVISOR is None:
        return False
    try:
        from hermes_cli.config import load_config

        shutdown_local_runtime()
        return ensure_local_runtime(load_config(), force=True) is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("local runtime refresh failed: %s", exc)
        return False


def ensure_local_runtime(config: dict, force: bool = False) -> "object | None":
    """Idempotent boot of the managed runtime. Returns the supervisor (or
    None when disabled/unavailable). Never raises into a session start —
    failures log and return None; chat falls back to configured providers.

    ``force=True`` skips the enabled gate — used by the explicit "Use this
    model" action, where the click IS the opt-in (the caller records it in
    config so future boots auto-start).
    """
    global _SUPERVISOR
    section = (config or {}).get("local_runtime") or {}
    if not force and not section.get("enabled"):
        return None
    if _SUPERVISOR is not None:
        return _SUPERVISOR

    # Residency: no staged models means nothing to serve — don't boot an
    # empty server. The walked-away story handled with zero configuration
    # (delete your last model and boots stop); Use force-boots as ever.
    if not force and not staged_models():
        logger.info("local runtime enabled but no models staged; not booting")
        return None

    # Another Hermes process may already be supervising — reuse via state.
    from hermes_cli.local_runtime.endpoint import _state_endpoint

    if _state_endpoint() is not None:
        logger.info("managed llama-server already running (another process)")
        return None

    try:
        from hermes_cli.local_runtime.binaries import (
            ensure_runtime_installed,
            select_backend,
        )
        from hermes_cli.local_runtime.hardware import probe_budget
        from hermes_cli.local_runtime.presets import generate_presets
        from hermes_cli.local_runtime.supervisor import LlamaServerSupervisor

        backend = section.get("backend", "auto")
        if backend == "auto":
            backend = select_backend(_detect_gpu_vendor())
        # Boot ladder: serve what is INSTALLED, never download here. The
        # configured tag (config root-of-trust; deep-merge supplies the
        # Hermes-release default when unpinned) is preferred; when it isn't
        # installed yet, the newest installed tag serves and the status
        # endpoint reports the pending update — the download is a deliberate
        # button click in the pane, not a boot-path surprise (a multi-minute
        # inline download here is exactly how the onboarding bounce returns).
        from hermes_cli.local_runtime.binaries import default_tag, installed_tags

        tag = section.get("tag") or default_tag()
        have = installed_tags()
        if tag not in have:
            if not have:
                logger.info("local runtime enabled but no build installed; "
                            "install happens in the Local Models pane")
                return None
            logger.info("configured tag %s not installed; serving %s "
                        "(update is a click in Local Models)", tag, have[0])
            tag = have[0]
        install_dir = ensure_runtime_installed(tag, backend)

        mdir = models_dir()
        mdir.mkdir(parents=True, exist_ok=True)

        # Context policy: one launch decision per staged model, carried to
        # the router via the preset INI. Priced against CAPACITY, not live
        # free VRAM: this runs while the outgoing server instance may still
        # hold the card (restart, refresh after a download), and its memory
        # is freed before the new instance loads anything. Pricing against
        # live-free here once pinned a fitting model's weights to CPU
        # because the probe saw the predecessor's VRAM as gone.
        preset_path = get_hermes_home() / "runtimes" / "llamacpp" / "presets.ini"
        try:
            entries = generate_presets(mdir, probe_budget(planning=True), preset_path)
            for entry in entries:
                if entry.refusal:
                    logger.warning("model refused by physics check: %s", entry.refusal)
        except Exception as exc:  # noqa: BLE001 — policy failure must not block serving
            logger.warning("preset generation failed (%s); router runs stock fit", exc)
            preset_path = None

        sup = LlamaServerSupervisor(
            install_dir, mdir,
            models_max=int(section.get("models_max", 4)),
            port=int(section.get("port", 0)) or None,
            preset_path=preset_path,
        )
        sup.start()
        _SUPERVISOR = sup
        logger.info("managed llama-server up at %s (backend=%s tag=%s)",
                    sup.base_url, backend, tag)
        _start_idle_sweeper(sup)
        return sup
    except Exception as exc:  # noqa: BLE001 — never break session start
        logger.warning("managed local runtime unavailable: %s", exc)
        return None


def shutdown_local_runtime() -> None:
    global _SUPERVISOR
    if _SUPERVISOR is not None:
        _SUPERVISOR.stop()
        _SUPERVISOR = None


def get_supervisor():
    """The process-local supervisor, or None (server may still be running
    under another process — check the state file)."""
    return _SUPERVISOR


def _start_idle_sweeper(sup) -> None:
    """Idle-residency loop: every couple of minutes, unload non-primary
    models idle past the supervisor's threshold. Daemon thread tied to the
    supervisor's lifetime — exits when the server stops."""
    import threading

    def _loop():
        while sup.proc is not None and sup.proc.poll() is None:
            time.sleep(120)
            try:
                sup.sweep_idle()
            except Exception as exc:  # noqa: BLE001
                logger.debug("idle sweep skipped: %s", exc)

    threading.Thread(target=_loop, daemon=True,
                     name="local-runtime-idle-sweep").start()
