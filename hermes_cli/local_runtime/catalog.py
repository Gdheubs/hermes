"""Curated starter catalog for the managed local runtime.

Small and honest: every entry carries the estimator inputs (measured on
real GGUFs) so the picker can price a model BEFORE the user downloads
gigabytes. Once a file is on disk, profile_from_gguf() is the authority
and the catalog numbers are only used for the download decision. Entries
whose base config is gated upstream carry a same-family conservative
prior (commented) — the GGUF header corrects it at load time.

Each model ships a QUANT LADDER (variants, best quality first). The ladder
floors at UD-Q4_K_XL — below Q4 the quality loss is too severe to ship as
someone's first local-AI experience. Selection is hardware-aware: pick the
highest-quality variant that zero-spills at the 64K floor on this machine;
else Q4 spilled (priced honestly by the fit policy); refuse only when even
Q4 fails the physics check.

Validation lifecycle: rungs proven end-to-end on real hardware are marked
validated. Day-0 entries ship before that proof under the "day-0" tag —
ensure_model_ready's touch generation still gates every first load at
runtime. The contract test requires every ladder floor to be Q4 AND
(validated OR day-0).

Multi-file models: variants may carry split-GGUF parts (llama-server loads
from the first part; all parts download together, each sha-verified).
Entries may carry an mmproj (vision projector) and a speculative-decode
draft model — both download alongside the weights. Spec decode is enabled
only when the launch decision spills, where its speedup is largest.

sha256s are pinned from HF LFS metadata (the lfs oid IS the file sha256),
reviewed like a version bump — parsed data, never executed commands.

This is deliberately not a live registry feed: entries are reviewed like a
version bump (the same policy governs vendor recipe ingestion — parsed
data, never executed commands).

Vendor recipes overlay: a per-SKU recipes repo may SUPPLEMENT these
entries where applicable — vendor SKUs only, never the base layer for
other platforms. A recipe may enrich identity (GGUF/quant/sha), perf
hints (-b/-ub, spec-decode), and sampling defaults; it never carries
context/slots/placement/serving flags (the fit policy owns those).
Resolution: exact SKU -> GPU-class bucket -> fit-only. Snapshot-synced,
reviewed like a tag bump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from hermes_cli.local_runtime.context_policy import (
    FLOOR,
    RUNTIME_OVERHEAD_BYTES,
    TARGET_WINDOW,
)
from hermes_cli.local_runtime.estimator import (
    HardwareBudget,
    LayerKind,
    ModelProfile,
    ctx_bytes,
)

_GIB = 1 << 30
_PART_SUFFIX = re.compile(r"-\d{5}-of-\d{5}$")


@dataclass(frozen=True)
class AssetFile:
    """One downloadable file: repo-relative path, exact bytes, sha256.
    ``local`` overrides the on-disk name (repos reuse generic names like
    mmproj-BF16.gguf across models). Non-model extras live under the
    models dir's assets/ subdirectory so the router never lists them."""

    path: str                   # repo-relative (may include a subdir)
    size_bytes: int
    sha256: str
    local: str | None = None

    @property
    def local_name(self) -> str:
        return self.local or PurePosixPath(self.path).name


@dataclass(frozen=True)
class QuantVariant:
    """One downloadable build of a model. Ordered best-quality-first in
    CatalogEntry.variants. Split GGUFs list every part in files; the model
    loads from the first part."""

    quant: str                  # e.g. "UD-Q8_K_XL"
    files: tuple                # AssetFile, first = the load target
    validated: bool = False     # proven end-to-end on real hardware

    @property
    def model_id(self) -> str:
        stem = PurePosixPath(self.files[0].path).name.removesuffix(".gguf")
        return _PART_SUFFIX.sub("", stem)

    @property
    def size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def weights_bytes(self) -> int:
        """Pre-download weights estimate: GGUF bytes ≈ tensor bytes + a
        small header (<2%) — a safe, slightly conservative stand-in until
        profile_from_gguf reads the real table."""
        return self.size_bytes


@dataclass(frozen=True)
class CatalogEntry:
    id: str                     # stable family id (variant-independent)
    display_name: str
    description: str            # one line, plain language
    repo: str                   # HF repo
    variants: tuple             # QuantVariant, best first
    # Estimator inputs (measured or config-derived; quant changes weights,
    # never KV). Entries with gated upstream configs carry a conservative
    # same-family prior — the GGUF header is the authority after download.
    n_ctx_train: int
    full_layers: int
    recurrent_layers: int
    per_layer_f16: int          # KV bytes/token per full-attention layer
    swa_layers: int = 0
    swa_window: int = 0
    moe: bool = False
    mtp: bool = False           # ships MTP heads (spec decode iff spilled)
    mmproj: "AssetFile | None" = None    # vision projector, downloads with model
    draft: "AssetFile | None" = None     # spec-decode draft model (e.g. DSpark)
    sampling: dict = field(default_factory=dict)  # INI long-form launch defaults
    tags: tuple = field(default_factory=tuple)

    def profile(self, variant: QuantVariant) -> ModelProfile:
        layers = ([(LayerKind.FULL, self.per_layer_f16)] * self.full_layers
                  + [(LayerKind.SWA, self.per_layer_f16)] * self.swa_layers
                  + [(LayerKind.RECURRENT, 0)] * self.recurrent_layers)
        return ModelProfile(
            name=variant.model_id, weights_bytes=variant.weights_bytes,
            embd_table_bytes=0, n_ctx_train=self.n_ctx_train,
            layers=layers, swa_window=self.swa_window, moe=self.moe)

    def download_files(self, variant: QuantVariant) -> tuple:
        """Everything a download job fetches for this variant, in order."""
        extras = tuple(a for a in (self.mmproj, self.draft) if a is not None)
        return tuple(variant.files) + extras

    def download_bytes(self, variant: QuantVariant) -> int:
        return sum(f.size_bytes for f in self.download_files(variant))


@dataclass(frozen=True)
class VariantChoice:
    """Selection result: which build this machine should download and why.
    reason_key is a UI-copy discriminator, not display text."""

    variant: QuantVariant
    zero_spill: bool
    reason_key: str  # "best-large-window" | "best-fits" | "smallest-fits-spilled"


def select_variant(entry: CatalogEntry, budget: HardwareBudget) -> VariantChoice | None:
    """Best build for this hardware, per the ladder policy.

    Ranked rules (each a guarantee the ones below may not break):
    1. Never below Q4 — the ladder's quality floor.
    2. Never below a 64K window — every session must be viable.
    3. Prefer reaching TARGET_WINDOW — compression should be the
       exception, not the routine.
    4. Then maximize quality — free once the above hold.

    Concretely: highest-quality variant that zero-spills at the target
    window; else highest-quality at the 64K floor (small cards keep the
    old behavior); else the smallest build spilled; else refusal. One
    quality step is worth multiple context rungs (dynamic-quant deltas
    between adjacent rungs are small; 64K vs 216K changes what a session
    can do), but nothing outranks full residency.
    """
    overhead = RUNTIME_OVERHEAD_BYTES + (entry.mmproj.size_bytes if entry.mmproj else 0)
    native = entry.n_ctx_train or FLOOR
    floor_kv = None
    target_kv = None
    floor_pick: QuantVariant | None = None
    for variant in entry.variants:
        profile = entry.profile(variant)
        if floor_kv is None:
            floor_kv = ctx_bytes(profile, min(FLOOR, native))
            target_kv = ctx_bytes(profile, min(TARGET_WINDOW, native))
        if (variant.weights_bytes + target_kv + overhead
                <= budget.usable_vram_bytes):
            return VariantChoice(variant=variant, zero_spill=True,
                                 reason_key="best-large-window")
        if floor_pick is None and (variant.weights_bytes + floor_kv + overhead
                                   <= budget.usable_vram_bytes):
            floor_pick = variant

    if floor_pick is not None:
        return VariantChoice(variant=floor_pick, zero_spill=True,
                             reason_key="best-fits")

    smallest = min(entry.variants, key=lambda v: v.size_bytes)
    needed = smallest.weights_bytes + (floor_kv or 0) + overhead
    if needed <= budget.usable_vram_bytes + budget.ram_available_bytes:
        return VariantChoice(variant=smallest, zero_spill=False,
                             reason_key="smallest-fits-spilled")
    return None


def _v(quant: str, *files, validated: bool = False) -> QuantVariant:
    return QuantVariant(quant=quant,
                        files=tuple(AssetFile(*f) for f in files),
                        validated=validated)


# Ordered: recommended first. File bytes + sha256s pinned from HF LFS
# metadata.
CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        id="qwen3.8-27b",
        display_name="Qwen3.8 27B",
        description="Best all-round agent model; sees images; long context stays fast",
        repo="unsloth/Qwen3.8-27B-GGUF",
        variants=(
            _v("UD-Q8_K_XL",
               ("Qwen3.8-27B-UD-Q8_K_XL.gguf", 31457991680,
                "af36ecb6b5db1407953345b746c14ac93f0657dda413910b4348683a2d990377")),
            _v("UD-Q6_K_XL",
               ("Qwen3.8-27B-UD-Q6_K_XL.gguf", 25924152384,
                "739202186fd9389bb58497c58b56c8a0d4253d99d20131e6a0427e363e678fc8")),
            _v("UD-Q5_K_XL",
               ("Qwen3.8-27B-UD-Q5_K_XL.gguf", 20218178624,
                "176a6a3f034e9cdc447c10cd00329fc9b31002e6589b9295f2ad4f1eefe0f6ab")),
            _v("UD-Q4_K_XL",
               ("Qwen3.8-27B-UD-Q4_K_XL.gguf", 17923394624,
                "bee238bbeb3dc0a34bde4d0dedbaee1f98c009e8bb4226f03070054c12fb1372")),
        ),
        n_ctx_train=262144,
        # Same hybrid family as Qwen3.6-27B (config-verified: 64 layers,
        # 16 full-attention via full_attention_interval=4, 4 KV heads x
        # 256 head_dim = 4 KiB/token per full layer). The GGUF header is
        # the authority after download.
        full_layers=16, recurrent_layers=48, per_layer_f16=4096,
        mmproj=AssetFile("mmproj-BF16.gguf", 931146432,
                         "83ee4f4f205fa514161778c41df1ea14144faa0f713510893b63c2395f5c2d53",
                         local="mmproj-Qwen3.8-27B-BF16.gguf"),
        sampling={"temp": "1.0", "top-p": "0.95", "top-k": "20", "min-p": "0.0"},
        tags=("recommended", "hybrid", "reasoning", "vision", "day-0"),
    ),
    CatalogEntry(
        id="qwen3.6-35b-a3b",
        display_name="Qwen3.6 35B-A3B",
        description="Bigger mixture-of-experts with multi-token prediction; sees images",
        repo="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
        variants=(
            _v("UD-Q8_K_XL",
               ("Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf", 39099447584,
                "6c6b816537abad90b250a0972b345466028d861ddfe316d5f0de31ca6440f781")),
            _v("UD-Q6_K_XL",
               ("Qwen3.6-35B-A3B-UD-Q6_K_XL.gguf", 32611711264,
                "35fce994cd36104a7dc1bd8a4bdf13778145664c00fdef6773aebc9246e5019c")),
            _v("UD-Q5_K_XL",
               ("Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf", 27159116064,
                "9de9a9420f61a0bb59bb2ca1ea170a6a57f6821fa1deec915bcaef523730a919")),
            # Validated on this repo's prior upload; upstream has since
            # re-uploaded. Same model id + pipeline — re-verify at the
            # next validation pass.
            _v("UD-Q4_K_XL",
               ("Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf", 22853663008,
                "55983c5a75a1ab969824077b3bb3de4146e82a9234072b48ad4e8f92ad3fe9f1"),
               validated=True),
        ),
        n_ctx_train=262144,
        # Config-derived (Qwen/Qwen3.6-35B-A3B): 40 layers, 10 full-attn
        # (interval 4) + 30 linear; KV heads 2 x head_dim 256.
        full_layers=10, recurrent_layers=30, per_layer_f16=2048,
        moe=True, mtp=True,
        mmproj=AssetFile("mmproj-BF16.gguf", 902822528,
                         "da63cb47a76763c712393f8a017070188a304fa39f8aeea6edc629ed7b975cfa",
                         local="mmproj-Qwen3.6-35B-A3B-BF16.gguf"),
        sampling={"temp": "1.0", "top-p": "0.95", "top-k": "20", "min-p": "0.0"},
        tags=("hybrid", "moe", "mtp", "vision"),
    ),
    CatalogEntry(
        id="nemotron-3.5-lightning-30b",
        display_name="Nemotron 3.5 Lightning 30B",
        description="NVIDIA's fast tool-calling model; 1M-token context",
        repo="unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF",
        variants=(
            _v("UD-Q8_K_XL",
               ("NVIDIA-Nemotron-3.5-Lightning-30B-A3B-UD-Q8_K_XL.gguf", 38615380032,
                "48140cfb8bb6a38553275d334345f2638c119d979ab8b6d4afdc4b62d26d8219")),
            _v("UD-Q6_K_XL",
               ("NVIDIA-Nemotron-3.5-Lightning-30B-A3B-UD-Q6_K_XL.gguf", 35004643392,
                "36b8b0f882ec739f895fe56ab6b9b892e702ba33b7b368614ca4078331ddfc29")),
            _v("UD-Q5_K_XL",
               ("NVIDIA-Nemotron-3.5-Lightning-30B-A3B-UD-Q5_K_XL.gguf", 30414829632,
                "92dcaf682faf39fef906db7bad4000782001d089f28c4d6aecec6d6f1b4697ca")),
            _v("UD-Q4_K_XL",
               ("NVIDIA-Nemotron-3.5-Lightning-30B-A3B-UD-Q4_K_XL.gguf", 25505724480,
                "112bf957489a497c18f60bf8bd44ee1dfa05e87368b8ed7f68998a4e38f275c9")),
        ),
        n_ctx_train=1048576,
        # Prior from Nemotron-3-Nano (same hybrid family; base config gated
        # upstream): 6 full-attn + 46 recurrent, ~1 KiB/tok/full-layer.
        # Prior from the same hybrid family (measured: 1M ctx in ~3.2 GiB
        # KV on the predecessor). GGUF header is
        # the authority after download.
        full_layers=6, recurrent_layers=46, per_layer_f16=1024,
        moe=True, mtp=True,
        sampling={"temp": "0.6", "top-p": "0.95", "min-p": "0.01"},
        tags=("day-0", "long-context", "hybrid", "moe", "mtp"),
    ),
    CatalogEntry(
        id="muse-glimmer-30b",
        display_name="Muse Glimmer 30B",
        description="Meta's open vision model for coding and agent work",
        repo="unsloth/Muse-Glimmer-30B-GGUF",
        variants=(
            _v("UD-Q8_K_XL",
               ("Muse-Glimmer-30B-UD-Q8_K_XL.gguf", 32300651040,
                "e63bf23b7710ecdea2579e4b1de58980c4a2b446e8ecf48b782cfcefd2e31770")),
            _v("UD-Q6_K_XL",
               ("Muse-Glimmer-30B-UD-Q6_K_XL.gguf", 26265362976,
                "fb5f80d110c4fa932cc652e70873c0bd12c0954009038aa675e65086104c2739")),
            _v("UD-Q5_K_XL",
               ("Muse-Glimmer-30B-UD-Q5_K_XL.gguf", 21789618976,
                "97a66c4b41d9e778af7cdfa43508e08dbf765fb5049b740c69ad815e5191c637")),
            _v("UD-Q4_K_XL",
               ("Muse-Glimmer-30B-UD-Q4_K_XL.gguf", 15878222368,
                "82bece304887a313ece08400bc030f6066c7bff5b906b0cd40308ec8a409fd38")),
        ),
        n_ctx_train=262144,
        # Conservative dense prior (base config gated upstream): 30B-class
        # dense, ~60 layers x 4 KiB/tok. Dense KV is the expensive shape —
        # overestimating here keeps the zero-spill promise safe until the
        # GGUF header corrects it.
        full_layers=60, recurrent_layers=0, per_layer_f16=4096,
        mmproj=AssetFile("mmproj-Muse-Glimmer-30B-BF16.gguf", 3849173728,
                         "d08cdcfa0b41d8e20554b52df404ba4f7b440d0bc502a90038508b6407df8ee1"),
        sampling={"temp": "1.0", "top-p": "0.95", "top-k": "64"},
        tags=("day-0", "vision", "dense"),
    ),
    CatalogEntry(
        id="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        description="Frontier-class model for machines with 128GB+ memory",
        repo="unsloth/DeepSeek-V4-Flash-0731-GGUF",
        variants=(
            # Q8 is bit-lossless vs the official QAT checkpoint; Q4 keeps
            # the MXFP4 experts bit-exact and only requants the other 4%.
            _v("UD-Q8_K_XL",
               ("UD-Q8_K_XL/DeepSeek-V4-Flash-0731-UD-Q8_K_XL-00001-of-00005.gguf", 5257408,
                "d13ce8f90855547bdaebe7312f531a1f2c4f822178d3103951f27fe884395cfa"),
               ("UD-Q8_K_XL/DeepSeek-V4-Flash-0731-UD-Q8_K_XL-00002-of-00005.gguf", 49215492960,
                "3da2f2443063f83635986f9b67fa7e8e3d03c53b81a9a08d2007936612423610"),
               ("UD-Q8_K_XL/DeepSeek-V4-Flash-0731-UD-Q8_K_XL-00003-of-00005.gguf", 49700372160,
                "7d622a7760d359ec9257b3493ad531e3bf0bfbe6f6533267e16e6dde8153ddce"),
               ("UD-Q8_K_XL/DeepSeek-V4-Flash-0731-UD-Q8_K_XL-00004-of-00005.gguf", 49466495968,
                "6ed2bce452214f156b85e7c5f7d4fc242a3052f409d1b90a61422f60669c2de3"),
               ("UD-Q8_K_XL/DeepSeek-V4-Flash-0731-UD-Q8_K_XL-00005-of-00005.gguf", 13481997024,
                "ea4727af4888fdca0fff796ec81ac2f3ebb43c310b2feb4798f41d82744b42ea")),
            _v("UD-Q4_K_XL",
               ("UD-Q4_K_XL/DeepSeek-V4-Flash-0731-UD-Q4_K_XL-00001-of-00005.gguf", 5257408,
                "d13ce8f90855547bdaebe7312f531a1f2c4f822178d3103951f27fe884395cfa"),
               ("UD-Q4_K_XL/DeepSeek-V4-Flash-0731-UD-Q4_K_XL-00002-of-00005.gguf", 48935523072,
                "d5b61668950f4743aacd677675d7fcf7507dbe1db6d304e8ff97ed1f00827bee"),
               ("UD-Q4_K_XL/DeepSeek-V4-Flash-0731-UD-Q4_K_XL-00003-of-00005.gguf", 48980787136,
                "9705db7e589f360685ca7bd48100b270d78d228d4f5aa980508f3b2778af5494"),
               ("UD-Q4_K_XL/DeepSeek-V4-Flash-0731-UD-Q4_K_XL-00004-of-00005.gguf", 49999168416,
                "7f13a68e3ca64208454c4ba32cc2757c0cbe78e3e5576c3142bf7007ca97da42"),
               ("UD-Q4_K_XL/DeepSeek-V4-Flash-0731-UD-Q4_K_XL-00005-of-00005.gguf", 7174505088,
                "ed0d93164d3784968d6ce40d6d201ba98337f16e7db1b31fe495b2b0f334cc09")),
        ),
        n_ctx_train=1048576,
        # Config-derived (deepseek-ai/DeepSeek-V4-Flash-0731): 43 layers,
        # MLA compressed KV (rank 512 + 64 rope) ~1.15 KiB/tok/layer f16.
        # Config shows sliding_window=128 with no per-layer map — priced
        # all-full (conservative); GGUF header decides after download.
        full_layers=43, recurrent_layers=0, per_layer_f16=1152,
        moe=True,
        draft=AssetFile("dspark-DeepSeek-V4-Flash-0731-Q8_0.gguf", 10896057440,
                        "2c7ac54b0b64a99df1f139a9f1371a00198265e1d6a614b77597d20a655a4249"),
        sampling={"temp": "1.0", "top-p": "0.95", "min-p": "0.01"},
        tags=("day-0", "long-context", "moe", "frontier"),
    ),
)


def catalog_by_id() -> dict[str, CatalogEntry]:
    return {entry.id: entry for entry in CATALOG}


def find_variant(entry_id: str, model_id: str) -> QuantVariant | None:
    entry = catalog_by_id().get(entry_id)
    if entry is None:
        return None
    return next((v for v in entry.variants if v.model_id == model_id), None)


def find_entry_for_model(model_id: str) -> "tuple[CatalogEntry, QuantVariant] | None":
    """Locate the entry + variant that owns a staged model id."""
    for entry in CATALOG:
        for variant in entry.variants:
            if variant.model_id == model_id:
                return entry, variant
    return None
