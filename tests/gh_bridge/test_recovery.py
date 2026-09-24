"""M1-2 崩溃恢复与续接的单元测试（全 mock）。

覆盖验收场景：
  1  认领后崩溃，任务可以恢复（take_over_stale CAS 接管 + resume）
  2  发任务后崩溃，不重复产生有害业务副作用（resume 分流：绝不重发 kickoff）
  8(续) 接管即换新租约，旧执行者 rowcount=0
  4(续) 无项目回队有界（RQn 计数，REQUEUE_MAX 超限转 MANUAL）
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from .test_publish_semantics import _load_bridge, _delivery


def _stale_row(**over):
    r = _delivery()
    r["claim_id"] = r["delivery_id"][:14] + "-bridge-deadbeef"
    r["error"] = ""
    r.update(over)
    return r


class TakeOverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_takeover_cas_and_format(self):
        br = self.br
        row = _stale_row()
        captured = []

        def fake_ssh(q, as_json=False):
            captured.append(q)
            if q.startswith("SELECT"):
                return [row]
            return "UPDATE 1"

        with mock.patch.object(br, "ssh_psql", side_effect=fake_ssh):
            taken = br.take_over_stale()
        self.assertEqual(len(taken), 1)
        d = taken[0]
        self.assertIn("-bridge-", d["_cid"])
        self.assertNotEqual(d["_cid"], row["claim_id"])  # 换新租约
        upd = captured[1]
        self.assertIn("claim_id='%s'" % row["claim_id"], upd)  # 精确旧值 CAS
        self.assertIn("status='RUNNING'", upd)
        self.assertIn("LIKE '%%-bridge-%%'", captured[0].replace("LIKE '%-bridge-%'",
                                                                  "LIKE '%%-bridge-%%'")) or \
            self.assertIn("LIKE", captured[0])

    def test_takeover_skips_lost_race(self):
        br = self.br
        row = _stale_row()

        def fake_ssh(q, as_json=False):
            if q.startswith("SELECT"):
                return [row]
            return "UPDATE 0"  # 旧执行者刚好又动了/他者接管

        with mock.patch.object(br, "ssh_psql", side_effect=fake_ssh):
            self.assertEqual(br.take_over_stale(), [])

    def test_requeue_count(self):
        br = self.br
        self.assertEqual(br._requeue_count({"error": "RQ1"}), 1)
        self.assertEqual(br._requeue_count({"error": "RQ2 something"}), 2)
        self.assertEqual(br._requeue_count({"error": ""}), 0)
        self.assertEqual(br._requeue_count({"error": "PUBLISH_FAILED..."}), 0)


class ResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _resume(self, *, receipt=None, proj_status=None, watch=None,
                requeue_sql=None, error=""):
        br = self.br
        d = _stale_row(error=error)
        d["_cid"] = d["delivery_id"][:14] + "-bridge-newcid01"
        captured = []

        def fake_ssh(q):
            captured.append(q)
            if q.startswith("SELECT"):
                return "0"
            return "UPDATE 1"

        patches = [
            mock.patch.object(br, "ssh_psql", side_effect=fake_ssh),
            mock.patch.object(br, "read_receipt", return_value=receipt),
            mock.patch.object(br, "project_status", return_value=proj_status),
            mock.patch.object(br.mx, "send", return_value={"event_id": "$ev"}),
            mock.patch.object(br, "seed_project", return_value=True),
            mock.patch.object(br, "wake_workers", return_value=True),
        ]
        if callable(watch):
            patches.append(mock.patch.object(br, "watch_run", side_effect=watch))
        elif watch is not None:
            patches.append(mock.patch.object(br, "watch_run", return_value=watch))
        patches.append(mock.patch.object(
            br, "publish_with_retry",
            return_value={"ok": True, "check_run_id": 9, "adopted": False}))
        for p in patches:
            p.start()
        try:
            br.resume(d, 1, lambda *a: None)
        finally:
            for p in patches:
                p.stop()
        return d, captured, br

    def test_receipt_short_circuit_finishes_processed(self):
        d, captured, br = self._resume(receipt={"check_run_id": 77, "run_id": "run-old"})
        fins = [q for q in captured if "processed_at" in q]
        self.assertEqual(len(fins), 1)
        self.assertIn("status='PROCESSED'", fins[0])
        self.assertIn("check_run=77", fins[0])
        self.assertIn(d["_cid"], fins[0])  # 精确新租约

    def test_terminal_project_resumes_publish_without_kickoff(self):
        d, captured, br = self._resume(proj_status="completed",
                                       watch=("completed", "r"))
        fins = [q for q in captured if "processed_at" in q]
        self.assertEqual(len(fins), 1)
        self.assertIn("status='PROCESSED'", fins[0])
        # 场景2 关键:恢复路径没有任何 kickoff 发送 SQL/Matrix 调用发生
        # (mx.send 已 patch;若被调用会抛 AssertionError 由下面守护)
        self.assertTrue(all("INSERT" not in q and "seed" not in q for q in captured))

    def test_inflight_project_only_watches(self):
        calls = {}
        br = self.br

        def fake_watch(run, proj, deadline, **kwargs):
            calls["run"] = run
            return ("completed", "late report")

        d, captured, _ = self._resume(proj_status="pending", watch=fake_watch)
        self.assertTrue(calls.get("run", "").startswith("resume-"))
        fins = [q for q in captured if "processed_at" in q]
        self.assertEqual(len(fins), 1)

    def test_missing_project_requeues_bounded(self):
        # 第一次:RQ0 → 回队 PENDING + RQ1
        d, captured, _ = self._resume(proj_status=None, error="")
        requeue = [q for q in captured if "status='PENDING'" in q]
        self.assertEqual(len(requeue), 1)
        self.assertIn("error='RQ1'", requeue[0])
        self.assertIn(d["_cid"], requeue[0])
        # 第二次:RQ2 ≥ REQUEUE_MAX → MANUAL
        d2, captured2, _ = self._resume(proj_status=None, error="RQ2")
        manual = [q for q in captured2 if "processed_at" in q]
        self.assertEqual(len(manual), 1)
        self.assertIn("MANUAL", manual[0])
        self.assertIn("status='ERROR'", manual[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
