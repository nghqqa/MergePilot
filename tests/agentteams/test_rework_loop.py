"""Tests for the multi-agent rework-loop mechanism verification (finals D1).

Offline tests pin the case fixtures (what each fix attempt does to the
acceptance tests). The PostgreSQL-backed test drives the REAL controller
(process_event) and is gated by DBVERIFY_PG_PORT / DBVERIFY_PG_PASSWORD_FILE;
it skips (never passes) without them."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "agentteams"))

import rework_loop_harness as h  # noqa: E402

PORT = os.environ.get("DBVERIFY_PG_PORT")
PWFILE = os.environ.get("DBVERIFY_PG_PASSWORD_FILE")


class TestCaseFixtures(unittest.TestCase):

    def test_acceptance_tests_decide_each_attempt(self):
        base = h.run_acceptance_tests(h.revision_files("base"))
        a1 = h.run_acceptance_tests(h.revision_files("attempt1"))
        a2 = h.run_acceptance_tests(h.revision_files("attempt2"))
        self.assertEqual(base["verdict"], "FAIL")
        self.assertEqual(a1["verdict"], "FAIL")
        self.assertEqual(a1["failed_tests"], ["test_second_payment_request_for_same_order_is_idempotent"])
        self.assertEqual(a2["verdict"], "PASS")
        self.assertEqual(a2["tests_run"], 5)

    def test_missing_acceptance_test_blocks_verification(self):
        r = h.run_acceptance_tests(h.revision_files("attempt2", with_tests=False))
        self.assertEqual(r["verdict"], "BLOCKED")

    def test_reviewer_finding_points_at_the_deciding_test(self):
        findings = json.loads((h.CASE / "reviewer_findings.json").read_text(encoding="utf-8"))
        self.assertEqual(findings["input_kind"], "CONTROLLED_INPUT")
        ref = findings["findings"][0]["acceptance_test"].split("::")[-1]
        self.assertIn(ref, (h.CASE / "base" / "test_payments.py").read_text(encoding="utf-8"))

    def test_revision_tree_shas_are_git_ids_and_distinct(self):
        shas = [h.tree_sha(h.revision_files(v)) for v in ("base", "attempt1", "attempt2")]
        for s in shas:
            self.assertRegex(s, r"^[0-9a-f]{40}$")
        self.assertEqual(len(set(shas)), 3)


@unittest.skipUnless(PORT and PWFILE, "DBVERIFY_PG_PORT / DBVERIFY_PG_PASSWORD_FILE not set")
class TestRealControllerLoop(unittest.TestCase):

    def test_all_scenarios_against_real_controller(self):
        out = Path(tempfile.mkdtemp(prefix="rework-test-"))
        args = h.argparse.Namespace(pg_host=os.environ.get("DBVERIFY_PG_HOST", "127.0.0.1"), pg_port=int(PORT), pg_user="mergepilot",
                                    pg_password_file=PWFILE, audit_db="mergepilot_audit", max_verify_attempts=3,
                                    run_suffix="t" + time.strftime("%H%M%S", time.gmtime()), out=str(out))
        report = h.Harness(args).run()
        self.assertEqual(report["summary"]["checks_failed"], [])
        a = report["scenarios"]["A_rework_then_pass"]
        self.assertEqual([(s["stage"], s["attempt"], s["verdict"]) for s in a["final"]["stage_runs"]],
                         [("review", 1, None), ("fix", 1, None), ("verify", 1, "FAIL"), ("fix", 2, None), ("verify", 2, "PASS")])
        self.assertEqual(a["final"]["task"]["status"], "PASS")
        self.assertEqual(report["scenarios"]["B_retry_cap_hold"]["final"]["task"]["current_stage"], "verify_max_hold")
        self.assertEqual(report["scenarios"]["C_missing_context_blocked_escalation"]["final"]["task"]["status"], "HOLD")
        self.assertTrue((out / "SHA256SUMS").is_file())


if __name__ == "__main__":
    unittest.main()
