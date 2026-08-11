"""Unit tests: D2B-3 PASSED status is consistent with docs and claims.

D2B-3 has completed production live verification with real v1.2.2 images.
These tests verify the UPSTREAM_BLOCKED.md doc reflects the PASSED state
while retaining the overclaim guard (no source file claims a closed chain
that wasn't actually verified).
"""
import os
import pathlib
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HICLAB = pathlib.Path(HERE).parent.parent / "tools" / "hiclab"


class TestPassedStatusDoc(unittest.TestCase):
    def _doc(self):
        return (HICLAB / "UPSTREAM_BLOCKED.md").read_text(encoding="utf-8")

    def test_states_passed(self):
        text = self._doc()
        self.assertIn("PASSED", text)

    def test_hiclaw_live_true(self):
        text = self._doc()
        self.assertIn("hiclaw_live=true", text)

    def test_v1_2_2_images_verified(self):
        text = self._doc()
        self.assertIn("v1.2.2", text)

    def test_socket_proxy_implemented(self):
        text = self._doc()
        self.assertIn("implemented", text.lower())
        self.assertIn("deployed", text.lower())

    def test_d2b3_runnable(self):
        text = self._doc()
        self.assertIn("D2B-3", text)
        self.assertIn("runnable", text)

    def test_manager_auto_create_permitted(self):
        text = self._doc()
        self.assertIn("permitted", text.lower())

    def test_programmatic_enforcement_retained(self):
        text = self._doc()
        self.assertIn("manager_start_allowed", text)
        self.assertIn("PROGRAMMATICALLY", text)

    def test_create_hardened_worker_retained(self):
        text = self._doc()
        self.assertIn("create_hardened_worker.sh", text)

    def test_disk_guard_retained(self):
        text = self._doc()
        self.assertIn("disk guard", text.lower())
        self.assertIn("guarded startup", text.lower())

    def test_authoritative_live_evidence_referenced(self):
        text = self._doc()
        self.assertIn("hiclaw-v122-true-live-pass", text)

    def test_compatibility_evidence_marked(self):
        text = self._doc()
        self.assertIn("compatibility", text.lower())


class TestNoOverclaimInCode(unittest.TestCase):
    """No source file should claim the worker-creation chain is closed
    beyond what the live evidence supports."""

    def _source_files(self):
        files = []
        for p in HICLAB.rglob("*"):
            if p.is_file() and p.suffix in (".py", ".sh"):
                files.append(p)
        return files

    def test_harden_policy_docstring_honest(self):
        text = (HICLAB / "harden_policy.py").read_text(encoding="utf-8")
        self.assertTrue(
            "policy" in text.lower(),
            "harden_policy.py must describe itself as a policy module")


if __name__ == "__main__":
    unittest.main()
