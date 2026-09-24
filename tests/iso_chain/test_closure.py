# -*- coding: utf-8 -*-
"""closure 批次确定性测试(CL-02..CL-07)。全部离线:无模型、无网络、无共享环境。"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.normpath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_REPO, "tools", "iso_chain"))
sys.path.insert(0, os.path.join(_REPO, "tools", "approval"))

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
sys.modules.setdefault("gate_ticket", sys.modules["approval_pkg.gate_ticket"])
SQLiteTicketStore = _smod.SQLiteTicketStore
sys.path.insert(0, os.path.join(_REPO, "tools", "iso_chain"))
import dispatch as disp  # noqa: E402
import verifier as vf  # noqa: E402
import patchwork as pw  # noqa: E402
structured_gate_iso = _load("structured_gate_iso",
                            os.path.join(_REPO, "tools", "iso_chain",
                                         "structured_gate.py"))
sg = structured_gate_iso
StructuredGateError = sg.StructuredGateError


def _load(name, path):
    spec = importlib.util.spec_from_file_location("approval_pkg." + name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["approval_pkg." + name] = mod
    spec.loader.exec_module(mod)
    return mod


_iso = _load("structured_gate_iso", os.path.join(_REPO, "tools", "iso_chain",
                                                 "structured_gate.py"))
sg = _iso

RUN = "iso-run-42ed1787"
HEAD = "42ed17879becbc02e31551938afbbf689351df96"
REPO = "nghqqa/fastapi-boilerplate-demo"
TASK = "gh-pr2-42ed1787-review-1"
NOW = "2026-09-24T12:00:00+00:00"
VERDICT_TEXT = ("STATUS: SUCCESS\nSUMMARY: STATUS: FINDING_CONFIRMED; "
                "SEVERITY: HIGH; HUMAN_VERIFICATION_REQUIRED: YES. "
                "PoC: ../outside-secret.txt HTTP 200. rag_retrieve -> "
                "org-standards/cwe-22-path-traversal.md#1")
AUDIT = [{"ts": "2026-09-24T00:32:26Z", "tool": "skill_diff_parse",
          "result_status": "OK"},
         {"ts": "2026-09-24T00:32:54Z", "tool": "rag.retrieve",
          "result_status": "OK", "document_count": 3}]


def make_store(path):
    return SQLiteTicketStore(path)


class StructuredGateTests(unittest.TestCase):
    """CL-02:结构化建票(证据绑定;不依赖 marker 文件/自然语言)。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sg-")
        self.store = make_store(os.path.join(self._tmp, "t.db"))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _open(self, **kw):
        kw.setdefault("executor_id", "iso-chain")
        kw.setdefault("run", {"run_id": RUN, "repo": REPO, "head_sha": HEAD})
        kw.setdefault("reviewer_result_text", VERDICT_TEXT)
        kw.setdefault("audit_records", AUDIT)
        kw.setdefault("task_id", TASK)
        kw.setdefault("now", NOW)
        return sg.open_structured_gate_ticket(self.store, **kw)

    def test_creates_pending_ticket_evidence_bound(self):
        t, created, why = self._open()
        self.assertFalse(why)
        self.assertTrue(created)
        self.assertEqual(t.status, "PENDING")
        self.assertEqual(t.binding.run_id, RUN)
        self.assertEqual(t.binding.repo, REPO)
        self.assertEqual(t.binding.head_sha, HEAD)

    def test_idempotent(self):
        t1, c1, _ = self._open()
        t2, c2, _ = self._open()
        self.assertTrue(c1 and not c2 and t1.ticket_id == t2.ticket_id)

    def test_not_confirmed_is_not_gate(self):
        t, created, why = self._open(
            reviewer_result_text="STATUS: FINDING_CONFIRMED; SEVERITY: HIGH; "
                                 "HUMAN_VERIFICATION_REQUIRED: NO")
        self.assertIsNone(t)
        self.assertIn("NOT_A_GATE", why)

    def test_no_audit_coexistence_refused(self):
        t, created, why = self._open(audit_records=[])
        self.assertIsNone(t)
        self.assertIn("EVIDENCE_COEXISTENCE", why)

    def test_verdict_missing_refused(self):
        t, created, why = self._open(reviewer_result_text="we fixed it, trust me")
        self.assertIsNone(t)
        self.assertIn("VERDICT_MISSING", why)


class HeadFreshnessTests(unittest.TestCase):
    """CL-03:head 新鲜度。"""

    def test_fresh_and_stale(self):
        import chain
        self.assertTrue(chain.head_is_fresh(HEAD.upper(), HEAD))
        self.assertFalse(chain.head_is_fresh("f" * 40, HEAD))


class DispatchFencingTests(unittest.TestCase):
    """CL-04:start_exec fencing + outbox 状态 + 未知结果对账。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="dsp-")
        self.store = make_store(os.path.join(self._tmp, "t.db"))
        self.outbox = disp.DispatchOutbox(os.path.join(self._tmp, "o.db"))
        t, _, _ = gt.open_gate_ticket(
            self.store, {"version": 1, "run_id": RUN, "task_id": TASK,
                         "severity": "HIGH", "requested_by": "leader",
                         "requested_at": NOW},
            RUN, REPO, HEAD, TASK, now=NOW)
        self.ticket = t

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_fencing_second_dispatch_rejected(self):
        self.store.transition(self.ticket.ticket_id, "approve",
                              actor="op", now=NOW)   # 先到 APPROVED 才可派发
        r1 = self.store.transition(self.ticket.ticket_id, "start_exec", now=NOW)
        r2 = self.store.transition(self.ticket.ticket_id, "start_exec", now=NOW)
        self.assertTrue(r1.ok)
        self.assertFalse(r2.ok)
        self.assertIn("EXECUTING", r2.reason)

    def test_outbox_sent_then_executed(self):
        did = "dsp-x1"
        self.outbox.record_sent(did, self.ticket.ticket_id, 1, "h", NOW)
        rec = self.outbox.get(did)
        self.assertEqual(rec["state"], "SENT")
        self.outbox.mark(did, "EXECUTED", NOW)
        self.assertEqual(self.outbox.get(did)["state"], "EXECUTED")

    def test_unknown_result_reconcile(self):
        did = "dsp-u1"
        self.outbox.record_sent(did, self.ticket.ticket_id, 1, "h", NOW)
        # probe 找到 → 回填 EXECUTED
        rep = disp.reconcile_unknown(self.outbox, self.store,
                                     lambda p: {"found": True} if p["dispatch_id"] == did else None,
                                     now=NOW)
        self.assertEqual(self.outbox.get(did)["state"], "EXECUTED")
        # probe 找不到 → 保持 SENT(unresolved),不得重派
        rep2 = disp.reconcile_unknown(self.outbox, self.store, lambda p: None, now=NOW)
        self.assertEqual(len(rep2["unresolved"]), 0)   # 已回填的不再重复对账

    def test_dispatch_without_executable_ticket_refused(self):
        called = []
        with self.assertRaises(disp.DispatchError):
            disp.dispatch_fixer(self.store, self.outbox, self.ticket.ticket_id,
                                lambda p: called.append(p) or {}, {}, now=NOW)
        self.assertEqual(called, [])   # 未 APPROVED 不得执行


class VerifierTests(unittest.TestCase):
    """CL-05:独立 verifier 输入边界与最终判定。"""

    def test_input_has_no_fixer_reasoning_param(self):
        import inspect
        sig = inspect.signature(vf.build_verifier_input)
        self.assertNotIn("fixer_reasoning", sig.parameters)
        self.assertNotIn("fixer_messages", sig.parameters)

    def test_finalize_matrix(self):
        tests_ok = {"all_passed": True}
        tests_bad = {"all_passed": False}
        apply_ok = {"ok": True}
        self.assertEqual(vf.finalize({"verdict": "VERIFIED"}, tests_ok, apply_ok)["final"],
                         "VERIFIED")
        self.assertEqual(vf.finalize({"verdict": "REJECTED"}, tests_ok, apply_ok)["final"],
                         "VERIFIED_WITH_MODEL_OBJECTION")
        self.assertEqual(vf.finalize({"verdict": "VERIFIED"}, tests_bad, apply_ok)["final"],
                         "NOT_VERIFIED")
        self.assertEqual(vf.finalize({}, tests_bad, {"ok": False})["final"],
                         "APPLY_FAILED")

    def test_parse_verdict_format(self):
        v = vf.parse_verdict("VERDICT: VERIFIED\nREASON: containment ok\nGAPS: none")
        self.assertEqual(v["verdict"], "VERIFIED")
        self.assertEqual(v["gaps"], "none")
        self.assertIsNone(vf.parse_verdict("no format")["verdict"])


class PatchArtifactsTests(unittest.TestCase):
    """CL-06:产物绑定/哈希/干净 checkout 应用。"""

    PATCH = """--- a/backend/src/interfaces/api/v1/demo_high_risk.py
+++ b/backend/src/interfaces/api/v1/demo_high_risk.py
@@ -38,3 +38,10 @@
 async def demo_download(name: str) -> FileResponse:
-    file_path = os.path.join(DEMO_FILES_DIR, name)
+    base = os.path.realpath(DEMO_FILES_DIR)
+    file_path = os.path.realpath(os.path.join(DEMO_FILES_DIR, name))
+    if os.path.commonpath([file_path, base]) != base:
+        from fastapi import HTTPException
+        raise HTTPException(status_code=400, detail="invalid path")
+    if not os.path.isfile(file_path):
+        from fastapi import HTTPException
+        raise HTTPException(status_code=404, detail="not found")
     return FileResponse(file_path, filename=name)
"""

    def test_artifacts_binding_and_hash(self):
        with tempfile.TemporaryDirectory() as td:
            man = pw.build_artifacts(self.PATCH, run_id="r", head_sha="a" * 40,
                                     ticket_id="tkt-1", attempt=1, report={},
                                     out_dir=td)
            self.assertEqual(man["patch_sha256"], pw.sha256_text(self.PATCH))
            self.assertIn("backend/src/interfaces/api/v1/demo_high_risk.py",
                          man["files_changed"])
            man2 = pw.build_artifacts(self.PATCH + "x", run_id="r",
                                      head_sha="a" * 40, ticket_id="tkt-1",
                                      attempt=1, report={}, out_dir=td)
            self.assertNotEqual(man2["patch_sha256"], man["patch_sha256"])

    def test_apply_on_clean_checkout(self):
        """git apply --check 于干净临时 git 仓库:合法补丁通过、篡改失败。"""
        with tempfile.TemporaryDirectory() as td:
            wt = os.path.join(td, "wt")
            os.makedirs(wt)
            target = os.path.join(wt, "backend", "src", "interfaces", "api", "v1",
                                  "demo_high_risk.py")
            os.makedirs(os.path.dirname(target))
            with open(target, "w", encoding="utf-8") as f:
                f.write("async def demo_download(name: str) -> FileResponse:\n"
                        "    file_path = os.path.join(DEMO_FILES_DIR, name)\n"
                        "    return FileResponse(file_path, filename=name)\n")
            import subprocess
            def git(*a):
                subprocess.run(["git"] + list(a), cwd=wt, check=True,
                               capture_output=True)
            git("init"); git("add", ".")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
            p = os.path.join(td, "p.diff")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.PATCH)
            r1 = pw.apply_check(wt, p)
            self.assertTrue(r1["ok"], r1["detail"])
            tampered = self.PATCH.replace("-    file_path = os.path.join(DEMO_FILES_DIR, name)",
                                          "-    file_path = DIFFERENT")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(tampered)
            r2 = pw.apply_check(wt, p)
            self.assertFalse(r2["ok"])


if __name__ == "__main__":
    unittest.main()
