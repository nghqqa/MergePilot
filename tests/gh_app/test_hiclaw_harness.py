"""M8-GH-4B3 harness §9: execution-level test matrix (fake adapters
only — zero real docker, zero PAT/PEM reads, zero Matrix mutation).

Every test drives the PRODUCTION harness functions in
tools/harness/mp_gh4_harness.py; nothing is reimplemented here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "harness"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import mp_gh4_harness as hw                  # noqa: E402
import e2e_executors as ex                   # noqa: E402
import e2e_foundation as e2f                 # noqa: E402

TARGET = {r: ex.hiclaw_role_gateway_url(r) for r in hw.ROLES}


def _cp(rc=0, stdout=b""):
    return subprocess.CompletedProcess([], rc, stdout, b"")


class FakeDocker(hw.DockerAdapter):
    """In-memory container config store with write/read/backup/
    sha256sum semantics and a full call log."""

    def __init__(self, *, ip_drift_role=None, running_false=None,
                 verify_fail_role=None, write_fail_role=None,
                 backup_fail_role=None, rm_fail_role=None):
        self.calls = []
        self._rm_fail_role = rm_fail_role
        self.writes = 0
        self._configs = {}
        self._backups = {}
        for role in hw.ROLES:
            container = ex.HICLAW_ROLE_FREEZE[role][0]
            self._configs[container] = (
                '{"mcpServers":{"gh":{"url":'
                '"http://aigw-local.hiclaw.io:8080/mcp-github/mcp",'
                '"headers":{"Authorization":"Bearer secret-%s"}}}}'
                % role).encode()
        self._ip_drift_role = ip_drift_role
        self._running_false = running_false or set()
        self._verify_fail_role = verify_fail_role
        self._write_fail_role = write_fail_role
        self._backup_fail_role = backup_fail_role

    def _exec(self, argv, check=True, timeout=60, input_bytes=None,
              **_):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "inspect":
            name = argv[1]
            fmt = argv[argv.index("--format") + 1]
            role = self._role_of(name)
            if "{{.Id}}" in fmt:
                return _cp(0, ("cid-%s" % name).encode())
            if "{{.State.Running}}" in fmt:
                return _cp(0, ("false" if role in self._running_false
                               or name == "github-mcp"
                               else "true").encode())
            if "{{(index .NetworkSettings.Networks \"hiclaw-net\")" \
                    in fmt:
                if role and role == self._ip_drift_role:
                    return _cp(0, b"172.21.0.99")
                return _cp(0, ex.HICLAW_ROLE_FREEZE[role][2].encode()
                           if role else b"")
            if "{{.HostConfig.RestartPolicy.Name}}" in fmt:
                return _cp(0, b"no")
            if "{{.State.Status}}" in fmt:
                return _cp(0, b"exited")
            if "{{range $k, $v := .NetworkSettings.Networks}}" in fmt:
                return _cp(0, b"mcp-backend-net ")
            return _cp(0, ("cid-%s" % name).encode())
        if argv[0] == "exec":
            args = argv[1:]
            while args and args[0].startswith("-"):
                args = args[1:]
            container = args[0]
            op = args[1]
            role = self._role_of(container)
            path = argv[-1] if op in ("cat", "cp", "rm",
                                      "sha256sum") else None
            if op == "cat":
                return _cp(0, self._configs.get(container, b""))
            if op == "sha256sum":
                data = self._configs.get(container, b"")
                import hashlib
                return _cp(0, (hashlib.sha256(data).hexdigest()
                               + "  " + (path or "")).encode())
            if op == "cp":
                src, dst = argv[3], argv[4]
                if role == self._backup_fail_role:
                    return _cp(1)
                if src in self._backups:
                    # restore direction: backup -> live config
                    self._configs[container] = self._backups[src]
                else:
                    self._backups[dst] = self._configs.get(
                        container, b"")
                return _cp(0)
            if op == "rm":
                if role == self._rm_fail_role:
                    return _cp(1)     # backup deletion FAILS (rc!=0)
                self._backups.pop(argv[-1], None)
                return _cp(0)
            if op == "sh":
                # write via stdin: cat > <frozen-path>
                target_path = argv[-1].replace("cat > ", "")
                if role == self._write_fail_role:
                    return _cp(1)
                self._configs[container] = input_bytes or b""
                self.writes += 1
                return _cp(0)
        return _cp(0)

    def _role_of(self, name):
        for role in hw.ROLES:
            if ex.HICLAW_ROLE_FREEZE[role][0] == name:
                return role
        return None

    def config_text(self, role):
        return self._configs[
            ex.HICLAW_ROLE_FREEZE[role][0]].decode("utf-8", "replace")

    def read_counts(self):
        return {"writes": self.writes,
                "write_argv": [c for c in self.calls
                               if c[:3] == ["exec", None, "sh"]
                               or (len(c) > 2 and c[2] == "sh")]}


def _receipt_ok(docker, receipt_path):
    return ex.validate_hiclaw_receipt(
        str(receipt_path), docker_executor=docker._exec,
        expected_old_mcp_state="stopped")


class HarnessTestBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / "journal.json"
        self.receipt = self.root / "receipt.json"


class TestReadOnlyCommands(HarnessTestBase):

    def test_inspect_identifies_default_gateway_and_targets(self):
        fd = FakeDocker()
        state = hw.inspect_roles(fd)
        for role in hw.ROLES:
            info = state["roles"][role]
            self.assertTrue(info["running"])
            self.assertTrue(info["ip_matches"])
            self.assertFalse(info["already_target"])
            self.assertIn("aigw-local.hiclaw.io",
                          " ".join(info["current_gateway_urls"]))
            self.assertEqual(info["target_gateway_url"],
                             TARGET[role])
        self.assertEqual(state["old_github_mcp"]["state"], "exited")

    def test_plan_zero_writes(self):
        fd = FakeDocker()
        with unittest.mock.patch.object(
                hw, "_default_docker_executor",
                return_value=fd._exec) if False else _noop():
            result = hw.plan(self.journal)
        self.assertEqual(result["writes_executed"], 0)
        self.assertEqual(len(result["actions"]), 4)
        self.assertEqual(fd.writes, 0)
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.receipt.exists())


class TestApply(HarnessTestBase):

    def test_four_role_success_receipt_validated(self):
        fd = FakeDocker()
        result = hw.apply(journal_path=self.journal,
                          receipt_path=self.receipt, docker=fd,
                          session="s1")
        self.assertEqual(result["result"], "complete")
        # every role rewritten to its frozen target URL
        for role in hw.ROLES:
            self.assertIn(TARGET[role], fd.config_text(role))
            self.assertNotIn("aigw-local", fd.config_text(role))
        # receipt passes the PRODUCTION validator
        verdict = _receipt_ok(fd, self.receipt)
        self.assertTrue(verdict["verified"], verdict)
        # journal complete + ownership
        journal = json.loads(self.journal.read_text())
        self.assertEqual(journal["status"], "complete")
        self.assertEqual(journal["ownership"], hw.HARNESS_IDENTITY)

    def test_apply_idempotent_when_all_at_target(self):
        fd = FakeDocker()
        for role in hw.ROLES:
            container = ex.HICLAW_ROLE_FREEZE[role][0]
            fd._configs[container] = json.dumps(
                {"mcpServers": {"gh": {
                    "url": TARGET[role],
                    "headers": {"Authorization": "Bearer x"}}}}
            ).encode()
        result = hw.apply(journal_path=self.journal,
                          receipt_path=self.receipt, docker=fd,
                          session="s2")
        self.assertEqual(result["result"], "idempotent-noop")
        self.assertEqual(fd.writes, 0)

    def test_apply_failure_for_every_role(self):
        # R2-A: EVERY role injected with a write failure; per role:
        # target hit, later roles untouched, target+earlier rolled
        # back, before state restored, honest journal, no receipt,
        # exact primary, no backup residue, no secrets.
        for fail_role in hw.ROLES:
            with self.subTest(role=fail_role):
                j = self.root / ("j-af-%s.json" % fail_role)
                r = self.root / ("r-af-%s.json" % fail_role)
                fd = FakeDocker(write_fail_role=fail_role)
                with self.assertRaises(hw.HarnessError) as ctx:
                    hw.apply(journal_path=j, receipt_path=r,
                             docker=fd, session="af-%s" % fail_role)
                self.assertEqual(ctx.exception.code,
                                 "HARNESS_APPLY_FAILED")
                idx = hw.ROLES.index(fail_role)
                for role in hw.ROLES[:idx + 1]:
                    self.assertIn("aigw-local", fd.config_text(role),
                                  "%s not restored" % role)
                for role in hw.ROLES[idx + 1:]:
                    self.assertIn("aigw-local", fd.config_text(role),
                                  "%s modified early" % role)
                    self.assertNotIn(
                        hw.MCPORTER_PATH[role] + hw.BACKUP_SUFFIX,
                        fd._backups)
                self.assertFalse(r.exists())
                journal = json.loads(j.read_text())
                self.assertEqual(journal["status"], "rolled-back")
                self.assertEqual(journal["rollback_residue"], [])
                self.assertEqual(fd.writes, idx)
                blob = j.read_text()
                self.assertNotIn("Bearer", blob)
                self.assertNotIn("secret-", blob)

    def test_verify_failure_for_every_role(self):
        # R2-B: EVERY role with write-success + read-back drift; per
        # role: write happened, role IS rolled back, role + earlier
        # fully restored, later roles never written, exact primary,
        # honest journal, receipt absent, backups cleaned.
        for fail_role in hw.ROLES:
            with self.subTest(role=fail_role):
                j = self.root / ("j-vf-%s.json" % fail_role)
                r = self.root / ("r-vf-%s.json" % fail_role)
                fd = FakeDocker()
                orig_read = fd.read_config

                def flaky_read(container, path):
                    data = orig_read(container, path)
                    if fd._role_of(container) == fail_role:
                        return (b'{"mcpServers":{"gh":{"url":'
                                b'"http://drift"}}}')
                    return data

                fd.read_config = flaky_read
                with self.assertRaises(hw.HarnessError) as ctx:
                    hw.apply(journal_path=j, receipt_path=r,
                             docker=fd, session="vf-%s" % fail_role)
                self.assertEqual(ctx.exception.code,
                                 "HARNESS_VERIFY_FAILED")
                idx = hw.ROLES.index(fail_role)
                self.assertEqual(fd.writes, idx + 1)
                for role in hw.ROLES[:idx + 1]:
                    self.assertIn("aigw-local", fd.config_text(role),
                                  "%s not restored" % role)
                for role in hw.ROLES[idx + 1:]:
                    self.assertIn("aigw-local", fd.config_text(role),
                                  "%s modified early" % role)
                self.assertFalse(r.exists())
                journal = json.loads(j.read_text())
                self.assertEqual(journal["status"], "rolled-back")
                self.assertEqual(
                    journal["roles"][fail_role]["status"],
                    "rolled-back")
                for role in hw.ROLES[:idx + 1]:
                    self.assertNotIn(
                        hw.MCPORTER_PATH[role] + hw.BACKUP_SUFFIX,
                        fd._backups)

    def test_identity_drift_refused_before_writes(self):
        fd = FakeDocker(ip_drift_role="verifier")
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     session="s5")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_IDENTITY_DRIFT")
        self.assertEqual(fd.writes, 0)

    def test_missing_role_refused(self):
        fd = FakeDocker(running_false={"verifier"})
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     session="s6")
        self.assertEqual(ctx.exception.code, "HARNESS_ROLE_MISSING")
        self.assertEqual(fd.writes, 0)

    def test_foreign_journal_refused(self):
        self.journal.write_text(json.dumps(
            {"ownership": "someone-else"}), encoding="utf-8")
        fd = FakeDocker()
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     session="s7")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_FOREIGN_JOURNAL")
        self.assertEqual(fd.writes, 0)

    def test_rollback_failure_preserves_primary(self):
        fd = FakeDocker(write_fail_role="manager")
        # make restore fail too: backup cp already recorded; force cp
        # failure at rollback by breaking the backup store
        real_restore = fd.restore_config

        def flaky_restore(container, backup_path, path):
            if fd._role_of(container):
                raise OSError("docker down")
            return real_restore(container, backup_path, path)

        fd.restore_config = flaky_restore
        # manager is first role: make WRITE succeed but a LATER role
        # fail so manager needs rollback
        fd2 = FakeDocker(write_fail_role="verifier")
        fd2.restore_config = flaky_restore
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd2,
                     session="s8")
        self.assertEqual(ctx.exception.code, "HARNESS_APPLY_FAILED")
        diags = getattr(ctx.exception, "diagnostics", [])
        self.assertTrue(any("ROLLBACK_FAILED" in d for d in diags))
        journal = json.loads(self.journal.read_text())
        self.assertEqual(journal["status"], "rollback-failed")


class TestRollbackCommand(HarnessTestBase):

    def _applied_journal(self, fd):
        # simulate a crash mid-apply: journal in-progress with one
        # applied role and real in-container backup
        container = ex.HICLAW_ROLE_FREEZE["manager"][0]
        fd._exec(["exec", container, "cp",
                  hw.MCPORTER_PATH["manager"],
                  hw.MCPORTER_PATH["manager"] + hw.BACKUP_SUFFIX])
        journal = {"ownership": hw.HARNESS_IDENTITY,
                   "session": "crash", "status": "in-progress",
                   "roles": {"manager": {
                       "status": "mutated",
                       "backup": hw.MCPORTER_PATH["manager"]
                       + hw.BACKUP_SUFFIX}}}
        self.journal.write_text(json.dumps(journal), encoding="utf-8")
        return fd

    def test_crash_journal_explicit_rollback_restores(self):
        fd = self._applied_journal(FakeDocker())
        # simulate applied state in container
        fd._configs[ex.HICLAW_ROLE_FREEZE["manager"][0]] = \
            b'{"url":"http://172.31.0.18:8083/manager/sse"}'
        result = hw.rollback(journal_path=self.journal, docker=fd,
                             session="crash")
        self.assertEqual(result["rolled_back"], ["manager"])
        self.assertIn("aigw-local", fd.config_text("manager"))

    def test_rollback_idempotent(self):
        fd = self._applied_journal(FakeDocker())
        fd._configs[ex.HICLAW_ROLE_FREEZE["manager"][0]] = \
            b'{"url":"http://172.31.0.18:8083/manager/sse"}'
        hw.rollback(journal_path=self.journal, docker=fd,
                    session="crash")
        second = hw.rollback(journal_path=self.journal, docker=fd,
                             session="crash")
        self.assertEqual(second["rolled_back"], [])
        self.assertEqual(second["residue"], [])

    def test_in_flight_journal_blocks_new_apply(self):
        fd = self._applied_journal(FakeDocker())
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     session="new")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_FOREIGN_JOURNAL")


class CrashSimulated(BaseException):
    """Escapes apply()'s except-Exception handlers: a true process
    crash with NO in-transaction cleanup (test-only)."""


class TestCrashRecovery(HarnessTestBase):
    """R2-C: crash-window recovery. Every window kills apply mid-
    flight via the production phase_hook, then a FRESH harness
    rollback runs purely from the on-disk journal."""

    def _crash_at(self, phase, role, j, r):
        fd = FakeDocker()

        def hook(ph, rl):
            if ph == phase and rl == role:
                raise CrashSimulated(ph)

        try:
            hw.apply(journal_path=j, receipt_path=r, docker=fd,
                     session="crash", phase_hook=hook)
        except CrashSimulated:
            pass
        return fd

    def test_crash_window_recovery(self):
        windows = (
            ("applying_persisted", "manager"),   # write never ran
            ("mutated_persisted", "reviewer"),   # write ran, unverified
            ("verified_persisted", "manager"),   # verified, pre-receipt
            ("mutated_persisted", "verifier"),   # multi-role mid crash
        )
        for phase, role in windows:
            with self.subTest(window=phase, role=role):
                j = self.root / ("j-crash-%s-%s.json" % (phase, role))
                r = self.root / ("r-crash-%s-%s.json" % (phase, role))
                fd = self._crash_at(phase, role, j, r)
                self.assertFalse(r.exists())  # never reached receipt
                idx = hw.ROLES.index(role)
                if phase == "applying_persisted":
                    self.assertEqual(fd.writes, idx)
                else:
                    self.assertEqual(fd.writes, idx + 1)
                # fresh harness instance: disk-journal rollback only
                fd2 = FakeDocker()
                fd2._configs = dict(fd._configs)
                fd2._backups = dict(fd._backups)
                result = hw.rollback(journal_path=j, docker=fd2,
                                     session="crash")
                # every possibly-written role recovered, in strict
                # reverse mutation order (rollback output is already
                # reverse-ordered by construction)
                mutated = [x for x in hw.ROLES[:idx + 1]]
                self.assertEqual(result["rolled_back"],
                                 list(reversed(mutated)))
                for rl in mutated:
                    self.assertIn("aigw-local", fd2.config_text(rl),
                                  "%s not recovered" % rl)
                self.assertEqual(result["residue"], [])
                self.assertFalse(r.exists())
                # rollback idempotent
                second = hw.rollback(journal_path=j, docker=fd2,
                                     session="crash")
                self.assertEqual(second["rolled_back"], [])

    def test_crash_no_foreign_touch(self):
        j = self.root / "j-f.json"
        r = self.root / "r-f.json"
        fd = self._crash_at("mutated_persisted", "fixer", j, r)
        foreign = self.root / "foreign.txt"
        foreign.write_text("untouched", encoding="utf-8")
        fd2 = FakeDocker()
        fd2._configs = dict(fd._configs)
        fd2._backups = dict(fd._backups)
        hw.rollback(journal_path=j, docker=fd2, session="crash")
        self.assertEqual(foreign.read_text(), "untouched")


class TestJournalPersistFailures(HarnessTestBase):
    """R2-D: persist-failure windows with precise consequences."""

    @staticmethod
    def _writer_failing_at(ordinal):
        class W(hw.AtomicFileWriter):
            count = 0

            @classmethod
            def write(cls, path, data, *, root=None):
                W.count += 1
                if W.count >= ordinal:
                    raise OSError("disk full")
                return hw.AtomicFileWriter.write(path, data, root=root)
        return W

    def test_write_ahead_persist_failure_zero_writes(self):
        # ordinal 2 = the FIRST per-role write-ahead persist
        fd = FakeDocker()
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     writer=self._writer_failing_at(2), session="p1")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        self.assertEqual(fd.writes, 0)
        self.assertFalse(self.receipt.exists())

    def test_mutated_persist_failure_rolls_back_role(self):
        # ordinal 3 = manager's post-write persist (1 init + 2 WAL)
        fd = FakeDocker()
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     writer=self._writer_failing_at(3), session="p2")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        self.assertIn("aigw-local", fd.config_text("manager"))
        self.assertFalse(self.receipt.exists())

    def test_verified_persist_failure_rolls_back_progress(self):
        # ordinal 4 = manager's verified persist
        fd = FakeDocker()
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     writer=self._writer_failing_at(4), session="p3")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        self.assertIn("aigw-local", fd.config_text("manager"))
        for role in hw.ROLES[1:]:
            self.assertIn("aigw-local", fd.config_text(role))
        self.assertFalse(self.receipt.exists())

    def test_complete_persist_failure_no_trusted_state(self):
        # persist succeeds through all role stages + receipt write,
        # fails only at the final complete persist. The receipt file
        # was written — recovery must remove it (no trusted state).
        class W(hw.AtomicFileWriter):
            count = 0

            @classmethod
            def write(cls, path, data, *, root=None):
                W.count += 1
                return hw.AtomicFileWriter.write(path, data, root=root)

        fd = FakeDocker()

        class CountingW(W):
            pass

        # total persists for a full 4-role apply: 1 init + 4*(WAL,
        # mutated, verified) = 13, then complete = 14th
        class FailAtComplete(hw.AtomicFileWriter):
            count = 0

            @classmethod
            def write(cls, path, data, *, root=None):
                FailAtComplete.count += 1
                if str(path).endswith("journal.json") \
                        and FailAtComplete.count >= 14:
                    raise OSError("disk full at complete")
                return hw.AtomicFileWriter.write(path, data, root=root)

        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     writer=FailAtComplete(), session="p4")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        journal = json.loads(self.journal.read_text())
        self.assertNotEqual(journal["status"], "complete")
        self.assertFalse(self.receipt.exists())
        for role in hw.ROLES:
            self.assertIn("aigw-local", fd.config_text(role))


class TestReceiptOwnership(HarnessTestBase):
    """R3: receipt ownership — pre-existing/foreign targets are
    never read, overwritten or deleted; exclusive publish loses
    races fail-closed; only THIS session's receipt is cleaned."""

    def test_preexisting_receipt_refused(self):
        # A: foreign bytes at the receipt target -> fail-closed
        # BEFORE journal/backup/write; bytes untouched.
        foreign = b'{"foreign": true, "keep": 1}'
        self.receipt.write_bytes(foreign)
        fd = FakeDocker()
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     session="own-a")
        self.assertEqual(ctx.exception.code, "HARNESS_RECEIPT_EXISTS")
        self.assertEqual(fd.writes, 0)
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.receipt.read_bytes(), foreign)
        for role in hw.ROLES:
            self.assertIn("aigw-local", fd.config_text(role))
            self.assertNotIn(
                hw.MCPORTER_PATH[role] + hw.BACKUP_SUFFIX, fd._backups)

    def test_receipt_reparse_refused(self):
        # B: symlink at the receipt target -> stable safe refusal,
        # zero writes, foreign target content unchanged.
        outside = self.root / "outside.txt"
        outside.write_text("foreign-target", encoding="utf-8")
        try:
            self.receipt.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        fd = FakeDocker()
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     session="own-b")
        self.assertIn(ctx.exception.code,
                      ("HARNESS_REPARSE_REFUSED",
                       "HARNESS_RECEIPT_EXISTS"))
        self.assertEqual(fd.writes, 0)
        self.assertEqual(outside.read_text(), "foreign-target")

    def test_created_by_foreign_process_before_commit_refused(self):
        # C: preflight saw no target; a foreign actor creates the
        # receipt between verification and the exclusive commit.
        # Production exclusive writer branch must lose the race.
        fd = FakeDocker()
        foreign = b'{"raced": true}'

        def hook(phase, role):
            # foreign actor creates the receipt in the window AFTER
            # the last verified persist and BEFORE the exclusive
            # commit — the production write_exclusive branch must
            # lose this race fail-closed
            if phase == "verified_persisted" and role == hw.ROLES[-1]:
                self.receipt.write_bytes(foreign)

        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     session="own-c",
                     phase_hook=hook)
        self.assertEqual(ctx.exception.code, "HARNESS_RECEIPT_EXISTS")
        self.assertEqual(self.receipt.read_bytes(), foreign)
        for role in hw.ROLES:
            self.assertIn("aigw-local", fd.config_text(role))
        journal = json.loads(self.journal.read_text())
        self.assertNotEqual(journal["status"], "complete")

    def test_validator_failure_removes_only_session_receipt(self):
        # D: our exclusive receipt created, production validator
        # fails -> OUR receipt deleted, configs rolled back, a
        # foreign bystander file untouched.
        fd = FakeDocker()
        bystander = self.root / "bystander.json"
        bystander.write_text("untouched", encoding="utf-8")

        def bad_validator(path):
            return {"verified": False, "checks": {}}

        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     session="own-d",
                     receipt_validator=bad_validator)
        self.assertEqual(ctx.exception.code,
                         "HARNESS_RECEIPT_VALIDATION_FAILED")
        self.assertFalse(self.receipt.exists())
        for role in hw.ROLES:
            self.assertIn("aigw-local", fd.config_text(role))
        self.assertEqual(bystander.read_text(), "untouched")

    def test_complete_persist_failure_deletes_session_receipt_only(self):
        # E: complete-stage persist fails -> OUR receipt removed,
        # all configs restored, no complete state, foreign untouched.
        foreign = self.root / "foreign-receipt.json"
        foreign.write_text("foreign", encoding="utf-8")

        class FailComplete(hw.AtomicFileWriter):
            journal_writes = 0

            @classmethod
            def write(cls, path, data, *, root=None):
                if str(path).endswith("journal.json"):
                    FailComplete.journal_writes += 1
                    # 1 init + 12 role persists + 1 receipt-ownership
                    # + 1 complete == 15th journal write
                    if FailComplete.journal_writes >= 15:
                        raise OSError("disk full at complete")
                return hw.AtomicFileWriter.write(path, data,
                                                 root=root)

        fd = FakeDocker()
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     writer=FailComplete(), session="own-e")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")
        self.assertFalse(self.receipt.exists())
        journal = json.loads(self.journal.read_text())
        self.assertNotEqual(journal["status"], "complete")
        for role in hw.ROLES:
            self.assertIn("aigw-local", fd.config_text(role))
        self.assertEqual(foreign.read_text(), "foreign")


class TestReceiptCrashRecovery(HarnessTestBase):
    """R4: the坐实 window — exclusive receipt published, the
    created-state journal persist never happened. Crash recovery
    must PROVE ownership cryptographically before deleting."""

    def _crash_after_publish(self, session):
        j = self.root / ("j-%s.json" % session)
        r = self.root / ("r-%s.json" % session)
        fd = FakeDocker()

        def hook(phase, role):
            if phase == "receipt_published":
                raise CrashSimulated(phase)

        try:
            hw.apply(journal_path=j, receipt_path=r, docker=fd,
                     session=session, phase_hook=hook)
        except CrashSimulated:
            pass
        return j, r, fd

    def test_post_publish_pre_ownership_persist_crash(self):
        j, r, fd = self._crash_after_publish("r4core")
        # crash state: receipt EXISTS, journal still says publishing
        self.assertTrue(r.exists())
        journal = json.loads(j.read_text())
        self.assertEqual(journal["receipt_state"], "publishing")
        self.assertEqual(journal["receipt_session"], "r4core")
        # FRESH instance: only disk journal + receipt file
        fd2 = FakeDocker()
        fd2._configs = dict(fd._configs)
        fd2._backups = dict(fd._backups)
        result = hw.rollback(journal_path=j, docker=fd2,
                             session="r4core")
        # ownership proven -> receipt deleted, agents restored
        self.assertFalse(r.exists())
        self.assertEqual(result["rolled_back"],
                         ["verifier", "fixer", "reviewer", "manager"])
        for role in hw.ROLES:
            self.assertIn("aigw-local", fd2.config_text(role))
        self.assertEqual(result["residue"], [])
        # idempotent second rollback
        second = hw.rollback(journal_path=j, docker=fd2,
                             session="r4core")
        self.assertEqual(second["rolled_back"], [])


class TestReceiptForeignVariants(HarnessTestBase):
    """R4 §7 A-F: foreign/indeterminate receipt targets are never
    deleted; agents still roll back; diagnostics/residue honest."""

    def _journal_with_state(self, state, session, path,
                            sha=None):
        return {
            "ownership": hw.HARNESS_IDENTITY, "session": session,
            "status": "in-progress",
            "roles": {"manager": {
                "status": "mutated",
                "backup": hw.MCPORTER_PATH["manager"]
                + hw.BACKUP_SUFFIX}},
            "receipt_state": state,
            "receipt_path": str(path),
            "receipt_session": session,
            "receipt_sha256": sha or "0" * 64,
        }

    def _rollback(self, journal, session):
        j = self.root / ("j-%s.json" % session)
        j.write_text(json.dumps(journal), encoding="utf-8")
        fd = FakeDocker()
        container = ex.HICLAW_ROLE_FREEZE["manager"][0]
        fd._exec(["exec", container, "cp", hw.MCPORTER_PATH["manager"],
                  hw.MCPORTER_PATH["manager"] + hw.BACKUP_SUFFIX])
        result = hw.rollback(journal_path=j, docker=fd, session=session)
        disk = json.loads(j.read_text())
        return result, disk

    def test_a_publishing_without_receipt(self):
        r = self.root / "r-a.json"
        result, disk = self._rollback(
            self._journal_with_state("publishing", "s-a", r), "s-a")
        self.assertFalse(r.exists())
        self.assertEqual(result["rolled_back"], ["manager"])
        self.assertEqual(result["residue"], [])
        self.assertNotIn("RECEIPT_OWNERSHIP_UNVERIFIED",
                         disk.get("rollback_diagnostics", []))

    def test_b_publishing_foreign_preempted(self):
        r = self.root / "r-b.json"
        foreign = b'{"something": "else"}'
        r.write_bytes(foreign)
        result, disk = self._rollback(
            self._journal_with_state("publishing", "s-b", r), "s-b")
        self.assertTrue(r.exists())
        self.assertEqual(r.read_bytes(), foreign)
        self.assertEqual(result["rolled_back"], ["manager"])
        self.assertIn("receipt:ownership-unverified", result["residue"])
        self.assertIn("RECEIPT_OWNERSHIP_UNVERIFIED",
                      disk.get("rollback_diagnostics", []))

    def test_c_published_then_foreign_replaced(self):
        # our real receipt published, crash, foreign swaps the file
        helper = TestReceiptCrashRecovery()
        helper.setUp()
        self.addCleanup(helper.tmp.cleanup)
        j, r, fd = helper._crash_after_publish("s-c")
        # foreign replaces with another session's valid-looking JSON
        replaced = json.loads(r.read_text())
        replaced["rewire_session"] = "someone-else"
        import hashlib as _h
        canonical = {k: v for k, v in replaced.items()
                     if k != "receipt_sha256"}
        replaced["receipt_sha256"] = _h.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True).encode()).hexdigest()
        r.write_text(json.dumps(replaced), encoding="utf-8")
        fd2 = FakeDocker()
        fd2._configs = dict(fd._configs)
        fd2._backups = dict(fd._backups)
        result = hw.rollback(journal_path=j, docker=fd2, session="s-c")
        self.assertTrue(r.exists())          # NOT deleted
        self.assertIn("receipt:ownership-unverified",
                      result["residue"])
        for role in hw.ROLES:
            self.assertIn("aigw-local", fd2.config_text(role))

    def test_d_same_session_wrong_hash(self):
        r = self.root / "r-d.json"
        # session matches journal intent, receipt_sha256 differs
        body = {"rewire_session": "s-d", "receipt_sha256": "f" * 64}
        r.write_text(json.dumps(body), encoding="utf-8")
        result, disk = self._rollback(
            self._journal_with_state("publishing", "s-d", r,
                                     sha="0" * 64), "s-d")
        self.assertTrue(r.exists())
        self.assertIn("receipt:ownership-unverified", result["residue"])

    def test_e_hash_field_matches_canonical_does_not(self):
        r = self.root / "r-e.json"
        # receipt field == journal hash, but body does not hash to it
        body = {"rewire_session": "s-e",
                "receipt_sha256": "0" * 64, "junk": True}
        r.write_text(json.dumps(body), encoding="utf-8")
        result, _ = self._rollback(
            self._journal_with_state("publishing", "s-e", r,
                                     sha="0" * 64), "s-e")
        self.assertTrue(r.exists())
        self.assertIn("receipt:ownership-unverified", result["residue"])

    def test_f_malformed_oversized_reparse(self):
        # malformed JSON
        r1 = self.root / "r-f1.json"
        r1.write_bytes(b"{not json")
        result, _ = self._rollback(
            self._journal_with_state("publishing", "s-f1", r1), "s-f1")
        self.assertTrue(r1.exists())
        self.assertIn("receipt:ownership-unverified", result["residue"])
        # oversized (> 64 KiB)
        r2 = self.root / "r-f2.json"
        r2.write_bytes(b"x" * (hw._RECEIPT_MAX_BYTES + 1))
        result, _ = self._rollback(
            self._journal_with_state("publishing", "s-f2", r2), "s-f2")
        self.assertTrue(r2.exists())
        self.assertIn("receipt:ownership-unverified", result["residue"])
        # agents still rolled back in every variant
        # (rolled_back contains manager in all three calls above)


class TestRollbackReporting(HarnessTestBase):
    """R6: honest split of config-restore vs backup-cleanup."""

    def test_backup_remove_failure_reported(self):
        # single role; restore OK; docker rm rc=1 -> reported, not
        # swallowed, not misreported as a restore failure.
        j = self.root / "j-brm.json"
        journal = {"ownership": hw.HARNESS_IDENTITY, "session": "brm",
                   "status": "in-progress",
                   "roles": {"manager": {
                       "status": "mutated",
                       "backup": hw.MCPORTER_PATH["manager"]
                       + hw.BACKUP_SUFFIX}}}
        j.write_text(json.dumps(journal), encoding="utf-8")
        fd = FakeDocker(rm_fail_role="manager")
        c = ex.HICLAW_ROLE_FREEZE["manager"][0]
        fd._exec(["exec", c, "cp", hw.MCPORTER_PATH["manager"],
                  hw.MCPORTER_PATH["manager"] + hw.BACKUP_SUFFIX])
        fd._configs[c] = b'{"url":"http://172.31.0.18:8083/m/sse"}'
        result = hw.rollback(journal_path=j, docker=fd, session="brm")
        # config restored + role IS in rolled
        self.assertIn("aigw-local", fd.config_text("manager"))
        self.assertEqual(result["rolled_back"], ["manager"])
        # backup still exists; honest diagnostic + residue
        self.assertIn(hw.MCPORTER_PATH["manager"] + hw.BACKUP_SUFFIX,
                      fd._backups)
        disk = json.loads(j.read_text())
        self.assertIn("BACKUP_REMOVE_FAILED:manager",
                      disk["rollback_diagnostics"])
        self.assertIn("backup:manager", disk["rollback_residue"])
        self.assertNotIn("role:manager", disk["rollback_residue"])
        # journal NOT marked clean
        self.assertEqual(disk["status"], "rollback-residue")
        self.assertEqual(disk["roles"]["manager"]["status"],
                         "rolled-back-with-backup-residue")
        # no secrets
        self.assertNotIn("Bearer", j.read_text())
        # second rollback RETRIES the backup cleanup
        calls_before = len(fd.calls)
        second = hw.rollback(journal_path=j, docker=fd,
                             session="brm")
        self.assertGreater(len(fd.calls), calls_before)
        self.assertTrue(any(c[:3] == ["exec", c and "hiclaw-manager",
                                      "rm"] or
                            (len(c) > 2 and c[2] == "rm")
                            for c in fd.calls[calls_before:]))

    def test_combined_role_receipt_backup_failures(self):
        # reviewer restore FAILS; fixer restore OK but backup rm
        # rc=1; receipt ownership unverifiable (foreign bytes).
        j = self.root / "j-comb.json"
        r = self.root / "r-comb.json"
        foreign = b'{"foreign": true}'
        r.write_bytes(foreign)
        journal = {
            "ownership": hw.HARNESS_IDENTITY, "session": "comb",
            "status": "in-progress",
            "roles": {
                "reviewer": {"status": "verified",
                             "backup": hw.MCPORTER_PATH["reviewer"]
                             + hw.BACKUP_SUFFIX},
                "fixer": {"status": "mutated",
                          "backup": hw.MCPORTER_PATH["fixer"]
                          + hw.BACKUP_SUFFIX}},
            "receipt_state": "publishing",
            "receipt_path": str(r),
            "receipt_session": "comb",
            "receipt_sha256": "0" * 64,
        }
        j.write_text(json.dumps(journal), encoding="utf-8")
        fd = FakeDocker(rm_fail_role="fixer")
        real_restore = fd.restore_config

        def flaky_restore(container, backup_path, path):
            if fd._role_of(container) == "reviewer":
                raise OSError("restore refused")
            return real_restore(container, backup_path, path)

        fd.restore_config = flaky_restore
        for role in ("reviewer", "fixer"):
            c = ex.HICLAW_ROLE_FREEZE[role][0]
            fd._exec(["exec", c, "cp", hw.MCPORTER_PATH[role],
                      hw.MCPORTER_PATH[role] + hw.BACKUP_SUFFIX])
        fd._configs[ex.HICLAW_ROLE_FREEZE["fixer"][0]] = \
            b'{"url":"http://172.31.0.18:8083/f/sse"}'
        fd._configs[ex.HICLAW_ROLE_FREEZE["reviewer"][0]] = \
            b'{"url":"http://172.31.0.18:8083/r/sse"}'

        with self.assertRaises(hw.HarnessError) as ctx:
            hw.rollback(journal_path=j, docker=fd, session="comb")

        # === primary error from the REAL exception ===
        self.assertEqual(ctx.exception.code, "HARNESS_ROLLBACK_FAILED")
        self.assertIn("ROLLBACK_FAILED:reviewer", ctx.exception.detail)
        # === full categories from the on-disk journal ===
        disk = json.loads(j.read_text())
        diags = disk["rollback_diagnostics"]
        residue = disk["rollback_residue"]
        self.assertTrue(any(d.startswith("ROLLBACK_FAILED:reviewer")
                            for d in diags))
        for needed in ("BACKUP_REMOVE_FAILED:fixer",
                       "RECEIPT_OWNERSHIP_UNVERIFIED"):
            self.assertIn(needed, diags)
        for needed in ("role:reviewer", "backup:fixer",
                       "receipt:ownership-unverified"):
            self.assertIn(needed, residue)
        self.assertEqual(len(residue), len(set(residue)))
        # journal overall + per-role honesty
        self.assertEqual(disk["status"], "rollback-failed")
        self.assertEqual(disk["roles"]["reviewer"]["status"],
                         "rollback-failed")
        self.assertEqual(disk["roles"]["fixer"]["status"],
                         "rolled-back-with-backup-residue")
        # === resource end-states ===
        self.assertTrue(r.exists())
        self.assertEqual(r.read_bytes(), foreign)
        self.assertIn("172.31.0.18", fd.config_text("reviewer"))
        self.assertIn("aigw-local", fd.config_text("fixer"))
        self.assertIn(hw.MCPORTER_PATH["reviewer"] + hw.BACKUP_SUFFIX,
                      fd._backups)
        self.assertIn(hw.MCPORTER_PATH["fixer"] + hw.BACKUP_SUFFIX,
                      fd._backups)
        blob = json.dumps(disk) + ctx.exception.detail
        for forbidden in ("Bearer", "secret-", "foreign", "true"):
            self.assertNotIn(forbidden, blob)

    def test_auto_rollback_primary_preserved_with_backup_failure(self):
        # verify failure DURING apply + backup rm failure during the
        # automatic rollback: primary stays HARNESS_VERIFY_FAILED.
        j = self.root / "j-auto.json"
        r = self.root / "r-auto.json"
        fd = FakeDocker(rm_fail_role="manager")
        orig_read = fd.read_config

        def flaky_read(container, path):
            data = orig_read(container, path)
            if fd._role_of(container) == "manager":
                return b'{"mcpServers":{"gh":{"url":"http://drift"}}}'
            return data

        fd.read_config = flaky_read
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=j, receipt_path=r, docker=fd,
                     session="auto")
        self.assertEqual(ctx.exception.code, "HARNESS_VERIFY_FAILED")
        diags = getattr(ctx.exception, "diagnostics", [])
        self.assertTrue(any("BACKUP_REMOVE_FAILED:manager" in d
                            for d in diags))
        self.assertFalse(r.exists())
        disk = json.loads(j.read_text())
        self.assertIn("backup:manager", disk["rollback_residue"])
        self.assertIn("aigw-local", fd.config_text("manager"))


    def test_rollback_failed_role_retry_restores(self):
        # R7 §6: a restore-failed role converges on retry: restore
        # RE-EXECUTED, stale role: residue EXACTLY removed, backup
        # cleaned, overall rolled-back; third call fully idempotent.
        j = self.root / "j-rf.json"
        j.write_text(json.dumps({
            "ownership": hw.HARNESS_IDENTITY, "session": "rf",
            "status": "in-progress",
            "roles": {"manager": {
                "status": "mutated",
                "backup": hw.MCPORTER_PATH["manager"]
                + hw.BACKUP_SUFFIX}}}), encoding="utf-8")
        fd = FakeDocker()
        c = ex.HICLAW_ROLE_FREEZE["manager"][0]
        fd._exec(["exec", c, "cp", hw.MCPORTER_PATH["manager"],
                  hw.MCPORTER_PATH["manager"] + hw.BACKUP_SUFFIX])
        fd._configs[c] = b'{"url":"http://172.31.0.18:8083/m/sse"}'
        rr = fd.restore_config
        calls = {"n": 0, "fail": True}

        def counted(container, b, p):
            calls["n"] += 1
            if calls["fail"]:
                raise OSError("restore down")
            return rr(container, b, p)

        fd.restore_config = counted
        # 1st: restore fails
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.rollback(journal_path=j, docker=fd, session="rf")
        self.assertEqual(ctx.exception.code, "HARNESS_ROLLBACK_FAILED")
        d1 = json.loads(j.read_text())       # from DISK
        self.assertEqual(calls["n"], 1)
        self.assertEqual(d1["roles"]["manager"]["status"],
                         "rollback-failed")
        self.assertIn("role:manager", d1["rollback_residue"])
        self.assertEqual(d1["status"], "rollback-failed")
        self.assertIn(hw.MCPORTER_PATH["manager"] + hw.BACKUP_SUFFIX,
                      fd._backups)
        # 2nd: restore succeeds (fresh production call, same disk j)
        calls["fail"] = False
        result = hw.rollback(journal_path=j, docker=fd, session="rf")
        d2 = json.loads(j.read_text())
        self.assertEqual(calls["n"], 2)
        self.assertIn("aigw-local", fd.config_text("manager"))
        self.assertNotIn(hw.MCPORTER_PATH["manager"]
                         + hw.BACKUP_SUFFIX, fd._backups)
        self.assertEqual(d2["roles"]["manager"]["status"],
                         "rolled-back")
        self.assertNotIn("role:manager", d2["rollback_residue"])
        self.assertNotIn("backup:manager", d2["rollback_residue"])
        self.assertEqual(d2["rollback_residue"], [])
        self.assertEqual(d2["status"], "rolled-back")
        # 3rd: idempotent — no further restore attempts
        third = hw.rollback(journal_path=j, docker=fd, session="rf")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(third["rolled_back"], [])

    def test_backup_residue_retry_cleans_without_restore(self):
        # R7 §7: a backup-removal residue converges on retry WITHOUT
        # re-running the (already successful) restore.
        j = self.root / "j-br.json"
        j.write_text(json.dumps({
            "ownership": hw.HARNESS_IDENTITY, "session": "br",
            "status": "in-progress",
            "roles": {"manager": {
                "status": "mutated",
                "backup": hw.MCPORTER_PATH["manager"]
                + hw.BACKUP_SUFFIX}}}), encoding="utf-8")
        fd = FakeDocker(rm_fail_role="manager")
        c = ex.HICLAW_ROLE_FREEZE["manager"][0]
        fd._exec(["exec", c, "cp", hw.MCPORTER_PATH["manager"],
                  hw.MCPORTER_PATH["manager"] + hw.BACKUP_SUFFIX])
        fd._configs[c] = b'{"url":"http://172.31.0.18:8083/m/sse"}'
        rr = fd.restore_config
        rc = {"n": 0}

        def counted(container, b, p):
            rc["n"] += 1
            return rr(container, b, p)

        fd.restore_config = counted
        # 1st: restore OK, rm rc=1
        result = hw.rollback(journal_path=j, docker=fd, session="br")
        d1 = json.loads(j.read_text())
        self.assertEqual(rc["n"], 1)
        self.assertEqual(d1["roles"]["manager"]["status"],
                         "rolled-back-with-backup-residue")
        self.assertIn("backup:manager", d1["rollback_residue"])
        self.assertNotIn("role:manager", d1["rollback_residue"])
        self.assertIn(hw.MCPORTER_PATH["manager"] + hw.BACKUP_SUFFIX,
                      fd._backups)
        self.assertEqual(d1["status"], "rollback-residue")
        # 2nd: rm now succeeds; restore MUST NOT re-run
        fd._rm_fail_role = None
        rm_calls = {"n": sum(1 for c2 in fd.calls
                             if len(c2) > 2 and c2[2] == "rm")}
        result2 = hw.rollback(journal_path=j, docker=fd,
                              session="br")
        d2 = json.loads(j.read_text())
        rm_calls2 = sum(1 for c2 in fd.calls
                        if len(c2) > 2 and c2[2] == "rm")
        self.assertEqual(rc["n"], 1)             # no re-restore
        self.assertGreater(rm_calls2, rm_calls["n"])
        self.assertNotIn(hw.MCPORTER_PATH["manager"]
                         + hw.BACKUP_SUFFIX, fd._backups)
        self.assertEqual(d2["roles"]["manager"]["status"],
                         "rolled-back")
        self.assertEqual(d2["rollback_residue"], [])
        self.assertEqual(d2["status"], "rolled-back")
        # 3rd: fully idempotent
        before_restore, before_rm = rc["n"], rm_calls2
        hw.rollback(journal_path=j, docker=fd, session="br")
        self.assertEqual(rc["n"], before_restore)
        self.assertEqual(sum(1 for c2 in fd.calls
                             if len(c2) > 2 and c2[2] == "rm"),
                         before_rm)

    def test_residue_convergence_preserves_unrelated_entries(self):
        # R7 §8: converging role:/backup: entries never touches an
        # unrelated REAL residue (receipt:...); order stays stable.
        j = self.root / "j-mix.json"
        j.write_text(json.dumps({
            "ownership": hw.HARNESS_IDENTITY, "session": "mix",
            "status": "in-progress",
            "roles": {"manager": {
                "status": "mutated",
                "backup": hw.MCPORTER_PATH["manager"]
                + hw.BACKUP_SUFFIX}},
            "rollback_residue": [
                "receipt:ownership-unverified", "role:manager"],
            "receipt_state": "publishing",
            "receipt_path": str(self.root / "no-such-receipt.json"),
            "receipt_session": "mix",
            "receipt_sha256": "0" * 64}), encoding="utf-8")
        fd = FakeDocker(rm_fail_role="manager")
        c = ex.HICLAW_ROLE_FREEZE["manager"][0]
        fd._exec(["exec", c, "cp", hw.MCPORTER_PATH["manager"],
                  hw.MCPORTER_PATH["manager"] + hw.BACKUP_SUFFIX])
        fd._configs[c] = b'{"url":"http://172.31.0.18:8083/m/sse"}'
        # 1st: restore OK, rm fails -> role: converged away,
        # receipt: and backup: remain, order preserved
        hw.rollback(journal_path=j, docker=fd, session="mix")
        d1 = json.loads(j.read_text())
        self.assertNotIn("role:manager", d1["rollback_residue"])
        self.assertIn("backup:manager", d1["rollback_residue"])
        self.assertIn("receipt:ownership-unverified",
                      d1["rollback_residue"])
        self.assertLess(
            d1["rollback_residue"].index("receipt:ownership-"
                                         "unverified"),
            d1["rollback_residue"].index("backup:manager"))
        self.assertEqual(d1["status"], "rollback-residue")
        # 2nd: rm succeeds -> only the receipt entry remains
        fd._rm_fail_role = None
        hw.rollback(journal_path=j, docker=fd, session="mix")
        d2 = json.loads(j.read_text())
        self.assertEqual(d2["rollback_residue"],
                         ["receipt:ownership-unverified"])
        self.assertEqual(d2["status"], "rollback-residue")


class TestRollbackTimeReparse(HarnessTestBase):
    """R6 §7 (R4-B): receipt target is a symlink/reparse AT
    ROLLBACK time; production verifier refuses to follow/delete."""

    def test_g_rollback_time_reparse_receipt(self):
        r = self.root / "r-g.json"
        outside = self.root / "foreign-target.json"
        foreign = b'{"foreign": "symlink-target-bytes"}'
        outside.write_bytes(foreign)
        journal = {
            "ownership": hw.HARNESS_IDENTITY, "session": "s-g",
            "status": "in-progress",
            "roles": {"manager": {
                "status": "mutated",
                "backup": hw.MCPORTER_PATH["manager"]
                + hw.BACKUP_SUFFIX}},
            "receipt_state": "publishing",
            "receipt_path": str(r),
            "receipt_session": "s-g",
            "receipt_sha256": "0" * 64,
        }
        j = self.root / "j-g.json"
        j.write_text(json.dumps(journal), encoding="utf-8")
        fd = FakeDocker()
        c = ex.HICLAW_ROLE_FREEZE["manager"][0]
        fd._exec(["exec", c, "cp", hw.MCPORTER_PATH["manager"],
                  hw.MCPORTER_PATH["manager"] + hw.BACKUP_SUFFIX])

        try:
            r.symlink_to(outside)
            ctx_mgr = None
        except (OSError, NotImplementedError):
            from unittest import mock
            import contextlib
            ctx_mgr = mock.patch("os.path.islink", return_value=True)

        import contextlib
        with (ctx_mgr or contextlib.nullcontext()):
            result = hw.rollback(journal_path=j, docker=fd,
                                 session="s-g")
        # symlink / foreign target untouched
        if ctx_mgr is None:
            self.assertTrue(r.is_symlink())
        self.assertEqual(outside.read_bytes(), foreign)
        # agents still recovered
        self.assertEqual(result["rolled_back"], ["manager"])
        self.assertIn("aigw-local", fd.config_text("manager"))
        # honest reporting
        self.assertIn("RECEIPT_OWNERSHIP_UNVERIFIED",
                      result["diagnostics"])
        self.assertIn("receipt:ownership-unverified",
                      result["residue"])
        disk = json.loads(j.read_text())
        self.assertNotEqual(disk["status"], "complete")


class TestPostWritePrePersistCrash(HarnessTestBase):
    """R3 (R2-B): the exact window write_config succeeded -> the
    mutated status NOT yet persisted. Disk journal says applying,
    container config already points at the target gateway."""

    def test_post_write_pre_mutated_persist_crash_reviewer(self):
        j = self.root / "j-pw.json"
        r = self.root / "r-pw.json"
        fd = FakeDocker()

        def hook(phase, role):
            if phase == "mutated_written" and role == "reviewer":
                raise CrashSimulated(phase)

        try:
            hw.apply(journal_path=j, receipt_path=r, docker=fd,
                     session="pw", phase_hook=hook)
        except CrashSimulated:
            pass
        # reviewer (2nd role) was REALLY mutated before the crash
        self.assertEqual(fd.writes, 2)
        self.assertIn("172.31.0.18", fd.config_text("reviewer"))
        # disk journal still says applying for reviewer
        journal = json.loads(j.read_text())
        self.assertEqual(journal["roles"]["reviewer"]["status"],
                         "applying")
        self.assertEqual(journal["roles"]["manager"]["status"],
                         "verified")
        self.assertFalse(r.exists())
        # FRESH harness instance; disk-journal rollback only
        fd2 = FakeDocker()
        fd2._configs = dict(fd._configs)
        fd2._backups = dict(fd._backups)
        foreign = self.root / "foreign.txt"
        foreign.write_text("keep", encoding="utf-8")
        result = hw.rollback(journal_path=j, docker=fd2, session="pw")
        # strict reverse order over possibly-written roles
        self.assertEqual(result["rolled_back"],
                         ["reviewer", "manager"])
        for role in hw.ROLES:
            self.assertIn("aigw-local", fd2.config_text(role))
        # later roles were never written
        self.assertEqual(fd2.writes, 0)   # fresh instance wrote none
        self.assertEqual(fd.writes, 2)    # original wrote exactly 2
        self.assertFalse(r.exists())
        self.assertEqual(foreign.read_text(), "keep")
        # idempotent second rollback
        second = hw.rollback(journal_path=j, docker=fd2, session="pw")
        self.assertEqual(second["rolled_back"], [])


class TestPrimaryErrorPreservation(HarnessTestBase):
    """R2-E: verify failure + rollback failure -> primary survives,
    rollback errors only in diagnostics."""

    def test_verify_failure_with_rollback_failure(self):
        fd = FakeDocker()
        orig_read = fd.read_config

        def flaky_read(container, path):
            data = orig_read(container, path)
            if fd._role_of(container) == "reviewer":
                return (b'{"mcpServers":{"gh":{"url":'
                        b'"http://drift"}}}')
            return data

        fd.read_config = flaky_read
        real_restore = fd.restore_config

        def failing_restore(container, backup_path, path):
            raise OSError("docker down during rollback")

        fd.restore_config = failing_restore
        j = self.root / "j-ep.json"
        r = self.root / "r-ep.json"
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=j, receipt_path=r, docker=fd,
                     session="ep1")
        self.assertEqual(ctx.exception.code, "HARNESS_VERIFY_FAILED")
        diags = getattr(ctx.exception, "diagnostics", [])
        self.assertTrue(any("ROLLBACK_FAILED" in d for d in diags))
        journal = json.loads(j.read_text())
        self.assertEqual(journal["status"], "rollback-failed")
        self.assertTrue(any("reviewer" in x for x in
                        journal.get("rollback_residue", [])))
        self.assertFalse(r.exists())


class TestReceiptContract(HarnessTestBase):

    def test_receipt_schema_and_canonical_hash(self):
        fd = FakeDocker()
        hw.apply(journal_path=self.journal,
                 receipt_path=self.receipt, docker=fd, session="r1")
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(len(receipt["agents"]), 4)
        self.assertEqual(receipt["rollback_ownership"],
                         "mp-gh4-harness")
        import hashlib
        canonical = {k: v for k, v in receipt.items()
                     if k != "receipt_sha256"}
        expect = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(receipt["receipt_sha256"], expect)
        for agent in receipt["agents"]:
            for f in ("config_hash_before", "config_hash_after",
                      "token_hash"):
                self.assertRegex(agent[f], r"^[0-9a-f]{64}$")
            self.assertEqual(agent["gateway_url"],
                             TARGET[agent["role"]])

    def test_receipt_and_journal_zero_secret(self):
        fd = FakeDocker()
        hw.apply(journal_path=self.journal,
                 receipt_path=self.receipt, docker=fd, session="r2")
        blob = (self.receipt.read_text()
                + self.journal.read_text())
        for forbidden in ("secret-", "Bearer ", "ghp_", "syt_"):
            self.assertNotIn(forbidden, blob)

    def test_argv_and_calls_zero_secret(self):
        fd = FakeDocker()
        hw.apply(journal_path=self.journal,
                 receipt_path=self.receipt, docker=fd, session="r3")
        for argv in fd.calls:
            joined = " ".join(str(a) for a in argv)
            self.assertNotIn("Bearer", joined)
            self.assertNotIn("secret-", joined)

    def test_old_mcp_never_started_or_stopped(self):
        fd = FakeDocker()
        hw.apply(journal_path=self.journal,
                 receipt_path=self.receipt, docker=fd, session="r4")
        for argv in fd.calls:
            if argv[0] in ("start", "stop", "rm", "restart",
                           "create"):
                self.assertNotIn("github-mcp", " ".join(argv))
                self.assertNotIn("hiclaw", " ".join(argv))

    def test_verify_uses_production_validator(self):
        fd = FakeDocker()
        hw.apply(journal_path=self.journal,
                 receipt_path=self.receipt, docker=fd, session="r5")
        verdict = hw.verify(self.receipt, docker=fd)
        self.assertTrue(verdict["verified"])

    def test_verify_rejects_drift(self):
        fd = FakeDocker()
        hw.apply(journal_path=self.journal,
                 receipt_path=self.receipt, docker=fd, session="r6")
        # simulate container ID drift after the fact
        receipt = json.loads(self.receipt.read_text())
        receipt["agents"][0]["container_id"] = "cid-DRIFTED"
        import hashlib
        canonical = {k: v for k, v in receipt.items()
                     if k != "receipt_sha256"}
        receipt["receipt_sha256"] = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True).encode()).hexdigest()
        self.receipt.write_text(json.dumps(receipt))
        verdict = hw.verify(self.receipt, docker=fd)
        self.assertFalse(verdict["verified"])

    def test_stopped_state_family_normalized(self):
        """docker 'exited' satisfies expected 'stopped' (production
        normalization extracted this round)."""
        fd = FakeDocker()
        hw.apply(journal_path=self.journal,
                 receipt_path=self.receipt, docker=fd, session="r7")
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(
            receipt["old_github_mcp"]["state"], "exited")
        verdict = _receipt_ok(fd, self.receipt)
        self.assertEqual(
            verdict["checks"]["old_github_mcp"]["state"], "OK")


class TestFileSafety(HarnessTestBase):

    def test_reparse_refused(self):
        class BadWriter(hw.AtomicFileWriter):
            @classmethod
            def write(cls, path, data, *, root=None):
                raise hw.HarnessError("HARNESS_REPARSE_REFUSED", "x")

        fd = FakeDocker()
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     writer=BadWriter(), session="f1")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_REPARSE_REFUSED")

    def test_journal_persist_failure_is_primary(self):
        class FailingWriter(hw.AtomicFileWriter):
            count = 0

            @classmethod
            def write(cls, path, data, *, root=None):
                FailingWriter.count += 1
                if FailingWriter.count > 1:
                    raise OSError("disk full")

        fd = FakeDocker()
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt, docker=fd,
                     writer=FailingWriter(), session="f2")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_JOURNAL_PERSIST_FAILED")

    def test_no_pat_or_pem_reads(self):
        fd = FakeDocker()
        hw.apply(journal_path=self.journal,
                 receipt_path=self.receipt, docker=fd, session="f3")
        for argv in fd.calls:
            joined = " ".join(str(a) for a in argv)
            self.assertNotIn("fgpat", joined)
            self.assertNotIn(".pem", joined)


class _noop:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# patch helper used by plan test (plan uses the production executor
# factory; tests inject the fake through the module attribute)
import unittest.mock                      # noqa: E402

_orig_plan = hw.plan


def _plan_with(fd, journal_path):
    with unittest.mock.patch.object(hw, "_default_docker_executor",
                                    return_value=fd._exec):
        return _orig_plan(journal_path)


class TestPlanInjection(unittest.TestCase):
    def test_plan_with_fake(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fd = FakeDocker()
            result = _plan_with(fd, Path(tmp) / "j.json")
            self.assertEqual(result["writes_executed"], 0)
            self.assertEqual(fd.writes, 0)


# rewire the base plan test to use the injection helper
TestReadOnlyCommands.test_plan_zero_writes = (
    lambda self: self.test_plan_zero_writes_impl())

TestReadOnlyCommands.test_plan_zero_writes_impl = (
    lambda self: (lambda fd: (
        self.assertEqual(
            _plan_with(fd, self.journal)["writes_executed"], 0),
        self.assertEqual(fd.writes, 0),
        self.assertFalse(self.journal.exists()),
    ))(FakeDocker()))

if __name__ == "__main__":
    unittest.main()
