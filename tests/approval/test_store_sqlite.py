"""SqliteTicketStore 测试(第 1-2 层):真实跨连接并发 CAS + 崩溃恢复重开。

不把进程内锁当跨进程 CAS:每个 store 实例是独立 sqlite3 连接,
竞争通过 BEGIN IMMEDIATE 排队 + 前置状态守卫 UPDATE(rowcount)仲裁。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

# 构造包上下文,使 store_sqlite 的相对导入(from .approval)可用
_APPROVAL_DIR = Path(__file__).resolve().parents[2] / "tools" / "approval"
_pkg = types.ModuleType("approval_pkg")
_pkg.__path__ = [str(_APPROVAL_DIR)]
sys.modules["approval_pkg"] = _pkg


def _load(name, path):
    spec = importlib.util.spec_from_file_location("approval_pkg." + name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["approval_pkg." + name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("approval", _APPROVAL_DIR / "approval.py")
_smod = _load("store_sqlite", _APPROVAL_DIR / "store_sqlite.py")

SqliteTicketStore = _smod.SqliteTicketStore

NOW = "2026-09-22T12:00:00+00:00"
LATER = "2026-09-22T13:00:00+00:00"
APPROVER = "test-approver"
HEAD = "a" * 40
PARAMS_HASH = core.canonical_hash({"method": "suggestion"})
PATCH_FP = core.canonical_hash("patch-bytes")


def _binding(**over):
    kw = dict(run_id="run-sql-1", repo="team/demo", head_sha=HEAD,
              action="generate_patch", params_hash=PARAMS_HASH,
              patch_fingerprint=PATCH_FP, finding_id="F1")
    kw.update(over)
    return core.Binding(**kw)


class SqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tickets.db")
        self.store = SqliteTicketStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_create_and_get_roundtrip(self):
        t, created = self.store.create(_binding(), approval_expires_at=LATER)
        self.assertTrue(created)
        got = self.store.get(t.ticket_id)
        self.assertEqual(got.binding, t.binding)
        self.assertEqual(got.status, "PENDING")
        self.assertEqual(got.approval_expires_at, LATER)

    def test_duplicate_create_same_connection_idempotent(self):
        t1, c1 = self.store.create(_binding())
        t2, c2 = self.store.create(_binding())
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(t1.ticket_id, t2.ticket_id)

    def test_approve_then_transition_via_store(self):
        t, _ = self.store.create(_binding(), approval_expires_at=LATER)
        r = self.store.transition(t.ticket_id, "approve", now=NOW, actor=APPROVER)
        self.assertTrue(r.ok)
        got = self.store.get(t.ticket_id)
        self.assertEqual(got.status, "APPROVED")
        self.assertEqual(got.approved_by, APPROVER)

    def test_cross_connection_race_approve_vs_reject(self):
        """跨连接竞争:approve 与 reject 先到先得,失败方 INVALID_TRANSITION。"""
        t, _ = self.store.create(_binding(), approval_expires_at=LATER)
        store2 = SqliteTicketStore(self.path)
        try:
            r1 = self.store.transition(t.ticket_id, "approve", now=NOW, actor=APPROVER)
            r2 = store2.transition(t.ticket_id, "reject", now=NOW, actor=APPROVER)
            self.assertTrue(r1.ok)
            self.assertFalse(r2.ok)
            self.assertEqual(r2.reason, "INVALID_TRANSITION:APPROVED")
            self.assertEqual(store2.get(t.ticket_id).status, "APPROVED")
        finally:
            store2.close()

    def test_cross_connection_duplicate_create_racing(self):
        """跨连接并发幂等创建:partial UNIQUE INDEX 兜底,必得同一张票。"""
        results = []

        def worker():
            try:
                t, created = self.store.create(_binding())
                results.append((t.ticket_id, created))
            except Exception as e:  # UNIQUE 冲突被串行化窗口避免;若发生记录之
                results.append(("EXC:" + type(e).__name__, str(e)[:60]))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        # 两个独立连接(不是共享连接的线程模型)
        store2 = SqliteTicketStore(self.path)

        def worker2():
            try:
                t, created = store2.create(_binding())
                results.append((t.ticket_id, created))
            except Exception as e:
                results.append(("EXC:" + type(e).__name__, str(e)[:60]))

        threads.append(threading.Thread(target=worker2))
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        store2.close()
        ids = {r[0] for r in results}
        self.assertEqual(len(ids), 1, "并发创建必须收敛到同一张票: %s" % results)
        self.assertEqual(sum(1 for r in results if r[1] is True), 1)

    def test_crash_recovery_reopen_preserves_state(self):
        """崩溃恢复:进程消失(连接未优雅关闭)后重开,状态保留可续转移。"""
        t, _ = self.store.create(_binding(), approval_expires_at=LATER)
        self.store.transition(t.ticket_id, "approve", now=NOW, actor=APPROVER)
        # 模拟崩溃:放弃旧连接(不 close),直接重开
        reopened = SqliteTicketStore(self.path)
        try:
            got = reopened.get(t.ticket_id)
            self.assertEqual(got.status, "APPROVED")
            r = reopened.transition(t.ticket_id, "start_exec", now=NOW)
            self.assertTrue(r.ok)
            self.assertEqual(reopened.get(t.ticket_id).status, "EXECUTING")
        finally:
            reopened.close()

    def test_execution_check_after_store_roundtrip(self):
        """红线校验在存储往返后仍成立(批A执B 拒绝)。"""
        t, _ = self.store.create(_binding(), approval_expires_at=LATER)
        self.store.transition(t.ticket_id, "approve", now=NOW, actor=APPROVER)
        got = self.store.get(t.ticket_id)
        ok = core.check_execution(
            got, core.ExecutionRequest(
                ticket_id=got.ticket_id, run_id=got.binding.run_id,
                repo=got.binding.repo, head_sha=got.binding.head_sha,
                params_hash=got.binding.params_hash,
                patch_fingerprint=got.binding.patch_fingerprint),
            now=NOW)
        self.assertTrue(ok.ok)
        bad = core.check_execution(
            got, core.ExecutionRequest(
                ticket_id=got.ticket_id, run_id="run-other",
                repo=got.binding.repo, head_sha=got.binding.head_sha,
                params_hash=got.binding.params_hash), now=NOW)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.reason, "BINDING_MISMATCH:run_id")


if __name__ == "__main__":
    unittest.main(verbosity=2)
