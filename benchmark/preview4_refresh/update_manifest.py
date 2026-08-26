#!/usr/bin/env python3
"""Deterministically (re)generate the preview4_refresh sections of
benchmark/source-manifest.json.

Rules:
- Preserves every pre-existing top-level key and every pre-existing
  ``files`` entry EXCEPT the two adapters this refresh intentionally
  modifies (their digests are refreshed in place — that is the coupling
  signal).
- Adds ``files`` entries for the refresh package and the coupled product
  files (souls). Skill *trees* are recorded in the dedicated
  ``preview4_refresh`` section as dir digests, not faked as single files.
- Idempotent: running twice yields identical output.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
MANIFEST = REPO_ROOT / "benchmark" / "source-manifest.json"
SOURCE_COMMIT = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
    capture_output=True, text=True).stdout.strip()


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _dir_digest(p: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        if f.is_file():
            h.update(f.relative_to(p).as_posix().encode())
            h.update(bytes.fromhex(_sha(f)))
    return h.hexdigest()


FILE_ROLES = [
    ("benchmark/adapters/single_agent.py", "adapter_group_A"),
    ("benchmark/adapters/mergepilot.py", "adapter_group_B"),
    ("benchmark/preview4_refresh/product_evidence.py", "coupling_bridge"),
    ("benchmark/preview4_refresh/design.json", "frozen_design"),
    ("benchmark/preview4_refresh/update_manifest.py", "manifest_tool"),
    ("benchmark/preview4_refresh/test_offline_refresh.py", "offline_test"),
    ("benchmark/preview4_refresh/run_smoke.py", "smoke_runner"),
    ("benchmark/preview4_refresh/run_formal.py", "formal_runner"),
    ("benchmark/dataset/cases.jsonl", "dataset_cases"),
    ("benchmark/dataset/dataset-design.json", "dataset_design"),
    ("benchmark/preview4_refresh/coupling-audit.json", "audit_report"),
    ("config/souls/reviewer/SOUL.md", "soul_prompt_reviewer"),
    ("config/souls/fixer/SOUL.md", "soul_prompt_fixer"),
]

DIR_ROLES = [
    ("skills/sast_scan", "skill_sast_scan"),
    ("skills/risk_classify", "skill_risk_classify"),
    ("benchmark/dataset/fixtures", "dataset_fixtures"),
]


def main() -> int:
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))

    files = doc.setdefault("files", {})
    section = {
        "generated_by": "benchmark/preview4_refresh/update_manifest.py",
        "source_commit": SOURCE_COMMIT,
        "note": "additive refresh over the 2026-08-11 baseline manifest; "
                "historical freeze provenance stays recorded in "
                "benchmark/formal-run-manifest.json",
        "files": [],
        "dirs": [],
    }
    for rel, role in FILE_ROLES:
        p = REPO_ROOT / rel
        if not p.is_file():
            print(f"skip (missing): {rel}", file=sys.stderr)
            continue
        sha = _sha(p)
        files[rel] = sha
        section["files"].append({"path": rel, "sha256": sha,
                                 "role": role, "source_commit": SOURCE_COMMIT})
    for rel, role in DIR_ROLES:
        p = REPO_ROOT / rel
        if not p.is_dir():
            print(f"skip (missing): {rel}", file=sys.stderr)
            continue
        section["dirs"].append({"path": rel, "sha256": _dir_digest(p),
                                "role": role, "source_commit": SOURCE_COMMIT})

    doc["preview4_refresh"] = section
    MANIFEST.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"updated {MANIFEST} (files={len(files)}, "
          f"refresh_files={len(section['files'])}, dirs={len(section['dirs'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
