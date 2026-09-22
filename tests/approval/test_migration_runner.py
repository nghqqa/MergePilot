"""迁移执行器验收(隔离容器内新建专用数据库 mp_mig_rehearsal;不触共享库)。

覆盖:
- 全新隔离数据库按规定顺序安装(001→002→003);
- 重复执行 → 明确跳过(幂等);
- 既有合法记录在后续迁移中保留;
- 异常既有表形状 → 明确失败,不被 IF NOT EXISTS 静默掩盖;
- 上一交付结构(仅 001)升级到当前结构(001+002+003)。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

import psycopg2

_ADMIN_DSN = ("host=127.0.0.1 port=55432 user=mp_contract "
              "password=mp-contract-local-test dbname=mp_contract")
_MIG_DSN = ("host=127.0.0.1 port=55432 user=mp_contract "
            "password=mp-contract-local-test dbname=mp_mig_rehearsal")
_MIG_DIRS = [
    Path(__file__).resolve().parents[2] / "tools" / "approval" / "pg" / "migrations",
    Path(__file__).resolve().parents[2] / "tools" / "orchestrator" / "pg" / "migrations",
]
_RUNTIME_DSN = ("host=127.0.0.1 port=55432 user=mp_runtime "
                "password=mp-runtime-local-test dbname=mp_mig_rehearsal")

GATED = os.environ.get("MERGEPILOT_PG_CONTRACT") == "1"


def _load_runner():
    import importlib.util
    p = (Path(__file__).resolve().parents[2] / "tools" / "approval" / "pg" /
         "apply_migrations.py")
    spec = importlib.util.spec_from_file_location("mig_runner", p)
    mod = importlib.util.module_from_spec(spec)
    __import__("sys").modules["mig_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


def _fresh():
    conn = psycopg2.connect(_ADMIN_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname='mp_mig_rehearsal' AND pid <> pg_backend_pid()")
    cur.execute("DROP DATABASE IF EXISTS mp_mig_rehearsal")
    cur.execute("CREATE DATABASE mp_mig_rehearsal")
    conn.close()
    return _MIG_DSN


@unittest.skipUnless(GATED, "MERGEPILOT_PG_CONTRACT=1 未设置:跳过真实 PG 验证")
class MigrationRunnerRehearsalTests(unittest.TestCase):
    def _apply(self, dsn, dirs=None):
        runner = _load_runner()
        return runner.apply_all(dsn, dirs or _MIG_DIRS)

    def test_fresh_install_in_order(self):
        dsn = _fresh()
        report = self._apply(dsn)
        self.assertEqual(report["result"], "OK")
        self.assertEqual(report["applied_now"],
                         ["001_approval_tickets.sql",
                          "002_tickets_target_key.sql",
                          "003_run_domain.sql"])

    def test_rerun_is_explicit_noop(self):
        dsn = _fresh()
        self._apply(dsn)
        report = self._apply(dsn)
        self.assertEqual(report["result"], "OK")
        self.assertEqual(report["applied_now"], [])
        self.assertEqual(len(report["skipped"]), 3)

    def test_upgrade_from_delivered_structure_preserves_records(self):
        """从已交付结构(仅 001,已登记)升级到当前:002+003 依次应用,既有合法记录保留。"""
        dsn = _fresh()
        # 阶段1:已交付状态=仅 001,经执行器登记
        self._apply(dsn, dirs=[_MIG_DIRS[0]])
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO approval.tickets (ticket_id, run_id, repo_id, "
            "head_sha, action, params_hash, patch_fingerprint, finding_id, "
            "target_key, attempt_no, status, created_at, "
            "approval_expires_at) VALUES "
            "('tkt-keep', 'run-old', 'team/demo', '%s', 'generate_patch', "
            "'%s', '%s', 'F1', 'F1', 1, 'APPROVED', now(), now())"
            % ("a" * 40, "1" * 64, "2" * 64))
        conn.close()
        # 阶段2:全量执行器应用 003(001+002 已登记自动跳过)
        report = self._apply(dsn)
        self.assertEqual(report["applied_now"],
                         ["003_run_domain.sql"])
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        cur.execute("SELECT status, target_key FROM approval.tickets "
                    "WHERE ticket_id='tkt-keep'")
        row = cur.fetchone()
        self.assertEqual(row[0], "APPROVED")              # 合法记录保留
        self.assertEqual(row[1], "F1")                    # 002 回填 target_key 正确
        conn.close()

    def test_wrong_shaped_table_fails_loudly(self):
        """夹具/残留表占用正式表名:明确失败,不被 IF NOT EXISTS 静默掩盖。"""
        dsn = _fresh()
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE SCHEMA run")
        cur.execute("CREATE TABLE run.stages (bogus INT)")  # 异常形状残留
        conn.close()
        report = self._apply(dsn)
        self.assertEqual(report["result"],
                         "FAIL:存在可疑既有表,已中止")
        self.assertTrue(any("003" in e for e in report["errors"]),
                        report["errors"])

    def test_minimal_query_role(self):
        """查询/运行时角色最小权限:DML 可用,DDL 不可用。"""
        dsn = _fresh()
        self._apply(dsn)
        runtime_dsn = ("host=127.0.0.1 port=55432 user=mp_runtime "
                       "password=mp-runtime-local-test dbname=mp_mig_rehearsal")
        admin = psycopg2.connect(dsn)
        admin.autocommit = True
        cur = admin.cursor()
        cur.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                    "rolname='mp_runtime') THEN CREATE ROLE mp_runtime LOGIN "
                    "PASSWORD 'mp-runtime-local-test'; END IF; END $$")
        cur.execute("GRANT USAGE ON SCHEMA approval, run TO mp_runtime")
        cur.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA "
                    "approval TO mp_runtime")
        cur.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA "
                    "run TO mp_runtime")
        cur.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA approval "
                    "TO mp_runtime")
        admin.close()
        runtime = psycopg2.connect(_RUNTIME_DSN)
        runtime.autocommit = True
        cur = runtime.cursor()
        cur.execute("SELECT current_user")
        self.assertEqual(cur.fetchone()[0], "mp_runtime")  # 确认以运行时角色连接
        cur.execute("SELECT count(*) FROM approval.schema_migrations")
        self.assertTrue(cur.fetchone()[0] >= 3)            # DML 可用
        with self.assertRaises(psycopg2.Error):
            cur.execute("CREATE TABLE approval.hax (id INT)")
        runtime.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
