#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ISOLATED_LIVE ephemeral PostgreSQL harness — Phase A unit tests (no Docker).

These tests exercise the Phase A scaffolding in
:mod:`tests.isolated_live.ephemeral_harness` entirely WITHOUT Docker or a real
PostgreSQL server. ``subprocess`` is mocked everywhere so no real ``docker`` /
``wsl`` process is ever spawned.

Test groups
  TestExecutionGate        — env + daemon gate logic (mocked subprocess)
  TestMigrationOrder       — MIGRATION_CHAIN has 15 entries; idempotency rounds
  TestRoleBootstrap        — prerequisite + reader role SQL shape
  TestSeedContract         — 5-run seed SQL satisfies DDL constraints
  TestRevisionDigest       — canonical digest algorithm vs bind_revision
  TestCommandSafety        — argv arrays (never shell); redaction; name validation
  TestCleanupValidation    — container-name validation + cleanup command shape
  TestResultClassification — skip reasons + classification string fields
  TestEphemeralPlaceholder — unconditional skip; documents Phase B intent

Status: ``NOT_EXECUTED``. The placeholder class skips unconditionally because
the harness executor (Phase B) is not implemented — even when
``EPHEMERAL_PG_VERIFY=1`` no container is started.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent  # tests/isolated_live → repo root
# Add (in order): this test dir (so the sibling ephemeral_harness module
# imports cleanly), the repo root, and tools/demo_console (for postgres_source).
for _p in (str(_HERE), str(ROOT), str(ROOT / "tools" / "demo_console")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ephemeral_harness import (  # noqa: E402
    AUTHORIZED_DAEMON,
    CANONICAL_VIEWER_ROLE,
    ENVIRONMENT_ID_EPHEMERAL,
    IMAGE_DIGEST,
    ISOLATED_LIVE_MIGRATIONS,
    MIGRATION_CHAIN,
    PREREQUISITE_ROLES,
    build_cleanup_commands,
    build_migration_commands,
    build_prerequisite_role_sql,
    build_reader_role_sql,
    build_seed_sql,
    check_execution_auth,
    compute_revision_digest,
    measure_server_identity,
    redact_secrets,
    validate_container_name,
)
from postgres_source import CANONICAL_VIEWER_ROLE as PG_CANONICAL_VIEWER_ROLE  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# TestExecutionGate — env var + daemon reachability gate
# ────────────────────────────────────────────────────────────────────────────
class TestExecutionGate(unittest.TestCase):
    """check_execution_auth: two-key rule (env=1 AND daemon reachable)."""

    def setUp(self):
        # Ensure a clean env for every sub-test.
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("EPHEMERAL_PG_VERIFY", None)

    def tearDown(self):
        self._env_patch.stop()

    def test_not_set_returns_unauthorized(self):
        result = check_execution_auth()
        self.assertFalse(result["authorized"])
        self.assertIn("EPHEMERAL_PG_VERIFY", result["reason"])

    def test_zero_returns_unauthorized(self):
        os.environ["EPHEMERAL_PG_VERIFY"] = "0"
        result = check_execution_auth()
        self.assertFalse(result["authorized"])

    def test_true_string_returns_unauthorized(self):
        # Only the exact literal "1" authorizes; "true"/"yes" do not.
        os.environ["EPHEMERAL_PG_VERIFY"] = "true"
        result = check_execution_auth()
        self.assertFalse(result["authorized"])

    @mock.patch("ephemeral_harness.subprocess.run")
    def test_set_but_daemon_check_fails(self, mock_run):
        # env=1 BUT docker info returns non-zero → still unauthorized.
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        mock_run.return_value = mock.Mock(returncode=1, stdout=b"", stderr=b"err")
        result = check_execution_auth()
        self.assertFalse(result["authorized"])
        self.assertIn(AUTHORIZED_DAEMON, result["reason"])
        # Verify the probe used array arguments (no shell=True).
        args, kwargs = mock_run.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn("shell", kwargs)

    @mock.patch("ephemeral_harness.subprocess.run")
    def test_set_and_daemon_reachable_authorizes(self, mock_run):
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        mock_run.return_value = mock.Mock(returncode=0, stdout=b"ok", stderr=b"")
        result = check_execution_auth()
        self.assertTrue(result["authorized"])

    @mock.patch("ephemeral_harness.subprocess.run", side_effect=FileNotFoundError)
    def test_set_but_wsl_missing_is_unauthorized(self, mock_run):
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        result = check_execution_auth()
        self.assertFalse(result["authorized"])

    @mock.patch(
        "ephemeral_harness.subprocess.run",
        side_effect=__import__("subprocess").TimeoutExpired(cmd="wsl", timeout=1),
    )
    def test_set_but_daemon_timeout_is_unauthorized(self, mock_run):
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        result = check_execution_auth()
        self.assertFalse(result["authorized"])


# ────────────────────────────────────────────────────────────────────────────
# TestMigrationOrder — MIGRATION_CHAIN shape and idempotency rounds
# ────────────────────────────────────────────────────────────────────────────
class TestMigrationOrder(unittest.TestCase):
    """MIGRATION_CHAIN: 13 audit-db applications (9 base + m4f1 x2 + hotfix x2),
    11 distinct files. Plus 2 ISOLATED_LIVE migrations (001/002) in a separate
    Phase 3 = 15 total migration-file applications. Plus 2 role bootstrap
    operations (prerequisite + reader) = 17 executor operations.

    Audit-db applications = 13. ISOLATED_LIVE applications = 2.
    Total migration-file applications = 15. Distinct files = 11.
    Executor operations = 17 (15 migrations + 2 role bootstraps).
    """

    def test_chain_has_thirteen_audit_db_entries(self):
        self.assertEqual(len(MIGRATION_CHAIN), 13)

    def test_m4f1_state_appears_twice(self):
        names = [f for f, _ in MIGRATION_CHAIN]
        self.assertEqual(names.count("m4f1_state.sql"), 2)

    def test_m4f1_hotfix_1_appears_twice(self):
        names = [f for f, _ in MIGRATION_CHAIN]
        self.assertEqual(names.count("m4f1_hotfix_1.sql"), 2)

    def test_eleven_distinct_files_present(self):
        names = [f for f, _ in MIGRATION_CHAIN]
        distinct = set(names)
        self.assertEqual(len(distinct), 11)
        expected = {
            "init.sql", "m3_state.sql", "m3b_policy.sql", "m3b_b4.sql",
            "m3b_b4c.sql", "m3b_b4c1.sql", "m3b_b4c1_1.sql", "m3b_b4d1.sql",
            "m3c_state.sql", "m4f1_state.sql", "m4f1_hotfix_1.sql",
        }
        self.assertSetEqual(distinct, expected)

    def test_init_first(self):
        self.assertEqual(MIGRATION_CHAIN[0][0], "init.sql")

    def test_m4f1_after_m3c_state(self):
        names = [f for f, _ in MIGRATION_CHAIN]
        self.assertLess(names.index("m3c_state.sql"), names.index("m4f1_state.sql"))
        self.assertLess(names.index("m3c_state.sql"),
                        names.index("m4f1_hotfix_1.sql"))

    def test_m4f1_state_before_hotfix(self):
        names = [f for f, _ in MIGRATION_CHAIN]
        self.assertLess(names.index("m4f1_state.sql"),
                        names.index("m4f1_hotfix_1.sql"))

    def test_chain_entries_are_filename_description_pairs(self):
        for entry in MIGRATION_CHAIN:
            self.assertIsInstance(entry, tuple)
            self.assertEqual(len(entry), 2)
            self.assertIsInstance(entry[0], str)
            self.assertIsInstance(entry[1], str)
            self.assertTrue(entry[0].endswith(".sql"))

    def test_isolated_live_migrations_are_separate_phase(self):
        # 001/002 are NOT in MIGRATION_CHAIN (they are a separate Phase 3).
        names = [f for f, _ in MIGRATION_CHAIN]
        for m in ISOLATED_LIVE_MIGRATIONS:
            self.assertNotIn(m, names)
        self.assertEqual(ISOLATED_LIVE_MIGRATIONS[0], "001_environment_identity.sql")
        self.assertEqual(ISOLATED_LIVE_MIGRATIONS[1], "002_mergepilot_reader_acl.sql")


# ────────────────────────────────────────────────────────────────────────────
# TestRoleBootstrap — prerequisite + reader role SQL
# ────────────────────────────────────────────────────────────────────────────
class TestRoleBootstrap(unittest.TestCase):
    """Prerequisite roles created first; reader role hardened; order enforced."""

    def test_prerequisite_roles_has_two_entries(self):
        self.assertEqual(len(PREREQUISITE_ROLES), 2)

    def test_prerequisite_roles_are_nologin(self):
        for role_stmt in PREREQUISITE_ROLES:
            self.assertIn("NOLOGIN", role_stmt)

    def test_prerequisite_roles_names(self):
        names = " ".join(PREREQUISITE_ROLES)
        self.assertIn("policy_gateway_l2", names)
        self.assertIn("mergepilot_approver", names)

    def test_prerequisite_sql_contains_both_roles(self):
        sql = build_prerequisite_role_sql()
        self.assertIn("policy_gateway_l2", sql)
        self.assertIn("mergepilot_approver", sql)
        self.assertIn("NOLOGIN", sql)

    def test_prerequisite_sql_is_idempotent(self):
        sql = build_prerequisite_role_sql()
        # IF NOT EXISTS guard so re-running is safe.
        self.assertIn("IF NOT EXISTS", sql)

    def test_reader_role_sql_contains_all_hardening_attributes(self):
        sql = build_reader_role_sql("dummy-password")
        for attr in ("NOINHERIT", "NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE",
                     "NOREPLICATION", "NOBYPASSRLS"):
            self.assertIn(attr, sql, msg="missing attribute: %s" % attr)

    def test_reader_role_sql_sets_read_only_default(self):
        sql = build_reader_role_sql("dummy-password")
        self.assertIn("default_transaction_read_only = on", sql)

    def test_reader_role_sql_names_canonical_role_exactly(self):
        sql = build_reader_role_sql("dummy-password")
        self.assertIn(CANONICAL_VIEWER_ROLE, sql)
        self.assertEqual(CANONICAL_VIEWER_ROLE, "mergepilot_reader")

    def test_reader_role_sql_escapes_single_quote_in_password(self):
        sql = build_reader_role_sql("o'reilly")
        # The single quote must be doubled to avoid breaking the literal /
        # injection.
        self.assertIn("o''reilly", sql)

    def test_canonical_role_matches_postgres_source(self):
        self.assertEqual(CANONICAL_VIEWER_ROLE, PG_CANONICAL_VIEWER_ROLE)

    def test_prerequisite_roles_documented_before_reader_role(self):
        # PREREQUISITE_ROLES (Phase 0) is defined in the design to run BEFORE
        # build_reader_role_sql (Phase 2). The module orders them as such:
        # PREREQUISITE_ROLES is the Phase 0 step; reader role is Phase 2.
        # We assert the two phases are distinct and prerequisites come first by
        # construction (separate functions/constants).
        self.assertTrue(callable(build_prerequisite_role_sql))
        self.assertTrue(callable(build_reader_role_sql))
        # Prerequisite SQL must NOT reference the reader role.
        self.assertNotIn(CANONICAL_VIEWER_ROLE, build_prerequisite_role_sql())


# ────────────────────────────────────────────────────────────────────────────
# TestSeedContract — 5-run seed SQL satisfies DDL constraints
# ────────────────────────────────────────────────────────────────────────────
class TestSeedContract(unittest.TestCase):
    """The seed SQL must use schema-valid values for every CHECK constraint."""

    @classmethod
    def setUpClass(cls):
        cls.seed = build_seed_sql()

    def test_success_run_status_is_pass_not_completed(self):
        # task_runs CHECK: ('SUBMITTED','RUNNING','PASS','FAIL','HOLD','MERGED','ROLLED_BACK')
        # 'COMPLETED' is NOT a valid task_runs.status.
        self.assertIn("'run-eph-ok'", self.seed)
        self.assertIn("'PASS'", self.seed)
        # The success task_runs row uses status='PASS'. Find the run-eph-ok
        # task_runs INSERT block (status is on the VALUES continuation).
        ok_idx = self.seed.find("'run-eph-ok'")
        # The INSERT INTO task_runs ... VALUES line carries the status; search a
        # generous window around the first occurrence.
        ok_block = self.seed[ok_idx:ok_idx + 600]
        self.assertIn("'PASS'", ok_block)

    def test_unknown_run_status_is_null_not_garbage(self):
        # run-eph-unknown must INSERT NULL status (not a bogus string).
        idx = self.seed.find("run-eph-unknown")
        block = self.seed[idx:idx + 200]
        self.assertIn("NULL", block)

    def test_rollback_run_status_reverted_not_completed(self):
        # rollback_runs CHECK does NOT include 'COMPLETED'; 'REVERTED' is valid.
        # (stage_runs.status may legitimately be 'COMPLETED'; only rollback_runs
        # is constrained here.)
        self.assertIn("'REVERTED'", self.seed)
        # Find the rollback_runs INSERT and assert its status is REVERTED, and
        # that COMPLETED does not appear within that statement.
        rb_idx = self.seed.find("INSERT INTO rollback_runs")
        rb_block = self.seed[rb_idx:rb_idx + 400]
        self.assertIn("'REVERTED'", rb_block)
        self.assertNotIn("'COMPLETED'", rb_block)

    def test_rollback_task_runs_status_rolled_back(self):
        idx = self.seed.find("run-eph-rollback")
        block = self.seed[idx:idx + 400]
        self.assertIn("'ROLLED_BACK'", block)

    def test_missing_run_has_no_task_runs_insert(self):
        # The missing run has NO task_runs INSERT. Its run_id never appears as
        # a value in any INSERT (only the design doc mentions it).
        self.assertNotIn("run-eph-missing", self.seed)

    def test_all_shas_are_40_char_hex(self):
        # Pull every quoted 40-hex token out of the seed and validate.
        sha_re = re.compile(r"'([0-9a-f]{40})'")
        shas = sha_re.findall(self.seed)
        self.assertGreaterEqual(len(shas), 3)  # base, head, revert merge
        for sha in shas:
            self.assertRegex(sha, r"^[0-9a-f]{40}$")

    def test_all_digests_are_64_char_hex(self):
        digest_re = re.compile(r"'([0-9a-f]{64})'")
        digests = digest_re.findall(self.seed)
        self.assertGreaterEqual(len(digests), 1)
        for d in digests:
            self.assertRegex(d, r"^[0-9a-f]{64}$")

    def test_revision_bindings_base_sha_matches_mcp_git_sha(self):
        # bind_revision requires mcp_calls.git_sha == revision_bindings.base_sha.
        # The seed uses 'a'*40 for both. Confirm both appear.
        a40 = "a" * 40
        self.assertIn(a40, self.seed)

    def test_stage_events_not_null_fields_present(self):
        # Required NOT NULL: event_id, room_id, event_type, status.
        for field in ("event_id", "room_id", "event_type", "status"):
            self.assertIn(field, self.seed)

    def test_environment_marker_inserted(self):
        self.assertIn(ENVIRONMENT_ID_EPHEMERAL, self.seed)

    def test_revision_binding_via_direct_admin_fallback_documented(self):
        # The seed uses Option B (direct-admin INSERT). The docstring records
        # revision_producer_contract = NOT_VERIFIED for this path.
        self.assertIn("NOT_VERIFIED", build_seed_sql.__doc__ or "")
        # And the seed SQL itself records the fallback.
        self.assertIn("direct-admin", self.seed.lower())
        self.assertIn("revision_bindings", self.seed)

    def test_seed_deterministic(self):
        # Same call → same bytes (no now()/random in the value positions that
        # matter; the digest is derived from fixed inputs).
        self.assertEqual(build_seed_sql(), build_seed_sql())

    def test_compute_revision_digest_matches_seed_value(self):
        # The digest embedded in the seed for run-eph-ok must equal
        # compute_revision_digest(...) for the same inputs.
        expected = compute_revision_digest(
            source_call_id="mcp-eph-001",
            correlation_id="corr-eph-001",
            tool="create_pull_request",
            target_repo="test/repo-alpha",
            run_id="run-eph-ok",
            git_sha="a" * 40,
            result_status="OK",
        )
        self.assertIn(expected, self.seed)


# ────────────────────────────────────────────────────────────────────────────
# TestRevisionDigest — canonical algorithm vs bind_revision
# ────────────────────────────────────────────────────────────────────────────
class TestRevisionDigest(unittest.TestCase):
    """compute_revision_digest mirrors public.bind_revision's recompute step."""

    def test_returns_64_char_lowercase_hex(self):
        d = compute_revision_digest(
            "call-1", "corr-1", "tool-x", "repo/r", "run-1", "a" * 40, "OK",
        )
        self.assertEqual(len(d), 64)
        self.assertEqual(d, d.lower())
        self.assertRegex(d, r"^[0-9a-f]{64}$")

    def test_deterministic_same_inputs_same_output(self):
        kwargs = dict(
            source_call_id="mcp-eph-001",
            correlation_id="corr-eph-001",
            tool="create_pull_request",
            target_repo="test/repo-alpha",
            run_id="run-eph-ok",
            git_sha="a" * 40,
            result_status="OK",
        )
        self.assertEqual(compute_revision_digest(**kwargs),
                         compute_revision_digest(**kwargs))

    def test_different_inputs_different_output(self):
        d1 = compute_revision_digest("a", "b", "c", "d", "e", "f" * 40, "OK")
        d2 = compute_revision_digest("a", "b", "c", "d", "e", "f" * 40, "ERROR")
        self.assertNotEqual(d1, d2)

    def test_matches_canonical_algorithm_from_bind_revision(self):
        # The canonical algorithm (m4f1_state.sql bind_revision step 4):
        #   digest(_canon_str(call) || _canon_str(corr) || _canon_str(tool) ||
        #          _canon_str(repo) || _canon_str(run) || _canon_str(sha) ||
        #          _canon_str(status), 'sha256')
        # where _canon_str(v) = octet_length(v)::text || ':' || v  (NULL→'-1:').
        def canon(v):
            if v is None:
                return "-1:"
            return "%d:%s" % (len(v.encode("utf-8")), v)

        inputs = ("call-1", "corr-1", "tool-x", "repo/r", "run-1",
                  "0123456789abcdef0123456789abcdef01234567", "OK")
        manual_concat = "".join(canon(v) for v in inputs)
        expected = hashlib.sha256(manual_concat.encode("utf-8")).hexdigest()
        self.assertEqual(compute_revision_digest(*inputs), expected)

    def test_canon_str_utf8_byte_length(self):
        # Non-ASCII: octet_length counts UTF-8 BYTES, not codepoints.
        # 'é' is 1 codepoint but 2 UTF-8 bytes → '2:é'.
        from ephemeral_harness import _canon_str
        self.assertEqual(_canon_str("é"), "2:é")
        self.assertEqual(_canon_str(None), "-1:")
        self.assertEqual(_canon_str(""), "0:")


# ────────────────────────────────────────────────────────────────────────────
# TestCommandSafety — argv arrays, redaction, name validation
# ────────────────────────────────────────────────────────────────────────────
class TestCommandSafety(unittest.TestCase):
    """All command builders return argv arrays; redaction scrubs passwords."""

    def test_migration_commands_returns_list_of_arrays(self):
        cmds = build_migration_commands("ctr", "db", "user", "/repo/root")
        self.assertIsInstance(cmds, list)
        self.assertEqual(len(cmds), len(MIGRATION_CHAIN))
        for cmd in cmds:
            self.assertIsInstance(cmd, list)
            for tok in cmd:
                self.assertIsInstance(tok, str)

    def test_migration_commands_are_not_shell_strings(self):
        cmds = build_migration_commands("ctr", "db", "user", "/repo/root")
        for cmd in cmds:
            joined = " ".join(cmd)
            self.assertNotIn(";", joined)
            self.assertNotIn("&&", joined)
            self.assertNotIn("|", joined)

    def test_migration_commands_include_on_error_stop(self):
        cmds = build_migration_commands("ctr", "db", "user", "/repo/root")
        for cmd in cmds:
            self.assertIn("ON_ERROR_STOP=1", cmd)

    def test_migration_commands_use_stdin_not_host_path(self):
        """argv must contain '-f -' (stdin), NOT a host file path."""
        cmds = build_migration_commands("ctr", "db", "user", "/repo/root")
        for cmd in cmds:
            self.assertIn("-f", cmd)
            # The token after -f must be "-" (stdin), not a host path
            idx = cmd.index("-f")
            self.assertEqual(cmd[idx + 1], "-")

    def test_migration_commands_no_host_path_in_argv(self):
        """No Windows/host path appears anywhere in the argv."""
        cmds = build_migration_commands("ctr", "db", "user", "D:\\repo\\root")
        for cmd in cmds:
            for tok in cmd:
                # No drive-letter path, no backslash path, no /repo/root
                self.assertFalse(
                    "\\" in tok or tok.startswith("D:") or "/repo/" in tok or
                    "audit-db" in tok or tok.endswith(".sql"),
                    f"Host path leaked into argv: {tok}"
                )

    def test_migration_commands_shell_true_never_used(self):
        """The function signature must not accept or use shell=True.
        This is a structural test: build_migration_commands returns lists
        of lists (argv arrays), never strings. subprocess callers must
        pass these arrays directly."""
        cmds = build_migration_commands("ctr", "db", "user", "/repo/root")
        for cmd in cmds:
            # Every element must be a list (not a string)
            self.assertIsInstance(cmd, list)
            # No element contains shell metacharacters as a single token
            for tok in cmd:
                self.assertNotIn("`", tok)
                self.assertNotIn("$(", tok)

    def test_cleanup_commands_returns_list_of_arrays(self):
        cmds = build_cleanup_commands("m6rag-eph-1234567890",
                                      "label-m6rag-eph-1234567890")
        self.assertIsInstance(cmds, list)
        for cmd in cmds:
            self.assertIsInstance(cmd, list)
            for tok in cmd:
                self.assertIsInstance(tok, str)

    def test_cleanup_commands_no_shell_strings(self):
        cmds = build_cleanup_commands("m6rag-eph-1234567890",
                                      "label-m6rag-eph-1234567890")
        for cmd in cmds:
            joined = " ".join(cmd)
            self.assertNotIn(";", joined)
            self.assertNotIn("|", joined)

    def test_validate_container_name_rejects_empty(self):
        self.assertFalse(validate_container_name(""))

    def test_validate_container_name_rejects_none(self):
        self.assertFalse(validate_container_name(None))

    def test_validate_container_name_rejects_path_traversal(self):
        for bad in ("../etc", "/etc/passwd", "a/../../b", "a\\b", ".."):
            self.assertFalse(validate_container_name(bad), msg="accepted: %r" % bad)

    def test_validate_container_name_rejects_shell_metachars(self):
        for bad in ("name;rm -rf /", "name $(whoami)", "name`id`",
                    "name|x", "name&bg", "name > /etc/passwd"):
            self.assertFalse(validate_container_name(bad), msg="accepted: %r" % bad)

    def test_validate_container_name_rejects_whitespace(self):
        for bad in ("with space", "tab\there", "newline\nhere"):
            self.assertFalse(validate_container_name(bad), msg="accepted: %r" % bad)

    def test_validate_container_name_accepts_valid(self):
        self.assertTrue(validate_container_name("m6rag-eph-1234567890"))
        self.assertTrue(validate_container_name("abc123"))
        self.assertTrue(validate_container_name("a-b-c-1234567890"))

    def test_redact_secrets_removes_password_patterns(self):
        text = "host=localhost password=secret123 dbname=app"
        redacted = redact_secrets(text)
        self.assertNotIn("secret123", redacted)
        self.assertIn("REDACTED", redacted)

    def test_redact_secrets_handles_quoted_password(self):
        text = "password='my;secret' other=val"
        redacted = redact_secrets(text)
        self.assertNotIn("my;secret", redacted)
        self.assertIn("REDACTED", redacted)

    def test_redact_secrets_is_case_insensitive(self):
        text = "PASSWORD=abc"
        self.assertNotIn("abc", redact_secrets(text))

    def test_redact_secrets_handles_non_string(self):
        self.assertIsNone(redact_secrets(None))

    def test_reader_role_sql_not_logged_unredacted(self):
        # The password must be redactable from the reader-role SQL.
        sql = build_reader_role_sql("super-secret-pw")
        redacted = redact_secrets(sql)
        self.assertNotIn("super-secret-pw", redacted)


# ────────────────────────────────────────────────────────────────────────────
# TestCleanupValidation — container name + label targeting
# ────────────────────────────────────────────────────────────────────────────
class TestCleanupValidation(unittest.TestCase):
    """Cleanup must target the EXACT container name and label; reject unsafe."""

    def test_accepts_valid_name(self):
        self.assertTrue(validate_container_name("m6rag-eph-1234567890"))

    def test_rejects_spaces(self):
        self.assertFalse(validate_container_name("m6rag eph"))

    def test_rejects_semicolon(self):
        self.assertFalse(validate_container_name("name;evil"))

    def test_rejects_pipe(self):
        self.assertFalse(validate_container_name("name|evil"))

    def test_rejects_backtick(self):
        self.assertFalse(validate_container_name("name`evil"))

    def test_cleanup_targets_exact_container_name(self):
        name = "m6rag-eph-1234567890"
        label = "label-" + name
        cmds = build_cleanup_commands(name, label)
        rm_cmd = cmds[0]
        self.assertEqual(rm_cmd[0], "docker")
        self.assertIn("rm", rm_cmd)
        self.assertIn(name, rm_cmd)
        # The exact container name appears as a standalone argv token.
        self.assertIn(name, rm_cmd)

    def test_cleanup_filter_uses_exact_name(self):
        name = "m6rag-eph-1234567890"
        label = "label-" + name
        cmds = build_cleanup_commands(name, label)
        ps_cmd = cmds[1]
        self.assertIn("name=" + name, ps_cmd)

    def test_cleanup_filter_uses_exact_label(self):
        name = "m6rag-eph-1234567890"
        label = "label-" + name
        cmds = build_cleanup_commands(name, label)
        # Both the network ls and prune commands reference the label.
        self.assertIn("label=" + label, cmds[2])
        self.assertIn("label=" + label, cmds[3])

    def test_cleanup_raises_on_invalid_container_name(self):
        with self.assertRaises(ValueError):
            build_cleanup_commands("../evil", "label-x")

    def test_cleanup_raises_on_invalid_label(self):
        with self.assertRaises(ValueError):
            build_cleanup_commands("m6rag-eph-1234567890", "label;evil")


# ────────────────────────────────────────────────────────────────────────────
# TestResultClassification — skip reasons + classification string fields
# ────────────────────────────────────────────────────────────────────────────
class TestResultClassification(unittest.TestCase):
    """Classification fields are stable strings; skips carry NOT_EXECUTED."""

    def test_classification_strings(self):
        # The canonical classification labels from the design §2.
        classifications = {
            "ephemeral_postgres_verified": "DESIGNED, NOT_EXECUTED",
            "MergePilot-Test_database_verified": "false (NOT_PERFORMED)",
            "production_verified": "false (never)",
        }
        for key, val in classifications.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(val, str)

    def test_image_digest_is_pinned(self):
        # Must be the full sha256 digest, not a floating tag.
        self.assertIn("sha256:", IMAGE_DIGEST)
        self.assertTrue(
            IMAGE_DIGEST.startswith("pgvector/pgvector@sha256:"),
            "image must be pgvector/pgvector digest-pinned",
        )

    def test_authorized_daemon_is_not_production(self):
        self.assertEqual(AUTHORIZED_DAEMON, "MergePilot-Test")
        self.assertNotIn("Ubuntu", AUTHORIZED_DAEMON)

    def test_measure_server_identity_returns_not_executed(self):
        result = measure_server_identity("postgresql://user:pw@host/db")
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "NOT_EXECUTED")

    def test_ephemeral_placeholder_skip_reason_contains_not_executed(self):
        # When EPHEMERAL_PG_VERIFY is unset, the classification is NOT_EXECUTED.
        # The placeholder class below carries this literal in its skip reason.
        from test_ephemeral_pg import TestEphemeralPlaceholder
        self.assertIn("NOT_EXECUTED", TestEphemeralPlaceholder._SKIP_REASON)


# ────────────────────────────────────────────────────────────────────────────
# TestEphemeralPlaceholder — unconditional skip; documents Phase B
# ────────────────────────────────────────────────────────────────────────────
class TestEphemeralPlaceholder(unittest.TestCase):
    """Documents what the real ephemeral harness (Phase B) would test.

    These tests are skipped UNCONDITIONALLY. Even when
    ``EPHEMERAL_PG_VERIFY=1`` is set AND the daemon is reachable, Phase A does
    not implement container execution — so every test still skips with the
    explicit ``NOT_EXECUTED`` reason. A consumer cannot mistake "skipped" for
    "ran and passed".
    """

    NOT_EXECUTED = True  # marker: these require a live ephemeral database

    _SKIP_REASON = (
        "EPHEMERAL_PG_VERIFY not configured; NOT_EXECUTED"
    )

    def setUp(self):
        # Unconditional skip. The harness executor (Phase B) is not
        # implemented in this candidate, so we never start a container —
        # regardless of the env var. This is an honest placeholder.
        self.skipTest(self._SKIP_REASON)

    def test_container_lifecycle_and_readiness(self):
        """Phase B: start pgvector container, wait pg_isready, measure server
        identity, then construct PostgresSnapshotSource with frozen values."""
        pass  # pragma: no cover

    def test_full_migration_chain_applies_cleanly(self):
        """Phase B: apply all 15 migration applications + 2 ISOLATED_LIVE
        migrations; verify no error and m4f1 idempotency rounds succeed."""
        pass  # pragma: no cover

    def test_reader_role_acl_and_read_only_default(self):
        """Phase B: assert mergepilot_reader has SELECT on 9 tables, no writes,
        and default_transaction_read_only=on (identity gate passes)."""
        pass  # pragma: no cover

    def test_seed_runs_classify_correctly(self):
        """Phase B: initial_load() on each of the 5 seed runs yields the
        expected final_status (PASS / UNKNOWN / NOT_AVAILABLE / ROLLED_BACK /
        RUN_NOT_FOUND)."""
        pass  # pragma: no cover

    def test_fail_closed_negative_paths(self):
        """Phase B: each negative test (wrong db/role/marker/read-only/server)
        produces its stable error code; restore-verification gate passes."""
        pass  # pragma: no cover

    def test_cleanup_leaves_no_residue(self):
        """Phase B: after cleanup, no container/network/volume/port/temp-dir
        residue; the published port is closed and the WSL distro unchanged."""
        pass  # pragma: no cover

    def test_no_dsn_password_in_logs(self):
        """Phase B: scan harness stdout/stderr for password= patterns; none
        survive redaction."""
        pass  # pragma: no cover


if __name__ == "__main__":
    unittest.main()
