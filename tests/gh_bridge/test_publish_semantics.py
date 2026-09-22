"""M1-1 桥可靠性语义的单元测试（全 mock，无真实 SSH/GitHub/Matrix/MinIO）。

覆盖验收场景（备忘 v2 M1 / 启动提示词四.）：
  7  回写失败不能标记投递成功，恢复后可完成回写（publish_with_retry + process 尾段）
  3  GitHub 已接受回写但本地未记录，可对账收敛（reconcile 采纳 + receipt）
  8  旧执行者失去租约后不能覆盖新执行者状态（finish 精确 claim_id）
  4  重试有界（PUBLISH_ATTEMPTS 上限与退避）
  5  重复 webhook 不重复执行（already_processed 去重守卫）
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

_BRIDGE_PATH = os.path.join(os.path.dirname(__file__), "..", "..",
                            "tools", "gh-bridge", "gh_bridge.py")


def _load_bridge():
    spec = importlib.util.spec_from_file_location("gh_bridge_m1", _BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _delivery(**over):
    d = {"delivery_id": "0123456789abcdef-1234-5678-9abc-def012345678",
         "event_name": "pull_request", "action": "synchronize",
         "repo": "nghqqa/fastapi-boilerplate-demo", "pr_number": 2,
         "observed_head_sha": "65de83d6d061413ec98c1e79515f470313ef9806",
         "observed_base_sha": "4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c",
         "received_at": "2026-09-19T08:50:58Z"}
    d.update(over)
    return d


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_publish_success_shape(self):
        out = json.dumps({"http": 201, "check_run_id": 12345, "url": "u"})
        r = self.br.parse_publish_out(out)
        self.assertTrue(r["ok"])
        self.assertEqual(r["check_run_id"], 12345)
        self.assertFalse(r["adopted"])

    def test_publish_rejects_non_201(self):
        for out in ('{"http": 404}', '{"http": 200}', '{"http": 201}',
                    "Traceback ...", "", '{"weird": 1}'):
            self.assertFalse(self.br.parse_publish_out(out)["ok"], out)

    def test_reconcile_adopt_and_empty(self):
        r = self.br.parse_reconcile_out(json.dumps(
            {"http": 200, "matches": [{"check_run_id": 9, "url": "x"}]}))
        self.assertTrue(r["ok"] and r["adopted"] and r["check_run_id"] == 9)
        empty = self.br.parse_reconcile_out(json.dumps({"http": 200, "matches": []}))
        self.assertTrue(empty["ok"] and not empty["adopted"] and empty["matches"] == 0)
        self.assertFalse(self.br.parse_reconcile_out("ssh broke")["ok"])


class ClaimFinishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_claim_id_rotates(self):
        d = _delivery()
        ids = set()
        with mock.patch.object(self.br, "ssh_psql", return_value="UPDATE 1"):
            for _ in range(3):
                cid = self.br.claim(d)
                self.assertTrue(cid)
                ids.add(cid)
        self.assertEqual(len(ids), 3)  # 每次认领轮换(对照 github_drain 合同)
        for cid in ids:
            self.assertIn("-bridge-", cid)

    def test_finish_requires_exact_claim(self):
        """场景8:带 cid 的 finish 必须精确匹配;旧执行者的 cid 不再命中."""
        captured = []

        def fake_ssh(q):
            captured.append(q)
            return "UPDATE 1"

        d = _delivery()
        with mock.patch.object(self.br, "ssh_psql", side_effect=fake_ssh):
            self.br.finish(d, True, "ok", cid="abc-bridge-0000ffff")
        q = captured[0]
        self.assertIn("claim_id='abc-bridge-0000ffff'", q)
        self.assertNotIn("LIKE", q)

    def test_finish_no_cid_terminates_pending_row_f1(self):
        """F1 修复:预认领拒绝(无 cid)按 status='PENDING' AND claim_id IS NULL
        终结——原 LIKE '%-bridge%' 对 PENDING 行(claim NULL)静默无效。"""
        br = self.br
        captured = []

        def fake_ssh(q):
            captured.append(q)
            return "UPDATE 1"

        with mock.patch.object(br, "ssh_psql", side_effect=fake_ssh):
            ok = br.finish(_delivery(), False, "repo not in bridge allowlist")
        self.assertTrue(ok)
        q = captured[0]
        self.assertIn("status='PENDING' AND claim_id IS NULL", q)
        self.assertNotIn("LIKE", q)

    def test_finish_no_cid_does_not_touch_claimed_row(self):
        """无 cid 的拒绝不得波及已被认领(RUNNING/带 cid)的行。"""
        br = self.br
        captured = []

        def fake_ssh(q):
            captured.append(q)
            return "UPDATE 1"

        with mock.patch.object(br, "ssh_psql", side_effect=fake_ssh):
            br.finish(_delivery(), False, "reject", cid=None)
        q = captured[0]
        self.assertIn("claim_id IS NULL", q)   # 只会命中未认领行

    def test_finish_no_cid_uses_pending_null_predicate(self):
        """F1 修复(设计审计):无 cid 终结按 PENDING+claim NULL 定位——
        原 LIKE '%-bridge%' 对 PENDING 行(claim NULL)永不匹配,拒绝行静默滞留。"""
        br = self.br
        captured = []

        def fake_ssh(q):
            captured.append(q)
            return "UPDATE 1"

        d = _delivery()
        with mock.patch.object(self.br, "ssh_psql", side_effect=fake_ssh):
            self.br.finish(d, False, "not in allowlist")
        q = captured[0]
        self.assertIn("status='PENDING' AND claim_id IS NULL", q)
        self.assertNotIn("LIKE", q)

    # (原 test_finish_prec_claim_path_keeps_like 已被 F1 修复取代:
    #  无 cid 终结按 PENDING+claim NULL 定位,见 test_finish_no_cid_*)
    def test_already_processed_guard(self):
        d = _delivery()
        captured = []

        def fake_ssh(q):
            captured.append(q)
            return "1"

        with mock.patch.object(self.br, "ssh_psql", side_effect=fake_ssh):
            self.assertTrue(self.br.already_processed(d))
        self.assertIn("status='PROCESSED'", captured[0])
        self.assertIn("pr_number=2", captured[0])
        self.assertIn(d["observed_head_sha"], captured[0])
        with mock.patch.object(self.br, "ssh_psql", return_value="0"):
            self.assertFalse(self.br.already_processed(d))


class PublishRetryTests(unittest.TestCase):
    """场景3/4/7:receipt→reconcile→POST 的有界发布链."""
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _run(self, *, receipt=None, exec_results=None, receipt_write=True):
        br = self.br
        d = _delivery()
        calls = {"exec": [], "receipts": [], "sleeps": []}
        with mock.patch.object(br, "read_receipt", return_value=receipt), \
             mock.patch.object(br, "write_receipt",
                               side_effect=lambda p, r, dd, run, v:
                               (calls["receipts"].append((p, r)) or receipt_write)), \
             mock.patch.object(br, "_reporter_exec",
                               side_effect=lambda s, p:
                               calls["exec"].append(s[:24]) or exec_results.pop(0)), \
             mock.patch.object(br.time, "sleep",
                               side_effect=lambda s: calls["sleeps"].append(s)):
            res = br.publish_with_retry(d, "pass", "report", "run-x", "proj-x",
                                        lambda *a: None)
        return res, calls

    def test_receipt_short_circuit(self):
        res, calls = self._run(receipt={"check_run_id": 77, "url": "u"})
        self.assertTrue(res["ok"] and res["adopted"] and res["from"] == "receipt")
        self.assertEqual(calls["exec"], [])  # 零网络调用

    def test_reconcile_adopt_writes_receipt(self):
        # 场景3:GitHub 已有本 App 的 check-run(此前发布过但本地未记录)→ 采纳,不重发
        res, calls = self._run(exec_results=[
            ('{"http": 200, "matches": [{"check_run_id": 5}]}', True)])
        self.assertTrue(res["ok"] and res["adopted"] and res["check_run_id"] == 5)
        self.assertEqual(len(calls["receipts"]), 1)
        self.assertEqual(len(calls["exec"]), 1)  # 只对账,零 POST

    def test_post_success_first_try(self):
        res, calls = self._run(exec_results=[
            ('{"http": 200, "matches": []}', True),
            ('{"http": 201, "check_run_id": 42}', True)])
        self.assertTrue(res["ok"] and res["check_run_id"] == 42 and not res["adopted"])
        self.assertEqual(calls["receipts"][0][1]["check_run_id"], 42)
        self.assertEqual(calls["sleeps"], [])

    def test_bounded_retry_then_success(self):
        res, calls = self._run(exec_results=[
            ('{"http": 200, "matches": []}', True), ("ssh: broken", False),
            ('{"http": 200, "matches": []}', True), ("Traceback", True),
            ('{"http": 200, "matches": []}', True),
            ('{"http": 201, "check_run_id": 43}', True)])
        self.assertTrue(res["ok"] and res["check_run_id"] == 43)
        self.assertEqual(calls["sleeps"], [10, 30])  # 退避有界
        self.assertEqual(len(calls["exec"]), 6)      # 3 轮 × (对账+POST)

    def test_exhausted_returns_failure(self):
        res, calls = self._run(exec_results=[
            ('{"http": 200, "matches": []}', True), ("x", False)] * 3)
        self.assertFalse(res["ok"])
        self.assertEqual(len(calls["exec"]), 6)
        self.assertEqual(calls["sleeps"], [10, 30])

    def test_receipt_write_failure_non_fatal(self):
        res, calls = self._run(receipt_write=False, exec_results=[
            ('{"http": 200, "matches": []}', True),
            ('{"http": 201, "check_run_id": 8}', True)])
        self.assertTrue(res["ok"] and res["check_run_id"] == 8)


class ProcessTerminalSemantics(unittest.TestCase):
    """场景7 端到端(单元级):PROCESSED 只在发布成功后;失败=PUBLISH_FAILED(retryable)."""
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _process(self, pub_result, terminal="completed"):
        br = self.br
        d = _delivery()
        captured = []

        def fake_ssh(q):
            captured.append(q)
            return "UPDATE 1" if q.startswith("UPDATE") else "0"

        gate = {"approval": "gate approved" if terminal == "blocked" else ""}.get(
            "approval") if False else None  # noqa: F841 —— 见下,直接 patch gate_record

        with mock.patch.object(br, "ssh_psql", side_effect=fake_ssh), \
             mock.patch.object(br, "already_processed", return_value=False), \
             mock.patch.object(br, "rag_dispatch_gate", return_value=(True, {})), \
             mock.patch.object(br, "seed_project", return_value=True), \
             mock.patch.object(br, "wake_workers", return_value=True), \
             mock.patch.object(br, "prepare_run_manifest",
                               return_value=("kickoff+manifest-ref", None)), \
             mock.patch.object(br.mx, "send",
                               return_value={"event_id": "$ev"}), \
             mock.patch.object(br, "watch_run",
                               return_value=(terminal, "leader report")), \
             mock.patch.object(br, "project_result", return_value="result.md"), \
             mock.patch.object(br, "gate_record", return_value=""), \
             mock.patch.object(br, "publish_with_retry", return_value=pub_result):
            br.process(d, timeout_min=1, dry=False)
        return [q for q in captured if "processed_at=now()" in q]

    def test_processed_only_after_publish_success(self):
        fins = self._process({"ok": True, "check_run_id": 5, "url": "u", "adopted": False})
        self.assertEqual(len(fins), 1)
        self.assertIn("status='PROCESSED'", fins[0])
        self.assertIn("check_run=5", fins[0])

    def test_publish_failure_marks_recoverable_error(self):
        fins = self._process({"ok": False, "raw": "ssh timeout"})
        self.assertEqual(len(fins), 1)
        self.assertIn("status='ERROR'", fins[0])
        self.assertIn("PUBLISH_FAILED(retryable)", fins[0])
        self.assertNotIn("PROCESSED", fins[0])

    def test_timeout_marks_manual_error_even_if_published(self):
        fins = self._process({"ok": True, "check_run_id": 6}, terminal="timeout")
        self.assertIn("status='ERROR'", fins[0])
        self.assertIn("TIMEOUT(manual)", fins[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
