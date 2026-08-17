"""Replay-economy bench: wire-vs-raw token score; --against <ref> for CI diff.

Deterministic (pure functions, fixed session). CI compares PR tip vs its
merge-base so future PRs nudging thresholds/gates below the absolute-floor
canary still fail. Feature-absent bases skip (unit canary governs).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RATIO_TOL = 1.05  # allow 5% ratio degradation
_SAVED_TOL = 0.95  # allow 5% saved-token loss


def _note(kind: str, msg: str) -> None:
    """Print a GitHub Actions annotation in CI, a plain line locally."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{kind}::{msg}")
    else:
        print(msg)


def _deepseek_session() -> list:
    """8 oversized tool results (~5K tok) + 6 plain turns w/ long reasoning."""
    msgs = []
    for i in range(8):
        msgs.append({"role": "user", "content": f"inspect module {i}"})
        msgs.append({"role": "assistant", "content": "", "reasoning_content": "r" * 2000,
                     "tool_calls": [{"id": f"c{i}", "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "T" * 20000})
    for i in range(6):
        msgs.append({"role": "user", "content": f"question {i}"})
        msgs.append({"role": "assistant", "content": "answer", "reasoning_content": "R" * 8000})
    return msgs


_WIRES = {
    "deepseek": {"provider": "deepseek", "model": "deepseek-v4-pro", "base_url": "https://api.deepseek.com/v1"},
    "mimo": {"provider": "xiaomi", "model": "MiMo-V2.5-Pro", "base_url": "https://api.xiaomimimo.com/v1"},
    "kimi": {"provider": "kimi-coding", "model": "kimi-k3", "base_url": "https://api.moonshot.ai/v1"},
    "zai": {"provider": "zai-org", "model": "glm-5.2", "base_url": ""},
    "openai": {"provider": "openai", "model": "gpt-4o", "base_url": ""},
    "anthropic": {"provider": "anthropic", "model": "claude-sonnet-4.7", "base_url": ""},
}


def score(wire: str = "deepseek") -> dict:
    from agent.deepseek_replay import apply_deepseek_replay_compaction
    from agent.model_metadata import estimate_messages_tokens_rough

    msgs = _deepseek_session()
    raw = estimate_messages_tokens_rough(msgs)
    cfg = _WIRES[wire]
    out, diag = apply_deepseek_replay_compaction(
        msgs,
        provider=cfg.get("provider"),
        model=cfg.get("model"),
        base_url=cfg.get("base_url"),
    )
    wire_tokens = estimate_messages_tokens_rough(out)
    return {
        "raw_tokens": raw,
        "wire_tokens": wire_tokens,
        "ratio": round(wire_tokens / raw, 4),
        "saved_tokens": max(0, raw - wire_tokens),
        "compacted": diag.compacted,
        "stripped_reasoning": diag.stripped_reasoning,
    }


def score_all() -> dict:
    """Score every guarded wire (deepseek + anthropic) for the differential."""
    return {name: score(name) for name in _WIRES}


def _run_in_worktree(ref: str):
    """Score the bench against ``ref`` in a throwaway worktree (venv shared).

    Returns the score dict, or "import-missing" when the base's modules need
    deps this PR's venv lacks (long-lived PR divergence), else None.
    """
    tmp = Path(tempfile.mkdtemp(prefix="replay-bench-"))
    added = False
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(tmp), ref],
                       cwd=_REPO_ROOT, check=True, capture_output=True)
        added = True
        proc = subprocess.run([sys.executable, "-m", "scripts.bench_replay_economy", "--score-only"],
                              cwd=tmp, capture_output=True, text=True)
        if proc.returncode != 0:
            if "ModuleNotFoundError" in proc.stderr or "ImportError" in proc.stderr:
                return "import-missing"
            return None
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError):
        return None
    finally:
        if added:
            subprocess.run(["git", "worktree", "remove", "--force", str(tmp)],
                           cwd=_REPO_ROOT, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _compare(pr: dict, base: dict) -> bool:
    ok = True
    for wire in _WIRES:
        p, b = pr[wire], base[wire]
        if b.get("ratio") and p.get("ratio") and p["ratio"] > b["ratio"] * _RATIO_TOL:
            _note("error", f"replay economy REGRESSION ({wire}): ratio {p['ratio']} > base {b['ratio']} * {_RATIO_TOL}")
            ok = False
        if b.get("saved_tokens") and p.get("saved_tokens") is not None \
                and p["saved_tokens"] < b["saved_tokens"] * _SAVED_TOL:
            _note("error", f"replay economy REGRESSION ({wire}): saved {p['saved_tokens']} < base {b['saved_tokens']} * {_SAVED_TOL}")
            ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--against", help="ref to compare the current tree against")
    ap.add_argument("--score-only", action="store_true")
    args = ap.parse_args()

    pr = score_all()
    print(json.dumps(pr))
    if args.score_only or not args.against:
        return 0

    def _exists(ref_path: str) -> bool:
        return subprocess.run(["git", "cat-file", "-e", f"{args.against}:{ref_path}"],
                              cwd=_REPO_ROOT, capture_output=True).returncode == 0

    if not _exists("agent/deepseek_replay.py") or not _exists("scripts/bench_replay_economy.py"):
        _note("warning", "replay economy: base lacks the feature/bench; differential skipped (unit canary governs)")
        return 0
    # A bench-script change makes PR-vs-base apples-to-oranges; skip instead
    # of false-flagging (review the bench diff).
    bench_changed = subprocess.run(
        ["git", "diff", "--quiet", f"{args.against}:scripts/bench_replay_economy.py",
         "--", "scripts/bench_replay_economy.py"],
        cwd=_REPO_ROOT, capture_output=True,
    ).returncode != 0
    if bench_changed:
        _note("warning", "replay economy: bench script differs from base; differential skipped (review the bench diff)")
        return 0
    base = _run_in_worktree(args.against)
    if base == "import-missing":
        _note("warning", "replay economy: base imports unavailable in this env; differential skipped")
        return 0
    if base is None:
        _note("error", "replay economy: bench failed to run on the base ref")
        return 1
    print(f"base: {json.dumps(base)}")
    return 0 if _compare(pr, base) else 1


if __name__ == "__main__":
    sys.exit(main())
