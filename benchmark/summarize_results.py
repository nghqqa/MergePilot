#!/usr/bin/env python3
"""Summarize raw benchmark results into results.csv + report.md.

METRIC DOWNGRADE: fix/verify/rollback metrics removed (no real patch/verify).
Supported metrics: case_pass_rate, execution_completion_rate, precision, recall,
F1, decision_accuracy, duration, token_usage, audit_completeness.
Unsupported (removed): first_pass_fix_rate, final_fix_rate, verification_rate,
rollback_success_rate, L2_miss_rate, RAG metrics, model_cost.
"""
from __future__ import annotations
import csv, json, os, statistics, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw-runs")

SUPPORTED_METRICS = [
    "n_runs", "case_pass_rate", "execution_completion_rate",
    "precision", "recall", "f1", "decision_accuracy",
    "mean_duration_s", "p50_duration_s", "p95_duration_s",
    "total_tokens", "audit_completeness",
]


def load_raw_runs():
    runs = []
    if not os.path.isdir(RAW):
        return runs
    for f in sorted(os.listdir(RAW)):
        if f.endswith(".json"):
            with open(os.path.join(RAW, f), encoding="utf-8") as fh:
                runs.append(json.load(fh))
    return runs


def compute_metrics(runs, cases_by_id):
    groups = defaultdict(list)
    for r in runs:
        groups[r.get("group", "?")].append(r)
    results = {}
    for group, gr in groups.items():
        n = len(gr)
        if n == 0:
            continue
        completed = [r for r in gr if r["status"] == "completed"]
        passed = [r for r in gr if r.get("eval_passed")]
        # Case pass rate (semantic)
        case_pass = len(passed) / n
        # Execution completion
        exec_rate = len(completed) / n
        # Finding-level metrics
        tp = sum(r.get("eval_tp", 0) for r in gr)
        fp = sum(r.get("eval_fp", 0) for r in gr)
        fn = sum(r.get("eval_fn", 0) for r in gr)
        prec = tp / (tp + fp) if (tp + fp) > 0 else None
        rec = tp / (tp + fn) if (tp + fn) > 0 else None
        f1 = (2 * prec * rec / (prec + rec)) if prec and rec and (prec + rec) > 0 else None
        # Decision accuracy
        dec_ok = sum(1 for r in gr if r.get("decision") == cases_by_id.get(r["case_id"], {}).get("expected_decision"))
        # Duration
        durs = [r["duration_seconds"] for r in completed if r.get("duration_seconds")]
        # Tokens
        toks = [r.get("token_usage", {}) or {} for r in completed]
        total_t = sum(t.get("total_tokens") or 0 for t in toks) if any(t.get("total_tokens") for t in toks) else None
        # Audit
        audit_ok = sum(1 for r in gr if r.get("audit_complete"))

        def _v(x):
            return round(x, 4) if isinstance(x, float) else ("null" if x is None else x)

        results[group] = {
            "n_runs": n,
            "case_pass_rate": _v(case_pass),
            "execution_completion_rate": _v(exec_rate),
            "precision": _v(prec),
            "recall": _v(rec),
            "f1": _v(f1),
            "decision_accuracy": _v(dec_ok / n),
            "mean_duration_s": _v(statistics.mean(durs)) if durs else "null",
            "p50_duration_s": _v(statistics.median(durs)) if durs else "null",
            "p95_duration_s": _v(statistics.quantiles(durs, n=20)[18]) if len(durs) >= 20 else (_v(max(durs)) if durs else "null"),
            "total_tokens": total_t if total_t is not None else "null",
            "audit_completeness": _v(audit_ok / n),
        }
    return results


def write_csv(results, path):
    cols = ["group"] + SUPPORTED_METRICS
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for g, m in sorted(results.items()):
            row = {"group": g}
            row.update({k: m.get(k, "null") for k in SUPPORTED_METRICS})
            w.writerow(row)


def write_report(results, runs, path):
    lines = ["# Benchmark Report\n",
             f"> Generated from {len(runs)} raw runs.\n"]
    if not results:
        lines.append("**No completed runs.**\n")
    else:
        lines.append("| Metric | A (single) | B (mergepilot) |")
        lines.append("|---|---|---|")
        a = results.get("A_single_agent", {})
        b = results.get("B_mergepilot", {})
        for k in SUPPORTED_METRICS:
            if k == "n_runs":
                continue
            lines.append(f"| {k} | {a.get(k,'n/a')} | {b.get(k,'n/a')} |")
    lines.append("\n**Unsupported metrics (removed):** first_pass_fix_rate, "
                 "final_fix_rate, verification_rate, rollback_success_rate, "
                 "L2_miss_rate, RAG metrics, model_cost.\n")
    lines.append("\n_This file is auto-generated. Do not edit manually._\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    runs = load_raw_runs()
    cases_by_id = {}
    cp = os.path.join(HERE, "dataset", "cases.jsonl")
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as f:
            for l in f:
                if l.strip():
                    c = json.loads(l)
                    cases_by_id[c["case_id"]] = c
    results = compute_metrics(runs, cases_by_id) if runs else {}
    write_csv(results, os.path.join(HERE, "results.csv"))
    write_report(results, runs, os.path.join(HERE, "report.md"))
    if runs:
        print(f"Summarized {len(runs)} runs")
    else:
        print("No raw runs found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
