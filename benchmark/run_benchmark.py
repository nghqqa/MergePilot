#!/usr/bin/env python3
"""Benchmark runner V2.2 — audit_events persisted, api_request_count, real timestamps."""
from __future__ import annotations
import argparse, json, os, sys, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from benchmark.adapters.base import AdapterInput, CaseMeta, AdapterOutput, derive_audit_complete
from benchmark.adapters.single_agent import SingleAgentAdapter
from benchmark.adapters.mergepilot import MergePilotAdapter
from benchmark.evaluator import evaluate

DATASET = os.path.join(HERE, "dataset")
FIXTURES = os.path.join(DATASET, "fixtures")
RAW = os.path.join(HERE, "raw-runs")
SMOKE = os.path.join(HERE, "smoke-runs")
SCHEMAS = os.path.join(HERE, "schemas")

ADAPTERS = {"A_single_agent": SingleAgentAdapter, "B_mergepilot": MergePilotAdapter}
DEFAULT_TIMEOUT = 120
DEFAULT_TOKEN_BUDGET = 4096
DEFAULT_ALLOWLIST = ("diff_parse", "risk_classify", "sast_scan",
                     "test_runner", "pr_lifecycle", "case_retrieval")


def load_cases():
    with open(os.path.join(DATASET, "cases.jsonl"), encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

def load_schema():
    with open(os.path.join(SCHEMAS, "run-result.schema.json"), encoding="utf-8") as f:
        return json.load(f)

def split_case(case, model, timeout, token_budget):
    ai = AdapterInput(
        run_id=f"{case['case_id']}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        case_id=case["case_id"],
        fixture_path=os.path.join(FIXTURES, case["fixture_path"]),
        fixture_sha256=case["fixture_sha256"],
        model=model, timeout_seconds=timeout, token_budget=token_budget,
        tool_allowlist=DEFAULT_ALLOWLIST)
    cm = CaseMeta(
        case_id=case["case_id"],
        expected_decision=case["expected_decision"],
        ground_truth_findings=case.get("ground_truth_findings", []),
        acceptable_variants=case.get("acceptable_variants", []),
        forbidden_actions=case.get("forbidden_actions", []),
        clean_case=case.get("clean_case", False),
        rollback_required=case.get("rollback_required", False),
        pass_fail_criteria=case.get("pass_fail_criteria", {}))
    return ai, cm

def build_result(inp, adapter, out, ev, started_at, finished_at):
    rid = f"{inp.case_id}-{adapter.group_name}-{inp.run_id.split('-')[-1]}"
    # Derive audit_complete from actual events, NOT adapter self-report
    audit_ok = derive_audit_complete(out.audit_events, adapter.group_name, len(out.findings) > 0)
    return {
        "run_id": rid, "case_id": inp.case_id, "group": adapter.group_name,
        "model": inp.model, "started_at": started_at, "finished_at": finished_at,
        "status": out.status, "findings": out.findings,
        "decision": out.decision, "fix_applied": out.fix_applied,
        "fix_description": out.fix_description,
        "verification_passed": out.verification_passed,
        "rollback_executed": out.rollback_executed,
        "human_interventions": out.human_interventions,
        "duration_seconds": round(out.duration_seconds, 2),
        "token_usage": out.token_usage, "model_cost": out.model_cost,
        "audit_events": out.audit_events,
        "audit_complete": audit_ok,
        "error_detail": out.error_detail,
        "rag_citations": out.rag_citations,
        "api_request_count": out.api_request_count,
        "eval_passed": ev.passed, "eval_reason": ev.reason,
        "eval_tp": ev.tp, "eval_fp": ev.fp, "eval_fn": ev.fn,
    }

def save_result(result, is_smoke):
    out_dir = SMOKE if is_smoke else RAW
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{result['run_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return path

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--group", choices=list(ADAPTERS), required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--case-id")
    args = p.parse_args()

    cases = load_cases()
    if args.smoke:
        if not args.case_id:
            print("ERROR: --smoke requires --case-id", file=sys.stderr); return 2
        cases = [c for c in cases if c["case_id"] == args.case_id]
        if not cases:
            print(f"ERROR: {args.case_id} not found", file=sys.stderr); return 2

    adapter = ADAPTERS[args.group]()
    try:
        import jsonschema
    except ImportError:
        print("ERROR: jsonschema required", file=sys.stderr); return 2
    schema = load_schema()

    saved = passed = failed = 0

    for case in cases:
        print(f"  [{case['case_id']}] {case['title'][:50]}...")
        ai, cm = split_case(case, args.model, args.timeout, args.token_budget)

        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        out = adapter.execute(ai)
        finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        ev = evaluate(out, cm)
        result = build_result(ai, adapter, out, ev, started_at, finished_at)

        try:
            jsonschema.validate(result, schema)
        except jsonschema.ValidationError as exc:
            result["status"] = "error"
            result["error_detail"] = "schema_failed"
            result["eval_passed"] = False
            result["eval_reason"] = "schema_failed"

        path = save_result(result, args.smoke)
        saved += 1

        if result["status"] == "prerequisite_missing":
            print(f"    prerequisite_missing: {result.get('error_detail','')[:60]}")
            break

        if result["status"] != "completed" or not result.get("eval_passed"):
            failed += 1
            verdict = "FAIL"
        else:
            passed += 1
            verdict = "PASS"
        print(f"    {result['status']} eval={verdict} ({result.get('eval_reason','')[:60]})")

    has_failures = failed > 0 or saved == 0
    print(f"\nDone: {saved} saved, {passed} passed, {failed} failed")
    return 1 if has_failures else 0

if __name__ == "__main__":
    sys.exit(main())
