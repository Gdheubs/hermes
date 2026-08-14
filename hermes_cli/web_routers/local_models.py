"""Local-models dashboard routes — the desktop's window into the managed
llama.cpp runtime.

Everything here is designed for a first-run user on an RTX laptop: every
payload carries plain-language, pre-formatted facts the UI can show verbatim
(what will this model do ON THIS MACHINE, how big is the download, what is
the runtime doing right now), never raw internals the renderer would have to
interpret.

Long jobs (runtime install, model download) follow the repo's job pattern:
start-POST -> {job_id} -> GET poll with byte progress. Downloads are
sha256-verified; a hash mismatch deletes the file and reports it plainly.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

_GIB = 1 << 30
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _human_gb(n: int | float) -> str:
    return f"{n / _GIB:.1f} GB"


def _job(kind: str, target: str, model_id: str | None = None) -> Dict[str, Any]:
    job = {
        "job_id": uuid.uuid4().hex[:12],
        "kind": kind,               # "runtime-install" | "model-download"
        "target": target,
        "model_id": model_id,       # catalog id for downloads; None otherwise
        "status": "running",        # running | done | error
        "phase": "starting",        # human-readable step name
        "detail": "",
        "total_bytes": None,
        "done_bytes": 0,
        "started_at": time.time(),
        "error": None,
    }
    with _JOBS_LOCK:
        _JOBS[job["job_id"]] = job
    return job


# ── fast download: ranged parallel streams ───────────────────

# One TCP stream to a CDN rarely fills a fast line; 8 ranged connections
# writing into a preallocated file saturate consumer gigabit. sha256 is
# computed in a sequential pass afterwards (NVMe read is seconds, and it
# keeps the hash independent of write ordering).
_DOWNLOAD_CONNECTIONS = 8
_CHUNK = 4 << 20


def _probe_range_support(url: str) -> int:
    """Total size when the server honors Range requests, else 0.

    Auth-shaped failures raise with a plain-language message — a 401/403
    from the CDN means the repo is gated or the catalog entry names a
    wrong repo, and the user deserves better than a bare status code.
    """
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status == 206:
                content_range = r.headers.get("Content-Range", "")
                if "/" in content_range:
                    return int(content_range.rsplit("/", 1)[1])
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError(
                "The model host refused the download (gated or moved). "
                "This is a catalog problem, not yours — please report it.") from exc
        raise
    except Exception:  # noqa: BLE001
        pass
    return 0


def _model_id_for(gguf: Path) -> str:
    """Variant model id for a staged file (strips split-part suffixes)."""
    import re

    return re.sub(r"-\d{5}-of-\d{5}$", "", gguf.stem)


def _variant_files_on_disk(model_id: str) -> "list[Path]":
    """Every local file belonging to a staged model: all split parts plus
    its catalog-declared assets (mmproj/draft) when present."""
    from hermes_cli.local_runtime.bootstrap import assets_dir
    from hermes_cli.local_runtime.catalog import find_entry_for_model

    mdir = _models_dir()
    files = [p for p in mdir.glob("*.gguf") if _model_id_for(p) == model_id]
    hit = find_entry_for_model(model_id)
    if hit is not None:
        entry, _variant = hit
        for asset in (entry.mmproj, entry.draft):
            if asset is not None:
                p = assets_dir() / asset.local_name
                if p.exists():
                    files.append(p)
    return files


def download_file(url: str, dest: Path, job: Dict[str, Any],
                  expected_sha256: str = "", *,
                  base_done: int = 0, keep_totals: bool = False) -> None:
    """Download url -> dest with byte progress on ``job``.

    Ranged-parallel when the server supports it, single-stream fallback
    otherwise. Verifies sha256 when given; a mismatch deletes the file and
    raises with a plain-language message. Never leaves a .part behind.

    Multi-file variants: ``base_done`` offsets the progress so this file's
    bytes accumulate onto the files before it, and ``keep_totals=True``
    stops the per-file size from overwriting the variant's total.
    """
    import hashlib
    import shutil
    import threading as _threading

    tmp = dest.with_suffix(".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    file_done = [0]
    progress_lock = _threading.Lock()

    def bump(n: int) -> None:
        with progress_lock:
            file_done[0] += n
            job["done_bytes"] = base_done + file_done[0]

    try:
        total = _probe_range_support(url)
        if total:
            if not keep_totals:
                job["total_bytes"] = total
            # Preallocate so each worker writes at its own offset.
            with open(tmp, "wb") as f:
                f.truncate(total)
            errors: list[Exception] = []
            bounds = [(i * total // _DOWNLOAD_CONNECTIONS,
                       (i + 1) * total // _DOWNLOAD_CONNECTIONS - 1)
                      for i in range(_DOWNLOAD_CONNECTIONS)]

            def fetch_range(start: int, end: int) -> None:
                try:
                    req = urllib.request.Request(
                        url, headers={"Range": f"bytes={start}-{end}"})
                    with urllib.request.urlopen(req, timeout=120) as r, \
                            open(tmp, "r+b") as f:
                        f.seek(start)
                        while True:
                            chunk = r.read(_CHUNK)
                            if not chunk:
                                break
                            f.write(chunk)
                            bump(len(chunk))
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [_threading.Thread(target=fetch_range, args=b, daemon=True,
                                         name=f"lm-dl-{i}")
                       for i, b in enumerate(bounds)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            if errors:
                raise errors[0]
            if file_done[0] != total:
                raise RuntimeError(
                    f"download incomplete ({file_done[0]} of {total} bytes)")
        else:
            # No range support: single stream, large chunks.
            with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
                length = int(r.headers.get("Content-Length") or 0)
                if length and not keep_totals:
                    job["total_bytes"] = length
                while True:
                    chunk = r.read(_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    bump(len(chunk))

        if expected_sha256:
            job["phase"] = "verifying"
            job["detail"] = "Checking file integrity"
            digest = hashlib.sha256()
            with open(tmp, "rb") as f:
                for chunk in iter(lambda: f.read(16 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise RuntimeError(
                    "Downloaded file failed its integrity check and was removed — try again")
        shutil.move(str(tmp), str(dest))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _models_dir() -> Path:
    from hermes_cli.local_runtime.bootstrap import models_dir

    return models_dir()


def _load_config() -> dict:
    from hermes_cli.config import load_config

    try:
        return load_config()
    except Exception:  # noqa: BLE001
        return {}


def _runtime_section() -> dict:
    return (_load_config() or {}).get("local_runtime") or {}


# ── status: the one call the pane opens with ─────────────────


@router.get("/api/local-models/status")
async def local_models_status():
    """Cheap, immediate, never blocks on probes (responsiveness standard):
    config state + installed runtime + staged models + supervisor state.
    GPU facts come from /api/local-models/hardware (slower, polled)."""
    from hermes_cli.local_runtime.binaries import (
        default_tag,
        installed_tags,
        runtimes_root,
        server_binary,
    )
    from hermes_cli.local_runtime.endpoint import _state_endpoint

    section = _runtime_section()
    configured_tag = section.get("tag") or default_tag()
    have = installed_tags()

    # The tag actually serving (boot ladder: configured if installed, else
    # newest installed). Present tense for the pane header.
    tag = configured_tag if configured_tag in have else (have[0] if have else configured_tag)

    # A pending engine update exists when the user runs the local engine
    # (enabled + something installed) and the configured tag — pinned or
    # the Hermes-release default — is newer than anything on disk. The
    # download is a button click, never automatic.
    update_available = bool(
        section.get("enabled") and have and configured_tag not in have)

    runtime_installed = False
    runtime_backend = None
    root = runtimes_root() / tag
    if root.exists():
        for backend_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            try:
                server_binary(backend_dir)
                runtime_installed = True
                runtime_backend = backend_dir.name
                break
            except Exception:  # noqa: BLE001
                continue

    staged = []
    mdir = _models_dir()
    if mdir.exists():
        from hermes_cli.local_runtime.bootstrap import staged_models

        # Split models: report the whole variant's bytes, not one part's.
        from hermes_cli.local_runtime.catalog import find_entry_for_model

        for gguf in staged_models():
            model_id = _model_id_for(gguf)
            size = gguf.stat().st_size
            hit = find_entry_for_model(model_id)
            if hit is not None:
                size = hit[1].size_bytes
            staged.append({
                "id": model_id,
                "size_bytes": size,
                "size_label": _human_gb(size),
            })

    running = _state_endpoint()

    # Which staged models are resident right now (loaded in VRAM). Read
    # from the live router when it's up; {} when down. Feeds the pane's
    # Loaded pills and eject buttons.
    loaded: Dict[str, str] = {}
    placement: Dict[str, Any] = {}
    if running is not None:
        try:
            import urllib.request as _url

            req = _url.Request(
                running["base_url"].rsplit("/v1", 1)[0] + "/models",
                headers={"Authorization": f"Bearer {running.get('api_key', '')}"})
            with _url.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
            loaded = {
                m["id"]: m.get("status", {}).get("value", "unknown")
                for m in data.get("data", [])
                # Everything resident or becoming resident: 'loading' renders
                # as its own state in the pane (a 20-GB load in flight is the
                # single most important thing the pane can show).
                if m.get("status", {}).get("value") in ("loaded", "ready", "loading")
            }
            # How each loaded model is actually running: the granted window
            # from the child itself, and the plan's spill facts from the
            # preset decision. The pane shows this verbatim — placement is
            # the difference between 'fast' and 'why is my CPU busy', so it
            # must be inspectable, not inferred from Task Manager.
            from hermes_cli.local_runtime.presets import read_preset_decisions

            decisions = read_preset_decisions()
            for model_id in loaded:
                entry_facts: Dict[str, Any] = {}
                plan = decisions.get(model_id)
                if plan is not None:
                    entry_facts["window"] = plan.window
                    entry_facts["window_label"] = f"{plan.window // 1024}K"
                    entry_facts["spilled"] = plan.spilled
                if loaded[model_id] in ("loaded", "ready"):
                    try:
                        preq = _url.Request(
                            running["base_url"].rsplit("/v1", 1)[0]
                            + f"/props?model={model_id}",
                            headers={"Authorization":
                                     f"Bearer {running.get('api_key', '')}"})
                        with _url.urlopen(preq, timeout=3) as pr:
                            props = json.loads(pr.read())
                        n_ctx = (props.get("default_generation_settings", {})
                                 .get("n_ctx"))
                        if n_ctx:
                            entry_facts["granted_window"] = int(n_ctx)
                            entry_facts["granted_window_label"] = f"{int(n_ctx) // 1024}K"
                    except Exception:  # noqa: BLE001
                        pass
                if entry_facts:
                    placement[model_id] = entry_facts
        except Exception as exc:  # noqa: BLE001
            # Never silent: an empty dict here renders as 'Not in memory'
            # on a machine whose VRAM is visibly full.
            logger.warning("loaded-models read failed: %r", exc)
            loaded = {}

    # The active main model, when it is one of ours (config authority: the
    # same model.provider + model.default that /api/model/set writes).
    active_model_id = None
    try:
        config = _load_config()
        model_section = (config or {}).get("model") or {}
        if str(model_section.get("provider", "")).strip().lower() in (
                "llamacpp", "llama.cpp", "llama-cpp"):
            active_model_id = str(
                model_section.get("default") or model_section.get("name") or ""
            ).strip() or None
    except Exception:  # noqa: BLE001
        pass

    return {
        "enabled": bool(section.get("enabled")),
        "tag": tag,
        "configured_tag": configured_tag,
        "update_available": update_available,
        "runtime_installed": runtime_installed,
        "runtime_backend": runtime_backend,
        "server_running": running is not None,
        "server_base_url": (running or {}).get("base_url"),
        "active_model_id": active_model_id,
        "loaded_models": loaded,
        "placement": placement,
        "models": staged,
        "models_dir": str(mdir),
    }


# ── hardware: what this machine can do ───────────────────────


@router.get("/api/local-models/hardware")
async def local_models_hardware():
    """The budget as plain facts. Polled by the pane and the statusbar
    resource item (throttled client-side)."""
    from hermes_cli.local_runtime.hardware import probe_budget, _nvidia_vram, _ram_bytes

    budget = probe_budget()
    ram_total, ram_avail = _ram_bytes()
    out = {
        "uma": budget.uma,
        "vram_total_bytes": budget.total_device_bytes,
        "vram_usable_bytes": budget.usable_vram_bytes,
        "ram_total_bytes": ram_total,
        "ram_available_bytes": ram_avail,
        "vram_label": _human_gb(budget.total_device_bytes),
        "gpu_name": None,
        "gpu_util_percent": None,
        "vram_used_bytes": None,
    }
    # GPU identity + live utilization (NVIDIA; other vendors degrade to None
    # and the UI hides those readouts).
    try:
        import subprocess

        smi = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if smi.returncode == 0 and smi.stdout.strip():
            name, util, used_mib = (x.strip() for x in smi.stdout.strip().splitlines()[0].split(","))
            out["gpu_name"] = name
            out["gpu_util_percent"] = int(util)
            out["vram_used_bytes"] = int(used_mib) << 20
    except Exception:  # noqa: BLE001
        pass
    return out


# ── catalog: priced for THIS machine before download ─────────


@router.get("/api/local-models/catalog")
async def local_models_catalog():
    """Every entry answers the user's three questions up front: how big is
    the download, will it fit, and what context/speed shape will I get —
    computed from the catalog's measured numbers + this machine's
    budget. Hardware-aware quant selection: the row advertises the BEST
    build for this machine (highest quality that runs fully on the GPU at
    the 64K floor; else the smallest that works, spilled and priced). No
    entry is hidden; unaffordable models show WHY."""
    from hermes_cli.local_runtime.catalog import CATALOG, select_variant
    from hermes_cli.local_runtime.context_policy import (
        RUNTIME_OVERHEAD_BYTES,
        initial_window,
    )
    from hermes_cli.local_runtime.estimator import PhysicsRefusal
    from hermes_cli.local_runtime.hardware import probe_budget

    # Planning budget: price against machine capacity, not live-free VRAM.
    # A loaded model must not make the catalog call every row unaffordable.
    budget = probe_budget(planning=True)
    mdir = _models_dir()
    entries = []
    for entry in CATALOG:
        choice = select_variant(entry, budget)
        # Any variant of this family already on disk counts as downloaded
        # (split variants stage under their first part).
        staged_ids = {_model_id_for(p) for p in mdir.glob("*.gguf")} if mdir.exists() else set()
        downloaded_variant = next(
            (v for v in entry.variants if v.model_id in staged_ids), None)
        row: Dict[str, Any] = {
            "id": entry.id,
            "display_name": entry.display_name,
            "description": entry.description,
            "native_context": entry.n_ctx_train,
            "native_context_label": f"{entry.n_ctx_train // 1024}K",
            "tags": list(entry.tags),
            "downloaded": downloaded_variant is not None,
            "downloaded_model_id": downloaded_variant.model_id if downloaded_variant else None,
            "downloaded_quant": downloaded_variant.quant if downloaded_variant else None,
            "mtp": entry.mtp,
            "vision": entry.mmproj is not None,
        }
        if choice is None:
            smallest = min(entry.variants, key=lambda v: v.size_bytes)
            smallest_total = entry.download_bytes(smallest)
            row.update({
                "fits": False,
                "size_bytes": smallest_total,
                "size_label": _human_gb(smallest_total),
                "fit_summary": "Needs more memory than this machine has",
                "fit_detail": (f"even the most compact build ({smallest.quant}, "
                               f"{_human_gb(smallest_total)}) exceeds GPU + system memory"),
            })
            entries.append(row)
            continue

        variant = choice.variant
        profile = entry.profile(variant)
        # Same overhead the launch decision prices (runtime buffers +
        # vision projector): the row must advertise the window the model
        # will actually get, not a paper number the server's own fit then
        # shaves down.
        overhead = RUNTIME_OVERHEAD_BYTES + (
            entry.mmproj.size_bytes if entry.mmproj else 0)
        decision = initial_window(profile, budget, overhead_bytes=overhead)
        download_total = entry.download_bytes(variant)
        row.update({
            "fits": True,
            "model_id": variant.model_id,
            "quant": variant.quant,
            "quant_validated": variant.validated,
            "size_bytes": download_total,
            "size_label": _human_gb(download_total),
            "variant_count": len(entry.variants),
        })
        if choice.reason_key == "best-large-window":
            best = entry.variants[0]
            row["quant_reason"] = (
                "Best quality build — runs fully on your GPU with a large context window"
                if variant.quant == best.quant
                else (f"Best balance for your GPU ({variant.quant}) — a larger "
                      "build would shrink the context window"))
        elif choice.reason_key == "best-fits":
            best = entry.variants[0]
            row["quant_reason"] = (
                "Best quality build — runs fully on your GPU"
                if variant.quant == best.quant
                else f"Highest quality that runs fully on your GPU ({variant.quant})")
        else:
            row["quant_reason"] = (
                f"Compact build sized for this machine ({variant.quant}) — "
                "larger than GPU memory, runs slower")
        if not isinstance(decision, PhysicsRefusal):
            row["start_window"] = decision.window
            row["start_window_label"] = f"{decision.window // 1024}K"
            row["spilled"] = decision.spilled
            if decision.window >= entry.n_ctx_train:
                shape = f"runs at its full {row['native_context_label']} context"
            else:
                shape = (f"starts at {row['start_window_label']} and grows toward "
                         f"{row['native_context_label']} as you use it")
            if decision.spilled:
                shape += " (larger than your GPU memory — runs slower)"
            row["fit_summary"] = shape
        else:
            row["fit_summary"] = row["quant_reason"]
        entries.append(row)
    return {"models": entries}


# ── runtime install (job) ────────────────────────────────────


class RuntimeInstallBody(BaseModel):
    backend: Optional[str] = None   # None/auto -> detect


@router.post("/api/local-models/runtime/install")
async def local_models_runtime_install(body: RuntimeInstallBody):
    from hermes_cli.local_runtime.binaries import (
        default_tag,
        resolve_assets,
        select_backend,
    )
    from hermes_cli.local_runtime.bootstrap import _detect_gpu_vendor

    section = _runtime_section()
    tag = section.get("tag") or default_tag()
    backend = body.backend or section.get("backend", "auto")
    if backend == "auto":
        backend = select_backend(_detect_gpu_vendor())
    # Resolve first so an impossible combination fails the POST, not the job.
    try:
        plan = resolve_assets(tag, backend)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))

    job = _job("runtime-install", f"llama.cpp {tag} ({backend})")

    def _run():
        try:
            from hermes_cli.local_runtime.binaries import (
                ensure_runtime_installed,
                installed_tags,
                prune_old_tags,
            )

            previous = installed_tags()
            job["phase"] = "downloading"
            job["detail"] = f"Fetching {len(plan.assets)} package(s) for {backend}"
            ensure_runtime_installed(tag, backend)

            # Engine update path: a server already running on an older tag
            # moves to the new one now — the click was the consent. Fresh
            # installs (no server) skip this; Use/boot handles their start.
            restarted = False
            try:
                from hermes_cli.local_runtime.bootstrap import (
                    ensure_local_runtime,
                    get_supervisor,
                    shutdown_local_runtime,
                )

                sup = get_supervisor()
                if sup is not None and previous and tag not in previous:
                    job["phase"] = "restarting"
                    job["detail"] = "Switching the running server to the new build"
                    shutdown_local_runtime()
                    ensure_local_runtime(_load_config(), force=True)
                    restarted = True
            except Exception as exc:  # noqa: BLE001
                # The new build is installed either way; the next boot serves
                # it. Never fail the job on the restart nicety.
                logger.warning("post-update restart skipped: %s", exc)

            # N-1 retention, only after the new tag verified: keep it and the
            # newest previous build as the rollback pin target.
            try:
                keep = [tag] + [t for t in previous if t != tag][:1]
                prune_old_tags(keep)
            except Exception as exc:  # noqa: BLE001
                logger.warning("runtime prune skipped: %s", exc)

            job["phase"] = "done"
            job["status"] = "done"
            job["detail"] = (f"llama.cpp {tag} ready ({backend})"
                             + (" — server restarted on the new build" if restarted else ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("runtime install failed: %s", exc)
            job["status"] = "error"
            job["error"] = str(exc)

    threading.Thread(target=_run, daemon=True, name="lr-runtime-install").start()
    return {"job_id": job["job_id"], "backend": backend, "tag": tag}


# ── model download (job with byte progress + sha256) ─────────


class ModelDownloadBody(BaseModel):
    model_id: str


@router.post("/api/local-models/download")
async def local_models_download(body: ModelDownloadBody):
    """Accepts either a family id (downloads this machine's selected
    variant) or an exact variant model_id."""
    from hermes_cli.local_runtime.catalog import (
        CATALOG,
        catalog_by_id,
        select_variant,
    )
    from hermes_cli.local_runtime.hardware import probe_budget

    entry = catalog_by_id().get(body.model_id)
    variant = None
    if entry is not None:
        # Same planning budget as the catalog — the user downloads exactly
        # the build the row advertised.
        choice = select_variant(entry, probe_budget(planning=True))
        if choice is None:
            raise HTTPException(status_code=409,
                                detail=f"no variant of {entry.id} fits this machine")
        variant = choice.variant
    else:
        for candidate in CATALOG:
            for v in candidate.variants:
                if v.model_id == body.model_id:
                    entry, variant = candidate, v
                    break
            if variant:
                break
    if entry is None or variant is None:
        raise HTTPException(status_code=404, detail=f"unknown model {body.model_id}")

    from hermes_cli.local_runtime.bootstrap import assets_dir, staged_model_ids

    if variant.model_id in staged_model_ids():
        return {"job_id": None, "already_downloaded": True, "model_id": variant.model_id}

    # Everything this variant needs: split parts + mmproj/draft assets.
    plan = []  # (url, dest, sha256, bytes)
    for asset in variant.files:
        plan.append((f"https://huggingface.co/{entry.repo}/resolve/main/{asset.path}",
                     _models_dir() / asset.local_name, asset.sha256, asset.size_bytes))
    for asset in (entry.mmproj, entry.draft):
        if asset is not None:
            plan.append((f"https://huggingface.co/{entry.repo}/resolve/main/{asset.path}",
                         assets_dir() / asset.local_name, asset.sha256, asset.size_bytes))

    total = sum(p[3] for p in plan)
    job = _job("model-download", f"{entry.display_name} ({variant.quant})",
               model_id=entry.id)
    job["total_bytes"] = total

    def _run():
        try:
            job["phase"] = "downloading"
            job["detail"] = f"{entry.display_name} — {_human_gb(total)}"
            done_before = 0
            for url, dest, sha, size in plan:
                if dest.exists():
                    done_before += size
                    job["done_bytes"] = done_before
                    continue
                download_file(url, dest, job, expected_sha256=sha,
                              base_done=done_before, keep_totals=True)
                job["phase"] = "downloading"
                done_before += size
                job["done_bytes"] = done_before
            job["phase"] = "done"
            job["status"] = "done"
            job["detail"] = f"{entry.display_name} ready"
            # A running router only scans models at spawn —
            # bounce it so the new model is servable
            # immediately instead of 400ing until the next app restart.
            try:
                from hermes_cli.local_runtime.bootstrap import refresh_local_runtime

                refresh_local_runtime()
            except Exception:  # noqa: BLE001
                logger.debug("post-download runtime refresh skipped", exc_info=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("model download failed: %s", exc)
            job["status"] = "error"
            job["error"] = str(exc)

    threading.Thread(target=_run, daemon=True, name="lr-model-download").start()
    return {"job_id": job["job_id"], "model_id": variant.model_id}


@router.delete("/api/local-models/models/{model_id}")
async def local_models_delete(model_id: str):
    """Remove a staged model: every split part plus its private assets.
    A running router keeps serving from its spawn-time scan, so bounce it
    off the request thread — deleting the active file mid-serve is the
    kind of stale state the refresh exists for."""
    files = _variant_files_on_disk(model_id)
    if not files:
        raise HTTPException(status_code=404, detail="model not found")
    for path in files:
        path.unlink(missing_ok=True)
    # Growth state dies with the model: a re-download starts back at its
    # zero-spill window instead of inheriting a stale grown one.
    try:
        from hermes_cli.local_runtime.growth import clear_window_override

        clear_window_override(model_id)
    except Exception:  # noqa: BLE001
        logger.debug("window-override clear skipped", exc_info=True)

    def _refresh():
        try:
            from hermes_cli.local_runtime.bootstrap import refresh_local_runtime

            refresh_local_runtime()
        except Exception:  # noqa: BLE001
            logger.debug("post-delete runtime refresh skipped", exc_info=True)

    threading.Thread(target=_refresh, daemon=True, name="lr-post-delete").start()
    return {"ok": True}


# ── server lifecycle: turn the engine on/off ─────────────────


class ServerActionBody(BaseModel):
    action: str                 # "stop" | "start"


@router.post("/api/local-models/server")
async def local_models_server(body: ServerActionBody):
    """Turn the local engine off (stop the server, free ALL GPU memory,
    and disable auto-start) or back on. The off switch is the whole-engine
    counterpart of per-model eject — and unlike eject it IS durable: the
    user said off, so boots stay off until they say on."""
    import asyncio

    from hermes_cli.config import load_config, save_config

    action = (body.action or "").strip().lower()
    if action not in ("stop", "start"):
        raise HTTPException(status_code=400, detail="action must be 'stop' or 'start'")

    def _stop():
        from hermes_cli.local_runtime.bootstrap import (
            get_supervisor,
            shutdown_local_runtime,
        )

        sup = get_supervisor()
        if sup is not None:
            shutdown_local_runtime()
        else:
            # Server owned by another process (or an orphan): best-effort
            # terminate via the state file's pid, then clear the state.
            endpoint = _state_endpoint()
            if endpoint is not None:
                try:
                    import psutil  # type: ignore

                    from hermes_cli.local_runtime.supervisor import state_path

                    state = json.loads(state_path().read_text(encoding="utf-8"))
                    pid = int(state.get("pid") or 0)
                    if pid > 0 and psutil.pid_exists(pid):
                        psutil.Process(pid).terminate()
                    state_path().unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        config = load_config()
        config.setdefault("local_runtime", {})["enabled"] = False
        save_config(config)

    def _start():
        from hermes_cli.local_runtime.bootstrap import ensure_local_runtime

        config = load_config()
        config.setdefault("local_runtime", {})["enabled"] = True
        save_config(config)
        sup = ensure_local_runtime(config, force=True)
        if sup is None and _state_endpoint() is None:
            raise RuntimeError("The local server could not start — check the "
                               "runtime is installed")

    try:
        await asyncio.to_thread(_stop if action == "stop" else _start)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "action": action}


# ── activate: make a downloaded model THE model ──────────────


class ModelEjectBody(BaseModel):
    model_id: str


@router.post("/api/local-models/eject")
async def local_models_eject(body: ModelEjectBody):
    """Free a loaded model's GPU memory now. Nothing reloads it except
    demand — the next message to it (residency v2: no automatic loading
    exists anywhere)."""
    from hermes_cli.local_runtime.bootstrap import get_supervisor

    sup = get_supervisor()
    if sup is not None:
        try:
            sup.unload_model(body.model_id)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Server owned by another process (or state-file only): drive the
    # router directly with the persisted endpoint.
    endpoint = _state_endpoint()
    if endpoint is None:
        raise HTTPException(status_code=409, detail="local server is not running")
    try:
        import urllib.request as _url

        req = _url.Request(
            endpoint["base_url"].rsplit("/v1", 1)[0] + "/models/unload",
            data=json.dumps({"model": body.model_id}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {endpoint.get('api_key', '')}"},
            method="POST")
        with _url.urlopen(req, timeout=120):
            pass
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class ModelActivateBody(BaseModel):
    model_id: str               # exact variant id (a staged .gguf stem)


@router.post("/api/local-models/activate")
async def local_models_activate(body: ModelActivateBody):
    """Make a downloaded model the default for new chats. Pure selection
    (residency v2): a config write through the same machinery as
    /api/model/set, plus making sure the server is up. NO model loading —
    models load on first inference, always; an empty router costs nothing.
    Fast enough to be synchronous-feeling, but kept as a job for UI
    continuity."""
    # Split variants stage under their first part — resolve like the rest
    # of the routes instead of assuming a single flat file.
    from hermes_cli.local_runtime.bootstrap import staged_model_ids

    if body.model_id not in staged_model_ids():
        raise HTTPException(status_code=404, detail=f"{body.model_id} is not downloaded")

    job = _job("model-activate", body.model_id, model_id=body.model_id)

    def _run():
        try:
            from hermes_cli.config import load_config, save_config
            from hermes_cli.local_runtime.bootstrap import (
                ensure_local_runtime,
                refresh_local_runtime,
            )

            job["phase"] = "starting-server"
            job["detail"] = "Starting the local server"
            config = load_config()
            sup = ensure_local_runtime(config, force=True)
            if sup is None:
                from hermes_cli.local_runtime.endpoint import _state_endpoint as _se

                if _se() is None:
                    raise RuntimeError(
                        "The local server could not start — check the runtime is installed")

            # Self-heal a stale router: the model list is spawn-only, so a
            # server started before this model finished downloading can't
            # serve it. If the router doesn't know the model, bounce it.
            if sup is not None:
                try:
                    if body.model_id not in sup.models():
                        job["detail"] = "Refreshing the local server"
                        refresh_local_runtime()
                except Exception:  # noqa: BLE001
                    logger.debug("activate rescan check skipped", exc_info=True)

            job["phase"] = "setting-default"
            job["detail"] = "Making it your default"
            config = load_config()
            config.setdefault("local_runtime", {})["enabled"] = True
            save_config(config)
            from hermes_cli.web_deps import late

            late("_apply_model_assignment_sync")(
                "main", "llamacpp", body.model_id, "", "", "")

            job["phase"] = "done"
            job["status"] = "done"
            job["detail"] = f"{body.model_id} is the default for new chats"
        except Exception as exc:  # noqa: BLE001
            logger.warning("model activate failed: %s", exc)
            job["status"] = "error"
            job["error"] = str(exc)

    threading.Thread(target=_run, daemon=True, name="lr-model-activate").start()
    return {"job_id": job["job_id"]}


# ── job polling ──────────────────────────────────────────────


@router.get("/api/local-models/jobs")
async def local_models_jobs():
    """All recent jobs, running first — the pane and the app-level poller
    rediscover in-flight work here after a remount or app restart."""
    with _JOBS_LOCK:
        jobs = sorted(_JOBS.values(),
                      key=lambda j: (j["status"] != "running", -j["started_at"]))
    out = []
    for job in jobs[:20]:
        entry = dict(job)
        if entry["total_bytes"]:
            entry["percent"] = min(100, round(entry["done_bytes"] / entry["total_bytes"] * 100))
        out.append(entry)
    return {"jobs": out}


@router.get("/api/local-models/jobs/{job_id}")
async def local_models_job(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    out = dict(job)
    if out["total_bytes"]:
        out["percent"] = min(100, round(out["done_bytes"] / out["total_bytes"] * 100))
    return out
