"""Unit tests for managed_containers.py (authoritative manifest)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import managed_containers as mc


class TestManifest(unittest.TestCase):
    def test_no_hiclaw_data(self):
        """hiclaw-data does NOT exist (MinIO embedded in hiclaw-controller)."""
        self.assertNotIn("hiclaw-data", mc.names())
        self.assertIn("hiclaw-data", mc.EXCLUDED)

    def test_all_six_present(self):
        expected = {"audit-pg", "github-mcp", "hiclaw-controller",
                    "policy-gw", "mergepilot-controller", "hiclaw-manager"}
        self.assertEqual(set(mc.names()), expected)

    def test_no_duplicates(self):
        self.assertTrue(mc.check_unique())
        self.assertEqual(len(mc.names()), len(set(mc.names())))

    def test_validate_no_excluded(self):
        self.assertTrue(mc.validate_no_excluded())

    def test_phase1_members(self):
        p1 = {m["name"] for m in mc.phase_members(mc.PHASE_1)}
        self.assertEqual(p1, {"audit-pg", "github-mcp", "hiclaw-controller"})

    def test_phase2_members(self):
        p2 = {m["name"] for m in mc.phase_members(mc.PHASE_2)}
        self.assertEqual(p2,
                         {"policy-gw", "mergepilot-controller", "hiclaw-manager"})

    def test_phase1_before_phase2_in_manifest(self):
        """Foundation (phase 1) must precede dependents (phase 2)."""
        phases = [m["phase"] for m in mc.MANAGED]
        # All phase 1 entries come before any phase 2 entry
        first_p2 = next((i for i, p in enumerate(phases) if p == mc.PHASE_2), None)
        self.assertIsNotNone(first_p2)
        self.assertTrue(all(p == mc.PHASE_1 for p in phases[:first_p2]))
        self.assertTrue(all(p == mc.PHASE_2 for p in phases[first_p2:]))

    def test_each_has_health_probe(self):
        for m in mc.MANAGED:
            self.assertIn("health", m, "%s missing health probe" % m["name"])
            self.assertIn("kind", m["health"])
            self.assertIn(m["health"]["kind"],
                          ("exec", "running_uptime", "docker_health"))

    def test_find(self):
        self.assertIsNotNone(mc.find("audit-pg"))
        self.assertIsNone(mc.find("hiclaw-data"))

    def test_audit_pg_is_phase1_first(self):
        """audit-pg (data store) must be the first foundation member."""
        self.assertEqual(mc.phase_members(mc.PHASE_1)[0]["name"], "audit-pg")


if __name__ == "__main__":
    unittest.main()
