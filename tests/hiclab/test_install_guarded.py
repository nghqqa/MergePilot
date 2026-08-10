"""Unit tests for install_guarded.py (full snapshot + complete rollback)."""
import hashlib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import install_guarded as ig
import managed_containers as mc

UNIT = "/etc/systemd/system/hiclab-guarded-start.service"
MF = "/etc/hiclab/managed-containers"
SUP = "/opt/hiclab/hiclab_supervisor.sh"


class MockDocker:
    def __init__(self):
        self.exists_set = set(mc.names())
        self.policies = {n: "unless-stopped" for n in mc.names()}
        self.set_fail = set()       # fail forward update to "no" only
        self.restore_fail = set()   # fail any restore (for status-2 tests)
        self.restore_raise = set()  # raise during restore (exception-safety)
        self.set_calls = []
        self.audit_overrides = {}

    def exists(self, name):
        return name in self.exists_set

    def get_restart_policy(self, name):
        pol = self.policies.get(name, "no")
        if pol == "no" and name in self.audit_overrides:
            return self.audit_overrides[name]
        return pol

    def set_restart(self, name, policy):
        self.set_calls.append((name, policy))
        if name in self.restore_raise:
            raise RuntimeError("restore boom: %s" % name)
        if name in self.restore_fail:
            return False
        if name in self.set_fail and policy == "no":
            return False
        self.policies[name] = policy
        return True


class MockFs:
    def __init__(self):
        self.files = {}       # path -> (content_bytes, mode)
        self.write_fail = set()
        self.write_raise = set()  # paths where atomic_write raises
        self.removed = []

    def read_with_mode(self, path):
        if path in self.files:
            content, mode = self.files[path]
            return (True, content, mode)
        return (False, None, None)

    def atomic_write(self, path, content_bytes, mode=None):
        if path in self.write_raise:
            raise OSError("simulated write failure: %s" % path)
        if path in self.write_fail:
            return False
        self.files[path] = (content_bytes, mode if mode is not None else 0o644)
        return True

    def remove(self, path):
        self.removed.append(path)
        self.files.pop(path, None)
        return True


class MockSystemd:
    def __init__(self, file_checker=None):
        # file_checker() -> bool: is the unit file present? When absent,
        # get_enabled_state returns 'not-found' (faithful to real systemd).
        self._file_checker = file_checker
        self.daemon_reload_ok = True
        self.daemon_reload_fail_first = 0
        self.enable_ok = True
        self.is_enabled_ok = True
        self.disable_ok = True
        self._enabled = "not-found"
        self.calls = []

    def get_enabled_state(self, unit):
        if self._file_checker is not None and not self._file_checker():
            return "not-found"
        return self._enabled

    def daemon_reload(self):
        self.calls.append("daemon_reload")
        if self.daemon_reload_fail_first > 0:
            self.daemon_reload_fail_first -= 1
            return False
        return self.daemon_reload_ok

    def enable(self, unit):
        self.calls.append("enable")
        if self.enable_ok:
            self._enabled = "enabled"
        return self.enable_ok

    def disable(self, unit):
        self.calls.append("disable")
        if self.disable_ok:
            self._enabled = "disabled"
        return self.disable_ok

    def is_enabled(self, unit):
        self.calls.append("is_enabled")
        return self.is_enabled_ok


def _run(docker=None, fs=None, systemd=None):
    docker = docker or MockDocker()
    fs = fs or MockFs()
    if systemd is None:
        systemd = MockSystemd()
    if systemd._file_checker is None:
        systemd._file_checker = lambda: UNIT in fs.files
    status, detail, rb = ig.apply(docker, fs, systemd, UNIT, MF, SUP)
    return status, detail, rb, docker, fs, systemd


class TestMissingContainer(unittest.TestCase):
    def test_missing_fails_no_change(self):
        docker = MockDocker()
        docker.exists_set.discard("github-mcp")
        status, detail, rb, d, fs, _s = _run(docker=docker)
        self.assertEqual(status, 1)
        self.assertIn("github-mcp", detail)
        self.assertEqual(fs.files, {})
        self.assertNotIn(("github-mcp", "no"), d.set_calls)


class TestDockerUpdateRollback(unittest.TestCase):
    def test_mid_update_failure_restores_policies(self):
        docker = MockDocker()
        docker.set_fail.add("policy-gw")
        status, detail, rb, d, fs, _s = _run(docker=docker)
        self.assertEqual(status, 1)
        self.assertIn("policy-gw", detail)
        prior = mc.names()[:mc.names().index("policy-gw")]
        for n in prior:
            self.assertEqual(d.policies[n], "unless-stopped")
        self.assertEqual(fs.files, {})


class TestFileWriteRollback(unittest.TestCase):
    def test_managed_file_write_fail_rolls_back(self):
        docker = MockDocker()
        fs = MockFs()
        fs.write_fail.add(MF)
        status, detail, rb, d, _fs, _s = _run(docker=docker, fs=fs)
        self.assertEqual(status, 1)
        for n in mc.names():
            self.assertEqual(d.policies[n], "unless-stopped")

    def test_unit_write_fail_rolls_back(self):
        docker = MockDocker()
        fs = MockFs()
        fs.write_fail.add(UNIT)
        status, detail, rb, d, _fs, _s = _run(docker=docker, fs=fs)
        self.assertEqual(status, 1)
        for n in mc.names():
            self.assertEqual(d.policies[n], "unless-stopped")
        self.assertNotIn(MF, fs.files)


class TestSystemctlRollback(unittest.TestCase):
    def _check(self, substr, systemd):
        status, detail, rb, d, fs, _s = _run(systemd=systemd)
        self.assertEqual(status, 1)
        self.assertIn(substr, detail)
        for n in mc.names():
            self.assertEqual(d.policies[n], "unless-stopped")
        self.assertEqual(fs.files, {})

    def test_daemon_reload_fail(self):
        sd = MockSystemd()
        sd.daemon_reload_fail_first = 1  # apply fails; rollback succeeds
        self._check("daemon-reload failed", sd)

    def test_enable_fail(self):
        sd = MockSystemd()
        sd.enable_ok = False
        self._check("enable failed", sd)

    def test_is_enabled_fail(self):
        sd = MockSystemd()
        sd.is_enabled_ok = False
        self._check("is-enabled verify failed", sd)


class TestPostInstallAudit(unittest.TestCase):
    def test_restart_not_no_rolls_back(self):
        docker = MockDocker()
        docker.audit_overrides = {"hiclaw-manager": "always"}
        status, detail, rb, d, fs, _s = _run(docker=docker)
        self.assertEqual(status, 1)
        self.assertIn("restart!=no", detail)
        for n in mc.names():
            self.assertEqual(d.policies[n], "unless-stopped")


class TestSnapshotRestoreExistingFiles(unittest.TestCase):
    """Original managed file + unit existed with specific bytes -> restored byte-exact."""

    def setUp(self):
        self.orig_managed = b"ORIGINAL-MANAGED\nold-line\n"
        self.orig_unit = b"[Unit]\nOriginalUnit\nold\n"
        self.orig_mode = 0o600

    def test_rollback_restores_managed_file_bytes_and_mode(self):
        docker = MockDocker()
        docker.set_fail.add("policy-gw")  # force rollback mid-apply
        fs = MockFs()
        fs.files[MF] = (self.orig_managed, self.orig_mode)
        fs.files[UNIT] = (self.orig_unit, self.orig_mode)
        _status, _detail, _rb, _d, fs2, _s = _run(docker=docker, fs=fs)
        # managed file restored to exact original bytes + mode
        ex, content, mode = fs2.read_with_mode(MF)
        self.assertTrue(ex)
        self.assertEqual(content, self.orig_managed)
        self.assertEqual(mode, self.orig_mode)
        ex, content, mode = fs2.read_with_mode(UNIT)
        self.assertTrue(ex)
        self.assertEqual(content, self.orig_unit)
        self.assertEqual(mode, self.orig_mode)


class TestSnapshotRestoreAbsentFiles(unittest.TestCase):
    """Original files did not exist -> new files removed on rollback."""

    def test_rollback_deletes_new_files_when_original_absent(self):
        docker = MockDocker()
        sd = MockSystemd()
        sd.is_enabled_ok = False  # fails AFTER files written + daemon-reload ok
        _status, _detail, _rb, _d, fs, _s = _run(docker=docker, systemd=sd)
        # both new files were written then removed on rollback
        self.assertNotIn(MF, fs.files)
        self.assertNotIn(UNIT, fs.files)


class TestEnabledStateRestore(unittest.TestCase):
    def test_originally_enabled_restored(self):
        docker = MockDocker()
        docker.set_fail.add("policy-gw")
        fs = MockFs()
        fs.files[UNIT] = (b"[Unit]\nOriginal\n", 0o644)  # pre-existing unit
        sd = MockSystemd()
        sd._enabled = "enabled"
        _status, _detail, _rb, _d, _fs, sd2 = _run(docker=docker, fs=fs,
                                                   systemd=sd)
        self.assertEqual(sd2.get_enabled_state(ig.UNIT_NAME), "enabled")

    def test_originally_disabled_restored(self):
        docker = MockDocker()
        fs = MockFs()
        fs.files[UNIT] = (b"[Unit]\nOriginal\n", 0o644)
        sd = MockSystemd()
        sd._enabled = "disabled"
        sd.daemon_reload_fail_first = 1  # apply daemon-reload fails, rollback ok
        _status, _detail, _rb, _d, _fs, sd2 = _run(docker=docker, fs=fs,
                                                   systemd=sd)
        self.assertNotEqual(sd2.get_enabled_state(ig.UNIT_NAME), "enabled")


class TestRollbackSecondaryFailure(unittest.TestCase):
    def test_rollback_failure_returns_status_2(self):
        docker2 = MockDocker()
        docker2.set_fail.add("policy-gw")       # apply fails at update
        docker2.restore_fail.add("policy-gw")   # restore also fails -> status 2
        status, detail, rb, _d, _fs, _s = _run(docker=docker2)
        self.assertEqual(status, 2)
        self.assertTrue(len(rb) > 0, "rollback failures must be visible: %s" % rb)
        self.assertIn("ROLLBACK ALSO FAILED", detail)

    def test_daemon_reload_fail_during_rollback_visible(self):
        docker = MockDocker()
        docker.set_fail.add("policy-gw")  # apply fails before daemon-reload
        sd = MockSystemd()
        sd.daemon_reload_fail_first = 1   # rollback's daemon-reload fails
        status, detail, rb, _d, _fs, _s = _run(docker=docker, systemd=sd)
        self.assertEqual(status, 2)
        self.assertTrue(any("daemon-reload" in f for f in rb), rb)


class TestAtomicUnitInstall(unittest.TestCase):
    def test_unit_content_correct(self):
        status, _detail, _rb, _d, fs, _s = _run()
        self.assertEqual(status, 0)
        ex, content, _mode = fs.read_with_mode(UNIT)
        self.assertTrue(ex)
        c = content.decode("utf-8")
        self.assertIn("[Unit]", c)
        self.assertIn("After=docker.service", c)
        self.assertIn("Requires=docker.service", c)
        self.assertIn("Restart=no", c)
        self.assertIn("ExecStart=" + SUP, c)

    def test_managed_file_has_all_names_no_hiclaw_data(self):
        status, _detail, _rb, _d, fs, _s = _run()
        self.assertEqual(status, 0)
        ex, content, _mode = fs.read_with_mode(MF)
        c = content.decode("utf-8")
        for n in mc.names():
            self.assertIn(n, c)
        self.assertNotIn("hiclaw-data", c)


class TestFullSuccess(unittest.TestCase):
    def test_all_restart_no_and_systemd_calls(self):
        status, detail, rb, d, _fs, sd = _run()
        self.assertEqual(status, 0)
        self.assertEqual(rb, [])
        self.assertEqual(sd.calls[:3],
                         ["daemon_reload", "enable", "is_enabled"])
        for n in mc.names():
            self.assertEqual(d.policies[n], "no")
        self.assertNotIn("hiclaw-data", d.policies)


class TestDryRun(unittest.TestCase):
    def test_dry_run_no_changes(self):
        docker = MockDocker()
        out = ig.dry_run(docker_ops=docker, unit_path=UNIT, managed_file=MF,
                         supervisor_path=SUP)
        self.assertIn("DRY-RUN", out)
        self.assertEqual(docker.set_calls, [])


class TestUnitContent(unittest.TestCase):
    def test_after_requires_docker(self):
        c = ig._unit_content(SUP)
        self.assertIn("After=docker.service", c)
        self.assertIn("Requires=docker.service", c)

    def test_restart_no(self):
        self.assertIn("Restart=no", ig._unit_content(SUP))


class TestExceptionSafety(unittest.TestCase):
    """Snapshot-then-exception must always route to rollback (no uncaught exit)."""

    def test_fs_write_exception_enters_rollback(self):
        docker = MockDocker()
        fs = MockFs()
        fs.write_raise.add(MF)  # managed file write raises (not returns False)
        status, detail, rb, d, _fs, _s = _run(docker=docker, fs=fs)
        # exception caught -> rollback -> policies restored
        self.assertEqual(status, 1)
        for n in mc.names():
            self.assertEqual(d.policies[n], "unless-stopped")
        self.assertIn("exception during apply", detail)
        self.assertIn("rolled back cleanly", detail)

    def test_rollback_step_exception_returns_status_2(self):
        docker = MockDocker()
        docker.set_fail.add("policy-gw")      # apply fails at update
        docker.restore_raise.add("policy-gw")  # rollback restore raises
        status, detail, rb, _d, _fs, _s = _run(docker=docker)
        self.assertEqual(status, 2)
        self.assertTrue(any("policy-gw" in f for f in rb), rb)

    def test_unit_write_exception_enters_rollback(self):
        docker = MockDocker()
        fs = MockFs()
        fs.write_raise.add(UNIT)
        status, detail, rb, d, _fs, _s = _run(docker=docker, fs=fs)
        self.assertEqual(status, 1)
        for n in mc.names():
            self.assertEqual(d.policies[n], "unless-stopped")


class TestUnsupportedState(unittest.TestCase):
    """masked/static/indirect unit states must be refused before any change."""

    def _state_refused(self, state):
        docker = MockDocker()
        fs = MockFs()
        fs.files[UNIT] = (b"[Unit]\n", 0o644)
        sd = MockSystemd()
        sd._enabled = state
        status, detail, rb, d, _fs, _s = _run(docker=docker, fs=fs, systemd=sd)
        self.assertEqual(status, 1)
        self.assertIn("unsupported", detail)
        self.assertIn(state, detail)
        for n in mc.names():
            self.assertEqual(d.policies[n], "unless-stopped")

    def test_masked_refused(self):
        self._state_refused("masked")

    def test_static_refused(self):
        self._state_refused("static")

    def test_indirect_refused(self):
        self._state_refused("indirect")


class TestModeVerification(unittest.TestCase):
    def test_managed_file_mode_restored(self):
        docker = MockDocker()
        docker.set_fail.add("policy-gw")  # rollback
        fs = MockFs()
        fs.files[MF] = (b"original-managed\n", 0o600)
        fs.files[UNIT] = (b"[Unit]\n", 0o644)
        _status, _detail, _rb, _d, fs2, _s = _run(docker=docker, fs=fs)
        _ex, _content, mode = fs2.read_with_mode(MF)
        self.assertEqual(mode, 0o600)

    def test_unit_file_mode_restored(self):
        docker = MockDocker()
        docker.set_fail.add("policy-gw")
        fs = MockFs()
        fs.files[MF] = (b"m\n", 0o644)
        fs.files[UNIT] = (b"[Unit]\n", 0o600)
        _status, _detail, _rb, _d, fs2, _s = _run(docker=docker, fs=fs)
        _ex, _content, mode = fs2.read_with_mode(UNIT)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
