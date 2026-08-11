#!/usr/bin/env python3
"""Generate machine smoke summary from smoke-runs/*.json.

V2.2: reads all smoke JSON, computes aggregate stats, marks audit_events_missing.
Does NOT modify existing smoke files. Does NOT hand-fill numbers.
"""
from __future__ import annotations
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SMOKE = os.path.join(HERE, "smoke-runs")


def main():
    files = sorted(f for f in os.listdir(SMOKE) if f.endswith(".json")) if os.path.isdir(SMOKE) else []
    runs = []
    for fn in files:
        with open(os.path.join(SMOKE, fn), encoding="utf-8") as f:
            runs.append(json.load(f))

    summary = {
        "total_runs": len(runs),
        "infrastructure_completed": 0,
        "semantic_pass": 0,
        "semantic_fail": 0,
        "total_tokens": 0,
        "total_api_requests": 0,
        "total_duration_seconds": 0.0,
        "cases": [],
    }

    for r in runs:
        completed = r.get("status") == "completed"
        if completed:
            summary["infrastructure_completed"] += 1
        if r.get("eval_passed"):
            summary["semantic_pass"] += 1
        else:
            summary["semantic_fail"] += 1

        tu = r.get("token_usage") or {}
        tt = tu.get("total_tokens") or 0
        summary["total_tokens"] += tt
        summary["total_api_requests"] += r.get("api_request_count", 0)
        summary["total_duration_seconds"] += r.get("duration_seconds", 0)

        has_audit = bool(r.get("audit_events"))
        summary["cases"].append({
            "run_id": r.get("run_id"),
            "case_id": r.get("case_id"),
            "group": r.get("group"),
            "status": r.get("status"),
            "eval_passed": r.get("eval_passed"),
            "eval_reason": r.get("eval_reason"),
            "eval_tp": r.get("eval_tp"),
            "eval_fp": r.get("eval_fp"),
            "eval_fn": r.get("eval_fn"),
            "decision": r.get("decision"),
            "findings_count": len(r.get("findings", [])),
            "total_tokens": tt,
            "api_request_count": r.get("api_request_count", 0),
            "duration_seconds": r.get("duration_seconds"),
            "audit_events_present": has_audit,
            "audit_events_missing": not has_audit,
            "audit_complete": r.get("audit_complete"),
            "audit_complete_unverifiable": not has_audit,
        })

    # Write JSON
    json_path = os.path.join(HERE, "smoke-summary.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # Write Markdown
    md_path = os.path.join(HERE, "smoke-summary.md")
    lines = ["# Smoke Summary (machine-generated)\n",
             f"> From {len(runs)} smoke JSON files. NOT hand-filled.\n"]
    lines.append(f"| Metric | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| infrastructure_completed | {summary['infrastructure_completed']}/{summary['total_runs']} |")
    lines.append(f"| semantic_pass | {summary['semantic_pass']} |")
    lines.append(f"| semantic_fail | {summary['semantic_fail']} |")
    lines.append(f"| total_tokens | {summary['total_tokens']} |")
    lines.append(f"| total_api_requests | {summary['total_api_requests']} |")
    lines.append(f"| total_duration_s | {round(summary['total_duration_seconds'], 2)} |")
    lines.append(f"\n| run_id | case | group | status | eval | tp | fp | fn | decision | tokens | api | dur | audit |")
    lines.append(f"|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c in summary["cases"]:
        audit = "OK" if c["audit_events_present"] else "MISSING"
        lines.append(f"| {c['case_id']} | {c['group']} | {c['status']} | {'PASS' if c['eval_passed'] else 'FAIL'} |"
                     f" {c['eval_tp']} | {c['eval_fp']} | {c['eval_fn']} | {c['decision']} |"
                     f" {c['total_tokens']} | {c['api_request_count']} | {c['duration_seconds']} | {audit} |")
    lines.append(f"\n_Note: smoke files with audit_events_missing=true are V2.1 legacy; "
                 f"their audit_complete is unverifiable._\n")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Generated: {json_path} + {md_path}")
    print(f"infra={summary['infrastructure_completed']}/{summary['total_runs']} "
          f"semantic={summary['semantic_pass']}/{summary['total_runs']} "
          f"tokens={summary['total_tokens']} api={summary['total_api_requests']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
