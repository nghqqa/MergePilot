"""SQLite → PG 票据迁移工具测试(测试副本 + 隔离 PG 演练;零生产触碰)。"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "integration_prep"
_pg_pkg = sys.modules.setdefault("integration_prep_pkg",
                                 types.ModuleType("integration_prep_pkg"))
_pg_pkg.__path__ = [str(_TOOLS)]


def _load(name):
    if "integration_prep_pkg." + name in sys.modules:
        return sys.modules["integration_prep_pkg." + name]
    spec = importlib.util.spec_from_file_location("integration_prep_pkg." + name,
                                                  _TOOLS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["integration_prep_pkg." + name] = mod
    spec.loader.exec_module(mod)
    return mod


mig = _load("migrate_tickets_sqlite_pg")


def _seed_sqlite(path: str, rows):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE tickets (
        ticket_id TEXT PRIMARY KEY, run_id TEXT, repo TEXT, head_sha TEXT,
        action TEXT, params_hash TEXT, patch_fingerprint TEXT,
        finding_fingerprint TEXT, finding_id TEXT, attempt_no INTEGER,
        status TEXT, created_at TEXT, created_by_run TEXT,
        approval_expires_at TEXT, approved_by TEXT, approved_at TEXT,
        result_fingerprint TEXT, error TEXT)""")
    for r in rows:
        cur.execute("INSERT INTO tickets VALUES (%s)"
                    % ",".join("?" * 18),
                    (r.get("ticket_id"), r.get("run_id"), r.get("repo"),
                     r.get("head_sha"), r.get("action"), r.get("params_hash"),
                     r.get("patch_fingerprint"), r.get("finding_fingerprint"),
                     r.get("finding_id"), r.get("attempt_no", 1),
                     r.get("status"), r.get("created_at"),
                     r.get("created_by_run"), r.get("approval_expires_at"),
                     r.get("approved_by"), r.get("approved_at"),
                     r.get("result_fingerprint"), r.get("error")))
    conn.commit()
    conn.close()


GOOD_TEMPLATE = {"ticket_id": "tkt-g1", "run_id": None, "repo": "team/demo",
                 "head_sha": "a" * 40, "action": "generate_patch",
                 "params_hash": "1" * 64, "patch_fingerprint": "2" * 64,
                 "finding_id": "F1", "attempt_no": 1, "status": "APPROVED",
                 "created_at": "2026-09-22T10:00:00+00:00",
                 "created_by_run": "legacy",
                 "approval_expires_at": "2099-01-01T00:00:00+00:00",
                 "approved_by": "test-approver",
                 "approved_at": "2026-09-22T11:00:00+00:00"}


class MigrateToolTests(unittest.TestCase):
    """测试副本演练:隔离 PG(55432)+ 合成源库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sqlite_path = os.path.join(self.tmp.name, "src.db")
        dsn = os.environ.get(
            "MERGEPILOT_PG_TEST_DSN",
            "host=127.0.0.1 port=55432 user=mp_contract "
            "password=mp-contract-local-test dbname=mp_contract")
        if not (os.environ.get("MERGEPILOT_PG_CONTRACT") == "1"):
            self.skipTest("MERGEPILOT_PG_CONTRACT=1 未设置:跳过真实 PG 演练")
        self.dsn = dsn
        # 清理历史票据(独立测试库;仅本任务测试数据)
        conn0 = __import__("psycopg2").connect(dsn)
        conn0.autocommit = True
        cur0 = conn0.cursor()
        cur0.execute("DELETE FROM approval.ticket_audit WHERE actor='migration'")
        cur0.execute("DELETE FROM approval.tickets WHERE ticket_id LIKE 'tkt-%'")
        conn0.close()
        # 经 PgRunStore 合法创建 target+run(父记录真实存在,不伪造)
        _oppkg = __import__("types").ModuleType("orchestrator_v3")
        _orch = Path(__file__).resolve().parents[2] / "tools" / "orchestrator"
        _oppkg.__path__ = [str(_orch)]
        __import__("sys").modules.setdefault("orchestrator_v3", _oppkg)
        spec = importlib.util.spec_from_file_location(
            "orchestrator_v3.pg_runstore",
            Path(__file__).resolve().parents[2] / "tools" / "orchestrator" / "pg_runstore.py")
        mod = importlib.util.module_from_spec(spec)
        __import__("sys").modules["orchestrator_v3.pg_runstore"] = mod
        spec.loader.exec_module(mod)
        store = mod.PgRunStore(dsn)
        run, _ = store.create_run("team/demo", 9, "a" * 40, "legacy", "execution",
                                  "migrate-setup:legacy:execution")
        store.close()
        self.run_id = run["run_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _good(self, ticket_id="tkt-g1"):
        return dict(GOOD_TEMPLATE, ticket_id=ticket_id, run_id=self.run_id)

    def test_dry_run_default_writes_nothing(self):
        _seed_sqlite(self.sqlite_path, [self._good()])
        report = mig.run(self.sqlite_path, self.dsn, apply=False)
        self.assertEqual(report["mode"], "dry-run")
        import psycopg2
        conn = psycopg2.connect(self.dsn)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM approval.tickets WHERE ticket_id='tkt-g1'")
        self.assertEqual(cur.fetchone()[0], 0)   # dry-run 未写目标
        conn.close()

    def test_apply_import_and_field_verification(self):
        _seed_sqlite(self.sqlite_path, [self._good()])
        report = mig.run(self.sqlite_path, self.dsn, apply=True)
        self.assertTrue(
            report.get("verify", {}).get("ok"),
            json.dumps(report, ensure_ascii=False, default=str))
        import psycopg2
        conn = psycopg2.connect(self.dsn)
        cur = conn.cursor()
        cur.execute("SELECT run_id, repo_id, head_sha, status, approved_by, "
                    "approval_expires_at FROM approval.tickets "
                    "WHERE ticket_id=%s", (self.run_id and "tkt-g1",))
        row = cur.fetchone()
        cur.execute("SELECT to_status FROM approval.ticket_audit "
                    "WHERE ticket_id='tkt-g1' AND actor='migration'")
        audit = cur.fetchall()
        conn.close()
        self.assertEqual(row[0], self.run_id)
        self.assertEqual(row[3], "APPROVED")
        self.assertEqual(row[4], "test-approver")
        self.assertEqual(audit, [("APPROVED",)])   # 迁移审计行存在

    def test_rerun_idempotent_conflicts_not_overwritten(self):
        _seed_sqlite(self.sqlite_path, [self._good()])
        mig.run(self.sqlite_path, self.dsn, apply=True)
        # 目标侧人为改动:冲突行不得被重跑覆盖
        import psycopg2
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = True
        conn.cursor().execute("UPDATE approval.tickets SET status='APPROVED', "
                              "error='post-import state' WHERE ticket_id='tkt-g1'")
        conn.close()
        report = mig.run(self.sqlite_path, self.dsn, apply=True)
        self.assertEqual(report["import"]["skipped_conflict"], 1)
        self.assertEqual(report["import"]["inserted"], 0)
        conn = psycopg2.connect(self.dsn)
        cur = conn.cursor()
        cur.execute("SELECT error FROM approval.tickets WHERE ticket_id='tkt-g1'")
        self.assertEqual(cur.fetchone()[0], "post-import state")  # 未覆盖
        conn.close()

    def test_invalid_source_aborts_before_write(self):
        _seed_sqlite(self.sqlite_path, [dict(self._good(), ticket_id="tkt-bad",
                                             status="NOT_A_STATUS")])
        report = mig.run(self.sqlite_path, self.dsn, apply=True)
        self.assertTrue(str(report["result"]).startswith("ABORT"))
        self.assertIn("非法状态", report["validation_errors"][0])

    def test_missing_parent_skipped_not_fabricated(self):
        _seed_sqlite(self.sqlite_path, [dict(self._good(), ticket_id="tkt-orphan",
                                             run_id="run-nonexistent")])
        report = mig.run(self.sqlite_path, self.dsn, apply=True)
        self.assertEqual(report["parent_missing"][0]["ticket_id"], "tkt-orphan")
        self.assertEqual(report["import"]["inserted"], 0)  # 不伪造父记录
        import psycopg2
        conn = psycopg2.connect(self.dsn)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM approval.tickets WHERE ticket_id='tkt-orphan'")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
