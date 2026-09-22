"""RunStore 持久化测试(第 1 层):幂等 UPSERT / 重启恢复 / 取代标记。"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
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


runstore = _load("runstore")


def _record(run_id="run-1", head="a" * 40, pr=2):
    return {"run_id": run_id, "repo": "team/demo", "pr_number": pr,
            "head_sha": head, "base_sha": "b" * 40, "mode": "shadow",
            "risk_tier": "FULL", "risk_json": {"level": "FULL"},
            "plan_json": [{"step_type": "reviewers"}],
            "stages_json": {"risk": {"status": "SUCCEEDED"}},
            "outcome_json": {"outcome": "REVIEW_PARTIAL",
                             "coverage_missing": ["review:generic"]},
            "coverage_missing": ["review:generic"],
            "downgrade_reason": "review:generic skipped(shadow)",
            "finding_validation": "NOT_APPLICABLE",
            "patch_validation": "NOT_APPLICABLE",
            "rag_snapshot": "fd34", "manifest_hash": "h" * 64,
            "created_at": "t0", "updated_at": "t1"}


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "runs.db")
        self.store = runstore.RunStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_save_get_roundtrip(self):
        self.store.save_run(_record())
        rec = self.store.get_run("run-1")
        self.assertEqual(rec["repo"], "team/demo")
        self.assertEqual(rec["risk_tier"], "FULL")
        self.assertEqual(rec["risk"]["level"], "FULL")          # JSON 反序列化
        self.assertEqual(rec["outcome"]["outcome"], "REVIEW_PARTIAL")
        self.assertEqual(rec["coverage_missing"], ["review:generic"])

    def test_upsert_idempotent_no_duplicate_rows(self):
        """相同 run 重复写入:更新而非新增(无界重复不产生)。"""
        self.store.save_run(_record())
        r2 = _record()
        r2["updated_at"] = "t2"
        self.store.save_run(r2)
        self.assertEqual(len(self.store.runs_for_pr("team/demo", 2)), 1)
        self.assertEqual(self.store.get_run("run-1")["updated_at"], "t2")

    def test_restart_recovery(self):
        """崩溃恢复:关闭连接后重开,已提交状态完整保留。"""
        self.store.save_run(_record())
        self.store.close()
        reopened = runstore.RunStore(self.path)
        try:
            rec = reopened.get_run("run-1")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["manifest_hash"], "h" * 64)
        finally:
            reopened.close()

    def test_mark_superseded_once(self):
        self.store.save_run(_record())
        self.assertTrue(self.store.mark_superseded("run-1", "t9"))
        self.assertFalse(self.store.mark_superseded("run-1", "t9"))  # 幂等
        self.assertTrue(self.store.get_run("run-1")["superseded"])

    def test_list_and_pr_filter(self):
        self.store.save_run(_record("run-1", pr=2))
        self.store.save_run(_record("run-2", pr=3, head="c" * 40))
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual([r["run_id"] for r in self.store.runs_for_pr("team/demo", 3)],
                         ["run-2"])

    def test_evidence_hash_deterministic(self):
        h1 = runstore.evidence_hash({"a": 1, "b": [1, 2]})
        h2 = runstore.evidence_hash({"b": [1, 2], "a": 1})
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
