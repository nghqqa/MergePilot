"""PostgreSQL-backed integration test for the migration-verification loop.

Gated: runs only when DBVERIFY_PG_PORT and DBVERIFY_PG_PASSWORD_FILE point at a
PostgreSQL whose mergepilot_audit database already carries the full audit-db
chain (000-roles … 001-init incl. m9). Skips otherwise — a skip is reported as a
skip, never as a pass."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "dbverify"))

PORT = os.environ.get("DBVERIFY_PG_PORT")
PWFILE = os.environ.get("DBVERIFY_PG_PASSWORD_FILE")


@unittest.skipUnless(PORT and PWFILE, "DBVERIFY_PG_PORT / DBVERIFY_PG_PASSWORD_FILE not set")
class TestMigrationLoopAgainstPostgres(unittest.TestCase):

    def test_full_loop_negatives_and_gate_sequence(self):
        import run_migration_loop as loop
        import time
        out = Path(tempfile.mkdtemp(prefix="dbverify-test-"))
        args = loop.argparse.Namespace(
            pg_host=os.environ.get("DBVERIFY_PG_HOST", "127.0.0.1"), pg_port=int(PORT), pg_user="mergepilot",
            pg_password_file=PWFILE, audit_db="mergepilot_audit",
            trial_instance=os.environ.get("DBVERIFY_TRIAL_INSTANCE", "test:isolated-postgres"),
            image="pgvector/pgvector:pg16", out=str(out), keep_baseline=False, plan_out=str(out / "migration-plan"),
            run_suffix="t" + time.strftime("%H%M%S", time.gmtime()))
        report = loop.Loop(args).run()
        failed = [n["name"] for n in report["negative_tests"] if not n["ok"]]
        self.assertEqual(failed, [], failed)
        self.assertEqual([g["reason"] for g in report["gate_timeline"]], [
            "NOT_BOUND_TO_VERIFICATION", "TICKET_NOT_APPROVED", "OK", "OK", "TARGET_DATA_DIGEST_MISMATCH",
            "STALE_SUPERSEDED_BY_NEW_REVISION", "STALE_SUPERSEDED_BY_NEW_REVISION",
            # post-claim re-check on the EXECUTING ticket: same head OK, revision pushed after the claim → STALE
            "OK", "STALE_SUPERSEDED_BY_NEW_REVISION",
            "TICKET_EXPIRED"])
        names = {n["name"] for n in report["negative_tests"]}
        for required in ("duplicate_callback_same_digest_is_noop", "claim_refused_on_stale_head",
                         "claim_refused_on_target_data_digest_change", "concurrent_claim_exactly_one_executes",
                         "claim_refused_on_expired_ticket", "claim_refused_before_approval",
                         "gateway_wrapper_maps_gate_refusal", "plain_ticket_claim_unchanged",
                         "reclaim_on_executing_returns_no_row", "claim_refused_without_target_digest_for_bound_ticket",
                         "claim_refused_when_migration_run_unbound", "recompute_before_migrate_matches_bound_digest",
                         "data_drift_after_claim_detected", "post_claim_gate_recheck_detects_new_revision"):
            self.assertIn(required, names)
        self.assertIn(report["environment"]["gateway_wrapper_mode"], ("MODULE_IMPORT", "AST_EXTRACT_FALLBACK"))
        try:
            import mcp  # noqa: F401
            self.assertEqual(report["environment"]["gateway_wrapper_mode"], "MODULE_IMPORT")
        except ImportError:
            pass
        steps = {s["step"]: s for s in report["steps"]}
        self.assertEqual(steps["verify_rev1_attempt1"]["outcome"], "FAIL/HISTORICAL_DATA_INCOMPATIBLE")
        self.assertEqual(steps["verify_rev1_attempt1"]["migration_error"]["sqlstate"], "23502")
        self.assertEqual(steps["S3_code_tests_rev1"]["outcome"], "PASS")
        self.assertEqual(steps["verify_rev2_attempt1"]["outcome"], "PASS/-")
        self.assertEqual(steps["verify_rev2_attempt1"]["assertions_passed"], steps["verify_rev2_attempt1"]["assertions_total"])
        self.assertTrue((out / "SHA256SUMS").is_file())


if __name__ == "__main__":
    unittest.main()
