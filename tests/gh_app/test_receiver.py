"""Receiver contract tests (M8-GH-1 §3) — fully mocked DB."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT / "tools" / "gh-app")):
    import sys
    if p not in sys.path:
        sys.path.insert(0, p)

import receiver as rec                          # noqa: E402
from fakes import FakeConnection                # noqa: E402

SECRET = "test-webhook-secret"
REPO = "nghqqa/MergePilot"
HEAD = "a" * 40
BASE = "b" * 40
DELIVERY = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
ALLOW = frozenset({REPO})


def pr_body(action="opened", **extra):
    payload = {
        "action": action,
        "installation": {"id": 42},
        "repository": {"full_name": REPO, "node_id": "R_x"},
        "pull_request": {
            "number": 101,
            "head": {"sha": HEAD, "ref": "feature/x", "label": "x:y"},
            "base": {"sha": BASE, "ref": "main"},
            "title": "A PR", "draft": False,
        },
        "sender": {"login": "someone"},
    }
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


def sign(raw, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw,
                                hashlib.sha256).hexdigest()


def call(raw, conn, *, event="pull_request", delivery=DELIVERY,
         signature=None, allowlist=ALLOW, secret=SECRET):
    # 签名固定用正确 SECRET 计算;secret 参数是验证方口令(测错口令路径)。
    return rec.handle_webhook(
        raw=raw, event_header=event, delivery_header=delivery,
        signature_header=signature if signature is not None
        else sign(raw, SECRET),
        secret=secret, connect=lambda: conn, allowlist=allowlist)


class TestHmacAndSize(unittest.TestCase):

    def test_valid_signature_new_delivery_202(self):
        conn = FakeConnection()
        status, outcome, _ = call(pr_body(), conn)
        self.assertEqual(status, 202)
        self.assertEqual(outcome, "accepted")
        self.assertEqual(len(conn.executed), 1)
        sql, params = conn.executed[0]
        self.assertIn("INSERT INTO public.github_deliveries", sql)
        # INSERT-only 合同:无 ON CONFLICT(仲裁列需 SELECT,真实 PG 拒绝)
        self.assertNotIn("ON CONFLICT", sql)
        self.assertNotIn("RETURNING", sql)      # 亦无表级 SELECT 依赖
        self.assertEqual(params[-1], "PENDING")
        self.assertEqual(conn.commits, 1)

    def test_tampered_signature_401_zero_db(self):
        conn = FakeConnection()
        raw = pr_body()
        status, outcome, _ = call(raw, conn,
                                  signature="sha256=" + "0" * 64)
        self.assertEqual(status, 401)
        self.assertEqual(conn.executed, [])      # 零数据库写入

    def test_wrong_secret_401_zero_db(self):
        conn = FakeConnection()
        raw = pr_body()
        status, _, _ = call(raw, conn, secret="other-secret")
        self.assertEqual(status, 401)
        self.assertEqual(conn.executed, [])

    def test_missing_signature_header_401(self):
        conn = FakeConnection()
        status, _, _ = rec.handle_webhook(
            raw=pr_body(), event_header="pull_request",
            delivery_header=DELIVERY, signature_header=None, secret=SECRET,
            connect=lambda: conn, allowlist=ALLOW)
        self.assertEqual(status, 401)
        self.assertEqual(conn.executed, [])

    def test_oversize_body_413_zero_db(self):
        conn = FakeConnection()
        raw = b"x" * (rec.MAX_BODY_BYTES + 1)
        status, outcome, _ = call(raw, conn)
        self.assertEqual(status, 413)
        self.assertEqual(conn.executed, [])


class TestStrictParsing(unittest.TestCase):

    def test_duplicate_json_key_400_zero_db(self):
        conn = FakeConnection()
        raw = b'{"action":"opened","action":"opened"}'
        status, _, _ = call(raw, conn)
        self.assertEqual(status, 400)
        self.assertEqual(conn.executed, [])

    def test_nan_rejected_400(self):
        conn = FakeConnection()
        raw = b'{"action":"opened","x":NaN}'
        status, _, _ = call(raw, conn)
        self.assertEqual(status, 400)
        self.assertEqual(conn.executed, [])

    def test_infinity_rejected_400(self):
        conn = FakeConnection()
        raw = b'{"action":"opened","x":Infinity}'
        status, _, _ = call(raw, conn)
        self.assertEqual(status, 400)
        self.assertEqual(conn.executed, [])

    def test_unknown_github_fields_allowed(self):
        conn = FakeConnection()
        raw = pr_body(brand_new_field={"nested": [1, 2, 3]})
        status, outcome, _ = call(raw, conn)
        self.assertEqual((status, outcome), (202, "accepted"))
        canonical = json.loads(conn.executed[0][1][9])
        self.assertNotIn("brand_new_field", canonical)  # 只存最小信封
        self.assertNotIn("sender", canonical)

    def test_missing_envelope_field_400(self):
        conn = FakeConnection()
        raw = json.dumps({"action": "opened",
                          "repository": {"full_name": REPO}}).encode()
        status, _, _ = call(raw, conn)
        self.assertEqual(status, 400)
        self.assertEqual(conn.executed, [])

    def test_canonical_payload_minimal_and_body_sha(self):
        conn = FakeConnection()
        raw = pr_body()
        call(raw, conn)
        params = conn.executed[0][1]
        canonical = json.loads(params[9])
        self.assertEqual(canonical["event_name"], "pull_request")
        self.assertEqual(canonical["installation_id"], 42)
        self.assertEqual(canonical["repo"], REPO)
        self.assertEqual(canonical["pr_number"], 101)
        self.assertEqual(canonical["observed_head_sha"], HEAD)
        self.assertEqual(canonical["branch"], "feature/x")
        self.assertEqual(params[8],
                         hashlib.sha256(raw).hexdigest())  # body_sha256


class TestClassificationAndReplay(unittest.TestCase):

    def test_ping_ignored_200(self):
        conn = FakeConnection()
        raw = json.dumps({"zen": "ok", "hook_id": 1}).encode()
        status, outcome, _ = call(raw, conn, event="ping")
        self.assertEqual((status, outcome), (200, "ignored"))
        self.assertEqual(conn.executed[0][1][-1], "IGNORED")

    def test_unmapped_action_ignored(self):
        conn = FakeConnection()
        status, outcome, _ = call(pr_body(action="closed"), conn)
        self.assertEqual((status, outcome), (200, "ignored"))
        self.assertEqual(conn.executed[0][1][-1], "IGNORED")

    def test_non_allowlisted_repo_ignored(self):
        conn = FakeConnection()
        raw = json.dumps({
            "action": "opened", "installation": {"id": 42},
            "repository": {"full_name": "someone/else"},
            "pull_request": {"number": 1,
                             "head": {"sha": HEAD, "ref": "b"},
                             "base": {"sha": BASE}}}).encode()
        status, outcome, _ = call(raw, conn)
        self.assertEqual((status, outcome), (200, "ignored"))

    def test_guid_replay_unique_violation_200(self):
        from fakes import FakeUniqueViolation
        conn = FakeConnection()
        conn.enqueue("INSERT INTO public.github_deliveries",
                     raise_exc=FakeUniqueViolation("duplicate key ... "
                                                   "github_deliveries_pkey"))
        status, outcome, _ = call(pr_body(), conn)
        self.assertEqual((status, outcome), (200, "duplicate"))
        self.assertEqual(conn.rollbacks, 1)

    def test_db_failure_503_and_rollback(self):
        class Boom(FakeConnection):
            def _on_execute(self, sql, params):
                raise RuntimeError("db down")
        conn = Boom()
        status, outcome, _ = call(pr_body(), conn)
        self.assertEqual((status, outcome), (503, "error"))
        self.assertEqual(conn.rollbacks, 1)

    def test_receiver_never_touches_governance_tables(self):
        conn = FakeConnection()
        call(pr_body(), conn)
        for governed in ("task_runs", "stage_runs", "dispatch_outbox",
                         "stage_events"):
            self.assertNotIn(governed, conn.executed[0][0])

    def test_healthz_select_one(self):
        conn = FakeConnection()
        self.assertTrue(rec.healthz(lambda: conn))
        self.assertIn("SELECT 1", conn.sqls()[0])


class TestHttpServerHelpers(unittest.TestCase):

    def test_allowlist_from_env(self):
        import http_server
        with unittest.mock.patch.dict(
                os.environ, {"GITHUB_REPO_ALLOWLIST": "a/b, c/d"}):
            self.assertEqual(http_server._allowlist_from_env(),
                             frozenset({"a/b", "c/d"}))
        with unittest.mock.patch.dict(os.environ, {"GITHUB_REPO_ALLOWLIST":
                                                   ""}):
            self.assertIsNone(http_server._allowlist_from_env())


import unittest.mock  # noqa: E402  (used above)

if __name__ == "__main__":
    unittest.main()
