# -*- coding: utf-8 -*-
"""D-B 启用前收口测试(enforce.py 策略执行点 + dispatch policy 闸)。

覆盖: 未配置 fail-closed、动作子集、越权审批人、head 新鲜度、
TTL 过期、重复决策幂等、派发闸、票据生命周期 E2E。
全部离线(SQLite 临时库 + 显式注入配置);无真实模型/GitHub/共享 PG。
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.normpath(os.path.join(_HERE, "..", ".."))
_APPROVAL = os.path.join(_REPO, "tools", "approval")
_ISO = os.path.join(_REPO, "tools", "iso_chain")

sys.path.insert(0, str(_APPROVAL))
sys.path.insert(0, str(_ISO))

import types  # noqa: E402
_pkg = types.ModuleType("approval_pkg")
_pkg.__path__ = [str(_APPROVAL)]
sys.modules["approval_pkg"] = _pkg


def _load(name, path, pkg="approval_pkg"):
    import importlib.util
    full = pkg + "." + name
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


policy_mod = _load("policy", os.path.join(_APPROVAL, "policy.py"))
enforce = _load("enforce", os.path.join(_APPROVAL, "enforce.py"))
gt = _load("gate_ticket", os.path.join(_APPROVAL, "gate_ticket.py"))
_smod = _load("store_sqlite", os.path.join(_APPROVAL, "store_sqlite.py"))
SQLiteTicketStore = _smod.SQLiteTicketStore
dispatch = _load("dispatch_iso", os.path.join(_ISO, "dispatch.py"))

RUN = "iso-run-d-b"
HEAD = "42ed17879becbc02e31551938afbbf689351df96"
REPO = "nghqqa/fastapi-boilerplate-demo"
TASK = "gh-pr2-42ed1787-review-1"
NOW = "2026-09-24T12:00:00+00:00"
MARKER = {"version": 1, "run_id": RUN, "task_id": TASK, "severity": "HIGH",
          "requested_by": "leader", "requested_at": NOW}


def configured_policy(actions=frozenset({"generate_patch"}),
                      approvers=("alice",), ttl=24):
    return policy_mod.ApprovalPolicy(
        allowed_actions=frozenset(actions),
        approver_map={REPO: list(approvers)}, ttl_hours=ttl)


class EnforceApprovalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="enforce-")
        self.store = SQLiteTicketStore(os.path.join(self._tmp, "t.db"))
        t, _, _ = gt.open_gate_ticket(
            self.store, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
        self.tid = t.ticket_id

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_unconfigured_policy_refused(self):
        r = enforce.authorize_approval(
            self.store, policy_mod.UnconfiguredPolicy(), self.tid,
            "alice", now=NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "POLICY_NOT_CONFIGURED")
        self.assertEqual(self.store.get(self.tid).status, "PENDING")

    def test_action_not_enabled_refused(self):
        r = enforce.authorize_approval(
            self.store,
            configured_policy(actions=frozenset({"run_poc"})),
            self.tid, "alice", now=NOW)
        self.assertFalse(r["ok"])
        self.assertIn("ACTION_NOT_ENABLED", r["reason"])
        self.assertEqual(self.store.get(self.tid).status, "PENDING")

    def test_unauthorized_approver_refused(self):
        r = enforce.authorize_approval(
            self.store, configured_policy(), self.tid, "mallory", now=NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "APPROVER_NOT_AUTHORIZED")
        self.assertEqual(self.store.get(self.tid).status, "PENDING")

    def test_stale_head_refused(self):
        r = enforce.authorize_approval(
            self.store, configured_policy(), self.tid, "alice",
            head_tip="f" * 40, now=NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "STALE_HEAD")
        self.assertEqual(self.store.get(self.tid).status, "PENDING")

    def test_valid_approve_cas(self):
        r = enforce.authorize_approval(
            self.store, configured_policy(), self.tid, "alice",
            head_tip=HEAD, now=NOW, reason="operator decision")
        self.assertTrue(r["ok"])
        self.assertEqual(self.store.get(self.tid).status, "APPROVED")
        self.assertEqual(self.store.get(self.tid).approved_by, "alice")

    def test_replay_is_noop_not_overwrite(self):
        enforce.authorize_approval(self.store, configured_policy(),
                                   self.tid, "alice", now=NOW)
        r2 = enforce.authorize_approval(self.store, configured_policy(),
                                        self.tid, "alice", now=NOW + "1")
        self.assertTrue(r2["ok"])                      # 幂等重放 NOOP
        self.assertEqual(r2["reason"], "NOOP")
        self.assertEqual(self.store.get(self.tid).approved_by, "alice")

    def test_unauthorized_replay_refused_at_policy(self):
        """未授权人重放: 策略层拒绝(先于 CAS),不影响已批准状态。"""
        enforce.authorize_approval(self.store, configured_policy(),
                                   self.tid, "alice", now=NOW)
        r = enforce.authorize_approval(self.store, configured_policy(),
                                       self.tid, "mallory", now=NOW + "1")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "APPROVER_NOT_AUTHORIZED")
        self.assertEqual(self.store.get(self.tid).approved_by, "alice")

    def test_ttl_expiry_refused(self):
        r = enforce.authorize_approval(
            self.store, configured_policy(ttl=24), self.tid, "alice",
            now="2026-09-30T00:00:00+00:00")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "EXPIRED")
        self.assertEqual(self.store.get(self.tid).status, "EXPIRED")

    def test_reject_requires_authorization_and_records_reason(self):
        r = enforce.authorize_reject(self.store, configured_policy(),
                                     self.tid, "alice", now=NOW,
                                     reason="not reproducible")
        self.assertTrue(r["ok"])
        self.assertEqual(self.store.get(self.tid).status, "REJECTED")
        self.assertEqual(self.store.get(self.tid).error, "not reproducible")

    def test_reject_unauthorized_refused(self):
        r = enforce.authorize_reject(self.store, configured_policy(),
                                     self.tid, "mallory", now=NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(self.store.get(self.tid).status, "PENDING")


class DispatchPolicyGateTests(unittest.TestCase):
    """feature flag 关闭(未配置)→ 真实执行路径无法创建。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="dsp-enf-")
        self.store = SQLiteTicketStore(os.path.join(self._tmp, "t.db"))
        self.outbox = dispatch.DispatchOutbox(os.path.join(self._tmp, "o.db"))
        t, _, _ = gt.open_gate_ticket(
            self.store, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
        self.tid = t.ticket_id

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_closed_without_policy(self):
        calls = []
        with self.assertRaises(dispatch.DispatchError) as cm:
            dispatch.dispatch_fixer(
                self.store, self.outbox, self.tid,
                lambda p: calls.append(p) or {}, {}, now=NOW, policy=None)
        self.assertIn("DISPATCH_CLOSED", str(cm.exception))
        self.assertEqual(calls, [])          # 执行器从未被调用

    def test_open_with_configured_policy_executes(self):
        enforce = _load("enforce_dsp2", os.path.join(_APPROVAL, "enforce.py"))
        pol = policy_mod.ApprovalPolicy(
            allowed_actions=frozenset({"generate_patch"}),
            approver_map={REPO: ["alice"]}, ttl_hours=24)
        enforce.authorize_approval(self.store, pol, self.tid, "alice",
                                   now=NOW)
        executed = []
        out = dispatch.dispatch_fixer(
            self.store, self.outbox, self.tid,
            lambda p: executed.append(p) or {"patch": "x"}, {"n": 1},
            now=NOW, policy=pol)
        self.assertTrue(out["ok"])
        self.assertEqual(len(executed), 1)

    def test_policy_action_not_enabled_blocks_dispatch(self):
        pol = policy_mod.ApprovalPolicy(
            allowed_actions=frozenset({"run_poc"}),      # generate_patch 未启用
            approver_map={REPO: ["alice"]}, ttl_hours=24)
        self.store.transition(self.tid, "approve", actor="alice", now=NOW)
        with self.assertRaises(dispatch.DispatchError) as cm:
            dispatch.dispatch_fixer(
                self.store, self.outbox, self.tid,
                lambda p: executed.append(p) or {}, {"n": 1}, now=NOW,
                policy=pol)
        self.assertIn("ACTION_NOT_ENABLED", str(cm.exception))


class LifecycleE2ETests(unittest.TestCase):
    """create → approve → executing → completed 全生命周期(离线)。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="e2e-")
        self.store = SQLiteTicketStore(os.path.join(self._tmp, "t.db"))
        t, _, _ = gt.open_gate_ticket(
            self.store, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
        self.tid = t.ticket_id

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_full_lifecycle(self):
        pol = policy_mod.ApprovalPolicy(
            allowed_actions=frozenset({"generate_patch"}),
            approver_map={REPO: ["alice"]}, ttl_hours=24)
        r1 = enforce.authorize_approval(self.store, pol, self.tid, "alice",
                                        head_tip=HEAD, now=NOW)
        self.assertTrue(r1["ok"])
        r2 = self.store.transition(self.tid, "start_exec", now=NOW + "1")
        self.assertTrue(r2.ok)
        fp = "c" * 64
        r3 = self.store.transition(self.tid, "complete",
                                   result_fingerprint=fp, now=NOW + "2")
        self.assertTrue(r3.ok)
        t = self.store.get(self.tid)
        self.assertEqual(t.status, "USED")
        self.assertEqual(t.result_fingerprint, fp)
        # 审计含全部尝试行
        rows = self.store._conn.execute(
            "SELECT from_status, to_status, actor FROM ticket_audit ORDER BY id"
        ).fetchall()
        self.assertIn(("PENDING", "APPROVED", "alice"), rows)
        # start_exec/complete 由编排系统自主发起: actor=None 是诚实记录
        self.assertIn(("APPROVED", "EXECUTING", None), rows)
        self.assertIn(("EXECUTING", "USED", None), rows)

    def test_negative_lifecycle_rejected(self):
        r = enforce.authorize_reject(self.store, configured_policy(),
                                     self.tid, "alice", now=NOW,
                                     reason="operator rejected")
        self.assertTrue(r["ok"])
        self.assertEqual(gt.run_gate_state(
            self.store.get(self.tid).status)["gate_state"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
