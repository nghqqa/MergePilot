"""PostgreSQLTicketStore 真实隔离 PG 验证。

环境:隔离容器 mp-pg-contract-test(postgres:16, 127.0.0.1:55432, 独立卷),
      数据库 mp_contract;**不连接共享业务库**。
门控:MERGEPILOT_PG_CONTRACT=1(未设置跳过,不伪造 PG 验证)。
覆盖:TicketStore 契约集(7 项)+ PG 专属验收:
  NULL 唯一性(NULLS NOT DISTINCT)/ 非 NULL 唯一 / 不同目标不互斥 /
  跨进程竞争(独立 OS 进程)/ 终态后新 attempt / 时区与到期边界 /
  回滚原子性(审计与状态同事务)/ 最小权限角色(运行时角色不能改 schema)。
"""
from __future__ import annotations

import importlib.util
import multiprocessing
import os
import unittest
from pathlib import Path

_DSN_ADMIN = os.environ.get(
    "MERGEPILOT_PG_TEST_DSN",
    "host=127.0.0.1 port=55432 user=mp_contract "
    "password=mp-contract-local-test dbname=mp_contract")
_DSN_RUNTIME = os.environ.get(
    "MERGEPILOT_PG_RUNTIME_DSN",
    "host=127.0.0.1 port=55432 user=mp_runtime "
    "password=mp-runtime-local-test dbname=mp_contract")
MIGRATION = (Path(__file__).resolve().parents[2] / "tools" / "approval" / "pg" /
             "migrations" / "001_approval_tickets.sql")

_APPROVAL_DIR = Path(__file__).resolve().parents[2] / "tools" / "approval"
_pkg = __import__("types").ModuleType("approval_pkg")
_pkg.__path__ = [str(_APPROVAL_DIR)]
__import__("sys").modules["approval_pkg"] = _pkg


def _load(name, path):
    spec = importlib.util.spec_from_file_location("approval_pkg." + name, path)
    mod = importlib.util.module_from_spec(spec)
    __import__("sys").modules["approval_pkg." + name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("approval", _APPROVAL_DIR / "approval.py")
pg_store = _load("pg_store", _APPROVAL_DIR / "pg_store.py")
contract = _load("contract", Path(__file__).resolve().parent / "store_contract.py")
contract.PARAMS_HASH = core.canonical_hash({"method": "suggestion"})
contract.PATCH_FP = core.canonical_hash("patch-bytes")

NOW = "2026-09-22T12:00:00+00:00"
LATER = "2026-09-22T13:00:00+00:00"
APPROVER = "test-approver"

GATED = os.environ.get("MERGEPILOT_PG_CONTRACT") == "1"


def _admin_conn():
    import psycopg2
    return psycopg2.connect(_DSN_ADMIN)


def _apply_migration():
    conn = _admin_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(MIGRATION.read_text(encoding="utf-8"))
    # 夹具父数据(tickets FK 目标)
    cur.execute("INSERT INTO run.repos (repo_id) VALUES ('team/demo') "
                "ON CONFLICT DO NOTHING")
    cur.execute("INSERT INTO run.runs (run_id, repo_id) VALUES ('run-contract', "
                "'team/demo') ON CONFLICT DO NOTHING")
    # 运行时最小权限角色(不存在则建;密码=本地隔离测试专用)
    cur.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                "rolname='mp_runtime') THEN CREATE ROLE mp_runtime LOGIN "
                "PASSWORD 'mp-runtime-local-test'; END IF; END $$")
    cur.execute("GRANT USAGE ON SCHEMA approval, run TO mp_runtime")
    cur.execute("GRANT SELECT, INSERT, UPDATE ON approval.tickets TO mp_runtime")
    cur.execute("GRANT SELECT, INSERT ON approval.ticket_audit TO mp_runtime")
    cur.execute("GRANT USAGE, SELECT ON SEQUENCE "
                "approval.ticket_audit_id_seq TO mp_runtime")
    cur.execute("GRANT SELECT, INSERT, UPDATE ON run.runs, run.repos TO mp_runtime")
    conn.close()


def _cleanup_tickets():
    """容器跨 pytest 运行持久:每测前清空票据与审计(独立测试库,仅本任务数据)。"""
    conn = _admin_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DELETE FROM approval.ticket_audit")
    cur.execute("DELETE FROM approval.tickets")
    conn.close()


def _pg_worker(queue, dsn, ticket_id, event, actor):
    """独立 OS 进程:打开自己的 PG 连接执行一次转移(跨进程验证)。"""
    try:
        store = pg_store.PostgreSQLTicketStore(dsn)
        try:
            r = store.transition(ticket_id, event, now=NOW, actor=actor)
            queue.put((event, r.ok, r.reason, r.status))
        finally:
            store.close()
    except Exception as e:                                   # pragma: no cover
        queue.put((event, False, type(e).__name__ + ":" + str(e)[:80], "?"))


@unittest.skipUnless(GATED, "MERGEPILOT_PG_CONTRACT=1 未设置:跳过真实 PG 验证")
class PgStoreContractTests(contract.StoreContractMixin):
    """TicketStore 契约集在真实隔离 PostgreSQL 上运行。"""

    Approver = APPROVER
    Now = NOW
    Later = LATER

    @classmethod
    def setUpClass(cls):
        _apply_migration()
        cls.core = core

    def setUp(self):
        _cleanup_tickets()
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.store = self.make_store()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make_store(self):
        return pg_store.PostgreSQLTicketStore(_DSN_ADMIN)


@unittest.skipUnless(GATED, "MERGEPILOT_PG_CONTRACT=1 未设置:跳过真实 PG 验证")
class PgSpecificAcceptance(unittest.TestCase):
    """PG 专属验收(提示词五节逐项)。"""

    @classmethod
    def setUpClass(cls):
        _apply_migration()
        cls.core = core
        cls.store = pg_store.PostgreSQLTicketStore(_DSN_ADMIN)
        cls.runtime = pg_store.PostgreSQLTicketStore(_DSN_RUNTIME)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.runtime.close()

    def setUp(self):
        _cleanup_tickets()

    def _binding(self, **over):
        kw = dict(run_id="run-contract", repo="team/demo", head_sha="a" * 40,
                  action="generate_patch",
                  params_hash=core.canonical_hash({}),
                  patch_fingerprint=core.canonical_hash("patch"),
                  finding_id="F1")
        kw.update(over)
        return core.Binding(**kw)

    def test_null_finding_id_single_active_ticket(self):
        """finding_id=NULL:NULLS NOT DISTINCT 保证仍只允许一张活动票。"""
        b = self._binding(finding_id=None)
        t1, c1 = self.store.create(b)
        t2, c2 = self.store.create(b)
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(t1.ticket_id, t2.ticket_id)

    def test_non_null_finding_id_uniqueness(self):
        b1 = self._binding(finding_id="F1")
        b2 = self._binding(finding_id="F2")
        t1, c1 = self.store.create(b1)
        t2, c2 = self.store.create(b2)
        self.assertTrue(c1 and c2)                     # 不同 finding 不互斥
        self.assertNotEqual(t1.ticket_id, t2.ticket_id)

    def test_cross_process_race_single_winner(self):
        """跨进程(独立 OS 进程)approve/reject 竞争:恰好一个成功。"""
        import multiprocessing
        t, _ = self.store.create(self._binding(), approval_expires_at=LATER)
        q = multiprocessing.get_context("spawn").Queue()
        procs = [
            multiprocessing.Process(target=_pg_worker,
                                    args=(q, _DSN_RUNTIME, t.ticket_id, ev, APPROVER))
            for ev in ("approve", "reject")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
        results = [q.get(timeout=5) for _ in procs]
        wins = [r for r in results if r[1]]
        self.assertEqual(len(wins), 1, results)
        final = self.store.get(t.ticket_id)
        self.assertIn(final.status, (core.APPROVED, core.REJECTED))

    def test_terminal_allows_new_attempt(self):
        t1, _ = self.store.create(self._binding(), attempt_no=1)
        self.store.transition(t1.ticket_id, "reject", now=NOW, actor=APPROVER)
        t2, created = self.store.create(self._binding(), attempt_no=2,
                                        approval_expires_at=LATER)
        self.assertTrue(created)
        self.assertEqual(t2.attempt_no, 2)

    def test_timezone_and_expiry_boundary(self):
        import datetime as dt
        store = self.store
        b = self._binding(finding_id="TZ")
        t, _ = store.create(b, approval_expires_at="2026-09-22T13:00:00+00:00")
        # 截止前(带时区)可以批准
        r = store.transition(t.ticket_id, "approve",
                             now=dt.datetime(2026, 9, 22, 12, 59,
                                             tzinfo=dt.timezone.utc),
                             actor=APPROVER)
        self.assertTrue(r.ok)
        # 截止后:新票 approve 拒绝(先过期)
        b2 = self._binding(finding_id="TZ2")
        t2, _ = store.create(b2, approval_expires_at="2026-09-22T13:00:00+00:00")
        r = store.transition(t2.ticket_id, "approve",
                             now=dt.datetime(2026, 9, 22, 13, 0, 1,
                                             tzinfo=dt.timezone.utc),
                             actor=APPROVER)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "EXPIRED")

    def test_rollback_atomicity_state_and_audit(self):
        """审计与状态同事务:审计写入被拒 → 状态回滚,不产生半条状态/孤立审计。"""
        import psycopg2
        admin = _admin_conn()
        admin.autocommit = True
        cur = admin.cursor()
        t, _ = self.runtime.create(self._binding(finding_id="RA"),
                                   approval_expires_at=LATER)  # 授权在位时先建票
        cur.execute("REVOKE INSERT ON approval.ticket_audit FROM mp_runtime")
        try:
            with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
                self.runtime.transition(t.ticket_id, "approve", now=NOW,
                                        actor=APPROVER)
            got = self.store.get(t.ticket_id)
            self.assertEqual(got.status, core.PENDING)     # 状态未变(回滚)
            cur.execute("SELECT count(*) FROM approval.ticket_audit "
                        "WHERE ticket_id=%s AND to_status='APPROVED'",
                        (t.ticket_id,))
            self.assertEqual(cur.fetchone()[0], 0)         # 被拒转移无审计(回滚干净)
            cur.execute("SELECT count(*) FROM approval.ticket_audit "
                        "WHERE ticket_id=%s AND to_status='PENDING'",
                        (t.ticket_id,))
            self.assertEqual(cur.fetchone()[0], 1)         # 建票审计仍在(合法)
        finally:
            cur.execute("GRANT INSERT ON approval.ticket_audit TO mp_runtime")
            admin.close()

    def test_runtime_role_cannot_alter_schema(self):
        import psycopg2
        conn = psycopg2.connect(_DSN_RUNTIME)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("CREATE TABLE approval.not_allowed (id INT)")
        finally:
            conn.close()

    def test_runtime_role_can_full_dml(self):
        t, created = self.runtime.create(
            self._binding(finding_id="DML"), approval_expires_at=LATER)
        self.assertTrue(created)
        r = self.runtime.transition(t.ticket_id, "approve", now=NOW, actor=APPROVER)
        self.assertTrue(r.ok)

    def test_audit_appended_on_transition(self):
        t, _ = self.store.create(self._binding(finding_id="AU"),
                                 approval_expires_at=LATER)
        self.store.transition(t.ticket_id, "approve", now=NOW, actor=APPROVER)
        import psycopg2
        admin = _admin_conn()
        cur = admin.cursor()
        cur.execute("SELECT from_status, to_status, actor FROM approval.ticket_audit "
                    "WHERE ticket_id=%s ORDER BY id", (t.ticket_id,))
        rows = cur.fetchall()
        admin.close()
        self.assertIn(("PENDING", "APPROVED", APPROVER), rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
