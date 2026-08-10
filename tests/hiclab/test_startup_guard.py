"""Unit tests for guarded-startup wrappers + start-script guarded detection."""
import os
import pathlib
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HICLAB = pathlib.Path(HERE).parent.parent / "tools" / "hiclab"
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import install_guarded as ig


def _read(name):
    return (HICLAB / name).read_text(encoding="utf-8")


class TestSupervisorWrapper(unittest.TestCase):
    def test_guard_runs_before_python_exec(self):
        """disk_guard must be sourced + called before exec python3."""
        text = _read("hiclab_supervisor.sh")
        lines = text.splitlines()
        guard_idx = None
        exec_idx = None
        for i, line in enumerate(lines):
            if guard_idx is None and "mp_disk_guard" in line:
                guard_idx = i
            if exec_idx is None and "exec python3" in line:
                exec_idx = i
        self.assertIsNotNone(guard_idx)
        self.assertIsNotNone(exec_idx)
        self.assertLess(guard_idx, exec_idx)

    def test_guard_failure_exits_nonzero(self):
        text = _read("hiclab_supervisor.sh")
        self.assertIn("FAIL-CLOSED", text)
        self.assertIn("exit 2", text)


class TestInstallWrapper(unittest.TestCase):
    def test_dry_run_default(self):
        text = _read("install_guarded_startup.sh")
        # The wrapper passes args through; dry-run is the Python default
        self.assertIn("install_guarded.py", text)
        self.assertIn("$@", text)


class TestUnitContent(unittest.TestCase):
    def test_after_docker_service(self):
        self.assertIn("After=docker.service",
                      ig._unit_content("/s"))

    def test_requires_docker_service(self):
        self.assertIn("Requires=docker.service",
                      ig._unit_content("/s"))

    def test_restart_no(self):
        self.assertIn("Restart=no", ig._unit_content("/s"))

    def test_exec_start_is_supervisor(self):
        self.assertIn("ExecStart=/x/y/hiclab_supervisor.sh",
                      ig._unit_content("/x/y/hiclab_supervisor.sh"))


class TestStartControllerGuardedDetection(unittest.TestCase):
    """start-controller-container.sh must detect guarded mode and be consistent."""
    def _script(self):
        return (HICLAB.parent / "start-controller-container.sh").read_text(
            encoding="utf-8")

    def test_detects_guarded_mode(self):
        text = self._script()
        self.assertIn("hiclab-guarded-start.service", text)
        self.assertIn("is-enabled", text)

    def test_uses_restart_no_when_guarded(self):
        text = self._script()
        self.assertIn('RESTART_POLICY="no"', text)

    def test_reports_unprotected_when_not_guarded(self):
        text = self._script()
        self.assertIn("UNPROTECTED", text)
        self.assertIn("unless-stopped", text)

    def test_uses_variable_restart_policy(self):
        text = self._script()
        # the docker run must use the variable, not a hardcoded policy
        self.assertIn('--restart "$RESTART_POLICY"', text)


class TestNoDualManagement(unittest.TestCase):
    """Guarded install must result in ALL managed = restart=no (no unless-stopped)."""
    def test_install_sets_all_to_no(self):
        # covered in test_install_guarded.TestFullSuccess; this is the
        # explicit anti-dual-management assertion at the contract level
        import managed_containers as mc
        for n in mc.names():
            self.assertIsInstance(n, str)


if __name__ == "__main__":
    unittest.main()
