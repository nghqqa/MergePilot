"""M3.5 桥派发边界接线测试:off 零影响 / shadow 只读证据 / 失败 fail-soft。"""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from .test_publish_semantics import _delivery, _load_bridge


def _make_stub(mode, evidence=None, raise_error=None):
    stub = types.ModuleType("mp_v3_adapter")
    stub.review_v3_mode = lambda environ=None: mode
    stub.calls = []

    class _FakeStore:
        def close(self):
            pass

    def run_v3_shadow(d, *, run_store, rag_snapshot=None, **kw):
        stub.calls.append({"delivery_id": d["delivery_id"],
                           "rag_snapshot": rag_snapshot})
        if raise_error:
            raise raise_error
        return evidence or {"run_id": "shadow-gh-pr2-aaaaaaaa", "manifest_hash": "h" * 64,
                            "outcome": {"coverage_missing": []}}

    stub.run_v3_shadow = run_v3_shadow
    stub.open_run_store = lambda path=None: _FakeStore()
    return stub


class V3HookBoundaryTests(unittest.TestCase):
    """桥内 v3_shadow_hook:只在派发边界追加只读行为,旧结论逐项不变。"""

    def setUp(self):
        self.br = _load_bridge()
        sys.modules.pop("mp_v3_adapter", None)

    def tearDown(self):
        sys.modules.pop("mp_v3_adapter", None)

    def _process(self):
        d = _delivery()
        fins = []

        def fake_ssh(q):
            fins.append(q)
            return "UPDATE 1"

        with mock.patch.object(self.br, "ssh_psql", side_effect=fake_ssh), \
             mock.patch.object(self.br, "already_processed", return_value=False), \
             mock.patch.object(self.br, "rag_dispatch_gate", return_value=(True, {})), \
             mock.patch.object(self.br, "seed_project", return_value=True), \
             mock.patch.object(self.br, "wake_workers", return_value=True), \
             mock.patch.object(self.br, "prepare_run_manifest",
                               return_value=("kickoff+ref", None)), \
             mock.patch.object(self.br.mx, "send",
                               return_value={"event_id": "$ev"}), \
             mock.patch.object(self.br, "watch_run",
                               return_value=("completed", "report")), \
             mock.patch.object(self.br, "project_result", return_value="res"), \
             mock.patch.object(self.br, "gate_record", return_value=""), \
             mock.patch.object(self.br, "publish_with_retry",
                               return_value={"ok": True, "check_run_id": 7}):
            self.br.process(d, timeout_min=1, dry=False)
        return [q for q in fins if "processed_at=now()" in q]

    def test_off_mode_no_adapter_invocation_legacy_unchanged(self):
        stub = _make_stub("off")
        sys.modules["mp_v3_adapter"] = stub
        fins = self._process()
        self.assertEqual(stub.calls, [])                       # 零开销
        self.assertEqual(len(fins), 1)
        self.assertIn("status='PROCESSED'", fins[0])           # 旧链路逐项不变
        self.assertIn("check_run=7", fins[0])

    def test_shadow_mode_evidence_written_legacy_unchanged(self):
        stub = _make_stub("shadow")
        sys.modules["mp_v3_adapter"] = stub
        fins = self._process()
        self.assertEqual(len(stub.calls), 1)                   # shadow 证据已产出
        self.assertEqual(len(fins), 1)
        self.assertIn("status='PROCESSED'", fins[0])           # 旧结论不变
        self.assertIn("check_run=7", fins[0])

    def test_shadow_error_does_not_break_legacy(self):
        stub = _make_stub("shadow", raise_error=RuntimeError("shadow exploded"))
        sys.modules["mp_v3_adapter"] = stub
        fins = self._process()
        self.assertEqual(len(stub.calls), 1)                   # 尝试过
        self.assertEqual(len(fins), 1)
        self.assertIn("status='PROCESSED'", fins[0])           # 旧链路照常完成


if __name__ == "__main__":
    unittest.main(verbosity=2)
