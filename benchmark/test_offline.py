#!/usr/bin/env python3
"""Offline tests V2.2 — audit persistence, api_count, timestamps, smoke summary, manifest."""
from __future__ import annotations
import dataclasses, hashlib, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from benchmark.adapters.base import AdapterInput, CaseMeta, AdapterOutput, derive_audit_complete
from benchmark.evaluator import evaluate

def _x(cond, msg):
    if not cond: raise AssertionError("FAIL: " + msg)
    print("  PASS:", msg)

def _dir_sha(path):
    h = hashlib.sha256()
    if os.path.isfile(path):
        with open(path, "rb") as f: h.update(f.read())
    elif os.path.isdir(path):
        for fn in sorted(os.listdir(path)):
            fp = os.path.join(path, fn)
            if os.path.isfile(fp):
                h.update(fn.encode())
                with open(fp, "rb") as f: h.update(f.read())
    return h.hexdigest()

REAL_PATHS = [
    os.path.join(HERE, "raw-runs"),
    os.path.join(HERE, "smoke-runs"),
    os.path.join(HERE, "results.csv"),
    os.path.join(HERE, "report.md"),
]

# ── Sentinels ──
def test_sentinels():
    print("=== Sentinel: real outputs unchanged ===")
    before = {p: _dir_sha(p) for p in REAL_PATHS}
    test_dataset()
    test_evaluator()
    test_isolation()
    test_audit_derive()
    test_api_count()
    test_smoke_summary()
    test_source_manifest()
    test_secret_scrub()
    test_parse_failclosed()
    test_protocol_validation()
    test_rollback_metadata_only()
    after = {p: _dir_sha(p) for p in REAL_PATHS}
    for p in REAL_PATHS:
        _x(before[p] == after[p], f"sentinel: {os.path.basename(p)}")

def test_dataset():
    print("=== T1: Dataset ===")
    r = subprocess.run([sys.executable, os.path.join(HERE, "validate_dataset.py")], capture_output=True, text=True)
    _x(r.returncode == 0, f"validate rc=0 (got {r.returncode})")

def test_evaluator():
    print("=== T2: Evaluator ===")
    mc = CaseMeta("c","APPROVE",[],[],[],True,False,{})
    ev = evaluate(AdapterOutput(status="completed",findings=[{"category":"other","description":"x"}],decision="APPROVE"), mc)
    _x(not ev.passed and ev.fp==1, "clean+1fp FAIL")
    ev = evaluate(AdapterOutput(status="completed",findings=[],decision="APPROVE"), mc)
    _x(ev.passed, "clean PASS")
    m3 = CaseMeta("c","HOLD",[{"category":"dependency","description":"flask CVE"},{"category":"dependency","description":"requests CVE"},{"category":"dependency","description":"pyyaml CVE"}],[],[],False,False,{})
    ev = evaluate(AdapterOutput(status="completed",findings=[{"category":"dependency","description":"flask outdated CVE"}],decision="HOLD"), m3)
    _x(not ev.passed and ev.tp==1 and ev.fn==2, f"3dep 1found tp=1 fn=2")
    ev = evaluate(AdapterOutput(status="completed",findings=[{"category":"dependency","description":"flask requests pyyaml all vulnerable"}],decision="HOLD"), m3)
    _x(ev.tp==1, f"1 finding matches 1 GT max (tp={ev.tp})")

def test_isolation():
    print("=== T3: GT isolation ===")
    fields = {f.name for f in dataclasses.fields(AdapterInput)}
    forbidden = {"risk_hint","risk_level","expected_decision","ground_truth_findings","clean_case","rollback_required","expected_fix","pass_fail_criteria","acceptable_variants","forbidden_actions"}
    _x(not (fields & forbidden), f"no GT leaked")
    _x("api_request_count" not in fields, "api_count NOT in input (output only)")

def test_audit_derive():
    print("=== T4: audit_complete derive ===")
    # Group A: review phase => True
    _x(derive_audit_complete([{"phase":"review"}], "A_single_agent", False), "A review=>True")
    _x(not derive_audit_complete([], "A_single_agent", False), "A no events=>False")
    # Group B clean: review+decision => True
    _x(derive_audit_complete([{"phase":"review"},{"phase":"decision"}], "B_mergepilot", False), "B clean review+decision=>True")
    _x(not derive_audit_complete([{"phase":"review"}], "B_mergepilot", False), "B clean missing decision=>False")
    # Group B non-clean: review+fix+decision => True
    _x(derive_audit_complete([{"phase":"review"},{"phase":"fix"},{"phase":"decision"}], "B_mergepilot", True), "B non-clean all phases=>True")
    _x(not derive_audit_complete([{"phase":"review"},{"phase":"decision"}], "B_mergepilot", True), "B non-clean missing fix=>False")

def test_api_count():
    print("=== T5: api_request_count ===")
    out = AdapterOutput(status="completed", api_request_count=3)
    _x(out.api_request_count == 3, "api_count=3 in output")
    # Verify schema has it
    s = json.load(open(os.path.join(HERE, "schemas", "run-result.schema.json"), encoding="utf-8"))
    _x("api_request_count" in s["required"], "api_count in schema required")
    _x("api_request_count" in s["properties"], "api_count in schema properties")

def test_smoke_summary():
    print("=== T6: smoke summary (temp) ===")
    with tempfile.TemporaryDirectory() as tmp:
        # Copy smoke files
        src = os.path.join(HERE, "smoke-runs")
        dst = os.path.join(tmp, "smoke-runs")
        os.makedirs(dst)
        for f in os.listdir(src):
            if f.endswith(".json"):
                shutil.copy(os.path.join(src, f), os.path.join(dst, f))
        # Copy gen script
        shutil.copy(os.path.join(HERE, "gen_smoke_summary.py"), tmp)
        r = subprocess.run([sys.executable, os.path.join(tmp, "gen_smoke_summary.py")],
                          capture_output=True, text=True, cwd=tmp)
        _x(r.returncode == 0, f"gen_smoke_summary rc=0")
        sj = os.path.join(tmp, "smoke-summary.json")
        _x(os.path.exists(sj), "smoke-summary.json generated")
        summary = json.load(open(sj, encoding="utf-8"))
        # Verify count matches actual smoke files
        smoke_count = len([f for f in os.listdir(src) if f.endswith(".json")])
        _x(summary["total_runs"] == smoke_count, f"total_runs={summary['total_runs']} matches {smoke_count}")
        # Verify all have audit_events_missing field
        for c in summary["cases"]:
            _x("audit_events_missing" in c, f"case {c['case_id']} has audit_events_missing")
        # Verify tokens are sum of individual
        sum_tokens = sum(c["total_tokens"] for c in summary["cases"])
        _x(summary["total_tokens"] == sum_tokens, f"tokens sum={summary['total_tokens']} vs {sum_tokens}")

def test_source_manifest():
    print("=== T7: source manifest ===")
    mp = os.path.join(HERE, "source-manifest.json")
    # Remove if exists from previous run
    if os.path.exists(mp):
        os.remove(mp)
    r = subprocess.run([sys.executable, os.path.join(HERE, "gen_source_manifest.py")],
                      capture_output=True, text=True)
    _x(r.returncode == 0, f"gen_source_manifest rc=0 (stderr={r.stderr[:200]})")
    _x(os.path.exists(mp), "source-manifest.json generated")
    m = json.load(open(mp, encoding="utf-8"))
    _x("git_head" in m, "manifest has git_head")
    _x("files" in m and len(m["files"]) >= 10, "manifest has files")
    _x("fixtures" in m and len(m["fixtures"]) >= 10, "manifest has >=10 fixtures")
    _x(m["model"] == "deepseek-v4-flash", "model correct")
    _x(m["token_budget"] == 4096, "token_budget=4096")
    # Recompute one file SHA to verify reproducibility
    sample_file = list(m["files"].keys())[0]
    real_sha = hashlib.sha256(open(os.path.join(ROOT, sample_file), "rb").read()).hexdigest()
    _x(m["files"][sample_file] == real_sha, f"SHA256 reproducible for {sample_file}")
    # Clean up (don't leave in real dir during tests)
    os.remove(mp)
    print("  (manifest generated + verified + cleaned)")

def test_secret_scrub():
    print("=== T8: Secret scrub ===")
    from benchmark.adapters.base import SAFE_ERRORS, safe_error
    for e in SAFE_ERRORS:
        _x("sk-" not in e and "ghp_" not in e, f"safe: {e}")
    _x("sk-" not in safe_error(401), "safe_error(401)")

def test_parse_failclosed():
    print("=== T9: Parse fail-closed ===")
    from benchmark.adapters.mergepilot import _safe_json
    _x(_safe_json("no json") is None, "non-json None")
    _x(_safe_json('{"a":1}') is not None, "valid parsed")
    _x(_safe_json('{broken') is None, "broken None")

def test_protocol_validation():
    print("=== T10: Protocol validation ===")
    from benchmark.adapters.mergepilot import _validate_decision_protocol
    _x(_validate_decision_protocol([], "APPROVE") is None, "clean+APPROVE")
    _x(_validate_decision_protocol([{"x":1}], "HOLD") is None, "findings+HOLD")
    _x(_validate_decision_protocol([{"x":1}], "REJECT") is None, "findings+REJECT")
    _x(_validate_decision_protocol([{"x":1}], "APPROVE") == "protocol_failed", "findings+APPROVE FAIL")
    _x(_validate_decision_protocol([], "HOLD") == "protocol_failed", "clean+HOLD FAIL")
    _x(_validate_decision_protocol([], "REJECT") == "protocol_failed", "clean+REJECT FAIL")

def test_rollback_metadata_only():
    print("=== T11: rollback metadata-only ===")
    meta = CaseMeta("bm-10","REJECT",
        [{"category":"data-loss","description":"rmtree destructive"},
         {"category":"data-loss","description":"irreversible drop column"},
         {"category":"secret","description":"production db hardcoded"}],[],[],False,True,{})
    out = AdapterOutput(status="completed",
        findings=[
            {"category":"data-loss","description":"shutil.rmtree destructive production"},
            {"category":"data-loss","description":"irreversible drop column migration"},
            {"category":"secret","description":"production db connection hardcoded"},
        ],
        decision="REJECT", rollback_executed=False)
    ev = evaluate(out, meta)
    _x(ev.passed, f"bm-10 PASS despite rollback=False ({ev.reason})")

def main():
    test_sentinels()
    print("\nALL V2.2 OFFLINE TESTS PASSED")

if __name__ == "__main__":
    main()
