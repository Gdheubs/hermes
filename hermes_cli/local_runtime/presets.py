"""Per-model preset generation (--models-preset INI) — the router-side
carrier for context-policy launch decisions.

The INI shape is what the router itself generates per child: a
[model-id] section whose keys are long-form
llama-server flag names without the leading dashes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.local_runtime.context_policy import (
    RUNTIME_OVERHEAD_BYTES,
    WindowDecision,
    initial_window,
    launch_args,
)
from hermes_cli.local_runtime.estimator import (
    HardwareBudget,
    PhysicsRefusal,
    profile_from_gguf,
)
from hermes_cli.local_runtime.gguf import read_gguf_header

logger = logging.getLogger(__name__)

# args list -> INI keys. Flags the policy owns; everything else stays out
# of the preset (recipe sampling defaults merge in a later pass).
_FLAG_TO_KEY = {
    "-c": "ctx-size",
    "-ctk": "cache-type-k",
    "-ctv": "cache-type-v",
    "-fa": "flash-attn",
    "-ot": "override-tensor",
    "--spec-type": "spec-type",
}


@dataclass
class PresetEntry:
    model_id: str
    window: int
    spilled: bool
    refusal: str | None = None
    keys: dict[str, str] | None = None


def _args_to_keys(args: list[str]) -> dict[str, str]:
    keys: dict[str, str] = {}
    i = 0
    while i < len(args):
        flag = args[i]
        key = _FLAG_TO_KEY.get(flag)
        if key is None:
            i += 1
            continue
        keys[key] = args[i + 1]
        i += 2
    return keys


def generate_presets(models_dir: Path, budget: HardwareBudget,
                     preset_path: Path,
                     mtp_capable: set[str] | None = None) -> list[PresetEntry]:
    """Walk the staged models, run the launch decision per model, and
    write one INI. Refused models get no section (the router simply won't
    have policy for them; the picker surfaces the refusal + smaller-quant
    suggestion from the returned entries).

    Catalog-declared companions merge in here: sampling defaults (policy
    keys always win), the vision projector when present, and a spec-decode
    draft model iff the decision spilled — the rule: speculative
    decode is a spill amplifier, so a resident draft accelerates a spilled
    main model; a zero-spill model doesn't pay the draft's memory."""
    from hermes_cli.local_runtime.bootstrap import assets_dir
    from hermes_cli.local_runtime.catalog import find_entry_for_model

    entries: list[PresetEntry] = []
    sections: list[str] = []
    for gguf in _staged_in(models_dir):
        model_id = _strip_part(gguf.stem)
        try:
            profile = profile_from_gguf(read_gguf_header(gguf))
        except (ValueError, OSError) as exc:
            logger.warning("preset skip %s: %s", gguf.name, exc)
            continue
        # Overhead beyond weights+KV: runtime buffers, plus the vision
        # projector when this model ships one (it loads beside the weights).
        entry = find_entry_for_model(model_id)
        mmproj_bytes = 0
        if entry is not None and entry.mmproj is not None:
            mmproj_path = assets_dir() / entry.mmproj.local_name
            if mmproj_path.exists():
                mmproj_bytes = entry.mmproj.size_bytes
        decision = initial_window(
            profile, budget,
            overhead_bytes=RUNTIME_OVERHEAD_BYTES + mmproj_bytes)
        if isinstance(decision, PhysicsRefusal):
            entries.append(PresetEntry(model_id=model_id, window=0,
                                       spilled=False, refusal=decision.message))
            continue

        # Session growth (growth.py): a persisted override lifts the launch
        # window to where the ladder last grew it — capped at native, and
        # only when physics still clears the bigger window on THIS boot's
        # budget (a smaller-VRAM day re-fits honestly back down).
        try:
            from hermes_cli.local_runtime.estimator import ctx_bytes
            from hermes_cli.local_runtime.growth import load_window_overrides

            override = load_window_overrides().get(model_id)
            native = profile.n_ctx_train or decision.window
            if override and override > decision.window:
                target = min(int(override), native)
                kv = ctx_bytes(profile, target)
                need = (profile.weights_bytes + kv
                        + RUNTIME_OVERHEAD_BYTES + mmproj_bytes)
                if need <= budget.usable_vram_bytes + budget.ram_available_bytes:
                    spill = max(0, need - budget.usable_vram_bytes)
                    decision = WindowDecision(
                        window=target, spill_bytes=spill,
                        kv_on_gpu=kv <= budget.usable_vram_bytes,
                        reasons=[f"grown window restored ({target // 1024}K)"])
        except Exception as exc:  # noqa: BLE001 — overrides are advisory
            logger.debug("window override skipped for %s: %s", model_id, exc)

        args = launch_args(profile, decision,
                           mtp_capable=model_id in (mtp_capable or set()))
        keys = _args_to_keys(args)

        hit = find_entry_for_model(model_id)
        if hit is not None:
            entry, _variant = hit
            # Sampling defaults under the policy keys (policy wins on clash).
            for k, v in (entry.sampling or {}).items():
                keys.setdefault(k, v)
            if entry.mmproj is not None:
                mmproj_path = assets_dir() / entry.mmproj.local_name
                if mmproj_path.exists():
                    keys["mmproj"] = str(mmproj_path)
            if entry.draft is not None and decision.spilled:
                draft_path = assets_dir() / entry.draft.local_name
                if draft_path.exists():
                    keys["model-draft"] = str(draft_path)
                    keys["spec-type"] = "draft-dspark"
                    # Unsloth's measured cliff: acceptance 83% at 2-3
                    # drafts, collapses at 4.
                    keys["spec-draft-n-max"] = "3"

        entries.append(PresetEntry(model_id=model_id, window=decision.window,
                                   spilled=decision.spilled, keys=keys))
        body = "\n".join(f"{k} = {v}" for k, v in keys.items())
        sections.append(f"[{model_id}]\n{body}\n")

    preset_path.parent.mkdir(parents=True, exist_ok=True)
    preset_path.write_text("\n".join(sections), encoding="utf-8")
    logger.info("wrote %d preset sections to %s", len(sections), preset_path)
    return entries


def read_preset_decisions(preset_path: Path | None = None) -> dict[str, PresetEntry]:
    """The launch decisions the running server was actually given, read
    back from the preset INI (the INI is the record — it's what spawned
    the children). Missing/unparseable file returns {}."""
    import configparser

    if preset_path is None:
        from hermes_constants import get_hermes_home

        preset_path = get_hermes_home() / "runtimes" / "llamacpp" / "presets.ini"
    out: dict[str, PresetEntry] = {}
    try:
        parser = configparser.ConfigParser()
        parser.read(preset_path, encoding="utf-8")
        for section in parser.sections():
            window = parser.getint(section, "ctx-size", fallback=0)
            spilled = parser.has_option(section, "override-tensor")
            out[section] = PresetEntry(model_id=section, window=window,
                                       spilled=spilled)
    except Exception as exc:  # noqa: BLE001
        logger.debug("preset read-back failed: %s", exc)
    return out


def _strip_part(stem: str) -> str:
    import re

    return re.sub(r"-\d{5}-of-\d{5}$", "", stem)


def _staged_in(models_dir: Path) -> "list[Path]":
    """Servable models in an arbitrary directory (split first-parts only) —
    the validation harness points at non-default dirs."""
    import re

    part = re.compile(r"-(\d{5})-of-\d{5}\.gguf$")
    out = []
    for p in sorted(models_dir.glob("*.gguf")):
        m = part.search(p.name)
        if m and m.group(1) != "00001":
            continue
        out.append(p)
    return out
