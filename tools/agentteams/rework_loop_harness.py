#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rework_loop_harness.py — 多 Agent 返工闭环的机制验证(决赛 D1)。

证据等级:**MECHANISM VERIFICATION**
  真实的 = Workflow Controller 代码(tools/workflow-controller/controller.py::process_event,
           旧路径 + MAX_VERIFY_ATTEMPTS 返工循环)、真实 PostgreSQL 审计链(task_runs / stage_runs /
           stage_events / dispatch_outbox)、真实测试执行(unittest 跑验收测试决定 VERDICT)。
  受控的 = Agent 的语义输出(Reviewer findings、Fixer 的两个补丁)由本 harness 提供,没有 LLM。
  没有的 = Matrix / Element 交接、CoPaw 容器、GitHub 写入。这些属于另一证据等级(真实 Agent 运行),
           本 harness 不替代其完成声明。

场景:
  A  review → fix#1 → verify FAIL(真实测试失败)→ 控制器退回 Fixer(outbox "回退修复")
     → fix#2 → verify PASS(真实测试通过)→ task PASS
  B  连续 3 次 verify FAIL → HOLD/verify_max_hold,不再派发第 4 次 fix(重试上限)
  C  上下文不足:Verifier 找不到验收测试 → VERDICT=BLOCKED → 控制器按非 PASS 处理并退回;
     连续 BLOCKED 达上限 → HOLD 转人工
  D  冲突/非法输入:非 verifier 发 verify 完成、重复 event_id、缺 VERDICT 的流式快照、非 admin 提交

保存:风险依据、两次补丁(unified diff)、每次尝试的代码树 SHA(git mktree)、测试命令与输出、
返工原因(outbox 正文 + task_runs.last_error)、run/stage/event id 关联、最终处置。

用法:
  python tools/agentteams/rework_loop_harness.py --pg-port 55432 --pg-password-file <file> \
      --out evidence/FINALS-REWORK-LOOP-20260914
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CASE = HERE / "rework_case"
CTRL_DIR = REPO_ROOT / "tools" / "workflow-controller"
ROOM = "!rework-mechanism:matrix-local"
SERVER = "matrix-local.hiclaw.io:18080"


def _repo_rel(p) -> str:
    """Repo-relative, forward-slash form of an output path (no machine-specific prefixes in evidence)."""
    try:
        return Path(p).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(p).name


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def git(args, input_bytes=None):
    return subprocess.run(["git"] + args, cwd=str(REPO_ROOT), input=input_bytes,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def tree_sha(files: dict) -> str:
    lines = []
    for name in sorted(files):
        blob = git(["hash-object", "--stdin"], files[name]).decode().strip()
        lines.append("100644 blob %s\t%s" % (blob, name))
    return git(["mktree", "--missing"], ("\n".join(lines) + "\n").encode()).decode().strip()


def revision_files(variant: str, with_tests: bool = True) -> dict:
    files = {"payments.py": (CASE / variant / "payments.py").read_bytes().replace(b"\r\n", b"\n")}
    if with_tests:
        files["test_payments.py"] = (CASE / "base" / "test_payments.py").read_bytes().replace(b"\r\n", b"\n")
    return files


def unified_diff(a: bytes, b: bytes, a_name: str, b_name: str) -> str:
    return "".join(difflib.unified_diff(a.decode().splitlines(True), b.decode().splitlines(True), a_name, b_name))


def run_acceptance_tests(files: dict) -> dict:
    """The Verifier's only source of truth: execute the PR's acceptance tests."""
    work = Path(tempfile.mkdtemp(prefix="rework-verify-"))
    for name, content in files.items():
        (work / name).write_bytes(content)
    if not (work / "test_payments.py").exists():
        shutil.rmtree(work, ignore_errors=True)
        return {"verdict": "BLOCKED", "reason": "acceptance test file test_payments.py not present in the revision; cannot verify", "tests_run": 0}
    suite = unittest.defaultTestLoader.discover(str(work), pattern="test_*.py", top_level_dir=str(work))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    out = stream.getvalue()
    shutil.rmtree(work, ignore_errors=True)
    for m in list(sys.modules):
        if m in ("payments", "test_payments"):
            del sys.modules[m]
    failed = [t.id().split(".")[-1] for t, _ in result.failures + result.errors]
    return {"verdict": "PASS" if result.wasSuccessful() and result.testsRun > 0 else "FAIL",
            "command": "python -m unittest discover -s <revision> -p 'test_*.py'",
            "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "failed_tests": failed, "output": out}


class Harness:
    def __init__(self, args):
        self.args = args
        pw = Path(args.pg_password_file).read_text(encoding="utf-8").strip()
        os.environ.update({"PG_HOST": args.pg_host, "PG_PORT": str(args.pg_port), "PG_DATABASE": args.audit_db,
                           "PG_USER": args.pg_user, "PG_PASS": pw, "MAX_VERIFY_ATTEMPTS": str(args.max_verify_attempts),
                           "L2_MERGE_ENABLED": "0", "M4F_ONLY_MODE": "0", "M4F_ENABLED": "0"})
        sys.path.insert(0, str(CTRL_DIR))
        # Load the REAL controller module from its file under a private module
        # name, after the environment is set: a `controller` module imported
        # earlier in the same process (e.g. by another test) would keep the
        # default audit-pg connection settings.
        import importlib.util
        spec = importlib.util.spec_from_file_location("mp_real_workflow_controller", str(CTRL_DIR / "controller.py"))
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        self.ctrl = controller
        self.conn = controller.ensure_pg()
        self.events = []
        self.report = {"report_version": "rework-loop-report.v1", "evidence_tier": "MECHANISM_VERIFICATION",
                       "real": ["controller.process_event", "postgresql audit chain", "acceptance test execution"],
                       "controlled_input": ["reviewer findings", "fixer patches"],
                       "not_executed": ["Matrix/Element handoff", "CoPaw agent runtime", "LLM calls", "GitHub writes"],
                       "controller": {"file": "tools/workflow-controller/controller.py", "max_verify_attempts": controller.MAX_VERIFY_ATTEMPTS,
                                      "mode": "legacy (M4F_ONLY_MODE=0) — the path that implements the verify-FAIL rework loop"},
                       "scenarios": {}, "outcome_signature": None}
        self.sig = []

    # ── event injection = what /sync would deliver ──
    def event(self, sender: str, body: str, event_id: str = None) -> str:
        eid = event_id or ("$" + uuid.uuid4().hex[:22])
        raw = "@%s:%s" % (sender, SERVER)
        self.ctrl.process_event(eid, ROOM, raw, sender, body, int(time.time() * 1000))
        self.events.append({"event_id": eid, "sender": sender, "body_first_line": body.splitlines()[0][:120]})
        return eid

    def q(self, sql, params=None, one=False):
        conn = self.ctrl.ensure_pg()
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.rollback()
        return rows[0] if one else rows

    def snapshot(self, run_id: str) -> dict:
        task = self.q("SELECT status, current_stage, verify_attempt, verdict, last_error FROM task_runs WHERE run_id=%s", (run_id,), one=True)
        stages = self.q("SELECT stage, agent, attempt, status, verdict FROM stage_runs WHERE run_id=%s ORDER BY id", (run_id,))
        outbox = self.q("SELECT target_agent, target_stage, attempt, status, body FROM dispatch_outbox WHERE run_id=%s ORDER BY id", (run_id,))
        events = self.q("SELECT event_id, sender, event_type, stage, status, error FROM stage_events WHERE run_id=%s OR event_id = ANY(%s) ORDER BY received_at",
                        (run_id, [e["event_id"] for e in self.events]))
        return {"task": dict(zip(["status", "current_stage", "verify_attempt", "verdict", "last_error"], task)) if task else None,
                "stage_runs": [dict(zip(["stage", "agent", "attempt", "status", "verdict"], s)) for s in stages],
                "dispatch_outbox": [dict(zip(["target_agent", "target_stage", "attempt", "status", "body"], o)) for o in outbox],
                "stage_events": [dict(zip(["event_id", "sender", "event_type", "stage", "status", "error"], e)) for e in events]}

    def submit(self, run_id: str) -> dict:
        payload = {"run_id": run_id, "repo": "mergepilot/payments-demo", "pr_number": 7, "branch": "fix/payment-idempotency"}
        self.event("admin", "TASK_SUBMITTED: " + json.dumps(payload))
        snap = self.snapshot(run_id)
        self.sig.append("%s:submit=%s/%s" % (run_id, snap["task"]["status"], snap["task"]["current_stage"]))
        return snap

    def review(self, run_id: str):
        findings = json.loads((CASE / "reviewer_findings.json").read_text(encoding="utf-8"))
        body = "TASK_COMPLETED: %s-review\nfindings: %s" % (run_id, json.dumps(findings["findings"], ensure_ascii=False)[:600])
        self.event("reviewer", body)
        snap = self.snapshot(run_id)
        self.sig.append("%s:review=%s" % (run_id, snap["task"]["current_stage"]))
        return findings

    def fix(self, run_id: str, variant: str, with_tests: bool = True, prev_files: dict = None):
        files = revision_files(variant, with_tests)
        sha = tree_sha(files)
        diff = unified_diff((prev_files or revision_files("base"))["payments.py"], files["payments.py"], "a/payments.py", "b/payments.py")
        self.event("fixer", "TASK_COMPLETED: %s-fix\nrevision_tree_sha=%s" % (run_id, sha))
        snap = self.snapshot(run_id)
        self.sig.append("%s:fix(%s)=%s" % (run_id, variant, snap["task"]["current_stage"]))
        return {"variant": variant, "tree_sha": sha, "diff": diff, "files": files}

    def verify(self, run_id: str, files: dict):
        result = run_acceptance_tests(files)
        body = "TASK_COMPLETED: %s-verify\nVERDICT=%s\ntests_run=%s failed=%s" % (
            run_id, result["verdict"], result.get("tests_run"), ",".join(result.get("failed_tests", [])) or result.get("reason", "-"))
        self.event("verifier", body)
        snap = self.snapshot(run_id)
        self.sig.append("%s:verify=%s→%s/%s/va%s" % (run_id, result["verdict"], snap["task"]["status"], snap["task"]["current_stage"], snap["task"]["verify_attempt"]))
        return result, snap

    # ── scenarios ──
    def scenario_a(self, sfx):
        run_id = "run-rework-%s-a" % sfx
        s0 = self.submit(run_id)
        findings = self.review(run_id)
        f1 = self.fix(run_id, "attempt1")
        v1, s1 = self.verify(run_id, f1["files"])
        rework_dispatch = [o for o in s1["dispatch_outbox"] if o["target_stage"] == "fix" and o["attempt"] == 2]
        f2 = self.fix(run_id, "attempt2", prev_files=f1["files"])
        v2, s2 = self.verify(run_id, f2["files"])
        final = self.snapshot(run_id)
        checks = {
            "submit_creates_review_stage": s0["task"]["current_stage"] == "review" and any(s["stage"] == "review" for s in s0["stage_runs"]),
            "attempt1_tests_fail": v1["verdict"] == "FAIL" and "test_second_payment_request_for_same_order_is_idempotent" in v1["failed_tests"],
            "controller_sends_back_to_fixer": s1["task"]["status"] == "RUNNING" and s1["task"]["current_stage"] == "fix" and s1["task"]["verify_attempt"] == 1,
            "rework_dispatch_row_created": len(rework_dispatch) == 1 and "回退修复" in rework_dispatch[0]["body"],
            "fix_attempt2_stage_row": any(s["stage"] == "fix" and s["attempt"] == 2 for s in s1["stage_runs"]),
            "attempt2_tests_pass": v2["verdict"] == "PASS" and v2["tests_run"] == 5,
            "verify_attempt2_recorded_pass": any(s["stage"] == "verify" and s["attempt"] == 2 and s["verdict"] == "PASS" for s in final["stage_runs"]),
            "task_final_pass": final["task"]["status"] == "PASS",
            "every_event_processed": all(e["status"] == "PROCESSED" for e in final["stage_events"]),
        }
        return {"run_id": run_id, "checks": checks, "risk_basis": findings, "attempts": [
            {"attempt": 1, "tree_sha": f1["tree_sha"], "diff_file": "patches/%s-attempt1.diff" % run_id, "tests": {k: v for k, v in v1.items() if k != "output"},
             "controller_state_after_verify": s1["task"], "rework_reason": rework_dispatch[0]["body"] if rework_dispatch else None},
            {"attempt": 2, "tree_sha": f2["tree_sha"], "diff_file": "patches/%s-attempt2.diff" % run_id, "tests": {k: v for k, v in v2.items() if k != "output"},
             "controller_state_after_verify": s2["task"]}],
            "final": final, "artifacts": {"patches": {"attempt1": f1["diff"], "attempt2": f2["diff"]}, "test_logs": {"attempt1": v1["output"], "attempt2": v2["output"]}}}

    def scenario_b(self, sfx):
        run_id = "run-rework-%s-b" % sfx
        self.submit(run_id); self.review(run_id)
        prev = None
        verdicts = []
        for i in range(1, self.args.max_verify_attempts + 1):
            f = self.fix(run_id, "attempt1", prev_files=prev)
            v, s = self.verify(run_id, f["files"])
            verdicts.append((v["verdict"], s["task"]["status"], s["task"]["current_stage"], s["task"]["verify_attempt"]))
            prev = f["files"]
        final = self.snapshot(run_id)
        checks = {
            "all_verdicts_fail": all(v[0] == "FAIL" for v in verdicts),
            "rework_dispatched_before_cap": sum(1 for o in final["dispatch_outbox"] if o["target_stage"] == "fix" and o["attempt"] >= 2) == self.args.max_verify_attempts - 1,
            "hold_at_cap": final["task"]["status"] == "HOLD" and final["task"]["current_stage"] == "verify_max_hold",
            "no_fix_dispatch_beyond_cap": not any(o["target_stage"] == "fix" and o["attempt"] > self.args.max_verify_attempts for o in final["dispatch_outbox"]),
            "last_error_names_cap": "MAX_VERIFY_ATTEMPTS" in (final["task"]["last_error"] or ""),
        }
        return {"run_id": run_id, "checks": checks, "verdict_sequence": verdicts, "final": final}

    def scenario_c(self, sfx):
        run_id = "run-rework-%s-c" % sfx
        self.submit(run_id); self.review(run_id)
        prev = None
        seq = []
        for i in range(1, self.args.max_verify_attempts + 1):
            f = self.fix(run_id, "attempt2", with_tests=False, prev_files=prev)   # Fixer omitted the acceptance test → Verifier lacks context
            v, s = self.verify(run_id, f["files"])
            seq.append({"verdict": v["verdict"], "reason": v.get("reason"), "task": s["task"]})
            prev = f["files"]
        final = self.snapshot(run_id)
        checks = {
            "verifier_blocked_on_missing_context": all(x["verdict"] == "BLOCKED" for x in seq),
            "blocked_recorded_as_needs_approval": any(s["verdict"] == "blocked-needs-approval" for s in final["stage_runs"] if s["stage"] == "verify"),
            "blocked_consumes_attempt_and_sends_back": seq[0]["task"]["current_stage"] == "fix" and seq[0]["task"]["verify_attempt"] == 1,
            "escalates_to_hold_at_cap": final["task"]["status"] == "HOLD",
        }
        return {"run_id": run_id, "checks": checks, "sequence": seq, "final": final,
                "observation": "Legacy path treats BLOCKED like FAIL (consumes a verify attempt, sends back to Fixer); the Verifier's reason lives in stage_events.raw_body, the outbox body is the generic rework text. Human escalation = HOLD at the cap."}

    def scenario_d(self, sfx):
        run_id = "run-rework-%s-d" % sfx
        self.submit(run_id); self.review(run_id)
        f = self.fix(run_id, "attempt2")
        obs = {}
        # 1. verify completion from a non-verifier sender
        e1 = self.event("fixer", "TASK_COMPLETED: %s-verify\nVERDICT=PASS" % run_id)
        st = self.q("SELECT status, error FROM stage_events WHERE event_id=%s", (e1,), one=True)
        task = self.q("SELECT status, current_stage FROM task_runs WHERE run_id=%s", (run_id,), one=True)
        obs["verify_from_non_verifier"] = {"event_status": st[0], "error": st[1], "task_after": list(task), "ok": task[1] == "verify" and task[0] == "RUNNING"}
        # 2. duplicate event_id (same verify event replayed)
        before = self.q("SELECT count(*) FROM stage_events", one=True)[0]
        e2 = self.event("verifier", "TASK_COMPLETED: %s-verify\nVERDICT=PASS" % run_id, event_id="$dup-%s" % sfx)
        self.event("verifier", "TASK_COMPLETED: %s-verify\nVERDICT=FAIL" % run_id, event_id="$dup-%s" % sfx)   # replay with different body
        after = self.q("SELECT count(*) FROM stage_events", one=True)[0]
        task = self.q("SELECT status, verify_attempt FROM task_runs WHERE run_id=%s", (run_id,), one=True)
        obs["duplicate_event_id_ignored"] = {"rows_added": after - before, "task_after": list(task), "ok": after - before == 1 and task[0] == "PASS" and task[1] == 0}
        # 3. late duplicate verify on a completed run → DUPLICATE
        e3 = self.event("verifier", "TASK_COMPLETED: %s-verify\nVERDICT=FAIL" % run_id)
        st = self.q("SELECT status FROM stage_events WHERE event_id=%s", (e3,), one=True)
        task = self.q("SELECT status FROM task_runs WHERE run_id=%s", (run_id,), one=True)
        obs["late_verify_after_completion"] = {"event_status": st[0], "task_after": list(task), "ok": st[0] == "DUPLICATE" and task[0] == "PASS"}
        # 4. streaming snapshot without VERDICT → PARTIAL (fresh run)
        run2 = "run-rework-%s-d2" % sfx
        self.submit(run2); self.review(run2); self.fix(run2, "attempt2")
        e4 = self.event("verifier", "TASK_COMPLETED: %s-verify\nstill running tests..." % run2)
        st = self.q("SELECT status, error FROM stage_events WHERE event_id=%s", (e4,), one=True)
        task = self.q("SELECT status, current_stage FROM task_runs WHERE run_id=%s", (run2,), one=True)
        obs["partial_without_verdict"] = {"event_status": st[0], "error": st[1], "task_after": list(task), "ok": st[0] == "PARTIAL" and task[1] == "verify"}
        # 5. TASK_SUBMITTED from a non-admin sender
        e5 = self.event("fixer", "TASK_SUBMITTED: " + json.dumps({"run_id": "run-rework-%s-rogue" % sfx, "repo": "x/y", "pr_number": 1, "branch": "b"}))
        rogue = self.q("SELECT count(*) FROM task_runs WHERE run_id=%s", ("run-rework-%s-rogue" % sfx,), one=True)[0]
        st = self.q("SELECT status FROM stage_events WHERE event_id=%s", (e5,), one=True)
        obs["submit_from_non_admin"] = {"event_status": st[0], "task_runs_created": rogue, "ok": rogue == 0}
        for k, v in obs.items():
            self.sig.append("%s:%s=%s" % (run_id, k, "ok" if v["ok"] else "FAILED"))
        return {"run_id": run_id, "checks": {k: v["ok"] for k, v in obs.items()}, "observations": obs}

    def run(self):
        sfx = self.args.run_suffix
        self.report["scenarios"]["A_rework_then_pass"] = self.scenario_a(sfx)
        self.report["scenarios"]["B_retry_cap_hold"] = self.scenario_b(sfx)
        self.report["scenarios"]["C_missing_context_blocked_escalation"] = self.scenario_c(sfx)
        self.report["scenarios"]["D_conflicts_and_invalid_inputs"] = self.scenario_d(sfx)
        # the run suffix is the only per-run token; strip it so the signature compares across runs
        self.report["outcome_signature"] = sha256_text("|".join(self.sig).replace(sfx, "SFX"))
        return self.write()

    def write(self):
        out = Path(self.args.out)
        (out / "patches").mkdir(parents=True, exist_ok=True)
        (out / "tests").mkdir(parents=True, exist_ok=True)
        a = self.report["scenarios"]["A_rework_then_pass"]
        arts = a.pop("artifacts")
        files = {}
        files["patches/%s-attempt1.diff" % a["run_id"]] = arts["patches"]["attempt1"]
        files["patches/%s-attempt2.diff" % a["run_id"]] = arts["patches"]["attempt2"]
        files["tests/%s-attempt1.log" % a["run_id"]] = arts["test_logs"]["attempt1"]
        files["tests/%s-attempt2.log" % a["run_id"]] = arts["test_logs"]["attempt2"]
        files["reviewer_findings.json"] = (CASE / "reviewer_findings.json").read_text(encoding="utf-8")
        all_checks = {sc: v["checks"] for sc, v in self.report["scenarios"].items()}
        failed = [(sc, k) for sc, cks in all_checks.items() for k, ok in cks.items() if not ok]
        self.report["summary"] = {"checks_total": sum(len(c) for c in all_checks.values()), "checks_failed": failed,
                                  "events_injected": len(self.events)}
        files["report.json"] = json.dumps(self.report, indent=2, ensure_ascii=False, default=str) + "\n"
        files["run-meta.json"] = json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "python": sys.version.split()[0],
                                             "git_head": git(["rev-parse", "HEAD"]).decode().strip(),
                                             "command": "python tools/agentteams/rework_loop_harness.py --pg-port %d --out %s" % (self.args.pg_port, _repo_rel(self.args.out)),
                                             "evidence_tier": "MECHANISM_VERIFICATION"}, indent=2) + "\n"
        for name, content in files.items():
            with open(out / name, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        sums = "".join("%s *%s\n" % (hashlib.sha256((out / n).read_bytes()).hexdigest(), n) for n in sorted(files))
        with open(out / "SHA256SUMS", "w", encoding="utf-8", newline="\n") as f:
            f.write(sums)
        print("[summary] checks=%d failed=%s events=%d signature=%s" % (self.report["summary"]["checks_total"], failed or "none", len(self.events), self.report["outcome_signature"][:16]))
        print("[evidence] %s" % out)
        return self.report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-host", default="127.0.0.1")
    ap.add_argument("--pg-port", type=int, default=55432)
    ap.add_argument("--pg-user", default="mergepilot")
    ap.add_argument("--pg-password-file", required=True)
    ap.add_argument("--audit-db", default="mergepilot_audit")
    ap.add_argument("--max-verify-attempts", type=int, default=3)
    ap.add_argument("--run-suffix", default=time.strftime("%H%M%S", time.gmtime()))
    ap.add_argument("--out", default=str(REPO_ROOT / "evidence" / "FINALS-REWORK-LOOP-20260914"))
    args = ap.parse_args()
    report = Harness(args).run()
    return 1 if report["summary"]["checks_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
