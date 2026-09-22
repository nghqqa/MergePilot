"""run 级版本清单(备忘九.2)单元测试(全 mock,不触真实 docker/MinIO)。

覆盖:派发前置 fail-closed / write-once(采纳/拒绝覆盖)/ 缺失项诚实标注 /
清单哈希确定性 / kickoff 引用清单摘要 / 配置摘要无秘密 / 恢复只读。
"""
from __future__ import annotations

import json
import re
import unittest
from unittest import mock

from .test_publish_semantics import _delivery, _load_bridge


def _base_kickoff():
    return "[kickoff] run-x - project elemiso-gh-pr2-abcd1234.\n\n=== SPEC ===\nreview it"


class BuildManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _build(self, **over):
        br = self.br
        d = _delivery()
        img = over.pop("img", "sha256:img")
        bsha = over.pop("bsha", "b" * 64)
        git = over.pop("git", "deadbee")
        model = over.pop("model", "agentteams-gateway/deepseek-chat")
        skh = over.pop("skh", {"diff_parse": "a" * 64})
        with mock.patch.object(br, "_worker_image_id", side_effect=lambda c: img), \
             mock.patch.object(br, "_bridge_source_sha", return_value=bsha), \
             mock.patch.object(br, "_git_commit", return_value=git), \
             mock.patch.object(br, "_worker_model_id", side_effect=lambda c, r: model), \
             mock.patch.object(br, "_skills_content_hashes", side_effect=lambda c: skh), \
             mock.patch.object(br, "_rag_service_state", return_value="unreachable"):
            return br.build_manifest(d, "run-x", "elemiso-gh-pr2-abcd1234",
                                     "gh-pr2-abcd1234-review-1", _base_kickoff(), 20)

    def test_required_fields_present(self):
        m = self._build()
        self.assertEqual(m["code"]["head_sha"], "65de83d6d061413ec98c1e79515f470313ef9806")
        self.assertEqual(m["prompt"]["kickoff_base_sha256"],
                         self.br._sha_text(_base_kickoff()))
        self.assertEqual(m["orchestrator"]["bridge_source_sha256"], "b" * 64)
        self.assertEqual(m["manifest_version"], 1)
        self.assertEqual(m["run_id"], "run-x")

    def test_missing_items_marked_not_fabricated(self):
        m = self._build()
        self.assertIsNone(m["model"]["generation_params"])   # agentloop 不外露,不伪造
        # repo 语料回退存在时快照可得;不可读路径时进 missing(见 test_rag_gate.py)
        self.assertNotIn("model.primary", m["missing"])           # 已从 worker 取得
        self.assertNotIn("skills.content_sha256", m["missing"])
        self.assertEqual(m["model"]["primary"], "agentteams-gateway/deepseek-chat")
        self.assertEqual(m["skills"]["content_sha256"]["diff_parse"], "a" * 64)

    def test_unavailable_worker_sources_fall_back_to_missing(self):
        """worker 探查失败时:不伪造,退回 null + missing(清单仍可派发)。"""
        m = self._build(model=None, skh=None)
        self.assertIsNone(m["model"]["primary"])
        self.assertIsNone(m["skills"]["content_sha256"])
        self.assertIn("model.primary", m["missing"])
        self.assertIn("skills.content_sha256", m["missing"])

    def test_unavailable_worker_images_marked_missing(self):
        m = self._build(img=None)
        self.assertIsNone(m["workers"]["images"]["reviewer"])
        self.assertIn("workers.images", m["missing"])

    def test_manifest_sha_deterministic_and_sensitive(self):
        m1, m2 = self._build(), self._build()
        # created_at 每次构建会变 → 哈希不同是正确的(绑定该次构建);
        # 但同一对象重复计算必须稳定。
        self.assertEqual(self.br.manifest_sha(m1), self.br.manifest_sha(m1))
        m2["code"]["head_sha"] = "f" * 40
        self.assertNotEqual(self.br.manifest_sha(m1), self.br.manifest_sha(m2))

    def test_config_summary_has_no_secret_shaped_values(self):
        m = self._build()
        blob = json.dumps(m["config"]["canonical"])
        for bad in ("token", "secret", "password", "pat_", "api_key"):
            self.assertNotIn(bad, blob.lower())


class WriteOnceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_absent_writes_once(self):
        br = self.br
        m = {"a": 1}
        with mock.patch.object(br, "read_run_manifest", return_value=None), \
             mock.patch.object(br, "minio_put", return_value=True) as put:
            r = br.write_run_manifest("proj", m)
        self.assertTrue(r["ok"])
        self.assertNotIn("adopted", r)
        put.assert_called_once()

    def test_existing_same_hash_adopted_without_write(self):
        br = self.br
        m = {"a": 1}
        with mock.patch.object(br, "read_run_manifest", return_value={"a": 1}), \
             mock.patch.object(br, "minio_put", return_value=True) as put:
            r = br.write_run_manifest("proj", m)
        self.assertTrue(r["ok"])
        self.assertTrue(r["adopted"])
        put.assert_not_called()  # write-once:不覆盖既有 run 的清单

    def test_existing_different_hash_refused(self):
        br = self.br
        with mock.patch.object(br, "read_run_manifest", return_value={"a": 2}), \
             mock.patch.object(br, "minio_put", return_value=True) as put:
            r = br.write_run_manifest("proj", {"a": 1})
        self.assertFalse(r["ok"])
        self.assertIn("conflict", r["reason"])
        put.assert_not_called()


class PrepareDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_success_appends_manifest_reference(self):
        br = self.br
        d = _delivery()
        with mock.patch.object(br, "build_manifest", return_value={"m": 1}), \
             mock.patch.object(br, "write_run_manifest", return_value={"ok": True}):
            kickoff, err = br.prepare_run_manifest(
                d, "run-x", "proj", "task", _base_kickoff(), 20)
        self.assertIsNone(err)
        self.assertTrue(kickoff.startswith(_base_kickoff()))
        ref = re.search(r"run-manifest: sha256 ([0-9a-f]{64})", kickoff)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.group(1), br.manifest_sha({"m": 1}))

    def test_failure_returns_error_and_no_kickoff(self):
        br = self.br
        d = _delivery()
        with mock.patch.object(br, "build_manifest", return_value={"m": 1}), \
             mock.patch.object(br, "write_run_manifest",
                               return_value={"ok": False, "reason": "mc pipe failed"}):
            kickoff, err = br.prepare_run_manifest(
                d, "run-x", "proj", "task", _base_kickoff(), 20)
        self.assertIsNone(kickoff)
        self.assertEqual(err, "mc pipe failed")


class ProcessDispatchGateTests(unittest.TestCase):
    """场景补充:清单持久化失败 → 不派发(fail-closed),投递标 ERROR。"""
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _process(self, prep_ret, prep_err):
        br = self.br
        d = _delivery()
        sent, fins = [], []

        def fake_ssh(q):
            fins.append(q)
            return "UPDATE 1"

        with mock.patch.object(br, "ssh_psql", side_effect=fake_ssh), \
             mock.patch.object(br, "already_processed", return_value=False), \
             mock.patch.object(br, "rag_dispatch_gate", return_value=(True, {})), \
             mock.patch.object(br, "seed_project", return_value=True), \
             mock.patch.object(br, "wake_workers", return_value=True), \
             mock.patch.object(br, "prepare_run_manifest",
                               return_value=(prep_ret, prep_err)), \
             mock.patch.object(br.mx, "send",
                               side_effect=lambda *a, **k: sent.append(a) or {"event_id": "$e"}), \
             mock.patch.object(br, "watch_run", return_value=("completed", "r")), \
             mock.patch.object(br, "project_result", return_value="res"), \
             mock.patch.object(br, "gate_record", return_value=""), \
             mock.patch.object(br, "publish_with_retry",
                               return_value={"ok": True, "check_run_id": 1}):
            br.process(d, timeout_min=1, dry=False)
        return sent, fins

    def test_manifest_success_then_kickoff_sent_with_reference(self):
        sent, fins = self._process(_base_kickoff() + "\nrun-manifest: sha256 %s\n" % ("a" * 64), None)
        self.assertEqual(len(sent), 1)
        self.assertIn("run-manifest: sha256", sent[0][2])  # (room, mention, kickoff)
        self.assertTrue(any("status='PROCESSED'" in q for q in fins))

    def test_manifest_failure_blocks_dispatch(self):
        sent, fins = self._process(None, "minio down")
        self.assertEqual(sent, [])  # 没有任何派发
        err = [q for q in fins if "status='ERROR'" in q]
        self.assertEqual(len(err), 1)
        self.assertIn("run-manifest failed", err[0])


class ResumeManifestReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_resume_missing_manifest_does_not_rewrite(self):
        br = self.br
        d = _delivery()
        d["claim_id"] = d["delivery_id"][:14] + "-bridge-cafebabe"
        d["_cid"] = d["claim_id"]
        put_calls = []
        with mock.patch.object(br, "ssh_psql", side_effect=lambda q: "UPDATE 1"), \
             mock.patch.object(br, "read_run_manifest", return_value=None), \
             mock.patch.object(br, "read_receipt", return_value={"check_run_id": 5}), \
             mock.patch.object(br, "minio_put",
                               side_effect=lambda *a, **k: put_calls.append(a) or True):
            br.resume(d, 1, lambda *a: None)
        self.assertEqual(put_calls, [])  # 恢复路径只读,不重建清单


if __name__ == "__main__":
    unittest.main(verbosity=2)
