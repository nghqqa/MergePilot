"""M2 审批纵向闭环集成验收(HTTP→真实隔离 PG,非 mock)。

票据由 store 直接创建(系统行为);HTTP POST 仅测 approve/reject 决策。
"""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
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
ISO1 = "2026-09-22T12:00:00+00:00"
ISO2 = "2026-09-22T12:05:00+00:00"
LATER = "2099-01-01T00:00:00+00:00"
APPROVER = "test-approver"
N = [0]  # 唯一 finding_id 计数器


def _seed_fixture(store):
    conn = pg_rs._connect(_DSN)
    conn.autocommit = True
    conn.cursor().execute("DROP SCHEMA IF EXISTS run CASCADE")
    for f in sorted((_ORCH.parent / "orchestrator" / "pg" / "migrations").glob("00*.sql")):
        if "003" in f.name or "004" in f.name:
            conn.cursor().execute(f.read_text(encoding="utf-8"))
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
    store.save_findings(run["run_id"], [
        {"finding_id": "gf-1", "finding_key": "path-traversal|api/upload.py",
         "source_stage": "review:generic", "category": "path-traversal",
         "severity": "HIGH", "confidence": "HIGH",
         "title": "Path traversal in upload", "path": "api/upload.py",
         "line": 42, "evidence_text": "PoC",
         "sources": [{"reviewer": "generic"}],
         "status": "AGGREGATED", "data_mode": "fixture"},
        {"finding_id": "gf-2", "finding_key": "style|api/names.py",
         "source_stage": "review:generic", "category": "style",
         "severity": "LOW", "confidence": "MEDIUM",
         "title": "Naming convention", "path": "api/names.py",
         "line": 10, "status": "AGGREGATED", "data_mode": "fixture"},
    ])
    store.save_validation(run["run_id"], "gf-1", "CONFIRMED",
                          anchor_status="in_diff")
    store.save_validation(run["run_id"], "gf-2", "REFUTED",
                          anchor_status="outside_diff")
    return run["run_id"]


def _free_port():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close()
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



def _cross_process_worker(dsn, ticket_id, event, actor, q):
    import importlib.util, types
    APPR = Path(__file__).resolve().parents[2] / 'tools' / 'approval'
    pkg = types.ModuleType('approval_pkg'); pkg.__path__ = [str(APPR)]
    __import__('sys').modules.setdefault('approval_pkg', pkg)
    spec = importlib.util.spec_from_file_location('approval_pkg.pg_store', APPR / 'pg_store.py')
    m = importlib.util.module_from_spec(spec)
    __import__('sys').modules.setdefault('approval_pkg.pg_store', m)
    spec.loader.exec_module(m)
    store = m.PostgreSQLTicketStore(dsn)
    try:
        r = store.transition(ticket_id, event, now=ISO1, actor=actor)
        q.put((event, r.ok))
    finally:
        store.close()


@unittest.skipUnless(GATED, "MERGEPILOT_PG_CONTRACT=1 未设置")
class M2ApprovalLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from approval_pkg.pg_store import PostgreSQLTicketStore
        from orchestrator_v3.pg_runstore import PgRunStore
        cls.run_store = PgRunStore(_DSN)
        cls.ticket_store = PostgreSQLTicketStore(_DSN)
        cls.run_id = _seed_fixture(cls.run_store)
        conn = pg_tk._connect(_DSN)
        conn.autocommit = True
        conn.cursor().execute("DELETE FROM approval.ticket_audit")
        conn.cursor().execute("DELETE FROM approval.tickets")
        conn.close()
        policy = type("P", (), {
            "allows_action": lambda s, a: a in ("generate_patch", "run_poc"),
            "can_approve": lambda s, p, r: p == APPROVER,
            "configured": True})()
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

    def _mk_ticket(self, **kw):
        core = _load("approval_pkg.approval", _APPR / "approval.py")
        b = core.Binding(
            run_id=kw.get("run_id", self.run_id),
            repo=kw.get("repo", REPO),
            head_sha=kw.get("head_sha", HEAD),
            action=kw.get("action", "generate_patch"),
            params_hash=core.canonical_hash(kw.get("params", {"x": 1})),
            patch_fingerprint=kw.get("patch_fingerprint", "e" * 64),
            finding_id=kw.get("finding_id"),
            finding_fingerprint=kw.get("finding_fingerprint"))
        t, c = self.ticket_store.create(
            b, approval_expires_at=kw.get("expires_at", LATER))
        return t, c

    def _post(self, path, principal=APPROVER):
        headers = {"X-Test-Principal": principal} if principal else {}
        return _req(self.base + path, method="POST", headers=headers)

    # ── 纵向闭环验收 ─────────────────────────────────────────────────────
    def test_01_run_detail_persisted_findings_validations(self):
        status, body = _req(self.base + "/api/runs/" + self.run_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["data_mode"], "fixture")
        self.assertEqual(len(body["findings"]), 2)
        self.assertEqual(body["findings"][0]["severity"], "HIGH")
        self.assertEqual(len(body["validations"]), 2)

    def test_02_approve_via_http(self):
        t, _ = self._mk_ticket()
        status, body = self._post("/api/approvals/%s/approve" % t.ticket_id)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        got = self.ticket_store.get(t.ticket_id)
        self.assertEqual(got.status, "APPROVED")
        self.assertEqual(got.approved_by, APPROVER)

    def test_03_reject_via_http(self):
        t, _ = self._mk_ticket(finding_id="gf-rej")
        status, body = self._post(
            "/api/approvals/%s/reject" % t.ticket_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "REJECTED")

    def test_04_reject_then_approve_fails(self):
        t, _ = self._mk_ticket(finding_id="gf-r1")
        self._post("/api/approvals/%s/reject" % t.ticket_id)
        status, body = self._post(
            "/api/approvals/%s/approve" % t.ticket_id)
        self.assertEqual(status, 409)

    def test_05_cross_process_single_winner(self):
        import multiprocessing
        t, _ = self._mk_ticket(finding_id="gf-race")
        q = multiprocessing.Queue()
        p1 = multiprocessing.Process(target=_cross_process_worker,
                                     args=(_DSN, t.ticket_id, "approve", APPROVER, q))
        p2 = multiprocessing.Process(target=_cross_process_worker,
                                     args=(_DSN, t.ticket_id, "reject", APPROVER, q))
        p1.start(); p2.start(); p1.join(); p2.join()
        results = [q.get() for _ in range(2)]
        self.assertEqual(sum(1 for r in results if r[1]), 1)

    def test_06_expiry_rejects(self):
        t, _ = self._mk_ticket(
            finding_id="gf-exp", expires_at="2020-01-01T00:00:00+00:00")
        status, body = self._post(
            "/api/approvals/%s/approve" % t.ticket_id)
        self.assertNotEqual(status, 200)

    def test_07_head_change_invalidates(self):
        store = self.run_store
        r_old, _ = store.create_run(REPO, 99, "e" * 40, "legacy", "execution",
                                    "seed-old:legacy:execution")
        store.transition_status(r_old["run_id"], "RUNNING", expected_status="PENDING")
        store.transition_status(r_old["run_id"], "SUCCEEDED", expected_status="RUNNING")
        r_new, _ = store.create_run(REPO, 99, "f" * 40, "legacy", "execution",
                                    "seed-new:legacy:execution")
        store.supersede_run(r_old["run_id"], r_new["run_id"])
        old = store.get_run(r_old["run_id"])
        self.assertEqual(old["superseded_by_run_id"], r_new["run_id"])
        self.assertEqual(old["status"], "SUPERSEDED")

    def test_08_run_detail_via_http_has_findings(self):
        status, body = _req(self.base + "/api/runs/" + self.run_id)
        self.assertEqual(status, 200)
        self.assertTrue(len(body.get("findings", [])) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
