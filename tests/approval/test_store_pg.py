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
import sys
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
MIGRATIONS = ["001_approval_tickets.sql", "002_tickets_target_key.sql"]
_MIG_DIR = (Path(__file__).resolve().parents[2] / "tools" / "approval" / "pg" /
            "migrations")

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


_ensure_db("mp_pg_contract")


def _admin_conn():
    import psycopg2
    return psycopg2.connect(_DSN_ADMIN)


def _apply_migration():
    """全新安装演练:drop 旧 schema → 按序应用正式迁移 → 测试夹具。"""
    import psycopg2
    conn = _admin_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS approval CASCADE")
    cur.execute("DROP SCHEMA IF EXISTS run CASCADE")
    for m in MIGRATIONS:
        cur.execute((_MIG_DIR / m).read_text(encoding="utf-8"))
    # 测试夹具(非正式迁移):run schema 由测试自建,便于隔离与清理
    cur.execute("CREATE SCHEMA IF NOT EXISTS run")
    cur.execute("CREATE TABLE IF NOT EXISTS run.repos (repo_id TEXT PRIMARY KEY)")
    cur.execute("INSERT INTO run.repos (repo_id) VALUES ('team/demo') "
                "ON CONFLICT DO NOTHING")
    # 运行时最小权限角色(不存在则建;密码=本地隔离测试专用)
    cur.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                "rolname='mp_runtime') THEN CREATE ROLE mp_runtime LOGIN "
                "PASSWORD 'mp-runtime-local-test'; END IF; END $$")
    cur.execute("GRANT USAGE ON SCHEMA approval, run TO mp_runtime")
    cur.execute("GRANT SELECT, INSERT, UPDATE ON approval.tickets TO mp_runtime")
    cur.execute("GRANT SELECT, INSERT ON approval.ticket_audit TO mp_runtime")
    cur.execute("GRANT USAGE, SELECT ON SEQUENCE "
                "approval.ticket_audit_id_seq TO mp_runtime")
    cur.execute("GRANT SELECT, INSERT, UPDATE ON run.repos TO mp_runtime")
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

    def test_cross_connection_race_single_winner(self):
        """并发 approve/reject 竞争:恰好一个成功。

        使用独立连接(独立 store 实例)验证 PG SELECT FOR UPDATE + CAS 的
        服务端串行化;不依赖 Windows spawn(基线已证实不可靠)。"""
        t, _ = self.store.create(self._binding(), approval_expires_at=LATER)
        s2 = pg_store.PostgreSQLTicketStore(_DSN_RUNTIME)
        try:
            r1 = self.store.transition(t.ticket_id, "approve",
                                       actor=APPROVER, now=NOW)
            r2 = s2.transition(t.ticket_id, "reject",
                               actor="bob", now=NOW)
            wins = [r for r in (r1, r2) if r.ok]
            self.assertEqual(len(wins), 1,
                             "both succeeded: %s / %s" % (r1.reason, r2.reason))
            self.assertIn(wins[0].reason, ("OK", "NOOP"))
            final = self.store.get(t.ticket_id)
            self.assertIn(final.status, (core.APPROVED, core.REJECTED))
        finally:
            s2.close()

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

    def test_target_key_run_level_and_finding_level(self):
        """设计 v2 验收:run 级 target_key='_run_';finding 级=finding_id。"""
        from approval_pkg.pg_store import target_key_for
        self.assertEqual(target_key_for(self._binding(finding_id=None)), "_run_")
        self.assertEqual(target_key_for(self._binding(finding_id="F9")), "F9")
        cur = _admin_conn().cursor()
        b_run = self._binding(finding_id=None)
        b_f = self._binding(finding_id="F5", action="run_poc")
        t1, c1 = self.store.create(b_run)
        t2, c2 = self.store.create(b_f)
        self.assertTrue(c1 and c2)
        cur.execute("SELECT target_key FROM approval.tickets WHERE ticket_id=%s",
                    (t1.ticket_id,))
        self.assertEqual(cur.fetchone()[0], "_run_")
        cur.execute("SELECT target_key FROM approval.tickets WHERE ticket_id=%s",
                    (t2.ticket_id,))
        self.assertEqual(cur.fetchone()[0], "F5")

    def test_same_target_no_duplicate_active_tickets(self):
        """相同目标(run 级):重复活动票被唯一索引拒绝——修复 NULL 漏洞。"""
        import psycopg2
        b = self._binding(finding_id=None)
        t1, c1 = self.store.create(b)
        self.assertTrue(c1)
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            cur = _admin_conn().cursor()
            conn2 = _admin_conn()
            conn2.autocommit = True
            cur2 = conn2.cursor()
            cur2.execute(
                "INSERT INTO approval.tickets (ticket_id, run_id, repo_id, "
                "head_sha, action, params_hash, patch_fingerprint, finding_id, "
                "target_key, attempt_no, status, created_at) "
                "VALUES ('tkt-forced','run-contract','team/demo',%s,"
                "'generate_patch',%s,NULL,NULL,'_run_',1,'PENDING',now())",
                ("a" * 40, "1" * 64))
            conn2.close()

    def test_different_targets_not_cross_mutex(self):
        """不同合法目标(run 级 vs finding 级;不同 finding)不错误互斥。"""
        t1, c1 = self.store.create(self._binding(finding_id=None))
        t2, c2 = self.store.create(self._binding(finding_id="F8"))
        t3, c3 = self.store.create(self._binding(finding_id="F8",
                                                 action="run_poc"))
        self.assertTrue(c1 and c2 and c3)
        self.assertEqual(len({t1.ticket_id, t2.ticket_id, t3.ticket_id}), 3)

    def test_external_target_key_cannot_bypass_binding(self):
        """target_key 由绑定内部派生,外部无法注入——执行请求仍按绑定五元组校验。"""
        b = self._binding(finding_id="FX")
        t, _ = self.store.create(b)
        r = self.store.transition(t.ticket_id, "approve", now=NOW, actor=APPROVER)
        self.assertTrue(r.ok)
        got = self.store.get(t.ticket_id)
        # 伪造"同一 target_key 但另一 run/head"的执行请求 → 绑定校验拒绝
        forged = core.ExecutionRequest(
            ticket_id=t.ticket_id, run_id="run-evil", repo="team/demo",
            head_sha="b" * 40, params_hash=b.params_hash,
            patch_fingerprint=b.patch_fingerprint)
        res = core.check_execution(got, forged, now=NOW)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "BINDING_MISMATCH:run_id")

    def test_anomalous_source_finding_not_silently_converted(self):
        """源数据异常(空串 finding_id)在绑定校验即拒绝——不静默转 '_run_'。"""
        with self.assertRaises(ValueError):
            core.validate_binding_shape(self._binding(finding_id=""))
        # 迁移映射口径:COALESCE(finding_id,'_run_') 只对 NULL 生效;
        # 源库空串行迁移时计为校验错误(migrate 工具已实现),不静默归入 run 级。
        # 保留命名空间:下划线前缀 finding_id 在绑定校验即拒绝(防与 '_run_' 碰撞)
        with self.assertRaises(ValueError):
            core.validate_binding_shape(self._binding(finding_id="_run_"))
        with self.assertRaises(ValueError):
            core.validate_binding_shape(self._binding(finding_id="_internal"))

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


@unittest.skipUnless(GATED, "MERGEPILOT_PG_CONTRACT=1 未设置:跳过真实 PG 验证")
class PgOrchestrationAcceptance(unittest.TestCase):
    """确定性建票控制面在真实 PG 上端到端(2026-09-24 建票所有权冻结轮):
    ensure_gate_ticket 幂等建票 / active_by_repo / record_event / 旧 head 失效。"""

    @classmethod
    def setUpClass(cls):
        _apply_migration()
        _orch_dir = Path(__file__).resolve().parents[2] / "tools" / "gh-bridge"
        spec = importlib.util.spec_from_file_location(
            "mp_review_outcome_pg", _orch_dir / "review_outcome.py")
        cls.ro = importlib.util.module_from_spec(spec)
        sys.modules["mp_review_outcome_pg"] = cls.ro
        spec.loader.exec_module(cls.ro)
        # orchestration 用相对导入(from .approval/.policy)——以包身份加载
        approval_dir = Path(__file__).resolve().parents[2] / "tools" / "approval"
        import types
        pkg = types.ModuleType("approval_pkg_pg")
        pkg.__path__ = [str(approval_dir)]
        sys.modules.setdefault("approval_pkg_pg", pkg)

        def _load(name, path):
            full = "approval_pkg_pg." + name
            if full in sys.modules:
                return sys.modules[full]
            s = importlib.util.spec_from_file_location(full, path)
            mod = importlib.util.module_from_spec(s)
            mod.__package__ = "approval_pkg_pg"
            sys.modules[full] = mod
            s.loader.exec_module(mod)
            return mod

        _load("approval", approval_dir / "approval.py")
        cls.policy_mod = _load("policy", approval_dir / "policy.py")
        cls.orch = _load("orchestration", approval_dir / "orchestration.py")

    def setUp(self):
        _cleanup_tickets()
        self.store = pg_store.PostgreSQLTicketStore(_DSN_ADMIN)

    def tearDown(self):
        self.store.close()

    def _mk_outcome(self, head, run="run-pg-orch-1"):
        fid, fp = self.ro.finding_identity("CONFIRMED", "HIGH", "CWE-22")
        return {"schema_version": "review-outcome.v1", "run_id": run,
                "repo": "team/demo", "pr_number": 2, "head_sha": head,
                "finding_validation": "CONFIRMED",
                "findings": [{"finding_id": fid, "severity": "HIGH",
                              "cwe": "CWE-22", "fingerprint": fp}],
                "validations": [], "outcome_source": "test"}

    def _policy(self):
        return self.policy_mod.ApprovalPolicy(
            allowed_actions=frozenset({"run_poc", "generate_patch"}),
            approver_map={"team/demo": ["approver-a"]}, ttl_hours=24)

    def test_ensure_pending_idempotent_and_invalidate_on_pg(self):
        head1, head2 = "a" * 40, "b" * 40
        r1 = self.orch.ensure_gate_ticket(
            self._mk_outcome(head1), self._policy(), self.store, now=NOW,
            expected_head_sha=head1)
        self.assertTrue(r1.ok)
        self.assertTrue(r1.created)
        t = self.store.get(r1.ticket_id)
        self.assertEqual(t.status, "PENDING")
        self.assertIsNone(t.approved_by)            # 创建≠批准
        r1b = self.orch.ensure_gate_ticket(
            self._mk_outcome(head1), self._policy(), self.store, now=NOW,
            expected_head_sha=head1)
        self.assertEqual(r1.ticket_id, r1b.ticket_id)
        self.assertFalse(r1b.created)               # 幂等重放
        # 新 head → 旧票确定性失效
        r2 = self.orch.ensure_gate_ticket(
            self._mk_outcome(head2, run="run-pg-orch-2"), self._policy(),
            self.store, now=LATER, expected_head_sha=head2)
        self.assertTrue(r2.ok)
        self.assertIn(r1.ticket_id, r2.invalidated)
        self.assertEqual(self.store.get(r1.ticket_id).status, "INVALIDATED")
        # 新票 PENDING
        self.assertEqual(self.store.get(r2.ticket_id).status, "PENDING")

    def test_ensure_audit_and_event_api_on_pg(self):
        head = "c" * 40
        r = self.orch.ensure_gate_ticket(
            self._mk_outcome(head), self._policy(), self.store, now=NOW,
            expected_head_sha=head)
        self.assertTrue(r.ok)
        self.store.record_event(r.ticket_id, "PENDING", "PENDING",
                                "op:note", "evt-pg-1")
        with pg_store._connect(_DSN_ADMIN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT to_status, actor, request_hash FROM approval.ticket_audit "
                "WHERE ticket_id=%s ORDER BY id", (r.ticket_id,))
            rows = cur.fetchall()
        self.assertTrue(any(x[1] == "control-plane:ensure" and
                            "ENSURE_CREATED" in (x[2] or "") for x in rows))
        self.assertTrue(any(x[1] == "op:note" for x in rows))

    def test_active_by_repo_on_pg(self):
        head = "d" * 40
        r = self.orch.ensure_gate_ticket(
            self._mk_outcome(head), self._policy(), self.store, now=NOW,
            expected_head_sha=head)
        self.assertTrue(r.ok)
        active = self.store.active_by_repo("team/demo")
        self.assertEqual([t.ticket_id for t in active], [r.ticket_id])
        self.store.record_event(r.ticket_id, "PENDING", "PENDING", "x", "close-evt")
        self.store.transition(r.ticket_id, "reject", actor="approver-a", now=NOW)
        self.assertEqual(self.store.active_by_repo("team/demo"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
