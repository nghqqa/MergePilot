#!/usr/bin/env python3
"""Deterministically (re)generate identity.json (v2 canonical scheme).

Run order: update_manifest.py first, then this script (it pins the current
source-manifest.json digest; identity.json itself is NOT listed in the
manifest, so there is no cycle).

v2 identity carries:
- hash_scheme = canonical-lf-v2
- canonical digests (LF-normalized text, raw binaries) for harness/dataset/
  manifest/design/contract — these must match a fresh checkout
- run_time_identity: the digests recorded inside the 120 formal raw runs at
  execution time (pre-normalization scheme), preserved verbatim
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.preview4_refresh.canonical_hash import (  # noqa: E402
    canonical_digest, canonical_dir_digest)
from benchmark.preview4_refresh.product_evidence import contract_sha256

HARNESS_FILES = [
    "benchmark/adapters/single_agent.py",
    "benchmark/adapters/mergepilot.py",
    "benchmark/adapters/base.py",
    "benchmark/preview4_refresh/product_evidence.py",
    "benchmark/preview4_refresh/canonical_hash.py",
    "benchmark/preview4_refresh/design.json",
    "benchmark/preview4_refresh/test_offline_refresh.py",
    "benchmark/preview4_refresh/update_manifest.py",
    "benchmark/preview4_refresh/gen_identity.py",
    "benchmark/preview4_refresh/run_smoke.py",
    "benchmark/preview4_refresh/run_candidate.py",
    "benchmark/preview4_refresh/run_formal.py",
    "benchmark/preview4_refresh/coupling-audit.json",
    "benchmark/preview4_refresh/__init__.py",
    "benchmark/source-manifest.json",
]
FORMAL_RAW = REPO_ROOT / "benchmark" / "preview4-refresh-formal-20260826" / "raw-runs"


def _commit() -> str:
    for args in (["git", "rev-parse", "v0.1.0-preview.4^{}"],
                 ["git", "rev-parse", "HEAD"]):
        v = subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True,
                           text=True).stdout.strip()
        if v:
            return v
    return "unavailable"


def _head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                          capture_output=True, text=True).stdout.strip()


def main() -> int:
    h = __import__("hashlib").sha256()
    for f in HARNESS_FILES:
        h.update(f.encode("utf-8"))
        h.update(bytes.fromhex(canonical_digest(REPO_ROOT / f)))
    ds = __import__("hashlib").sha256()
    for f in ("benchmark/dataset/cases.jsonl",
              "benchmark/dataset/dataset-design.json"):
        ds.update(f.encode("utf-8"))
        ds.update(bytes.fromhex(canonical_digest(REPO_ROOT / f)))
    ds.update(b"fixtures")
    ds.update(bytes.fromhex(
        canonical_dir_digest(REPO_ROOT / "benchmark" / "dataset" / "fixtures")))

    # run-time identity preserved verbatim from the formal raw runs
    run_time = {}
    runs = sorted(FORMAL_RAW.glob("*.json"))
    if runs:
        first = json.loads(runs[0].read_text(encoding="utf-8"))
        run_time = {k: first[k] for k in
                    ("product_source_commit", "benchmark_harness_digest",
                     "design_json_sha256", "source_manifest_sha256",
                     "dataset_manifest_sha256", "untrusted_contract_sha256",
                     "schedule_sha256")}
        for p in runs:  # assert all runs agree
            r = json.loads(p.read_text(encoding="utf-8"))
            for k, v in run_time.items():
                assert r[k] == v, (p.name, k)

    ident = {
        "hash_scheme": "canonical-lf-v2",
        "product_source_commit": _commit(),
        "archive_head_at_generation": _head(),
        "benchmark_harness_digest": h.hexdigest(),
        "design_json_sha256": canonical_digest(
            REPO_ROOT / "benchmark/preview4_refresh/design.json"),
        "source_manifest_sha256": canonical_digest(
            REPO_ROOT / "benchmark/source-manifest.json"),
        "untrusted_contract_sha256": contract_sha256(),
        "dataset_manifest_sha256": ds.hexdigest(),
        "harness_files": HARNESS_FILES,
        "run_time_identity": run_time,
        "note": ("canonical digests are LF-normalized for UTF-8 text and "
                 "raw for binaries, so they reproduce on any platform's "
                 "checkout; run_time_identity preserves the digests recorded "
                 "inside the 120 formal raw runs at execution time "
                 "(pre-normalization scheme); product commit alone does NOT "
                 "represent the benchmark implementation"),
    }
    out = HERE / "identity.json"
    out.write_bytes((json.dumps(ident, ensure_ascii=False, indent=2,
                                sort_keys=True) + "\n").encode("utf-8"))
    print("identity v2 written; harness=%s dataset=%s run_time_pairs=%d"
          % (h.hexdigest()[:8], ds.hexdigest()[:8], len(run_time)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
