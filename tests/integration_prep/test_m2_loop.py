"""M2 审批纵向闭环集成验收(HTTP→真实隔离 PG,非 mock)。

覆盖: fixture 数据生成 → findings/validations 持久化 → 票据创建 →
审批列表 → HTTP 决策 → PG 状态变更 → 读模型一致。
门控: MERGEPILOT_PG_CONTRACT=1。
"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ORCH = _ROOT / "tools" / "orchestrator"
_APPR = _ROOT / "tools" / "approval"
_oppkg = __import__("types").ModuleType("orchestrator_v3")
_oppkg.__path__ = [str(_ORCH)]
__import__("sys").modules["orchestrator_v3"] = _oppkg
_apkg = __import__("types").ModuleType("approval_pkg")
_apkg.__path__ = [str(_APPR)]
__import__("sys").modules["approval_pkg"] = _apkg


def _load(full, path):
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    __import__("sys").modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


stages = _load("orchestrator_v3.stages", _ORCH / "stages.py")
pg_rs = _load("orchestrator_v3.pg_runstore", _ORCH / "pg_runstore.py")
pg_tk = _load("approval_pkg.pg_store", _APPR / "pg_store.py")
core = _load("approval_pkg.approval", _APPR / "approval.py")
srv = _load("console_pg_server", _ROOT / "tools" / "console_pg" / "server.py")

_DSN = os.environ.get(
    "MERGEPILOT_PG_TEST_DSN",
    "host=127.0.0.1 port=55432 user=mp_contract "
    "password=mp-contract-local-test dbname=mp_contract")
GATED = os.environ.get("MERGEPILOT_PG_CONTRACT") == "1"

REPO = "team/repo-m2"
HEAD = "c" * 40
HEAD2 = "d" * 40
ISO1 = "2026-09-22T12:00:00+00:00"
ISO2 = "2026-09-22T12:05:00+00:00"
LATER = "2099-01-01T00:00:00+00:00"
APPROVER = "test-approver"


def _seed_fixture(store):
    """可重复 fixture:一个 target + 两条 run + findings。"""
    conn = pg_rs._connect(_DSN)
    conn.autocommit = True
    conn.cursor().execute("DROP SCHEMA IF EXISTS run CASCADE")
    mig003 = (_ROOT / "tools" / "orchestrator" / "pg" / "migrations" /
              "003_run_domain.sql")
    conn.cursor().execute(mig003.read_text(encoding="utf-8"))
    mig004 = (_ROOT / "tools" / "orchestrator" / "pg" / "migrations" /
              "004_findings_validations.sql")
    conn.cursor().execute(mig004.read_text(encoding="utf-8"))
    conn.close()
    run, _ = store.create_run(REPO, 9, HEAD, "legacy", "execution",
                              "m2-seed:legacy:execution")
    store.transition_status(run["run_id"], "RUNNING", expected_status="PENDING")
    st = stages.RunStages()
    st.transition("review:generic", stages.RUNNING, at=ISO1)
    st.transition("review:generic", stages.SUCCEEDED, at=ISO2)
    st.transition("finding_validation", stages.RUNNING, at=ISO2)
    st.transition("finding_validation", stages.SUCCEEDED, at=ISO2)
    st.transition("publish", stages.RUNNING, at=ISO2)
    st.transition("publish", stages.SUCCEEDED, at=ISO2)
    store.save_stages(run["run_id"], st)
    store.transition_status(run["run_id"], "SUCCEEDED",
                            expected_status="RUNNING")
    # findings
    store.save_findings(run["run_id"], [
        {"finding_id": "gf-1", "finding_key": "path-traversal|api/upload.py",
         "source_stage": "review:generic", "category": "path-traversal",
         "severity": "HIGH", "confidence": "HIGH",
         "title": "Path traversal in upload", "path": "api/upload.py",
         "line": 42, "evidence_text": "PoC output",
         "sources": [{"reviewer": "generic", "severity": "HIGH"}],
         "status": "AGGREGATED", "data_mode": "fixture"},
        {"finding_id": "gf-2", "finding_key": "style|api/names.py",
         "source_stage": "review:generic", "category": "style",
         "severity": "LOW", "confidence": "MEDIUM",
         "title": "Naming convention", "path": "api/names.py",
         "line": 10, "status": "AGGREGATED", "data_mode": "fixture"},
    ])
    # validations
    store.save_validation(run["run_id"], "gf-1", "CONFIRMED",
                          anchor_status="in_diff")
    store.save_validation(run["run_id"], "gf-2", "REFUTED",
                          anchor_status="outside_diff")
    return run["run_id"]


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _req(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@unittest.skipUnless(GATED, "MERGEPILOT_PG_CONTRACT=1 未设置")
class M2ApprovalLoopTests(unittest.TestCase):
    """M2 纵向闭环: findings → validations → 票据 → HTTP 决策 → PG → 读模型。"""

    @classmethod
    def setUpClass(cls):
        from approval_pkg.pg_store import PostgreSQLTicketStore
        from orchestrator_v3.pg_runstore import PgRunStore
        cls.run_store = PgRunStore(_DSN)
        cls.ticket_store = PostgreSQLTicketStore(_DSN)
        cls.run_id = _seed_fixture(cls.run_store)
        # 清空票据
        conn = pg_tk._connect(_DSN)
        conn.autocommit = True
        conn.cursor().execute("DELETE FROM approval.ticket_audit")
        conn.cursor().execute("DELETE FROM approval.tickets")
        conn.close()
        # 构建 handler
        cls.ticket_store = PostgreSQLTicketStore(_DSN)
        policy = type("P", (), {
            "allows_action": lambda s, a: a in ("generate_patch", "run_poc"),
            "can_approve": lambda s, p, r: p == APPROVER,
            "configured": True,
        })()
        cls.port = _free_port()
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", cls.port),
            srv.make_handler(cls.run_store, cls.ticket_store,
                             test_auth=True, policy=policy))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.ticket_store.close()
        cls.run_store.close()

    def _post(self, path, data=None, principal=APPROVER):
        headers = {}
        if principal:
            headers["X-Test-Principal"] = principal
        return _req(self.base + path, method="POST", data=data,
                    headers=headers)

    def test_01_run_detail_has_findings_and_validations(self):
        """run 详情返回持久化的 findings 和 validations。"""
        status, body = _req(self.base + "/api/runs/" + self.run_id)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["findings"]), 2)
        self.assertEqual(body["findings"][0]["severity"], "HIGH")
        self.assertEqual(len(body["validations"]), 2)
        self.assertEqual(body["data_mode"], "fixture")

    def test_02_create_ticket_via_http(self):
        status, body = self._post("/api/approvals", {
            "run_id": self.run_id, "repo": REPO, "head_sha": HEAD,
            "action": "generate_patch",
            "params": {"target": "gf-1"},
            "patch_fingerprint": "e" * 64,
            "finding_id": "gf-1", "expires_at": LATER,
        }, principal=APPROVER)
        self.assertIn(status, (200, 201))
        self.assertEqual(body["status"], "PENDING")

    def test_03_approve_via_http(self):
        status, body = self._post("/api/approvals", {
            "run_id": self.run_id, "repo": REPO, "head_sha": HEAD,
            "action": "run_poc", "params": {"target": "gf-1"},
            "finding_id": "gf-2", "expires_at": LATER,
        }, principal=APPROVER)
        tid = body["ticket_id"]
        status, body = self._post(
            "/api/approvals/%s/approve" % tid, {}, principal=APPROVER)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        t = self.ticket_store.get(tid)
        self.assertEqual(t.status, "APPROVED")
        self.assertEqual(t.approved_by, APPROVER)

    def test_04_reject_via_http(self):
        status, body = self._post("/api/approvals", {
            "run_id": self.run_id, "repo": REPO, "head_sha": HEAD,
            "action": "generate_patch", "params": {"k": "v"},
            "patch_fingerprint": "f" * 64, "finding_id": "gf-r",
            "expires_at": LATER,
        }, principal=APPROVER)
        tid = body["ticket_id"]
        status, body = self._post(
            "/api/approvals/%s/reject" % tid, {}, principal=APPROVER)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "REJECTED")

    def test_05_reject_then_approve_fails(self):
        status, body = self._post("/api/approvals", {
            "run_id": self.run_id, "repo": REPO, "head_sha": HEAD,
            "action": "generate_patch", "params": {"x": 1},
            "patch_fingerprint": "e" * 64, "finding_id": "gf-r2",
            "expires_at": LATER,
        }, principal=APPROVER)
        tid = body["ticket_id"]
        self._post("/api/approvals/%s/reject" % tid, {}, principal=APPROVER)
        status, body = self._post(
            "/api/approvals/%s/approve" % tid, {}, principal=APPROVER)
        self.assertEqual(status, 409)  # REJECTED → 不能 approve

    def test_06_cross_process_single_winner(self):
        import multiprocessing
        status, body = self._post("/api/approvals", {
            "run_id": self.run_id, "repo": REPO, "head_sha": HEAD,
            "action": "run_poc", "params": {"x": 1},
            "finding_id": "gf-race", "expires_at": LATER,
        }, principal=APPROVER)
        tid = body["ticket_id"]

        def _worker(event, q):
            r = self.ticket_store.transition(tid, event, now=ISO1,
                                             actor=APPROVER)
            q.put((event, r.ok, r.status))

        q = multiprocessing.Queue()
        p1 = multiprocessing.Process(target=_worker, args=("approve", q))
        p2 = multiprocessing.Process(target=_worker, args=("reject", q))
        p1.start()
        p2.start()
        p1.join()
        p2.join()
        results = [q.get() for _ in range(2)]
        wins = [r for r in results if r[1]]
        self.assertEqual(len(wins), 1)  # 恰好一个成功

    def test_07_expiry_410(self):
        from datetime import datetime, timezone
        past = "2020-01-01T00:00:00+00:00"
        status, body = self._post("/api/approvals", {
            "run_id": self.run_id, "repo": REPO, "head_sha": HEAD,
            "action": "generate_patch", "params": {"x": 1},
            "patch_fingerprint": "e" * 64, "finding_id": "gf-exp",
            "expires_at": past,
        }, principal=APPROVER)
        tid = body["ticket_id"]
        status, body = self._post(
            "/api/approvals/%s/approve" % tid, {}, principal=APPROVER)
        self.assertNotEqual(status, 200)  # 过期 → 拒绝

    def test_08_disabled_action_403(self):
        status, body = self._post("/api/approvals", {
            "run_id": self.run_id, "repo": REPO, "head_sha": HEAD,
            "action": "publish_result",  # 不在 D-1 fixture 集中
            "params": {"x": 1}, "patch_fingerprint": "e" * 64,
            "expires_at": LATER,
        }, principal=APPROVER)
        # publish_result 未在 fixture 集中 → 创建被拒
        # 注意:fixture 集={generate_patch, run_poc};publish_result 不在
        self.assertIn(status, (403, 422))

    def test_09_run_detail_shows_validation_status(self):
        """finding validation 结果在 run 详情中可见。"""
        status, body = _req(self.base + "/api/runs/" + self.run_id)
        findings = body.get("findings", [])
        self.assertTrue(any(f["severity"] == "HIGH" for f in findings))
        self.assertTrue(any(f["status"] == "CONFIRMED" for f in findings))

    def test_10_head_change_invalidates(self):
        """PR 更新(新 head)→ 旧 run 标记 SUPERSEDED,旧结论不代表当前。"""
        store = self.run_store
        r_old, _ = store.create_run(REPO, 99, "e" * 40, "legacy", "execution",
                                    "seed-old:legacy:execution")
        # 模拟 PR 更新:新 head run 创建,旧 run superseded
        r_new, _ = store.create_run(REPO, 99, "f" * 40, "legacy", "execution",
                                    "seed-new:legacy:execution")
        store.supersede_run(r_old["run_id"], r_new["run_id"])
        old = store.get_run(r_old["run_id"])
        self.assertEqual(old["superseded_by_run_id"], r_new["run_id"])
        self.assertEqual(old["status"], "SUPERSEDED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
