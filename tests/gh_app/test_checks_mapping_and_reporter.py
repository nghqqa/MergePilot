"""Checks mapping + reconcile + reporter tests (M8-GH-1 §5/§6)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT / "tools" / "workflow-controller"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fakes import FakeConnection                            # noqa: E402
import github_drain as gd                                   # noqa: E402
import checks_reporter as cr                                # noqa: E402

REPO = "nghqqa/MergePilot"
RUN = "gh-" + "1" * 24
SHA_A = "a" * 40
SHA_B = "b" * 40
OUTBOX = "chk-" + "2" * 24
EXTERNAL = "mergepilot/%s" % RUN

STAGES = ["review", "fix", "verify", "merge", "reverify",
          "m5_verify_passed", "m5_verify_failed", "l2_binding",
          "l2_awaiting_ticket", None]


class TestChecksMappingTotal(unittest.TestCase):
    """(status, current_stage, rollback, stale) 全组合确定性快照。"""

    def assert_map(self, expected, **kwargs):
        self.assertEqual(gd.desired_check_state(**kwargs), expected)

    def test_stale_first(self):
        self.assert_map(
            ("completed", "neutral",
             "stale_delivery_superseded_by_authoritative_read"),
            status="MERGED", current_stage="merge", rollback_status=None,
            last_error=None, stale=True)

    def test_submitted_running_approval(self):
        self.assert_map(("queued", None, "registered"),
                        status="SUBMITTED", current_stage=None,
                        rollback_status=None, last_error=None, stale=False)
        self.assert_map(("in_progress", None, "stage:review"),
                        status="RUNNING", current_stage="review",
                        rollback_status=None, last_error=None, stale=False)
        self.assert_map(("completed", "action_required", "l2_approval_pending"),
                        status="APPROVAL_PENDING", current_stage="l2_binding",
                        rollback_status=None, last_error=None, stale=False)

    def test_terminal_success_failure(self):
        self.assert_map(("completed", "success", "verify_passed"),
                        status="PASS", current_stage="verify",
                        rollback_status=None, last_error=None, stale=False)
        self.assert_map(("completed", "success", "merged"),
                        status="MERGED", current_stage="merge",
                        rollback_status=None, last_error=None, stale=False)
        self.assert_map(("completed", "failure", "terminal_fail"),
                        status="FAIL", current_stage="m5_verify_failed",
                        rollback_status=None, last_error="verdict FAIL",
                        stale=False)

    def test_rolled_back_matrix(self):
        base = dict(status="ROLLED_BACK", current_stage="reverify",
                    last_error=None, stale=False)
        self.assert_map(("completed", "success",
                         "revert_reverified_recovered"),
                        rollback_status="RECOVERED", **base)
        self.assert_map(("in_progress", None,
                         "revert_applied_reverify_pending"),
                        rollback_status="REVERIFYING", **base)
        for other in ("REVERTED", "HELD", "AWAITING_APPROVAL", None):
            self.assert_map(("completed", "failure",
                             "merge_reverted_rollback_executed"),
                            rollback_status=other, **base)

    def test_hold_specific_before_generic(self):
        self.assert_map(("completed", "neutral", "infra_hold_not_measurable"),
                        status="HOLD", current_stage="m4f_producer_timeout",
                        rollback_status=None, last_error=None, stale=False)
        self.assert_map(("completed", "neutral", "infra_hold_not_measurable"),
                        status="HOLD", current_stage="whatever",
                        rollback_status=None,
                        last_error="PRODUCER_TIMEOUT: waited too long",
                        stale=False)
        for stage in ("m4f_skill_failed", "l2_binding_failed",
                      "revision_superseded", "verify_max_hold",
                      "reverify_failed", "m5_verify_passed",
                      "m5_verify_failed"):
            self.assert_map(("completed", "neutral", "hold_reason_%s" % stage),
                            status="HOLD", current_stage=stage,
                            rollback_status=None, last_error=None,
                            stale=False)
        self.assert_map(("completed", "neutral", "internal_hold_unclassified"),
                        status="HOLD", current_stage=None,
                        rollback_status=None, last_error=None, stale=False)
        self.assert_map(("completed", "neutral", "internal_hold_unclassified"),
                        status="HOLD", current_stage="brand_new_stage",
                        rollback_status=None, last_error="任何自由文本",
                        stale=False)

    def test_unknown_combination_fail_closed(self):
        self.assert_map(("completed", "neutral", "internal_state_unmapped"),
                        status="WEIRD_STATUS", current_stage="review",
                        rollback_status=None, last_error=None, stale=False)

    def test_all_stage_combinations_deterministic(self):
        for status in ("SUBMITTED", "RUNNING", "APPROVAL_PENDING", "PASS",
                       "MERGED", "FAIL", "HOLD"):
            for stage in STAGES:
                result = gd.desired_check_state(
                    status=status, current_stage=stage, last_error=None,
                    rollback_status=None, stale=False)
                self.assertIn(result[0], ("queued", "in_progress",
                                          "completed"))
                if result[0] == "completed":
                    self.assertIn(result[1], ("success", "failure",
                                              "neutral", "action_required"))
                else:
                    self.assertIsNone(result[1])


class TestReconcileUpsert(unittest.TestCase):

    def _reconcile_conn(self, *, binding_sha=SHA_A, observed=SHA_A,
                         rollback=None, upsert_rowcount=1):
        conn = FakeConnection()
        conn.enqueue("FROM public.task_runs r", rowcount=1,
                     fetchall=[(RUN, "RUNNING", "review", None, REPO, 101)])
        conn.enqueue("SELECT head_sha FROM public.run_pr_bindings",
                     rowcount=1,
                     fetchone=(binding_sha,) if binding_sha else None)
        conn.enqueue("SELECT observed_head_sha FROM public.github_deliveries",
                     rowcount=1, fetchone=(observed,))
        conn.enqueue("SELECT status FROM public.rollback_runs", rowcount=1,
                     fetchone=(rollback,) if rollback else None)
        conn.enqueue("ON CONFLICT (external_id) DO UPDATE", rowcount=upsert_rowcount)
        return conn

    def test_upsert_sql_cas_predicates(self):
        sql = gd._UPSERT_CHECK_SQL
        self.assertIn("IS DISTINCT FROM", sql)          # 未变不加版本
        self.assertIn("desired_version + 1", sql)
        self.assertIn("THEN NULL", sql)                  # SHA 变更清 check_run_id
        self.assertIn("claim_id           = NULL", sql)  # 旧 claim 失效
        self.assertIn("'LEASED','PUBLISHED','TERMINAL'", sql)
        self.assertIn("'PENDING'", sql)

    def test_desired_unchanged_no_version_bump(self):
        # ON CONFLICT 分支 WHERE 不满足 → rowcount=0(由 SQL 保证;此处验证
        # reconcile 不重试、不重复 upsert)
        conn = self._reconcile_conn(upsert_rowcount=0)
        updated = gd.reconcile_github_checks(lambda: conn)
        self.assertEqual(updated, 0)
        self.assertEqual(len(conn.params_of("ON CONFLICT (external_id)")), 1)

    def test_params_carry_mapping_and_sha(self):
        import hashlib
        conn = self._reconcile_conn()
        updated = gd.reconcile_github_checks(lambda: conn)
        self.assertEqual(updated, 1)
        params = conn.params_of("ON CONFLICT (external_id) DO UPDATE")[0]
        expected_outbox = "chk-" + hashlib.sha256(
            RUN.encode("utf-8")).hexdigest()[:24]
        self.assertEqual(params[0], expected_outbox)
        self.assertEqual(params[1], RUN)
        self.assertEqual(params[2], REPO)
        self.assertEqual(params[3], 101)
        self.assertEqual(params[4], SHA_A)
        self.assertEqual(params[5], EXTERNAL)
        self.assertEqual(params[6], "in_progress")   # RUNNING 映射
        self.assertIsNone(params[7])

    def test_stale_binding_maps_neutral(self):
        conn = self._reconcile_conn(binding_sha=SHA_B, observed=SHA_A)
        events = []
        gd.reconcile_github_checks(lambda: conn, observer=events.append)
        self.assertEqual(events[-1]["reason"],
                         "stale_delivery_superseded_by_authoritative_read")

    def test_no_delivery_observation_skipped(self):
        conn = FakeConnection()
        conn.enqueue("FROM public.task_runs r", rowcount=1,
                     fetchall=[(RUN, "RUNNING", "review", None, REPO, 101)])
        conn.enqueue("SELECT head_sha FROM", rowcount=1, fetchone=None)
        conn.enqueue("SELECT observed_head_sha FROM", rowcount=1,
                     fetchone=None)
        self.assertEqual(gd.reconcile_github_checks(lambda: conn), 0)
        self.assertEqual(conn.params_of("ON CONFLICT (external_id)"), [])


def claimed_row(*, check_run_id=None, sha=SHA_A, desired="in_progress",
                conclusion=None, version=1, published=0, attempt=1):
    return (OUTBOX, "claim-9", RUN, REPO, 101, sha, EXTERNAL, check_run_id,
            desired, conclusion, version, published, attempt)


class ReporterTransport:
    """可编程 HTTP stub:按 (method, url 关键字) 返回脚本化响应。"""

    def __init__(self, script):
        self.script = list(script)      # [(match, (status, headers, body))]
        self.calls = []                 # [(method, url, body, headers)]

    def __call__(self, method, url, *, headers=None, body=None):
        self.calls.append((method, url, body, headers))
        for index, (match, response) in enumerate(self.script):
            if match in url and not self.script[index].__contains__("_used"):
                self.script[index] = (match + "_used", response)
                return response
        raise cr.TransportError("unrouted url %s" % url)


def reporter_conn(row, *, confirm_rowcount=1):
    conn = FakeConnection()
    conn.enqueue("UPDATE public.github_check_outbox o", rowcount=1,
                 fetchone=row)
    conn.enqueue("UPDATE public.github_check_outbox", rowcount=confirm_rowcount)
    return conn


class TestReporter(unittest.TestCase):

    def test_create_flow_lookup_then_post_and_triple_cas(self):
        row = claimed_row()
        transport = ReporterTransport([
            ("/commits/%s/check-runs" % SHA_A,
             (200, {}, {"check_runs": []})),          # 同名异 SHA 不在集内
            ("/repos/%s/check-runs" % REPO,
             (201, {}, {"id": 987654})),
        ])
        conn = reporter_conn(row)
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport, token="tok")
        self.assertEqual(outcome, "published")
        lookup_url = transport.calls[0][1]
        self.assertIn("/repos/%s/commits/%s/check-runs" % (REPO, SHA_A),
                      lookup_url)                      # 三元组之 SHA 维度
        create_method, create_url, create_body, _ = transport.calls[1]
        self.assertEqual(create_method, "POST")
        self.assertEqual(create_body["name"], EXTERNAL)
        self.assertEqual(create_body["head_sha"], SHA_A)
        self.assertIn("Bearer tok", str(transport.calls[0]))
        confirm = conn.params_of("published_status")[0]
        self.assertEqual(confirm[3], 987654)           # check_run_id 回填
        confirm_sql = [sql for sql, _ in conn.executed
                       if "published_status" in sql][0]
        self.assertIn("claim_id = %s", confirm_sql)
        self.assertIn("observed_head_sha = %s", confirm_sql)   # SHA CAS
        self.assertIn("%s > published_version", confirm_sql)   # 单调门

    def test_existing_check_reuses_by_name_within_same_sha(self):
        row = claimed_row()
        transport = ReporterTransport([
            ("/commits/%s/check-runs" % SHA_A,
             (200, {}, {"check_runs": [
                 {"name": "other-check", "id": 1},
                 {"name": EXTERNAL, "id": 555}]})),
            ("/check-runs/555", (200, {}, {"id": 555})),
        ])
        conn = reporter_conn(row)
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport)
        self.assertEqual(outcome, "published")
        # 查找命中 → PATCH(第二个调用),非 create
        patch_method, patch_url, _, _ = transport.calls[1]
        self.assertEqual(patch_method, "PATCH")
        self.assertIn("/check-runs/555", patch_url)
        self.assertEqual(conn.params_of("published_status")[0][3], 555)

    def test_check_run_id_cached_patches_without_lookup(self):
        row = claimed_row(check_run_id=555)
        transport = ReporterTransport([
            ("/repos/%s/check-runs/555" % REPO, (200, {}, {"id": 555})),
        ])
        conn = reporter_conn(row)
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport)
        self.assertEqual(outcome, "published")
        self.assertEqual(transport.calls[0][0], "PATCH")   # 无 GET 查找
        self.assertEqual(conn.params_of("published_status")[0][3], 555)

    def test_sha_a_late_response_cannot_confirm_sha_b(self):
        # 认领时 SHA-A;行内(确认阶段)已是 SHA-B —— 三重 CAS 的 SQL 结构
        # 保证 rowcount=0。此处验证确认谓词与参数把 seen_sha 带入。
        row = claimed_row(sha=SHA_A)
        transport = ReporterTransport([
            ("/commits/%s/check-runs" % SHA_A, (200, {}, {"check_runs": []})),
            ("/repos/%s/check-runs" % REPO, (201, {}, {"id": 777})),
        ])
        conn = reporter_conn(row, confirm_rowcount=0)   # CAS 失败(rowcount 0)
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport)
        self.assertEqual(outcome, "published")          # 发布尝试完成…
        confirm = conn.params_of("published_status")[0]
        self.assertEqual(confirm[8], SHA_A)             # …但 seen_sha=SHA-A
        # confirm_rowcount=0 由 SQL 谓词(claim/sha/version)决定,静默丢弃

    def test_403_terminal(self):
        row = claimed_row(check_run_id=555)
        transport = ReporterTransport([
            ("/check-runs/555", (403, {}, {"message": "forbidden"})),
        ])
        conn = reporter_conn(row)
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport)
        self.assertEqual(outcome, "terminal")
        terminal = conn.params_of("publish_state = 'TERMINAL'")[0]
        self.assertIn("http 403", terminal[0])

    def test_422_terminal(self):
        row = claimed_row(check_run_id=555)
        transport = ReporterTransport([
            ("/check-runs/555", (422, {}, {"message": "validation"})),
        ])
        conn = reporter_conn(row)
        self.assertEqual(cr.publish_once(lambda: conn, api_base="http://stub",
                                         transport=transport), "terminal")

    def test_429_uses_retry_after(self):
        row = claimed_row(check_run_id=555)
        transport = ReporterTransport([
            ("/check-runs/555", (429, {"Retry-After": "37"}, {})),
        ])
        conn = reporter_conn(row)
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport)
        self.assertEqual(outcome, "retry")
        retry = conn.params_of("publish_state = 'PENDING',")[0]
        self.assertEqual(retry[0], 37)

    def test_5xx_backoff_retry(self):
        row = claimed_row(check_run_id=555)
        transport = ReporterTransport([
            ("/check-runs/555", (503, {}, {})),
        ])
        conn = reporter_conn(row)
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport)
        self.assertEqual(outcome, "retry")
        retry = conn.params_of("publish_state = 'PENDING',")[0]
        self.assertEqual(retry[0], 30)               # 30*2^0

    def test_transport_error_retry_and_no_governance_writes(self):
        row = claimed_row(check_run_id=555)
        def boom(method, url, *, headers=None, body=None):
            raise cr.TransportError("connection reset")
        conn = reporter_conn(row)
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=boom)
        self.assertEqual(outcome, "retry")
        # reporter 永不写治理表
        for governed in ("task_runs", "stage_runs", "dispatch_outbox",
                         "stage_events"):
            self.assertFalse(any(governed in sql for sql in conn.sqls()))

    def test_output_contains_no_secrets(self):
        row = claimed_row()
        captured = []
        def capture(method, url, *, headers=None, body=None):
            captured.append(body or {})
            if method == "GET":
                return (200, {}, {"check_runs": []})
            return (201, {}, {"id": 1})
        conn = reporter_conn(row)
        cr.publish_once(lambda: conn, api_base="http://stub",
                        transport=capture, token="secret-token-value")
        create_body = captured[-1]
        summary = create_body["output"]["summary"]
        self.assertIn(RUN, summary)
        self.assertNotIn("secret-token-value", summary)


if __name__ == "__main__":
    unittest.main()
