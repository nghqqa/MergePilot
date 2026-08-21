"""M8-GH-4B3-W3B-R2 §5/§8: runtime ownership/reparse boundaries and
the Reporter health interface. All fake/injected; production branches
executed for real (the fs adapter simulates reparse attributes that
Windows cannot create here)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import e2e_runtime_specs as rs         # noqa: E402
import e2e_foundation as e2f           # noqa: E402
import reporter_health as rh           # noqa: E402


def _rc():
    from tests.gh_app.test_e2e_lifecycle_r2 import _rc as base
    return base()


class _ReparseFS(rs.RealFilesystem):
    """Simulates a reparse point at ONE target path (the production
    branch executes for real against this adapter)."""

    def __init__(self, reparse_path):
        self._reparse = os.fspath(reparse_path)

    def is_reparse(self, path):
        return os.fspath(path) == self._reparse

    @staticmethod
    def realpath(path):
        return os.path.realpath(path)

    @staticmethod
    def exists(path):
        return os.path.exists(path)

    @staticmethod
    def read_bytes(path):
        with open(path, "rb") as fh:
            return fh.read()

    @staticmethod
    def write_bytes_atomic(path, data):
        tmp = path + ".mp-tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    @staticmethod
    def unlink(path):
        os.unlink(path)

    @staticmethod
    def chmod0600(path):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


class TestOwnershipReparse(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_create_refuses_reparse_target(self):
        fs = _ReparseFS(self.dir / "gateway_e2e.env")
        journal = {}
        with self.assertRaises(rs.RuntimeSpecError) as ctx:
            rs.create_runtime_files(
                rs.validate_runtime_configs(_rc()),
                directory=self.dir, journal=journal, fs=fs)
        self.assertEqual(ctx.exception.code,
                         "RUNTIME_FILE_REPARSE_REFUSED")
        # no half file for the refused target
        self.assertFalse((self.dir / "gateway_e2e.env").exists())

    def test_remove_refuses_reparse_target(self):
        journal = {}
        rs.create_runtime_files(
            rs.validate_runtime_configs(_rc()),
            directory=self.dir, journal=journal)
        target = Path(journal["policy-gateway"]["file"])
        content_before = target.read_bytes()
        fs = _ReparseFS(target)
        with self.assertRaises(rs.RuntimeSpecError) as ctx:
            rs.remove_runtime_files(directory=self.dir,
                                    journal=journal, fs=fs)
        self.assertEqual(ctx.exception.code,
                         "RUNTIME_REMOVE_REPARSE_REFUSED")
        # external target untouched (same bytes)
        self.assertEqual(target.read_bytes(), content_before)

    def test_foreign_file_never_removed(self):
        journal = {}
        rs.create_runtime_files(
            rs.validate_runtime_configs(_rc()),
            directory=self.dir, journal=journal)
        foreign = self.dir / "foreign.env"
        foreign.write_bytes(b"FOREIGN=1\n")
        removed = rs.remove_runtime_files(directory=self.dir,
                                          journal=journal)
        self.assertNotIn("foreign", removed)
        self.assertTrue(foreign.exists())
        self.assertEqual(removed, sorted(
            rs.SERVICE_RUNTIME_SPECS))

    def test_foreign_journal_entry_skipped_not_absorbed(self):
        journal = {
            "intruder": {"file": str(self.dir / "foreign.env"),
                         "ownership": "foreign-operator"}}
        foreign = self.dir / "foreign.env"
        foreign.write_bytes(b"FOREIGN=1\n")
        removed = rs.remove_runtime_files(directory=self.dir,
                                          journal=journal)
        self.assertEqual(removed, [])
        self.assertTrue(foreign.exists())
        self.assertIn("intruder", journal)   # not absorbed

    def test_owned_remove_idempotent(self):
        journal = {}
        rs.create_runtime_files(
            rs.validate_runtime_configs(_rc()),
            directory=self.dir, journal=journal)
        first = rs.remove_runtime_files(directory=self.dir,
                                        journal=journal)
        self.assertEqual(len(first), 6)
        second = rs.remove_runtime_files(directory=self.dir,
                                         journal=journal)
        self.assertEqual(second, [])
        self.assertEqual(journal, {})

    def test_persist_failure_primary_error_and_cleanup_diags(self):
        journal = {}
        diags = []

        def persist(_j):
            if not diags:
                diags.append(1)
            else:
                raise OSError("disk full")

        # make the reverse-delete ALSO fail for one file to prove
        # cleanup errors never replace the primary persistence error
        real_unlink = rs.RealFilesystem.unlink

        class FailingUnlinkFS(rs.RealFilesystem):
            def unlink(self, path):
                if os.fspath(path).endswith("gh_proxy_b.env"):
                    raise OSError("locked")
                real_unlink(path)

        with self.assertRaises(rs.RuntimeSpecError) as ctx:
            rs.create_runtime_files(
                rs.validate_runtime_configs(_rc()),
                directory=self.dir, journal=journal,
                persist_callback=persist, fs=FailingUnlinkFS())
        self.assertEqual(ctx.exception.code,
                         "RUNTIME_JOURNAL_PERSIST_FAILED")
        self.assertTrue(any("RUNTIME_CLEANUP_FAILED" in d
                            for d in getattr(ctx.exception,
                                             "diagnostics", [])))
        # files after the failing persist were never created
        self.assertFalse((self.dir / "gh_proxy_r.env").exists())
        # gh_proxy_b remains on disk BY DESIGN: its cleanup failed and
        # the diagnostic records it (cleanup failure never silently
        # pretends success)
        self.assertEqual(journal, {})

    def test_partial_create_failure_reverse_delete_no_later_files(self):
        real_write = rs.RealFilesystem.write_bytes_atomic

        class HalfwayFS(rs.RealFilesystem):
            count = 0

            def write_bytes_atomic(self, path, data):
                HalfwayFS.count += 1
                if HalfwayFS.count == 3:
                    raise OSError("io error")
                real_write(path, data)

        journal = {}
        with self.assertRaises(rs.RuntimeSpecError):
            rs.create_runtime_files(
                rs.validate_runtime_configs(_rc()),
                directory=self.dir, journal=journal,
                fs=HalfwayFS())
        # no half files; later services never created
        leftovers = list(self.dir.glob("*.env"))
        self.assertEqual(leftovers, [])
        self.assertEqual(journal, {})

    def test_directory_drift_refused(self):
        class DriftFS(rs.RealFilesystem):
            calls = 0

            def realpath(self, path):
                DriftFS.calls += 1
                if DriftFS.calls > 1:
                    return str(Path(str(path)).parent / "elsewhere")
                return super().realpath(path)

        journal = {}
        with self.assertRaises(rs.RuntimeSpecError) as ctx:
            rs.create_runtime_files(
                rs.validate_runtime_configs(_rc()),
                directory=self.dir, journal=journal, fs=DriftFS())
        self.assertEqual(ctx.exception.code,
                         "RUNTIME_DIR_DRIFT_REFUSED")


def _reporter_env():
    return {
        "GITHUB_PUBLISHER_DSN":
            "postgresql://u:synthetic@postgres/db?connect_timeout=5",
        "GITHUB_API_BASE": "https://api.github.com",
        "GITHUB_APP_ID": "1", "GITHUB_INSTALLATION_ID": "1",
        "GITHUB_REPOSITORY_ID": "1",
        "GITHUB_PRIVATE_KEY_PATH":
            "/run/secrets/github-app-private-key.pem",
        "GH_REPORTER_POLL_SECONDS": "5",
        "GH_REPORTER_LEASE_SECONDS": "120",
        "GH_REPORTER_MAX_ATTEMPTS": "8",
        "HTTPS_PROXY": e2f.E2E_REPORTER_PROXY_R,
    }


class TestReporterHealth(unittest.TestCase):

    def test_ready_false_checks_zero_external_ops(self):
        (pem_reader, jwt_signer, token_exchanger, checks_caller,
         network_transport, counts) = rh.make_zero_op_adapters()
        result = rh.reporter_health_status(
            _reporter_env(),
            expected_proxy=e2f.E2E_REPORTER_PROXY_R,
            db_ready=lambda: True,
            pem_reader=pem_reader,
            jwt_signer=jwt_signer,
            token_exchanger=token_exchanger,
            checks_caller=checks_caller,
            network_transport=network_transport)
        self.assertTrue(result["reporter_ready"])
        self.assertFalse(result["real_checks_verified"])
        # every forbidden adapter stayed at ZERO calls
        self.assertEqual(counts, {"pem_read": 0, "jwt_sign": 0,
                                  "token_exchange": 0, "checks_call": 0,
                                  "network": 0})

    def test_wrong_proxy_not_ready(self):
        env = _reporter_env()
        env["HTTPS_PROXY"] = "http://wrong:1"
        result = rh.reporter_health_status(
            env, expected_proxy=e2f.E2E_REPORTER_PROXY_R)
        self.assertFalse(result["reporter_ready"])
        self.assertFalse(result["checks"]["proxy_r_exact"])

    def test_wrong_pem_target_not_ready(self):
        env = _reporter_env()
        env["GITHUB_PRIVATE_KEY_PATH"] = "/elsewhere/key.pem"
        result = rh.reporter_health_status(
            env, expected_proxy=e2f.E2E_REPORTER_PROXY_R)
        self.assertFalse(result["checks"]["pem_target_spec"])

    def test_missing_key_not_ready(self):
        env = _reporter_env()
        env.pop("GH_REPORTER_MAX_ATTEMPTS")
        result = rh.reporter_health_status(
            env, expected_proxy=e2f.E2E_REPORTER_PROXY_R)
        self.assertFalse(result["reporter_ready"])
        self.assertEqual(result["checks"]["schema_missing"],
                         ["GH_REPORTER_MAX_ATTEMPTS"])

    def test_extra_key_not_ready(self):
        env = _reporter_env()
        env["EXTRA"] = "x"
        result = rh.reporter_health_status(
            env, expected_proxy=e2f.E2E_REPORTER_PROXY_R)
        self.assertEqual(result["checks"]["schema_unknown"], ["EXTRA"])

    def test_db_not_ready_blocks(self):
        result = rh.reporter_health_status(
            _reporter_env(), expected_proxy=e2f.E2E_REPORTER_PROXY_R,
            db_ready=lambda: False)
        self.assertFalse(result["reporter_ready"])
        self.assertFalse(result["checks"]["db_ready"])

    def test_real_checks_verified_always_false(self):
        for db in (None, lambda: True):
            result = rh.reporter_health_status(
                _reporter_env(),
                expected_proxy=e2f.E2E_REPORTER_PROXY_R, db_ready=db)
            self.assertIs(result["real_checks_verified"], False)


if __name__ == "__main__":
    unittest.main()
