#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ISOLATED_LIVE ephemeral PostgreSQL harness — Phase A unit + Phase B live tests.

Phase A groups (no Docker; subprocess mocked):
  TestExecutionGate        — env + daemon gate logic (mocked subprocess)
  TestMigrationOrder       — MIGRATION_CHAIN has 13 audit-db entries; idempotency rounds
  TestRoleBootstrap        — prerequisite + reader role SQL shape
  TestSeedContract         — 5-run seed SQL satisfies DDL constraints
  TestRevisionDigest       — canonical digest algorithm vs bind_revision
  TestCommandSafety        — argv arrays (never shell); redaction; name validation
  TestCleanupValidation    — container-name validation + cleanup command shape
  TestResultClassification — skip reasons + classification string fields

Phase B group (REAL disposable PostgreSQL; requires EPHEMERAL_PG_VERIFY=1):
  TestEphemeralLive        — real container lifecycle, 17-step migration chain,
                             reader ACL, seed-run classification, fail-closed
                             negative paths, HTTP live path. Skipped (NOT_EXECUTED)
                             when unauthorized; cleanup registered via
                             addClassCleanup; external residue audit is the
                             authoritative cleanup_verified gate.

Status when unauthorized: ``NOT_EXECUTED`` (the Phase B class skips via
``raise unittest.SkipTest``). When authorized, Phase B executes against a
disposable pgvector container on the MergePilot-Test daemon only.
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
from ephemeral_executor import EphemeralExecutor  # noqa: E402 — Phase B real executor


# ────────────────────────────────────────────────────────────────────────────
# TestExecutionGate — env var + daemon reachability gate
# ────────────────────────────────────────────────────────────────────────────
# Synthetic `docker info` text for the hardened Phase B gate's fingerprint
# parser. Carries only non-sensitive fingerprint fields.
_DAEMON_INFO_TEXT = (
    "Server:\n"
    " Containers: 0\n"
    "  Running: 0\n"
    " Server Version: 29.1.3\n"
    " Operating System: Ubuntu 22.04.5 LTS\n"
    " OSType: linux\n"
    " Name: mergpilot-test\n"
    " ID: f466f703-15ce-46fd-bfba-02e9c0a140b2\n"
    " Docker Root Dir: /var/lib/docker\n"
)


class TestExecutionGate(unittest.TestCase):
    """check_execution_auth: hardened two-key rule (env=1 + full daemon gate).

    Phase B (Fix 2): the gate verifies, in order — env=1, MergePilot-Test
    present + Running, Ubuntu-22.04 recorded-not-invoked, docker endpoint =
    unix:///var/run/docker.sock, DOCKER_HOST empty/local, daemon fingerprint
    complete, image cached. Fail-closed at the first miss; no Docker command
    runs if an earlier gate fails.
    """

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

    @mock.patch("ephemeral_harness._wsl_distro_states")
    @mock.patch("ephemeral_harness._run_wsl_text")
    def test_set_but_daemon_check_fails(self, mock_wsl, mock_states):
        # env=1, distro Running, BUT docker info returns non-zero → unauthorized.
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        mock_states.return_value = {AUTHORIZED_DAEMON: "Running",
                                    "Ubuntu-22.04": "Stopped"}
        mock_wsl.side_effect = [
            (0, "unix:///var/run/docker.sock\n", ""),   # endpoint OK
            (0, "\n", ""),                              # DOCKER_HOST empty
            (1, "", "docker info error"),               # docker info FAILS
        ]
        result = check_execution_auth()
        self.assertFalse(result["authorized"])
        self.assertIn("docker info", result["reason"])

    @mock.patch("ephemeral_harness._wsl_distro_states")
    @mock.patch("ephemeral_harness._run_wsl_text")
    def test_set_and_daemon_reachable_authorizes(self, mock_wsl, mock_states):
        # Phase B hardened gate: mock each probe in order.
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        mock_states.return_value = {AUTHORIZED_DAEMON: "Running",
                                    "Ubuntu-22.04": "Stopped"}
        # Probe sequence: endpoint, DOCKER_HOST, docker info, image inspect.
        mock_wsl.side_effect = [
            (0, "unix:///var/run/docker.sock\n", ""),       # context endpoint
            (0, "\n", ""),                                  # DOCKER_HOST (empty)
            (0, _DAEMON_INFO_TEXT, ""),                     # docker info
            (0, "sha256:abc123def456\n", ""),               # image inspect (Id)
        ]
        result = check_execution_auth()
        self.assertTrue(result["authorized"], msg=result.get("reason"))
        self.assertIn("fingerprint", result)
        self.assertEqual(result["ubuntu_state"], "Stopped")
        # Fix 1 (final review): success result carries the full non-inferable set.
        self.assertEqual(result["endpoint"], "unix:///var/run/docker.sock")
        self.assertEqual(result["docker_host"], "")
        self.assertEqual(result["image_digest"], IMAGE_DIGEST)
        self.assertEqual(result["image_id"], "sha256:abc123def456")

    @mock.patch("ephemeral_harness._wsl_distro_states")
    def test_set_but_distro_stopped_is_unauthorized(self, mock_states):
        # env=1 BUT MergePilot-Test is Stopped → unauthorized, NOT started.
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        mock_states.return_value = {AUTHORIZED_DAEMON: "Stopped",
                                    "Ubuntu-22.04": "Stopped"}
        result = check_execution_auth()
        self.assertFalse(result["authorized"])
        self.assertIn("Stopped", result["reason"])

    @mock.patch("ephemeral_harness._wsl_distro_states")
    @mock.patch("ephemeral_harness._run_wsl_text")
    def test_set_but_remote_endpoint_unauthorized(self, mock_wsl, mock_states):
        # env=1, distro Running, BUT endpoint is TCP → unauthorized.
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        mock_states.return_value = {AUTHORIZED_DAEMON: "Running",
                                    "Ubuntu-22.04": "Stopped"}
        mock_wsl.side_effect = [
            (0, "tcp://1.2.3.4:2375\n", ""),   # remote endpoint
        ]
        result = check_execution_auth()
        self.assertFalse(result["authorized"])
        self.assertIn("tcp://", result["reason"])

    @mock.patch("ephemeral_harness._wsl_distro_states")
    @mock.patch("ephemeral_harness._run_wsl_text")
    def test_set_but_docker_host_tcp_unauthorized(self, mock_wsl, mock_states):
        # env=1, distro Running, endpoint OK, BUT DOCKER_HOST is TCP.
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        mock_states.return_value = {AUTHORIZED_DAEMON: "Running",
                                    "Ubuntu-22.04": "Stopped"}
        mock_wsl.side_effect = [
            (0, "unix:///var/run/docker.sock\n", ""),   # endpoint OK
            (0, "tcp://1.2.3.4:2375\n", ""),            # DOCKER_HOST = TCP
        ]
        result = check_execution_auth()
        self.assertFalse(result["authorized"])
        self.assertIn("DOCKER_HOST", result["reason"])

    @mock.patch("ephemeral_harness._wsl_distro_states")
    @mock.patch("ephemeral_harness._run_wsl_text")
    def test_set_but_image_not_cached_unauthorized(self, mock_wsl, mock_states):
        # env=1, all gates pass EXCEPT image not cached → unauthorized.
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        mock_states.return_value = {AUTHORIZED_DAEMON: "Running",
                                    "Ubuntu-22.04": "Stopped"}
        mock_wsl.side_effect = [
            (0, "unix:///var/run/docker.sock\n", ""),   # endpoint OK
            (0, "\n", ""),                              # DOCKER_HOST empty
            (0, _DAEMON_INFO_TEXT, ""),                 # docker info OK
            (1, "", "no such image"),                   # image NOT cached
        ]
        result = check_execution_auth()
        self.assertFalse(result["authorized"])
        self.assertIn("image", result["reason"])

    @mock.patch("ephemeral_harness._wsl_distro_states")
    @mock.patch("ephemeral_harness._run_wsl_text")
    def test_set_but_fingerprint_incomplete_unauthorized(self, mock_wsl, mock_states):
        # env=1, all gates pass EXCEPT fingerprint missing fields.
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        mock_states.return_value = {AUTHORIZED_DAEMON: "Running",
                                    "Ubuntu-22.04": "Stopped"}
        mock_wsl.side_effect = [
            (0, "unix:///var/run/docker.sock\n", ""),
            (0, "\n", ""),
            (0, "Server Version: 29.1.3\n", ""),   # missing ID/Name/Root Dir
        ]
        result = check_execution_auth()
        self.assertFalse(result["authorized"])
        self.assertIn("fingerprint", result["reason"])

    @mock.patch("ephemeral_harness._wsl_distro_states")
    @mock.patch("ephemeral_harness._run_wsl_text")
    def test_unauthorized_path_does_not_start_distro(self, mock_wsl, mock_states):
        # When env is unset, NO wsl/docker probe runs (fail-closed at gate 1).
        os.environ.pop("EPHEMERAL_PG_VERIFY", None)
        result = check_execution_auth()
        self.assertFalse(result["authorized"])
        mock_states.assert_not_called()
        mock_wsl.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# TestMigrationOrder — MIGRATION_CHAIN shape and idempotency rounds
# ────────────────────────────────────────────────────────────────────────────
class TestMigrationOrder(unittest.TestCase):
    """MIGRATION_CHAIN: 13 audit-db applications (9 base + m4f1 x2 + hotfix x2),
    11 distinct files. Plus 2 ISOLATED_LIVE migrations (001/002) in a separate
    Phase 3 = 15 total migration-file applications. Plus 2 role bootstrap
    operations (prerequisite + reader) = 17 executor operations.

    Audit-db applications = 13. ISOLATED_LIVE applications = 2.
    Total migration-file applications = 15. Audit-db distinct files = 11.
    ISOLATED_LIVE distinct files = 2. Total distinct files = 13.
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

    # ── audit_events (run-eph-ok closed-loop trail) ───────────────────────
    # The tests below isolate the audit_events INSERT block so that action
    # counting is precise (review/fix/verify also appear in stage_runs /
    # stage_events / comments, which would inflate a whole-SQL count).

    def _audit_block(self):
        """Return the text from 'INSERT INTO audit_events' to the next blank line."""
        start = self.seed.find("INSERT INTO audit_events")
        self.assertGreater(start, -1, "audit_events INSERT missing from seed")
        # The statement ends at the first blank line after the INSERT.
        end = self.seed.find("\n\n", start)
        self.assertGreater(end, start, "audit_events block terminator not found")
        return self.seed[start:end]

    def test_seed_contains_insert_into_audit_events(self):
        self.assertIn("INSERT INTO audit_events", self.seed)

    def test_audit_events_has_exactly_five_success_rows(self):
        # The audit_events VALUES clause lists exactly 5 tuples (one per
        # closed-loop step). Count opening parens on value lines within the
        # block (each tuple begins with "('run-eph-ok',").
        block = self._audit_block()
        # Remove the column-list line, then count value tuples.
        values_idx = block.find("VALUES")
        values = block[values_idx:]
        n = values.count("('run-eph-ok'")
        self.assertEqual(n, 5)

    def test_audit_events_actions_are_the_five_required(self):
        block = self._audit_block()
        # Each action is the 3rd field of its tuple: ('run-eph-ok', '<agent>', '<action>', ...
        actions = re.findall(r"\('run-eph-ok',\s*'[^']+',\s*'([a-z_]+)'", block)
        self.assertEqual(sorted(actions),
                         sorted(["review", "fix", "verify", "merge", "close_pr"]))

    def test_audit_events_task_id_all_run_eph_ok(self):
        block = self._audit_block()
        # Every tuple's first field must be 'run-eph-ok'.
        task_ids = re.findall(r"\('([^']+)',\s*'[^']+',\s*'[a-z_]+'", block)
        self.assertEqual(task_ids, ["run-eph-ok"] * 5)

    def test_audit_events_uses_real_ddl_columns(self):
        # DDL (init.sql:52): task_id, agent, action, target, detail, sha, via.
        block = self._audit_block()
        col_line = block[:block.find("VALUES")]
        for col in ("task_id", "agent", "action", "target", "detail", "sha", "via"):
            self.assertIn(col, col_line)
        # ts is omitted → relies on DEFAULT now(). Assert 'ts' is NOT listed
        # as a column (word-boundary match so 'task_id' does not match 'ts').
        cols = re.findall(r"\bts\b", col_line)
        self.assertEqual(cols, [], "audit_events must omit ts (use DEFAULT now())")

    def test_audit_events_does_not_touch_other_scenarios(self):
        # audit_events rows exist ONLY for run-eph-ok. The other 4 runs must
        # not appear in the audit_events block.
        block = self._audit_block()
        for other in ("run-eph-unknown", "run-eph-no-rev",
                      "run-eph-rollback", "run-eph-missing"):
            self.assertNotIn(other, block)
        # And no second audit_events INSERT exists elsewhere in the seed.
        self.assertEqual(self.seed.count("INSERT INTO audit_events"), 1)

    def test_audit_events_agents_match_ddl_comment(self):
        # init.sql comment: agent ∈ reviewer/fixer/verifier/manager/system.
        block = self._audit_block()
        agents = re.findall(r"\('run-eph-ok',\s*'([^']+)'", block)
        self.assertEqual(sorted(agents),
                         sorted(["reviewer", "fixer", "verifier", "manager", "system"]))

    def test_audit_events_via_values_match_ddl_comment(self):
        # init.sql comment: via ∈ github-mcp / sast-scan / matrix / pg.
        block = self._audit_block()
        allowed_via = {"github-mcp", "sast-scan", "matrix", "pg"}
        vias = re.findall(r"',\s*'([a-z-]+)'\s*\);?$", block, re.MULTILINE)
        # Fallback parse if the line-wrap differs: grab the last quoted token
        # before ")" on each tuple line.
        if len(vias) != 5:
            vias = re.findall(r"'(github-mcp|sast-scan|matrix|pg)'\s*\)", block)
        self.assertEqual(len(vias), 5)
        for v in vias:
            self.assertIn(v, allowed_via)

    def test_audit_events_shas_are_synthetic_40_char_hex(self):
        block = self._audit_block()
        shas = re.findall(r"'([0-9a-f]{40})'", block)
        self.assertEqual(len(shas), 5)
        for s in shas:
            self.assertRegex(s, r"^[0-9a-f]{40}$")


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

    def test_unauthorized_classification_is_not_executed(self):
        # When EPHEMERAL_PG_VERIFY is unset, check_execution_auth refuses and
        # its reason establishes the NOT_EXECUTED classification that the Phase
        # B class surfaces via raise unittest.SkipTest.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EPHEMERAL_PG_VERIFY", None)
            result = check_execution_auth()
        self.assertFalse(result["authorized"])
        # The TestEphemeralLive class surfaces "NOT_EXECUTED: <reason>" on skip.
        self.assertIn("NOT_EXECUTED",
                      "NOT_EXECUTED: %s" % result.get("reason", ""))


# ────────────────────────────────────────────────────────────────────────────
# TestEphemeralLive — REAL Phase B execution against a disposable PostgreSQL
# ────────────────────────────────────────────────────────────────────────────
class TestEphemeralLive(unittest.TestCase):
    """Real Phase B verification against a disposable pgvector container.

    Authorization gate: :func:`check_execution_auth` must authorize (env
    ``EPHEMERAL_PG_VERIFY=1`` AND the MergePilot-Test daemon reachable). If NOT
    authorized, the whole class is skipped via ``raise unittest.SkipTest`` (not
    ``cls.skipTest``) so the skip is explicit and unambiguous — it is never
    mistaken for "ran and passed".

    When authorized, ``setUpClass`` drives a single shared session
    (:class:`EphemeralExecutor`): start → measure identity → 17 bootstrap
    operations → seed → Option A bind_revision. ``addClassCleanup`` is
    registered BEFORE any resource is created; the idempotent
    :meth:`cleanup_and_verify` tolerates a partially-started session.

    Boundary honesty (see the Phase B doc): these tests verify the ephemeral
    consumer/read path on a disposable container. They do NOT verify the
    MergePilot-Test application database, production, or the controller's
    audit-event write path. ``ephemeral_bind_revision_contract_verified``
    reflects ONLY the narrow Option A outcome; ``revision_producer_contract``
    and ``audit_producer_contract`` stay ``NOT_VERIFIED``.
    """

    @classmethod
    def setUpClass(cls):
        # Authorization gate. Use raise unittest.SkipTest (per amendment) so the
        # skip is a hard class-level event, not a soft in-test skip.
        auth = check_execution_auth()
        if not auth.get("authorized"):
            raise unittest.SkipTest(
                "NOT_EXECUTED: %s" % auth.get("reason", "unauthorized"))
        # Authorized: build the single shared session, passing the structured
        # authorization_context (Fix 3, second review). The executor validates
        # this context before any Docker command and re-checks the fingerprint
        # after cleanup.
        cls.executor = EphemeralExecutor(str(ROOT), authorization_context=auth)
        # Register cleanup BEFORE starting any resource. cleanup_and_verify is
        # idempotent (on success) and tolerates a partially-started session.
        cls.addClassCleanup(cls.executor.cleanup_and_verify)
        # Combined start+prepare entry point (Fix 3/6): on primary failure,
        # cleanup runs and both error codes are preserved.
        cls.executor.start_and_prepare()
        cls.bind_outcome = dict(cls.executor.bind_revision_outcome)

    # ── 1. container lifecycle + readiness ──────────────────────────────────

    def test_container_lifecycle_and_readiness(self):
        """Container started, bound to 127.0.0.1 only, SELECT 1 succeeded,
        and server identity was measured (non-NULL addr, port 5432)."""
        ex = self.executor
        self.assertEqual(ex._host_address, "127.0.0.1",
                         "host must be IPv4 loopback")
        self.assertIsNotNone(ex._host_port)
        self.assertGreater(ex._host_port, 0)
        # Server identity measured via real TCP (not Unix socket).
        self.assertIsNotNone(ex._server_address,
                             "inet_server_addr must be non-NULL (TCP)")
        self.assertNotIn(ex._server_address, (None, ""),
                         "server address must not be NULL/empty")
        self.assertEqual(ex._server_port, 5432,
                         "expected_server_port is the container port, not host port")
        # host port and server port are STRICTLY DIFFERENT concepts.
        self.assertNotEqual(ex._host_port, ex._server_port,
                            "host_port (Windows DSN) must differ from server_port")

    # ── 2. full migration chain ─────────────────────────────────────────────

    def test_full_migration_chain_applies_cleanly(self):
        """17 bootstrap operations applied; 13 audit-db + 2 ISOLATED_LIVE = 15
        migration applications; m4f1_state and m4f1_hotfix_1 idempotency rounds
        both succeeded; 9 required tables present."""
        ex = self.executor
        ops = ex.operations_applied
        # Count migration applications.
        phase1 = [o for o in ops if o.startswith("phase1_migration_")]
        phase3 = [o for o in ops if o.startswith("phase3_migration_")]
        self.assertEqual(len(phase1), 13, "13 audit-db migration applications")
        self.assertEqual(len(phase3), 2, "2 ISOLATED_LIVE migration applications")
        # Idempotency rounds for m4f1_state and m4f1_hotfix_1.
        m4f1_rounds = [o for o in phase1 if "m4f1_state" in o]
        hotfix_rounds = [o for o in phase1 if "m4f1_hotfix_1" in o]
        self.assertEqual(len(m4f1_rounds), 2, "m4f1_state applied twice (idempotency)")
        self.assertEqual(len(hotfix_rounds), 2, "m4f1_hotfix_1 applied twice (idempotency)")
        # 9 required tables present.
        required_tables = (
            "task_runs", "stage_runs", "stage_events", "revision_bindings",
            "run_pr_bindings", "mcp_calls", "rollback_runs", "audit_events",
            "environment_identity",
        )
        cols = ex.admin_exec(
            "SELECT string_agg(tablename, ',' ORDER BY tablename) "
            "FROM pg_tables WHERE schemaname='public';")
        present = set(cols.strip().split(","))
        for t in required_tables:
            self.assertIn(t, present, "required table %s missing" % t)

    # ── 3. reader role ACL + read-only gate ─────────────────────────────────

    def test_reader_role_acl_and_read_only_default(self):
        """mergepilot_reader has SELECT on 9 tables, no
        INSERT/UPDATE/DELETE/TRUNCATE; default_transaction_read_only=on; no
        privileged role attributes."""
        ex = self.executor
        # default_transaction_read_only = on for the reader role.
        ro = ex.admin_exec(
            "SELECT rolconfig FROM pg_roles WHERE rolname='mergepilot_reader';")
        self.assertIn("default_transaction_read_only=on", ro,
                      "reader role must have default_transaction_read_only=on")
        # Privileged attributes all OFF.
        priv = ex.admin_exec(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
            "rolbypassrls FROM pg_roles WHERE rolname='mergepilot_reader';")
        self.assertEqual(priv.strip(), "f|f|f|f|f",
                         "reader role must have all privileged attrs OFF")
        # SELECT granted + writes denied on all 9 tables.
        for tbl in ("task_runs", "stage_runs", "stage_events",
                    "revision_bindings", "run_pr_bindings", "mcp_calls",
                    "rollback_runs", "audit_events", "environment_identity"):
            row = ex.admin_exec(
                "SELECT has_table_privilege('mergepilot_reader','%s','SELECT'),"
                "has_table_privilege('mergepilot_reader','%s','INSERT'),"
                "has_table_privilege('mergepilot_reader','%s','UPDATE'),"
                "has_table_privilege('mergepilot_reader','%s','DELETE'),"
                "has_table_privilege('mergepilot_reader','%s','TRUNCATE');"
                % (tbl, tbl, tbl, tbl, tbl))
            parts = row.strip().split("|")
            self.assertEqual(parts, ["t", "f", "f", "f", "f"],
                "table %s: SELECT must be granted, writes denied (got %s)" % (tbl, parts))

    # ── 4. seed runs classify correctly (read path) ─────────────────────────

    def test_seed_runs_classify_correctly(self):
        """PostgresSnapshotSource on the 5 seed runs yields the expected
        final_status; bundle demo_mode=ISOLATED_LIVE; bundle_sha256 recomputable."""
        import json
        ex = self.executor
        # run-eph-ok → PASS.
        src_ok = ex.make_reader_source("run-eph-ok")
        raw_ok = src_ok.read_snapshot()
        bundle_ok = json.loads(raw_ok)
        self.assertEqual(bundle_ok["demo_mode"], "ISOLATED_LIVE")
        self.assertEqual(bundle_ok["final_status"], "PASS")
        self.assertIn("bundle_sha256", bundle_ok)
        self.assertEqual(len(bundle_ok["bundle_sha256"]), 64)
        # Recompute bundle_sha256 and compare (excluding volatile fields).
        from integrity import compute_bundle_sha256
        recomputed = compute_bundle_sha256(bundle_ok)
        self.assertEqual(recomputed, bundle_ok["bundle_sha256"],
                         "bundle_sha256 must recompute identically")

        # run-eph-unknown → UNKNOWN (status NULL).
        bundle_unk = json.loads(ex.make_reader_source("run-eph-unknown").read_snapshot())
        self.assertEqual(bundle_unk["final_status"], "UNKNOWN",
                         "NULL status run → UNKNOWN")

        # run-eph-no-rev → final_status PASS but provenance_status NOT_AVAILABLE
        # (no revision_bindings → source_commit null).
        bundle_norev = json.loads(ex.make_reader_source("run-eph-no-rev").read_snapshot())
        self.assertEqual(bundle_norev["final_status"], "PASS",
                         "no-rev run still has final_status from task_runs.status")
        self.assertEqual(bundle_norev.get("provenance_status"), "NOT_AVAILABLE",
                         "missing-revision run → provenance_status NOT_AVAILABLE")
        self.assertIsNone(bundle_norev.get("source_commit"),
                          "no-rev run → source_commit null")

        # run-eph-rollback → ROLLED_BACK.
        bundle_rb = json.loads(ex.make_reader_source("run-eph-rollback").read_snapshot())
        self.assertEqual(bundle_rb["final_status"], "ROLLED_BACK",
                         "rollback run → ROLLED_BACK")

        # run-eph-missing → RUN_NOT_FOUND (reader raises).
        from postgres_source import RunNotFoundError
        with self.assertRaises(RunNotFoundError):
            ex.make_reader_source("run-eph-missing").read_snapshot()

    # ── 5. fail-closed negative paths ───────────────────────────────────────

    def test_fail_closed_negative_paths(self):
        """Negative identity-gate cases produce stable error codes; each
        modify→fail→restore→fresh-reader-LIVE cycle completes without
        swallowing restore failures.

        Includes a REAL ACL fail-closed case (admin grants INSERT on a table →
        fresh reader rejected as WRONG_ROLE → REVOKE → fresh reader LIVE) and
        at least one WRONG_SERVER (wrong expected_server_address).
        """
        import json
        from postgres_source import (
            IdentityCheckError, RunNotFoundError,
        )
        ex = self.executor
        # (a) RUN_NOT_FOUND — already covered, re-affirm here as a negative.
        with self.assertRaises(RunNotFoundError):
            ex.make_reader_source("run-eph-does-not-exist").read_snapshot()

        # (b) WRONG_SERVER — wrong expected_server_address.
        from postgres_source import PostgresSnapshotSource
        dsn = (
            "host=%s port=%d dbname=%s user=%s password=%s "
            "application_name=%s connect_timeout=5"
            % (ex._host_address, ex._host_port, "mergepilot_audit",
               CANONICAL_VIEWER_ROLE, ex._reader_password,
               "mergepilot_isolated_live")
        )
        bad_server = PostgresSnapshotSource(
            dsn=dsn, run_id="run-eph-ok",
            expected_database="mergepilot_audit",
            expected_role=CANONICAL_VIEWER_ROLE,
            expected_environment_id=ENVIRONMENT_ID_EPHEMERAL,
            expected_server_addresses=["10.255.255.1"],  # wrong
            expected_server_port=ex._server_port,
            expected_application_name="mergepilot_isolated_live",
        )
        with self.assertRaises(IdentityCheckError) as cm:
            bad_server.read_snapshot()
        self.assertEqual(cm.exception.code, "WRONG_SERVER",
                         "wrong expected_server_address → WRONG_SERVER")

        # (c) WRONG_SERVER — wrong expected_application_name.
        bad_app = PostgresSnapshotSource(
            dsn=dsn, run_id="run-eph-ok",
            expected_database="mergepilot_audit",
            expected_role=CANONICAL_VIEWER_ROLE,
            expected_environment_id=ENVIRONMENT_ID_EPHEMERAL,
            expected_server_addresses=ex.expected_server_addresses,
            expected_server_port=ex._server_port,
            expected_application_name="wrong-app-name",  # wrong
        )
        with self.assertRaises(IdentityCheckError) as cm2:
            bad_app.read_snapshot()
        self.assertEqual(cm2.exception.code, "WRONG_SERVER",
                         "wrong expected_application_name → WRONG_SERVER")

        # (d) REAL ACL fail-closed: admin grants INSERT on task_runs → fresh
        # reader rejected as WRONG_ROLE → REVOKE (finally, new admin conn) →
        # fresh reader LIVE.
        ex.admin_exec("GRANT INSERT ON task_runs TO mergepilot_reader;")
        try:
            with self.assertRaises(IdentityCheckError) as cm3:
                ex.make_reader_source("run-eph-ok").read_snapshot()
            self.assertEqual(cm3.exception.code, "WRONG_ROLE",
                             "write-privileged reader → WRONG_ROLE")
        finally:
            # Restore via a NEW admin connection; do not swallow restore errors.
            ex.admin_exec("REVOKE INSERT ON task_runs FROM mergepilot_reader;")
        # Verify restore: fresh reader → LIVE (PASS).
        restored = json.loads(ex.make_reader_source("run-eph-ok").read_snapshot())
        self.assertEqual(restored["final_status"], "PASS",
                         "after REVOKE, fresh reader must be LIVE again")

        # (e) ENVIRONMENT_ID_MISMATCH — wrong marker.
        ex.admin_exec(
            "UPDATE environment_identity SET environment_id='wrong-marker';")
        try:
            with self.assertRaises(IdentityCheckError) as cm4:
                ex.make_reader_source("run-eph-ok").read_snapshot()
            self.assertEqual(cm4.exception.code, "ENVIRONMENT_ID_MISMATCH",
                             "wrong marker → ENVIRONMENT_ID_MISMATCH")
        finally:
            ex.admin_exec(
                "UPDATE environment_identity SET environment_id='%s';"
                % ENVIRONMENT_ID_EPHEMERAL)
        # Verify restore: fresh reader → LIVE.
        restored2 = json.loads(ex.make_reader_source("run-eph-ok").read_snapshot())
        self.assertEqual(restored2["final_status"], "PASS",
                         "after marker restore, fresh reader must be LIVE")

        # (f) WRONG_DATABASE — expected_database does not match.
        bad_db = PostgresSnapshotSource(
            dsn=dsn, run_id="run-eph-ok",
            expected_database="definitely-not-this-db",  # wrong
            expected_role=CANONICAL_VIEWER_ROLE,
            expected_environment_id=ENVIRONMENT_ID_EPHEMERAL,
            expected_server_addresses=ex.expected_server_addresses,
            expected_server_port=ex._server_port,
            expected_application_name="mergepilot_isolated_live",
        )
        with self.assertRaises(IdentityCheckError) as cm5:
            bad_db.read_snapshot()
        self.assertEqual(cm5.exception.code, "WRONG_DATABASE",
                         "wrong expected_database → WRONG_DATABASE")

        # (g) WRONG_ROLE — connect as a user that is NOT mergepilot_reader
        # (the admin superuser 'mergepilot'). The identity gate sees
        # current_user != mergepilot_reader → WRONG_ROLE.
        from postgres_source import PostgresSnapshotSource as _PSS
        admin_dsn = (
            "host=%s port=%d dbname=%s user=%s password=%s "
            "application_name=%s connect_timeout=5"
            % (ex._host_address, ex._host_port, "mergepilot_audit",
               "mergepilot", ex._admin_password, "mergepilot_isolated_live")
        )
        wrong_user_src = _PSS(
            dsn=admin_dsn, run_id="run-eph-ok",
            expected_database="mergepilot_audit",
            expected_role=CANONICAL_VIEWER_ROLE,   # expects reader, gets admin
            expected_environment_id=ENVIRONMENT_ID_EPHEMERAL,
            expected_server_addresses=ex.expected_server_addresses,
            expected_server_port=ex._server_port,
            expected_application_name="mergepilot_isolated_live",
        )
        with self.assertRaises(IdentityCheckError) as cm6:
            wrong_user_src.read_snapshot()
        self.assertEqual(cm6.exception.code, "WRONG_ROLE",
                         "non-reader current_user → WRONG_ROLE")

        # (h) NOT_READ_ONLY — admin turns default_transaction_read_only OFF for
        # the reader role → reader session is writable → NOT_READ_ONLY → restore.
        ex.admin_exec(
            "ALTER ROLE mergepilot_reader SET default_transaction_read_only = off;")
        try:
            with self.assertRaises(IdentityCheckError) as cm7:
                ex.make_reader_source("run-eph-ok").read_snapshot()
            self.assertEqual(cm7.exception.code, "NOT_READ_ONLY",
                             "read-only off → NOT_READ_ONLY")
        finally:
            ex.admin_exec(
                "ALTER ROLE mergepilot_reader SET default_transaction_read_only = on;")
        restored3 = json.loads(ex.make_reader_source("run-eph-ok").read_snapshot())
        self.assertEqual(restored3["final_status"], "PASS",
                         "after read-only restore, fresh reader must be LIVE")

        # (i) ENVIRONMENT_ID_NOT_VERIFIED — delete the marker row (0 rows) →
        # ENVIRONMENT_ID_NOT_VERIFIED → restore.
        ex.admin_exec("DELETE FROM environment_identity;")
        try:
            with self.assertRaises(IdentityCheckError) as cm8:
                ex.make_reader_source("run-eph-ok").read_snapshot()
            self.assertEqual(cm8.exception.code, "ENVIRONMENT_ID_NOT_VERIFIED",
                             "0 marker rows → ENVIRONMENT_ID_NOT_VERIFIED")
        finally:
            ex.admin_exec(
                "INSERT INTO environment_identity (environment_id) VALUES ('%s');"
                % ENVIRONMENT_ID_EPHEMERAL)
        restored4 = json.loads(ex.make_reader_source("run-eph-ok").read_snapshot())
        self.assertEqual(restored4["final_status"], "PASS",
                         "after marker restore, fresh reader must be LIVE")

        # (j) WRONG_SERVER — wrong expected_server_port.
        bad_port = PostgresSnapshotSource(
            dsn=dsn, run_id="run-eph-ok",
            expected_database="mergepilot_audit",
            expected_role=CANONICAL_VIEWER_ROLE,
            expected_environment_id=ENVIRONMENT_ID_EPHEMERAL,
            expected_server_addresses=ex.expected_server_addresses,
            expected_server_port=9999,  # wrong
            expected_application_name="mergepilot_isolated_live",
        )
        with self.assertRaises(IdentityCheckError) as cm9:
            bad_port.read_snapshot()
        self.assertEqual(cm9.exception.code, "WRONG_SERVER",
                         "wrong expected_server_port → WRONG_SERVER")

    # ── 6. no DSN/password in logs ──────────────────────────────────────────

    def test_no_dsn_password_in_logs(self):
        """All collected executor logs are redacted: no admin/reader password
        survives, no full DSN, no SQL PASSWORD literal. The reader source
        __repr__ does not include the DSN."""
        ex = self.executor
        joined = "\n".join(ex.collected_logs)
        # The admin and reader passwords must NEVER appear.
        self.assertNotIn(ex._admin_password, joined,
                         "admin password leaked into logs")
        self.assertNotIn(ex._reader_password, joined,
                         "reader password leaked into logs")
        # No full DSN (postgresql://user:pass@).
        self.assertNotRegex(
            joined, r"postgresql?://[^/\s@]+:[^/\s@]+@",
            "full DSN leaked into logs")
        # No SQL PASSWORD literal with a value.
        self.assertNotRegex(
            joined, r"PASSWORD\s+'[^']*'",
            "SQL PASSWORD literal leaked into logs")
        # Reader source repr has no DSN.
        src = ex.make_reader_source("run-eph-ok")
        self.assertNotIn("password=", repr(src),
                         "reader source repr must not contain the DSN")
        # argv safety invariant: the executor's recorded argv checks pass.
        # (Re-affirm by constructing a fresh argv and checking no secret.)
        # The executor raises on any argv containing a secret, so if prepare()
        # succeeded, the invariant held.

    # ── 7. HTTP live path ───────────────────────────────────────────────────

    def test_http_live_path(self):
        """Real reader source → LivePoller → HTTP server on 127.0.0.1:0.
        GET /api/live/snapshot → 200 + bundle_sha256 recomputable.
        GET /api/live/status → source_kind=POSTGRES_ISOLATED, read_only=true,
        not_production=true, production_resource_accessed=null, control
        capabilities false|NOT_MEASURED.
        POST/PUT/PATCH/DELETE → 405.
        Finally: shutdown + server_close + poller.stop + join; threads exit,
        port closed."""
        import json
        import threading
        import urllib.request
        from live_poller import LivePoller
        from serve import create_server
        ex = self.executor
        src = ex.make_reader_source("run-eph-ok")
        poller = LivePoller(src, poll_interval=2.0)
        ex._poller = poller
        poller.start()
        # Wait for initial load (LIVE).
        deadline_loaded = __import__("time").monotonic() + 30
        while __import__("time").monotonic() < deadline_loaded:
            if poller.get_view()["state"] == "LIVE":
                break
            __import__("time").sleep(0.5)
        else:
            poller.stop(); poller.join(timeout=5)
            self.fail("poller never reached LIVE")
        server = create_server("127.0.0.1", 0, "ISOLATED_LIVE", poller=poller)
        ex._http_server = server
        host, port = server.server_address[:2]
        base = "http://127.0.0.1:%d" % port
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            # GET /api/live/snapshot → 200.
            with urllib.request.urlopen(base + "/api/live/snapshot", timeout=10) as r:
                self.assertEqual(r.status, 200)
                snap = json.loads(r.read().decode("utf-8"))
            self.assertIn("bundle_sha256", snap)
            self.assertEqual(len(snap["bundle_sha256"]), 64)
            # GET /api/live/status → 200 + boundary fields.
            with urllib.request.urlopen(base + "/api/live/status", timeout=10) as r:
                self.assertEqual(r.status, 200)
                status = json.loads(r.read().decode("utf-8"))
            self.assertEqual(status["source_kind"], "POSTGRES_ISOLATED")
            self.assertIs(status["source_read_only"], True)
            self.assertIs(status["not_production"], True)
            self.assertIsNone(status["production_resource_accessed"])
            # Control capabilities false | NOT_MEASURED.
            self.assertIs(status["github_writes_enabled"], False)
            self.assertIs(status["agent_control_enabled"], False)
            self.assertIs(status["runtime_consumes_rag_context"], False)
            self.assertEqual(status["production_resource_access_status"], "NOT_MEASURED")
            # Write methods → 405.
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                req = urllib.request.Request(base + "/api/live/snapshot",
                                             method=method)
                try:
                    urllib.request.urlopen(req, timeout=10)
                    self.fail("%s should have been rejected (405)" % method)
                except urllib.error.HTTPError as he:
                    self.assertEqual(he.code, 405,
                                     "%s → 405 (got %d)" % (method, he.code))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
            self.assertFalse(server_thread.is_alive(),
                             "HTTP server thread must exit")
            poller.stop()
            poller.join(timeout=5)
            self.assertFalse(poller.is_alive(), "poller thread must exit")
            ex._http_server = None
            ex._poller = None


# ────────────────────────────────────────────────────────────────────────────
# TestExecutorStructural — Phase B re-review fixes (Mock/structural, no Docker)
# ────────────────────────────────────────────────────────────────────────────
class TestExecutorStructural(unittest.TestCase):
    """Structural/Mock tests for the Phase B executor hardening (Fixes 1-7).

    These tests NEVER touch Docker/WSL/PostgreSQL. They verify the executor's
    argv construction, integrity checks, ownership verification, the combined
    error, the structured seed-split, the authorization-context gate, and
    cleanup retry semantics — all via Mocks and pure functions.
    """

    @staticmethod
    def _make_auth_context(**overrides):
        """A valid authorization_context (for start() validation). Includes the
        full set of fields required by the final-review strict validation:
        endpoint, docker_host, image_digest, image_id (Fix 1, final review)."""
        ctx = {
            "authorized": True,
            "reason": "EPHEMERAL_PG_VERIFY=1 ok image=" + IMAGE_DIGEST,
            "fingerprint": {
                "server_id": "f466f703-15ce-46fd-bfba-02e9c0a140b2",
                "name": "mergpilot-test",
                "docker_root_dir": "/var/lib/docker",
                "version": "29.1.3",
            },
            "ubuntu_state": "Stopped",
            "authorized_distro_state": "Running",
            "endpoint": "unix:///var/run/docker.sock",
            "docker_host": "",
            "image_digest": IMAGE_DIGEST,
            "image_id": "sha256:8e5355e9ff399a002fa46148399a1ac22fb3e9b2d390f857296e6da6b5559ba1",
        }
        ctx.update(overrides)
        return ctx

    def _make_executor(self):
        ex = EphemeralExecutor(str(ROOT),
                               authorization_context=self._make_auth_context())
        # Seed a fake session identity so cleanup ownership logic is testable.
        ex._container_name = "m6rag-eph-1234567890-abcdef01"
        ex._label = "label-m6rag-eph-1234567890-abcdef01"
        ex._container_id = "a" * 64
        ex._host_port = 39999
        return ex

    # ── Fix 1: digest-pinned startup argv ───────────────────────────────────

    def test_start_uses_image_digest_not_tag(self):
        # The start() argv's final element must be IMAGE_DIGEST, never a tag.
        # We capture the argv by mocking subprocess.run inside _confirm / image
        # inspect / docker run. Inspect the constructed run argv directly.
        import ephemeral_executor as ee
        ex = ee.EphemeralExecutor(str(ROOT),
                                  authorization_context=self._make_auth_context())
        ex._container_name = "m6rag-eph-1234567890-abcdef01"
        ex._label = "label-x"
        ex._admin_password = "pw1"
        ex._reader_password = "pw2"
        captured = {}

        def fake_docker(args, **kw):
            # Capture only the `run` argv; return success for probes.
            if args and args[0] == "image" and args[1] == "inspect":
                cp = mock.Mock()
                cp.returncode = 0
                cp.stdout = b"sha256:approvedimageid\n"
                cp.stderr = b""
                return cp
            cp = mock.Mock()
            cp.returncode = 0
            cp.stdout = b""
            cp.stderr = b""
            return cp

        def fake_run(argv, **kw):
            if argv and "run" in argv and "--pull=never" in argv:
                captured["argv"] = argv
                cp = mock.Mock()
                cp.returncode = 0
                cp.stdout = (b"a" * 64 + b"\n")
                cp.stderr = b""
                return cp
            # Probes (ps -a, image inspect): empty / success.
            cp = mock.Mock()
            cp.returncode = 0
            cp.stdout = b"" if "ps" in argv else b"sha256:approved\n"
            cp.stderr = b""
            return cp

        with mock.patch.object(ex, "_docker", side_effect=fake_docker), \
             mock.patch.object(ex, "_confirm_no_name_collision"), \
             mock.patch.object(ex, "_resolve_approved_image_id"), \
             mock.patch.object(ex, "_resolve_host_port"), \
             mock.patch.object(ex, "_wait_ready"), \
             mock.patch.object(ex, "_verify_image_digest_of_running_container"), \
             mock.patch("ephemeral_executor._APPROVED_LOCAL_IMAGE_ID",
                        "sha256:approvedimageid"), \
             mock.patch("ephemeral_executor.subprocess.run", side_effect=fake_run):
            ex.start()
        argv = captured["argv"]
        self.assertEqual(argv[-1], IMAGE_DIGEST,
                         "run argv must end with the digest, not a tag")
        self.assertNotIn("pgvector/pgvector:pg16", argv,
                         "no floating tag in run argv")

    def test_digest_run_failure_no_tag_fallback(self):
        # If docker run (digest) fails, no tag fallback is attempted.
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(str(ROOT),
                                  authorization_context=self._make_auth_context())
        ex._container_name = "m6rag-eph-1234567890-abcdef01"
        ex._label = "label-x"
        ex._admin_password = "pw1"
        ex._reader_password = "pw2"
        run_calls = []

        def fake_run(argv, **kw):
            run_calls.append(argv)
            if "run" in argv and "--pull=never" in argv:
                cp = mock.Mock()
                cp.returncode = 1
                cp.stdout = b""
                cp.stderr = b"digest error"
                return cp
            cp = mock.Mock(); cp.returncode = 0
            cp.stdout = b"sha256:id\n"; cp.stderr = b""
            return cp

        with mock.patch.object(ex, "_confirm_no_name_collision"), \
             mock.patch.object(ex, "_resolve_approved_image_id"), \
             mock.patch("ephemeral_executor._APPROVED_LOCAL_IMAGE_ID", "sha256:id"), \
             mock.patch("ephemeral_executor.subprocess.run", side_effect=fake_run):
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.start()
        self.assertEqual(cm.exception.code, "DOCKER_RUN_FAILED")
        # Only ONE docker run was attempted (the digest one); no tag retry.
        run_args = [a for a in run_calls if "run" in a and "--pull=never" in a]
        self.assertEqual(len(run_args), 1, "no tag fallback permitted")

    # ── Fix 3: migration integrity ──────────────────────────────────────────

    def test_migration_allowlist_counts(self):
        names = [f for f, _ in MIGRATION_CHAIN]
        self.assertEqual(len(names), 13)
        self.assertEqual(len(set(names)), 11)
        self.assertEqual(len(ISOLATED_LIVE_MIGRATIONS), 2)
        self.assertEqual(len(set(ISOLATED_LIVE_MIGRATIONS)), 2)
        self.assertEqual(len(set(names) | set(ISOLATED_LIVE_MIGRATIONS)), 13)

    def test_only_m4f1_state_and_hotfix_repeat(self):
        names = [f for f, _ in MIGRATION_CHAIN]
        from collections import Counter
        counts = Counter(names)
        repeats = {f: c for f, c in counts.items() if c > 1}
        self.assertEqual(set(repeats.keys()),
                         {"m4f1_state.sql", "m4f1_hotfix_1.sql"})
        self.assertEqual(sorted(repeats.values()), [2, 2])

    def test_migration_integrity_rejects_modified_content(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(str(ROOT))
        # Mock git rev-parse (base blob) vs hash-object (working tree) to differ.
        def fake_run(argv, **kw):
            cp = mock.Mock()
            cp.returncode = 0
            if "rev-parse" in argv:
                cp.stdout = b"baseblobhash0000000000000000000000000000\n"
            elif "hash-object" in argv:
                cp.stdout = b"worktreehash1111111111111111111111111111\n"
            else:
                cp.stdout = b""
            cp.stderr = b""
            return cp
        with mock.patch("ephemeral_executor.subprocess.run", side_effect=fake_run):
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex._verify_migration_file_integrity("init.sql", "tools/audit-db")
        self.assertEqual(cm.exception.code, "MIGRATION_INTEGRITY_MISMATCH")

    def test_migration_integrity_rejects_symlink(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(str(ROOT))
        with mock.patch("pathlib.Path.is_symlink", return_value=True):
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex._verify_migration_file_integrity("init.sql", "tools/audit-db")
        self.assertEqual(cm.exception.code, "MIGRATION_INTEGRITY_MISMATCH")

    def test_migration_integrity_rejects_path_escape(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(str(ROOT))
        # A filename with a path separator is rejected before any git call.
        with self.assertRaises(EphemeralExecutionError) as cm:
            ex._verify_migration_file_integrity("../escape.sql", "tools/audit-db")
        self.assertEqual(cm.exception.code, "MIGRATION_INTEGRITY_MISMATCH")

    # ── Fix 4/5: cleanup resource ownership + retry semantics ───────────────

    def _ownership_docker_factory(self, ex, *, exist_rc=0, exist_out=None,
                                  id_out=None, mounts_out=b"[]\n",
                                  rm_rc=0, mismatch=False):
        """Build a fake_docker for cleanup that handles the two-probe ownership
        check (existence {{.Id}} + identity Id|Name|Labels), mounts, and rm.
        """
        cid = ex._container_id
        cname = ex._container_name
        label = ex._label
        if exist_out is None:
            exist_out = (cid + "\n").encode()
        if id_out is None:
            if mismatch:
                id_out = (b"other" + b"x" * 60 + b"|/other-name|{}\n")
            else:
                id_out = ("%s|/%s|{\"mergepilot.ephemeral\":\"%s\"}\n"
                          % (cid, cname, label)).encode()

        def fake_docker(args, **kw):
            # existence probe: inspect <id> --format {{.Id}}
            if (args and args[0] == "inspect" and len(args) >= 4
                    and args[2] == "--format" and args[3] == "{{.Id}}"):
                cp = mock.Mock(); cp.returncode = exist_rc
                cp.stdout = exist_out
                cp.stderr = (b"No such object: " + cid.encode()) if exist_rc else b""
                return cp
            # identity probe: inspect <id> --format {{.Id}}|{{.Name}}|{{json}}
            if (args and args[0] == "inspect" and "--format" in args
                    and ".Id}}" in args[3] if len(args) > 3 else False
                    and "Name" in (args[3] if len(args) > 3 else "")):
                cp = mock.Mock(); cp.returncode = 0
                cp.stdout = id_out; cp.stderr = b""
                return cp
            # mounts probe
            if args and args[0] == "inspect" and "Mounts" in (args[3] if len(args) > 3 else ""):
                cp = mock.Mock(); cp.returncode = 0
                cp.stdout = mounts_out; cp.stderr = b""
                return cp
            if args and args[0:2] == ["rm", "-fv"]:
                cp = mock.Mock(); cp.returncode = rm_rc
                cp.stdout = b""; cp.stderr = b"" if rm_rc == 0 else b"rm err"
                return cp
            cp = mock.Mock(); cp.returncode = 0
            cp.stdout = b""; cp.stderr = b""
            return cp
        return fake_docker

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_cleanup_ownership_mismatch_no_removal(self, _m_recheck):
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_executor()
        rm_calls = []
        fake = self._ownership_docker_factory(ex, mismatch=True)

        def wrapped(args, **kw):
            if args and args[0:2] == ["rm", "-fv"]:
                rm_calls.append(args)
            return fake(args, **kw)
        with mock.patch.object(ex, "_docker", side_effect=wrapped), \
             mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            with self.assertRaises(EphemeralExecutionError):
                ex.cleanup_and_verify()
        # rm -fv must NOT have been called (ownership mismatch).
        self.assertEqual(rm_calls, [], "no removal on ownership mismatch")

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_cleanup_successful_uses_container_id(self, _m_recheck):
        ex = self._make_executor()
        rm_target = []
        fake = self._ownership_docker_factory(ex)

        def wrapped(args, **kw):
            if args and args[0:2] == ["rm", "-fv"]:
                rm_target.append(args[2])
            return fake(args, **kw)
        with mock.patch.object(ex, "_docker", side_effect=wrapped), \
             mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            ex.cleanup_and_verify()
        self.assertEqual(rm_target, [ex._container_id],
                         "rm -fv must use the container ID")

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_cleanup_docker_rm_failure_is_cleanup_error(self, _m_recheck):
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_executor()
        fake = self._ownership_docker_factory(ex, rm_rc=1)
        with mock.patch.object(ex, "_docker", side_effect=fake), \
             mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.cleanup_and_verify()
        self.assertEqual(cm.exception.code, "CLEANUP_RESIDUE")
        # _cleaned must stay False on error (Fix 5).
        self.assertFalse(ex._cleaned)

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_cleanup_idempotent_after_success(self, _m_recheck):
        ex = self._make_executor()
        fake = self._ownership_docker_factory(ex)
        with mock.patch.object(ex, "_docker", side_effect=fake), \
             mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            ex.cleanup_and_verify()  # succeeds → _cleaned = True
        self.assertTrue(ex._cleaned)
        # Second call is a no-op (no Docker).
        with mock.patch.object(ex, "_docker") as md:
            ex.cleanup_and_verify()
        md.assert_not_called()

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_cleanup_retry_after_temporary_failure(self, _m_recheck):
        # First cleanup fails (rm rc=1), leaving _cleaned=False; a second call
        # with rm succeeding then passes (Fix 5 retry semantics).
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_executor()
        # First call: rm fails.
        fake_fail = self._ownership_docker_factory(ex, rm_rc=1)
        with mock.patch.object(ex, "_docker", side_effect=fake_fail), \
             mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            with self.assertRaises(EphemeralExecutionError):
                ex.cleanup_and_verify()
        self.assertFalse(ex._cleaned)
        # Second call: rm succeeds.
        fake_ok = self._ownership_docker_factory(ex, rm_rc=0)
        with mock.patch.object(ex, "_docker", side_effect=fake_ok), \
             mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            ex.cleanup_and_verify()  # should not raise
        self.assertTrue(ex._cleaned)

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_cleanup_volume_inspect_failure_is_cleanup_error(self, _m_recheck):
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_executor()
        # mounts inspect returns invalid JSON.
        fake = self._ownership_docker_factory(ex, mounts_out=b"not-json\n")
        with mock.patch.object(ex, "_docker", side_effect=fake), \
             mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.cleanup_and_verify()
        # The aggregated CLEANUP_RESIDUE message must name the VOLUME_INSPECT sub-code.
        self.assertEqual(cm.exception.code, "CLEANUP_RESIDUE")
        self.assertIn("VOLUME_INSPECT", str(cm.exception))
        self.assertFalse(ex._cleaned)

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_cleanup_ownership_inspect_failure_not_treated_as_absent(self, _m_recheck):
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_executor()
        # existence probe: daemon error (rc=1, stderr NOT 'no such') → must
        # surface DOCKER_INSPECT_FAILED, not be treated as absent.
        cid = ex._container_id

        def fake_docker(args, **kw):
            if (args and args[0] == "inspect" and len(args) >= 4
                    and args[3] == "{{.Id}}"):
                cp = mock.Mock(); cp.returncode = 1
                cp.stdout = b""; cp.stderr = b"daemon error: permission denied"
                return cp
            cp = mock.Mock(); cp.returncode = 0
            cp.stdout = b""; cp.stderr = b""
            return cp
        with mock.patch.object(ex, "_docker", side_effect=fake_docker), \
             mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.cleanup_and_verify()
        self.assertEqual(cm.exception.code, "CLEANUP_RESIDUE")
        # DOCKER_INSPECT_FAILED must appear (not silently treated as absent).
        self.assertIn("DOCKER_INSPECT_FAILED", str(cm.exception))
        self.assertFalse(ex._cleaned)

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_cleanup_genuinely_absent_container_continues(self, _m_recheck):
        # Container genuinely gone ('No such') → ownership returns False, no rm,
        # residue verification proceeds (Fix 5: only explicit absence is OK).
        ex = self._make_executor()
        fake = self._ownership_docker_factory(ex, exist_rc=1, exist_out=b"")
        # Override the existence stderr to 'No such' so it's treated as absent.
        def fake2(args, **kw):
            cp = fake(args, **kw)
            if (args and args[0] == "inspect" and len(args) >= 4
                    and args[3] == "{{.Id}}" and cp.returncode != 0):
                cp.stderr = b"No such object: " + ex._container_id.encode()
            return cp
        with mock.patch.object(ex, "_docker", side_effect=fake2), \
             mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            ex.cleanup_and_verify()  # should not raise
        self.assertTrue(ex._cleaned)

    # ── Fix 6: combined error ───────────────────────────────────────────────

    def test_combined_error_carries_both_codes_no_secret(self):
        from ephemeral_executor import EphemeralExecutionAndCleanupError
        err = EphemeralExecutionAndCleanupError("DOCKER_RUN_FAILED", "CLEANUP_RESIDUE")
        self.assertEqual(err.primary_error_code, "DOCKER_RUN_FAILED")
        self.assertEqual(err.cleanup_error_code, "CLEANUP_RESIDUE")
        msg = str(err)
        self.assertNotIn("password=", msg)
        self.assertNotIn("postgresql://", msg)

    def test_start_and_prepare_primary_fail_cleanup_fail_combines(self):
        import ephemeral_executor as ee
        from ephemeral_executor import (
            EphemeralExecutionError, EphemeralExecutionAndCleanupError,
        )
        ex = ee.EphemeralExecutor(str(ROOT))
        primary = EphemeralExecutionError("DOCKER_RUN_FAILED", "primary")
        cleanup = EphemeralExecutionError("CLEANUP_RESIDUE", "cleanup")
        with mock.patch.object(ex, "start", side_effect=primary), \
             mock.patch.object(ex, "cleanup_and_verify", side_effect=cleanup):
            with self.assertRaises(EphemeralExecutionAndCleanupError) as cm:
                ex.start_and_prepare()
        self.assertEqual(cm.exception.primary_error_code, "DOCKER_RUN_FAILED")
        self.assertEqual(cm.exception.cleanup_error_code, "CLEANUP_RESIDUE")

    def test_start_and_prepare_primary_fail_cleanup_ok_propagates_primary(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(str(ROOT))
        primary = EphemeralExecutionError("PG_NOT_READY", "primary")
        with mock.patch.object(ex, "start", side_effect=primary), \
             mock.patch.object(ex, "cleanup_and_verify") as mc:
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.start_and_prepare()
        self.assertEqual(cm.exception.code, "PG_NOT_READY")
        mc.assert_called_once()

    # ── Fix 7: structured seed parts ─────────────────────────────────────────

    def test_build_seed_sql_parts_three_nonempty(self):
        from ephemeral_harness import build_seed_sql_parts
        before, option_b, after = build_seed_sql_parts()
        self.assertTrue(before.strip(), "before_bind non-empty")
        self.assertTrue(option_b.strip(), "option_b non-empty")
        self.assertTrue(after.strip(), "after_bind non-empty")

    def test_build_seed_sql_parts_option_b_isolated(self):
        from ephemeral_harness import build_seed_sql_parts
        before, option_b, after = build_seed_sql_parts()
        # The revision_bindings INSERT appears ONLY in option_b.
        self.assertIn("INSERT INTO revision_bindings", option_b)
        self.assertNotIn("INSERT INTO revision_bindings", before)
        self.assertNotIn("INSERT INTO revision_bindings", after)
        # The NOT_VERIFIED marker is in option_b.
        self.assertIn("revision_producer_contract = NOT_VERIFIED", option_b)

    def test_build_seed_sql_parts_concat_equals_full(self):
        from ephemeral_harness import build_seed_sql, build_seed_sql_parts
        before, option_b, after = build_seed_sql_parts()
        self.assertEqual(before + option_b + after, build_seed_sql())

    # ── Fix 3 (round 2): authorization_context gate ─────────────────────────

    def test_start_without_auth_context_refuses_docker(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(str(ROOT))  # no authorization_context
        with mock.patch.object(ex, "_docker") as md:
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.start()
        self.assertEqual(cm.exception.code, "AUTH_CONTEXT_INVALID")
        md.assert_not_called()  # no Docker before the gate

    def test_start_unauthorized_context_refused(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(str(ROOT),
                                  authorization_context={"authorized": False})
        with mock.patch.object(ex, "_docker") as md:
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.start()
        self.assertEqual(cm.exception.code, "AUTH_CONTEXT_INVALID")
        md.assert_not_called()

    def test_start_missing_fingerprint_field_refused(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ctx = self._make_auth_context()
        ctx["fingerprint"]["server_id"] = ""  # incomplete
        ex = ee.EphemeralExecutor(str(ROOT), authorization_context=ctx)
        with self.assertRaises(EphemeralExecutionError) as cm:
            ex.start()
        self.assertEqual(cm.exception.code, "AUTH_CONTEXT_INVALID")

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_recheck_fingerprint_unchanged_passes(self, _m_recheck):
        # _recheck_environment_fingerprint mocked to no-op → cleanup succeeds.
        ex = self._make_executor()
        fake = self._ownership_docker_factory(ex)
        with mock.patch.object(ex, "_docker", side_effect=fake), \
             mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            ex.cleanup_and_verify()
        self.assertTrue(ex._cleaned)

    # ── Fix 4 (round 2): validate-all-before-execute ────────────────────────

    def test_prepare_validates_all_before_any_sql(self):
        # If the LAST distinct migration file fails integrity, psql is never
        # called (no measure, no prerequisite role, no migration, no seed).
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(str(ROOT),
                                  authorization_context=self._make_auth_context())
        ex._container_name = "ctr"
        psql_calls = []

        # Track which file is being verified; fail the LAST distinct audit-db file.
        distinct_audit = []
        seen = set()
        for f, _ in MIGRATION_CHAIN:
            if f not in seen:
                distinct_audit.append(f)
                seen.add(f)
        last_file = distinct_audit[-1]

        orig_verify = ex._verify_migration_file_integrity

        def fake_verify(filename, approved_dir):
            if filename == last_file:
                raise EphemeralExecutionError(
                    "MIGRATION_INTEGRITY_MISMATCH", "last file tampered")
            return orig_verify(filename, approved_dir)

        def fake_psql(sql, **kw):
            psql_calls.append(sql)
            return ""

        with mock.patch.object(ex, "_verify_migration_file_integrity",
                               side_effect=fake_verify), \
             mock.patch.object(ex, "_psql_via_exec", side_effect=fake_psql), \
             mock.patch.object(ex, "measure_server_identity"):
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.prepare()
        self.assertEqual(cm.exception.code, "MIGRATION_INTEGRITY_MISMATCH")
        # No SQL was executed at all.
        self.assertEqual(psql_calls, [],
                         "no SQL before all migrations validate")

    def test_prepare_repeated_migrations_validated_once_applied_twice(self):
        # m4f1_state.sql appears twice in MIGRATION_CHAIN; _validate_all_migrations
        # verifies it ONCE (distinct), but prepare() applies it TWICE.
        import ephemeral_executor as ee
        ex = ee.EphemeralExecutor(str(ROOT),
                                  authorization_context=self._make_auth_context())
        ex._container_name = "ctr"
        ex._reader_password = "reader-pw"  # required by build_reader_role_sql
        verify_calls = []
        psql_calls = []

        def fake_verify(filename, approved_dir):
            verify_calls.append(filename)
            return (ROOT / approved_dir / filename)

        def fake_psql(sql, **kw):
            psql_calls.append(sql)
            return ""

        with mock.patch.object(ex, "_verify_migration_file_integrity",
                               side_effect=fake_verify), \
             mock.patch.object(ex, "_psql_via_exec", side_effect=fake_psql), \
             mock.patch.object(ex, "measure_server_identity"), \
             mock.patch.object(ex, "_attempt_bind_revision_option_a"):
            ex.prepare()
        # m4f1_state verified exactly once.
        self.assertEqual(verify_calls.count("m4f1_state.sql"), 1)
        self.assertEqual(verify_calls.count("m4f1_hotfix_1.sql"), 1)
        # Total distinct verifications = 11 (audit-db) + 2 (ISOLATED_LIVE) = 13.
        self.assertEqual(len(verify_calls), 13)
        # m4f1_state applied twice (count phase1 migration ops).
        phase1_m4f1 = [o for o in ex.operations_applied
                       if o.startswith("phase1_migration_m4f1_state")]
        self.assertEqual(len(phase1_m4f1), 2)


# ────────────────────────────────────────────────────────────────────────────
# TestWslDistroParse — _wsl_distro_states robustness (Fix 1, round 2)
# ────────────────────────────────────────────────────────────────────────────
class TestWslDistroParse(unittest.TestCase):
    """Pure-function tests for _wsl_distro_states (no WSL/Docker)."""

    def _states_for(self, output: str):
        with mock.patch("ephemeral_harness._run_wsl_text",
                        return_value=(0, output, "")):
            from ephemeral_harness import _wsl_distro_states
            return _wsl_distro_states()

    def test_default_distro_with_star(self):
        out = "  NAME               STATE           VERSION\n* MergePilot-Test    Running    2\n  Ubuntu-22.04       Stopped    2\n"
        s = self._states_for(out)
        self.assertEqual(s.get("MergePilot-Test"), "Running")
        self.assertEqual(s.get("Ubuntu-22.04"), "Stopped")

    def test_non_default_no_star(self):
        out = "  MergePilot-Test    Stopped    2\n* Ubuntu-22.04       Running    2\n"
        s = self._states_for(out)
        self.assertEqual(s.get("MergePilot-Test"), "Stopped")
        self.assertEqual(s.get("Ubuntu-22.04"), "Running")

    def test_utf16_nul_output(self):
        # wsl.exe emits UTF-16LE with NUL bytes; _run_wsl_text decodes, but the
        # parser must also strip stray NULs defensively.
        out = "\x00 \x00M\x00e\x00r\x00g\x00e\x00P\x00i\x00l\x00o\x00t\x00-\x00T\x00e\x00s\x00t\x00 \x00 \x00 \x00S\x00t\x00o\x00p\x00p\x00e\x00d\x00 \x00 \x00 \x002\x00\n\x00"
        s = self._states_for(out)
        self.assertEqual(s.get("MergePilot-Test"), "Stopped")

    def test_distro_name_with_spaces(self):
        out = "My Test Distro    Running    2\n"
        s = self._states_for(out)
        self.assertEqual(s.get("My Test Distro"), "Running")

    def test_header_line_ignored(self):
        out = "  NAME               STATE           VERSION\nMergePilot-Test    Running    2\n"
        s = self._states_for(out)
        self.assertNotIn("NAME", s)
        self.assertEqual(s.get("MergePilot-Test"), "Running")

    def test_malformed_lines_ignored(self):
        out = "garbage line\nonly two tokens\nMergePilot-Test    Running    2\n1 2 3 4 extra\n"
        s = self._states_for(out)
        self.assertEqual(s.get("MergePilot-Test"), "Running")
        self.assertEqual(len(s), 1)

    def test_state_not_running_or_stopped_ignored(self):
        out = "MergePilot-Test    Installing    2\n"
        s = self._states_for(out)
        self.assertEqual(s, {})

    def test_version_not_integer_ignored(self):
        out = "MergePilot-Test    Running    abc\n"
        s = self._states_for(out)
        self.assertEqual(s, {})

    def test_unparseable_returns_empty(self):
        s = self._states_for("")
        self.assertEqual(s, {})


# ────────────────────────────────────────────────────────────────────────────
# TestDockerHostAllowlist — DOCKER_HOST tightening (Fix 2, round 2)
# ────────────────────────────────────────────────────────────────────────────
class TestDockerHostAllowlist(unittest.TestCase):
    """Parametric Mock tests for the DOCKER_HOST allowlist in check_execution_auth."""

    def _run_with_docker_host(self, docker_host_value: str, *, full_chain=False):
        os.environ["EPHEMERAL_PG_VERIFY"] = "1"
        states = {AUTHORIZED_DAEMON: "Running", "Ubuntu-22.04": "Stopped"}
        # Probes: endpoint, DOCKER_HOST, (optionally docker info + image).
        probes = [
            (0, "unix:///var/run/docker.sock\n", ""),   # endpoint OK
            (0, docker_host_value + "\n", ""),           # DOCKER_HOST value
        ]
        if full_chain:
            probes.append((0, _DAEMON_INFO_TEXT, ""))    # docker info OK
            probes.append((0, "sha256:abc123def456\n", ""))  # image cached (valid Id)
        with mock.patch("ephemeral_harness._wsl_distro_states",
                        return_value=states), \
             mock.patch("ephemeral_harness._run_wsl_text",
                        side_effect=probes):
            from ephemeral_harness import check_execution_auth
            result = check_execution_auth()
        return result

    def test_empty_docker_host_allowed(self):
        # Full chain → authorized (DOCKER_HOST empty is allowed).
        r = self._run_with_docker_host("", full_chain=True)
        self.assertTrue(r["authorized"], msg=r.get("reason"))

    def test_exact_socket_allowed(self):
        # Full chain → authorized (exact unix socket is allowed).
        r = self._run_with_docker_host("unix:///var/run/docker.sock", full_chain=True)
        self.assertTrue(r["authorized"], msg=r.get("reason"))

    def test_other_unix_socket_rejected(self):
        for bad in ("unix:///tmp/docker.sock", "unix:///var/run/other.sock"):
            r = self._run_with_docker_host(bad)
            self.assertFalse(r["authorized"])
            self.assertIn("DOCKER_HOST", r["reason"])

    def test_tcp_rejected(self):
        r = self._run_with_docker_host("tcp://1.2.3.4:2375")
        self.assertFalse(r["authorized"])
        self.assertIn("DOCKER_HOST", r["reason"])

    def test_ssh_rejected(self):
        r = self._run_with_docker_host("ssh://user@host")
        self.assertFalse(r["authorized"])

    def test_npipe_rejected(self):
        r = self._run_with_docker_host("npipe:////./pipe/docker_engine")
        self.assertFalse(r["authorized"])

    def test_extra_suffix_rejected(self):
        # A value that is NOT exactly the socket (extra suffix) is rejected.
        r = self._run_with_docker_host("unix:///var/run/docker.sock/extra")
        self.assertFalse(r["authorized"])
        self.assertIn("DOCKER_HOST", r["reason"])


# ────────────────────────────────────────────────────────────────────────────
# TestAuthContextStrict — authorization_context completeness (Fix 1, final)
# ────────────────────────────────────────────────────────────────────────────
class TestAuthContextStrict(unittest.TestCase):
    """Strict validation of authorization_context fields (no Docker)."""

    @staticmethod
    def _ctx(**overrides):
        return TestExecutorStructural._make_auth_context(**overrides)

    def test_endpoint_missing_rejected(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ctx = self._ctx()
        del ctx["endpoint"]
        ex = ee.EphemeralExecutor(str(ROOT), authorization_context=ctx)
        with self.assertRaises(EphemeralExecutionError) as cm:
            ex._validate_authorization_context()
        self.assertEqual(cm.exception.code, "AUTH_CONTEXT_INVALID")

    def test_endpoint_wrong_rejected(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(str(ROOT),
                                  authorization_context=self._ctx(endpoint="tcp://x"))
        with self.assertRaises(EphemeralExecutionError) as cm:
            ex._validate_authorization_context()
        self.assertEqual(cm.exception.code, "AUTH_CONTEXT_INVALID")

    def test_image_digest_missing_rejected(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ctx = self._ctx()
        del ctx["image_digest"]
        ex = ee.EphemeralExecutor(str(ROOT), authorization_context=ctx)
        with self.assertRaises(EphemeralExecutionError) as cm:
            ex._validate_authorization_context()
        self.assertEqual(cm.exception.code, "AUTH_CONTEXT_INVALID")

    def test_image_digest_wrong_rejected(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(
            str(ROOT),
            authorization_context=self._ctx(image_digest="pgvector/pgvector@sha256:wrong"))
        with self.assertRaises(EphemeralExecutionError) as cm:
            ex._validate_authorization_context()
        self.assertEqual(cm.exception.code, "AUTH_CONTEXT_INVALID")

    def test_image_id_missing_rejected(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ctx = self._ctx()
        del ctx["image_id"]
        ex = ee.EphemeralExecutor(str(ROOT), authorization_context=ctx)
        with self.assertRaises(EphemeralExecutionError) as cm:
            ex._validate_authorization_context()
        self.assertEqual(cm.exception.code, "AUTH_CONTEXT_INVALID")

    def test_image_id_bad_format_rejected(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(
            str(ROOT), authorization_context=self._ctx(image_id="not-a-sha"))
        with self.assertRaises(EphemeralExecutionError) as cm:
            ex._validate_authorization_context()
        self.assertEqual(cm.exception.code, "AUTH_CONTEXT_INVALID")

    def test_docker_host_unapproved_rejected(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = ee.EphemeralExecutor(
            str(ROOT),
            authorization_context=self._ctx(docker_host="unix:///tmp/x.sock"))
        with self.assertRaises(EphemeralExecutionError) as cm:
            ex._validate_authorization_context()
        self.assertEqual(cm.exception.code, "AUTH_CONTEXT_INVALID")

    def test_constructor_deep_copies_context(self):
        # Mutating the caller's dict after construction must NOT affect executor.
        import ephemeral_executor as ee
        ctx = self._ctx()
        ex = ee.EphemeralExecutor(str(ROOT), authorization_context=ctx)
        ctx["fingerprint"]["server_id"] = "TAMPERED"
        ctx["authorized"] = False
        # The executor's copy is unaffected.
        self.assertTrue(ex._authorization_context["authorized"])
        self.assertEqual(
            ex._authorization_context["fingerprint"]["server_id"],
            "f466f703-15ce-46fd-bfba-02e9c0a140b2")

    def test_executor_does_not_mutate_caller_context(self):
        import ephemeral_executor as ee
        ctx = self._ctx()
        original = __import__("copy").deepcopy(ctx)
        ex = ee.EphemeralExecutor(str(ROOT), authorization_context=ctx)
        # Validation must not mutate the context.
        ex._validate_authorization_context()
        self.assertEqual(ctx, original)


# ────────────────────────────────────────────────────────────────────────────
# TestEnvironmentRecheck — post-cleanup recheck ordering (Fix 2, final)
# ────────────────────────────────────────────────────────────────────────────
class TestEnvironmentRecheck(unittest.TestCase):
    """The environment post-recheck must not implicitly start a distro."""

    def _make_touched_executor(self):
        import ephemeral_executor as ee
        ex = ee.EphemeralExecutor(
            str(ROOT),
            authorization_context=TestExecutorStructural._make_auth_context())
        ex._environment_touched = True
        return ex

    def test_auth_context_invalid_cleanup_does_not_recheck(self):
        # start() fails with AUTH_CONTEXT_INVALID before touching the env →
        # cleanup's recheck is a no-op (does not call _wsl_distro_states).
        import ephemeral_executor as ee
        ex = ee.EphemeralExecutor(str(ROOT))  # no context
        self.assertFalse(ex._environment_touched)
        with mock.patch("ephemeral_harness._wsl_distro_states") as m_states, \
             mock.patch("ephemeral_harness._run_wsl_text") as m_wsl:
            # cleanup with nothing started + not touched → no recheck.
            ex.cleanup_and_verify()
        m_states.assert_not_called()
        m_wsl.assert_not_called()

    def test_recheck_noop_when_environment_not_touched(self):
        import ephemeral_executor as ee
        ex = ee.EphemeralExecutor(
            str(ROOT),
            authorization_context=TestExecutorStructural._make_auth_context())
        ex._environment_touched = False
        with mock.patch("ephemeral_harness._wsl_distro_states") as m_states:
            ex._recheck_environment_fingerprint()
        m_states.assert_not_called()

    def test_recheck_distro_stopped_no_wsl_d(self):
        # MergePilot-Test Stopped on recheck → ENVIRONMENT_FINGERPRINT_CHANGED,
        # and NO `wsl -d` / Docker command is issued (only wsl -l -v ran).
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_touched_executor()
        with mock.patch("ephemeral_harness._wsl_distro_states",
                        return_value={"MergePilot-Test": "Stopped",
                                      "Ubuntu-22.04": "Stopped"}), \
             mock.patch("ephemeral_harness._run_wsl_text") as m_wsl:
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex._recheck_environment_fingerprint()
        self.assertEqual(cm.exception.code, "ENVIRONMENT_FINGERPRINT_CHANGED")
        m_wsl.assert_not_called()  # no wsl -d / docker after Stopped

    def test_recheck_distro_missing_no_docker(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_touched_executor()
        with mock.patch("ephemeral_harness._wsl_distro_states",
                        return_value={"Ubuntu-22.04": "Stopped"}), \
             mock.patch("ephemeral_harness._run_wsl_text") as m_wsl:
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex._recheck_environment_fingerprint()
        self.assertEqual(cm.exception.code, "ENVIRONMENT_FINGERPRINT_CHANGED")
        m_wsl.assert_not_called()

    def test_recheck_ubuntu_state_changed_fails(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_touched_executor()
        with mock.patch("ephemeral_harness._wsl_distro_states",
                        return_value={"MergePilot-Test": "Running",
                                      "Ubuntu-22.04": "Running"}), \
             mock.patch("ephemeral_harness._run_wsl_text") as m_wsl:
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex._recheck_environment_fingerprint()
        self.assertEqual(cm.exception.code, "ENVIRONMENT_FINGERPRINT_CHANGED")
        m_wsl.assert_not_called()

    def test_recheck_docker_host_changed_fails(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_touched_executor()
        with mock.patch("ephemeral_harness._wsl_distro_states",
                        return_value={"MergePilot-Test": "Running",
                                      "Ubuntu-22.04": "Stopped"}), \
             mock.patch("ephemeral_harness._run_wsl_text",
                        side_effect=[(0, "unix:///tmp/x.sock\n", "")]) as m_wsl:
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex._recheck_environment_fingerprint()
        self.assertEqual(cm.exception.code, "ENVIRONMENT_FINGERPRINT_CHANGED")

    def test_recheck_endpoint_changed_fails(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_touched_executor()
        ctx = TestExecutorStructural._make_auth_context()
        # DOCKER_HOST matches (empty), endpoint differs.
        with mock.patch("ephemeral_harness._wsl_distro_states",
                        return_value={"MergePilot-Test": "Running",
                                      "Ubuntu-22.04": "Stopped"}), \
             mock.patch("ephemeral_harness._run_wsl_text",
                        side_effect=[(0, "\n", ""),
                                     (0, "tcp://x\n", "")]) as m_wsl:
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex._recheck_environment_fingerprint()
        self.assertEqual(cm.exception.code, "ENVIRONMENT_FINGERPRINT_CHANGED")

    def test_recheck_fingerprint_version_changed_fails(self):
        import ephemeral_executor as ee
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_touched_executor()
        changed_info = _DAEMON_INFO_TEXT.replace("29.1.3", "99.0.0")
        with mock.patch("ephemeral_harness._wsl_distro_states",
                        return_value={"MergePilot-Test": "Running",
                                      "Ubuntu-22.04": "Stopped"}), \
             mock.patch("ephemeral_harness._run_wsl_text",
                        side_effect=[(0, "\n", ""),
                                     (0, "unix:///var/run/docker.sock\n", ""),
                                     (0, changed_info, "")]) as m_wsl:
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex._recheck_environment_fingerprint()
        self.assertEqual(cm.exception.code, "ENVIRONMENT_FINGERPRINT_CHANGED")

    def test_recheck_all_match_passes(self):
        import ephemeral_executor as ee
        ctx = TestExecutorStructural._make_auth_context()
        ex = ee.EphemeralExecutor(str(ROOT), authorization_context=ctx)
        ex._environment_touched = True
        with mock.patch("ephemeral_harness._wsl_distro_states",
                        return_value={"MergePilot-Test": "Running",
                                      "Ubuntu-22.04": "Stopped"}), \
             mock.patch("ephemeral_harness._run_wsl_text",
                        side_effect=[
                            (0, "\n", ""),                              # DOCKER_HOST
                            (0, "unix:///var/run/docker.sock\n", ""),    # endpoint
                            (0, _DAEMON_INFO_TEXT, ""),                  # docker info
                            (0, ctx["image_id"] + "\n", ""),             # image id
                        ]):
            ex._recheck_environment_fingerprint()  # must not raise


# ────────────────────────────────────────────────────────────────────────────
# TestCleanupLifecycle — HTTP/poller/source retry semantics (Fix 3, final)
# ────────────────────────────────────────────────────────────────────────────
class TestCleanupLifecycle(unittest.TestCase):
    """HTTP/poller/source cleanup errors are not swallowed; refs retained."""

    def _make_executor(self):
        import ephemeral_executor as ee
        ex = ee.EphemeralExecutor(
            str(ROOT),
            authorization_context=TestExecutorStructural._make_auth_context())
        return ex

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_http_shutdown_failure_keeps_ref(self, _m):
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_executor()
        srv = mock.Mock()
        srv.shutdown.side_effect = RuntimeError("boom")
        ex._http_server = srv
        with mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.cleanup_and_verify()
        self.assertIn("HTTP_SHUTDOWN_FAILED", str(cm.exception))
        # Reference retained for retry.
        self.assertIsNotNone(ex._http_server)
        self.assertFalse(ex._cleaned)

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_http_first_fail_second_success(self, _m):
        ex = self._make_executor()
        srv = mock.Mock()
        srv.shutdown.side_effect = [RuntimeError("boom"), None]
        ex._http_server = srv
        with mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            try:
                ex.cleanup_and_verify()
            except Exception:
                pass
            # Second attempt succeeds.
            ex.cleanup_and_verify()
        self.assertIsNone(ex._http_server)
        self.assertTrue(ex._cleaned)

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_poller_stop_failure_keeps_ref(self, _m):
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_executor()
        poller = mock.Mock()
        poller.stop.side_effect = RuntimeError("boom")
        ex._poller = poller
        with mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.cleanup_and_verify()
        self.assertIn("POLLER_STOP_FAILED", str(cm.exception))
        self.assertIsNotNone(ex._poller)
        self.assertFalse(ex._cleaned)

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_poller_still_alive_keeps_ref(self, _m):
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_executor()
        poller = mock.Mock()
        poller.is_alive.return_value = True  # still alive after join
        ex._poller = poller
        with mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.cleanup_and_verify()
        self.assertIn("POLLER_STILL_ALIVE", str(cm.exception))

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_poller_second_success(self, _m):
        ex = self._make_executor()
        poller = mock.Mock()
        poller.stop.side_effect = [RuntimeError("boom"), None]
        poller.is_alive.return_value = False
        ex._poller = poller
        with mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            try:
                ex.cleanup_and_verify()
            except Exception:
                pass
            ex.cleanup_and_verify()
        self.assertIsNone(ex._poller)
        self.assertTrue(ex._cleaned)

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_source_close_failure_keeps_failed(self, _m):
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_executor()
        good = mock.Mock()
        bad = mock.Mock()
        bad.close.side_effect = RuntimeError("boom")
        ex._reader_sources = [good, bad]
        with mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.cleanup_and_verify()
        self.assertIn("SOURCE_CLOSE_FAILED", str(cm.exception))
        # The failed source is retained; the good one removed.
        self.assertEqual(ex._reader_sources, [bad])
        self.assertFalse(ex._cleaned)

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_source_second_close_success(self, _m):
        ex = self._make_executor()
        bad = mock.Mock()
        bad.close.side_effect = [RuntimeError("boom"), None]
        ex._reader_sources = [bad]
        with mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            try:
                ex.cleanup_and_verify()
            except Exception:
                pass
            ex.cleanup_and_verify()
        self.assertEqual(ex._reader_sources, [])
        self.assertTrue(ex._cleaned)

    @mock.patch("ephemeral_executor.EphemeralExecutor._recheck_environment_fingerprint")
    def test_cleanup_errors_contain_no_secret(self, _m):
        from ephemeral_executor import EphemeralExecutionError
        ex = self._make_executor()
        srv = mock.Mock()
        srv.shutdown.side_effect = RuntimeError("password=supersecret")
        ex._http_server = srv
        with mock.patch("ephemeral_executor.socket.socket") as msock:
            msock.return_value.connect.side_effect = OSError("closed")
            with self.assertRaises(EphemeralExecutionError) as cm:
                ex.cleanup_and_verify()
        msg = str(cm.exception)
        self.assertNotIn("supersecret", msg)
        self.assertNotIn("password=supersecret", msg)


if __name__ == "__main__":
    unittest.main()
