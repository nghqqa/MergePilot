"""Static contract tests for the finals D2 migration-verification extension.

Offline (no PostgreSQL): they parse the SQL text and the two places that
enumerate the migration chain, so CI catches drift between
tools/audit-db/m9_migration_verification.sql, release/offline/db-init/001-init.sql
and tools/cli/mergepilot.py. The PostgreSQL-backed loop itself is exercised by
test_migration_loop_pg.py (gated by DBVERIFY_PG_PORT)."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
M9 = ROOT / "tools" / "audit-db" / "m9_migration_verification.sql"
OFFLINE_INIT = ROOT / "release" / "offline" / "db-init" / "001-init.sql"
sys.path.insert(0, str(ROOT / "tools" / "cli"))

SQL = M9.read_text(encoding="utf-8")

TABLES = ("data_baselines", "migration_candidates", "migration_verifications", "approval_verification_bindings")
FUNCTIONS = ("mv_register_baseline", "mv_register_candidate", "mv_record_verification",
             "l2_bind_verification", "db_release_gate", "mv_run_status")
GATE_REASONS = ("TICKET_NOT_FOUND", "NOT_BOUND_TO_VERIFICATION", "TICKET_NOT_APPROVED", "TICKET_EXPIRED",
                "TICKET_HEAD_MISMATCH", "REVISION_HEAD_MISMATCH", "PR_BINDING_HEAD_MISMATCH",
                "STALE_SUPERSEDED_BY_NEW_REVISION", "VERIFICATION_MISMATCH", "BASELINE_MISMATCH",
                "TARGET_DATA_DIGEST_MISMATCH")


class TestM9Shape(unittest.TestCase):

    def test_single_transaction_and_self_check(self):
        self.assertTrue(SQL.lstrip().startswith("--"))
        self.assertIn("\nBEGIN;\n", SQL)
        self.assertTrue(SQL.rstrip().endswith("COMMIT;"))
        self.assertIn("m9 self-check failed", SQL)

    def test_four_child_tables_only_and_idempotent_ddl(self):
        created = re.findall(r"CREATE TABLE IF NOT EXISTS public\.([a-z_]+)", SQL)
        self.assertEqual(sorted(created), sorted(TABLES))
        self.assertNotIn("CREATE TABLE public.", SQL)   # every table is IF NOT EXISTS
        self.assertNotIn("CREATE TABLE IF NOT EXISTS public.task_runs", SQL)  # no parallel run state machine

    def test_extends_existing_model_not_parallel(self):
        # candidates hang off the immutable revision binding; approvals link off the existing ticket
        self.assertIn("REFERENCES public.revision_bindings(binding_id)", SQL)
        self.assertIn("REFERENCES public.approvals(ticket_id)", SQL)
        self.assertNotIn("ALTER TABLE public.revision_bindings", SQL)   # revision_bindings stays untouched
        self.assertNotIn("ALTER TABLE public.approvals", SQL)

    def test_verification_object_binds_all_versions(self):
        for col in ("head_sha", "script_digest", "baseline_id", "trial_instance", "trial_kind", "env_versions", "report_digest"):
            self.assertIn(col, SQL, col)
        self.assertIn("CHECK (trial_kind IN ('AGENTIC_DB_BRANCH','ISOLATED_POSTGRES','SIMULATED'))", SQL)

    def test_uniqueness_and_immutability(self):
        self.assertIn("UNIQUE (candidate_id, baseline_id, attempt)", SQL)
        self.assertIn("UNIQUE (run_id, candidate_key, revision_no)", SQL)
        self.assertIn("UNIQUE (schema_digest, data_digest)", SQL)
        for t in TABLES:
            self.assertRegex(SQL, r"CREATE TRIGGER trg_%s_immutable BEFORE UPDATE OR DELETE ON public\.%s" % (t, t))
        self.assertIn("EXECUTE FUNCTION public._immutable()", SQL)

    def test_functions_and_gate_reasons(self):
        for fn in FUNCTIONS:
            self.assertRegex(SQL, r"CREATE OR REPLACE FUNCTION public\.%s\(" % fn)
        for reason in GATE_REASONS:
            self.assertIn("'%s'" % reason, SQL)
        self.assertIn("DROP FUNCTION IF EXISTS public.db_release_gate(TEXT);", SQL)  # signature change stays idempotent
        self.assertIn("p_target_data_digest TEXT DEFAULT NULL", SQL)

    def test_grants_follow_deny_by_not_granted(self):
        self.assertIn("REVOKE ALL ON public.data_baselines", SQL)
        self.assertIn("TO mergepilot_reader;", SQL)
        self.assertIn("GRANT EXECUTE ON FUNCTION public.l2_bind_verification(TEXT,TEXT) TO mergepilot_approver;", SQL)
        self.assertNotIn("PASSWORD", SQL.upper())
        self.assertNotIn("GRANT ALL", SQL.upper())


class TestChainIntegration(unittest.TestCase):

    def test_offline_init_carries_identical_m9_section(self):
        text = OFFLINE_INIT.read_text(encoding="utf-8")
        marker = "-- ===== tools/audit-db/m9_migration_verification.sql =====\n"
        self.assertIn(marker, text)
        section = text.split(marker, 1)[1]
        self.assertEqual(section.strip(), SQL.strip())
        order = re.findall(r"-- ===== tools/audit-db/([a-z0-9_]+)\.sql =====", text)
        self.assertEqual(order[-2:], ["m8gh1_github_ingress", "m9_migration_verification"])

    def test_cli_chain_appends_m9_after_github_ingress(self):
        import mergepilot as mp  # noqa: WPS433
        chain = list(mp.AUDIT_DB_MIGRATION_CHAIN)
        self.assertEqual(chain[-2:], ["m8gh1_github_ingress.sql", "m9_migration_verification.sql"])
        self.assertTrue((ROOT / "tools" / "audit-db" / chain[-1]).is_file())


if __name__ == "__main__":
    unittest.main()
