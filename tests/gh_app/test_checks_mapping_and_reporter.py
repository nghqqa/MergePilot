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


def reporter_conn(row, *, confirm_rowcount=1, reap_rowcount=0):
    conn = FakeConnection()
    # M8-GH-4B2: publish_once now runs the MAX_ATTEMPTS reap BEFORE the
    # claim — script it explicitly so the generic confirm entry keeps its
    # original meaning.
    conn.enqueue("attempt_count >= %s", rowcount=reap_rowcount)
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
        terminal = conn.params_of("last_error = %s, claim_id = NULL")[0]
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




# ── M8-GH-4B2: max_attempts atomic termination + provider auth ─────────────

class FakeProvider:
    """Minimal GitHubAppTokenProvider stand-in (token sequence + counters)."""

    def __init__(self, tokens=("provider-tok-1", "provider-tok-2")):
        self.tokens = list(tokens)
        self.get_calls = 0
        self.invalidate_calls = 0
        self.forced = 0

    def get_token(self, *, force_refresh=False):
        self.get_calls += 1
        if force_refresh:
            self.forced += 1
            return self.tokens[min(self.forced, len(self.tokens) - 1)]
        return self.tokens[0]

    def invalidate(self):
        self.invalidate_calls += 1


class TestMaxAttemptsTermination(unittest.TestCase):

    def test_claim_sql_only_selects_below_max(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=1))
        cr.publish_once(lambda: conn, api_base="http://stub",
                        transport=ReporterTransport(
                            [("/check-runs/555", (500, {}, {}))]),
                        max_attempts=5)
        claim_sql = [sql for sql, _ in conn.executed
                     if "SKIP LOCKED" in sql][0]
        self.assertIn("attempt_count < %s", claim_sql)

    def test_below_max_failure_returns_pending(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=4))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([("/check-runs/555",
                                          (500, {}, {}))]),
            max_attempts=5)
        self.assertEqual(outcome, "retry")
        retry = conn.params_of("next_retry_at = now() + make_interval")[0]
        self.assertIn("http 500", retry[1])

    def test_at_max_failure_direct_terminal_with_class(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=5))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([("/check-runs/555",
                                          (503, {}, {}))]),
            max_attempts=5)
        self.assertEqual(outcome, "terminal")
        terminal = conn.params_of("last_error = %s, claim_id = NULL")[0]
        self.assertEqual(terminal[0], "MAX_ATTEMPTS:HTTP_5XX")

    def test_at_max_transport_error_terminal_transport_class(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=3))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([]),   # unrouted -> TransportError
            max_attempts=3)
        self.assertEqual(outcome, "terminal")
        terminal = conn.params_of("last_error = %s, claim_id = NULL")[0]
        self.assertEqual(terminal[0], "MAX_ATTEMPTS:TRANSPORT")

    def test_reap_runs_before_claim_with_max_param(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=1))
        cr.publish_once(lambda: conn, api_base="http://stub",
                        transport=ReporterTransport(
                            [("/check-runs/555", (200, {}, {"id": 9}))]),
                        max_attempts=7)
        first_sql, first_params = conn.executed[0]
        self.assertIn("attempt_count >= %s", first_sql)
        self.assertEqual(first_params, (7,))

    def test_crashed_final_attempt_reaped_without_http(self):
        # Crash aftermath: expired-LEASED row with attempt_count == max.
        # A publish_once round must TERMINAL-reap it with ZERO transport
        # calls (the claim finds nothing eligible below max).
        conn = reporter_conn(None)
        transport = ReporterTransport([])
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport, max_attempts=5)
        self.assertEqual(outcome, "idle")
        self.assertEqual(transport.calls, [])
        reap_sql, reap_params = conn.executed[0]
        self.assertIn("lease_expires_at < now()", reap_sql)
        self.assertEqual(reap_params, (5,))

    def test_terminal_confirm_is_single_cas_statement(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=5),
                             confirm_rowcount=0)
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([("/check-runs/555",
                                          (503, {}, {}))]),
            max_attempts=5)
        self.assertEqual(outcome, "terminal")
        confirms = [sql for sql, _ in conn.executed
                    if "last_error = %s, claim_id = NULL" in sql]
        self.assertEqual(len(confirms), 1)


class TestProviderAuth(unittest.TestCase):

    def test_shared_auth_context_and_success(self):
        provider = FakeProvider()
        conn = reporter_conn(claimed_row(check_run_id=555))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([("/check-runs/555",
                                          (200, {}, {"id": 555}))]),
            token_provider=provider)
        self.assertEqual(outcome, "published")
        self.assertEqual(provider.get_calls, 1)   # ONE fetch per attempt

    def test_401_forces_one_refresh_and_retries_current_op(self):
        provider = FakeProvider()
        conn = reporter_conn(claimed_row(check_run_id=555))
        transport = ReporterTransport([
            ("/check-runs/555", (401, {}, {})),
            ("/check-runs/555", (200, {}, {"id": 555})),
        ])
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport,
                                  token_provider=provider)
        self.assertEqual(outcome, "published")
        self.assertEqual(provider.invalidate_calls, 1)
        self.assertEqual(len(transport.calls), 2)   # op retried ONCE
        headers_after = transport.calls[1][3]
        self.assertIn("Bearer", headers_after.get("Authorization", ""))

    def test_second_401_is_terminal(self):
        provider = FakeProvider()
        conn = reporter_conn(claimed_row(check_run_id=555))
        transport = ReporterTransport([
            ("/check-runs/555", (401, {}, {})),
            ("/check-runs/555", (401, {}, {})),
        ])
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport,
                                  token_provider=provider)
        self.assertEqual(outcome, "terminal")
        self.assertEqual(provider.invalidate_calls, 1)   # no infinite loop
        self.assertEqual(len(transport.calls), 2)

    def test_provider_error_confirms_retry_class(self):
        class ExplodingProvider(FakeProvider):
            def get_token(self, *, force_refresh=False):
                raise RuntimeError("token exchange http 500")
        conn = reporter_conn(claimed_row(check_run_id=555))
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=ReporterTransport([]),
                                  token_provider=ExplodingProvider())
        self.assertEqual(outcome, "retry")
        retry = conn.params_of("next_retry_at = now() + make_interval")[0]
        self.assertIn("token", retry[1])

    def test_authorization_never_in_observer_events(self):
        events = []
        provider = FakeProvider()
        conn = reporter_conn(claimed_row(check_run_id=555))
        cr.publish_once(lambda: conn, api_base="http://stub",
                        transport=ReporterTransport(
                            [("/check-runs/555", (403, {}, {}))]),
                        token_provider=provider, observer=events.append)
        self.assertNotIn("provider-tok", str(events))




class _RaisingProvider:
    """Raises a classified token-provider error on the Nth call."""

    def __init__(self, error, raise_on_call=1):
        self.error = error
        self.raise_on_call = raise_on_call
        self.calls = 0

    def get_token(self, *, force_refresh=False):
        self.calls += 1
        if self.calls >= self.raise_on_call:
            raise self.error
        return "tok"

    def invalidate(self):
        pass


class TestProviderErrorClassification(unittest.TestCase):
    """§3: terminal/retry classification at every auth point."""

    def _terminal_err(self, code="TOKEN_EXCHANGE_HTTP_403"):
        import token_provider as tp
        return tp.TokenExchangeTerminalError(code, "classified detail")

    def _retry_err(self, code="TOKEN_EXCHANGE_HTTP_5XX", retry_after=None):
        import token_provider as tp
        return tp.TokenExchangeRetryError(code, "classified", retry_after)

    def test_exchange_403_immediate_terminal_attempt1(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=1))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([]),
            token_provider=_RaisingProvider(
                self._terminal_err("TOKEN_EXCHANGE_HTTP_403")),
            max_attempts=8)
        self.assertEqual(outcome, "terminal")
        terminal = conn.params_of("last_error = %s, claim_id = NULL")[0]
        self.assertEqual(terminal[0], "TOKEN_TERMINAL:TOKEN_EXCHANGE_HTTP_403")

    def test_exchange_422_immediate_terminal(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=1))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([]),
            token_provider=_RaisingProvider(
                self._terminal_err("TOKEN_EXCHANGE_HTTP_422")),
            max_attempts=8)
        self.assertEqual(outcome, "terminal")
        terminal = conn.params_of("last_error = %s, claim_id = NULL")[0]
        self.assertIn("TOKEN_TERMINAL:TOKEN_EXCHANGE_HTTP_422", terminal[0])

    def test_malformed_immediate_terminal(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=1))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([]),
            token_provider=_RaisingProvider(
                self._terminal_err("TOKEN_EXCHANGE_MALFORMED")),
            max_attempts=8)
        self.assertEqual(outcome, "terminal")
        terminal = conn.params_of("last_error = %s, claim_id = NULL")[0]
        self.assertIn("TOKEN_EXCHANGE_MALFORMED", terminal[0])

    def test_scope_mismatch_immediate_terminal(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=1))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([]),
            token_provider=_RaisingProvider(
                self._terminal_err("TOKEN_SCOPE_MISMATCH")),
            max_attempts=8)
        self.assertEqual(outcome, "terminal")

    def test_429_retry_after_used_precisely(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=2))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([]),
            token_provider=_RaisingProvider(
                self._retry_err("TOKEN_EXCHANGE_RATE_LIMITED", retry_after=77)),
            max_attempts=8)
        self.assertEqual(outcome, "retry")
        retry = conn.params_of("next_retry_at = now() + make_interval")[0]
        self.assertEqual(retry[0], 77)

    def test_5xx_uses_bounded_backoff(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=2))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([]),
            token_provider=_RaisingProvider(
                self._retry_err("TOKEN_EXCHANGE_HTTP_5XX")),
            max_attempts=8)
        self.assertEqual(outcome, "retry")
        retry = conn.params_of("next_retry_at = now() + make_interval")[0]
        self.assertEqual(retry[0], cr._backoff_seconds(2))

    def test_retry_class_at_max_terminal_token(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=4))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([]),
            token_provider=_RaisingProvider(self._retry_err()),
            max_attempts=4)
        self.assertEqual(outcome, "terminal")
        terminal = conn.params_of("last_error = %s, claim_id = NULL")[0]
        self.assertEqual(terminal[0], "MAX_ATTEMPTS:TOKEN")

    def test_refresh_during_401_terminal_classified(self):
        # lookup answers 401; the FORCED refresh raises a terminal error —
        # must land TERMINAL (not the unclassified generic path)
        conn = reporter_conn(claimed_row(check_run_id=None, attempt=1))
        provider = _RaisingProvider(self._terminal_err(
            "TOKEN_EXCHANGE_HTTP_403"), raise_on_call=2)
        transport = ReporterTransport([
            ("/commits/%s/check-runs" % SHA_A, (401, {}, {}))])
        outcome = cr.publish_once(lambda: conn, api_base="http://stub",
                                  transport=transport,
                                  token_provider=provider, max_attempts=8)
        self.assertEqual(outcome, "terminal")
        terminal = conn.params_of("last_error = %s, claim_id = NULL")[0]
        self.assertEqual(terminal[0],
                         "TOKEN_TERMINAL:TOKEN_EXCHANGE_HTTP_403")

    def test_unknown_provider_error_retry_class_no_body(self):
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=1))
        outcome = cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([]),
            token_provider=_RaisingProvider(
                RuntimeError("super secret detail body")),
            max_attempts=8)
        self.assertEqual(outcome, "retry")
        retry = conn.params_of("next_retry_at = now() + make_interval")[0]
        self.assertNotIn("super secret", retry[1])
        self.assertIn("token", retry[1])

    def test_last_error_and_observer_never_leak(self):
        events = []
        conn = reporter_conn(claimed_row(check_run_id=555, attempt=1))
        cr.publish_once(
            lambda: conn, api_base="http://stub",
            transport=ReporterTransport([]),
            token_provider=_RaisingProvider(
                self._terminal_err("TOKEN_SCOPE_MISMATCH")),
            observer=events.append, max_attempts=8)
        blob = str(events) + str(conn.executed)
        for forbidden in ("classified detail", "Bearer ", "eyJ"):
            self.assertNotIn(forbidden, blob)


if __name__ == "__main__":
    unittest.main()
