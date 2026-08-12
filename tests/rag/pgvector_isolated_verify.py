#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-RAG · Final Skill CLI + timeout + evidence verification.

Runs the full pipeline: real Skill CLI with pgvector, strict timeout
via CaseRetrievalBridge, real residue, secret scan.
"""
from __future__ import annotations

import json, os, shutil, subprocess, sys, threading, time, hashlib, re

from jsonschema import Draft202012Validator

REPO = "/mnt/d/goai/mergepilot-os"
for p in [REPO, os.path.join(REPO, "tools", "rag"), os.path.join(REPO, "tools", "otel"),
          os.path.join(REPO, "skills"), os.path.join(REPO, "skills", "common"),
          os.path.join(REPO, "skills", "common", "runtime")]:
    if p not in sys.path: sys.path.insert(0, p)

import psycopg2

ADMIN_DSN = "postgresql://postgres:testpass@127.0.0.1:15432/ragtest"
READER_DSN = "postgresql://ragreader:testpass@127.0.0.1:15432/ragtest"

def make_emb(text, dim=384):
    h = hashlib.sha256(text.encode()).digest()
    return [(h[i%32]/255.0-0.5)*2 for i in range(dim)]

def measure_residue():
    conn = psycopg2.connect(ADMIN_DSN); conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()"); pid = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname='ragtest' AND state='active' AND pid != %s", (pid,)); active = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname='ragtest' AND state='idle' AND pid != %s", (pid,)); idle = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname='ragtest' AND pid != %s", (pid,)); total = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname='ragtest' AND state IN ('idle in transaction','active') AND xact_start IS NOT NULL AND pid != %s", (pid,)); txn = cur.fetchone()[0]
    conn.close()
    return active, idle, total, txn

SECRET_PATS = [r"sk-[A-Za-z0-9]{16,}", r"ghp_[0-9A-Za-z]{20,}", r"AKIA[0-9A-Z]{12,}", r"testpass", r"xox[baprs]-[A-Za-z0-9-]{10,}"]
def scan_secrets(text):
    return sum(len(re.findall(p, text)) for p in SECRET_PATS)


def schema_valid(value, relative_path):
    with open(os.path.join(REPO, relative_path), encoding="utf-8") as fh:
        validator = Draft202012Validator(json.load(fh))
    return not list(validator.iter_errors(value))


def main():
    results = []; measured = {}
    def check(n, c, d=""):
        results.append((n, bool(c), d)); print(("  PASS " if c else "  FAIL ")+n+("  "+d if d and not c else ""))

    print("=== M6-RAG FINAL VERIFICATION ===")

    # Setup data
    conn = psycopg2.connect(ADMIN_DSN); conn.autocommit = True
    vs = lambda v: "["+",".join(str(x) for x in v)+"]"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM knowledge WHERE task_id LIKE 'rag-f-%'")
        for cid, scope, cat, sev, issue, fix, url, emb in [
            ("rag-f-001","repo-A","sql_injection","high","SQL injection","Use parameterized queries","https://github.com/t/r-A/pull/1",make_emb("sql injection")),
            ("rag-f-002","repo-A","hardcoded_secret","critical","Hardcoded API key","Move to env var","https://github.com/t/r-A/pull/2",make_emb("hardcoded api key secret")),
            ("rag-f-003","repo-B","xss","medium","XSS","HTML encode","https://github.com/t/r-B/pull/3",make_emb("xss")),
        ]:
            cur.execute("INSERT INTO knowledge (task_id,repo_scope,category,severity,issue,fix,source_pr_url,source_version,embedding_model,embedding_version,embedding) VALUES (%s,%s,%s,%s,%s,%s,%s,'1.0.0','BAAI/bge-small-en-v1.5','1.0.0',%s::vector)",(cid,scope,cat,sev,issue,fix,url,vs(emb)))
    conn.close()
    check("test data inserted", True)
    baseline_threads = threading.active_count() - 1

    os.environ["MERGEPILOT_CR_PG_DSN"] = READER_DSN
    os.environ["MERGEPILOT_CR_REPO_SCOPE"] = "repo-A"
    os.environ["MERGEPILOT_CR_DB_SCHEMA"] = "public"
    os.environ["MERGEPILOT_CR_DB_TABLE"] = "knowledge"
    os.environ["MERGEPILOT_CR_EMBEDDING_MODEL"] = "BAAI/bge-small-en-v1.5"
    os.environ["MERGEPILOT_CR_EMBEDDING_VERSION"] = "1.0.0"
    os.environ["MERGEPILOT_RUN_ID"] = "rag-final-001"

    from rag_retrieval_service import CaseRetrievalBridge, query_for_reviewer, query_for_fixer
    # Match CaseRetrievalBridge's canonical import path to avoid loading the
    # same exception class under both case_retrieval.* and skills.case_retrieval.*.
    from case_retrieval.core import CaseRetrievalError, TIMEOUT_SUB
    from case_retrieval.adapters.pg_vector import PgVectorAdapter

    # Bridge queries
    print("\n=== BRIDGE QUERIES ===")
    bridge = CaseRetrievalBridge(timeout_ms=120000)
    try:
        raw = bridge.retrieve("sql injection", top_k=5)
        check("reviewer hit", len(raw)>0); measured["reviewer_hit_count"]=len(raw)
        if raw:
            check("reviewer citation", "github.com" in str(raw[0].get("source_pr_url","")))
    except Exception as e: check("reviewer", False, str(e))
    try:
        raw = bridge.retrieve("hardcoded api key", top_k=5)
        check("fixer hit", len(raw)>0); measured["fixer_hit_count"]=len(raw)
    except Exception as e: check("fixer", False, str(e))
    try:
        raw = bridge.retrieve("zzzz_nonexistent", top_k=5, min_score=0.99)
        check("empty result", len(raw)==0)
    except Exception as e: check("empty", False, str(e))
    try:
        raw = bridge.retrieve("xss reflected", top_k=5)
        check("repo_scope isolation", all("rag-f-003" not in str(r.get("case_id","")) for r in raw))
        measured["repo_scope_isolation"] = True
    except Exception as e: check("repo_scope", False, str(e))

    # TIMEOUT via Bridge
    print("\n=== TIMEOUT VIA BRIDGE ===")
    os.environ["MERGEPILOT_CR_STATEMENT_TIMEOUT_MS"] = "1"
    tb = CaseRetrievalBridge(timeout_ms=5000)
    ts=""; tr=""; sqlstate=""; tw=0
    original_map_db_error = PgVectorAdapter._map_db_error

    def observe_map_db_error(adapter, exc, default="database operation failed"):
        nonlocal sqlstate
        observed = str(getattr(exc, "pgcode", None) or "")
        if observed:
            sqlstate = observed
        return original_map_db_error(adapter, exc, default)

    PgVectorAdapter._map_db_error = observe_map_db_error
    try:
        start = time.monotonic()
        try:
            tb.retrieve("sql injection", top_k=5)
            check("bridge timeout raised", False, "query unexpectedly completed")
        except CaseRetrievalError as exc:
            check("bridge timeout subcode", exc.subcode == TIMEOUT_SUB,
                  "got %s" % exc.subcode)
            check("timeout SQLSTATE=57014", sqlstate == "57014",
                  "got %s" % sqlstate)

        resp = query_for_reviewer(
            "sql injection", "r", "t",
            adapter=CaseRetrievalBridge(timeout_ms=5000), timeout_ms=5000)
        tw = round((time.monotonic()-start)*1000,1)
        ts=resp.status; tr=resp.fallback_reason
        check("timeout status=retrieval_unavailable", ts=="retrieval_unavailable", "got %s"%ts)
        check("timeout reason=timeout", tr=="timeout", "got %s"%tr)
        check("timeout bounded <10000ms", tw<10000, "wall=%sms"%tw)
        measured["timeout_status"]=ts; measured["timeout_reason"]=tr
        measured["timeout_sqlstate"]=sqlstate; measured["timeout_wall_ms"]=tw
    except Exception as e: check("timeout test", False, str(e))
    finally:
        PgVectorAdapter._map_db_error = original_map_db_error
        del os.environ["MERGEPILOT_CR_STATEMENT_TIMEOUT_MS"]

    # POST-TIMEOUT recovery
    print("\n=== POST-TIMEOUT RECOVERY ===")
    try:
        b3 = CaseRetrievalBridge(timeout_ms=120000)
        raw = b3.retrieve("sql injection", top_k=3)
        check("post-timeout success", len(raw)>0); measured["post_timeout_success"]=True
    except Exception as e: check("post-timeout", False, str(e))

    # REAL SKILL CLI
    print("\n=== REAL SKILL CLI (sast_scan) ===")
    cli_env = dict(os.environ)
    cli_env["MERGEPILOT_SAST_WORKSPACE"] = "/tmp/rag-sast-ws"
    os.makedirs("/tmp/rag-sast-ws/src", exist_ok=True)
    with open("/tmp/rag-sast-ws/src/db.py","w") as f: f.write("import os\nx=1\n")
    # Ensure no leftover statement_timeout from the timeout test
    cli_env.pop("MERGEPILOT_CR_STATEMENT_TIMEOUT_MS", None)
    # Also insert a case matching what the sast_scan CLI will query
    # (the CLI builds query from filenames like "db.py")
    conn_extra = psycopg2.connect(ADMIN_DSN); conn_extra.autocommit = True
    with conn_extra.cursor() as cur:
        cur.execute("INSERT INTO knowledge (task_id,repo_scope,category,severity,issue,fix,source_pr_url,source_version,embedding_model,embedding_version,embedding) VALUES ('rag-f-cli','repo-A','sql_injection','high','SQL injection in db.py','Use parameterized','https://github.com/t/r-A/pull/9','1.0.0','BAAI/bge-small-en-v1.5','1.0.0',%s::vector)", (vs(make_emb("db.py sql injection")),))
    conn_extra.close()
    req = json.dumps({"contract_version":"1","request_id":"req-cli-s","trace_id":"trace-cli-s","input":{"mode":"paths","paths":["src/db.py"]}})
    try:
        r = subprocess.run([sys.executable,"-m","skills.sast_scan.run"],
            input=req, capture_output=True, text=True, timeout=120, cwd=REPO, env=cli_env)
        env_sast = None
        for line in r.stdout.strip().split("\n"):
            try: env_sast = json.loads(line); break
            except: pass
        check("sast_scan CLI completed", env_sast is not None, "rc=%d"%r.returncode)
        if env_sast:
            check("sast_scan envelope has evidence[]", isinstance(env_sast.get("evidence"),list))
            check("sast_scan evidence has rag_advisory",
                  any(e.get("kind")=="rag_advisory" for e in env_sast.get("evidence",[])))
            rag_ev = [e for e in env_sast.get("evidence",[]) if e.get("kind")=="rag_advisory"]
            if rag_ev:
                parsed = json.loads(rag_ev[0]["ref"])
                check("sast_scan rag hit_count>0", parsed.get("hit_count",0)>0,
                      "hit=%d"%parsed.get("hit_count",0))
                check("sast_scan rag untrusted=true", parsed.get("untrusted")==True)
                check("sast_scan rag adopted=false", parsed.get("adopted")==False)
                if parsed.get("cases"):
                    check("sast_scan rag has case_id", "case_id" in parsed["cases"][0])
                    check("sast_scan rag has similarity", "similarity" in str(parsed["cases"][0]))
                    check("sast_scan rag has citation_url", "citation_url" in str(parsed["cases"][0]))
                measured["skill_cli_sast_pass"]=True
            # Core SAST action still executed
            sast_output_ok = schema_valid(
                env_sast.get("output"), "skills/sast_scan/schema/output.schema.json")
            sast_envelope_ok = schema_valid(
                env_sast, "skills/common/schema/response.envelope.schema.json")
            check("sast_scan output schema", sast_output_ok)
            check("sast_scan envelope schema", sast_envelope_ok)
            sast_core_ok = (
                env_sast.get("status") in ("OK", "PARTIAL") and
                env_sast.get("output", {}).get("stats", {}).get("files_scanned") == 1)
            check("sast_scan core action executed", sast_core_ok,
                  "status=%s"%env_sast.get("status"))
            measured["sast_output_schema_pass"] = sast_output_ok
            measured["sast_envelope_schema_pass"] = sast_envelope_ok
            measured["sast_core_action_pass"] = sast_core_ok
    except Exception as e: check("sast_scan CLI", False, str(e))

    print("\n=== REAL SKILL CLI (pr_lifecycle production entry) ===")
    req2 = json.dumps({"contract_version":"1","request_id":"req-cli-p","trace_id":"trace-cli-p",
        "input":{"action":"ensure_fix_pr","idempotency_key":"rag-cli-001",
                 "changes":[{"path":"src/app.py","content":"x=1\n"}],
                 "commit_message":"test","pr_title":"T","pr_body":"B"}})
    try:
        direct = subprocess.run([sys.executable,"-m","skills.pr_lifecycle.run"],
            input=req2, capture_output=True, text=True, timeout=120, cwd=REPO, env=cli_env)
        env_pr_direct = None
        for line in direct.stdout.strip().split("\n"):
            try: env_pr_direct = json.loads(line); break
            except: pass
        direct_rag = [e for e in (env_pr_direct or {}).get("evidence", [])
                      if e.get("kind") == "rag_advisory"]
        direct_data = json.loads(direct_rag[0]["ref"]) if direct_rag else {}
        direct_schema_ok = bool(env_pr_direct) and schema_valid(
            env_pr_direct, "skills/common/schema/response.envelope.schema.json")
        direct_ok = (
            direct.returncode == 4 and
            (env_pr_direct or {}).get("error_code") == "DENIED" and
            direct_schema_ok and direct_data.get("hit_count", 0) > 0)
        check("pr_lifecycle production CLI completed", direct_ok,
              "rc=%d status=%s" % (
                  direct.returncode, (env_pr_direct or {}).get("status")))
        check("pr_lifecycle production evidence has pgvector hit",
              direct_data.get("hit_count", 0) > 0)
        measured["skill_cli_pr_lifecycle_pass"] = direct_ok

        print("\n=== PRLIFECYCLE CORE ACTION (isolated gateway fixture) ===")
        r = subprocess.run([sys.executable,"tests/rag/pr_lifecycle_cli_harness.py"],
            input=req2, capture_output=True, text=True, timeout=120, cwd=REPO, env=cli_env)
        env_pr = None
        for line in r.stdout.strip().split("\n"):
            try:
                payload = json.loads(line)
                env_pr = payload.get("envelope")
                fixture_calls = payload.get("fixture_calls", [])
                break
            except: pass
        check("pr_lifecycle fixture CLI completed", env_pr is not None and r.returncode == 0,
              "rc=%d"%r.returncode)
        if env_pr:
            check("pr_lifecycle evidence has rag_advisory",
                  any(e.get("kind")=="rag_advisory" for e in env_pr.get("evidence",[])))
            pr_rag = [e for e in env_pr.get("evidence", [])
                      if e.get("kind") == "rag_advisory"]
            pr_rag_data = json.loads(pr_rag[0]["ref"]) if pr_rag else {}
            check("pr_lifecycle rag hit_count>0",
                  pr_rag_data.get("hit_count", 0) > 0)
            check("pr_lifecycle rag untrusted=true",
                  pr_rag_data.get("untrusted") is True)
            check("pr_lifecycle rag adopted=false",
                  pr_rag_data.get("adopted") is False)
            pr_output_ok = schema_valid(
                env_pr.get("output"), "skills/pr_lifecycle/schema/output.schema.json")
            pr_envelope_ok = schema_valid(
                env_pr, "skills/common/schema/response.envelope.schema.json")
            pr_core_ok = (
                env_pr.get("status") == "OK" and
                env_pr.get("output", {}).get("outcome") == "CREATED" and
                "PR_CREATED" in env_pr.get("output", {}).get("phases", []) and
                "create_pull_request" in fixture_calls)
            check("pr_lifecycle output schema", pr_output_ok)
            check("pr_lifecycle envelope schema", pr_envelope_ok)
            check("pr_lifecycle core action executed", pr_core_ok,
                  "status=%s outcome=%s" % (
                      env_pr.get("status"), env_pr.get("output", {}).get("outcome")))
            measured["pr_fixture_core_pass"] = (
                r.returncode == 0 and pr_output_ok and pr_envelope_ok and pr_core_ok)
            measured["pr_output_schema_pass"] = pr_output_ok
            measured["pr_envelope_schema_pass"] = pr_envelope_ok
            measured["pr_core_action_pass"] = pr_core_ok
    except Exception as e: check("pr_lifecycle CLI", False, str(e))

    # RESIDUE
    print("\n=== RESIDUE ===")
    time.sleep(2)
    active, idle, total, txn = measure_residue()
    ft = threading.active_count()-1
    measured["active_query_residue"]=active; measured["idle_connection_residue"]=idle
    measured["connection_residue"]=total; measured["transaction_residue"]=txn
    measured["worker_thread_delta"]=ft-baseline_threads
    check("active_query_residue=0", active==0, "active=%d"%active)
    check("idle_connection_residue=0", idle==0, "idle=%d"%idle)
    check("connection_residue=0", total==0, "total=%d"%total)
    check("transaction_residue=0", txn==0, "txn=%d"%txn)
    check("worker_thread_delta=0", ft==baseline_threads, "delta=%d"%(ft-baseline_threads))

    # CLEANUP
    print("\n=== CLEANUP ===")
    conn3 = psycopg2.connect(ADMIN_DSN); conn3.autocommit=True
    with conn3.cursor() as c:
        c.execute("DELETE FROM knowledge WHERE task_id LIKE 'rag-f-%'")
        c.execute("SELECT count(*) FROM knowledge WHERE task_id LIKE 'rag-f-%'")
        remaining = int(c.fetchone()[0])
    conn3.close()
    measured["test_data_residue"]=remaining
    check("test_data_residue=0", remaining==0, "remaining=%d"%remaining)
    shutil.rmtree("/tmp/rag-sast-ws", ignore_errors=True)
    temp_residue = int(os.path.exists("/tmp/rag-sast-ws"))
    measured["temp_dir_residue"] = temp_residue
    check("temp_dir_residue=0", temp_residue == 0,
          "remaining=%d" % temp_residue)

    # SECRET SCAN
    print("\n=== SECRET SCAN ===")
    all_text = ""
    if env_sast: all_text += json.dumps(env_sast)
    if env_pr: all_text += json.dumps(env_pr)
    ev_path = os.path.join(REPO,"evidence/m6/rag/pgvector-isolated-verification.json")
    if os.path.exists(ev_path): all_text += open(ev_path).read()
    leaks = scan_secrets(all_text)
    measured["secret_scan_targets"]=["envelope_sast","envelope_pr","evidence"]
    measured["secret_leaks"]=leaks
    check("secret_leaks=0 (scanned)", leaks==0, "found %d"%leaks)

    # BUILD EVIDENCE
    all_ok = all(r[1] for r in results)
    commit = subprocess.check_output(["git","-C",REPO,"rev-parse","HEAD"]).decode().strip()
    evidence = {
        "kind":"m6-rag-pgvector-isolated-verification",
        "runtime_source_commit":commit,
        "verification_commit":commit,
        "database":"isolated-postgres-pgvector",
        "reviewer_hit":measured.get("reviewer_hit_count",0)>0,
        "reviewer_hit_count":measured.get("reviewer_hit_count",0),
        "fixer_hit":measured.get("fixer_hit_count",0)>0,
        "fixer_hit_count":measured.get("fixer_hit_count",0),
        "repo_scope_isolation":measured.get("repo_scope_isolation",False),
        "skill_cli_sast_pass":measured.get("skill_cli_sast_pass",False),
        "skill_cli_pr_lifecycle_pass":measured.get("skill_cli_pr_lifecycle_pass",False),
        "envelope_schema_pass":bool(
            measured.get("sast_envelope_schema_pass") and
            measured.get("pr_envelope_schema_pass")),
        "skill_output_schema_pass":bool(
            measured.get("sast_output_schema_pass") and
            measured.get("pr_output_schema_pass")),
        "core_action_after_rag_pass":bool(
            measured.get("sast_core_action_pass") and
            measured.get("pr_core_action_pass") and
            measured.get("pr_fixture_core_pass")),
        "timeout_status":measured.get("timeout_status",""),
        "timeout_reason":measured.get("timeout_reason",""),
        "timeout_sqlstate":measured.get("timeout_sqlstate", ""),
        "timeout_wall_ms":measured.get("timeout_wall_ms",0),
        "post_timeout_success":measured.get("post_timeout_success",False),
        "active_query_residue":measured.get("active_query_residue",-1),
        "idle_connection_residue":measured.get("idle_connection_residue",-1),
        "connection_residue":measured.get("connection_residue",-1),
        "transaction_residue":measured.get("transaction_residue",-1),
        "worker_thread_delta":measured.get("worker_thread_delta",-99),
        "test_data_residue":remaining,
        "temp_dir_residue":measured.get("temp_dir_residue", -1),
        "secret_scan_targets":measured.get("secret_scan_targets",[]),
        "secret_leaks":leaks,
        "all_ok":bool(all_ok),
        "passed":sum(1 for r in results if r[1]),
        "failed":sum(1 for r in results if not r[1]),
        "checks":[{"name":n,"ok":ok,"detail":d} for n,ok,d in results],
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
    }
    os.makedirs(os.path.dirname(ev_path),exist_ok=True)
    tmp=ev_path+".tmp"
    with open(tmp,"w") as f: json.dump(evidence,f,indent=2)
    os.replace(tmp,ev_path)
    print("\n=== SUMMARY: %d passed, %d failed ==="%(evidence["passed"],evidence["failed"]))
    print("all_ok=%s commit=%s"%(evidence["all_ok"],commit[:12]))
    return 0 if all_ok else 1

if __name__=="__main__": sys.exit(main())
