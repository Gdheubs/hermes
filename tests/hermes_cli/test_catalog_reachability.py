"""Catalog integrity: every entry's files must exist on the host with
matching sha256s (HF LFS oids ARE the file hashes).

Network-marked (skipped in hermetic CI unless explicitly enabled) — this is
the test that catches wrong repo names (the Nemotron 401), moved files, and
upstream re-uploads before a user's download does. Run before any catalog
commit:

    HERMES_TEST_NETWORK=1 scripts/run_tests.sh tests/hermes_cli/test_catalog_reachability.py
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HERMES_TEST_NETWORK"),
    reason="network test; set HERMES_TEST_NETWORK=1 to run",
)


def test_every_catalog_file_resolves():
    from hermes_cli.local_runtime.catalog import CATALOG

    problems = []
    for entry in CATALOG:
        url = f"https://huggingface.co/api/models/{entry.repo}/tree/main?recursive=true"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                files = {f["path"]: f.get("lfs") or {} for f in json.load(r)}
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{entry.id}: repo {entry.repo} unreachable ({exc})")
            continue
        for variant in entry.variants:
            for asset in entry.download_files(variant):
                if asset.path not in files:
                    problems.append(
                        f"{entry.id}/{variant.quant}: {asset.path} not in {entry.repo}")
                    continue
                live_sha = files[asset.path].get("oid", "")
                if live_sha and live_sha != asset.sha256:
                    problems.append(
                        f"{entry.id}/{variant.quant}: sha drift on {asset.path} — "
                        f"catalog {asset.sha256[:12]} vs live {live_sha[:12]} "
                        f"(upstream re-uploaded; re-pin deliberately)")
    assert not problems, "\n".join(problems)
