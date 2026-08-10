"""Unit tests for storage_opt_probe.py (disposable-container probe model)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import storage_opt_probe as sop


class TestProbeWithDisposableContainer(unittest.TestCase):
    def test_probe_success_enables(self):
        def runner(argv):
            return (0, "", "")  # rc=0 -> supported
        self.assertTrue(sop.probe_with_disposable_container(runner=runner))

    def test_probe_failure_disables(self):
        def runner(argv):
            return (125, "", "unsupported storage-opt")  # rc!=0 -> unsupported
        self.assertFalse(sop.probe_with_disposable_container(runner=runner))

    def test_probe_exception_returns_none(self):
        def runner(argv):
            raise RuntimeError("docker unavailable")
        self.assertIsNone(sop.probe_with_disposable_container(runner=runner))

    def test_probe_uses_rm_network_none_label(self):
        seen = []

        def runner(argv):
            seen.append(argv)
            return (0, "", "")
        sop.probe_with_disposable_container(runner=runner, image="img")
        self.assertTrue(any("--rm" in a for a in seen), seen)
        self.assertTrue(any("--network" in a and "none" in a[i + 1]
                            for a, i in ((s, s.index("--network")) for s in seen
                                         if "--network" in s)), seen)
        self.assertTrue(any("storageopt-probe" in " ".join(a) for a in seen), seen)
        self.assertTrue(any("--storage-opt" in a for a in seen), seen)


class TestDetect(unittest.TestCase):
    def test_default_unsupported_when_disabled(self):
        r = sop.detect(enable_real_probe=False)
        self.assertFalse(r["supported"])
        self.assertIn("default unsupported", r["reason"])

    def test_probe_error_unsupported(self):
        def runner(argv):
            raise RuntimeError("no docker")
        r = sop.detect(runner=runner)
        self.assertFalse(r["supported"])
        self.assertFalse(r["probed"])
        self.assertIn("fail-safe", r["reason"])

    def test_probe_success_supported(self):
        def runner(argv):
            return (0, "", "")
        r = sop.detect(runner=runner)
        self.assertTrue(r["supported"])
        self.assertTrue(r["probed"])

    def test_probe_failure_unsupported(self):
        def runner(argv):
            return (1, "", "")
        r = sop.detect(runner=runner)
        self.assertFalse(r["supported"])

    def test_ext4_not_statically_supported(self):
        """No static ext4 path exists -- ext4 alone never enables support."""
        def runner(argv):
            raise RuntimeError("never runs for static detection")
        r = sop.detect(runner=runner)
        # Even if backing fs were ext4, without a successful probe -> unsupported
        self.assertFalse(r["supported"])

    def test_no_static_backing_fs_attribute(self):
        """The module must NOT have a static backing-fs -> supported path."""
        # detect() result has no 'backing_fs' or 'driver' keys (those were removed)
        r = sop.detect(enable_real_probe=False)
        self.assertNotIn("backing_fs", r)
        self.assertNotIn("driver", r)
        self.assertNotIn("root_fs", r)


class TestCleanupResidue(unittest.TestCase):
    def test_only_matches_probe_label(self):
        calls = []
        containers = ["mp-storageopt-probe-abc", "hiclaw-worker-fixer"]

        def runner(argv):
            calls.append(argv)
            if "--format" in argv:
                return (0, "\n".join(containers) + "\n", "")
            return (0, "", "")
        removed = sop.cleanup_probe_residue(runner=runner)
        self.assertIn("mp-storageopt-probe-abc", removed)
        self.assertNotIn("hiclaw-worker-fixer", removed)

    def test_runner_failure_returns_empty(self):
        def runner(argv):
            raise RuntimeError("no docker")
        self.assertEqual(sop.cleanup_probe_residue(runner=runner), [])


if __name__ == "__main__":
    unittest.main()
