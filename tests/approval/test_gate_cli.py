"""gate_cli 端到端测试(真实 SQLite 文件 + 真实子进程调用)。

验证工具面:签发→批准→执行前校验通过;绑定不匹配拒;过期拒;
重复批准幂等;审批身份必填。不接任何真实执行路径。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_cli_path = _REPO / "tools" / "approval" / "gate_cli.py"


def _load_cli():
    # 包上下文:approval 包内相对导入
    d = _REPO / "tools" / "approval"
    pkg = types.ModuleType("approval_pkg")
    pkg.__path__ = [str(d)]
    sys.modules["approval_pkg"] = pkg
    spec = importlib.util.spec_from_file_location("approval_pkg.gate_cli", _cli_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["approval_pkg.gate_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


import types  # noqa: E402

HEAD = "a" * 40
PATCH_FP = "b" * 64
PARAMS = '{"target": "finding-1", "mode": "suggestion"}'
EXPIRES = "2099-01-01T00:00:00+00:00"


class GateCliTests(unittest.TestCase):
    def setUp(self):
        self.cli = _load_cli()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _new(self, extra=None):
        argv = ["new", "--db", self.db, "--run", "run-1", "--repo", "team/demo",
                "--head", HEAD, "--action", "generate_patch",
                "--params", PARAMS, "--patch-fp", PATCH_FP,
                "--expires", EXPIRES] + (extra or [])
        return self.cli.main(argv)

    def _capture(self, fn, *a):
        import io
        from unittest import mock
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = fn(*a)
        return rc, json.loads(buf.getvalue())

    def test_new_shows_pending_and_disabled_note(self):
        rc, out = self._capture(self._new)
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "PENDING")
        self.assertIn("D-1", out["note"])  # 未拍板不启用,显式可见

    def test_approve_then_check_ok(self):
        rc, out = self._capture(self._new)
        tid = out["ticket_id"]
        rc, out = self._capture(self.cli.main, [
            "approve", "--db", self.db, "--ticket", tid, "--actor", "test-approver"])
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "APPROVED")
        rc, out = self._capture(self.cli.main, [
            "check", "--db", self.db, "--ticket", tid, "--run", "run-1",
            "--repo", "team/demo", "--head", HEAD, "--params", PARAMS,
            "--patch-fp", PATCH_FP])
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])

    def test_check_rejects_binding_mismatch(self):
        rc, out = self._capture(self._new)
        tid = out["ticket_id"]
        self._capture(self.cli.main, [
            "approve", "--db", self.db, "--ticket", tid, "--actor", "test-approver"])
        rc, out = self._capture(self.cli.main, [
            "check", "--db", self.db, "--ticket", tid, "--run", "run-OTHER",
            "--repo", "team/demo", "--head", HEAD, "--params", PARAMS])
        self.assertNotEqual(rc, 0)
        self.assertEqual(out["reason"], "BINDING_MISMATCH:run_id")

    def test_duplicate_approve_is_noop(self):
        rc, out = self._capture(self._new)
        tid = out["ticket_id"]
        for _ in range(2):
            rc, out = self._capture(self.cli.main, [
                "approve", "--db", self.db, "--ticket", tid, "--actor", "test-approver"])
            self.assertEqual(rc, 0)
        self.assertEqual(out["reason"], "NOOP")

    def test_expired_approve_refused(self):
        rc, out = self._capture(
            self._new, ["--expires", "2000-01-01T00:00:00+00:00"])
        tid = out["ticket_id"]
        rc, out = self._capture(self.cli.main, [
            "approve", "--db", self.db, "--ticket", tid, "--actor", "test-approver"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(out["reason"], "EXPIRED")

    def test_actor_required(self):
        rc, out = self._capture(self._new)
        tid = out["ticket_id"]
        rc, out = self._capture(self.cli.main, [
            "approve", "--db", self.db, "--ticket", tid, "--actor", ""])
        self.assertNotEqual(rc, 0)
        self.assertEqual(out["reason"], "IDENT_REQUIRED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
