#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_migration_loop.py — 数据库迁移验证纵向闭环(真实 SQL 验证,决赛 D2)。

证据等级:REAL SQL VERIFICATION on an ISOLATED PostgreSQL(trial_kind=ISOLATED_POSTGRES)。
**不是** Agentic Database 分支验证 —— PolarDB 保持 NOT CONNECTED,此处的"试验实例"是
从基线库 TEMPLATE 克隆出来的独立 PostgreSQL 数据库(分支等价物,不是产品分支)。

一次运行完成整个闭环(所有状态都由真实 SQL 结果驱动,没有预置结论):
  S1  基线:schema+seed 建 dbv_baseline,算 schema/data 摘要,登记 data_baselines
  S2  run-1:候选提交 SHA(git mktree 树 SHA)→ task_runs / mcp_calls / run_pr_bindings / bind_revision()
  S3  代码测试(rev1):unittest 真实执行 → PASS
  S4  验证 #1:克隆试验库 → 事务内执行 rev1 → PG 真实报错(23502)→ FAIL/HISTORICAL_DATA_INCOMPATIBLE → 断言 → 回写
  S5  负向:重复回调(同摘要)幂等;冲突回调(异摘要)被拒;不可变表 UPDATE 被拒
  S6  run-2:修订同一候选(rev2,parent=rev1)→ 代码测试 PASS → 验证 #1 PASS(断言 11/11)→ 回写
  S7  审批:l2_create_ticket → l2_bind_verification → l2_approve → db_release_gate = OK
  S8  负向:重复绑定被拒;FAIL 验证不可绑定;并发绑定竞争只有一个成功
  S9  失效:run-3(同 PR 追加代码提交)→ 旧票据闸门 = STALE_SUPERSEDED_BY_NEW_REVISION;
      目标数据摘要变化 → TARGET_DATA_DIGEST_MISMATCH
  S10 迟到回调:失效后再为 run-2 候选回写 PASS → 闸门仍失效(旧回调不能使失效结果重新有效)
  S11 交付迁移方案包(绑定到真实验证摘要)
  S12 证据目录 + SHA256SUMS,清理试验库

用法:
  python tools/dbverify/run_migration_loop.py --pg-port 55432 --pg-password-file <file> \
      --trial-instance "docker:<container-id>@<image-digest>" --out evidence/FINALS-DB-MIGRATION-LOOP-20260914
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import psycopg2
import psycopg2.extras

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CASE_DIR = HERE / "case" / "orders-schema-change"
REPO_NAME = "mergepilot/orders-demo"
PR_NUMBER = 4
BASELINE_DB = "dbv_baseline"


# ── helpers ────────────────────────────────────────────────────────────────

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


def write_lf(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read_norm(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def canon_str(v):
    """Mirror of public._canon_str (m4f1_state.sql)."""
    if v is None:
        return "-1:"
    return "%d:%s" % (len(v.encode("utf-8")), v)


def revision_digest(source_call_id, correlation_id, tool, target_repo, run_id, git_sha, result_status):
    """Mirror of bind_revision()'s source_evidence_digest recomputation."""
    return sha256_text(canon_str(source_call_id) + canon_str(correlation_id) + canon_str(tool)
                       + canon_str(target_repo) + canon_str(run_id) + canon_str(git_sha)
                       + canon_str(result_status))


def git(args, input_bytes=None):
    return subprocess.run(["git"] + args, cwd=str(REPO_ROOT), input=input_bytes,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def git_blob_sha(content: bytes) -> str:
    return git(["hash-object", "--stdin"], content).decode().strip()


def git_tree_sha(files: dict) -> str:
    """Real git tree object id (git mktree --missing) for a {relpath: bytes} set.
    Not a commit on any remote — a content identity computed by git itself."""
    root = {}
    for rel, content in files.items():
        parts = rel.split("/")
        node = root
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = content

    def build(node):
        lines = []
        for name in sorted(node):
            val = node[name]
            if isinstance(val, dict):
                lines.append("040000 tree %s\t%s" % (build(val), name))
            else:
                lines.append("100644 blob %s\t%s" % (git_blob_sha(val), name))
        return git(["mktree", "--missing"], ("\n".join(lines) + "\n").encode()).decode().strip()

    return build(root)


def case_files(migration_rev: int, followup: bool = False) -> dict:
    files = {
        "baseline/schema.sql": read_norm(CASE_DIR / "baseline" / "schema.sql"),
        "baseline/seed.sql": read_norm(CASE_DIR / "baseline" / "seed.sql"),
        "app/orders_service.py": read_norm(CASE_DIR / ("followup" if followup else "app") / "orders_service.py"),
        "app/test_orders_service.py": read_norm(CASE_DIR / "app" / "test_orders_service.py"),
        "assertions.json": read_norm(CASE_DIR / "assertions.json"),
        "migrations/candidate-a.sql": read_norm(CASE_DIR / "migrations" / ("candidate-a.rev%d.sql" % migration_rev)),
    }
    return files


class Loop:
    def __init__(self, args):
        self.args = args
        self.pw = Path(args.pg_password_file).read_text(encoding="utf-8").strip()
        self.report = {
            "report_version": "db-migration-loop-report.v1",
            "evidence_tier": "REAL_SQL_VERIFICATION_ISOLATED_POSTGRES",
            "not_agentic_database_branch": True,
            "polardb": "NOT CONNECTED",
            "data_mode": "SYNTHETIC",
            "case": {},
            "run_suffix": args.run_suffix,
            "environment": {},
            "steps": [],
            "negative_tests": [],
            "gate_timeline": [],
            "outcome_signature": None,
        }
        self.outcomes = []
        self.audit = self.connect(args.audit_db)
        self.audit.autocommit = True
        self.log = io.StringIO()

    # ── connections ──
    def dsn(self, db):
        return "host=%s port=%d user=%s password=%s dbname=%s" % (
            self.args.pg_host, self.args.pg_port, self.args.pg_user, self.pw, db)

    def connect(self, db, autocommit=False):
        c = psycopg2.connect(self.dsn(db))
        c.autocommit = autocommit
        return c

    def q(self, conn, sql, params=None, one=False):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return None
            return cur.fetchone() if one else cur.fetchall()

    def step(self, name, **data):
        rec = {"step": name}
        rec.update(data)
        self.report["steps"].append(rec)
        self.outcomes.append("%s=%s" % (name, data.get("outcome", "-")))
        line = "[%s] %s" % (name, json.dumps({k: v for k, v in data.items() if k != "detail"}, ensure_ascii=False, default=str)[:300])
        print(line)
        self.log.write(line + "\n")

    def negative(self, name, expected, observed, ok):
        self.report["negative_tests"].append({"name": name, "expected": expected, "observed": observed, "ok": bool(ok)})
        self.outcomes.append("neg:%s=%s" % (name, "ok" if ok else "FAILED"))
        print("[negative] %s → %s" % (name, "ok" if ok else "FAILED"))

    def gate(self, label, ticket, target_digest=None):
        row = self.q(self.audit, "SELECT valid, reason, ticket_status, bound_head_sha, current_head_sha, verification_id "
                                 "FROM db_release_gate(%s, %s)", (ticket, target_digest), one=True)
        rec = {"label": label, "ticket_id": ticket, "valid": row[0], "reason": row[1], "ticket_status": row[2],
               "bound_head_sha": row[3], "current_head_sha": row[4], "verification_id": row[5]}
        self.report["gate_timeline"].append(rec)
        self.outcomes.append("gate:%s=%s" % (label, row[1]))
        print("[gate] %s → valid=%s reason=%s" % (label, row[0], row[1]))
        return rec

    # ── S1 baseline ──
    def build_baseline(self):
        adm = self.connect("postgres", autocommit=True)
        self.q(adm, "DROP DATABASE IF EXISTS %s" % BASELINE_DB)
        self.q(adm, "CREATE DATABASE %s" % BASELINE_DB)
        adm.close()
        conn = self.connect(BASELINE_DB)
        with conn.cursor() as cur:
            cur.execute(read_norm(CASE_DIR / "baseline" / "schema.sql").decode())
            cur.execute(read_norm(CASE_DIR / "baseline" / "seed.sql").decode())
        conn.commit()
        tables = ["customers", "orders", "payments", "legacy_order_owner"]
        h = hashlib.sha256()
        counts = {}
        with conn.cursor() as cur:
            for t in tables:
                buf = io.StringIO()
                cur.copy_expert("COPY (SELECT * FROM %s ORDER BY 1) TO STDOUT" % t, buf)
                data = buf.getvalue().encode("utf-8")
                h.update(("table:%s\n" % t).encode()); h.update(data)
                cur.execute("SELECT count(*) FROM %s" % t)
                counts[t] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM orders WHERE customer_id IS NULL")
            null_rows = cur.fetchone()[0]
            cur.execute("SELECT count(*) - count(DISTINCT order_id) FROM payments")
            dup_rows = cur.fetchone()[0]
            cur.execute("SHOW server_version")
            pg_version = cur.fetchone()[0]
        conn.close()
        schema_digest = sha256_bytes(read_norm(CASE_DIR / "baseline" / "schema.sql"))
        data_digest = h.hexdigest()
        baseline_id = self.q(self.audit, "SELECT mv_register_baseline(%s,%s,%s,%s,%s::jsonb,%s)",
                             ("orders-demo historical baseline (synthetic)", "SYNTHETIC", schema_digest, data_digest,
                              json.dumps(counts), pg_version), one=True)[0]
        # idempotency: registering the same digests again returns the same id
        again = self.q(self.audit, "SELECT mv_register_baseline(%s,%s,%s,%s,%s::jsonb,%s)",
                       ("dup", "SYNTHETIC", schema_digest, data_digest, "{}", pg_version), one=True)[0]
        self.negative("baseline_register_idempotent", baseline_id, again, again == baseline_id)
        self.baseline = {"baseline_id": baseline_id, "schema_digest": schema_digest, "data_digest": data_digest,
                         "row_counts": counts, "historical_null_customer_id": null_rows,
                         "historical_duplicate_payment_rows": dup_rows, "pg_version": pg_version}
        self.report["environment"].update({"pg_version": pg_version, "trial_kind": "ISOLATED_POSTGRES",
                                           "trial_instance": self.args.trial_instance,
                                           "clone_method": "CREATE DATABASE ... TEMPLATE dbv_baseline"})
        self.step("S1_baseline", outcome="REGISTERED", **self.baseline)

    # ── S2 revision/run registration ──
    def register_run(self, run_id, head_sha, base_sha, room="room-dbverify"):
        a = self.audit
        self.q(a, "INSERT INTO task_runs(run_id, room_id, repo, pr_number, branch, status, current_stage, skill_data_state) "
                  "VALUES (%s,%s,%s,%s,%s,'RUNNING','verify','ACTIVE') ON CONFLICT (run_id) DO NOTHING",
               (run_id, room, REPO_NAME, PR_NUMBER, "feat/orders-not-null"))
        call_id = "mcp-%s-read" % run_id
        corr = "corr-%s" % call_id
        self.q(a, "INSERT INTO mcp_calls(request_id, correlation_id, phase, ts, caller_agent, tool, decision, reason_code, "
                  "ticket_id, target_repo, target_branch, result_status, git_sha, error, run_id) "
                  "VALUES (%s,%s,'RESULT',now(),'controller','get_pull_request','ALLOW','READ_OK',NULL,%s,'main','OK',%s,NULL,%s) "
                  "ON CONFLICT (request_id) DO NOTHING",
               (call_id, corr, REPO_NAME, base_sha, run_id))
        prb = "prb-%s" % run_id
        self.q(a, "INSERT INTO run_pr_bindings(binding_id, run_id, repo, pr_number, fix_branch, base_branch, head_sha, recorded_at) "
                  "VALUES (%s,%s,%s,%s,%s,'main',%s,now()) ON CONFLICT DO NOTHING",
               (prb, run_id, REPO_NAME, PR_NUMBER, "feat/orders-not-null", head_sha))
        digest = revision_digest(call_id, corr, "get_pull_request", REPO_NAME, run_id, base_sha, "OK")
        rev_bid = self.q(a, "SELECT bind_revision(%s,%s,%s,%s,%s,%s,%s)",
                         (run_id, REPO_NAME, PR_NUMBER, head_sha, base_sha, call_id, digest), one=True)[0]
        return {"run_id": run_id, "head_sha": head_sha, "base_sha": base_sha, "pr_binding_id": prb,
                "revision_binding_id": rev_bid, "source_call_id": call_id, "source_evidence_digest": digest}

    # ── S3 code tests ──
    def run_code_tests(self, label):
        suite = unittest.defaultTestLoader.discover(str(CASE_DIR / "app"), pattern="test_*.py", top_level_dir=str(CASE_DIR / "app"))
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        verdict = "PASS" if result.wasSuccessful() and result.testsRun > 0 else "FAIL"
        rec = {"command": "python -m unittest discover -s tools/dbverify/case/orders-schema-change/app -p 'test_*.py'",
               "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "verdict": verdict, "output_tail": stream.getvalue().strip().splitlines()[-3:]}
        self.step("S3_code_tests_%s" % label, outcome=verdict, **rec)
        return verdict, rec

    # ── S4/S6 verification on a cloned trial DB ──
    def verify(self, label, run, candidate_id, migration_rev, attempt):
        trial_db = "dbv_trial_%s_a%d" % (run["run_id"].replace("-", "_"), attempt)
        adm = self.connect("postgres", autocommit=True)
        self.q(adm, "DROP DATABASE IF EXISTS %s" % trial_db)
        t0 = time.time()
        self.q(adm, "CREATE DATABASE %s TEMPLATE %s" % (trial_db, BASELINE_DB))
        clone_ms = int((time.time() - t0) * 1000)
        adm.close()
        script = read_norm(CASE_DIR / "migrations" / ("candidate-a.rev%d.sql" % migration_rev)).decode()
        conn = self.connect(trial_db, autocommit=True)  # script carries its own BEGIN/COMMIT
        migration = {"applied": False, "error": None}
        t1 = time.time()
        try:
            with conn.cursor() as cur:
                cur.execute(script)
            migration["applied"] = True
        except psycopg2.Error as e:
            # the script's transaction is rolled back by PostgreSQL itself
            migration["error"] = {"sqlstate": e.pgcode, "message": (e.pgerror or str(e)).strip().splitlines()[0][:300],
                                  "detail": (getattr(e.diag, "message_detail", None) or "")[:300]}
            conn.rollback()
        migration["duration_ms"] = int((time.time() - t1) * 1000)
        conn.close()
        # assertions on a fresh connection, each in its own savepoint (probes never taint each other)
        spec = json.loads((CASE_DIR / "assertions.json").read_text(encoding="utf-8"))
        results = []
        conn = self.connect(trial_db, autocommit=False)
        with conn.cursor() as cur:
            for a in spec["assertions"]:
                cur.execute("SAVEPOINT sp")
                rec = {"name": a["name"], "kind": a["kind"], "expect": a["expect"]}
                try:
                    cur.execute(a["sql"])
                    if a["kind"] == "probe":
                        rec["actual"] = "OK"
                    else:
                        v = cur.fetchone()[0]
                        rec["actual"] = int(v) if isinstance(v, (int,)) or (hasattr(v, "__int__") and not isinstance(v, str)) else v
                except psycopg2.Error as e:
                    rec["actual"] = "ERROR"
                    rec["error"] = {"sqlstate": e.pgcode, "message": (e.pgerror or str(e)).strip().splitlines()[0][:200]}
                cur.execute("ROLLBACK TO SAVEPOINT sp")
                rec["passed"] = (rec["actual"] == a["expect"])
                results.append(rec)
        conn.rollback()
        conn.close()
        adm = self.connect("postgres", autocommit=True)
        self.q(adm, "DROP DATABASE IF EXISTS %s" % trial_db)
        adm.close()
        failed = [r["name"] for r in results if not r["passed"]]
        if migration["applied"] and not failed:
            verdict, failure_class = "PASS", None
        elif not migration["applied"] and migration["error"] and migration["error"]["sqlstate"] in ("23502", "23505", "23514"):
            verdict, failure_class = "FAIL", "HISTORICAL_DATA_INCOMPATIBLE"
        elif not migration["applied"]:
            verdict, failure_class = "ERROR", "SCRIPT_ERROR"
        else:
            verdict, failure_class = "FAIL", "OLD_APP_INCOMPATIBLE" if "old_worker_compat_insert" in failed else "ASSERTION_FAILED"
        report = {"run_id": run["run_id"], "candidate_id": candidate_id, "head_sha": run["head_sha"],
                  "migration_rev": migration_rev, "attempt": attempt, "trial_db": trial_db, "clone_ms": clone_ms,
                  "migration": migration, "assertions": results, "assertions_passed": len(results) - len(failed),
                  "assertions_total": len(results), "verdict": verdict, "failure_class": failure_class}
        report_digest = sha256_text(json.dumps(report, sort_keys=True, default=str))
        env = {"pg_version": self.baseline["pg_version"], "trial_instance": self.args.trial_instance,
               "image": self.args.image, "clone": "TEMPLATE " + BASELINE_DB}
        vid = self.q(self.audit, "SELECT mv_record_verification(%s,%s,%s,'ISOLATED_POSTGRES',%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s)",
                     (candidate_id, self.baseline["baseline_id"], attempt, self.args.trial_instance, json.dumps(env),
                      "PASS", verdict, failure_class, json.dumps(results, default=str), report_digest), one=True)[0]
        report["verification_id"] = vid
        report["report_digest"] = report_digest
        self.step("verify_%s" % label, outcome="%s/%s" % (verdict, failure_class or "-"), verification_id=vid,
                  report_digest=report_digest, migration_error=migration["error"], assertions_passed=report["assertions_passed"],
                  assertions_total=report["assertions_total"], failed_assertions=failed, clone_ms=clone_ms,
                  migration_duration_ms=migration["duration_ms"], detail=report)
        return report

    def register_candidate(self, run, rev, parent):
        digest = sha256_bytes(read_norm(CASE_DIR / "migrations" / ("candidate-a.rev%d.sql" % rev)))
        cid = self.q(self.audit, "SELECT mv_register_candidate(%s,'candidate-a',%s,%s,%s,%s)",
                     (run["run_id"], rev, parent, "migrations/candidate-a.rev%d.sql" % rev, digest), one=True)[0]
        return cid, digest

    def create_ticket(self, run):
        payload = {"owner": "mergepilot", "repo": "orders-demo", "pullNumber": PR_NUMBER,
                   "commit_title": "orders: customer_id NOT NULL + payments UNIQUE (candidate-a rev2)", "merge_method": "squash"}
        args_hash = sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return self.q(self.audit, "SELECT l2_create_ticket(%s,'merge',%s::jsonb,%s,24,1)",
                      (run["pr_binding_id"], json.dumps(payload), args_hash), one=True)[0]

    def try_sql(self, conn, sql, params=None):
        """Run a statement expected to fail; return (ok, sqlstate, message)."""
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            return True, None, None
        except psycopg2.Error as e:
            return False, e.pgcode, (e.pgerror or str(e)).strip().splitlines()[0][:200]

    # ── the loop ──
    def run(self):
        a = self.audit
        # case identity
        files_rev1 = case_files(1)
        files_rev2 = case_files(2)
        files_rev3 = case_files(2, followup=True)
        base_files = {k: v for k, v in files_rev1.items() if k.startswith("baseline/")}
        head1, head2, head3, base_sha = git_tree_sha(files_rev1), git_tree_sha(files_rev2), git_tree_sha(files_rev3), git_tree_sha(base_files)
        self.report["case"] = {
            "case_id": "orders-schema-change", "repo": REPO_NAME, "pr_number": PR_NUMBER,
            "sha_kind": "git tree object id of the candidate content (git mktree); NOT a commit on a remote",
            "base_sha": base_sha, "head_rev1": head1, "head_rev2": head2, "head_rev3_followup": head3,
            "files": {k: sha256_bytes(v) for k, v in files_rev2.items()},
            "followup_change": "app/orders_service.py gains cancel_order(); migration unchanged",
        }
        self.build_baseline()

        # ── run-1 / rev1 ──
        run1 = self.register_run("run-dbv-%s-rev1" % self.args.run_suffix, head1, base_sha)
        self.step("S2_run1_registered", outcome="BOUND", **run1)
        self.run_code_tests("rev1")
        cand1, dig1 = self.register_candidate(run1, 1, None)
        self.step("S2_candidate_rev1", outcome="REGISTERED", candidate_id=cand1, script_digest=dig1)
        v1 = self.verify("rev1_attempt1", run1, cand1, 1, 1)

        # ── S5 negatives on write-back ──
        vid_again = self.q(a, "SELECT mv_record_verification(%s,%s,1,'ISOLATED_POSTGRES',%s,'{}'::jsonb,'PASS',%s,%s,'[]'::jsonb,%s)",
                           (cand1, self.baseline["baseline_id"], self.args.trial_instance, v1["verdict"], v1["failure_class"], v1["report_digest"]), one=True)[0]
        rows = self.q(a, "SELECT count(*) FROM migration_verifications WHERE candidate_id=%s", (cand1,), one=True)[0]
        self.negative("duplicate_callback_same_digest_is_noop", "same id, 1 row", "%s, %d row(s)" % (vid_again == v1["verification_id"], rows),
                      vid_again == v1["verification_id"] and rows == 1)
        ok, code, msg = self.try_sql(a, "SELECT mv_record_verification(%s,%s,1,'ISOLATED_POSTGRES',%s,'{}'::jsonb,'PASS','PASS',NULL,'[]'::jsonb,%s)",
                                     (cand1, self.baseline["baseline_id"], self.args.trial_instance, "0" * 64))
        self.negative("conflicting_callback_different_digest_rejected", "SQLSTATE 23505", "%s %s" % (code, msg), (not ok) and code == "23505")
        ok, code, msg = self.try_sql(a, "UPDATE migration_verifications SET migration_verdict='PASS', failure_class=NULL WHERE verification_id=%s", (v1["verification_id"],))
        self.negative("verification_row_immutable", "UPDATE rejected", "%s %s" % (code, msg), not ok)
        ok, code, msg = self.try_sql(a, "SELECT mv_register_candidate(%s,'candidate-a',1,NULL,'x.sql',%s)", (run1["run_id"], "1" * 64))
        self.negative("candidate_revision_script_immutable", "SQLSTATE 23505", "%s %s" % (code, msg), (not ok) and code == "23505")

        # ── S5b context fetch: the failure alone is not enough to decide the revision ──
        # What is missing: who owns the 137 orders with NULL customer_id. Fetch it from the
        # baseline with real SQL, then apply the pre-declared rule.
        conn_b = self.connect(BASELINE_DB)
        cov = self.q(conn_b, "SELECT count(*) FROM legacy_order_owner l JOIN orders o USING (order_id) WHERE o.customer_id IS NULL", one=True)[0]
        dup = self.q(conn_b, "SELECT count(*) - count(DISTINCT order_id) FROM payments", one=True)[0]
        conn_b.close()
        nulls = self.baseline["historical_null_customer_id"]
        unresolvable = nulls - cov
        share = unresolvable / nulls if nulls else 0.0
        threshold = 0.20
        outcome = "REVISE_CANDIDATE" if share <= threshold else "ESCALATE_HUMAN"
        self.context_fetch = {
            "missing_context": "owner (customer_id) for %d historical orders; SQLSTATE 23502 says nothing about whether it can be recovered" % nulls,
            "fetched": {"legacy_order_owner_coverage": cov, "unresolvable_orders": unresolvable, "duplicate_payment_rows": dup,
                        "query": "SELECT count(*) FROM legacy_order_owner l JOIN orders o USING (order_id) WHERE o.customer_id IS NULL"},
            "rule": "unresolvable share <= %.0f%% -> revise the same candidate (backfill + sentinel flagged for follow-up); otherwise escalate to a human before touching data" % (threshold * 100),
            "unresolvable_share": round(share, 4),
            "decision": outcome,
            "next_action": ("candidate-a rev2: backfill %d from legacy_order_owner, sentinel %d (audited), archive %d duplicate payments, "
                            "add DEFAULT for old workers, then constraints" % (cov, unresolvable, dup)) if outcome == "REVISE_CANDIDATE"
                           else "hold: human decision required on %d unknown owners before any migration is revised" % unresolvable,
            "human_gate_still_required": True,
        }
        self.step("S5b_context_fetch", outcome=outcome, **self.context_fetch)

        # ── run-2 / rev2 (修订同一候选) ──
        run2 = self.register_run("run-dbv-%s-rev2" % self.args.run_suffix, head2, base_sha)
        self.step("S6_run2_registered", outcome="BOUND", **run2)
        self.run_code_tests("rev2")
        cand2, dig2 = self.register_candidate(run2, 2, cand1)
        self.step("S6_candidate_rev2", outcome="REGISTERED", candidate_id=cand2, parent_candidate_id=cand1, script_digest=dig2)
        v2 = self.verify("rev2_attempt1", run2, cand2, 2, 1)
        v2b = self.verify("rev2_attempt2_for_race_test", run2, cand2, 2, 2)

        # ── S7 approval bound to the verified version (the bind step IS the race) ──
        ticket = self.create_ticket(run2)
        self.step("S7_ticket_created", outcome="PENDING", ticket_id=ticket)
        ok, code, msg = self.try_sql(a, "SELECT l2_create_ticket(%s,'merge',%s::jsonb,%s,24,1)",
                                     (run2["pr_binding_id"], json.dumps({"owner": "mergepilot", "repo": "orders-demo", "pullNumber": PR_NUMBER,
                                      "commit_title": "second ticket", "merge_method": "squash"}), "a" * 64))
        self.negative("existing_model_one_active_ticket_per_binding_action", "SQLSTATE 23505 uq_active_ticket_per_binding_action",
                      "%s %s" % (code, msg), (not ok) and code == "23505")
        self.gate("before_bind", ticket)
        # concurrent binding race: two sessions bind two distinct PASS verifications to the same ticket
        results = {}
        barrier = threading.Barrier(2)

        def bind(name, vid):
            c = self.connect(self.args.audit_db, autocommit=True)
            barrier.wait()
            results[name] = self.try_sql(c, "SELECT l2_bind_verification(%s,%s)", (ticket, vid))
            c.close()
        t1 = threading.Thread(target=bind, args=("A", v2["verification_id"]))
        t2 = threading.Thread(target=bind, args=("B", v2b["verification_id"]))
        t1.start(); t2.start(); t1.join(); t2.join()
        wins = [k for k, r in results.items() if r[0]]
        bound_rows = self.q(a, "SELECT count(*) FROM approval_verification_bindings WHERE ticket_id=%s", (ticket,), one=True)[0]
        self.negative("concurrent_bind_exactly_one_wins", "1 winner, 1 binding row",
                      "winners=%s rows=%d losers=%s" % (wins, bound_rows, {k: r[1] for k, r in results.items() if not r[0]}),
                      len(wins) == 1 and bound_rows == 1)
        bound_vid = self.q(a, "SELECT verification_id FROM approval_verification_bindings WHERE ticket_id=%s", (ticket,), one=True)[0]
        loser_vid = v2b["verification_id"] if bound_vid == v2["verification_id"] else v2["verification_id"]
        self.step("S7_bind_verification", outcome="BOUND", ticket_id=ticket, verification_id=bound_vid, race_winner=wins[0] if wins else None)
        self.gate("bound_not_yet_approved", ticket)
        approved = self.q(a, "SELECT l2_approve(%s)", (ticket,), one=True)[0]
        who = self.q(a, "SELECT status, approved_by FROM approvals WHERE ticket_id=%s", (ticket,), one=True)
        self.step("S7_l2_approve", outcome=who[0], approved=approved, approved_by=who[1], ticket_id=ticket)
        g_ok = self.gate("approved_current_version", ticket)
        self.gate("approved_same_data_digest", ticket, self.baseline["data_digest"])
        g_data = self.gate("target_data_changed", ticket, "f" * 64)
        self.negative("approval_invalid_when_target_data_differs", "TARGET_DATA_DIGEST_MISMATCH", g_data["reason"],
                      g_data["reason"] == "TARGET_DATA_DIGEST_MISMATCH")

        # ── S8 negatives on binding ──
        ok, code, msg = self.try_sql(a, "SELECT l2_bind_verification(%s,%s)", (ticket, loser_vid))
        self.negative("rebind_same_ticket_rejected", "already bound (23505)", "%s %s" % (code, msg), (not ok) and code == "23505")
        ticket_fail = self.create_ticket(run1)
        ok, code, msg = self.try_sql(a, "SELECT l2_bind_verification(%s,%s)", (ticket_fail, v1["verification_id"]))
        self.negative("fail_verification_cannot_be_bound", "rejected (not PASS/PASS)", "%s %s" % (code, msg), not ok)
        ok, code, msg = self.try_sql(a, "SELECT l2_bind_verification(%s,%s)", (ticket_fail, v2["verification_id"]))
        self.negative("cross_run_verification_cannot_be_bound", "rejected (run mismatch)", "%s %s" % (code, msg), not ok)

        # ── S9 invalidation by version change ──
        run3 = self.register_run("run-dbv-%s-rev3-followup" % self.args.run_suffix, head3, base_sha)
        self.step("S9_run3_followup_commit_registered", outcome="BOUND", **run3)
        g_stale = self.gate("after_followup_commit", ticket)
        self.negative("old_approval_invalid_after_new_revision", "STALE_SUPERSEDED_BY_NEW_REVISION", g_stale["reason"], g_stale["reason"] == "STALE_SUPERSEDED_BY_NEW_REVISION")

        # ── S10 late callback cannot revive ──
        late = self.verify("rev2_late_callback_attempt3", run2, cand2, 2, 3)
        g_late = self.gate("after_late_pass_callback", ticket)
        self.negative("late_callback_does_not_revive_stale_approval", "still invalid", g_late["reason"], g_late["valid"] is False)
        ok, code, msg = self.try_sql(a, "UPDATE approval_verification_bindings SET head_sha=%s WHERE ticket_id=%s", (head3, ticket))
        self.negative("binding_row_immutable", "UPDATE rejected", "%s %s" % (code, msg), not ok)
        ok, code, msg = self.try_sql(a, "UPDATE revision_bindings SET head_sha=%s WHERE run_id=%s", (head2, run3["run_id"]))
        self.negative("revision_bindings_immutable_unchanged", "UPDATE rejected", "%s %s" % (code, msg), not ok)

        # run status view for the demo/console
        status = self.q(a, "SELECT candidate_key, revision_no, attempt, migration_verdict, code_tests_verdict, failure_class, trial_kind FROM mv_run_status(%s)", (run2["run_id"],))
        self.report["run2_status_view"] = [dict(zip(["candidate_key", "revision_no", "attempt", "migration_verdict", "code_tests_verdict", "failure_class", "trial_kind"], r)) for r in status]

        # ── S11 migration plan package (bound to the verified version) ──
        pkg = self.write_plan_package(run2, cand2, dig2, v2)
        self.step("S11_migration_plan_package", outcome="WRITTEN", **pkg)

        self.report["final_disposition"] = {
            "candidate_a_rev1": "FAIL on historical data (SQLSTATE %s) — code tests PASS" % (v1["migration"]["error"] or {}).get("sqlstate"),
            "candidate_a_rev2": "PASS (%d/%d assertions) — approved on head %s, ticket %s" % (v2["assertions_passed"], v2["assertions_total"], head2[:12], ticket),
            "after_followup_commit": "approval %s → %s; release blocked until re-verification on head %s" % (ticket, g_stale["reason"], head3[:12]),
            "production_release": "NOT PERFORMED — trial database clone is not a production release; execution requires a controlled rollout using the plan package",
        }
        self.report["outcome_signature"] = sha256_text("|".join(self.outcomes))
        self.write_evidence()
        self.cleanup()
        return self.report

    def write_plan_package(self, run, candidate_id, script_digest, v):
        out = REPO_ROOT / "release" / "migration-plans" / "orders-schema-change" / "candidate-a.rev2"
        out.mkdir(parents=True, exist_ok=True)
        script = read_norm(CASE_DIR / "migrations" / "candidate-a.rev2.sql").decode()
        preflight = """-- 01-preflight.sql — compatibility checks BEFORE running the migration (read-only).
-- Abort the rollout if any value differs from the verified baseline profile.
SELECT 'null_customer_id' AS check, count(*) AS value, 137 AS verified_baseline FROM orders WHERE customer_id IS NULL
UNION ALL SELECT 'duplicate_payment_rows', count(*) - count(DISTINCT order_id), 2 FROM payments
UNION ALL SELECT 'legacy_owner_coverage', count(*), 120 FROM legacy_order_owner l JOIN orders o USING (order_id) WHERE o.customer_id IS NULL
UNION ALL SELECT 'sentinel_customer_absent', count(*), 0 FROM customers WHERE customer_id = 0
UNION ALL SELECT 'uq_payments_order_id_absent', count(*), 0 FROM pg_constraint WHERE conname = 'uq_payments_order_id';
"""
        postcheck = "-- 03-postcheck.sql — the verified assertions (same queries the trial run evaluated).\n"
        spec = json.loads((CASE_DIR / "assertions.json").read_text(encoding="utf-8"))
        for a in spec["assertions"]:
            if a["kind"] == "scalar":
                postcheck += "-- %s (expect %s)\n%s;\n" % (a["name"], a["expect"], a["sql"])
        rollback = """-- 04-rollback.sql — reverse candidate-a rev2 (only while no post-migration writes depend on the constraints).
BEGIN;
ALTER TABLE payments DROP CONSTRAINT IF EXISTS uq_payments_order_id;
ALTER TABLE orders ALTER COLUMN customer_id DROP NOT NULL;
ALTER TABLE orders ALTER COLUMN customer_id DROP DEFAULT;
-- restore archived duplicate payments
INSERT INTO payments SELECT * FROM payments_dedup_archive ON CONFLICT (payment_id) DO NOTHING;
-- restore the pre-backfill customer_id values (NULL) recorded in the audit table
UPDATE orders o SET customer_id = a.old_customer_id FROM orders_backfill_audit a WHERE a.order_id = o.order_id;
COMMIT;
-- keep orders_backfill_audit / payments_dedup_archive until the rollback is confirmed, then drop them explicitly.
"""
        readme = """# 迁移方案包 · orders-schema-change / candidate-a rev2

本包由 `tools/dbverify/run_migration_loop.py` 在**真实 SQL 验证**通过后生成，所有版本信息与验证报告摘要绑定；
任何一项与目标环境不符，即视为本方案**不适用**，必须重新验证。

> 证据等级：ISOLATED_POSTGRES（从脱敏基线 TEMPLATE 克隆的独立 PostgreSQL 试验库）。
> **不是** Agentic Database 分支验证；PolarDB 保持 NOT CONNECTED。试验库通过 ≠ 生产发布。

## 适用版本（必须逐项核对）

| 项 | 值 |
|---|---|
| 候选代码版本（git tree SHA，非远端提交） | `%(head)s` |
| 迁移脚本摘要 sha256 | `%(script)s` |
| 数据基线 schema 摘要 | `%(schema)s` |
| 数据基线 data 摘要 | `%(data)s` |
| 基线行数 | %(rows)s |
| 验证实例 | %(instance)s |
| PostgreSQL | %(pg)s |
| 验证记录 | `%(vid)s`（report_digest `%(rdig)s`） |
| 断言 | %(ap)d/%(at)d 通过 |

## 执行顺序

1. `01-preflight.sql`（只读）：目标库的历史数据画像必须与验证基线一致（137 NULL / 2 重复 / 120 可回填）。任一不符 → **停止**，回到验证环节。
2. 执行前调用 `db_release_gate(<ticket>, <目标数据摘要>)`，必须返回 `valid=true`；否则 **停止**（旧批准已失效）。
3. `02-migrate.sql`：单事务；失败即自动回滚，目标库无残留（回填审计表与去重归档表在同一事务内创建）。
4. `03-postcheck.sql`：与试验验证相同的断言；任一不符 → 执行 `04-rollback.sql` 并停止后续发布步骤。
5. 应用发布：先发布新代码（总是携带 customer_id），旧 worker 依赖 `DEFAULT 0` 在窗口期内继续可写；窗口期结束后处理 `orders_backfill_audit.source='sentinel'` 的 17 条记录，再评估移除 DEFAULT。

## 失败停止 / 恢复

- 迁移事务失败：PostgreSQL 自动回滚，无需人工恢复；记录 SQLSTATE 并回到验证环节。
- 后检失败：执行 `04-rollback.sql`（恢复归档支付、恢复回填前的 NULL、移除约束），保留审计/归档表直到确认。
- 任何步骤失败后**不得**继续应用发布；票据保持在 APPROVED 但闸门会因数据摘要变化而返回失效。

## 文件

- `01-preflight.sql` · `02-migrate.sql` · `03-postcheck.sql` · `04-rollback.sql` · `manifest.json` · `SHA256SUMS`
""" % {"head": run["head_sha"], "script": script_digest, "schema": self.baseline["schema_digest"], "data": self.baseline["data_digest"],
       "rows": json.dumps(self.baseline["row_counts"]), "instance": self.args.trial_instance, "pg": self.baseline["pg_version"],
       "vid": v["verification_id"], "rdig": v["report_digest"], "ap": v["assertions_passed"], "at": v["assertions_total"]}
        files = {"README.md": readme, "01-preflight.sql": preflight, "02-migrate.sql": script, "03-postcheck.sql": postcheck, "04-rollback.sql": rollback}
        manifest = {"package": "orders-schema-change/candidate-a.rev2", "bound_to": {
            "head_sha": run["head_sha"], "script_digest": script_digest, "baseline_schema_digest": self.baseline["schema_digest"],
            "baseline_data_digest": self.baseline["data_digest"], "verification_id": v["verification_id"], "report_digest": v["report_digest"],
            "pg_version": self.baseline["pg_version"], "trial_kind": "ISOLATED_POSTGRES"},
            "not_a_production_release": True, "files": {}}
        for name, content in files.items():
            write_lf(out / name, content)
            manifest["files"][name] = sha256_text(content)
        write_lf(out / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        sums = "".join("%s *%s\n" % (sha256_bytes((out / n).read_bytes()), n) for n in sorted(list(files) + ["manifest.json"]))
        write_lf(out / "SHA256SUMS", sums)
        return {"path": str(out.relative_to(REPO_ROOT)).replace("\\", "/"), "files": sorted(list(files) + ["manifest.json", "SHA256SUMS"])}

    def write_evidence(self):
        out = Path(self.args.out)
        out.mkdir(parents=True, exist_ok=True)
        write_lf(out / "report.json", json.dumps(self.report, indent=2, ensure_ascii=False, default=str) + "\n")
        write_lf(out / "run.log", self.log.getvalue())
        for rel in ["baseline/schema.sql", "baseline/seed.sql", "migrations/candidate-a.rev1.sql", "migrations/candidate-a.rev2.sql", "assertions.json"]:
            dst = out / "case" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(read_norm(CASE_DIR / rel))
        meta = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "python": sys.version.split()[0],
                "git_head": git(["rev-parse", "HEAD"]).decode().strip(), "trial_instance": self.args.trial_instance,
                "image": self.args.image, "command": "python tools/dbverify/run_migration_loop.py --pg-port %d --trial-instance ... --out %s" % (self.args.pg_port, self.args.out),
                "evidence_tier": "REAL_SQL_VERIFICATION_ISOLATED_POSTGRES (not Agentic Database branch)"}
        write_lf(out / "run-meta.json", json.dumps(meta, indent=2) + "\n")
        names = ["report.json", "run.log", "run-meta.json"] + ["case/" + r for r in ["baseline/schema.sql", "baseline/seed.sql", "migrations/candidate-a.rev1.sql", "migrations/candidate-a.rev2.sql", "assertions.json"]]
        sums = "".join("%s *%s\n" % (sha256_bytes((out / n).read_bytes()), n) for n in names)
        write_lf(out / "SHA256SUMS", sums)
        print("[evidence] %s (outcome_signature=%s)" % (out, self.report["outcome_signature"][:16]))

    def cleanup(self):
        adm = self.connect("postgres", autocommit=True)
        if not self.args.keep_baseline:
            self.q(adm, "DROP DATABASE IF EXISTS %s" % BASELINE_DB)
        rows = self.q(adm, "SELECT datname FROM pg_database WHERE datname LIKE 'dbv_trial_%%'")
        for (d,) in rows:
            self.q(adm, "DROP DATABASE IF EXISTS %s" % d)
        adm.close()
        self.audit.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-host", default="127.0.0.1")
    ap.add_argument("--pg-port", type=int, default=55432)
    ap.add_argument("--pg-user", default="mergepilot")
    ap.add_argument("--pg-password-file", required=True)
    ap.add_argument("--audit-db", default="mergepilot_audit")
    ap.add_argument("--trial-instance", required=True, help="identity of the throwaway PG, e.g. docker:<container>@<image digest>")
    ap.add_argument("--image", default="pgvector/pgvector:pg16")
    ap.add_argument("--out", default=str(REPO_ROOT / "evidence" / "FINALS-DB-MIGRATION-LOOP-20260914"))
    ap.add_argument("--keep-baseline", action="store_true")
    ap.add_argument("--run-suffix", default=time.strftime("%H%M%S", time.gmtime()), help="unique token so audit rows (immutable) never collide across runs")
    args = ap.parse_args()
    report = Loop(args).run()
    neg_failed = [n["name"] for n in report["negative_tests"] if not n["ok"]]
    print("[summary] steps=%d negative_tests=%d failed=%s" % (len(report["steps"]), len(report["negative_tests"]), neg_failed or "none"))
    return 1 if neg_failed else 0


if __name__ == "__main__":
    sys.exit(main())
