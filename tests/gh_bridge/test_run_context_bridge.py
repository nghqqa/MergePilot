# -*- coding: utf-8 -*-
"""桥侧 run 上下文接线契约测试(R5 透传修复)。

验收点:write-once 冲突 fail-closed;审计通道 advisory(不可达不阻断);
attempt_no 只由投递行 RQn 推导;process() 新步骤失败 → 投递 ERROR 不派发。
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..", "..", "tools", "gh-bridge")))


def _load_bridge():
    name = "mp_gh_bridge_under_test"
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_HERE, "..", "..", "tools", "gh-bridge", "gh_bridge.py"))
    br = importlib.util.module_from_spec(spec)
    sys.modules[name] = br
    spec.loader.exec_module(br)
    return br


def _delivery():
    return {"delivery_id": "90cf0420-b70d-11f1-831e-d062c23f6de2",
            "event_name": "pull_request", "action": "synchronize",
            "repo": "nghqqa/fastapi-boilerplate-demo", "pr_number": 9,
            "observed_head_sha": "89c65a47" + "0" * 32,
            "observed_base_sha": "fdde4f41" + "0" * 32,
            "error": None, "received_at": "2026-09-23T05:13:55Z"}


def _manifest():
    return {"run_id": "run-gh-pr9-89c65a47-052152",
            "code": {"repo": "nghqqa/fastapi-boilerplate-demo",
                     "head_sha": "89c65a47" + "0" * 32,
                     "base_sha": "fdde4f41" + "0" * 32},
            "skills": {"content_sha256": {"skill_diff_parse": "aa" * 32}},
            "rag": {"retrieval_mode": "lexical-zh-en-v1"}}


class RunContextWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_prepare_run_context_happy_path(self):
        br = self.br
        d, man, written, posted = _delivery(), _manifest(), [], []
        with mock.patch.object(br, "write_run_context",
                               side_effect=lambda p, c: written.append(c) or {"ok": True}), \
             mock.patch.object(br, "audit_post_record",
                               side_effect=lambda r: posted.append(r) or True):
            ctx, err = br.prepare_run_context(man, d, "proj-x", attempt_no=2)
        self.assertIsNone(err)
        self.assertEqual(ctx["attempt_no"], 2)             # 桥计数,非投递行之外来源
        self.assertEqual(ctx["run_id"], man["run_id"])
        self.assertEqual(written, [ctx])
        self.assertEqual(posted[0]["record_type"], "bridge.run_context")

    def test_write_conflict_fails_closed(self):
        br = self.br
        with mock.patch.object(br, "write_run_context",
                               return_value={"ok": False, "reason": "run-context conflict"}):
            ctx, err = br.prepare_run_context(_manifest(), _delivery(), "proj-x", 1)
        self.assertIsNone(ctx)
        self.assertIn("conflict", err)

    def test_module_missing_degrades_honestly(self):
        br = self.br
        with mock.patch.object(br, "_rc", None):
            ctx, err = br.prepare_run_context(_manifest(), _delivery(), "proj-x", 1)
        self.assertIsNone(ctx)
        self.assertIn("not synced", err)

    def test_attempt_no_from_delivery_row(self):
        self.assertEqual(self.br.attempt_no_for(_delivery()), 1)
        d = _delivery()
        d["error"] = "RQ2"
        self.assertEqual(self.br.attempt_no_for(d), 3)     # 1 + 回队 2 次

    def test_process_run_context_failure_blocks_dispatch(self):
        """fail-closed:run-context 写失败 → 投递 ERROR、不发 kickoff。"""
        br = self.br
        d = _delivery()
        sent = []

        def fake_ssh(q):
            return "UPDATE 1"

        with mock.patch.object(br, "ssh_psql", side_effect=fake_ssh), \
             mock.patch.object(br, "already_processed", return_value=False), \
             mock.patch.object(br, "v3_shadow_hook", return_value=None), \
             mock.patch.object(br, "rag_dispatch_gate", return_value=(True, {})), \
             mock.patch.object(br, "seed_project", return_value=True), \
             mock.patch.object(br, "wake_workers", return_value=True), \
             mock.patch.object(br, "prepare_run_manifest",
                               return_value=("k+ref", {"m": 1}, None)), \
             mock.patch.object(br, "prepare_run_context",
                               return_value=(None, "run-context conflict (write-once per run)")), \
             mock.patch.object(br.mx, "send",
                               side_effect=lambda *a, **k: sent.append(a) or {"event_id": "$e"}):
            br.process(d, timeout_min=1, dry=False)
        self.assertEqual(sent, [])   # 未派发


if __name__ == "__main__":
    unittest.main()
