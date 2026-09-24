"""console_pg HTTP→真实隔离 PG 集成验收(非 mock:HTTP 进入真实 PgRunStore)。"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_ORCH = Path(__file__).resolve().parents[2] / "tools" / "orchestrator"
_CON = Path(__file__).resolve().parents[2] / "tools" / "console_pg"
_oppkg = __import__("types").ModuleType("orchestrator_v3")
_oppkg.__path__ = [str(_ORCH)]
__import__("sys").modules["orchestrator_v3"] = _oppkg


def _load(full, path):
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    __import__("sys").modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


stages = _load("orchestrator_v3.stages", _ORCH / "stages.py")
pg_runstore = _load("orchestrator_v3.pg_runstore", _ORCH / "pg_runstore.py")
srv = _load("console_pg_server", _CON / "server.py")

_DSN = os.environ.get(
    "MERGEPILOT_PG_TEST_DSN",
    "host=127.0.0.1 port=55432 user=mp_contract "
    "password=mp-contract-local-test dbname=mp_pg_console")
GATED = os.environ.get("MERGEPILOT_PG_CONTRACT") == "1"


def _ensure_db(dbname: str):
    """确保隔离测试数据库存在(测试基础设施,非共享库)。"""
    import psycopg2
    admin = psycopg2.connect(
        "host=127.0.0.1 port=55432 user=mp_contract "
        "password=mp-contract-local-test dbname=postgres")
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dbname,))
    if cur.fetchone() is None:
        cur.execute("CREATE DATABASE " + dbname)
    admin.close()


_ensure_db("mp_pg_console")

REPO_A = "team/repo-alpha"
REPO_B = "team/repo-beta"          # 同 PR 编号,不同仓库(隔离验证用)
HEAD1 = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
HEAD2 = "b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4a1"
ISO1 = "2026-09-22T12:00:00+00:00"
ISO2 = "2026-09-22T12:05:00+00:00"


def _req(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@unittest.skipUnless(GATED, "MERGEPILOT_PG_CONTRACT=1 未设置:跳过真实 PG 验证")
class ConsolePgHttpTests(unittest.TestCase):
    """HTTP → 真实 PgRunStore → 隔离 PG(全程无 mock)。"""

    @classmethod
    def setUpClass(cls):
        # 完整 schema 重放:确保 run + approval 域全部就位
        import psycopg2
        conn = psycopg2.connect(_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DROP SCHEMA IF EXISTS run CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS approval CASCADE")
        mig_root = _ORCH.parent
        for d in (mig_root / "approval" / "pg" / "migrations",
                  mig_root / "orchestrator" / "pg" / "migrations"):
            for f in sorted(d.glob("*.sql")):
                cur.execute(f.read_text(encoding="utf-8"))
        conn.close()
        cls.store = pg_runstore.PgRunStore(_DSN)
        _seed(cls.store)
        cls.port = _free_port()
        cls.httpd = srv.ThreadingHTTPServer(
            ("127.0.0.1", cls.port), srv.make_handler(cls.store))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()

    def _get(self, path):
        return _req(self.base + path)

    # ── 能力/数据来源/认证 ────────────────────────────────────────────────
    def test_healthz_declares_fixture_and_no_auth(self):
        status, body = self._get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["data_mode"], "fixture")
        self.assertEqual(body["auth"], "not_implemented")

    def test_auth_session_401_honest(self):
        status, body = self._get("/api/auth/session")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["reason"], "not_authenticated")

    def test_capabilities_require_session_401(self):
        status, body = self._get("/api/me/capabilities?repo=team%2Frepo-alpha")
        self.assertEqual(status, 401)

    # ── 仓库列表与跨仓库隔离 ──────────────────────────────────────────────
    def test_repos_list_two_repos(self):
        status, body = self._get("/api/repos")
        self.assertEqual(status, 200)
        repo_ids = {r["repo_id"] for r in body["items"]}
        self.assertEqual(repo_ids, {REPO_A, REPO_B})

    def test_same_pr_number_no_cross_repo_leak(self):
        """两仓库同 PR 编号:互不串数据。"""
        status, a = self._get("/api/prs?repo=" + REPO_A.replace("/", "%2F"))
        self.assertEqual(a["items"][0]["head_sha"], HEAD1)
        self.assertEqual(a["items"][0]["pr_number"], 7)
        status, b = self._get("/api/prs?repo=" + REPO_B.replace("/", "%2F"))
        self.assertEqual(b["items"][0]["head_sha"], HEAD2)
        self.assertEqual(b["items"][0]["pr_number"], 7)

    def test_repo_param_required(self):
        status, body = self._get("/api/prs")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["reason"], "repo_required")

    # ── PR 详情/运行历史/多 head 多 run ───────────────────────────────────
    def test_pr_runs_multi_head_multi_chain(self):
        status, body = self._get(
            "/api/runs?repo=" + REPO_A.replace("/", "%2F") + "&pr=7")
        self.assertEqual(status, 200)
        self.assertEqual(body["data_mode"], "fixture")
        self.assertEqual(body["total"], 3)   # legacy(exec) + v3(shadow) + rerun
        run_ids = {i["run_id"] for i in body["items"]}
        self.assertEqual(len(run_ids), 3)
        chains = {i["run_id"]: (i["chain"], i["run_class"])
                  for i in body["items"]}
        self.assertIn(("legacy", "execution"), chains.values())
        self.assertIn(("v3", "evidence"), chains.values())

    def test_run_detail_stages_events_evidence(self):
        status, body = self._get("/api/runs/" + RUN_MAIN)
        self.assertEqual(status, 200)
        self.assertEqual(body["head_sha"], HEAD1)
        self.assertEqual(body["stages"]["review:generic"]["status"], "SUCCEEDED")
        self.assertEqual(body["stages"]["publish"]["status"], "SUCCEEDED")
        self.assertTrue(any(e["event_type"] == "run.created"
                            for e in body["events"]))
        self.assertIn("manifest_sha256", body["evidence"])
        # findings/validations/knowledge: 004 已建表,当前 seed 无 findings → 空列表
        self.assertIsInstance(body["findings"], list)
        self.assertIsInstance(body["validations"], list)

    def test_unknown_run_404_error_shape(self):
        status, body = self._get("/api/runs/run-nonexistent")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["reason"], "run_not_found")

    # ── 分页稳定 / 错误≠空 / 写方法 405 ──────────────────────────────────
    def test_pagination_stable(self):
        status, p1 = self._get("/api/runs?limit=2&offset=0")
        status, p2 = self._get("/api/runs?limit=2&offset=2")
        ids1 = {i["run_id"] for i in p1["items"]}
        ids2 = {i["run_id"] for i in p2["items"]}
        self.assertFalse(ids1 & ids2)            # 不重叠
        self.assertEqual(len(p1["items"]), 2)

    def test_db_failure_is_503_not_empty(self):
        """数据库不可达 → 503 错误,不伪装空列表(另起坏 DSN 服务)。"""
        bad = pg_runstore.PgRunStore("host=127.0.0.1 port=1 user=x "
                                     "password=y dbname=z connect_timeout=1")
        bad_httpd = srv.ThreadingHTTPServer(
            ("127.0.0.1", 0), srv.make_handler(bad))
        t = threading.Thread(target=bad_httpd.serve_forever, daemon=True)
        t.start()
        port = bad_httpd.server_address[1]
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/repos" % port)
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("expected 503")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 503)
                body = json.loads(e.read().decode("utf-8"))
                self.assertEqual(body["error"]["reason"],
                                 "backend_unavailable")
        finally:
            bad_httpd.shutdown()
            bad_httpd.server_close()
            bad.close()

    def test_write_methods_405(self):
        """写方法全部返回 405(服务端 read-only)。"""
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            req = urllib.request.Request(
                self.base + "/api/runs", data=b"{}", method=method)
            try:
                urllib.request.urlopen(req, timeout=5)
                self.fail("%s should be rejected" % method)
            except urllib.error.HTTPError as e:
                self.assertIn(e.code, (405, 501))


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _seed(store):
    """可重复 fixture:两仓库同 PR 号;同 PR 多 head;legacy+shadow;
    running/failed/completed;PARTIAL。"""
    conn = pg_runstore._connect(_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    for stmt in ("DELETE FROM run.run_events", "DELETE FROM run.stages",
                 "DELETE FROM run.runs", "DELETE FROM run.targets"):
        cur.execute(stmt)
    conn.close()
    global RUN_MAIN
    RUN_MAIN = None
    # repo-alpha: PR7 head1 legacy(execution, completed) + v3(shadow, partial)
    r1, _ = store.create_run(REPO_A, 7, HEAD1, "legacy", "execution",
                             "seed-d1:legacy:execution")
    RUN_MAIN = r1["run_id"]
    store.transition_status(r1["run_id"], "RUNNING",
                            expected_status="PENDING")
    st = stages.RunStages()
    st.transition("review:generic", stages.RUNNING, at=ISO1)
    st.transition("review:generic", stages.SUCCEEDED, at=ISO2)
    st.transition("publish", stages.RUNNING, at=ISO2)
    st.transition("publish", stages.SUCCEEDED, at=ISO2)
    store.save_stages(r1["run_id"], st)
    store.transition_status(r1["run_id"], "SUCCEEDED",
                            expected_status="RUNNING")
    r2, _ = store.create_run(REPO_A, 7, HEAD1, "v3", "evidence",
                             "seed-d2:v3:evidence", mode="shadow")
    st2 = stages.RunStages()
    st2.transition("risk", stages.RUNNING, at=ISO1)
    st2.transition("risk", stages.TIMEOUT, at=ISO1, error="agent timeout")
    store.save_stages(r2["run_id"], st2)   # PARTIAL(shadow 取证)
    store.transition_status(r2["run_id"], "FAILED",
                            expected_status="PENDING")
    # 显式重跑(同 head 同 class,seq=2,active)
    r4, _ = store.create_run(REPO_A, 7, HEAD1, "legacy", "execution",
                             "seed-rerun:legacy:execution",
                             trigger_kind="manual_rerun", triggered_by="ops")
    store.transition_status(r4["run_id"], "RUNNING",
                            expected_status="PENDING")
    # repo-beta: PR7 head2 legacy(running)
    r3, _ = store.create_run(REPO_B, 7, HEAD2, "legacy", "execution",
                             "seed-d3:legacy:execution")
    store.transition_status(r3["run_id"], "RUNNING",
                            expected_status="PENDING")


RUN_MAIN = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
