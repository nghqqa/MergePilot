"""PostgreSQL RunStore 最小纵向验收(隔离 PG 16,真实跨进程)。

门控:MERGEPILOT_PG_CONTRACT=1(未设置跳过,不伪造 PG 验证)。
"""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import unittest
from pathlib import Path

_ORCH = Path(__file__).resolve().parents[2] / "tools" / "orchestrator"
_oppkg = __import__("types").ModuleType("orchestrator_v3")
_oppkg.__path__ = [str(_ORCH)]
__import__("sys").modules["orchestrator_v3"] = _oppkg


def _load(name, pkg="orchestrator_v3"):
    full = pkg + "." + name
    if full in __import__("sys").modules:
        return __import__("sys").modules[full]
    spec = importlib.util.spec_from_file_location(full, _ORCH / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    __import__("sys").modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


stages = _load("stages")
pg_runstore = _load("pg_runstore")
console_contract = _load("console_contract")
import psycopg2  # noqa: E402

_DSN = os.environ.get(
    "MERGEPILOT_PG_TEST_DSN",
    "host=127.0.0.1 port=55432 user=mp_contract "
    "password=mp-contract-local-test dbname=mp_contract")
GATED = os.environ.get("MERGEPILOT_PG_CONTRACT") == "1"

REPO = "nghqqa/fastapi-boilerplate-demo"
HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
ISO1 = "2026-09-22T12:00:00+00:00"
ISO2 = "2026-09-22T12:05:00+00:00"


def _pg_worker_create(queue, request_key, chain, run_class):
    """独立 OS 进程:创建 run(跨进程验证)。"""
    try:
        store = pg_runstore.PgRunStore(_DSN)
        try:
            run, created = store.create_run(
                REPO, 9, HEAD, chain, run_class, request_key)
            queue.put({"run_id": run["run_id"], "created": created,
                       "exec_seq": run["exec_seq"]})
        finally:
            store.close()
    except Exception as e:
        queue.put({"error": type(e).__name__ + ":" + str(e)[:100]})


@unittest.skipUnless(GATED, "MERGEPILOT_PG_CONTRACT=1 未设置:跳过真实 PG 验证")
class PgRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = pg_runstore.PgRunStore(_DSN)
        conn = pg_runstore._connect(_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        for stmt in ("DELETE FROM run.run_events", "DELETE FROM run.stages",
                     "DELETE FROM run.runs", "DELETE FROM run.targets"):
            cur.execute(stmt)
        conn.close()

    def tearDown(self):
        self.store.close()

    def test_run_id_derivation_vectors(self):
        """设计 §3.2 向量:派生确定性、gh-+24hex、链路/类别区分。"""
        a = pg_runstore.derive_run_id("v3", "evidence", REPO, 9, HEAD, 1)
        b = pg_runstore.derive_run_id("legacy", "execution", REPO, 9, HEAD, 1)
        for rid in (a, b):
            self.assertTrue(rid.startswith("gh-"))
            self.assertEqual(len(rid), 27)
        self.assertNotEqual(a, b)
        canon = json.dumps({"v": 1, "chain": "v3", "class": "evidence",
                            "repo": REPO, "pr": 9, "head": HEAD, "seq": 1},
                           sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
        import hashlib
        self.assertEqual(a, "gh-" + hashlib.sha256(canon).hexdigest()[:24])

    def test_target_and_first_run(self):
        run, created = self.store.create_run(
            REPO, 9, HEAD, "legacy", "execution",
            "delivery-x:legacy:execution", base_sha="b" * 40)
        self.assertTrue(created)
        self.assertEqual(run["exec_seq"], 1)
        self.assertEqual(run["status"], "PENDING")
        self.assertEqual(run["target_id"],
                         pg_runstore.derive_target_id(REPO, 9, HEAD))

    def test_request_key_replay_returns_same_run(self):
        r1, c1 = self.store.create_run(REPO, 9, HEAD, "legacy", "execution",
                                       "delivery-x:legacy:execution")
        r2, c2 = self.store.create_run(REPO, 9, HEAD, "legacy", "execution",
                                       "delivery-x:legacy:execution")
        self.assertTrue(c1 and not c2)
        self.assertEqual(r1["run_id"], r2["run_id"])

    def test_active_run_blocks_duplicate_dispatch(self):
        self.store.create_run(REPO, 9, HEAD, "legacy", "execution",
                              "delivery-x:legacy:execution")
        with self.assertRaises(pg_runstore.ActiveRunExists):
            self.store.create_run(REPO, 9, HEAD, "legacy", "execution",
                                  "delivery-Y:legacy:execution")

    def test_legacy_and_shadow_coexist(self):
        self.store.create_run(REPO, 9, HEAD, "legacy", "execution",
                              "delivery-x:legacy:execution")
        shadow, created = self.store.create_run(
            REPO, 9, HEAD, "v3", "evidence", "delivery-x:v3:evidence",
            mode="shadow")
        self.assertTrue(created)
        self.assertEqual(shadow["run_class"], "evidence")
        self.assertEqual(len(self.store.runs_for_pr(REPO, 9)), 2)

    def test_same_head_rerun_preserves_history(self):
        r1, _ = self.store.create_run(REPO, 9, HEAD, "legacy", "execution",
                                      "delivery-x:legacy:execution")
        self.assertTrue(self.store.transition_status(
            r1["run_id"], "RUNNING", expected_status="PENDING"))
        st = stages.RunStages()
        st.transition("review:generic", stages.RUNNING, at=ISO1)
        st.transition("review:generic", stages.SUCCEEDED, at=ISO2)
        st.transition("publish", stages.RUNNING, at=ISO2)
        st.transition("publish", stages.SUCCEEDED, at=ISO2)
        self.store.save_stages(r1["run_id"], st)
        self.assertTrue(self.store.transition_status(
            r1["run_id"], "SUCCEEDED", expected_status="RUNNING"))
        r2, created = self.store.create_run(REPO, 9, HEAD, "legacy", "execution",
                                            "rerun-token-1",
                                            trigger_kind="manual_rerun",
                                            triggered_by="ops")
        self.assertTrue(created)
        self.assertEqual(r2["exec_seq"], 2)
        self.assertTrue(self.store.supersede_run(r1["run_id"], r2["run_id"]))
        old = self.store.get_run(r1["run_id"])
        self.assertEqual(old["superseded_by_run_id"], r2["run_id"])
        self.assertEqual(old["status"], "SUPERSEDED")
        old_stages = self.store.get_stages(old["run_id"])
        self.assertEqual(old_stages["publish"]["status"], stages.SUCCEEDED)
        self.assertEqual(old_stages["review:generic"]["status"], stages.SUCCEEDED)

    def test_stage_and_event_same_transaction_rollback(self):
        run, _ = self.store.create_run(REPO, 9, HEAD, "legacy", "execution",
                                       "delivery-x:legacy:execution")
        st = stages.RunStages()
        bad = st.record("review:generic")
        bad.status = "BOGUS"                           # 触发 DB CHECK 约束
        st.transition("risk", stages.RUNNING, at=ISO1)
        with self.assertRaises(psycopg2.Error):
            self.store.save_stages(run["run_id"], st)
        conn = pg_runstore._connect(_DSN)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM run.stages WHERE run_id=%s",
                    (run["run_id"],))
        self.assertEqual(cur.fetchone()[0], 0)         # 坏阶段回滚
        cur.execute("SELECT count(*) FROM run.run_events WHERE run_id=%s "
                    "AND event_type='stages.saved'", (run["run_id"],))
        self.assertEqual(cur.fetchone()[0], 0)         # 事件未孤立写入
        conn.close()

    def test_old_executor_cannot_overwrite_status(self):
        run, _ = self.store.create_run(REPO, 9, HEAD, "legacy", "execution",
                                       "delivery-x:legacy:execution")
        self.store.transition_status(run["run_id"], "RUNNING")
        with self.assertRaises(ValueError):
            self.store.save_stages(run["run_id"], stages.RunStages(),
                                   expected_status="PENDING")

    def test_console_read_model_consistent(self):
        run, _ = self.store.create_run(REPO, 9, HEAD, "legacy", "execution",
                                       "delivery-x:legacy:execution")
        self.store.transition_status(run["run_id"], "RUNNING")
        st = stages.RunStages()
        st.transition("review:generic", stages.RUNNING, at=ISO1)
        st.transition("review:generic", stages.SUCCEEDED, at=ISO2)
        self.store.save_stages(run["run_id"], st)
        record = self.store.get_run(run["run_id"])
        saved = stages.RunStages()
        for dim, rec in self.store.get_stages(run["run_id"]).items():
            r = saved.record(dim)
            r.status = rec["status"]
            r.attempts = rec["attempts"]
        outcome = stages.derive_outcome(saved)
        self.assertEqual(outcome["outcome"], stages.REVIEW_COMPLETED)
        self.assertEqual(record["status"], "RUNNING")
        payload = console_contract.build_console_payload(
            run_id=run["run_id"],
            risk={"level": record.get("risk_tier")},
            stages_dict=saved.to_dict(), outcome=outcome,
            aggregates=None, finding_validation=None, fixer=None,
            patch_validation=None, github_writeback=None,
            rag={"snapshot_id": None, "evidence_refs": []},
            manifest={"run_id": run["run_id"]})
        self.assertTrue(payload["review_complete"])

    def test_cross_process_concurrent_first_run(self):
        """跨进程(独立 OS 进程)同 request_key 并发首建:收敛同一 run。"""
        q = multiprocessing.get_context("spawn").Queue()
        key = "delivery-x:legacy:execution"
        procs = [multiprocessing.Process(target=_pg_worker_create,
                                         args=(q, key, "legacy", "execution"))
                 for _ in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
        results = [q.get(timeout=5) for _ in procs]
        self.assertTrue(all("error" not in r for r in results), results)
        ids = {r["run_id"] for r in results}
        self.assertEqual(len(ids), 1)
        self.assertEqual(sorted(r["created"] for r in results), [False, True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
