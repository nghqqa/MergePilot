"""架构 v3 验收矩阵测试(ARCHITECTURE-V3 §9,全本地确定性)。"""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "orchestrator"
_pkg = types.ModuleType("orchestrator_v3")
_pkg.__path__ = [str(_TOOLS)]
sys.modules["orchestrator_v3"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location("orchestrator_v3." + name,
                                                  _TOOLS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator_v3." + name] = mod
    spec.loader.exec_module(mod)
    return mod


risk = _load("risk")
stages = _load("stages")
scheduler = _load("scheduler")
aggregate = _load("aggregate")
verify_finding = _load("verify_finding")
console = _load("console_contract")
flag = _load("flag")


def _files(specs):
    return [risk.FileChange(p, a, d) for (p, a, d) in specs]


# ── 风险分级 ────────────────────────────────────────────────────────────────
class RiskRoutingTests(unittest.TestCase):
    def test_trivial_route(self):
        g = risk.grade_risk(_files([("app.py", 10, 5)]))
        self.assertEqual(g.level, risk.RISK_TRIVIAL)
        self.assertEqual(g.reviewers, ("generic",))
        self.assertFalse(g.human_review_required)

    def test_lite_route_by_lines(self):
        g = risk.grade_risk(_files([("app.py", 120, 30)]))
        self.assertEqual(g.level, risk.RISK_LITE)
        self.assertEqual(len(g.reviewers), 2)

    def test_lite_route_by_files(self):
        g = risk.grade_risk(_files([("m%d.py" % i, 2, 1) for i in range(6)]))
        self.assertEqual(g.level, risk.RISK_LITE)

    def test_full_route_by_size(self):
        g = risk.grade_risk(_files([("app.py", 600, 0)]))
        self.assertEqual(g.level, risk.RISK_FULL)
        self.assertTrue(g.human_review_required)

    def test_sensitive_path_forces_full_even_tiny_diff(self):
        """红线:小 diff 不豁免敏感路径检查。"""
        g = risk.grade_risk(_files([("app/auth/login.py", 3, 1)]))
        self.assertEqual(g.level, risk.RISK_FULL)
        self.assertEqual(g.sensitive_hits, ("app/auth/login.py",))
        self.assertTrue(g.human_review_required)
        self.assertIn("security", g.reviewers)

    def test_sensitive_full_includes_all_specialists(self):
        rules = risk.RiskRules(specialist_reviewers=("security", "deps", "migration"))
        g = risk.grade_risk(_files([("db/migration/0002.sql", 5, 2)]), rules)
        self.assertEqual(g.level, risk.RISK_FULL)
        self.assertEqual(g.reviewers, ("generic", "security", "deps", "migration"))

    def test_custom_thresholds_and_reasons(self):
        rules = risk.RiskRules(trivial_max_lines=5)
        g = risk.grade_risk(_files([("app.py", 6, 0)]), rules)
        self.assertEqual(g.level, risk.RISK_LITE)
        self.assertTrue(any("LITE" in r for r in g.reasons))  # 可解释

    def test_grade_dict_shape(self):
        g = risk.grade_risk(_files([("a.py", 1, 1)]))
        d = g.to_dict()
        for key in ("level", "reviewers", "human_review_required", "reasons",
                    "sensitive_hits", "lines_changed", "files_changed"):
            self.assertIn(key, d)


# ── 阶段状态机与 outcome ───────────────────────────────────────────────────
class StageStateTests(unittest.TestCase):
    def test_happy_transition_and_attempts(self):
        s = stages.RunStages()
        self.assertTrue(s.transition("review:generic", stages.RUNNING, at="t1"))
        self.assertTrue(s.transition("review:generic", stages.SUCCEEDED, at="t2"))
        rec = s.stages["review:generic"]
        self.assertEqual((rec.attempts, rec.status), (1, stages.SUCCEEDED))
        self.assertFalse(s.transition("review:generic", stages.RUNNING))  # 不可逆

    def test_invalid_transition_from_pending_to_succeeded(self):
        s = stages.RunStages()
        self.assertFalse(s.transition("review:generic", stages.SUCCEEDED))

    def test_retry_reentry_and_attempt_counting(self):
        """FAILED 可因重试回 RUNNING(预算守卫在执行器层,见
        test_retry_budget_exhaustion);attempts 累计每次尝试。"""
        s = stages.RunStages()
        s.transition("review:generic", stages.RUNNING, at="t1")
        s.transition("review:generic", stages.FAILED, at="t1")
        self.assertTrue(s.transition("review:generic", stages.RUNNING, at="t2"))
        s.transition("review:generic", stages.FAILED, at="t2")
        self.assertEqual(s.stages["review:generic"].attempts, 2)
        # 新维度首次置 RUNNING 同样成立(计数从 1 起)
        s2 = stages.RunStages()
        self.assertTrue(s2.transition("review:new", stages.RUNNING, at="t3"))
        self.assertEqual(s2.stages["review:new"].attempts, 1)

    def test_outcome_all_success_is_completed(self):
        s = stages.RunStages()
        for d in ("review:generic", "review:security", "finding_validation"):
            s.transition(d, stages.RUNNING)
            s.transition(d, stages.SUCCEEDED)
        out = stages.derive_outcome(s)
        self.assertEqual(out["outcome"], stages.REVIEW_COMPLETED)
        self.assertTrue(out["review_complete"])

    def test_noncritical_timeout_gives_partial_not_completed(self):
        s = stages.RunStages()
        for d in ("review:generic", "review:security"):
            s.transition(d, stages.RUNNING)
            s.transition(d, stages.SUCCEEDED)
        s.transition("review:style", stages.RUNNING)
        s.transition("review:style", stages.TIMEOUT, error="agent timeout")
        out = stages.derive_outcome(s)
        self.assertEqual(out["outcome"], stages.REVIEW_PARTIAL)
        self.assertFalse(out["review_complete"])
        self.assertIn("review:style", out["coverage_missing"])

    def test_critical_timeout_never_passes(self):
        s = stages.RunStages(critical_reviewers=("security",))
        s.transition("review:generic", stages.RUNNING)
        s.transition("review:generic", stages.SUCCEEDED)
        s.transition("review:security", stages.RUNNING)
        s.transition("review:security", stages.TIMEOUT, error="model down")
        out = stages.derive_outcome(s)
        self.assertEqual(out["outcome"], stages.MANUAL_ATTENTION)
        self.assertFalse(out["review_complete"])
        self.assertEqual(out["critical_failures"], ["review:security"])

    def test_pending_dimension_is_not_disguised(self):
        s = stages.RunStages()
        s.transition("review:generic", stages.RUNNING)
        s.transition("review:generic", stages.SUCCEEDED)
        s.record("review:security")   # PENDING 未跑
        out = stages.derive_outcome(s)
        self.assertEqual(out["outcome"], stages.MANUAL_ATTENTION)

    def test_lifecycle_stage_separation(self):
        """审查完成/已发布/待批/修复中/回写是不同阶段,逐段独立。"""
        s = stages.RunStages()
        s.transition("review:generic", stages.RUNNING)
        s.transition("review:generic", stages.SUCCEEDED)
        base = stages.derive_outcome(s)
        self.assertEqual(base["outcome"], stages.REVIEW_COMPLETED)
        pub = stages.derive_outcome(s, conclusion_published=True)
        self.assertEqual(pub["outcome"], stages.CONCLUSION_PUBLISHED)
        gate = stages.derive_outcome(s, conclusion_published=True, gate_approved=False)
        self.assertEqual(gate["outcome"], stages.AWAITING_APPROVAL)
        fixing = stages.derive_outcome(s, gate_approved=True)
        self.assertEqual(fixing["outcome"], stages.FIXING)
        pv = stages.derive_outcome(s, gate_approved=True, fix_started=True,
                                   patch_validating=True)
        self.assertEqual(pv["outcome"], stages.PATCH_VALIDATING)
        wb = stages.derive_outcome(s, patch_validating=True, writeback_ok=True)
        self.assertEqual(wb["outcome"], stages.WRITEBACK_OK)
        wbf = stages.derive_outcome(s, writeback_ok=False)
        self.assertEqual(wbf["outcome"], stages.WRITEBACK_FAILED)

    def test_pr_update_cancels_old_run(self):
        """PR 更新:旧 run 维度置 CANCELLED,不得再推进为完成。"""
        s = stages.RunStages()
        s.transition("review:generic", stages.RUNNING)
        self.assertTrue(s.transition("review:generic", stages.CANCELLED,
                                     error="new head pushed"))
        out = stages.derive_outcome(s)
        self.assertEqual(out["outcome"], stages.MANUAL_ATTENTION)
        self.assertFalse(out["review_complete"])


# ── 调度器 ──────────────────────────────────────────────────────────────────
def _grade(level=None):
    return risk.grade_risk(_files([("app/auth/x.py", 3, 1)]))  # FULL(含 security)


class SchedulerTests(unittest.TestCase):
    def test_plan_trivial_no_gate_by_default(self):
        planner = scheduler.DispatchPlanner()
        g = risk.grade_risk(_files([("app.py", 10, 2)]))
        plan = planner.build_plan(g)
        types_ = [s.step_type for s in plan]
        self.assertNotIn(scheduler.STEP_GATE, types_)
        self.assertNotIn(scheduler.STEP_FIX, types_)

    def test_fix_requires_gate_enabled(self):
        """fixer 只能在人工门正确批准后启动:门未配置 → 拒绝出计划。"""
        planner = scheduler.DispatchPlanner(scheduler.OrchestratorConfig(gate_enabled=False))
        with self.assertRaises(ValueError):
            planner.build_plan(_grade(), needs_fix=True)

    def test_plan_with_gate_includes_patch_validation(self):
        planner = scheduler.DispatchPlanner(
            scheduler.OrchestratorConfig(gate_enabled=True))
        plan = planner.build_plan(_grade(), needs_fix=True)
        types_ = [s.step_type for s in plan]
        self.assertIn(scheduler.STEP_GATE, types_)
        self.assertIn(scheduler.STEP_FIX, types_)
        self.assertIn(scheduler.STEP_PATCH_VALIDATION, types_)
        # 顺序:门 → fix → 补丁验证(补丁验证独立于 finding 验证)
        self.assertLess(types_.index(scheduler.STEP_GATE),
                        types_.index(scheduler.STEP_PATCH_VALIDATION))

    def test_two_reviewers_run_in_parallel(self):
        """并行证明:双 barrier,串行执行必有一方超时失败。"""
        barrier = threading.Barrier(2, timeout=5)
        calls = []

        def runner(step, name):
            calls.append(name)
            barrier.wait()  # 两个审查器必须同时到位
            if name == "security":
                time.sleep(0.05)  # generic 先到终点也无妨

        cfg = scheduler.OrchestratorConfig(reviewer_concurrency=2)
        ex = scheduler.SerialReviewExecutor(cfg, runner=runner)
        s = stages.RunStages()
        step = scheduler.PlanStep(scheduler.STEP_REVIEWERS,
                                  {"reviewers": ["generic", "security"],
                                   "concurrency": 2})
        res = ex.run_reviewers(step, s, now="t1")
        self.assertEqual(sorted(calls), ["generic", "security"])
        self.assertEqual(res["review:generic"], stages.SUCCEEDED)
        self.assertEqual(res["review:security"], stages.SUCCEEDED)

    def test_serial_mode_when_concurrency_one(self):
        order = []

        def runner(step, name):
            order.append("start:" + name)
            time.sleep(0.02)
            order.append("end:" + name)

        cfg = scheduler.OrchestratorConfig(reviewer_concurrency=1)
        ex = scheduler.SerialReviewExecutor(cfg, runner=runner)
        s = stages.RunStages()
        step = scheduler.PlanStep(scheduler.STEP_REVIEWERS,
                                  {"reviewers": ["generic", "security"],
                                   "concurrency": 1})
        ex.run_reviewers(step, s, now="t1")
        self.assertEqual(order, ["start:generic", "end:generic",
                                 "start:security", "end:security"])

    def test_noncritical_timeout_others_publishable(self):
        def runner(step, name):
            if name == "style":
                raise TimeoutError("agent deadline")

        ex = scheduler.SerialReviewExecutor(runner=runner)
        s = stages.RunStages()
        step = scheduler.PlanStep(scheduler.STEP_REVIEWERS,
                                  {"reviewers": ["generic", "style"],
                                   "concurrency": 2})
        res = ex.run_reviewers(step, s, now="t1")
        self.assertEqual(res["review:generic"], stages.SUCCEEDED)
        self.assertEqual(res["review:style"], stages.TIMEOUT)
        out = stages.derive_outcome(s)
        self.assertEqual(out["outcome"], stages.REVIEW_PARTIAL)  # 不伪装完整通过

    def test_critical_timeout_not_pass(self):
        def runner(step, name):
            if name == "security":
                raise TimeoutError("critical agent down")

        ex = scheduler.SerialReviewExecutor(runner=runner)
        s = stages.RunStages(critical_reviewers=("security",))
        step = scheduler.PlanStep(scheduler.STEP_REVIEWERS,
                                  {"reviewers": ["generic", "security"],
                                   "concurrency": 2})
        ex.run_reviewers(step, s, now="t1")
        out = stages.derive_outcome(s)
        self.assertEqual(out["outcome"], stages.MANUAL_ATTENTION)
        self.assertFalse(out["review_complete"])

    def test_retry_budget_exhaustion(self):
        calls = {"n": 0}

        def runner(step, name):
            calls["n"] += 1
            raise RuntimeError("model unavailable")

        cfg = scheduler.OrchestratorConfig()
        ex = scheduler.SerialReviewExecutor(cfg, runner=runner)
        s = stages.RunStages()
        step = scheduler.PlanStep(scheduler.STEP_REVIEWERS,
                                  {"reviewers": ["generic"], "concurrency": 1},
                                  max_attempts=3)
        res = ex.run_reviewers(step, s, now="t1")
        self.assertEqual(calls["n"], 3)                       # 预算内重试
        self.assertEqual(res["review:generic"], stages.FAILED)
        self.assertEqual(s.stages["review:generic"].attempts, 3)

    def test_retry_then_success(self):
        calls = {"n": 0}

        def runner(step, name):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")

        ex = scheduler.SerialReviewExecutor(runner=runner)
        s = stages.RunStages()
        step = scheduler.PlanStep(scheduler.STEP_REVIEWERS,
                                  {"reviewers": ["generic"], "concurrency": 1},
                                  max_attempts=2)
        res = ex.run_reviewers(step, s, now="t1")
        self.assertEqual(res["review:generic"], stages.SUCCEEDED)
        self.assertEqual(s.stages["review:generic"].attempts, 2)

    def test_global_budget_hook_blocks(self):
        """全部并发任务共享全局预算:预算耗尽 → 阶段 FAILED,不无预算执行。"""
        from unittest import mock

        def budget():
            raise RuntimeError("BudgetExceeded: 预算耗尽")

        ex = scheduler.SerialReviewExecutor(runner=lambda s, n: None,
                                            budget_hook=budget)
        s = stages.RunStages()
        step = scheduler.PlanStep(scheduler.STEP_REVIEWERS,
                                  {"reviewers": ["generic"], "concurrency": 1},
                                  max_attempts=1)
        res = ex.run_reviewers(step, s, now="t1")
        self.assertEqual(res["review:generic"], stages.FAILED)
        self.assertIn("BudgetExceeded", s.stages["review:generic"].error)

    def test_global_pr_concurrency_constant(self):
        self.assertEqual(scheduler.MAX_PR_CONCURRENCY, 1)  # 授权前不得调高


# ── 聚合 ────────────────────────────────────────────────────────────────────
class AggregateTests(unittest.TestCase):
    def _f(self, fid, src, cat, sev, title, path, line=10):
        return aggregate.Finding(fid, src, cat, sev, title, path, line)

    def test_exact_dedupe_keeps_all_sources(self):
        fs = [self._f("f1", "generic", "path-traversal", "HIGH", "Path traversal in upload", "api/upload.py"),
              self._f("f2", "security", "path-traversal", "HIGH", "Path traversal in upload", "api/upload.py")]
        out = aggregate.aggregate_findings(fs)
        self.assertEqual(len(out.findings), 1)
        self.assertEqual(out.dropped_duplicates, 1)
        self.assertEqual(sorted(s["reviewer"] for s in out.findings[0].sources),
                         ["generic", "security"])  # 来源保留

    def test_near_duplicate_merged_by_title_similarity(self):
        fs = [self._f("f1", "generic", "cmd-injection", "HIGH",
                      "OS command injection via user input", "api/exec.py"),
              self._f("f2", "security", "cmd-injection", "HIGH",
                      "command injection via user input", "api/exec.py")]
        out = aggregate.aggregate_findings(fs)
        self.assertEqual(len(out.findings), 1)

    def test_distinct_findings_not_merged(self):
        fs = [self._f("f1", "generic", "cmd-injection", "HIGH", "Command injection in exec", "api/exec.py"),
              self._f("f2", "security", "path-traversal", "HIGH", "Path traversal in download", "api/download.py")]
        out = aggregate.aggregate_findings(fs)
        self.assertEqual(len(out.findings), 2)
        self.assertEqual(out.dropped_duplicates, 0)

    def test_severity_max_and_ranking(self):
        fs = [self._f("f1", "generic", "xss", "LOW", "Reflected XSS output", "web/view.py"),
              self._f("f2", "security", "xss", "HIGH", "Reflected XSS output", "web/view.py"),
              self._f("f3", "generic", "typos", "INFO", "Minor typo issues", "docs/x.md")]
        out = aggregate.aggregate_findings(fs)
        self.assertEqual(out.findings[0].severity, "HIGH")   # 高严重度优先
        self.assertEqual(out.findings[-1].severity, "INFO")

    def test_same_path_different_category_not_merged(self):
        fs = [self._f("f1", "generic", "cmd-injection", "HIGH", "Injection risk", "api/x.py"),
              self._f("f2", "security", "path-traversal", "HIGH", "Traversal risk", "api/x.py")]
        out = aggregate.aggregate_findings(fs)
        self.assertEqual(len(out.findings), 2)


# ── finding verifier 隔离 ──────────────────────────────────────────────────
class VerifierIsolationTests(unittest.TestCase):
    def test_input_rejects_reasoning_fields(self):
        """结构性隔离:携带其他 agent 推理的输入在构造期即拒绝。"""
        with self.assertRaises(ValueError):
            verify_finding.VerifierInput(
                finding={"title": "x", "reasoning": "reviewer 的内部推理"},
                diff="--- a\n+++ b")

    def test_base_verifier_has_no_access_path(self):
        """接口类型上不存在推理字段;基类未实现即拒绝(真实适配待授权)。"""
        inp = verify_finding.VerifierInput(
            finding={"title": "x", "path": "a.py"}, diff="diff", allowed_context_paths=("a.py",))
        v = verify_finding.FindingVerifier()
        with self.assertRaises(NotImplementedError):
            v.verify(inp)
        # 输入字段枚举:仅 finding/diff/allowed_context_paths
        self.assertEqual(set(inp.__dataclass_fields__), 
                         {"finding", "diff", "allowed_context_paths"})

    def test_verdict_validation(self):
        with self.assertRaises(ValueError):
            verify_finding.VerifierOutput(verdict="MAYBE")


# ── 控制台契约 ──────────────────────────────────────────────────────────────
class ConsoleContractTests(unittest.TestCase):
    def _payload(self, security_status=None):
        s = stages.RunStages(critical_reviewers=("security",))
        s.transition("review:generic", stages.RUNNING)
        s.transition("review:generic", stages.SUCCEEDED)
        if security_status:
            s.transition("review:security", stages.RUNNING)
            s.transition("review:security", security_status, error="model down")
        out = stages.derive_outcome(s)
        return console.build_console_payload(
            run_id="run-1",
            risk=risk.grade_risk(_files([("app/auth/x.py", 3, 1)])).to_dict(),
            stages_dict=s.to_dict(), outcome=out, aggregates=None,
            finding_validation=None, fixer=None, patch_validation=None,
            github_writeback=None, rag={"snapshot_id": "fd34"}, manifest={"run_id": "run-1"})

    def test_partial_completion_visible_not_single_green(self):
        p = self._payload(stages.TIMEOUT)
        self.assertEqual(p["outcome"], stages.MANUAL_ATTENTION)
        self.assertFalse(p["coverage"]["complete"])
        self.assertEqual(p["coverage"]["critical_failures"], ["review:security"])
        sec = [r for r in p["reviewers"] if r["name"] == "security"][0]
        self.assertEqual(sec["status"], stages.TIMEOUT)
        self.assertTrue(any(d["stage"] == "review:security" for d in p["degradations"]))

    def test_contract_fields_present(self):
        p = self._payload()
        for key in ("run_id", "risk_level", "reviewers", "findings",
                    "finding_validation", "outcome", "review_complete", "coverage",
                    "degradations", "fixer", "patch_validation", "github_writeback",
                    "rag", "manifest"):
            self.assertIn(key, p)

    def test_rag_snapshot_in_console(self):
        p = self._payload()
        self.assertEqual(p["rag"]["snapshot_id"], "fd34")


# ── flag ────────────────────────────────────────────────────────────────────
class FlagTests(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(flag.review_v3_enabled({}))

    def test_env_on(self):
        self.assertTrue(flag.review_v3_enabled({"MERGEPILOT_REVIEW_V3": "1"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
