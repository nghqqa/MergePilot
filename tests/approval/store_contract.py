"""TicketStore 存储契约测试(可复用):任何实现跑同一套验收。

契约(与 store.py Protocol 一致):
  幂等创建(活动票唯一) / CAS 转移先到先得 / 崩溃恢复 / 红线校验往返。
使用方式:实现方提供 make_store()(每次返回独立连接的 store 实例)。
"""
from __future__ import annotations

import unittest


HEAD = "a" * 40
PARAMS_HASH = None  # 由具体测试模块注入(canonical_hash 依赖)
PATCH_FP = None


def make_binding(core, **over):
    kw = dict(run_id="run-contract", repo="team/demo", head_sha=HEAD,
              action="generate_patch", params_hash=PARAMS_HASH,
              patch_fingerprint=PATCH_FP, finding_id="F1")
    kw.update(over)
    return core.Binding(**kw)


class StoreContractMixin(unittest.TestCase):
    """存储契约测试集。要求宿主类提供:
    - self.make_store() -> 实现 TicketStore 的独立实例(独立连接)
    - self.core         -> approval 纯逻辑模块
    - self.Approver/Now/Later 测试常量
    """

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    def tearDown(self):
        self.store.close()

    # ── 契约 1:创建与读取往返 ────────────────────────────────────────────
    def test_contract_create_and_get_roundtrip(self):
        core = self.core
        b = make_binding(core)
        t, created = self.store.create(b, approval_expires_at=self.Later)
        self.assertTrue(created)
        got = self.store.get(t.ticket_id)
        self.assertEqual(got.binding, t.binding)
        self.assertEqual(got.status, core.PENDING)

    # ── 契约 2:活动票唯一(同连接幂等) ───────────────────────────────────
    def test_contract_duplicate_create_idempotent(self):
        core = self.core
        t1, c1 = self.store.create(make_binding(core))
        t2, c2 = self.store.create(make_binding(core))
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(t1.ticket_id, t2.ticket_id)

    # ── 契约 3:CAS 转移 ─────────────────────────────────────────────────
    def test_contract_approve_transition(self):
        core = self.core
        t, _ = self.store.create(make_binding(core), approval_expires_at=self.Later)
        r = self.store.transition(t.ticket_id, "approve", now=self.Now, actor=self.Approver)
        self.assertTrue(r.ok)
        got = self.store.get(t.ticket_id)
        self.assertEqual(got.status, core.APPROVED)
        self.assertEqual(got.approved_by, self.Approver)

    # ── 契约 4:跨连接竞争(approve/reject 先到先得) ──────────────────────
    def test_contract_cross_connection_race(self):
        core = self.core
        t, _ = self.store.create(make_binding(core), approval_expires_at=self.Later)
        store2 = self.make_store()
        try:
            r1 = self.store.transition(t.ticket_id, "approve", now=self.Now, actor=self.Approver)
            r2 = store2.transition(t.ticket_id, "reject", now=self.Now, actor=self.Approver)
            self.assertTrue(r1.ok)
            self.assertFalse(r2.ok)
            self.assertEqual(r2.reason, "INVALID_TRANSITION:APPROVED")
            self.assertEqual(store2.get(t.ticket_id).status, core.APPROVED)
        finally:
            store2.close()

    # ── 契约 5:跨连接并发幂等创建(收敛同一张票) ─────────────────────────
    def test_contract_duplicate_create_racing(self):
        import threading
        core = self.core
        b = make_binding(core)
        results = []
        store2 = self.make_store()

        def worker(s):
            try:
                t, created = s.create(b)
                results.append((t.ticket_id, created))
            except Exception as e:
                results.append(("EXC:" + type(e).__name__, str(e)[:60]))

        threads = [threading.Thread(target=worker, args=(self.store,)),
                   threading.Thread(target=worker, args=(store2,))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        store2.close()
        ids = {r[0] for r in results}
        self.assertEqual(len(ids), 1, "并发创建必须收敛同一张票: %s" % results)
        self.assertEqual(sum(1 for r in results if r[1] is True), 1)

    # ── 契约 6:崩溃恢复(重开保留状态) ───────────────────────────────────
    def test_contract_crash_recovery_reopen(self):
        core = self.core
        t, _ = self.store.create(make_binding(core), approval_expires_at=self.Later)
        self.store.transition(t.ticket_id, "approve", now=self.Now, actor=self.Approver)
        self.store.close()   # 模拟进程退出(非优雅关闭)
        self.store = self.make_store()
        got = self.store.get(t.ticket_id)
        self.assertEqual(got.status, core.APPROVED)

    # ── 契约 7:红线校验往返 ─────────────────────────────────────────────
    def test_contract_execution_check_roundtrip(self):
        core = self.core
        t, _ = self.store.create(make_binding(core), approval_expires_at=self.Later)
        self.store.transition(t.ticket_id, "approve", now=self.Now, actor=self.Approver)
        got = self.store.get(t.ticket_id)
        ok = core.check_execution(
            got, core.ExecutionRequest(
                ticket_id=got.ticket_id, run_id=got.binding.run_id,
                repo=got.binding.repo, head_sha=got.binding.head_sha,
                params_hash=got.binding.params_hash,
                patch_fingerprint=got.binding.patch_fingerprint), now=self.Now)
        self.assertTrue(ok.ok)
        bad = core.check_execution(
            got, core.ExecutionRequest(
                ticket_id=got.ticket_id, run_id="run-other",
                repo=got.binding.repo, head_sha=got.binding.head_sha,
                params_hash=got.binding.params_hash), now=self.Now)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.reason, "BINDING_MISMATCH:run_id")
