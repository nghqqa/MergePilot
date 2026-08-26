#!/usr/bin/env python3
"""Candidate runner — 10 fixtures x 2 groups x 1 repetition (20 runs).

Same frozen parameters and hardened protocol as smoke2 (see
benchmark/preview4_refresh/design.json, version phaseA2-hardened).
Output dir: benchmark/preview4-refresh-candidate-20260826/

Key handling: process env only, never echoed, never written to artifacts.
No retries, no JSON repair, no selective reruns.
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

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.adapters.base import AdapterInput, CaseMeta, derive_audit_complete  # noqa: E402
from benchmark.adapters.single_agent import SingleAgentAdapter  # noqa: E402
from benchmark.adapters.mergepilot import MergePilotAdapter  # noqa: E402
from benchmark.evaluator import evaluate  # noqa: E402
from benchmark.preview4_refresh.product_evidence import (  # noqa: E402
    build_static_evidence, evidence_digest, load_soul, skill_provenance,
    contract_sha256)

OUT_DIR = REPO_ROOT / "benchmark" / "preview4-refresh-candidate2-20260826"
RAW_DIR = OUT_DIR / "raw-runs"
DATASET = REPO_ROOT / "benchmark" / "dataset"
MODEL_DEFAULT = "deepseek-v4-flash"
TIMEOUT = 120
TOKEN_BUDGET = 4096
CASES = [f"bm-{i:02d}" for i in range(1, 11)]
ALLOWLIST = ("diff_parse", "risk_classify", "sast_scan",
             "test_runner", "pr_lifecycle", "case_retrieval")
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9]"),
)


def _utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha_file(p: Path):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _identity():
    ident = json.loads((HERE / "identity.json").read_text(encoding="utf-8"))
    if ident["product_source_commit"] != subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
            capture_output=True, text=True).stdout.strip():
        raise SystemExit("FATAL: HEAD drifted from identity.json")
    return ident


def load_cases_all():
    rows = []
    for line in (DATASET / "cases.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return {r["case_id"]: r for r in rows}


def check_env_key():
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        if (os.environ.get(name) or "").strip():
            return name
    return None


def run(model: str) -> int:
    ident = _identity()
    key_var = check_env_key()
    if key_var is None:
        print("prerequisite_missing: no API key in env; zero requests made.",
              file=sys.stderr)
        return 3

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cases_all = load_cases_all()
    _, rv_soul_sha = load_soul("reviewer")
    _, fx_soul_sha = load_soul("fixer")
    provenance = skill_provenance()
    adapters = {"A_single_agent": SingleAgentAdapter,
                "B_mergepilot": MergePilotAdapter}

    for case_id in CASES:
        case = cases_all[case_id]
        fx = DATASET / "fixtures" / case["fixture_path"]
        fx_sha = _sha_file(fx)
        if fx_sha != case["fixture_sha256"]:
            print(f"FATAL: fixture pin mismatch {case_id}", file=sys.stderr)
            return 4
        evidence = build_static_evidence(str(fx))
        ev_digest = evidence_digest(evidence)

        for group, cls in adapters.items():  # A then B adjacent
            adapter = cls()
            run_id = (f"{case_id}-{group}-cand-"
                      f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}")
            ai = AdapterInput(
                run_id=run_id, case_id=case_id, fixture_path=str(fx),
                fixture_sha256=fx_sha, model=model,
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
            started, t0 = _utc(), time.time()
            out = adapter.execute(ai)          # one attempt, no retry
            finished = _utc()
            ev = evaluate(out, cm)
            record = {
                "run_id": run_id, "case_id": case_id, "group": group,
                "repetition": 1,
                "product_source_commit": ident["product_source_commit"],
                "benchmark_harness_digest": ident["benchmark_harness_digest"],
                "design_json_sha256": ident["design_json_sha256"],
                "source_manifest_sha256": ident["source_manifest_sha256"],
                "untrusted_contract_sha256": contract_sha256(),
                "fixture_sha256": fx_sha,
                "product_evidence": {
                    "digest_sha256": ev_digest,
                    "sast_rules_version":
                        evidence["sast_scan"]["rules_version"],
                    "provenance": provenance},
                "souls": ({"reviewer_sha256": rv_soul_sha,
                           "fixer_sha256": fx_soul_sha}
                          if group == "B_mergepilot" else None),
                "model_identity": {
                    "provider": ("deepseek" if model.startswith("deepseek")
                                 else "openai-compatible"),
                    "model": model, "temperature": 0.1,
                    "token_budget": TOKEN_BUDGET,
                    "timeout_seconds": TIMEOUT,
                    "key_env_var": key_var,
                    "model_version_comparable_to_20260811": "NOT_CONFIRMED"},
                "input_composition": {
                    "fixture_sha256": fx_sha,
                    "evidence_digest_sha256": ev_digest,
                    "reviewer_soul_sha256": rv_soul_sha,
                    "fixer_soul_sha256": fx_soul_sha,
                    "untrusted_contract_sha256": contract_sha256()},
                "started_at_utc": started, "finished_at_utc": finished,
                "duration_seconds": round(time.time() - t0, 2),
                "adapter_duration_seconds": round(out.duration_seconds, 2),
                "token_usage": out.token_usage,
                "api_request_count": out.api_request_count,
                "status": out.status,
                "parse_status": ("ok" if out.status == "completed"
                                 else (out.error_detail or "n/a")),
                "first_stable_error": out.error_detail,
                "findings": out.findings, "decision": out.decision,
                "fix_description": out.fix_description,
                "audit_events": out.audit_events,
                "audit_complete": derive_audit_complete(
                    out.audit_events, adapter.group_name,
                    len(out.findings) > 0),
                "eval_passed": ev.passed, "eval_reason": ev.reason,
                "eval_tp": ev.tp, "eval_fp": ev.fp, "eval_fn": ev.fn,
                "retry_policy": "none; first stable error preserved",
            }
            (RAW_DIR / f"{run_id}.json").write_bytes(
                (json.dumps(record, ensure_ascii=False, indent=2) + "\n")
                .encode("utf-8"))
            print(f"{run_id}: status={out.status} api={out.api_request_count} "
                  f"err={out.error_detail}")
    return post_process(ident, key_var, model)


def post_process(ident: dict, key_var: str, model: str) -> int:
    raws = sorted(RAW_DIR.glob("*.json"))
    if len(raws) != 20:
        print(f"FATAL: expected 20 raw runs, found {len(raws)}",
              file=sys.stderr)
        return 4
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in raws]
    cases_all = load_cases_all()

    def metrics(group):
        rs = [r for r in runs if r["group"] == group]
        tp = sum(r["eval_tp"] for r in rs)
        fp = sum(r["eval_fp"] for r in rs)
        fn = sum(r["eval_fn"] for r in rs)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = (2 * prec * rec / (prec + rec)) if prec + rec else 0.0
        dec_ok = sum(1 for r in rs
                     if r["decision"] == cases_all[r["case_id"]]["expected_decision"])
        clean = [r for r in rs if cases_all[r["case_id"]].get("clean_case")]
        return {
            "runs": len(rs),
            "schema_completed": sum(1 for r in rs if r["status"] == "completed"),
            "parse_failed": sum(1 for r in rs if r["parse_status"] != "ok"),
            "findings_total": sum(len(r["findings"]) for r in rs),
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(f1, 4),
            "decision_accuracy": round(dec_ok / len(rs), 4),
            "clean_case_runs": len(clean),
            "clean_case_fp": sum(r["eval_fp"] for r in clean),
            "tokens_total": sum((r["token_usage"] or {}).get("total_tokens", 0)
                                for r in rs),
            "api_requests": sum(r["api_request_count"] for r in rs),
            "fixer_triggered": sum(1 for r in rs
                                   if r["api_request_count"] >= 2),
        }

    pairing = {}
    for cid in CASES:
        a = next(r for r in runs if r["case_id"] == cid
                 and r["group"] == "A_single_agent")
        b = next(r for r in runs if r["case_id"] == cid
                 and r["group"] == "B_mergepilot")
        pairing[cid] = {
            "A": {"decision": a["decision"], "fp": a["eval_fp"],
                  "status": a["status"]},
            "B": {"decision": b["decision"], "fp": b["eval_fp"],
                  "status": b["status"]},
            "expected": cases_all[cid]["expected_decision"],
            "evidence_digest_match":
                a["product_evidence"]["digest_sha256"]
                == b["product_evidence"]["digest_sha256"],
        }

    inj = [pairing["bm-09"], ]
    summary = {
        "kind": "preview4-refresh-candidate2-v3",
        "generated_at_utc": _utc(),
        "identity": {k: ident[k] for k in
                     ("product_source_commit", "benchmark_harness_digest",
                      "design_json_sha256", "source_manifest_sha256",
                      "untrusted_contract_sha256")},
        "n_cases": 10, "groups": 2, "repetitions": 1, "total_runs": 20,
        "total_api_requests": sum(r["api_request_count"] for r in runs),
        "key_env_var": key_var,
        "model": model,
        "model_version_comparable_to_20260811": "NOT_CONFIRMED",
        "historical_absolute_comparison": "NOT_COMPARABLE",
        "by_group": {"A_single_agent": metrics("A_single_agent"),
                     "B_mergepilot": metrics("B_mergepilot")},
        "per_case_pairing": pairing,
        "prompt_injection_case": {"bm-09": pairing["bm-09"]},
        "limits": ("N=10 single repetition; directional evidence only; "
                   "cannot replace the 2026-08-11 baseline or enter "
                   "competition materials; formal claims need 20x2x3 with "
                   "variance/CI; Controller/Gateway/GitHub MCP/real E2E "
                   "remain NOT_VERIFIED"),
    }
    (OUT_DIR / "summary.json").write_bytes(
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        .encode("utf-8"))

    # secrets: synthetic fixture values vs real
    syn_vals = set()
    for f in (DATASET / "fixtures").glob("*.py"):
        syn_vals.update(re.findall(r"sk-[A-Za-z0-9]{20,}",
                                   f.read_text(encoding="utf-8")))
    syn, real = [], []
    files = sorted(p for p in OUT_DIR.rglob("*")
                   if p.is_file() and p.name != "SHA256SUMS.txt")
    for p in files:
        t = p.read_text(encoding="utf-8", errors="ignore")
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(t):
                (syn if m.group(0) in syn_vals else real).append(str(p))
    (OUT_DIR / "secret-scan.json").write_bytes(
        (json.dumps({"files_scanned": len(files),
                     "synthetic_fixture_echoes": sorted(set(syn)),
                     "real_credential_hits": sorted(set(real))},
                    ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    lines = [f"{_sha_file(p)}  {p.relative_to(OUT_DIR).as_posix()}"
             for p in sorted(OUT_DIR.rglob("*"))
             if p.is_file() and p.name != "SHA256SUMS.txt"]
    (OUT_DIR / "SHA256SUMS.txt").write_bytes(
        ("\n".join(lines) + "\n").encode("utf-8"))
    print(json.dumps({
        "total_runs": 20,
        "total_api_requests": summary["total_api_requests"],
        "A": summary["by_group"]["A_single_agent"],
        "B": summary["by_group"]["B_mergepilot"],
        "real_secret_hits": sorted(set(real))}, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=MODEL_DEFAULT)
    return run(ap.parse_args().model)


if __name__ == "__main__":
    raise SystemExit(main())
