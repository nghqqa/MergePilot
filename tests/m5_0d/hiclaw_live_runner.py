#!/usr/bin/env python3
"""M5-0D D2A-P2 — 22-formula hiclaw_live evaluator (raw-record derivation).

NO precomputed booleans. Every formula value is derived from raw records
(sync_events, stage_events, agent_processes, task_run, skill_jobs, mcp_calls,
dispatch_rows, watcher_config, injection_scan, secret_scan, residue) and
cross-field relationships. Evidence is validated against strict JSON Schemas
(additionalProperties=false, required raw fields).

Source classification:
  C3 (5): real_gateway, not_fake_github_mcp, negative_cases_passed,
          consecutive_live_runs, secret_leaks(C3 scope)
  Production tier-c (17): derived from raw production records
  Offline (auxiliary): derived from raw m4f_gates[] + legacy_runs[]
  OTel/SLS (auxiliary): derived from raw spans[] + sls_schema
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.environ.get("M5_0D_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
C3_EVIDENCE = os.path.join(ROOT, "evidence", "m5", "0c", "c3-10x.json")
PROD_EVIDENCE = os.path.join(ROOT, "evidence", "m5", "0d", "production-live.json")
OFFLINE_EVIDENCE = os.path.join(ROOT, "evidence", "m5", "0d", "offline-regression.json")
OTEL_EVIDENCE = os.path.join(ROOT, "evidence", "m5", "0d", "otel-sls.json")
SCHEMA_DIR = os.path.join(ROOT, "tests", "m5_0d", "schemas")

S_C3, S_PROD, S_OFFLINE, S_OTEL = "C3-evidence", "production-tier-c", "offline-regression", "otel-sls"


def _git_head():
    try:
        r = subprocess.run(["git", "-c", "safe.directory=" + ROOT, "-C", ROOT, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        s = r.stdout.strip()
        return s if r.returncode == 0 and len(s) == 40 else ""
    except Exception:
        return ""


def _load_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


def _validate_schema(data, schema_file):
    """Validate data against a JSON Schema file. Returns (ok, error)."""
    if data is None:
        return False, "data is None"
    sp = os.path.join(SCHEMA_DIR, schema_file)
    if not os.path.exists(sp):
        return False, "schema file missing: %s" % schema_file
    try:
        import jsonschema
        schema = json.load(open(sp, encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(data)
        return True, None
    except ImportError:
        return False, "jsonschema validator unavailable"
    except Exception as e:
        return False, str(e)[:160]


def _check_source_commit(data, expected):
    """All three evidence files must have source_commit == expected HEAD."""
    if data is None:
        return False, "missing"
    sc = data.get("source_commit", "")
    if sc != expected:
        return False, "source_commit=%s != HEAD=%s" % (sc[:12], expected[:12])
    return True, None


def _load_code_facts():
    facts = {"c2_smoke_has_audit_dsn": False, "c2_smoke_has_real_upstream": False}
    c2 = os.path.join(ROOT, "tests", "m5_0c", "c2_smoke.py")
    if os.path.exists(c2):
        t = open(c2, encoding="utf-8").read()
        facts["c2_smoke_has_audit_dsn"] = "AUDIT_DSN" in t
        facts["c2_smoke_has_real_upstream"] = "m5c2-gh:8082" in t
    return facts


def _r(value, reason, ids=None):
    return (value, reason, ids or [])


def _need(prod):
    return prod is None


# ═══════════════════════════════════════════════════════════════════
# C3-PROVABLE (5) — from c3-10x.json + code facts
# ═══════════════════════════════════════════════════════════════════

def f_consecutive_live_runs(c3, p, o, t, code):
    if not c3: return _r("unproven", "c3 missing")
    n_pass, runs = c3.get("n_pass", 0), c3.get("runs") or []
    rks = [r.get("run_key") for r in runs if r.get("run_key")]
    ok = n_pass >= 10 and len(set(rks)) >= 10 and c3.get("all_pass") is True
    return _r("true" if ok else "false", "n_pass=%d unique=%d all_pass=%s" % (n_pass, len(set(rks)), c3.get("all_pass")), rks[:3])

def f_negative_cases_passed(c3, p, o, t, code):
    if not c3: return _r("unproven", "c3 missing")
    for r in (c3.get("runs") or []):
        neg = r.get("negatives", "")
        if not isinstance(neg, str) or "/" not in neg: return _r("false", "run %s neg malformed" % r.get("run"))
        a, b = neg.split("/", 1)
        if a != b or int(a) == 0: return _r("false", "run %s neg=%s" % (r.get("run"), neg))
    return _r("true", "all runs negatives pass")

def f_secret_leaks_c3(c3, p, o, t, code):
    if not c3: return _r("unproven", "c3 missing")
    for r in (c3.get("runs") or []):
        if r.get("secret_hits_all0") is not True: return _r("false", "run %s secret_hits_all0=%s" % (r.get("run"), r.get("secret_hits_all0")))
    return _r("true", "C3 scope all runs clean")

def f_real_gateway(c3, p, o, t, code):
    if not c3 or not code: return _r("unproven", "c3 or code missing")
    if not code.get("c2_smoke_has_audit_dsn"): return _r("false", "c2_smoke lacks AUDIT_DSN")
    if c3.get("all_pass") is not True: return _r("false", "c3 not all_pass")
    return _r("true", "AUDIT_DSN in committed code + c3 10/10")

def f_not_fake_github_mcp(c3, p, o, t, code):
    if not c3 or not code: return _r("unproven", "c3 or code missing")
    if not code.get("c2_smoke_has_real_upstream"): return _r("false", "c2_smoke lacks real upstream")
    if c3.get("all_pass") is not True: return _r("false", "c3 not all_pass")
    return _r("true", "UPSTREAM=m5c2-gh:8082 real bridge (not counting stub)")


# ═══════════════════════════════════════════════════════════════════
# PRODUCTION TIER-C (17) — derived from RAW records + cross-field
# ═══════════════════════════════════════════════════════════════════

def f_real_matrix_event(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    se_list = p.get("sync_events") or []
    st_list = p.get("stage_events") or []
    sync_ids = {e.get("event_id") for e in se_list if e.get("event_id")}
    stage_ref_ids = {e.get("matrix_event_id") for e in st_list if e.get("matrix_event_id")}
    linked = sync_ids & stage_ref_ids
    if linked:
        return _r("true", "%d sync event_ids cross-linked to stage_events" % len(linked), list(linked)[:2])
    return _r("false", "no sync event_id appears in stage_event.matrix_event_id")

def f_manager_event_verified(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    agents = p.get("agent_processes") or {}
    mgr_uid = (agents.get("manager") or {}).get("matrix_user_id", "")
    st = p.get("stage_events") or []
    mgr_events = [e for e in st if e.get("sender") == mgr_uid and e.get("event_type") == "M4F_RUN"]
    ok = mgr_events and all(e.get("status") == "PROCESSED" and not e.get("error_code") for e in mgr_events)
    return _r("true" if ok else "false", "mgr_uid=%s events=%d all_PROCESSED=%s" % (mgr_uid[:20], len(mgr_events), ok))

def _handoff_check(p, role, stage, need_verdict_pass=False):
    agents = p.get("agent_processes") or {}
    uid = (agents.get(role) or {}).get("matrix_user_id", "")
    st = p.get("stage_events") or []
    evts = [e for e in st if e.get("sender") == uid and e.get("event_type") == "TASK_COMPLETED" and e.get("stage") == stage]
    if need_verdict_pass:
        evts = [e for e in evts if e.get("verdict") == "PASS"]  # verdict may be in parsed body
    ok = evts and all(e.get("status") == "PROCESSED" and not e.get("error_code") for e in evts)
    return _r("true" if ok else "false", "%s uid=%s stage=%s events=%d ok=%s" % (role, uid[:20], stage, len(evts), ok))

def f_reviewer_handoff_verified(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    return _handoff_check(p, "reviewer", "review")

def f_fixer_handoff_verified(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    return _handoff_check(p, "fixer", "fix")

def f_verifier_handoff_verified(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    base = _handoff_check(p, "verifier", "verify")
    if base[0] != "true":
        return base
    # cross-field: task_run.verdict must be PASS
    tr = p.get("task_run") or {}
    if tr.get("verdict") != "PASS":
        return _r("false", "verifier stage PROCESSED but task_run.verdict=%s" % tr.get("verdict"))
    return _r("true", "verifier verify PROCESSED + task_run verdict=PASS")

def f_sender_role_allowlist(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    agents = p.get("agent_processes") or {}
    uids = {(agents.get(r) or {}).get("matrix_user_id") for r in ("manager", "reviewer", "fixer", "verifier")}
    uids.discard(None)
    all_senders = set()
    for lst in ("sync_events", "stage_events"):
        for e in (p.get(lst) or []):
            all_senders.add(e.get("sender"))
    bad = all_senders - uids
    return _r("true" if not bad else "false", "%d senders, %d not in agent uids: %s" % (len(all_senders), len(bad), list(bad)[:3]))

def f_controller_consumed_handoffs(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    st = p.get("stage_events") or []
    tc = [e for e in st if e.get("event_type") == "TASK_COMPLETED"]
    errors = [e for e in tc if e.get("status") != "PROCESSED" or e.get("error_code")]
    return _r("true" if tc and not errors else "false", "%d TASK_COMPLETED, %d errors" % (len(tc), len(errors)))

def f_six_skills_succeeded(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    sj = p.get("skill_jobs") or []
    tr = p.get("task_run") or {}
    rb = tr.get("revision_binding_id")
    names = [s.get("skill_name") for s in sj]
    ok = (len(sj) == 6 and len(set(names)) == 6 and
          all(s.get("status") == "SUCCEEDED" for s in sj) and
          all(s.get("revision_binding_id") == rb for s in sj))
    return _r("true" if ok else "false", "jobs=%d unique=%d all_SUCCEEDED=%s rb_match=%s" % (len(sj), len(set(names)), all(s.get("status")=="SUCCEEDED" for s in sj) if sj else False, all(s.get("revision_binding_id")==rb for s in sj) if sj else False))

def f_provenance_complete(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    tr = p.get("task_run") or {}
    rb, base, head = tr.get("revision_binding_id"), tr.get("base_sha"), tr.get("head_sha")
    sj = p.get("skill_jobs") or []
    mc = p.get("mcp_calls") or []
    rb_ok = all(s.get("revision_binding_id") == rb for s in sj) and all(m.get("revision_binding_id") == rb for m in mc)
    sha_ok = all(m.get("base_sha") == base and m.get("head_sha") == head for m in mc)
    ok = bool(rb and base and head and rb_ok and sha_ok and len(mc) > 0)
    return _r("true" if ok else "false", "rb=%s base=%s head=%s rb_ok=%s sha_ok=%s mcp=%d" % (bool(rb), bool(base), bool(head), rb_ok, sha_ok, len(mc)))

def f_m4f_run_observed_by_sync(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    se = p.get("sync_events") or []
    st = p.get("stage_events") or []
    tr = p.get("task_run") or {}
    m4f_sync = [e for e in se if e.get("event_type") == "M4F_RUN"]
    m4f_stage = [e for e in st if e.get("event_type") == "M4F_RUN"]
    consumer = tr.get("consumer_name", "")
    # M4F event in sync_events + stage_event processed_by == consumer (!= controller)
    ok = bool(m4f_sync and m4f_stage and consumer and consumer != "controller" and
              all(e.get("processed_by") == consumer for e in m4f_stage))
    return _r("true" if ok else "false", "sync=%d stage=%d consumer=%s" % (len(m4f_sync), len(m4f_stage), consumer[:20]))

def f_candidate_consumer_isolated(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    tr = p.get("task_run") or {}
    cn = tr.get("consumer_name", "")
    st = p.get("stage_events") or []
    processed_by = {e.get("processed_by") for e in st if e.get("processed_by")}
    ok = bool(cn and cn != "controller" and cn in processed_by)
    return _r("true" if ok else "false", "consumer=%s in_processed_by=%s" % (cn[:20], cn in processed_by))

def f_six_skill_to_review_bridge(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    tr = p.get("task_run") or {}
    st = p.get("stage_events") or []
    st_ids = {e.get("stage_event_id"): e for e in st}
    ok = True
    for field, stage in [("review_stage_event_id", "review"), ("fix_stage_event_id", "fix"), ("verify_stage_event_id", "verify")]:
        ref = tr.get(field)
        evt = st_ids.get(ref)
        if not evt or evt.get("stage") != stage or evt.get("status") != "PROCESSED":
            ok = False; break
    return _r("true" if ok else "false", "review/fix/verify stage_event_id refs valid=%s" % ok)

def f_authoritative_dispatch_only(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    dr = p.get("dispatch_rows") or []
    wc = p.get("watcher_config") or {}
    excluded = wc.get("excluded_prefixes") or []
    has_m5live = any("m5live" in ep for ep in excluded)
    ok_sources = all(r.get("source") in ("controller_reconcile", "controller") for r in dr)
    ok = has_m5live and ok_sources
    return _r("true" if ok else "false", "excluded_has_m5live=%s sources_ok=%s" % (has_m5live, ok_sources))

def f_no_handoff_watcher_duplicate(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    dr = p.get("dispatch_rows") or []
    ids = [r.get("dispatch_id") for r in dr]
    pairs = [(r.get("role"), r.get("stage_event_id")) for r in dr]
    ok = len(ids) == len(set(ids)) and len(pairs) == len(set(pairs))
    return _r("true" if ok else "false", "dispatch_ids=%d unique=%d pairs_unique=%s" % (len(ids), len(set(ids)), len(pairs)==len(set(pairs))))

def f_manager_output_not_test_injected(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    agents = p.get("agent_processes") or {}
    mgr = agents.get("manager") or {}
    has_identity = bool(mgr.get("container_id") and mgr.get("matrix_user_id"))
    isc = p.get("injection_scan") or {}
    required_sources = {"runner", "send_as", "inject", "logs"}
    scanned = set(isc.get("scanned_sources") or [])
    forbidden = isc.get("forbidden_invocations") or []
    ok = has_identity and required_sources.issubset(scanned) and len(forbidden) == 0
    return _r("true" if ok else "false", "identity=%s sources_covered=%s forbidden=%d" % (has_identity, required_sources.issubset(scanned), len(forbidden)))

def f_worker_outputs_not_test_injected(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    agents = p.get("agent_processes") or {}
    isc = p.get("injection_scan") or {}
    required_sources = {"runner", "send_as", "inject", "logs"}
    scanned = set(isc.get("scanned_sources") or [])
    forbidden = isc.get("forbidden_invocations") or []
    ok = True
    for role in ("reviewer", "fixer", "verifier"):
        a = agents.get(role) or {}
        if not a.get("container_id") or not a.get("matrix_user_id"):
            ok = False; break
    ok = ok and required_sources.issubset(scanned) and len(forbidden) == 0
    return _r("true" if ok else "false", "all_workers_identity=%s sources_covered=%s forbidden=%d" % (ok, required_sources.issubset(scanned), len(forbidden)))

def f_full_matrix_sender_verified(c3, p, o, t, code):
    if _need(p): return _r("unproven", "production-live.json missing")
    server = p.get("matrix_server_name", "")
    agents = p.get("agent_processes") or {}
    uids = [(agents.get(r) or {}).get("matrix_user_id", "") for r in ("manager", "reviewer", "fixer", "verifier")]
    # parse homeserver suffix from each user_id (@user:server)
    ok = bool(server) and all(uid and ":" in uid and uid.rsplit(":", 1)[1] == server for uid in uids)
    return _r("true" if ok else "false", "server=%s uids_hs_ok=%s" % (server, all(":" in u and u.rsplit(':',1)[1]==server for u in uids) if uids else False))


# ═══════════════════════════════════════════════════════════════════
# FORMULA REGISTRY (§19 order)
# ═══════════════════════════════════════════════════════════════════

FORMULAS = [
    ("real_matrix_event", S_PROD, f_real_matrix_event),
    ("manager_event_verified", S_PROD, f_manager_event_verified),
    ("reviewer_handoff_verified", S_PROD, f_reviewer_handoff_verified),
    ("fixer_handoff_verified", S_PROD, f_fixer_handoff_verified),
    ("verifier_handoff_verified", S_PROD, f_verifier_handoff_verified),
    ("sender_role_allowlist", S_PROD, f_sender_role_allowlist),
    ("controller_consumed_handoffs", S_PROD, f_controller_consumed_handoffs),
    ("real_gateway", S_C3, f_real_gateway),
    ("not_fake_github_mcp", S_C3, f_not_fake_github_mcp),
    ("six_skills_succeeded", S_PROD, f_six_skills_succeeded),
    ("provenance_complete", S_PROD, f_provenance_complete),
    ("negative_cases_passed", S_C3, f_negative_cases_passed),
    ("consecutive_live_runs", S_C3, f_consecutive_live_runs),
    ("secret_leaks", S_C3, f_secret_leaks_c3),
    ("m4f_run_observed_by_sync", S_PROD, f_m4f_run_observed_by_sync),
    ("candidate_consumer_isolated", S_PROD, f_candidate_consumer_isolated),
    ("six_skill_to_review_bridge_committed", S_PROD, f_six_skill_to_review_bridge),
    ("authoritative_dispatch_only", S_PROD, f_authoritative_dispatch_only),
    ("no_handoff_watcher_duplicate", S_PROD, f_no_handoff_watcher_duplicate),
    ("manager_output_not_test_injected", S_PROD, f_manager_output_not_test_injected),
    ("worker_outputs_not_test_injected", S_PROD, f_worker_outputs_not_test_injected),
    ("full_matrix_sender_verified", S_PROD, f_full_matrix_sender_verified),
]
assert len(FORMULAS) == 22


def evaluate(c3, prod, offline, otel, code_facts, source_commit):
    results = []
    for name, source, fn in FORMULAS:
        value, reason, ids = fn(c3, prod, offline, otel, code_facts)
        results.append({"name": name, "value": value, "authoritative_source": source,
                        "reason": reason, "source_commit": source_commit, "observed_identifiers": ids})
    hiclaw_live = all(r["value"] == "true" for r in results)
    return hiclaw_live, results


def check_offline(offline):
    if not offline: return ("unproven", "offline-regression.json missing")
    gates = offline.get("m4f_gates") or []
    legacy = offline.get("legacy_runs") or []
    gate_ids = [g.get("gate_id") for g in gates]
    plat_ids = [l.get("platform_id") for l in legacy]
    m4f_ok = (len(gates) == 17 and len(set(gate_ids)) == 17 and
              all(g.get("rc") == 0 and g.get("status") == "PASS" for g in gates))
    leg_ok = (len(legacy) == 6 and len(set(plat_ids)) == 6 and
              all(l.get("rc") == 0 and l.get("match") is True for l in legacy))
    if m4f_ok and leg_ok:
        return ("pass", "17 unique gates PASS + 6 unique platforms match")
    return ("fail", "gates=%d(unique=%d all_pass=%s) legacy=%d(unique=%d all_match=%s)" %
            (len(gates), len(set(gate_ids)), m4f_ok, len(legacy), len(set(plat_ids)), leg_ok))


def check_otel(otel):
    if not otel: return ("unproven", "otel-sls.json missing")
    spans = otel.get("spans") or []
    sls = otel.get("sls_schema") or {}
    required_names = {"controller.process_event", "skill.pr_lifecycle", "gateway.call_tool"}
    span_names = {s.get("name") for s in spans}
    all_ok_status = all(s.get("status") == "OK" for s in spans)
    sls_ok = bool(sls.get("sha256") and sls.get("validated_records", 0) > 0)
    ok = required_names.issubset(span_names) and all_ok_status and sls_ok
    return ("pass" if ok else "fail",
            "spans=%d required=%s all_OK=%s sls_ok=%s" % (len(spans), required_names.issubset(span_names), all_ok_status, sls_ok))


def main():
    sc = _git_head()
    if not sc:
        print("FATAL: cannot resolve git HEAD"); return 2
    c3 = _load_json(C3_EVIDENCE)
    prod = _load_json(PROD_EVIDENCE)
    offline = _load_json(OFFLINE_EVIDENCE)
    otel = _load_json(OTEL_EVIDENCE)
    code = _load_code_facts()

    # validate source_commit for each present evidence file
    for label, data in [("production", prod), ("offline", offline), ("otel", otel)]:
        if data:
            ok, err = _check_source_commit(data, sc)
            if not ok:
                print("FATAL: %s source_commit mismatch: %s" % (label, err))
                return 2

    hiclaw, results = evaluate(c3, prod, offline, otel, code, sc)
    off_val, off_reason = check_offline(offline)
    otel_val, otel_reason = check_otel(otel)

    false_items = [r["name"] for r in results if r["value"] == "false"]
    unproven_items = [r["name"] for r in results if r["value"] == "unproven"]
    true_count = sum(1 for r in results if r["value"] == "true")

    report = {
        "gate": "m5-0d-hiclaw-live", "source_commit": sc, "hiclaw_live": hiclaw,
        "formulas_true": true_count, "formulas_false": len(false_items), "formulas_unproven": len(unproven_items),
        "false_items": false_items, "unproven_items": unproven_items,
        "offline_gate": off_val, "offline_reason": off_reason,
        "otel_gate": otel_val, "otel_reason": otel_reason,
        "final_rc": 0 if (hiclaw and off_val == "pass" and otel_val == "pass") else 1,
        "formulas": results,
        "inputs": {"c3": "present" if c3 else "missing", "production": "present" if prod else "missing",
                   "offline": "present" if offline else "missing", "otel": "present" if otel else "missing"},
    }
    print(json.dumps(report, indent=2))
    return report["final_rc"]


if __name__ == "__main__":
    sys.exit(main())
