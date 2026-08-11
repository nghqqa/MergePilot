#!/usr/bin/env python3
"""Generate source manifest for benchmark reproducibility.

Captures HEAD + all code/dataset SHA256 + run parameters.
Must be regenerated before each formal N=10x2 run.
"""
from __future__ import annotations
import hashlib, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FILES = [
    "benchmark/adapters/base.py",
    "benchmark/adapters/single_agent.py",
    "benchmark/adapters/mergepilot.py",
    "benchmark/evaluator.py",
    "benchmark/run_benchmark.py",
    "benchmark/summarize_results.py",
    "benchmark/gen_smoke_summary.py",
    "benchmark/gen_source_manifest.py",
    "benchmark/validate_dataset.py",
    "benchmark/test_offline.py",
    "benchmark/dataset/cases.jsonl",
    "benchmark/schemas/case.schema.json",
    "benchmark/schemas/run-result.schema.json",
    "benchmark/README.md",
]

FIXTURES = sorted(f for f in os.listdir(os.path.join(HERE, "dataset", "fixtures")) if f.startswith("bm-"))


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def git_head():
    r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT)
    return r.stdout.strip()


def main():
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_head": git_head(),
        "model": "deepseek-v4-flash",
        "timeout_seconds": 120,
        "token_budget": 4096,
        "temperature": 0.1,
        "files": {f: sha256(os.path.join(ROOT, f)) for f in FILES},
        "fixtures": {f: sha256(os.path.join(HERE, "dataset", "fixtures", f)) for f in FIXTURES},
    }
    path = os.path.join(HERE, "source-manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"Generated: {path}")
    print(f"HEAD={manifest['git_head']}")
    print(f"files={len(manifest['files'])} fixtures={len(manifest['fixtures'])}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
