#!/usr/bin/env python3
"""D2A-P2 unit tests: raw-record derivation + cross-field rejection paths.
Pure (no WSL, no network). Each case builds synthetic raw-record evidence."""
from __future__ import annotations
import builtins, copy, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hiclaw_live_runner as H

HEAD = "e" * 40
CODE = {"c2_smoke_has_audit_dsn": True, "c2_smoke_has_real_upstream": True}
HS = "matrix-local.hiclaw.io"


def _c3():
    return {"gate":"m5-0c-c3","n_runs":10,"n_pass":10,"all_pass":True,"final_rc":0,"state_stable":True,
            "docker_state_pre":{},"docker_state_post":{},
            "runs":[{"run":i,"run_key":"rk%02d"%i,"final_rc":0,"c2_exit_rc":0,"positives":"22/22","negatives":"15/15",
                     "secret_hits_all0":True} for i in range(1,11)]}


H64 = "f" * 64


def _fix_provenance_digests(base):
    """Recompute the 3 recomputable digests so CP.validate_production passes on
    the clean fixture (raw_capture_sha256, collector_command_digest,
    collector_script_sha256). Trust-boundary digests stay as H64 placeholders."""
    import hashlib
    import capture_production_live as CP
    prov = base["provenance"]
    cw = prov["capture_window"]
    command = ["capture_production_live.py", "--run-id", prov["run_key"],
               "--room-id", base["task_run"]["room_id"],
               "--window-start", cw["started_at"], "--window-end", cw["ended_at"]]
    raw_no_prov = {k: v for k, v in base.items() if k != "provenance"}
    with open(os.path.join(HERE, "capture_production_live.py"), "rb") as stream:
        prov["collector_script_sha256"] = hashlib.sha256(stream.read()).hexdigest()
    prov["collector_command_digest"] = hashlib.sha256(CP.canonical_bytes(command)).hexdigest()
    prov["raw_capture_sha256"] = hashlib.sha256(CP.canonical_bytes(raw_no_prov)).hexdigest()


def _prod():
    """Complete raw-record production fixture with provenance — all 17 formulas derive true.
    Consumer is the real Candidate value 'm5-0-candidate'; provenance digests are
    internally consistent so CP.validate_production also passes on the clean fixture."""
    base = {
        "schema_version":"1","source_commit":HEAD,"matrix_server_name":HS,
        "sync_events":[
            {"sync_batch_id":H64,"event_id":"$m4f_evt","room_id":"!room","sender":"@manager:"+HS,
             "event_type":"M4F_RUN","body_sha256":"a"*64,"received_at":"t1","consumer_name":"m5-0-candidate"}],
        "stage_events":[
            {"stage_event_id":"se-mgr","matrix_event_id":"$m4f_evt","room_id":"!room","sender":"@manager:"+HS,
             "event_type":"M4F_RUN","stage":"m4f_snapshot","status":"PROCESSED","parsed_run_id":"m5live-run-1","processed_by":"m5-0-candidate","error_code":""},
            {"stage_event_id":"se-rev","matrix_event_id":"$rev_evt","room_id":"!room","sender":"@reviewer:"+HS,
             "event_type":"TASK_COMPLETED","stage":"review","status":"PROCESSED","parsed_run_id":"m5live-run-1","processed_by":"m5-0-candidate","error_code":""},
            {"stage_event_id":"se-fix","matrix_event_id":"$fix_evt","room_id":"!room","sender":"@fixer:"+HS,
             "event_type":"TASK_COMPLETED","stage":"fix","status":"PROCESSED","parsed_run_id":"m5live-run-1","processed_by":"m5-0-candidate","error_code":""},
            {"stage_event_id":"se-ver","matrix_event_id":"$ver_evt","room_id":"!room","sender":"@verifier:"+HS,
             "event_type":"TASK_COMPLETED","stage":"verify","status":"PROCESSED","parsed_run_id":"m5live-run-1","processed_by":"m5-0-candidate","error_code":""}],
        "agent_processes":{
            "manager":{"role":"manager","container_id":"cid-mgr","image_id":"img-mgr","matrix_user_id":"@manager:"+HS,"started_at":"t0","command_digest":H64,"log_digest":H64},
            "reviewer":{"role":"reviewer","container_id":"cid-rev","image_id":"img-wrk","matrix_user_id":"@reviewer:"+HS,"started_at":"t0","command_digest":H64,"log_digest":H64},
            "fixer":{"role":"fixer","container_id":"cid-fix","image_id":"img-wrk","matrix_user_id":"@fixer:"+HS,"started_at":"t0","command_digest":H64,"log_digest":H64},
            "verifier":{"role":"verifier","container_id":"cid-ver","image_id":"img-wrk","matrix_user_id":"@verifier:"+HS,"started_at":"t0","command_digest":H64,"log_digest":H64}},
        "task_run":{"run_id":"m5live-run-1","room_id":"!room","status":"HOLD","current_stage":"m4f_await_review","verdict":"PASS",
                    "consumer_name":"m5-0-candidate","revision_binding_id":"bnd-1","base_sha":"a"*40,"head_sha":"b"*40,
                    "review_stage_event_id":"se-rev","fix_stage_event_id":"se-fix","verify_stage_event_id":"se-ver"},
        "skill_jobs":[{"skill_name":"diff-parse","job_id":"j1","invocation_id":"i1","status":"SUCCEEDED","revision_binding_id":"bnd-1","output_schema_validated":True},
                      {"skill_name":"risk-classify","job_id":"j2","invocation_id":"i2","status":"SUCCEEDED","revision_binding_id":"bnd-1","output_schema_validated":True},
                      {"skill_name":"sast-scan","job_id":"j3","invocation_id":"i3","status":"SUCCEEDED","revision_binding_id":"bnd-1","output_schema_validated":True},
                      {"skill_name":"test-runner","job_id":"j4","invocation_id":"i4","status":"SUCCEEDED","revision_binding_id":"bnd-1","output_schema_validated":True},
                      {"skill_name":"case-retrieval","job_id":"j5","invocation_id":"i5","status":"SUCCEEDED","revision_binding_id":"bnd-1","output_schema_validated":True},
                      {"skill_name":"pr-lifecycle","job_id":"j6","invocation_id":"i6","status":"SUCCEEDED","revision_binding_id":"bnd-1","output_schema_validated":True}],
        "mcp_calls":[{"call_id":"c1","caller_agent":"fixer","tool":"create_branch","decision":"ALLOW","revision_binding_id":"bnd-1","base_sha":"a"*40,"head_sha":"b"*40,"upstream_kind":"github-mcp","audit_dsn_kind":"postgresql"}],
        "dispatch_rows":[{"dispatch_id":"d1","role":"reviewer","stage_event_id":"se-rev","source":"controller_reconcile"}],
        "watcher_config":{"excluded_prefixes":["m5live-"]},
        "injection_scan":{"scanned_sources":["runner","send_as","inject","logs"],"forbidden_invocations":[]},
        "secret_scan":{"scanned_targets":["cand","gw","gh","hs","pg"],"matches":[]},
        "residue":{"containers":[],"networks":[],"volumes":[],"temp_dirs":[],"open_prs":[],"branches":[]},
        "provenance":{
            "collector_kind":"deploy-owned-production-tier-c",
            "collector_script_sha256":H64,"collector_command_digest":H64,
            "run_key":"m5live-run-1","capture_window":{"started_at":"2026-01-01T00:00:00Z","ended_at":"2026-01-01T01:00:00Z"},
            "collected_at":"2026-01-01T01:01:00Z","raw_capture_sha256":H64,
            "db_snapshot_sha256":H64,"matrix_sync_sha256":H64,
            "container_snapshot_sha256":H64,"gateway_audit_sha256":H64,
            "github_mcp_log_sha256":H64,
            "matrix_event_provenance":{"sync_event_ids":["$m4f_evt"],"stage_event_ids":sorted(["$m4f_evt","$rev_evt","$fix_evt","$ver_evt"])},
        },
    }
    _fix_provenance_digests(base)
    return base


def _offline():
    return {"schema_version":"1","source_commit":HEAD,
            "m4f_gates":[{"gate_id":"g%02d"%i,"rc":0,"status":"PASS","output_sha256":"f"*64} for i in range(1,18)],
            "legacy_runs":[{"platform_id":"p%d"%i,"rc":0,"match":True,"expected_count":10,"actual_count":10,"output_sha256":"f"*64} for i in range(1,7)]}


def _fix_otel_digests(prov):
    """Recompute the 2 recomputable OTel digests so CT.validate_otel passes on
    the clean fixture (collector_command_digest, collector_script_sha256).
    raw_capture_sha256 + collector_endpoint_digest stay H64 (trust-boundary)."""
    import hashlib
    import capture_otel_sls as CT
    cw = prov["capture_window"]
    command = ["capture_otel_sls.py", "--run-id", prov["trace_run_binding"]["expected_run_id"],
               "--window-start", cw["started_at"], "--window-end", cw["ended_at"]]
    with open(os.path.join(HERE, "capture_otel_sls.py"), "rb") as stream:
        prov["collector_script_sha256"] = hashlib.sha256(stream.read()).hexdigest()
    prov["collector_command_digest"] = hashlib.sha256(CT.canonical_bytes(command)).hexdigest()


def _otel():
    rec = {"schema_version":"1","source_commit":HEAD,
            "provenance":{
                "collector_kind":"deploy-owned-otel-sls",
                "collector_script_sha256":H64,"collector_command_digest":H64,
                "collector_endpoint_digest":H64,
                "capture_window":{"started_at":"2026-01-01T00:00:00Z","ended_at":"2026-01-01T01:00:00Z"},
                "captured_at":"2026-01-01T01:01:00Z","raw_capture_sha256":H64,
                "trace_run_binding":{"expected_run_id":"m5live-run-1","observed_run_ids":["m5live-run-1"]},
            },
            "spans":[{"trace_id":"t1","span_id":"s1","name":"controller.process_event","status":"OK","run_id":"m5live-run-1","attributes":{}},
                     {"trace_id":"t2","span_id":"s2","name":"skill.pr_lifecycle","status":"OK","run_id":"m5live-run-1","attributes":{}},
                     {"trace_id":"t3","span_id":"s3","name":"gateway.call_tool","status":"OK","run_id":"m5live-run-1","attributes":{}}],
            "sls_schema":{"name":"mergepilot-sls","version":"1","sha256":"f"*64,"validated_records":100}}
    _fix_otel_digests(rec["provenance"])
    return rec


def _e(c3=_c3(), p=_prod(), o=_offline(), t=_otel()):
    return H.evaluate(c3, p, o, t, CODE, HEAD)

def _x(cond, msg):
    if not cond: raise AssertionError("FAIL: "+msg)
    print("  PASS:", msg)

def _f(res, name): return next(r for r in res if r["name"]==name)


# ── C3 + complete ──

def test_c3_only_fail_closed():
    hl,res=_e(p=None,o=None,t=None)
    _x(hl is False,"C3-only hiclaw_live=false")
    _x(sum(1 for r in res if r["value"]=="true")==5,"5 C3 true")
    _x(sum(1 for r in res if r["value"]=="unproven")==17,"17 production unproven")

def test_complete_22_true():
    hl,res=_e()
    _x(hl is True,"complete raw fixture hiclaw_live=true")
    bad=[r["name"] for r in res if r["value"]!="true"]
    _x(len(bad)==0,"0 false/unproven (got %s)"%bad)

def test_no_hardcoded_true():
    hl,res=_e(p=None)
    _x(hl is False,"no prod → false (not hardcoded)")

# ── cross-field rejection ──

def test_sender_mismatch():
    p=_prod(); p["agent_processes"]["manager"]["matrix_user_id"]="@wrong:"+HS
    hl,res=_e(p=p)
    _x(_f(res,"manager_event_verified")["value"]=="false","mgr uid mismatch → false")

def test_sync_stage_event_id_mismatch():
    p=_prod(); p["sync_events"][0]["event_id"]="$different"
    hl,res=_e(p=p)
    _x(_f(res,"real_matrix_event")["value"]=="false","sync event_id != stage matrix_event_id → false")

def test_wrong_stage_event_ref():
    p=_prod(); p["task_run"]["review_stage_event_id"]="se-fix"  # wrong ref
    hl,res=_e(p=p)
    _x(_f(res,"six_skill_to_review_bridge_committed")["value"]=="false","wrong stage_event_id ref → false")

def test_provenance_inconsistent():
    p=_prod(); p["skill_jobs"][0]["revision_binding_id"]="wrong-bnd"
    hl,res=_e(p=p)
    _x(_f(res,"provenance_complete")["value"]=="false","skill rb mismatch → false")

def test_duplicate_dispatch():
    p=_prod(); p["dispatch_rows"].append({"dispatch_id":"d1","role":"reviewer","stage_event_id":"se-rev","source":"controller_reconcile"})
    hl,res=_e(p=p)
    _x(_f(res,"no_handoff_watcher_duplicate")["value"]=="false","duplicate dispatch_id → false")

def test_watcher_missing_m5live():
    p=_prod(); p["watcher_config"]["excluded_prefixes"]=[]
    hl,res=_e(p=p)
    _x(_f(res,"authoritative_dispatch_only")["value"]=="false","no m5live exclusion → false")

def test_injection_scan_incomplete():
    p=_prod(); p["injection_scan"]["scanned_sources"]=["runner","logs"]  # missing send_as, inject
    hl,res=_e(p=p)
    _x(_f(res,"manager_output_not_test_injected")["value"]=="false","injection_scan incomplete → false")

def test_forbidden_invocations():
    p=_prod(); p["injection_scan"]["forbidden_invocations"]=["send_as_override"]
    hl,res=_e(p=p)
    _x(_f(res,"worker_outputs_not_test_injected")["value"]=="false","forbidden_invocations non-empty → false")

def test_homeserver_mismatch():
    p=_prod(); p["matrix_server_name"]="wrong.hiclaw.io"
    hl,res=_e(p=p)
    _x(_f(res,"full_matrix_sender_verified")["value"]=="false","HS mismatch → false")

def test_9_runs():
    hl,res=_e(c3={"n_pass":9,"all_pass":False,"runs":[{"run_key":"rk%d"%i,"negatives":"15/15","secret_hits_all0":True} for i in range(1,10)]})
    _x(_f(res,"consecutive_live_runs")["value"]=="false","n_pass=9 → false")

# ── offline ──

def test_offline_16_17():
    o=_offline(); o["m4f_gates"]=o["m4f_gates"][:16]  # 16 gates
    val,_=H.check_offline(o)
    _x(val=="fail","16/17 gates → fail")

def test_offline_dup_gate():
    o=_offline(); o["m4f_gates"][1]["gate_id"]=o["m4f_gates"][0]["gate_id"]
    val,_=H.check_offline(o)
    _x(val=="fail","duplicate gate_id → fail")

# ── otel ──

def test_otel_missing_span():
    t=_otel(); t["spans"]=[s for s in t["spans"] if s["name"]!="gateway.call_tool"]
    val,_=H.check_otel(t)
    _x(val=="fail","missing required span → fail")

# ── source_commit ──

def test_source_commit_mismatch():
    ok,err=H._check_source_commit({"source_commit":"f"*40},HEAD)
    _x(ok is False,"source_commit mismatch → reject")

# ── schema rejection ──

def test_schema_additional_property():
    ok,err=H._validate_schema({"schema_version":"1","source_commit":HEAD,"extra_field":"bad"},"otel-sls.schema.json")
    _x(ok is False,"additional property → schema reject")

def test_schema_validator_unavailable():
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("blocked for fail-closed test")
        return original_import(name, *args, **kwargs)

    builtins.__import__ = blocked_import
    try:
        ok, err = H._validate_schema(_otel(), "otel-sls.schema.json")
    finally:
        builtins.__import__ = original_import
    _x(ok is False and "unavailable" in err, "missing jsonschema validator -> fail closed")


def test_delete_input():
    hl,res=_e(p=None)
    _x(hl is False,"no prod → false")
    _x(sum(1 for r in res if r["value"]=="unproven")==17,"17 unproven")


def test_run_id_mismatch_fails_capture():
    """Unifying run_id: changing task_run.run_id or provenance.run_key to a
    different value must fail capture validation (validate_production)."""
    import capture_production_live as CP
    p = _prod()
    # Consistent fixture passes
    ok, errs = CP.validate_production(p, HEAD)
    _x(ok, "consistent fixture passes validate_production")
    # Change task_run.run_id but not provenance.run_key → mismatch
    p2 = _prod(); p2["task_run"]["run_id"] = "m5live-different"
    ok2, errs2 = CP.validate_production(p2, HEAD)
    _x(not ok2 and any("run_key" in e for e in errs2), "task_run.run_id mismatch → fail")
    # OTel: span run_id must match provenance.trace_run_binding; mismatch caught
    import capture_otel_sls as CT
    t = _otel()
    ok3, errs3 = CT.validate_otel(t["spans"], t["sls_schema"], t["provenance"])
    _x(ok3, "valid otel spans+provenance pass validate_otel (errs=%s)" % errs3)
    t["spans"][0]["run_id"] = "m5live-other"  # diverge from expected_run_id
    ok4, errs4 = CT.validate_otel(t["spans"], t["sls_schema"], t["provenance"])
    _x(not ok4 and any("run binding" in e for e in errs4), "span run_id mismatch → fail")


def main():
    for n,fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            print("=== %s ==="%n); fn()
    print("\nALL UNIT TESTS PASSED")

if __name__=="__main__": main()
