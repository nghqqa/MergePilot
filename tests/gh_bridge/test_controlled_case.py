"""受控案例定向认领(MERGEPILOT_TARGET_PR/HEAD)与发布未知态测试。"""
from __future__ import annotations

import unittest
from unittest import mock

from .test_publish_semantics import _delivery, _load_bridge

HEAD = "f" * 40


class TargetFilterTests(unittest.TestCase):
    """受控案例:只认领获批的 repo+PR+head;其他 PENDING 行保持不动。"""

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
                br.TARGET_REPO_ENV: "team/demo",
                br.TARGET_PR_ENV: "9",
                br.TARGET_HEAD_ENV: HEAD}), \
             mock.patch.object(br, "ssh_psql", side_effect=fake_ssh):
            br.pending_deliveries()
        q = captured[0]
        self.assertIn("repo='team/demo'", q)
        self.assertIn("pr_number=9", q)
        self.assertIn("observed_head_sha='%s'" % HEAD, q)

    def test_filter_off_by_default(self):
        br = self.br
        captured = []

        def fake_ssh(q, as_json=False):
            captured.append(q)
            return "[]"

        with mock.patch.dict("os.environ", {
                br.TARGET_REPO_ENV: "", br.TARGET_PR_ENV: "",
                br.TARGET_HEAD_ENV: ""}), \
             mock.patch.object(br, "ssh_psql", side_effect=fake_ssh):
            br.pending_deliveries()
        self.assertNotIn("pr_number=", captured[0].split("repo IS NOT NULL")[1])

    def test_invalid_values_rejected(self):
        br = self.br
        base = {br.TARGET_REPO_ENV: "team/demo", br.TARGET_PR_ENV: "9",
                br.TARGET_HEAD_ENV: HEAD}
        for key, bad in ((br.TARGET_REPO_ENV, "bad repo name"),
                         (br.TARGET_PR_ENV, "9; DROP TABLE x"),
                         (br.TARGET_HEAD_ENV, "short")):
            env = dict(base)
            env[key] = bad
            with mock.patch.dict("os.environ", env):
                with self.assertRaises(ValueError):
                    br.target_filter_sql()

    def test_any_missing_component_is_inactive(self):
        br = self.br
        full = {br.TARGET_REPO_ENV: "team/demo", br.TARGET_PR_ENV: "9",
                br.TARGET_HEAD_ENV: HEAD}
        for miss in (br.TARGET_REPO_ENV, br.TARGET_PR_ENV, br.TARGET_HEAD_ENV):
            env = dict(full)
            env[miss] = ""
            with mock.patch.dict("os.environ", env):
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
                br.TARGET_REPO_ENV: "team/demo", br.TARGET_PR_ENV: "9",
                br.TARGET_HEAD_ENV: HEAD}), \
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
                # reconcile 干净返回:无既有 check(明确无未知);stdout=明文 JSON
                return json_dumps({"http": 200, "matches": []}), True
            # POST 被明确拒绝(结构化 422,新 reporter 错误形态)
            return json_dumps({"http": 422, "error": "Unprocessable",
                               "body": "Invalid request"}), True

        with mock.patch.object(br, "_reporter_exec", side_effect=fake_exec), \
             mock.patch.object(br.time, "sleep"):
            res = br.publish_with_retry(d, "pass", "report", "run-x", "proj",
                                        log=lambda *a: None)
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("outcome"), "permanent")  # 明确拒绝≠未知(五分类)


def json_dumps(obj):
    import json
    return json.dumps(obj)


class PublishClassificationTests(unittest.TestCase):
    """执行保护复核:五类发布结果分类(unknown/auth/retryable/permanent/成功)。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _run(self, post_shape, exec_ok=True):
        br = self.br
        d = _delivery()
        reconcile_b64 = br.base64.b64encode(
            br.CHECK_RECONCILE_SCRIPT.encode()).decode()

        def fake_exec(script_b64, payload_b64):
            if script_b64 == reconcile_b64:
                return json_dumps({"http": 200, "matches": []}), True
            if not exec_ok:
                return "", False   # 传输层失败(超时/SSH)
            return json_dumps(post_shape), True

        with mock.patch.object(br, "_reporter_exec", side_effect=fake_exec),              mock.patch.object(br.time, "sleep"):
            return br.publish_with_retry(d, "pass", "report", "run-x", "proj",
                                         log=lambda *a: None)

    def test_422_is_permanent(self):
        res = self._run({"http": 422, "error": "Unprocessable"})
        self.assertEqual(res["outcome"], "permanent")

    def test_403_forbidden_is_permanent(self):
        res = self._run({"http": 403, "error": "Forbidden"})
        self.assertEqual(res["outcome"], "permanent")

    def test_403_rate_limit_is_retryable(self):
        res = self._run({"http": 403, "error": "rate limit exceeded"})
        self.assertEqual(res["outcome"], "retryable")

    def test_401_is_auth(self):
        res = self._run({"http": 401, "error": "Bad credentials"})
        self.assertEqual(res["outcome"], "auth")

    def test_5xx_is_retryable(self):
        res = self._run({"http": 502, "error": "Bad gateway"})
        self.assertEqual(res["outcome"], "retryable")

    def test_transport_failure_is_unknown(self):
        res = self._run({"http": 201, "check_run_id": 1}, exec_ok=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["outcome"], "unknown")

    def test_unclassifiable_is_conservative_unknown(self):
        res = self._run({"weird": "shape"})
        self.assertEqual(res["outcome"], "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
