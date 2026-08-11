"""Unit tests for managed_containers.py (authoritative manifest)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import managed_containers as mc


class TestManifest(unittest.TestCase):
    def test_no_data_container(self):
        """{prefix}data does NOT exist (MinIO embedded in controller)."""
        self.assertNotIn(mc.DATA_NAME, mc.names())
        self.assertIn(mc.DATA_NAME, mc.EXCLUDED)

    def test_all_six_present(self):
        expected = {"audit-pg", "github-mcp", mc.CONTROLLER_NAME,
                    "policy-gw", "mergepilot-controller", mc.MANAGER_NAME}
        self.assertEqual(set(mc.names()), expected)

    def test_no_duplicates(self):
        self.assertTrue(mc.check_unique())
        self.assertEqual(len(mc.names()), len(set(mc.names())))

    def test_validate_no_excluded(self):
        self.assertTrue(mc.validate_no_excluded())

    def test_phase1_members(self):
        p1 = {m["name"] for m in mc.phase_members(mc.PHASE_1)}
        self.assertEqual(p1, {"audit-pg", "github-mcp", mc.CONTROLLER_NAME})

    def test_phase2_members(self):
        p2 = {m["name"] for m in mc.phase_members(mc.PHASE_2)}
        self.assertEqual(p2,
                         {"policy-gw", "mergepilot-controller", mc.MANAGER_NAME})

    def test_phase1_before_phase2_in_manifest(self):
        """Foundation (phase 1) must precede dependents (phase 2)."""
        phases = [m["phase"] for m in mc.MANAGED]
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
        self.assertIsNone(mc.find(mc.DATA_NAME))

    def test_audit_pg_is_phase1_first(self):
        """audit-pg (data store) must be the first foundation member."""
        self.assertEqual(mc.phase_members(mc.PHASE_1)[0]["name"], "audit-pg")

    def test_v1_2_2_default_naming(self):
        """D2B-3 v1.2.2 upgrade: default names use agentteams- prefix."""
        self.assertTrue(mc.CONTROLLER_NAME.startswith("agentteams-"))
        self.assertTrue(mc.MANAGER_NAME.startswith("agentteams-"))

    def test_v1_1_2_legacy_compat(self):
        """HICLAB_LEGACY_PREFIX=hiclaw- produces v1.1.2 names."""
        import importlib
        old = os.environ.get("HICLAB_LEGACY_PREFIX", "")
        try:
            os.environ["HICLAB_LEGACY_PREFIX"] = "hiclaw-"
            importlib.reload(mc)
            self.assertEqual(mc.CONTROLLER_NAME, "hiclaw-controller")
            self.assertEqual(mc.MANAGER_NAME, "hiclaw-manager")
        finally:
            if old:
                os.environ["HICLAB_LEGACY_PREFIX"] = old
            else:
                os.environ.pop("HICLAB_LEGACY_PREFIX", None)
            importlib.reload(mc)


if __name__ == "__main__":
    unittest.main()
