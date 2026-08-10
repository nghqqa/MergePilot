"""Unit tests for disk_guard.py (host Python; no WSL/Docker)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import disk_guard

VHDX = "E:\\WSL\\Ubuntu-22.04\\ext4.vhdx"


def make_guest_runner(avail_kib):
    """Simulate `df -P -k` output with the given Available (KiB)."""
    def runner(argv):
        return (
            "Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
            "/dev/sda1       56564044   44530380  %d      78%% /\n" % avail_kib
        )
    return runner


def make_host_runner(value_or_exc):
    """Simulate powershell.exe returning free bytes, or raising."""
    def runner(argv):
        if isinstance(value_or_exc, BaseException):
            raise value_or_exc
        return "%d\n" % value_or_exc
    return runner


class TestGuestProbe(unittest.TestCase):
    def test_parses_available_kib(self):
        runner = make_guest_runner(104857600)
        kib = disk_guard.probe_guest_free_kib("/var/lib/docker", runner)
        self.assertEqual(kib, 104857600)

    def test_returns_none_on_failure(self):
        def runner(argv):
            raise RuntimeError("df failed")
        self.assertIsNone(
            disk_guard.probe_guest_free_kib("/var/lib/docker", runner))

    def test_returns_none_on_malformed(self):
        def runner(argv):
            return "garbage\n"
        self.assertIsNone(
            disk_guard.probe_guest_free_kib("/var/lib/docker", runner))


class TestHostProbe(unittest.TestCase):
    def test_parses_bytes(self):
        runner = make_host_runner(161061273600)
        b = disk_guard.probe_host_free_bytes(VHDX, runner=runner)
        self.assertEqual(b, 161061273600)

    def test_returns_none_on_exception(self):
        runner = make_host_runner(RuntimeError("powershell missing"))
        self.assertIsNone(disk_guard.probe_host_free_bytes(VHDX, runner=runner))

    def test_returns_none_on_non_numeric(self):
        def bad_runner(argv):
            return "not-a-number\n"
        self.assertIsNone(
            disk_guard.probe_host_free_bytes(VHDX, runner=bad_runner))

    def test_returns_none_on_empty_vhdx(self):
        self.assertIsNone(
            disk_guard.probe_host_free_bytes("", runner=make_host_runner(999)))


class TestCheck(unittest.TestCase):
    def test_both_ok(self):
        ok, d = disk_guard.check(
            min_guest_gib=100, min_host_gib=150, vhdx_path=VHDX,
            guest_runner=make_guest_runner(200 * 1024 * 1024),
            host_runner=make_host_runner(300 * 1024 ** 3),
        )
        self.assertTrue(ok, d)
        self.assertEqual(d["guest_free_gib"], 200)
        self.assertEqual(d["host_free_gib"], 300)

    def test_guest_below_threshold(self):
        ok, d = disk_guard.check(
            min_guest_gib=100, min_host_gib=150, vhdx_path=VHDX,
            guest_runner=make_guest_runner(50 * 1024 * 1024),
            host_runner=make_host_runner(300 * 1024 ** 3),
        )
        self.assertFalse(ok)
        self.assertIn("guest free 50GiB", d["error"])

    def test_host_below_threshold(self):
        ok, d = disk_guard.check(
            min_guest_gib=100, min_host_gib=150, vhdx_path=VHDX,
            guest_runner=make_guest_runner(200 * 1024 * 1024),
            host_runner=make_host_runner(100 * 1024 ** 3),
        )
        self.assertFalse(ok)
        self.assertIn("host free 100GiB", d["error"])

    def test_host_query_fails_fail_closed(self):
        ok, d = disk_guard.check(
            min_guest_gib=100, min_host_gib=150, vhdx_path=VHDX,
            guest_runner=make_guest_runner(200 * 1024 * 1024),
            host_runner=make_host_runner(RuntimeError("ps failed")),
        )
        self.assertFalse(ok)
        self.assertIn("host probe failed", d["error"])

    def test_vhdx_unset_fail_closed(self):
        ok, d = disk_guard.check(
            min_guest_gib=100, min_host_gib=150, vhdx_path="",
            guest_runner=make_guest_runner(200 * 1024 * 1024),
            host_runner=make_host_runner(300 * 1024 ** 3),
        )
        self.assertFalse(ok)
        self.assertIn("host probe failed", d["error"])

    def test_defaults_100_150(self):
        self.assertEqual(disk_guard.DEFAULT_MIN_GUEST_GIB, 100)
        self.assertEqual(disk_guard.DEFAULT_MIN_HOST_GIB, 150)

    def test_env_override(self):
        os.environ["MP_DISK_MIN_GUEST_GIB"] = "50"
        os.environ["MP_DISK_MIN_HOST_GIB"] = "80"
        try:
            ok, d = disk_guard.check(
                vhdx_path=VHDX,
                guest_runner=make_guest_runner(60 * 1024 * 1024),
                host_runner=make_host_runner(90 * 1024 ** 3),
            )
            self.assertTrue(ok, d)
        finally:
            del os.environ["MP_DISK_MIN_GUEST_GIB"]
            del os.environ["MP_DISK_MIN_HOST_GIB"]


class TestGuardBeforeDocker(unittest.TestCase):
    """Static check: startup scripts source disk_guard before first docker op."""

    def _check_script(self, rel):
        path = os.path.join(HERE, "..", "..", rel)
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        guard_idx = None
        docker_idx = None
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            # The guard executes via the mp_disk_guard call (sourced from
            # disk_guard.sh); detect the call, which is what runs the check.
            if guard_idx is None and "mp_disk_guard" in line:
                guard_idx = i
            if docker_idx is None and (
                stripped.startswith("docker build")
                or stripped.startswith("docker run")
                or stripped.startswith("docker rm")
                or stripped.startswith("docker restart")
            ):
                docker_idx = i
        self.assertIsNotNone(guard_idx, "disk_guard source not in %s" % rel)
        self.assertIsNotNone(docker_idx, "first docker op not in %s" % rel)
        self.assertLess(guard_idx, docker_idx,
                        "guard must precede first docker op in %s" % rel)

    def test_controller_script(self):
        self._check_script(os.path.join("tools", "start-controller-container.sh"))

    def test_candidate_script(self):
        self._check_script(os.path.join("tools", "start-m5-0-candidate.sh"))


if __name__ == "__main__":
    unittest.main()
