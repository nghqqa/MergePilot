"""桥的 RAG 快照绑定与派发门单元测试(全 mock,不触真实服务)。

对应 RAG-AUDIT 缺口修复:RAG-4(派发前快照绑定)/RAG-6(required 降级显式化)。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from .test_publish_semantics import _delivery, _load_bridge

CORPUS = os.path.normpath(os.path.join(os.path.dirname(__file__),
                                       "..", "..", "tools", "rag", "corpus",
                                       "org-security-knowledge-v1.json"))


class RagSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()
        cls.assertTrue(os.path.isfile(CORPUS), "repo 语料事实源必须存在")

    def test_snapshot_info_from_repo_corpus(self):
        br = self.br
        with mock.patch.dict(os.environ, {br.RAG_CORPUS_ENV: CORPUS}):
            info = br._rag_snapshot_info()
        self.assertIsNotNone(info)
        self.assertEqual(info["chunks"], 12)
        self.assertEqual(len(info["snapshot_id"]), 64)
        self.assertEqual(info["data_mode"], "SYNTHETIC")
        self.assertEqual(info["corpus_path"] if "corpus_path" in info else info["source"],
                         "org-security-knowledge-v1.json")

    def test_snapshot_missing_when_no_corpus(self):
        br = self.br
        with mock.patch.dict(os.environ, {br.RAG_CORPUS_ENV: "Z:/no/such.json"}):
            self.assertIsNone(br._rag_snapshot_info())

    def test_manifest_binds_rag_snapshot(self):
        br = self.br
        d = _delivery()
        with mock.patch.dict(os.environ, {br.RAG_CORPUS_ENV: CORPUS}), \
             mock.patch.object(br, "_rag_service_state", return_value="unreachable"), \
             mock.patch.object(br, "_worker_image_id", return_value="sha256:x"), \
             mock.patch.object(br, "_bridge_source_sha", return_value="b" * 64), \
             mock.patch.object(br, "_git_commit", return_value="deadbee"), \
             mock.patch.object(br, "_worker_model_id", return_value="m1"), \
             mock.patch.object(br, "_skills_content_hashes", return_value={"s": "a" * 64}):
            m = br.build_manifest(d, "run-x", "proj", "task", "kick", 20)
        self.assertEqual(m["rag"]["snapshot_id"], br._rag_snapshot_info()["snapshot_id"])
        self.assertEqual(m["rag"]["service_state_at_dispatch"], "unreachable")
        self.assertEqual(m["rag"]["policy"], "optional")
        self.assertNotIn("rag.snapshot_id", m["missing"])

    def test_manifest_marks_missing_snapshot(self):
        br = self.br
        d = _delivery()
        with mock.patch.dict(os.environ, {br.RAG_CORPUS_ENV: "Z:/no.json"}), \
             mock.patch.object(br, "_rag_service_state", return_value="unreachable"), \
             mock.patch.object(br, "_worker_image_id", return_value="sha256:x"), \
             mock.patch.object(br, "_bridge_source_sha", return_value="b" * 64), \
             mock.patch.object(br, "_git_commit", return_value="deadbee"), \
             mock.patch.object(br, "_worker_model_id", return_value="m1"), \
             mock.patch.object(br, "_skills_content_hashes", return_value={"s": "a" * 64}):
            m = br.build_manifest(d, "run-x", "proj", "task", "kick", 20)
        self.assertIsNone(m["rag"]["snapshot_id"])
        self.assertIn("rag.snapshot_id", m["missing"])


class RagGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_optional_policy_always_passes(self):
        br = self.br
        with mock.patch.dict(os.environ, {br.RAG_CORPUS_ENV: "Z:/no.json",
                                          br.RAG_REQUIRED_ENV: ""}), \
             mock.patch.object(br, "_rag_service_state", return_value="unreachable"):
            ok, detail = br.rag_dispatch_gate()
        self.assertTrue(ok)  # advisory:降级可见但不阻断
        self.assertEqual(detail["policy"], "optional")

    def test_required_refuses_without_snapshot(self):
        br = self.br
        with mock.patch.dict(os.environ, {br.RAG_CORPUS_ENV: "Z:/no.json",
                                          br.RAG_REQUIRED_ENV: "1"}), \
             mock.patch.object(br, "_rag_service_state", return_value="reachable"):
            ok, detail = br.rag_dispatch_gate()
        self.assertFalse(ok)
        self.assertIn("RAG_REQUIRED_UNAVAILABLE", detail)

    def test_required_refuses_when_service_down(self):
        br = self.br
        with mock.patch.dict(os.environ, {br.RAG_CORPUS_ENV: CORPUS,
                                          br.RAG_REQUIRED_ENV: "1"}), \
             mock.patch.object(br, "_rag_service_state", return_value="unreachable"):
            ok, detail = br.rag_dispatch_gate()
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)

    def test_required_passes_when_snapshot_and_service_ok(self):
        br = self.br
        with mock.patch.dict(os.environ, {br.RAG_CORPUS_ENV: CORPUS,
                                          br.RAG_REQUIRED_ENV: "1"}), \
             mock.patch.object(br, "_rag_service_state", return_value="reachable"):
            ok, detail = br.rag_dispatch_gate()
        self.assertTrue(ok)
        self.assertEqual(detail["policy"], "required")


class RagGateDispatchTests(unittest.TestCase):
    """RAG_REQUIRED 下服务不可用 → 不派发,投递 ERROR(fail-closed)。"""
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_required_unavailable_blocks_dispatch(self):
        br = self.br
        d = _delivery()
        sent, fins = [], []

        def fake_ssh(q):
            fins.append(q)
            return "UPDATE 1"

        with mock.patch.dict(os.environ, {br.RAG_REQUIRED_ENV: "1"}), \
             mock.patch.object(br, "rag_dispatch_gate",
                               return_value=(False, "RAG_REQUIRED_UNAVAILABLE: test")), \
             mock.patch.object(br, "ssh_psql", side_effect=fake_ssh), \
             mock.patch.object(br, "already_processed", return_value=False), \
             mock.patch.object(br, "seed_project", return_value=True) as seed, \
             mock.patch.object(br, "wake_workers", return_value=True), \
             mock.patch.object(br, "prepare_run_manifest", return_value=("k+ref", None)), \
             mock.patch.object(br.mx, "send",
                               side_effect=lambda *a, **k: sent.append(a) or {"event_id": "$e"}):
            br.process(d, timeout_min=1, dry=False)
        self.assertEqual(sent, [])          # 无派发
        seed.assert_not_called()            # 连项目都不播种(尽早失败)
        err = [q for q in fins if "status='ERROR'" in q]
        self.assertEqual(len(err), 1)
        self.assertIn("RAG_REQUIRED_UNAVAILABLE", err[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
