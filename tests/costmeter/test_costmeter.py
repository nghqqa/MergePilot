"""成本计量脚手架单元测试(纯逻辑,零 IO)。

对应提示词七.C 的 M4 前验证清单,逐项:
预留 → 重试计入 → 结算 → 超限阻止 → 并发预留不重复 → usage 缺失/崩溃处理。
注意:这是 SCAFFOLD 的逻辑层验证;真实调用路径接入与端到端验证仍为 M4 前工作。
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import unittest
from pathlib import Path


def _load_core():
    p = Path(__file__).resolve().parents[2] / "tools" / "costmeter" / "core.py"
    spec = importlib.util.spec_from_file_location("costmeter_core", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load_core()


class ReserveTests(unittest.TestCase):
    def test_reserve_and_remaining(self):
        g = core.BudgetGuard(run_id="r1", limit=1000)
        rid = g.reserve(400)
        self.assertEqual(g.status()["remaining"], 600)
        self.assertEqual(g.status()["active_reservations"], 1)
        g.release(rid)
        self.assertEqual(g.status()["remaining"], 1000)

    def test_over_limit_denied(self):
        g = core.BudgetGuard(run_id="r1", limit=100)
        with self.assertRaises(core.BudgetExceeded):
            g.reserve(200)
        rid = g.reserve(60)
        with self.assertRaises(core.BudgetExceeded):
            g.reserve(60)  # 余 40,不足
        g.release(rid)

    def test_limit_must_be_explicit(self):
        with self.assertRaises(ValueError):
            core.BudgetGuard(run_id="r1", limit=0)
        with self.assertRaises(ValueError):
            core.BudgetGuard(run_id="r1", limit=-5)

    def test_retry_reuses_reservation(self):
        """重试沿用原预留:额度不被重复占用。"""
        g = core.BudgetGuard(run_id="r1", limit=1000)
        rid = g.reserve(300)
        rid2 = g.reserve(300, retry_of=rid)
        self.assertEqual(rid2, rid)
        self.assertEqual(g.status()["remaining"], 700)
        self.assertEqual(g.status()["active_reservations"], 1)
        r = g.commit(rid, actual=250)
        self.assertEqual(r["consumed"], 250)
        self.assertEqual(r["remaining"], 750)  # 300 预留,实耗 250,退 50

    def test_retry_reissued_call_commits_cumulative_cost(self):
        """预留去重 ≠ 成本去重:重试真实重发的用量必须累计结算,不得漏计。"""
        g = core.BudgetGuard(run_id="r1", limit=1000)
        rid = g.reserve(300)
        g.reserve(300, retry_of=rid)          # 第 2 次尝试沿用同一预留
        # 两次真实模型调用:尝试1 耗 200(中途失败也计费),尝试2 耗 250
        r = g.commit(rid, actual=200 + 250)   # 结算必须传累计值
        self.assertEqual(r["consumed"], 450)
        self.assertEqual(r["remaining"], 550)

    def test_retry_of_settled_reservation_rejected(self):
        g = core.BudgetGuard(run_id="r1", limit=1000)
        rid = g.reserve(100)
        g.commit(rid, actual=100)
        with self.assertRaises(ValueError):
            g.reserve(100, retry_of=rid)

    def test_commit_under_estimate_refunds(self):
        g = core.BudgetGuard(run_id="r1", limit=1000)
        rid = g.reserve(500)
        r = g.commit(rid, actual=200)
        self.assertEqual(r["consumed"], 200)
        self.assertEqual(r["remaining"], 800)  # 1000-200:预留 500 已退 300

    def test_commit_over_estimate_within_budget(self):
        g = core.BudgetGuard(run_id="r1", limit=1000)
        rid = g.reserve(500)
        r = g.commit(rid, actual=800)
        self.assertEqual(r["remaining"], 200)

    def test_commit_over_estimate_beyond_budget_denies(self):
        """实际用量超预留且超余额:拒绝(超限阻止的结算面)。"""
        g = core.BudgetGuard(run_id="r1", limit=600)
        rid = g.reserve(500)
        with self.assertRaises(core.BudgetExceeded):
            g.commit(rid, actual=700)

    def test_usage_missing_consumes_estimate_and_records_gap(self):
        """usage 缺失:按预留额消耗 + 记 gap(保守,不静默放行也不虚减)。"""
        g = core.BudgetGuard(run_id="r1", limit=1000)
        rid = g.reserve(300)
        r = g.commit(rid, actual=None)
        self.assertTrue(r["usage_missing"])
        self.assertEqual(r["gaps"], 1)
        self.assertEqual(r["remaining"], 700)


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_reserves_never_oversubscribe(self):
        """并发预留不重复使用同一额度:总量永不超限。"""
        g = core.BudgetGuard(run_id="r1", limit=1000)
        denied = []

        def worker():
            try:
                g.reserve(100)
            except core.BudgetExceeded:
                denied.append(1)

        ts = [threading.Thread(target=worker) for _ in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(denied), 10)   # 恰好 10 个成功、10 个拒绝
        self.assertEqual(g.status()["remaining"], 0)
        self.assertEqual(g.status()["active_reservations"], 10)


class CrashRecoveryTests(unittest.TestCase):
    def test_ledger_survives_restart(self):
        import tempfile, os
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ledger.json")
        g = core.BudgetGuard(run_id="r1", limit=1000, ledger_path=path)
        g.reserve(400)
        g2 = core.BudgetGuard(run_id="r1", limit=1000, ledger_path=path)
        self.assertEqual(g2.status()["remaining"], 600)
        self.assertEqual(g2.status()["active_reservations"], 1)

    def test_stale_reservation_reclaimed(self):
        """崩溃残留:过时预留回收退额(重试/接管方能继续)。"""
        import tempfile, os
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ledger.json")
        g = core.BudgetGuard(run_id="r1", limit=1000, ledger_path=path,
                             stale_after_s=60)
        g.reserve(900, now=1000.0)
        g2 = core.BudgetGuard(run_id="r1", limit=1000, ledger_path=path,
                              stale_after_s=60)
        r = g2.reserve(500, now=1200.0)   # 旧预留已 stale,回收后可预留
        self.assertIsNotNone(r)
        self.assertEqual(g2.status()["remaining"], 500)


class SummarizeTests(unittest.TestCase):
    def test_summarize_counts_and_missing(self):
        recs = [
            core.UsageRecord(run_id="r1", role="reviewer", calls=12,
                             input_tokens=100, output_tokens=50, model="m1"),
            core.UsageRecord(run_id="r1", role="fixer", calls=3, missing=True,
                             source="span-count"),
        ]
        s = core.summarize(recs)
        self.assertEqual(s["runs"]["r1"]["calls"], 15)
        self.assertEqual(s["runs"]["r1"]["roles"]["reviewer"]["input_tokens"], 100)
        self.assertEqual(s["runs"]["r1"]["roles"]["fixer"]["missing"], 3)
        self.assertEqual(s["missing_usage_records"], 1)
        self.assertNotIn("cost", s)  # 无价目表 → 无币值(不伪造)

    def test_cost_only_with_explicit_price_table(self):
        recs = [core.UsageRecord(run_id="r1", role="reviewer", calls=1,
                                 input_tokens=1000, output_tokens=500, model="m1")]
        s = core.summarize(recs, prices={"m1": {"input_per_1k": 2.0,
                                                "output_per_1k": 8.0}})
        self.assertEqual(s["cost"]["r1"], 2.0 + 4.0)

    def test_unknown_model_never_priced(self):
        recs = [core.UsageRecord(run_id="r1", role="reviewer", calls=1,
                                 input_tokens=1000, output_tokens=0, model="mystery")]
        s = core.summarize(recs, prices={"m1": {"input_per_1k": 2.0,
                                                "output_per_1k": 8.0}})
        self.assertNotIn("r1", s["cost"])
        self.assertEqual(s["cost"]["unknown_models"], ["mystery"])


class CollectorTests(unittest.TestCase):
    def _collect(self):
        import types
        tools = Path(__file__).resolve().parents[2] / "tools" / "costmeter"
        pkg = types.ModuleType("costmeter_pkg")
        pkg.__path__ = [str(tools)]
        sys.modules["costmeter_pkg"] = pkg
        cspec = importlib.util.spec_from_file_location("costmeter_pkg.core",
                                                       tools / "core.py")
        cmod = importlib.util.module_from_spec(cspec)
        sys.modules["costmeter_pkg.core"] = cmod
        cspec.loader.exec_module(cmod)
        uspec = importlib.util.spec_from_file_location("costmeter_pkg.collect",
                                                       tools / "collect.py")
        col = importlib.util.module_from_spec(uspec)
        sys.modules["costmeter_pkg.collect"] = col
        uspec.loader.exec_module(col)
        return col

    def test_collect_marks_tokens_unavailable(self):
        col = self._collect()

        def fake_reader(cname):
            return {"elemiso-worker-reviewer": ["s1", "s2"]}.get(cname)

        recs = col.collect_span_counts("r1", {"reviewer": "elemiso-worker-reviewer",
                                              "ghost": "elemiso-worker-ghost"},
                                       reader=fake_reader)
        self.assertEqual(recs[0].calls, 2)
        self.assertTrue(all(r.missing for r in recs))
        report = col.build_usage_report(recs, {"limit": 1})
        self.assertTrue(report["scaffold"])
        self.assertIn("unavailable", report["token_source"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
