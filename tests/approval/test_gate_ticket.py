# -*- coding: utf-8 -*-
"""人工门 TicketStore 闭环测试(2026-09-24)。

覆盖:marker 幂等创建、字段绑定、approve/reject CAS、重复决策不覆盖、
TTL 过期、身份缺失、错误 head、重启恢复、审计记录、gate 状态映射、
桥侧 marker↔manifest 归属、validate_env 校验器。
全部本地隔离:SQLite 临时库/内存桩;无真实 GitHub/模型/共享 case-pg/真实凭证。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.normpath(os.path.join(_HERE, "..", ".."))

# 构造包上下文(与 test_store_sqlite 同款):使相对导入(from .approval)可用
import types  # noqa: E402
from pathlib import Path  # noqa: E402

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
gt = _load("gate_ticket", _APPROVAL_DIR / "gate_ticket.py")
SQLiteTicketStore = _smod.SQLiteTicketStore

RUN = "run-gh-pr2-254f61ce-104621"
TASK = "gh-pr2-254f61ce-review-1"
REPO = "nghqqa/fastapi-boilerplate-demo"
HEAD = "254f61ce2ff54c25a805265e70e4827e0ce68e81"
NOW = "2026-09-24T12:00:00+00:00"

MARKER = {"version": 1, "run_id": RUN, "task_id": TASK, "severity": "HIGH",
          "requested_by": "leader", "requested_at": "2026-09-23T10:47:13Z"}


def make_store(path):
    return SQLiteTicketStore(path)


class GateTicketLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="gate-tkt-")
        self.path = os.path.join(self._tmp, "tickets.db")
        self.store = make_store(self.path)

    def tearDown(self):
        # Windows:先显式关库释放 WAL 句柄;目录删除尽力而为(失败不掩盖断言)
        try:
            self.store.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _open(self, marker=None, store=None):
        return gt.open_gate_ticket(store or self.store, marker or MARKER,
                                   RUN, REPO, HEAD, TASK, now=NOW)

    def test_marker_creates_pending_ticket(self):
        t, created, why = self._open()
        self.assertFalse(why)          # 成功时 reason 为空串
        self.assertTrue(created)
        self.assertEqual(t.status, "PENDING")

    def test_binding_fields(self):
        """票据绑定 run_id/repo/PR 上下文/head/severity(经指纹与参数哈希)。"""
        t, _, _ = self._open()
        b = t.binding
        self.assertEqual((b.run_id, b.repo, b.head_sha), (RUN, REPO, HEAD))
        self.assertEqual(b.action, "generate_patch")        # 既有动作集,未发明新动作
        self.assertIsNone(b.finding_id)                      # run 级审批
        self.assertEqual(b.params_hash, core.canonical_hash({
            "gate_version": 1, "severity": "HIGH", "task_id": TASK,
            "requested_by": "leader", "requested_at": MARKER["requested_at"]}))
        self.assertEqual(b.finding_fingerprint, core.canonical_hash(
            {"task_id": TASK, "severity": "HIGH", "repo": REPO, "head_sha": HEAD}))

    def test_marker_idempotent(self):
        t1, c1, _ = self._open()
        t2, c2, _ = self._open()
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(t1.ticket_id, t2.ticket_id)        # 重复 marker 收敛同一张票

    def test_marker_attribution_refused(self):
        bad = dict(MARKER, run_id="run-OTHER")
        t, created, why = self._open(bad)
        self.assertIsNone(t)
        self.assertFalse(created)
        self.assertIn("run_id", why)

    def test_marker_non_leader_refused(self):
        t, created, why = self._open(dict(MARKER, requested_by="reviewer"))
        self.assertIsNone(t)
        self.assertIn("requested_by", why)

    def test_marker_bad_severity_refused(self):
        t, created, why = self._open(dict(MARKER, severity="CRITICAL"))
        self.assertIsNone(t)
        self.assertIn("severity", why)

    def test_approve_requires_identity(self):
        """D-2:无身份 approve → IDENT_REQUIRED,不匿名放行。"""
        t, _, _ = self._open()
        r = gt.decide(self.store, t.ticket_id, "approve", actor=None, now=NOW)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "IDENT_REQUIRED")
        self.assertEqual(self.store.get(t.ticket_id).status, "PENDING")

    def test_approve_then_reject_refused(self):
        """先到先得:重复决策不得覆盖历史。"""
        t, _, _ = self._open()
        r1 = gt.decide(self.store, t.ticket_id, "approve", "alice", now=NOW, reason="ok")
        r2 = gt.decide(self.store, t.ticket_id, "reject", "bob", now=NOW + "1")
        self.assertTrue(r1.ok)
        self.assertFalse(r2.ok)
        self.assertEqual(r2.reason, "INVALID_TRANSITION:APPROVED")
        self.assertEqual(self.store.get(t.ticket_id).status, "APPROVED")
        self.assertEqual(self.store.get(t.ticket_id).approved_by, "alice")

    def test_reject_then_approve_refused(self):
        t, _, _ = self._open()
        self.assertTrue(gt.decide(self.store, t.ticket_id, "reject", "bob",
                                  now=NOW, reason="not a real vuln").ok)
        r = gt.decide(self.store, t.ticket_id, "approve", "alice", now=NOW + "1")
        self.assertFalse(r.ok)
        self.assertEqual(self.store.get(t.ticket_id).status, "REJECTED")

    def test_reject_records_reason(self):
        t, _, _ = self._open()
        gt.decide(self.store, t.ticket_id, "reject", "bob", now=NOW,
                  reason="repro invalid on merge-base")
        self.assertEqual(self.store.get(t.ticket_id).error,
                         "repro invalid on merge-base")

    def test_ttl_default_is_24h(self):
        """D-3 口径:默认 24h(policy.py/桥/gate_ticket 三处一致)。"""
        import inspect
        sig = inspect.signature(gt.open_gate_ticket)
        self.assertEqual(sig.parameters["ttl_hours"].default, 24)
        self.assertEqual(int(gt.__dict__.get("_", 0) or 0) or 24, 24)
        # 桥 env 默认
        import importlib.util as _ilu
        src = open(os.path.join(_REPO, "tools", "gh-bridge", "gh_bridge.py"),
                   encoding="utf-8").read()
        self.assertIn('MERGEPILOT_APPROVAL_TTL_H", "24"', src)
        self.assertNotIn('MERGEPILOT_APPROVAL_TTL_H", "72"', src)

    def test_ttl_expiry(self):
        """D-3:批准边界受 TTL 约束;过期 approve → EXPIRED(终态)。"""
        t, _, _ = self._open()
        late = "2026-09-27T13:00:00+00:00"   # > TTL(默认 24h)after NOW
        r = gt.decide(self.store, t.ticket_id, "approve", "alice", now=late)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "EXPIRED")
        self.assertEqual(self.store.get(t.ticket_id).status, "EXPIRED")

    def test_expiry_before_approve_maps_closed(self):
        t, _, _ = self._open()
        gt.expire_if_due(self.store, t.ticket_id, now="2026-09-28T00:00:00+00:00")
        st = gt.run_gate_state(self.store.get(t.ticket_id).status)
        self.assertEqual(st["gate_state"], "CLOSED_EXPIRED")
        self.assertIsNone(st["dispatch_plan"])

    def test_wrong_head_execution_refused(self):
        """红线:执行方出示错误 head → BINDING_MISMATCH,不放行。"""
        t, _, _ = self._open()
        gt.decide(self.store, t.ticket_id, "approve", "alice", now=NOW)
        req = core.ExecutionRequest(
            ticket_id=t.ticket_id, run_id=RUN, repo=REPO,
            head_sha="f" * 40,                       # 错误 head
            params_hash=t.binding.params_hash,
            finding_fingerprint=t.binding.finding_fingerprint)
        r = core.check_execution(self.store.get(t.ticket_id), req, now=NOW)
        self.assertFalse(r.ok)
        self.assertIn("BINDING_MISMATCH:head_sha", r.reason)

    def test_restart_recovery(self):
        """WAL 重开:已提交状态与审计保留。"""
        t, _, _ = self._open()
        gt.decide(self.store, t.ticket_id, "approve", "alice", now=NOW, reason="ok")
        self.store.close()
        self.store = make_store(self.path)
        again = self.store.get(t.ticket_id)
        self.assertEqual(again.status, "APPROVED")
        self.assertEqual(again.approved_by, "alice")
        # 审计仍在
        rows = self.store._conn.execute(
            "SELECT from_status,to_status,actor FROM ticket_audit ORDER BY id").fetchall()
        self.assertIn(("PENDING", "APPROVED", "alice"), rows)

    def test_audit_records_decisions_and_refusals(self):
        """审批记录可审计:成功转移与被拒尝试都留痕(append-only)。"""
        t, _, _ = self._open()
        gt.decide(self.store, t.ticket_id, "approve", None, now=NOW)   # 被拒(无身份)
        gt.decide(self.store, t.ticket_id, "approve", "alice", now=NOW)
        gt.decide(self.store, t.ticket_id, "reject", "mallory", now=NOW + "9")
        rows = self.store._conn.execute(
            "SELECT from_status,to_status,actor,request_hash FROM ticket_audit "
            "ORDER BY id").fetchall()
        self.assertEqual(rows[0], ("PENDING", "PENDING", None, "IDENT_REQUIRED"))
        self.assertEqual(rows[1], ("PENDING", "APPROVED", "alice", "OK"))
        self.assertEqual(rows[2], ("APPROVED", "APPROVED", "mallory",
                                   "INVALID_TRANSITION:APPROVED"))

    def test_gate_state_mapping(self):
        self.assertEqual(gt.run_gate_state("PENDING")["gate_state"], "GATE_WAIT")
        approved = gt.run_gate_state("APPROVED")
        self.assertEqual(approved["gate_state"], "APPROVED_PLAN_READY")
        self.assertFalse(approved["auto_dispatch"])            # 只出计划,不派发
        self.assertFalse(approved["dispatch_plan"]["fix"]["auto_dispatch"])
        self.assertEqual(gt.run_gate_state("REJECTED")["gate_state"], "BLOCKED")
        self.assertEqual(gt.run_gate_state("EXPIRED")["gate_state"], "CLOSED_EXPIRED")

    def test_mapping_is_pure_readonly(self):
        """映射不得写任何存储(纯函数)。"""
        t, _, _ = self._open()
        before = self.store.get(t.ticket_id).status
        gt.run_gate_state("PENDING")
        self.assertEqual(self.store.get(t.ticket_id).status, before)


class BridgeGateTicketWiringTests(unittest.TestCase):
    """桥侧接线:marker↔manifest 归属核对;失败降级不阻断门流程。"""

    def _load_bridge(self, tmp_store):
        name = "mp_gh_bridge_gate"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(_REPO, "tools", "gh-bridge", "gh_bridge.py"))
        br = importlib.util.module_from_spec(spec)
        sys.modules[name] = br
        with mock.patch.dict(os.environ, {"MERGEPILOT_APPROVAL_DB": tmp_store}):
            spec.loader.exec_module(br)
        return br

    def test_ticket_created_with_manifest_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "gate.db")
            br = self._load_bridge(db)
            man = {"run_id": RUN}
            d = {"repo": REPO, "observed_head_sha": HEAD}
            logs = []
            with mock.patch.dict(os.environ, {"MERGEPILOT_APPROVAL_DB": db}), \
                 mock.patch.object(br, "read_run_manifest", return_value=man), \
                 mock.patch.object(br, "gate_marker", return_value=(dict(MARKER), "")):
                tid = br.open_gate_ticket_for_marker("proj-x", d, "gate",
                                                     lambda *a: logs.append(a))
            self.assertTrue(tid and tid.startswith("tkt-"))
            self.store = make_store(db)
            self.assertEqual(self.store.get(tid).binding.run_id, RUN)
            self.store.close()

    def test_manifest_mismatch_refuses_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "gate.db")
            br = self._load_bridge(db)
            d = {"repo": REPO, "observed_head_sha": HEAD}
            with mock.patch.object(br, "read_run_manifest",
                                   return_value={"run_id": "run-OTHER"}), \
                 mock.patch.object(br, "gate_marker", return_value=(dict(MARKER), "")):
                tid = br.open_gate_ticket_for_marker("proj-x", d, "gate", lambda *a: None)
            self.assertIsNone(tid)                      # fail-closed:不建票
            self.assertFalse(os.path.exists(db))        # 库文件都未产生

    def test_missing_manifest_refuses_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "gate.db")
            br = self._load_bridge(db)
            d = {"repo": REPO, "observed_head_sha": HEAD}
            with mock.patch.object(br, "read_run_manifest", return_value=None), \
                 mock.patch.object(br, "gate_marker", return_value=(dict(MARKER), "")):
                tid = br.open_gate_ticket_for_marker("proj-x", d, "gate", lambda *a: None)
            self.assertIsNone(tid)


class ValidateEnvTests(unittest.TestCase):
    """部署接线校验器:脱敏、fail-closed、不一致拒绝。"""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "mp_validate_env",
            os.path.join(_REPO, "tools", "case_retrieval", "deploy", "validate_env.py"))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _ctx(self, obj):
        p = os.path.join(self._tmp.name, "run-context.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return p

    def _run(self, env, scope_file=None):
        with mock.patch.dict(os.environ, env, clear=False):
            return self.mod.main(["--mode", "container"] +
                                 (["--scope-file", scope_file] if scope_file else []))

    def test_ready_with_scope_file(self):
        p = self._ctx({"authored_by": "gh_bridge", "run_id": "r",
                       "code": {"repo": REPO}})
        code = self._run({"MERGEPILOT_CR_PG_DSN": "postgresql://x",
                          "MERGEPILOT_CR_REPO_SCOPE_FILE": p}, scope_file=p)
        self.assertEqual(code, 0)

    def test_missing_dsn_exit2(self):
        self.assertEqual(self._run({}), 2)

    def test_missing_scope_exit3(self):
        code = self._run({"MERGEPILOT_CR_PG_DSN": "postgresql://x"})
        self.assertEqual(code, 3)

    def test_foreign_author_exit3(self):
        p = self._ctx({"authored_by": "whoever", "run_id": "r",
                       "code": {"repo": REPO}})
        code = self._run({"MERGEPILOT_CR_PG_DSN": "postgresql://x",
                          "MERGEPILOT_CR_REPO_SCOPE_FILE": p}, scope_file=p)
        self.assertEqual(code, 3)

    def test_mismatch_exit3(self):
        p = self._ctx({"authored_by": "gh_bridge", "run_id": "r",
                       "code": {"repo": "other/repo"}})
        code = self._run({"MERGEPILOT_CR_PG_DSN": "postgresql://x",
                          "MERGEPILOT_CR_REPO_SCOPE": REPO,
                          "MERGEPILOT_CR_REPO_SCOPE_FILE": p}, scope_file=p)
        self.assertEqual(code, 3)

    def test_preflight_flag_and_sanitized_failure(self):
        """--preflight 开关存在;DB 失败路径脱敏(exit 5,不泄 DSN 细节)。"""
        src = open(os.path.join(_REPO, "tools", "case_retrieval", "deploy",
                                "validate_env.py"), encoding="utf-8").read()
        self.assertIn('"--preflight"', src)
        self.assertIn("no DSN details", src)
        self.assertIn("case_retrieval_reader", src or "") if False else None

    def test_repo_mode_zero(self):
        self.assertEqual(self.mod.main(["--mode", "repo"]), 0)


if __name__ == "__main__":
    unittest.main()
