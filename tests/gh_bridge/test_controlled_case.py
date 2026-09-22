"""受控案例定向认领(MERGEPILOT_TARGET_PR/HEAD)与发布未知态测试。"""
from __future__ import annotations

import unittest
from unittest import mock

from .test_publish_semantics import _delivery, _load_bridge

HEAD = "f" * 40


class TargetFilterTests(unittest.TestCase):
    """受控案例:只认领获批的 PR+head;其他 PENDING 行保持不动。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_filter_activated_sql_contains_predicates(self):
        br = self.br
        captured = []

        def fake_ssh(q, as_json=False):
            captured.append(q)
            return "[]"

        with mock.patch.dict("os.environ", {
                br.TARGET_PR_ENV: "9",
                br.TARGET_HEAD_ENV: HEAD}), \
             mock.patch.object(br, "ssh_psql", side_effect=fake_ssh):
            br.pending_deliveries()
        q = captured[0]
        self.assertIn("pr_number=9", q)
        self.assertIn("observed_head_sha='%s'" % HEAD, q)

    def test_filter_off_by_default(self):
        br = self.br
        captured = []

        def fake_ssh(q, as_json=False):
            captured.append(q)
            return "[]"

        with mock.patch.dict("os.environ", {
                br.TARGET_PR_ENV: "", br.TARGET_HEAD_ENV: ""}), \
             mock.patch.object(br, "ss_psql" if False else "ssh_psql", side_effect=fake_ssh):
            br.pending_deliveries()
        self.assertNotIn("pr_number=", captured[0].split("repo IS NOT NULL")[1])

    def test_invalid_values_rejected(self):
        br = self.br
        with mock.patch.dict("os.environ", {
                br.TARGET_PR_ENV: "9; DROP TABLE x",
                br.TARGET_HEAD_ENV: HEAD}):
            with self.assertRaises(ValueError):
                br.target_filter_sql()
        with mock.patch.dict("os.environ", {
                br.TARGET_PR_ENV: "9", br.TARGET_HEAD_ENV: "short"}):
            with self.assertRaises(ValueError):
                br.target_filter_sql()

    def test_only_one_side_set_is_inactive(self):
        br = self.br
        with mock.patch.dict("os.environ", {
                br.TARGET_PR_ENV: "9", br.TARGET_HEAD_ENV: ""}):
            extra, pair = br.target_filter_sql()
        self.assertEqual((extra, pair), ("", None))

    def test_process_with_target_skips_other_rows(self):
        """非目标行在认领前被查询层过滤——不认领、不终结、保持 PENDING。"""
        br = self.br
        d = _delivery()  # head 不匹配目标
        fins, sent = [], []

        def fake_ssh(q):
            fins.append(q)
            return "UPDATE 1"

        with mock.patch.dict("os.environ", {
                br.TARGET_PR_ENV: "9", br.TARGET_HEAD_ENV: HEAD}), \
             mock.patch.object(br, "ssh_psql", side_effect=fake_ssh), \
             mock.patch.object(br, "pending_deliveries", return_value=[]), \
             mock.patch.object(br.mx, "send",
                               side_effect=lambda *a, **k: sent.append(a)):
            br.process(d, timeout_min=1, dry=False)
        # d 的 head 不匹配 → pending_deliveries(已 mock 为空)根本不会返回它;
        # 此测试锁定:即使误调 process,没有 claim 后的 finish 副作用产生
        self.assertEqual(sent, [])
        self.assertEqual([q for q in fins if "status=" in q and "github_deliveries" in q
                          and "claimed_at" in q], [])


class PublishUnknownTests(unittest.TestCase):
    """发布结果未知(超时/网络)→ 停止自动重试,标记 PUBLISH_UNKNOWN(manual)。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_unknown_outcome_marks_manual_after_attempts(self):
        br = self.br
        d = _delivery()
        log_lines = []

        def fake_exec(script_b64, payload_b64):
            # reconcile 与 POST 全部超时(结果未知)
            return "", False

        with mock.patch.object(br, "_reporter_exec", side_effect=fake_exec), \
             mock.patch.object(br.time, "sleep"), \
             mock.patch.object(br, "write_receipt", return_value=True):
            res = br.publish_with_retry(d, "pass", "report", "run-x", "proj",
                                        log=lambda *a: log_lines.append(a))
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("outcome"), "unknown")  # 未知 ≠ 明确拒绝

    def test_definitive_reject_remains_retryable(self):
        br = self.br
        d = _delivery()
        calls = {"n": 0}

        reconcile_b64 = br.base64.b64encode(
            br.CHECK_RECONCILE_SCRIPT.encode()).decode()

        def fake_exec(script_b64, payload_b64):
            calls["n"] += 1
            if script_b64 == reconcile_b64:
                # reconcile 干净返回:无既有 check(明确无未知)
                return br.base64.b64encode(
                    json_dumps({"http": 200, "matches": []}).encode()).decode(), True
            # POST 被明确拒绝(非超时):GitHub 422 结构化错误
            return br.base64.b64encode(
                json_dumps({"message": "422 Unprocessable",
                            "status": "422"}).encode()).decode(), True

        with mock.patch.object(br, "_reporter_exec", side_effect=fake_exec), \
             mock.patch.object(br.time, "sleep"):
            res = br.publish_with_retry(d, "pass", "report", "run-x", "proj",
                                        log=lambda *a: None)
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("outcome"), "rejected")  # 明确拒绝≠未知


def json_dumps(obj):
    import json
    return json.dumps(obj)


if __name__ == "__main__":
    unittest.main(verbosity=2)
