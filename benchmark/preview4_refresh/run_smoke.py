#!/usr/bin/env python3
"""Phase B smoke runner — 3 fixtures x 2 groups x 1 repetition (6 requests).

Frozen parameters (Phase A design.json): model deepseek-v4-flash,
temperature 0.1 (inside adapters' _call_llm), timeout 120s, token budget
4096, identical static evidence for A and B, SOUL prompts for B.

Hard rules implemented here:
- API key ONLY from process environment (OPENAI_API_KEY / DEEPSEEK_API_KEY).
  If absent: exit 3 with prerequisite_missing BEFORE any request. The key is
  never echoed, logged, or written to any artifact.
- Exactly one attempt per (case, group); no retries, first stable error kept.
- Output directory is exclusive: benchmark/preview4-refresh-smoke-20260826/
- After runs: summary is recomputed from raw files on disk only; secrets are
  scanned; SHA256SUMS generated; historical fingerprints re-verified.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent          # benchmark/preview4_refresh
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.adapters.base import AdapterInput, CaseMeta, derive_audit_complete  # noqa: E402
from benchmark.adapters.single_agent import SingleAgentAdapter  # noqa: E402
from benchmark.adapters.mergepilot import MergePilotAdapter  # noqa: E402
from benchmark.evaluator import evaluate  # noqa: E402
from benchmark.preview4_refresh.product_evidence import (  # noqa: E402
    build_static_evidence, evidence_digest, load_soul, skill_provenance,
    contract_sha256)

OUT_DIR = REPO_ROOT / "benchmark" / "preview4-refresh-smoke3-20260826"
RAW_DIR = OUT_DIR / "raw-runs"
DATASET = REPO_ROOT / "benchmark" / "dataset"
MODEL_DEFAULT = "deepseek-v4-flash"
TIMEOUT = 120
TOKEN_BUDGET = 4096
SMOKE_CASES = ["bm-01", "bm-02", "bm-09"]
ALLOWLIST = ("diff_parse", "risk_classify", "sast_scan",
             "test_runner", "pr_lifecycle", "case_retrieval")
HISTORICAL_PINNED = {
    "benchmark/formal-summary.json":
        "90badbb42591d2395b8ded2ad0a9c097058e87cebe79cc89ae114bee5cee13ea",
    "benchmark/formal-summary.md":
        "6315535a7feb64ab3c02653237fb08d8e35789daa0af825e8afdb50d922dbb93",
    "benchmark/formal-run-manifest.json":
        "389483cb943fc889ef06c5418824cbc233b4035f0c9cecd6eb57a2a3eee187e6",
}
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9]"),
)


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                          capture_output=True, text=True).stdout.strip()


from benchmark.preview4_refresh.canonical_hash import canonical_digest as _sha_file  # noqa: E402


def load_case(case_id: str) -> dict:
    for line in (DATASET / "cases.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r["case_id"] == case_id:
                return r
    raise SystemExit(f"case not found: {case_id}")


def check_env_key() -> str | None:
    """Return the env var NAME holding a key, or None. Never the value."""
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        v = os.environ.get(name)
        if v and v.strip():
            return name
    return None


def run_smoke(model: str) -> int:
    commit = _git_commit()
    if not commit.startswith("5bb2635"):
        print(f"FATAL: worktree HEAD drifted: {commit}", file=sys.stderr)
        return 4
    key_var = check_env_key()
    if key_var is None:
        print("prerequisite_missing: no DEEPSEEK_API_KEY/OPENAI_API_KEY in "
              "environment. Zero model requests were made.", file=sys.stderr)
        return 3
    import json as _json
    ident = _json.loads((HERE / "identity.json").read_text(encoding="utf-8"))
    if ident["product_source_commit"] != commit:
        print("FATAL: HEAD drifted from identity.json", file=sys.stderr)
        return 4

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    _, reviewer_soul_sha = load_soul("reviewer")
    _, fixer_soul_sha = load_soul("fixer")
    provenance = skill_provenance()
    adapters = {"A_single_agent": SingleAgentAdapter,
                "B_mergepilot": MergePilotAdapter}

    manifest_runs = []
    for case_id in SMOKE_CASES:
        case = load_case(case_id)
        fx = DATASET / "fixtures" / case["fixture_path"]
        fx_sha_disk = _sha_file(fx)
        if fx_sha_disk != case["fixture_sha256"]:
            print(f"FATAL: fixture pin mismatch for {case_id}", file=sys.stderr)
            return 4
        evidence = build_static_evidence(str(fx))
        ev_digest = evidence_digest(evidence)

        for group, cls in adapters.items():   # A then B, adjacent in time
            adapter = cls()
            run_id = f"{case_id}-{group}-smoke-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
            ai = AdapterInput(
                run_id=run_id, case_id=case_id, fixture_path=str(fx),
                fixture_sha256=fx_sha_disk, model=model,
                timeout_seconds=TIMEOUT, token_budget=TOKEN_BUDGET,
                tool_allowlist=ALLOWLIST)
            cm = CaseMeta(
                case_id=case_id,
                expected_decision=case["expected_decision"],
                ground_truth_findings=case.get("ground_truth_findings", []),
                acceptable_variants=case.get("acceptable_variants", []),
                forbidden_actions=case.get("forbidden_actions", []),
                clean_case=case.get("clean_case", False),
                rollback_required=case.get("rollback_required", False),
                pass_fail_criteria=case.get("pass_fail_criteria", {}))

            started = _utc()
            t0 = time.time()
            out = adapter.execute(ai)          # exactly one attempt, no retry
            finished = _utc()
            ev = evaluate(out, cm)

            record = {
                "run_id": run_id,
                "case_id": case_id,
                "group": group,
                "repetition": 1,
                "product_source_commit": commit,
                "benchmark_harness_digest": ident["benchmark_harness_digest"],
                "design_json_sha256": ident["design_json_sha256"],
                "source_manifest_sha256": ident["source_manifest_sha256"],
                "fixture_sha256": fx_sha_disk,
                "product_evidence": {
                    "digest_sha256": ev_digest,
                    "sast_rules_version":
                        evidence["sast_scan"]["rules_version"],
                    "provenance": provenance,
                },
                "souls": ({"reviewer_sha256": reviewer_soul_sha,
                           "fixer_sha256": fixer_soul_sha}
                          if group == "B_mergepilot" else None),
                "hardening": {
                    "untrusted_contract_sha256": contract_sha256(),
                    "design_version": "preview4-refresh-20260826-phaseA2-hardened",
                },
                "model_identity": {
                    "provider": ("deepseek" if model.startswith("deepseek")
                                 else "openai-compatible"),
                    "model": model, "temperature": 0.1,
                    "token_budget": TOKEN_BUDGET, "timeout_seconds": TIMEOUT,
                    "key_env_var": key_var,   # name only, never the value
                    "model_version_comparable_to_20260811": "NOT_CONFIRMED",
                },
                "input_composition": {
                    "fixture_sha256": fx_sha_disk,
                    "evidence_digest_sha256": ev_digest,
                    "reviewer_soul_sha256": reviewer_soul_sha,
                    "fixer_soul_sha256": fixer_soul_sha,
                },
                "started_at_utc": started,
                "finished_at_utc": finished,
                "duration_seconds": round(time.time() - t0, 2),
                "adapter_duration_seconds": round(out.duration_seconds, 2),
                "token_usage": out.token_usage,
                "api_request_count": out.api_request_count,
                "status": out.status,
                "parse_status": ("ok" if out.status == "completed"
                                 else (out.error_detail or "n/a")),
                "first_stable_error": out.error_detail,
                "findings": out.findings,
                "decision": out.decision,
                "fix_description": out.fix_description,
                "audit_events": out.audit_events,
                "audit_complete": derive_audit_complete(
                    out.audit_events, adapter.group_name,
                    len(out.findings) > 0),
                "eval_passed": ev.passed, "eval_reason": ev.reason,
                "eval_tp": ev.tp, "eval_fp": ev.fp, "eval_fn": ev.fn,
                "retry_policy": "none; first stable error preserved",
            }
            path = RAW_DIR / f"{run_id}.json"
            path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            manifest_runs.append({"file": path.name, "status": out.status,
                                  "api_request_count": out.api_request_count})
            print(f"{run_id}: status={out.status} "
                  f"api={out.api_request_count} err={out.error_detail}")

    return post_process(commit, key_var, manifest_runs)


def post_process(commit: str, key_var: str, manifest_runs: list) -> int:
    """Recompute summary from disk; secrets scan; SHA256SUMS; history check."""
    ident = json.loads((HERE / "identity.json").read_text(encoding="utf-8"))
    raws = sorted(RAW_DIR.glob("*.json"))
    if len(raws) != 6:
        print(f"FATAL: expected 6 raw runs, found {len(raws)}",
              file=sys.stderr)
        return 4
    total_requests = 0
    by_group = {}
    for p in raws:
        r = json.loads(p.read_text(encoding="utf-8"))
        total_requests += r["api_request_count"]
        g = by_group.setdefault(r["group"], {"runs": 0, "completed": 0,
                                             "errors": []})
        g["runs"] += 1
        if r["status"] == "completed":
            g["completed"] += 1
        elif r["first_stable_error"]:
            g["errors"].append({"run_id": r["run_id"],
                                "error": r["first_stable_error"]})

    # fairness check from disk: A and B evidence digests must match per case
    digests = {}
    for p in raws:
        r = json.loads(p.read_text(encoding="utf-8"))
        digests.setdefault(r["case_id"], {})[r["group"]] = \
            r["product_evidence"]["digest_sha256"]
    fairness_ok = all(v.get("A_single_agent") == v.get("B_mergepilot")
                      for v in digests.values())
    commits_ok = all(json.loads(p.read_text(encoding="utf-8"))
                     ["product_source_commit"] == commit for p in raws)

    summary = {
        "kind": "preview4-refresh-smoke3-jsonmode",
        "generated_at_utc": _utc(),
        "source_commit": commit,
        "identity": {k: ident[k] for k in
                     ("product_source_commit", "benchmark_harness_digest",
                      "design_json_sha256", "source_manifest_sha256",
                      "untrusted_contract_sha256")},
        "design": "benchmark/preview4_refresh/design.json",
        "n_cases": len(SMOKE_CASES), "groups": 2, "repetitions": 1,
        "total_runs": len(raws),
        "total_api_requests": total_requests,
        "key_env_var": key_var,
        "by_group": by_group,
        "fairness_evidence_digests_match_per_case": fairness_ok,
        "source_commit_consistent": commits_ok,
        "historical_absolute_comparison": "NOT_COMPARABLE "
        "(model identity not re-confirmed vs 2026-08-11)",
        "verdict_hint": "smoke readiness only; NOT a Preview 4 performance "
                        "conclusion",
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    # secrets scan over every artifact in OUT_DIR
    findings = []
    for p in sorted(OUT_DIR.rglob("*")):
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="ignore")
            for pat in SECRET_PATTERNS:
                if pat.search(text):
                    findings.append(str(p.relative_to(OUT_DIR)))
    (OUT_DIR / "secret-scan.json").write_text(json.dumps(
        {"files_scanned": len(list(OUT_DIR.rglob("*"))),
         "hits": sorted(set(findings))}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    # historical fingerprints
    history_ok = True
    for rel, pinned in HISTORICAL_PINNED.items():
        if _sha_file(REPO_ROOT / rel) != pinned:
            history_ok = False
    (OUT_DIR / "history-check.json").write_text(json.dumps(
        {"historical_files_unchanged": history_ok},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # SHA256SUMS for all final artifacts
    lines = []
    for p in sorted(OUT_DIR.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS.txt":
            lines.append(f"{_sha_file(p)}  {p.relative_to(OUT_DIR).as_posix()}")
    (OUT_DIR / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n",
                                            encoding="utf-8")

    print(json.dumps({"total_runs": len(raws),
                      "total_api_requests": total_requests,
                      "fairness_ok": fairness_ok,
                      "commits_ok": commits_ok,
                      "secret_hits": sorted(set(findings)),
                      "history_ok": history_ok}, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=MODEL_DEFAULT)
    args = ap.parse_args()
    return run_smoke(args.model)


if __name__ == "__main__":
    raise SystemExit(main())
