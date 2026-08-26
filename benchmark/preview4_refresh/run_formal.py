#!/usr/bin/env python3
"""Formal runner — 20 cases x 2 groups x 3 repetitions (120 runs).

Frozen deterministic balanced schedule (schedule.json, written before any
API call; SHA256 recorded). Order alternates AB/BA by (case_index + rep)
parity => exactly 30 AB and 30 BA pairs. Single-threaded.

Resume contract: raw-run filenames are derived from schedule_index only, so
re-invocation skips completed items. A started-journal entry whose raw run is
missing means ambiguous attribution => the round must be BLOCKED, so we abort
with a dedicated exit code instead of re-running it.

No retries, no JSON repair, no selective reruns, no case special-casing.
Key: process env only, never printed, never written to artifacts.
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

OUT_DIR = REPO_ROOT / "benchmark" / "preview4-refresh-formal-20260826"
RAW_DIR = OUT_DIR / "raw-runs"
JOURNAL = OUT_DIR / "journal.jsonl"
DATASET = REPO_ROOT / "benchmark" / "dataset"
MODEL_DEFAULT = "deepseek-v4-flash"
TIMEOUT = 120
TOKEN_BUDGET = 4096
REPS = 3
ALLOWLIST = ("diff_parse", "risk_classify", "sast_scan",
             "test_runner", "pr_lifecycle", "case_retrieval")
BOOTSTRAP_SEED = 20260826
BOOTSTRAP_N = 10000
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9]"),
)


def _utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


from benchmark.preview4_refresh.canonical_hash import canonical_digest as _sha_file


def load_cases():
    rows = [json.loads(l) for l in
            (DATASET / "cases.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    rows.sort(key=lambda r: r["case_id"])
    return rows


def build_schedule(cases):
    items = []
    idx = 0
    for ci, case in enumerate(cases):
        for rep in range(1, REPS + 1):
            order = "AB" if (ci + rep) % 2 == 0 else "BA"
            groups = (["A_single_agent", "B_mergepilot"] if order == "AB"
                      else ["B_mergepilot", "A_single_agent"])
            for g in groups:
                items.append({"schedule_index": idx, "case_id": case["case_id"],
                              "group": g, "repetition": rep,
                              "pair_order": order})
                idx += 1
    return items


def check_env_key():
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        if (os.environ.get(name) or "").strip():
            return name
    return None


def _run_id(item):
    return f"{item['case_id']}-{item['group']}-formal-s{item['schedule_index']:03d}"


def _journal_append(rec):
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def run(model):
    ident = json.loads((HERE / "identity.json").read_text(encoding="utf-8"))
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                            capture_output=True, text=True).stdout.strip()
    if ident["product_source_commit"] != commit:
        print("FATAL: HEAD drifted from identity.json", file=sys.stderr)
        return 4
    key_var = check_env_key()
    if key_var is None:
        print("prerequisite_missing: no API key in env; zero requests.",
              file=sys.stderr)
        return 3

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cases = {c["case_id"]: c for c in load_cases()}
    schedule = build_schedule(list(cases.values()))

    sched_path = OUT_DIR / "schedule.json"
    if sched_path.exists():
        frozen = json.loads(sched_path.read_text(encoding="utf-8"))
        if frozen["items"] != schedule:
            print("FATAL: schedule drift vs frozen schedule.json",
                  file=sys.stderr)
            return 4
    else:
        sched_path.write_bytes((json.dumps(
            {"generated_at_utc": _utc(), "items": schedule},
            ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        _journal_append({"event": "schedule_frozen",
                         "schedule_sha256": _sha_file(sched_path),
                         "utc": _utc()})
    print(f"schedule: {len(schedule)} items, sha="
          f"{_sha_file(sched_path)[:12]}")

    # ambiguous-attribution guard: journal 'started' with no raw run on disk
    done = {p.stem for p in RAW_DIR.glob("*.json")}
    started = set()
    if JOURNAL.exists():
        for line in JOURNAL.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("event") == "run_started":
                    started.add(r["run_id"])
    ambiguous = [rid for rid in started - done
                 if not (RAW_DIR / f"{rid}.json").exists()]
    if ambiguous:
        print(f"FATAL: ambiguous attribution for {len(ambiguous)} run(s); "
              "round must be BLOCKED by hand.", file=sys.stderr)
        return 5

    _, rv_soul_sha = load_soul("reviewer")
    _, fx_soul_sha = load_soul("fixer")
    provenance = skill_provenance()
    adapters = {"A_single_agent": SingleAgentAdapter,
                "B_mergepilot": MergePilotAdapter}
    evidence_cache = {}

    for item in schedule:
        rid = _run_id(item)
        if rid in done:
            continue
        case = cases[item["case_id"]]
        fx = DATASET / "fixtures" / case["fixture_path"]
        fx_sha = _sha_file(fx)
        if fx_sha != case["fixture_sha256"]:
            print(f"FATAL: fixture pin mismatch {item['case_id']}",
                  file=sys.stderr)
            return 4
        if item["case_id"] not in evidence_cache:
            evidence_cache[item["case_id"]] = build_static_evidence(str(fx))
        evidence = evidence_cache[item["case_id"]]
        ev_digest = evidence_digest(evidence)

        adapter = adapters[item["group"]]()
        ai = AdapterInput(
            run_id=rid, case_id=item["case_id"], fixture_path=str(fx),
            fixture_sha256=fx_sha, model=model,
            timeout_seconds=TIMEOUT, token_budget=TOKEN_BUDGET,
            tool_allowlist=ALLOWLIST)
        cm = CaseMeta(
            case_id=item["case_id"],
            expected_decision=case["expected_decision"],
            ground_truth_findings=case.get("ground_truth_findings", []),
            acceptable_variants=case.get("acceptable_variants", []),
            forbidden_actions=case.get("forbidden_actions", []),
            clean_case=case.get("clean_case", False),
            rollback_required=case.get("rollback_required", False),
            pass_fail_criteria=case.get("pass_fail_criteria", {}))
        _journal_append({"event": "run_started", "run_id": rid,
                         "schedule_index": item["schedule_index"],
                         "utc": _utc()})
        t0 = time.time()
        started = _utc()
        out = adapter.execute(ai)        # exactly one attempt
        record = {
            "run_id": rid, "case_id": item["case_id"], "group": item["group"],
            "repetition": item["repetition"],
            "schedule_index": item["schedule_index"],
            "pair_order": item["pair_order"],
            "product_source_commit": ident["product_source_commit"],
            "benchmark_harness_digest": ident["benchmark_harness_digest"],
            "design_json_sha256": ident["design_json_sha256"],
            "source_manifest_sha256": ident["source_manifest_sha256"],
            "dataset_manifest_sha256": ident.get("dataset_manifest_sha256"),
            "untrusted_contract_sha256": contract_sha256(),
            "schedule_sha256": _sha_file(sched_path),
            "fixture_sha256": fx_sha,
            "product_evidence": {
                "digest_sha256": ev_digest,
                "sast_rules_version": evidence["sast_scan"]["rules_version"],
                "provenance": provenance},
            "souls": ({"reviewer_sha256": rv_soul_sha,
                       "fixer_sha256": fx_soul_sha}
                      if item["group"] == "B_mergepilot" else None),
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
            "started_at_utc": started, "finished_at_utc": _utc(),
            "duration_seconds": round(time.time() - t0, 2),
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
            "eval_passed": None, "eval_reason": None,
            "eval_tp": None, "eval_fp": None, "eval_fn": None,
            "no_retry": True, "no_auto_repair": True,
        }
        ev = evaluate(out, cm)
        record.update({"eval_passed": ev.passed, "eval_reason": ev.reason,
                       "eval_tp": ev.tp, "eval_fp": ev.fp, "eval_fn": ev.fn})
        (RAW_DIR / f"{rid}.json").write_bytes(
            (json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            .encode("utf-8"))
        _journal_append({"event": "run_finished", "run_id": rid,
                         "status": out.status, "utc": _utc()})
        done.add(rid)
        if item["schedule_index"] % 20 == 0 or item["schedule_index"] == 119:
            print(f"[{item['schedule_index']+1}/120] {rid}: {out.status}",
                  flush=True)
    return post_process(ident, key_var, model)


def post_process(ident, key_var, model):
    import statistics
    raws = sorted(RAW_DIR.glob("*.json"))
    if len(raws) != 120:
        print(f"FATAL: expected 120 raw runs, found {len(raws)}",
              file=sys.stderr)
        return 4
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in raws]
    cases = {c["case_id"]: c for c in load_cases()}
    schedule_sha = runs[0]["schedule_sha256"]

    def group_metrics(rs):
        tp = sum(r["eval_tp"] for r in rs)
        fp = sum(r["eval_fp"] for r in rs)
        fn = sum(r["eval_fn"] for r in rs)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        dec = sum(1 for r in rs if r["decision"]
                  == cases[r["case_id"]]["expected_decision"])
        clean = [r for r in rs if cases[r["case_id"]].get("clean_case")]
        durs = sorted(r["duration_seconds"] for r in rs)
        p95 = durs[min(int(0.95 * len(durs)), len(durs) - 1)]
        return {"runs": len(rs), "tp": tp, "fp": fp, "fn": fn,
                "precision": round(prec, 4), "recall": round(rec, 4),
                "f1": round(f1, 4),
                "decision_accuracy": round(dec / len(rs), 4),
                "clean_case_runs": len(clean),
                "clean_case_fp": sum(r["eval_fp"] for r in clean),
                "tokens_total": sum((r["token_usage"] or {})
                                    .get("total_tokens", 0) for r in rs),
                "api_requests": sum(r["api_request_count"] for r in rs),
                "fixer_triggered": sum(1 for r in rs
                                       if r["api_request_count"] >= 2),
                "duration_mean": round(sum(durs) / len(durs), 2),
                "duration_p50": round(durs[len(durs)//2], 2),
                "duration_p95": round(p95, 2)}

    by_group = {g: group_metrics([r for r in runs if r["group"] == g])
                for g in ("A_single_agent", "B_mergepilot")}
    by_rep = {}
    for rep in (1, 2, 3):
        by_rep[rep] = {g: group_metrics([r for r in runs
                                         if r["group"] == g
                                         and r["repetition"] == rep])
                       for g in ("A_single_agent", "B_mergepilot")}
    mean_sd = {}
    for g in ("A_single_agent", "B_mergepilot"):
        mean_sd[g] = {}
        for m in ("precision", "recall", "f1", "decision_accuracy"):
            vals = [by_rep[rep][g][m] for rep in (1, 2, 3)]
            mean_sd[g][m] = {"mean": round(statistics.mean(vals), 4),
                             "sd": round(statistics.stdev(vals), 4)}

    # per-case pairing and stability
    pairing, stability = {}, {}
    for cid in sorted(cases):
        a = [r for r in runs if r["case_id"] == cid
             and r["group"] == "A_single_agent"]
        b = [r for r in runs if r["case_id"] == cid
             and r["group"] == "B_mergepilot"]
        pairing[cid] = {
            "expected": cases[cid]["expected_decision"],
            "A_decisions": [r["decision"] for r in a],
            "B_decisions": [r["decision"] for r in b],
            "A_fp_mean": round(sum(r["eval_fp"] for r in a) / 3, 2),
            "B_fp_mean": round(sum(r["eval_fp"] for r in b) / 3, 2)}
        stability[cid] = {
            "A_stable": len({r["decision"] for r in a}) == 1,
            "B_stable": len({r["decision"] for r in b}) == 1}

    # case-cluster bootstrap for B-A deltas (resample 20 cases with replacement)
    import random
    rng = random.Random(BOOTSTRAP_SEED)
    case_ids = sorted(cases)
    metrics = ("precision", "recall", "f1", "decision_accuracy")
    boot = {m: [] for m in metrics}
    boot["clean_fp_delta"] = []
    boot["token_delta_pct"] = []
    idx_by = {}
    for cid in case_ids:
        idx_by[cid] = {g: [r for r in runs if r["case_id"] == cid
                           and r["group"] == g]
                       for g in ("A_single_agent", "B_mergepilot")}

    def _cell(rs, m):
        tp = sum(r["eval_tp"] for r in rs); fp = sum(r["eval_fp"] for r in rs)
        fn = sum(r["eval_fn"] for r in rs)
        pr = tp/(tp+fp) if tp+fp else 0.0
        rc = tp/(tp+fn) if tp+fn else 0.0
        f1 = 2*pr*rc/(pr+rc) if pr+rc else 0.0
        dec = sum(1 for r in rs if r["decision"]
                  == cases[rs[0]["case_id"]]["expected_decision"])
        return {"precision": pr, "recall": rc, "f1": f1,
                "decision_accuracy": dec / len(rs)}[m]

    for _ in range(BOOTSTRAP_N):
        sample = [rng.choice(case_ids) for _ in case_ids]
        A = [r for cid in sample for r in idx_by[cid]["A_single_agent"]]
        B = [r for cid in sample for r in idx_by[cid]["B_mergepilot"]]
        for m in metrics:
            boot[m].append(_cell(B, m) - _cell(A, m))
        cleanA = [r for r in A if cases[r["case_id"]].get("clean_case")]
        cleanB = [r for r in B if cases[r["case_id"]].get("clean_case")]
        boot["clean_fp_delta"].append(
            sum(r["eval_fp"] for r in cleanB) - sum(r["eval_fp"] for r in cleanA))
        ta = sum((r["token_usage"] or {}).get("total_tokens", 0) for r in A)
        tb = sum((r["token_usage"] or {}).get("total_tokens", 0) for r in B)
        boot["token_delta_pct"].append((tb - ta) / ta * 100 if ta else 0.0)

    def ci(vals):
        vals = sorted(vals)
        lo = vals[int(0.025 * len(vals))]
        hi = vals[int(0.975 * len(vals))]
        return [round(lo, 4), round(hi, 4)]

    deltas = {m: {"B_minus_A_mean": round(statistics.mean(boot[m]), 4),
                  "ci95": ci(boot[m])} for m in metrics}
    deltas["clean_case_fp_delta"] = {
        "B_minus_A_mean": round(statistics.mean(boot["clean_fp_delta"]), 3),
        "ci95": ci(boot["clean_fp_delta"])}
    deltas["token_delta_pct"] = {
        "mean": round(statistics.mean(boot["token_delta_pct"]), 2),
        "ci95": ci(boot["token_delta_pct"])}

    summary = {
        "kind": "preview4-refresh-formal-20x2x3",
        "generated_at_utc": _utc(),
        "identity": {**{k: ident[k] for k in
                        ("product_source_commit", "benchmark_harness_digest",
                         "design_json_sha256", "source_manifest_sha256",
                         "untrusted_contract_sha256")},
                     "dataset_manifest_sha256":
                         ident.get("dataset_manifest_sha256"),
                     "schedule_sha256": schedule_sha},
        "n_cases": 20, "groups": 2, "repetitions": 3, "total_runs": 120,
        "total_api_requests": sum(r["api_request_count"] for r in runs),
        "key_env_var": key_var, "model": model,
        "model_version_comparable_to_20260811": "NOT_CONFIRMED",
        "historical_absolute_comparison": "NOT_COMPARABLE",
        "bootstrap": {"seed": BOOTSTRAP_SEED, "n": BOOTSTRAP_N,
                      "unit": "case-cluster (resample 20 cases, keep 3 reps "
                              "and A/B pairing)"},
        "by_group": by_group, "by_repetition": by_rep, "mean_sd": mean_sd,
        "per_case_pairing": pairing, "per_case_stability": stability,
        "bootstrap_B_minus_A": deltas,
        "limits": ("20 synthetic fixtures x3 reps controlled evaluation; "
                   "directional evidence; NOT comparable by attribution to "
                   "2026-08-11; Controller/Gateway/GitHub MCP/real E2E "
                   "NOT_VERIFIED; not production ready; do not enter "
                   "competition materials before user review"),
    }
    (OUT_DIR / "summary.json").write_bytes(
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        .encode("utf-8"))

    # secrets scan (synthetic fixture values classified separately)
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
    (OUT_DIR / "secret-scan.json").write_bytes((json.dumps(
        {"files_scanned": len(files),
         "synthetic_fixture_echoes": sorted(set(syn)),
         "real_credential_hits": sorted(set(real))},
        ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    lines = [f"{_sha_file(p)}  {p.relative_to(OUT_DIR).as_posix()}"
             for p in sorted(OUT_DIR.rglob("*"))
             if p.is_file() and p.name != "SHA256SUMS.txt"]
    (OUT_DIR / "SHA256SUMS.txt").write_bytes(
        ("\n".join(lines) + "\n").encode("utf-8"))
    print(json.dumps({"total_runs": 120,
                      "total_api_requests": summary["total_api_requests"],
                      "A": by_group["A_single_agent"],
                      "B": by_group["B_mergepilot"],
                      "real_secret_hits": sorted(set(real))},
                     ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=MODEL_DEFAULT)
    return run(ap.parse_args().model)


if __name__ == "__main__":
    raise SystemExit(main())
