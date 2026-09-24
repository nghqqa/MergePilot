"""M3.5 纵向链路集成测试(fixture 管道)与 shadow 诚实性测试。

链路:PR fixture → 固定 head → risk classify → reviewer plan → reviewer slots
→ aggregate → finding validation → derive outcome → run store → console read model。
shadow 测试证明:只读、无外部写、无 Agent、不伪装审查完成。
全部本地;不冒充真实 Agent 验证。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "orchestrator"
_pkg = types.ModuleType("orchestrator_v3")
_pkg.__path__ = [str(_TOOLS)]
sys.modules.setdefault("orchestrator_v3", _pkg)


def _load(name):
    if "orchestrator_v3." + name in sys.modules:
        return sys.modules["orchestrator_v3." + name]
    spec = importlib.util.spec_from_file_location("orchestrator_v3." + name,
                                                  _TOOLS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator_v3." + name] = mod
    spec.loader.exec_module(mod)
    return mod


stages = _load("stages")
runstore = _load("runstore")
adapter = _load("adapter")
stages_risk = _load("risk")


def _delivery(head="a" * 40, pr=2):
    return {"delivery_id": "d-" + head[:10], "repo": "team/demo", "pr_number": pr,
            "observed_head_sha": head, "observed_base_sha": "b" * 40,
            "action": "synchronize"}


def _f(fid, src, cat, sev, title, path):
    return {"finding_id": fid, "category": cat, "severity": sev,
            "title": title, "path": path, "line": 7, "evidence": "poc"}


AUTH_FILES = [("app/auth/login.py", 200, 30), ("app/util.py", 20, 3)]


class VerticalChainTests(unittest.TestCase):
    """fixture 纵向链路(本地假审查器;mode=fixture,永不冒充真实运行)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = runstore.RunStore(self.tmp.name + "/runs.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _two_reviewer_findings(self):
        return {
            "generic": [_f("f1", "generic", "path-traversal", "HIGH",
                           "Path traversal in upload handler", "api/upload.py"),
                        _f("f3", "generic", "style", "LOW",
                           "Naming convention deviation", "api/names.py")],
            "security": [_f("f2", "security", "path-traversal", "HIGH",
                            "Path traversal in upload handler", "api/upload.py")],
        }

    def test_full_chain_two_reviewers_dedupe_validation_outcome(self):
        ev = adapter.run_v3_fixture(
            _delivery(), run_store=self.store,
            reviewer_findings=self._two_reviewer_findings(),
            files=AUTH_FILES, rag_snapshot="fd34" * 8)
        # 档位:敏感路径 → FULL,双审查器
        self.assertEqual(ev["risk"]["level"], "FULL")
        self.assertTrue(ev["risk"]["human_review_required"])
        self.assertEqual(sorted(ev["plan"][0]["payload"]["reviewers"]),
                         ["generic", "security"])
        # 审查器槽位全部成功(并行执行)
        self.assertEqual(ev["stages"]["review:generic"]["status"], "SUCCEEDED")
        self.assertEqual(ev["stages"]["review:security"]["status"], "SUCCEEDED")
        # 聚合:两条重复 findings 合一,来源保留
        self.assertEqual(ev["aggregates"]["total"], 2)      # traversal + style
        self.assertEqual(ev["aggregates"]["dropped_duplicates"], 1)
        trav = [f for f in ev["aggregates"]["findings"]
                if f["category"] == "path-traversal"][0]
        self.assertEqual(sorted(s["reviewer"] for s in trav["sources"]),
                         ["generic", "security"])
        # finding validation:HIGH→CONFIRMED, LOW→REFUTED(fixture 默认验证器)
        self.assertEqual(ev["finding_validation"]["status"], "SUCCEEDED")
        self.assertEqual(ev["finding_validation"]["confirmed"], 1)
        self.assertEqual(ev["finding_validation"]["refuted"], 1)
        # outcome:全维度成功 → REVIEW_COMPLETED(本地 fixture 语义)
        self.assertEqual(ev["outcome"]["outcome"], stages.REVIEW_COMPLETED)
        self.assertTrue(ev["outcome"]["review_complete"])
        # 持久化 + 控制台读模型一致
        rec = self.store.get_run(ev["run_id"])
        self.assertIsNotNone(rec)
        self.assertEqual(rec["mode"], "fixture")
        rm = adapter.build_read_model(rec)
        self.assertEqual(rm["outcome"], stages.REVIEW_COMPLETED)
        self.assertEqual(rm["rag"]["snapshot_id"], "fd34" * 8)
        self.assertEqual(len(rm["reviewers"]), 2)
        self.assertEqual(rm["findings"]["by_source"],
                         {"generic": 2, "security": 1})
        self.assertEqual(rm["manifest"]["manifest_hash"], ev["manifest_hash"])

    def test_trivial_chain_single_reviewer(self):
        ev = adapter.run_v3_fixture(
            _delivery(head="c" * 40), run_store=self.store,
            reviewer_findings={"generic": [_f("f1", "generic", "style", "LOW",
                                              "Minor issue", "app.py")]},
            files=[("app.py", 10, 2)])
        self.assertEqual(ev["risk"]["level"], "TRIVIAL")
        self.assertEqual(ev["plan"][0]["payload"]["reviewers"], ["generic"])

    def test_noncritical_reviewer_failure_partial(self):
        ev = adapter.run_v3_fixture(
            _delivery(head="d" * 40), run_store=self.store,
            reviewer_findings=self._two_reviewer_findings(),
            files=AUTH_FILES, reviewer_fail={"generic": "failed"})
        self.assertEqual(ev["stages"]["review:generic"]["status"], stages.FAILED)
        self.assertEqual(ev["stages"]["review:security"]["status"], stages.SUCCEEDED)
        self.assertEqual(ev["outcome"]["outcome"], stages.REVIEW_PARTIAL)
        self.assertIn("review:generic", ev["outcome"]["coverage_missing"])

    def test_critical_reviewer_timeout_never_passes(self):
        ev = adapter.run_v3_fixture(
            _delivery(head="e" * 40), run_store=self.store,
            reviewer_findings=self._two_reviewer_findings(),
            files=AUTH_FILES, reviewer_fail={"security": "timeout"})
        self.assertEqual(ev["stages"]["review:security"]["status"], stages.TIMEOUT)
        self.assertEqual(ev["outcome"]["outcome"], stages.MANUAL_ATTENTION)
        self.assertFalse(ev["outcome"]["review_complete"])
        self.assertEqual(ev["outcome"]["critical_failures"], ["review:security"])

    def test_verifier_input_free_of_reasoning(self):
        seen = []

        def verify(finding):
            seen.append(finding)
            return "CONFIRMED"

        adapter.run_v3_fixture(
            _delivery(head="f" * 40), run_store=self.store,
            reviewer_findings=self._two_reviewer_findings(),
            files=AUTH_FILES, verify_fn=verify)
        for finding in seen:
            self.assertNotIn("reasoning", finding)
            self.assertNotIn("transcript", finding)
            # 构造 verifier 输入也必须合法(结构性隔离贯穿)
            verify_finding = _load("verify_finding")
            verify_finding.VerifierInput(finding=finding, diff="--- a\n+++ b")

    def test_budget_exhaustion_blocks_plan(self):
        def budget():
            raise RuntimeError("BudgetExceeded: 限额耗尽")

        ev = adapter.run_v3_fixture(
            _delivery(head="1" * 40), run_store=self.store,
            reviewer_findings=self._two_reviewer_findings(),
            files=AUTH_FILES, budget_hook=budget)
        self.assertEqual(ev["stages"]["plan"]["status"], stages.FAILED)
        self.assertIn("BudgetExceeded", ev["stages"]["plan"]["error"])
        self.assertTrue(any("plan" in r for r in ev["downgrade_reasons"]))

    def test_pr_update_supersedes_old_run(self):
        """PR 更新:旧 head run 被取代标记,不再被当成现行 run。"""
        ev1 = adapter.run_v3_fixture(
            _delivery(head="a" * 40), run_store=self.store,
            reviewer_findings=self._two_reviewer_findings(), files=AUTH_FILES)
        ev2 = adapter.run_v3_fixture(
            _delivery(head="9" * 40), run_store=self.store,
            reviewer_findings=self._two_reviewer_findings(), files=AUTH_FILES)
        self.assertNotEqual(ev1["run_id"], ev2["run_id"])
        old = self.store.get_run(ev1["run_id"])
        self.assertTrue(old["superseded"])
        self.assertFalse(self.store.get_run(ev2["run_id"])["superseded"])


class ShadowHonestyTests(unittest.TestCase):
    """shadow 模式:真实 PR 形状数据 + 零 Agent + 零外部写。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = runstore.RunStore(self.tmp.name + "/runs.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_shadow_with_real_shape_diff_readonly(self):
        calls = []

        def fetcher(repo, pr):
            calls.append((repo, pr))
            return [stages_risk.FileChange("app/auth/login.py", 80, 10),
                stages_risk.FileChange("README.md", 2, 0)]

        ev = adapter.run_v3_shadow(_delivery(), run_store=self.store,
                                   rag_snapshot="fd34" * 8,
                                   diff_fetcher=fetcher)
        self.assertEqual(calls, [("team/demo", 2)])     # 一次只读 GET
        self.assertEqual(ev["external_writes"], "none")
        self.assertEqual(ev["github_calls"], ["GET pulls/files (read-only)"])
        self.assertEqual(ev["risk"]["level"], "FULL")   # 敏感路径命中
        # 审查器 SKIPPED(shadow 不执行),绝不伪装已审查
        self.assertEqual(ev["stages"]["review:generic"]["status"], stages.SKIPPED)
        self.assertEqual(ev["stages"]["review:security"]["status"], stages.SKIPPED)
        # 关键审查器被跳过 ⇒ 诚实派生 MANUAL_ATTENTION(shadow 永不产生"完成")
        self.assertEqual(ev["outcome"]["outcome"], stages.MANUAL_ATTENTION)
        self.assertFalse(ev["outcome"]["review_complete"])
        self.assertEqual(ev["outcome"]["critical_failures"], ["review:security"])
        self.assertIn("shadow", ev["downgrade_reasons"][0])
        rec = self.store.get_run(ev["run_id"])
        self.assertEqual(rec["mode"], "shadow")
        self.assertGreaterEqual(rec["downgrade_reason"].count("shadow"), 2)  # 审查器跳过

    def test_shadow_diff_unavailable_degrades_without_fabrication(self):
        """diff 取不到:档位显式 FAILED,不猜测档位、不伪造计划。"""
        ev = adapter.run_v3_shadow(_delivery(head="e" * 40), run_store=self.store,
                                   diff_fetcher=lambda repo, pr: None)
        self.assertEqual(ev["risk"], None)
        self.assertEqual(ev["stages"]["risk"]["status"], stages.FAILED)
        self.assertEqual(ev["stages"]["plan"]["status"], stages.SKIPPED)
        self.assertTrue(any("档位未知" in r for r in ev["downgrade_reasons"]))

    def test_shadow_pr_update_supersedes_and_cancels(self):
        ev1 = adapter.run_v3_shadow(_delivery(head="a" * 40), run_store=self.store,
                                    diff_fetcher=lambda r, p: [stages_risk.FileChange("x.py", 1, 1)])
        ev2 = adapter.run_v3_shadow(_delivery(head="7" * 40), run_store=self.store,
                                    diff_fetcher=lambda r, p: [stages_risk.FileChange("x.py", 1, 1)])
        old = self.store.get_run(ev1["run_id"])
        self.assertTrue(old["superseded"])
        # 取代语义单元验证:RUNNING/PENDING 维度被状态机置 CANCELLED,
        # 已完成维度保留历史状态(superseded 标记承担"整 run 失效")
        runstore_rec = {"run_id": "old", "repo": "team/demo", "pr_number": 2,
                        "head_sha": "0" * 40, "mode": "shadow",
                        "superseded": 0, "updated_at": "t0", "created_at": "t0",
                        "stages_json": json.dumps({
                            "review:generic": {"status": "RUNNING", "attempts": 1},
                            "risk": {"status": "SUCCEEDED", "attempts": 1,
                                     "error": "seeded error"}}),
        }
        self.store.save_run(runstore_rec)
        adapter._supersede_old_runs(self.store, "team/demo", 2, "8" * 40, "t5")
        rec = self.store.get_run("old")
        self.assertEqual(rec["stages"]["review:generic"]["status"], stages.CANCELLED)
        self.assertEqual(rec["stages"]["risk"]["status"], stages.SUCCEEDED)
        # 保真:已完成维度的 error 字段不因取代而丢失
        self.assertEqual(rec["stages"]["risk"]["error"], "seeded error")

    def test_shadow_budget_hook_recorded(self):
        ev = adapter.run_v3_shadow(_delivery(head="3" * 40), run_store=self.store,
                                   diff_fetcher=lambda r, p: [stages_risk.FileChange("x.py", 1, 1)],
                                   budget_hook=lambda: None)
        self.assertEqual(ev["budget"], "hook consulted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
