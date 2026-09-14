"""Offline tests for the pure helpers of tools/dbverify/run_migration_loop.py and
the case fixtures it verifies."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "dbverify"))
sys.path.insert(0, str(ROOT / "tools" / "demo_console"))

import run_migration_loop as loop  # noqa: E402

CASE = ROOT / "tools" / "dbverify" / "case" / "orders-schema-change"


class TestDigestMirrors(unittest.TestCase):

    def test_revision_digest_mirrors_showcase_seed_algorithm(self):
        # tools/demo_console/showcase_cases._revision_digest is the existing Python
        # mirror of bind_revision(); the runner must produce identical digests.
        import showcase_cases as sc
        args = ("mcp-x-read", "corr-mcp-x-read", "get_pull_request", "mergepilot/orders-demo", "run-x", "a" * 40, "OK")
        self.assertEqual(loop.revision_digest(*args), sc._revision_digest(*args))

    def test_canon_str_matches_sql_definition(self):
        self.assertEqual(loop.canon_str(None), "-1:")
        self.assertEqual(loop.canon_str("ab"), "2:ab")
        self.assertEqual(loop.canon_str("中"), "3:中")   # octet_length, not char length


class TestCandidateIdentity(unittest.TestCase):

    def test_tree_sha_is_a_real_git_object_id_and_revision_sensitive(self):
        rev1, rev2, rev3 = loop.git_tree_sha(loop.case_files(1)), loop.git_tree_sha(loop.case_files(2)), loop.git_tree_sha(loop.case_files(2, followup=True))
        for sha in (rev1, rev2, rev3):
            self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(len({rev1, rev2, rev3}), 3)
        self.assertEqual(rev2, loop.git_tree_sha(loop.case_files(2)))  # deterministic

    def test_migration_scripts_differ_and_are_single_transaction(self):
        r1 = (CASE / "migrations" / "candidate-a.rev1.sql").read_text(encoding="utf-8")
        r2 = (CASE / "migrations" / "candidate-a.rev2.sql").read_text(encoding="utf-8")
        self.assertNotEqual(r1, r2)
        for s in (r1, r2):
            self.assertIn("BEGIN;", s)
            self.assertTrue(s.rstrip().endswith("COMMIT;"))
        self.assertIn("SET NOT NULL", r1)
        self.assertNotIn("orders_backfill_audit", r1)
        self.assertIn("orders_backfill_audit", r2)
        self.assertIn("SET DEFAULT 0", r2)   # old-worker compatibility


class TestCaseFixtures(unittest.TestCase):

    def test_assertions_json_well_formed(self):
        spec = json.loads((CASE / "assertions.json").read_text(encoding="utf-8"))
        names = [a["name"] for a in spec["assertions"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("old_worker_compat_insert", names)
        for a in spec["assertions"]:
            self.assertIn(a["kind"], ("scalar", "probe"))
            self.assertIn("expect", a)

    def test_pr_unit_tests_pass_on_their_own(self):
        out = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(CASE / "app"), "-p", "test_*.py"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(out.returncode, 0, out.stdout.decode(errors="replace")[-400:])
        self.assertIn(b"Ran 7 tests", out.stdout)

    def test_seed_plants_the_documented_defects(self):
        seed = (CASE / "baseline" / "seed.sql").read_text(encoding="utf-8")
        self.assertIn("g <= 137 THEN NULL", seed)
        self.assertIn("(10001, 500", seed)
        self.assertIn("(10002, 777", seed)
        self.assertIn("generate_series(1, 120)", seed)


if __name__ == "__main__":
    unittest.main()
