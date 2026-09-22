"""M2 审批票据单元测试(全离线,规格 §7 验收映射)。

覆盖:动作集剥离 merge / 绑定五元组形状 / 批准/拒绝竞争 CAS /
重复创建幂等 / PR 更新失效 / 执行前逐字段校验(红线"批A执B") /
过期 / 单次有效 / 身份非空。
测试身份统一用显式标注的 "test-approver"(规格 §6,非真实审批人)。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import unittest
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2] / "tools" / "approval" / "approval.py"
_spec = importlib.util.spec_from_file_location("approval_core_m2", _CORE)
_core = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _core  # dataclass 前向引用需要按名找到宿主模块
_spec.loader.exec_module(_core)

APPROVED = _core.APPROVED
EXECUTING = _core.EXECUTING
EXPIRED = _core.EXPIRED
FAILED = _core.FAILED
INVALIDATED = _core.INVALIDATED
PENDING = _core.PENDING
REJECTED = _core.REJECTED
USED = _core.USED
ALLOWED_ACTIONS = _core.ALLOWED_ACTIONS
Binding = _core.Binding
ExecutionRequest = _core.ExecutionRequest
InMemoryTicketStore = _core.InMemoryTicketStore
canonical_hash = _core.canonical_hash
check_execution = _core.check_execution
create_ticket = _core.create_ticket
transition = _core.transition
validate_binding_shape = _core.validate_binding_shape

APPROVER = "test-approver"  # 隔离测试专用身份,非真实审批人
NOW = dt.datetime(2026, 9, 22, 12, 0, 0)
LATER = NOW + dt.timedelta(hours=1)
AFTER_TTL = NOW + dt.timedelta(hours=25)

HEAD = "a" * 40
HEAD2 = "b" * 40
PARAMS = {"method": "suggestion", "target": "finding-1"}
PARAMS_HASH = canonical_hash(PARAMS)
OTHER_HASH = canonical_hash({"x": 1})
PATCH_FP = canonical_hash("--- a/x.py\n+++ b/x.py\n")
FINDING_FP = canonical_hash({"rule": "S1", "loc": "x.py:1"})


def _binding(**over) -> Binding:
    kw = dict(run_id="run-20260922-01", repo="team/demo-repo", head_sha=HEAD,
              action="generate_patch", params_hash=PARAMS_HASH,
              patch_fingerprint=PATCH_FP, finding_id="F1")
    kw.update(over)
    return Binding(**kw)


def _req(ticket, **over) -> ExecutionRequest:
    b = ticket.binding
    kw = dict(ticket_id=ticket.ticket_id, run_id=b.run_id, repo=b.repo,
              head_sha=b.head_sha, params_hash=b.params_hash,
              patch_fingerprint=b.patch_fingerprint,
              finding_fingerprint=b.finding_fingerprint)
    kw.update(over)
    return ExecutionRequest(**kw)


def _approved_ticket(store: InMemoryTicketStore, **bind_over):
    t, _ = store.create(_binding(**bind_over), approval_expires_at=LATER)
    r = store.transition(t.ticket_id, "approve", now=NOW, actor=APPROVER)
    assert r.ok, r.reason
    return t


# ── 四问1:动作集(剥离 merge) ────────────────────────────────────────────
class ActionSetTests(unittest.TestCase):
    def test_action_set_excludes_merge(self):
        self.assertNotIn("merge", ALLOWED_ACTIONS)
        self.assertNotIn("close", ALLOWED_ACTIONS)
        self.assertNotIn("revert", ALLOWED_ACTIONS)

    def test_binding_rejects_merge_action(self):
        with self.assertRaises(ValueError):
            validate_binding_shape(_binding(action="merge"))


# ── 四问2:绑定形状 ───────────────────────────────────────────────────────
class BindingShapeTests(unittest.TestCase):
    def test_valid_binding_roundtrip(self):
        t = create_ticket(_binding(), approval_expires_at=LATER)
        self.assertEqual(t.binding.head_sha, HEAD)
        self.assertEqual(t.status, PENDING)

    def test_params_hash_required_64hex(self):
        with self.assertRaises(ValueError):
            validate_binding_shape(_binding(params_hash="deadbeef"))
        with self.assertRaises(ValueError):
            validate_binding_shape(_binding(params_hash=""))

    def test_head_sha_must_be_full_40hex(self):
        with self.assertRaises(ValueError):
            validate_binding_shape(_binding(head_sha=HEAD[:12]))

    def test_repo_must_be_owner_slash_name(self):
        with self.assertRaises(ValueError):
            validate_binding_shape(_binding(repo="just-a-name"))

    def test_patch_or_finding_fp_required(self):
        with self.assertRaises(ValueError):
            validate_binding_shape(_binding(patch_fingerprint=None))

    def test_canonical_hash_deterministic(self):
        self.assertEqual(canonical_hash({"a": 1, "b": 2}), canonical_hash({"b": 2, "a": 1}))


# ── 四问4:身份 / 竞争 / 幂等 ─────────────────────────────────────────────
class ApprovalRaceTests(unittest.TestCase):
    def test_approve_requires_identity(self):
        t = create_ticket(_binding())
        r = transition(t, "approve", now=NOW, actor="")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "IDENT_REQUIRED")
        self.assertEqual(t.status, PENDING)  # 未放行
        r = transition(t, "approve", now=NOW, actor="   ")
        self.assertFalse(r.ok)

    def test_approve_then_reject_race_first_wins(self):
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        r = store.transition(t.ticket_id, "reject", now=NOW, actor=APPROVER)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "INVALID_TRANSITION:APPROVED")
        self.assertEqual(store.get(t.ticket_id).status, APPROVED)  # 不覆盖

    def test_reject_then_approve_race_first_wins(self):
        store = InMemoryTicketStore()
        t, _ = store.create(_binding(), approval_expires_at=LATER)
        r = store.transition(t.ticket_id, "reject", now=NOW, actor=APPROVER)
        self.assertTrue(r.ok)
        r = store.transition(t.ticket_id, "approve", now=NOW, actor=APPROVER)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "INVALID_TRANSITION:REJECTED")

    def test_duplicate_approve_is_noop(self):
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        r = store.transition(t.ticket_id, "approve", now=LATER, actor=APPROVER)
        self.assertTrue(r.ok)
        self.assertEqual(r.reason, "NOOP")

    def test_duplicate_reject_is_noop(self):
        store = InMemoryTicketStore()
        t, _ = store.create(_binding(), approval_expires_at=LATER)
        store.transition(t.ticket_id, "reject", now=NOW, actor=APPROVER)
        r = store.transition(t.ticket_id, "reject", now=NOW, actor=APPROVER)
        self.assertTrue(r.ok)
        self.assertEqual(r.reason, "NOOP")

    def test_duplicate_create_returns_existing_ticket(self):
        store = InMemoryTicketStore()
        t1, created1 = store.create(_binding(), approval_expires_at=LATER)
        t2, created2 = store.create(_binding(), approval_expires_at=LATER)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(t1.ticket_id, t2.ticket_id)
        self.assertIs(store.active_for(_binding()), t1)

    def test_new_attempt_allowed_after_terminal(self):
        store = InMemoryTicketStore()
        t1, _ = store.create(_binding(), approval_expires_at=LATER)
        store.transition(t1.ticket_id, "reject", now=NOW, actor=APPROVER)
        t2, created = store.create(_binding(), attempt_no=2, approval_expires_at=LATER)
        self.assertTrue(created)
        self.assertNotEqual(t1.ticket_id, t2.ticket_id)
        self.assertEqual(t2.attempt_no, 2)


# ── 四问3:PR 更新失效 ────────────────────────────────────────────────────
class InvalidationTests(unittest.TestCase):
    def test_new_head_invalidates_active_ticket(self):
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        r = store.transition(t.ticket_id, "invalidate_for_new_head", now=NOW, actor=HEAD2)
        self.assertTrue(r.ok)
        self.assertEqual(store.get(t.ticket_id).status, INVALIDATED)

    def test_same_head_is_not_invalidation(self):
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        r = store.transition(t.ticket_id, "invalidate_for_new_head", now=NOW, actor=HEAD)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "NOT_A_NEW_HEAD")
        self.assertEqual(t.status, APPROVED)

    def test_invalidated_cannot_execute(self):
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        store.transition(t.ticket_id, "invalidate_for_new_head", now=NOW, actor=HEAD2)
        r = check_execution(store.get(t.ticket_id), _req(t), now=NOW)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "NOT_EXECUTABLE:INVALIDATED")

    def test_invalidation_is_correctness_independent(self):
        """即使不显式标 INVALIDATED,新 run/head 的执行请求也因绑定不匹配被拒。"""
        store = InMemoryTicketStore()
        t = _approved_ticket(store)  # 不调用 invalidate
        r = check_execution(t, _req(t, run_id="run-new", head_sha=HEAD2), now=NOW)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "BINDING_MISMATCH:run_id")


# ── 红线:执行前逐字段校验 ────────────────────────────────────────────────
class ExecutionCheckTests(unittest.TestCase):
    def _approved(self):
        store = InMemoryTicketStore()
        return _approved_ticket(store)

    def test_happy_path_ok(self):
        t = self._approved()
        r = check_execution(t, _req(t), now=NOW)
        self.assertTrue(r.ok, r.reason)

    def test_execution_binding_mismatch_rejected_per_field(self):
        t = self._approved()
        cases = [
            ("run_id", dict(run_id="run-other")),
            ("repo", dict(repo="team/other-repo")),
            ("head_sha", dict(head_sha=HEAD2)),
            ("params_hash", dict(params_hash=OTHER_HASH)),
            ("patch_fingerprint", dict(patch_fingerprint=OTHER_HASH)),
        ]
        for fname, over in cases:
            with self.subTest(field=fname):
                r = check_execution(t, _req(t, **over), now=NOW)
                self.assertFalse(r.ok)
                self.assertEqual(r.reason, "BINDING_MISMATCH:%s" % fname)

    def test_ticket_mismatch(self):
        t = self._approved()
        r = check_execution(t, _req(t, ticket_id="tkt-other"), now=NOW)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "TICKET_MISMATCH")

    def test_pending_cannot_execute(self):
        t = create_ticket(_binding(), approval_expires_at=LATER)
        r = check_execution(t, _req(t), now=NOW)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "NOT_EXECUTABLE:PENDING")

    def test_used_is_single_use(self):
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        store.transition(t.ticket_id, "start_exec", now=NOW)
        r = check_execution(store.get(t.ticket_id), _req(t), now=NOW)
        self.assertTrue(r.ok)  # EXECUTING 中可续验(恢复/重试场景)
        store.transition(t.ticket_id, "complete", now=LATER, result_fingerprint=PATCH_FP)
        r = check_execution(store.get(t.ticket_id), _req(t), now=LATER)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "NOT_EXECUTABLE:USED")

    def test_rejected_cannot_execute(self):
        store = InMemoryTicketStore()
        t, _ = store.create(_binding(), approval_expires_at=LATER)
        store.transition(t.ticket_id, "reject", now=NOW, actor=APPROVER)
        r = check_execution(store.get(t.ticket_id), _req(t), now=NOW)
        self.assertFalse(r.ok)


# ── 生命周期:过期 / 完成 / 失败 ──────────────────────────────────────────
class LifecycleTests(unittest.TestCase):
    def test_expired_cannot_execute(self):
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        r = check_execution(t, _req(t), now=AFTER_TTL)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "EXPIRED")

    def test_expired_blocks_other_transitions_first(self):
        store = InMemoryTicketStore()
        t, _ = store.create(_binding(), approval_expires_at=LATER)
        r = store.transition(t.ticket_id, "approve", now=AFTER_TTL, actor=APPROVER)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "EXPIRED")
        self.assertEqual(store.get(t.ticket_id).status, EXPIRED)

    def test_expire_event_on_pending_and_approved(self):
        store = InMemoryTicketStore()
        t1, _ = store.create(_binding(), approval_expires_at=LATER)
        self.assertTrue(store.transition(t1.ticket_id, "expire", now=NOW).ok)
        self.assertEqual(store.get(t1.ticket_id).status, EXPIRED)
        t2 = _approved_ticket(store)
        self.assertTrue(store.transition(t2.ticket_id, "expire", now=NOW).ok)
        self.assertEqual(store.get(t2.ticket_id).status, EXPIRED)

    def test_complete_requires_result_fingerprint(self):
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        store.transition(t.ticket_id, "start_exec", now=NOW)
        r = store.transition(t.ticket_id, "complete", now=LATER)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "RESULT_FP_REQUIRED")

    def test_executing_completes_after_approval_deadline(self):
        """审批期限只拦授权(approve/start_exec);已开始的执行允许收尾。"""
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        store.transition(t.ticket_id, "start_exec", now=NOW)
        # 期限已过后:不能再开始新的执行
        other, _ = store.create(_binding(finding_id="F2"), approval_expires_at=LATER)
        r = store.transition(other.ticket_id, "approve", now=AFTER_TTL, actor=APPROVER)
        self.assertFalse(r.ok)
        # 但在途执行可以完成
        r = store.transition(t.ticket_id, "complete", now=AFTER_TTL,
                             result_fingerprint=PATCH_FP)
        self.assertTrue(r.ok)
        self.assertEqual(store.get(t.ticket_id).status, USED)

    def test_full_lifecycle_to_used(self):
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        self.assertTrue(store.transition(t.ticket_id, "start_exec", now=NOW).ok)
        self.assertEqual(store.get(t.ticket_id).status, EXECUTING)
        r = store.transition(t.ticket_id, "complete", now=LATER, result_fingerprint=PATCH_FP)
        self.assertTrue(r.ok)
        done = store.get(t.ticket_id)
        self.assertEqual(done.status, USED)
        self.assertEqual(done.result_fingerprint, PATCH_FP)
        self.assertEqual(done.approved_by, APPROVER)

    def test_fail_allows_new_attempt(self):
        store = InMemoryTicketStore()
        t = _approved_ticket(store)
        store.transition(t.ticket_id, "start_exec", now=NOW)
        self.assertTrue(store.transition(t.ticket_id, "fail", now=NOW, error="boom").ok)
        self.assertEqual(store.get(t.ticket_id).status, FAILED)
        t2, created = store.create(_binding(), attempt_no=2, approval_expires_at=LATER)
        self.assertTrue(created)

    def test_unknown_event(self):
        t = create_ticket(_binding())
        r = transition(t, "self_destruct")
        self.assertFalse(r.ok)
        self.assertTrue(r.reason.startswith("UNKNOWN_EVENT"))

    def test_frozen_binding_immutability(self):
        """红线之一:绑定创建后不可改(批 A 后改绑 B 不可表达)。"""
        b = _binding()
        with self.assertRaises(Exception):
            b.head_sha = HEAD2  # type: ignore[misc]  # dataclass frozen


if __name__ == "__main__":
    unittest.main()
