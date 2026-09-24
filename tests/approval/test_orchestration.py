# -*- coding: utf-8 -*-
"""确定性建票控制面测试(建票所有权冻结轮,2026-09-24)。

覆盖整改规格 §八 场景 1-13(approval 侧;桥侧见 tests/gh_bridge/
test_ticket_orchestration.py 场景 9 桥映射/14 check-run 语义/15 旧数据)。
全部离线:无模型、无 GitHub、无共享 PG(场景 5 用同库双实例)。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_APPROVAL = _REPO / "tools" / "approval"

_pkg = types.ModuleType("approval_pkg")
_pkg.__path__ = [str(_APPROVAL)]
sys.modules.setdefault("approval_pkg", _pkg)


def _load(name, path):
    full = "approval_pkg." + name
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "approval_pkg"
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


for _n in ("approval", "store_sqlite", "policy", "orchestration"):
    _load(_n, _APPROVAL / (_n + ".py"))
core = sys.modules["approval_pkg.approval"]
orch = sys.modules["approval_pkg.orchestration"]
policy_mod = sys.modules["approval_pkg.policy"]
Store = sys.modules["approval_pkg.store_sqlite"].SQLiteTicketStore

RUN = "run-gh-pr2-r2-000000"
HEAD = "26ed8f1e4ca28692933df15ae6c2fd2cdf633a9b"
HEAD2 = "b" * 40
REPO = "nghqqa/fastapi-boilerplate-demo"
NOW = "2026-09-24T12:00:00+00:00"
NODE_ID = "MDQ6VXNlcjM1OTg3NDg="


def make_policy(actions=("run_poc", "generate_patch"), approvers=(NODE_ID,), ttl=24):
    return policy_mod.ApprovalPolicy(
        allowed_actions=frozenset(actions),
        approver_map={REPO: list(approvers)}, ttl_hours=ttl)


def make_outcome(**over):
    from importlib import import_module
    ro = _load_review_outcome()
    fv = over.pop("finding_validation", "CONFIRMED")
    severity = over.pop("severity", "HIGH")
    cwe = over.pop("cwe", "CWE-22")
    head = over.pop("head_sha", HEAD)
    run = over.pop("run_id", RUN)
    fid, fp = ro.finding_identity(fv, severity, cwe)
    out = {
        "schema_version": "review-outcome.v1", "run_id": run, "repo": REPO,
        "pr_number": 2, "head_sha": head,
        "finding_validation": fv,
        "findings": [{"finding_id": fid, "severity": severity, "cwe": cwe,
                      "fingerprint": fp}] if fv == "CONFIRMED" else [],
        "validations": over.pop("validations", []),
        "outcome_source": "test",
    }
    out.update(over)
    out["outcome_digest"] = core.canonical_hash(out)
    return out


_RO_CACHE = {}


def _load_review_outcome():
    if "ro" in _RO_CACHE:
        return _RO_CACHE["ro"]
    spec = importlib.util.spec_from_file_location(
        "mp_review_outcome_test", _REPO / "tools" / "gh-bridge" / "review_outcome.py")
    ro = importlib.util.module_from_spec(spec)
    sys.modules["mp_review_outcome_test"] = ro
    spec.loader.exec_module(ro)
    _RO_CACHE["ro"] = ro
    return ro


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_deterministic_and_algorithm_pinned(self):
        ro = _load_review_outcome()
        self.assertEqual(ro.canonical_hash({"a": 1, "b": 2}),
                         core.canonical_hash({"b": 2, "a": 1}))   # 同一算法
        f1 = ro.finding_identity("CONFIRMED", "HIGH", "CWE-22")
        f2 = ro.finding_identity("CONFIRMED", "HIGH", "CWE-22")
        self.assertEqual(f1, f2)
        self.assertNotEqual(f1, ro.finding_identity("CONFIRMED", "HIGH", None))
        self.assertNotEqual(f1, ro.finding_identity("INCONCLUSIVE", "HIGH", "CWE-22"))

    def test_policy_fingerprint_explicit_vs_content(self):
        p = make_policy()
        self.assertNotEqual(orch.policy_fingerprint(p), "")
        p2 = policy_mod.ApprovalPolicy(
            allowed_actions=frozenset({"run_poc", "generate_patch"}),
            approver_map={REPO: [NODE_ID]}, ttl_hours=24, policy_version="db-2026-09-24")
        self.assertEqual(orch.policy_fingerprint(p2), "db-2026-09-24")


class SelectActionTests(unittest.TestCase):
    def test_confirmed_high_without_validation_selects_run_poc(self):
        action, why = orch.select_action(make_outcome(), make_policy())
        self.assertEqual((action, why), ("run_poc", "OK"))

    def test_confirmed_with_current_run_validation_selects_generate_patch(self):
        out = make_outcome(validations=[{"kind": "poc", "run_id": RUN,
                                         "head_sha": HEAD, "evidence_refs": ["poc.log"]}])
        action, why = orch.select_action(out, make_policy())
        self.assertEqual((action, why), ("generate_patch", "OK"))

    def test_validation_from_other_run_or_head_does_not_count(self):
        for bad in ({"kind": "poc", "run_id": "old-run", "head_sha": HEAD,
                     "evidence_refs": ["x"]},
                    {"kind": "poc", "run_id": RUN, "head_sha": "c" * 40,
                     "evidence_refs": ["x"]}):
            out = make_outcome(validations=[bad])
            action, _ = orch.select_action(out, make_policy())
            self.assertEqual(action, "run_poc")

    def test_inconclusive_never_creates_generate_patch(self):
        action, why = orch.select_action(make_outcome(finding_validation="INCONCLUSIVE"),
                                         make_policy())
        self.assertIsNone(action)
        self.assertEqual(why, "NO_CONFIRMED_FINDING:INCONCLUSIVE")

    def test_medium_low_below_gate(self):
        action, why = orch.select_action(make_outcome(severity="MEDIUM"), make_policy())
        self.assertIsNone(action)
        self.assertTrue(why.startswith("SEVERITY_BELOW_GATE"))

    def test_unconfigured_policy_fails_closed(self):
        action, why = orch.select_action(make_outcome(), policy_mod.UnconfiguredPolicy())
        self.assertIsNone(action)
        self.assertEqual(why, "POLICY_NOT_CONFIGURED")

    def test_disabled_action_refused(self):
        action, why = orch.select_action(
            make_outcome(validations=[{"kind": "poc", "run_id": RUN, "head_sha": HEAD,
                                       "evidence_refs": ["x"]}]),
            make_policy(actions=("run_poc",)))
        self.assertIsNone(action)
        self.assertEqual(why, "ACTION_NOT_ENABLED:generate_patch")

    def test_no_approver_configured_refused(self):
        action, why = orch.select_action(make_outcome(), make_policy(approvers=()))
        self.assertIsNone(action)
        self.assertEqual(why, "NO_APPROVER_CONFIGURED:%s" % REPO)

    def test_forbidden_actions_frozen(self):
        self.assertEqual(orch.FORBIDDEN_ACTIONS,
                         frozenset({"push_branch", "merge", "close", "revert"}))
        self.assertTrue(orch.FORBIDDEN_ACTIONS_IS_FROZEN)
        self.assertFalse(orch.FORBIDDEN_ACTIONS & set(("run_poc", "generate_patch")))


class EnsureTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "t.db")
        self.store = Store(self.path)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _ensure(self, store=None, outcome=None, marker=None, policy=None, mode="auto"):
        return orch.ensure_gate_ticket(
            outcome or make_outcome(), policy or make_policy(),
            store or self.store, now=NOW, mode=mode, marker=marker)

    def test_1_high_confirmed_marker_absent_creates_pending(self):
        r = self._ensure(marker=None)
        self.assertTrue(r.ok)
        self.assertTrue(r.created)
        self.assertEqual(r.marker_status, "ABSENT")
        t = self.store.get(r.ticket_id)
        self.assertEqual(t.status, "PENDING")          # 创建≠批准
        self.assertIsNone(t.approved_by)
        self.assertEqual(t.binding.action, "run_poc")
        self.assertEqual(t.binding.head_sha, HEAD)

    def test_2_marker_consistent_same_ticket_compatible(self):
        marker = {"version": 1, "run_id": RUN, "task_id": "t-1", "severity": "HIGH",
                  "requested_by": "leader", "requested_at": NOW}
        r1 = self._ensure(marker=marker)
        self.assertEqual(r1.marker_status, "COMPATIBLE")
        r2 = self._ensure(marker=marker)
        self.assertEqual(r1.ticket_id, r2.ticket_id)

    def test_3_marker_conflict_outcome_authoritative_recorded(self):
        marker = {"version": 1, "run_id": RUN, "task_id": "t-1", "severity": "LOW",
                  "requested_by": "leader", "requested_at": NOW}
        r = self._ensure(marker=marker)
        self.assertTrue(r.ok)                          # 票据照建(outcome 权威)
        self.assertTrue(r.marker_status.startswith("CONFLICT:"))
        # 冲突进审计 request_hash
        row = self.store._conn.execute(
            "SELECT request_hash FROM ticket_audit WHERE to_status='PENDING' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn("ENSURE_CREATED", row[0])
        self.assertIn("CONFLICT", row[0])

    def test_4_replay_returns_same_ticket(self):
        r1 = self._ensure()
        r2 = self._ensure()
        self.assertEqual(r1.ticket_id, r2.ticket_id)
        self.assertTrue(r1.created)
        self.assertFalse(r2.created)
        self.assertIn("ENSURE_REPLAYED", self.store._conn.execute(
            "SELECT request_hash FROM ticket_audit ORDER BY id DESC LIMIT 1").fetchone()[0])

    def test_5_concurrent_creators_single_ticket(self):
        import threading
        s1, s2 = Store(self.path), Store(self.path)
        self.addCleanup(s1.close)
        self.addCleanup(s2.close)
        results = []

        def worker(store):
            results.append(orch.ensure_gate_ticket(make_outcome(), make_policy(),
                                                   store, now=NOW))

        threads = [threading.Thread(target=worker, args=(s,)) for s in (s1, s2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].ticket_id, results[1].ticket_id)
        self.assertEqual(sorted(r.created for r in results), [False, True])
        n = self.store._conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE status='PENDING'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_6_new_head_invalidates_old_tickets(self):
        r_old = self._ensure()   # head=HEAD
        self.assertTrue(r_old.ok)
        out_new = make_outcome(head_sha=HEAD2)
        r_new = orch.ensure_gate_ticket(out_new, make_policy(), self.store, now=NOW)
        self.assertTrue(r_new.ok)
        self.assertIn(r_old.ticket_id, r_new.invalidated)
        self.assertEqual(self.store.get(r_old.ticket_id).status, "INVALIDATED")

    def test_7_policy_off_no_ticket(self):
        n0 = self.store._conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        r = self._ensure(policy=policy_mod.UnconfiguredPolicy())
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "POLICY_NOT_CONFIGURED")
        n1 = self.store._conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        self.assertEqual(n0, n1)

    def test_8_action_disabled_refused(self):
        r = self._ensure(policy=make_policy(actions=("generate_patch",)))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "ACTION_NOT_ENABLED:run_poc")

    def test_9_malformed_outcome_refused_no_side_effect(self):
        for bad, frag in (("not-an-object", "not object"),
                          ({"schema_version": "x"}, "schema_version"),
                          (make_outcome(head_sha="short"), "40hex"),
                          (make_outcome(finding_validation="CONFIRMED", findings=[]),
                           "without findings")):
            n0 = self.store._conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            r = self._ensure(outcome=bad)
            self.assertFalse(r.ok, bad)
            self.assertIn(frag, r.reason)
            n1 = self.store._conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            self.assertEqual(n0, n1)

    def test_10_inconclusive_no_ticket(self):
        r = self._ensure(outcome=make_outcome(finding_validation="INCONCLUSIVE"))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "NO_CONFIRMED_FINDING:INCONCLUSIVE")
        self.assertEqual(self.store._conn.execute(
            "SELECT COUNT(*) FROM tickets").fetchone()[0], 0)

    def test_11_order_run_poc_then_generate_patch_sequential(self):
        r_poc = self._ensure()
        self.assertTrue(r_poc.ok)
        out = make_outcome(validations=[{"kind": "poc", "run_id": RUN,
                                         "head_sha": HEAD, "evidence_refs": ["poc.log"]}])
        r_blocked = self._ensure(outcome=out)   # run_poc 仍活动 → 拒绝无序并存
        self.assertFalse(r_blocked.ok)
        self.assertEqual(r_blocked.reason,
                         "SEQUENTIAL_GATE_REQUIRED:active=run_poc")
        # 顺序推进:approve → start_exec → complete(USE)→ generate_patch 可建
        t = self.store.get(r_poc.ticket_id)
        self.store.transition(r_poc.ticket_id, "approve", actor=NODE_ID, now=NOW)
        self.store.transition(r_poc.ticket_id, "start_exec", now=NOW)
        self.store.transition(r_poc.ticket_id, "complete",
                              result_fingerprint=core.canonical_hash({"poc": "done"}),
                              now=NOW)
        self.assertEqual(self.store.get(r_poc.ticket_id).status, "USED")
        r_patch = self._ensure(outcome=out)
        self.assertTrue(r_patch.ok)
        self.assertEqual(r_patch.action, "generate_patch")

    def test_12_outcome_drift_same_run_same_action_different_finding_refused(self):
        r1 = self._ensure(outcome=make_outcome(cwe="CWE-22"))
        self.assertTrue(r1.ok)
        r2 = self._ensure(outcome=make_outcome(cwe=None))   # 指纹漂移 → 不同 finding_id
        self.assertFalse(r2.ok)
        self.assertEqual(r2.reason, "OUTCOME_DRIFT:active finding_id differs")

    def test_13_crash_recovery_replay_no_duplicate(self):
        r1 = self._ensure()
        self.store.close()                     # 模拟桥崩溃
        self.store = Store(self.path)          # 重新打开
        r2 = self._ensure()
        self.assertEqual(r1.ticket_id, r2.ticket_id)
        self.assertFalse(r2.created)
        n = self.store._conn.execute(
            "SELECT COUNT(*) FROM tickets").fetchone()[0]
        self.assertEqual(n, 1)

    def test_15_old_run_outcome_cannot_reactivate_or_harm_after_new_head(self):
        """旧 CASE2-B 数据不可复用:旧 head 票据被新 head 确定性失效;
        旧 outcome 重放被 STALE_HEAD_OUTCOME 拒绝,不得失效当前票。"""
        old = make_outcome(run_id="run-gh-pr2-42ed1787-003205", head_sha="4" * 40)
        r_old = orch.ensure_gate_ticket(old, make_policy(), self.store, now=NOW)
        self.assertTrue(r_old.ok)
        r_new = orch.ensure_gate_ticket(make_outcome(), make_policy(), self.store,
                                        now=NOW, expected_head_sha=HEAD)
        self.assertTrue(r_new.ok)
        self.assertIn(r_old.ticket_id, r_new.invalidated)
        self.assertEqual(self.store.get(r_old.ticket_id).status, "INVALIDATED")
        # 旧 outcome 重放(携带可信当前 head)→ 拒绝,当前票不受影响
        r_stale = orch.ensure_gate_ticket(old, make_policy(), self.store, now=NOW,
                                          expected_head_sha=HEAD)
        self.assertFalse(r_stale.ok)
        self.assertEqual(r_stale.reason, "STALE_HEAD_OUTCOME")
        self.assertEqual(self.store.get(r_new.ticket_id).status, "PENDING")
        # 重放当前 outcome(带 expected)→ 幂等收敛同一张票
        r_new2 = orch.ensure_gate_ticket(make_outcome(), make_policy(), self.store,
                                         now=NOW, expected_head_sha=HEAD)
        self.assertEqual(r_new.ticket_id, r_new2.ticket_id)
        self.assertFalse(r_new2.created)

    def test_ensure_never_approves(self):
        r = self._ensure()
        t = self.store.get(r.ticket_id)
        self.assertEqual(t.status, "PENDING")
        self.assertIsNone(t.approved_by)
        decisions = self.store._conn.execute(
            "SELECT COUNT(*) FROM ticket_audit WHERE to_status IN "
            "('APPROVED','REJECTED')").fetchone()[0]
        self.assertEqual(decisions, 0)

    def test_observe_mode_writes_nothing(self):
        n0 = self.store._conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        r = self._ensure(mode="observe")
        self.assertFalse(r.ok)
        self.assertIn("OBSERVE_MODE", r.reason)
        self.assertIn("would_action=run_poc", r.reason)
        n1 = self.store._conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        self.assertEqual(n0, n1)


class StoreEventTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.d, "t.db"))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_record_event_append_only(self):
        r = orch.ensure_gate_ticket(make_outcome(), make_policy(), self.store, now=NOW)
        self.store.record_event(r.ticket_id, "PENDING", "PENDING", "op:note", "evt-1")
        rows = self.store._conn.execute(
            "SELECT to_status, actor FROM ticket_audit WHERE ticket_id=? "
            "ORDER BY id", (r.ticket_id,)).fetchall()
        self.assertTrue(any(x[1] == "control-plane:ensure" for x in rows))
        self.assertTrue(any(x[1] == "op:note" for x in rows))

    def test_active_by_repo_filters_repo(self):
        orch.ensure_gate_ticket(make_outcome(), make_policy(), self.store, now=NOW)
        other = Store(os.path.join(self.d, "t2.db"))
        self.addCleanup(other.close)
        orch.ensure_gate_ticket(make_outcome(repo="other/repo"), make_policy(),
                                other, now=NOW)
        self.assertEqual(len(self.store.active_by_repo(REPO)), 1)
        self.assertEqual(len(self.store.active_by_repo("other/repo")), 0)


class DispatchGateTests(unittest.TestCase):
    """场景 11/12:REJECTED/EXPIRED 不可派发;PENDING 派发为 0。"""

    @classmethod
    def setUpClass(cls):
        # 显式加载(不读 sys.modules 现状——合并运行时可能被其他套件替换/打桩)
        full = "approval_pkg.enforce"
        if full not in sys.modules or getattr(
                sys.modules[full], "__file__", None) is None:
            spec = importlib.util.spec_from_file_location(
                full, _APPROVAL / "enforce.py")
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = "approval_pkg"
            sys.modules[full] = mod
            spec.loader.exec_module(mod)
        cls.enforce = sys.modules[full]

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.d, "t.db"))
        self.r = orch.ensure_gate_ticket(make_outcome(), make_policy(),
                                         self.store, now=NOW)
        self.assertTrue(self.r.ok)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_pending_ticket_zero_dispatch(self):
        t = self.store.get(self.r.ticket_id)
        self.assertEqual(t.status, "PENDING")
        res = self.enforce.authorize_dispatch(make_policy(), t)
        self.assertFalse(res["ok"])                     # 仅 APPROVED/EXECUTING 可派发

    def test_rejected_ticket_zero_dispatch(self):
        self.store.transition(self.r.ticket_id, "reject", actor=NODE_ID, now=NOW)
        t = self.store.get(self.r.ticket_id)
        self.assertEqual(t.status, "REJECTED")
        res = self.enforce.authorize_dispatch(make_policy(), t)
        self.assertFalse(res["ok"])

    def test_expired_approval_zero_dispatch(self):
        self.store.transition(self.r.ticket_id, "approve", actor=NODE_ID,
                              now="2026-09-30T00:00:00+00:00")
        t = self.store.get(self.r.ticket_id)
        self.assertEqual(t.status, "EXPIRED")   # 过期边界 approve → EXPIRED
        self.assertNotEqual(t.status, "APPROVED")


if __name__ == "__main__":
    unittest.main()
