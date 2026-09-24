# -*- coding: utf-8 -*-
"""D-B readiness: enforce.py 策略执行点 + dispatch policy 闸 确定性测试。

全部离线;无模型/GitHub/共享 PG。审批人身份 = 稳定 GitHub node ID。
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest

_REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_APPROVAL = os.path.join(_REPO, "tools", "approval")
_ISO = os.path.join(_REPO, "tools", "iso_chain")
sys.path.insert(0, str(_APPROVAL))
sys.path.insert(0, str(_ISO))

pkg = types.ModuleType("approval_pkg"); pkg.__path__ = [str(_APPROVAL)]
sys.modules["approval_pkg"] = pkg


def _load(name, path):
    import importlib.util
    full = "approval_pkg." + name
    if full in sys.modules: return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec); mod.__package__ = "approval_pkg"
    sys.modules[full] = mod; spec.loader.exec_module(mod); return mod


for n in ("approval", "store_sqlite", "gate_ticket", "policy", "enforce"):
    _load(n, os.path.join(_APPROVAL, n + ".py"))
policy_mod = sys.modules["approval_pkg.policy"]
enforce = sys.modules["approval_pkg.enforce"]
gt = sys.modules["approval_pkg.gate_ticket"]
Store = sys.modules["approval_pkg.store_sqlite"].SQLiteTicketStore

sys.path.insert(0, str(_ISO))
import dispatch as disp_mod  # noqa: E402

RUN = "iso-run-d-b"
HEAD = "42ed17879becbc02e31551938afbbf689351df96"
REPO = "nghqqa/fastapi-boilerplate-demo"
TASK = "gh-pr2-review-1"
NOW = "2026-09-24T12:00:00+00:00"
NODE_ID = "MDQ6VXNlcjM1OTg3NDg="
BAD_NODE = "UNKNOWN_NODE_ID"
MARKER = {"version": 1, "run_id": RUN, "task_id": TASK, "severity": "HIGH",
          "requested_by": "leader", "requested_at": NOW}


def pol(actions=frozenset({"generate_patch"}), approvers=(NODE_ID,), ttl=24):
    return policy_mod.ApprovalPolicy(allowed_actions=actions,
        approver_map={REPO: list(approvers)}, ttl_hours=ttl)


class EnforceTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.s = Store(os.path.join(self.d, "t.db"))
        t, _, _ = gt.open_gate_ticket(self.s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
        self.tid = t.ticket_id
    def tearDown(self):
        try: self.s.close()
        except Exception: pass
    def _approve(self, **kw):
        kw.setdefault("actor_id", NODE_ID)
        kw.setdefault("now", NOW)
        return enforce.authorize_approval(self.s, pol(), self.tid, **kw)

    def test_happy(self):
        r = self._approve()
        self.assertTrue(r["ok"]); self.assertEqual(self.s.get(self.tid).status, "APPROVED")
    def test_unconfigured_fail_closed(self):
        r = enforce.authorize_approval(self.s, policy_mod.UnconfiguredPolicy(), self.tid, actor_id=NODE_ID, now=NOW)
        self.assertFalse(r["ok"]); self.assertEqual(r["reason"], "POLICY_NOT_CONFIGURED")
    def test_action_not_enabled(self):
        p = pol(actions=frozenset({"run_poc"}))
        r = enforce.authorize_approval(self.s, p, self.tid, actor_id=NODE_ID, now=NOW)
        self.assertFalse(r["ok"]); self.assertIn("ACTION_NOT_ENABLED", r["reason"])
    def test_unauthorized_node_id(self):
        r = self._approve(actor_id=BAD_NODE)
        self.assertFalse(r["ok"]); self.assertEqual(r["reason"], "APPROVER_NOT_AUTHORIZED")
    def test_stale_head(self):
        r = self._approve(head_tip="f" * 40)
        self.assertFalse(r["ok"]); self.assertEqual(r["reason"], "STALE_HEAD")
    def test_no_actor_id_fail_closed(self):
        r = self._approve(actor_id="")
        self.assertFalse(r["ok"]); self.assertEqual(r["reason"], "ACTOR_ID_REQUIRED")
    def test_ttl_expiry(self):
        r = self._approve(now="2026-09-30T00:00:00+00:00")
        self.assertFalse(r["ok"]); self.assertEqual(r["reason"], "EXPIRED")
    def test_replay_noop(self):
        self._approve()
        r2 = self._approve()
        self.assertTrue(r2["ok"]); self.assertEqual(r2["reason"], "NOOP")
    def test_approved_by_is_node_id(self):
        self._approve()
        self.assertEqual(self.s.get(self.tid).approved_by, NODE_ID)


class DispatchGateTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.s = Store(os.path.join(self.d, "t.db"))
        self.outbox = disp_mod.DispatchOutbox(os.path.join(self.d, "o.db"))
        t, _, _ = gt.open_gate_ticket(self.s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
        self.tid = t.ticket_id
    def tearDown(self):
        try: self.s.close()
        except Exception: pass
    def test_closed_without_policy(self):
        calls = []
        try:
            disp_mod.dispatch_fixer(self.s, self.outbox, self.tid,
                lambda p: calls.append(p) or {}, {}, now=NOW, policy=None)
        except disp_mod.DispatchError:
            pass
        self.assertEqual(calls, [])
    def test_open_with_policy(self):
        enforce.authorize_approval(self.s, pol(), self.tid, actor_id=NODE_ID, now=NOW)
        ran = []
        out = disp_mod.dispatch_fixer(self.s, self.outbox, self.tid,
            lambda p: ran.append(p) or {"patch": "x"}, {"n": 1}, now=NOW, policy=pol())
        self.assertTrue(out["ok"]); self.assertEqual(len(ran), 1)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.s = Store(os.path.join(self.d, "t.db"))
        self.pol = pol()
        t, _, _ = gt.open_gate_ticket(self.s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
        self.tid = t.ticket_id
    def tearDown(self):
        try: self.s.close()
        except Exception: pass
    def test_reject_records_reason(self):
        enforce.authorize_reject(self.s, self.pol, self.tid, actor_id=NODE_ID, now=NOW, reason="no repro")
        t = self.s.get(self.tid)
        self.assertEqual(t.status, "REJECTED"); self.assertEqual(t.error, "no repro")
    def test_audit_rows(self):
        enforce.authorize_approval(self.s, self.pol, self.tid, actor_id=NODE_ID, now=NOW)
        rows = self.s._conn.execute("SELECT from_status,to_status,actor FROM ticket_audit ORDER BY id").fetchall()
        self.assertTrue(any(r[0]=="PENDING" and r[1]=="APPROVED" and NODE_ID in (r[2] or "") for r in rows))
    def test_gate_state_block_on_reject(self):
        r = enforce.authorize_reject(self.s, self.pol, self.tid, actor_id=NODE_ID, now=NOW, reason="x")
        self.assertTrue(r["ok"])
        self.assertEqual(gt.run_gate_state(self.s.get(self.tid).status)["gate_state"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
