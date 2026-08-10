"""PURE STRATEGY unit tests for harden_policy.py.

IMPORTANT: these tests verify the request-matching + hardening-injection
LOGIC in isolation. They are NOT socket-proxy integration tests and do NOT
prove any real Docker creation chain is intercepted. The proxy daemon is
BLOCKED_UPSTREAM (see tools/hiclab/UPSTREAM_BLOCKED.md, option b). Until a
real deployable proxy exists, Manager auto-create is forbidden by operating
policy, and create_hardened_worker.sh is the only permitted worker path.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import harden_policy as hp


class TestIsTargetRequest(unittest.TestCase):
    def test_worker_create(self):
        kind = hp.is_target_request(
            "POST", "/v1.41/containers/create",
            {"name": "hiclaw-worker-fixer"}, {})
        self.assertEqual(kind, "worker")

    def test_manager_create(self):
        kind = hp.is_target_request(
            "POST", "/containers/create",
            {"name": "hiclaw-manager"}, {})
        self.assertEqual(kind, "manager")

    def test_non_worker_name_passthrough(self):
        kind = hp.is_target_request(
            "POST", "/containers/create",
            {"name": "alice-test-worker"}, {})
        self.assertIsNone(kind)

    def test_get_request_not_target(self):
        kind = hp.is_target_request(
            "GET", "/containers/create", {"name": "hiclaw-worker-fixer"}, {})
        self.assertIsNone(kind)

    def test_non_create_path_not_target(self):
        kind = hp.is_target_request(
            "POST", "/containers/hiclaw-worker-fixer/start", {}, {})
        self.assertIsNone(kind)

    def test_name_from_body_when_query_missing(self):
        kind = hp.is_target_request(
            "POST", "/containers/create", {}, {"Name": "hiclaw-worker-fixer"})
        self.assertEqual(kind, "worker")


class TestApplyHardeningWorker(unittest.TestCase):
    def _cfg(self, **kw):
        cfg = {"storage_opt_supported": True, "storage_opt_gib": 10,
               "run_id": "run1", "scope": "prod",
               "sizes": {"codex_tmp_mib": 512, "tmp_mib": 256}}
        cfg.update(kw)
        return cfg

    def test_injects_tmpfs(self):
        body = {"Name": "hiclaw-worker-fixer", "HostConfig": {}}
        out = hp.apply_hardening(body, "worker", self._cfg())
        tmpfs = out["HostConfig"]["Tmpfs"]
        self.assertIn(
            "/root/hiclaw-fs/agents/fixer/.codex/tmp", tmpfs)
        self.assertIn("/tmp", tmpfs)
        self.assertIn("size=512m", tmpfs[
            "/root/hiclaw-fs/agents/fixer/.codex/tmp"])

    def test_restart_no(self):
        body = {"Name": "hiclaw-worker-fixer", "HostConfig": {}}
        out = hp.apply_hardening(body, "worker", self._cfg())
        self.assertEqual(out["HostConfig"]["RestartPolicy"], {"Name": "no"})

    def test_storage_opt_only_if_supported(self):
        body = {"Name": "hiclaw-worker-fixer", "HostConfig": {}}
        out_supported = hp.apply_hardening(
            body, "worker", self._cfg(storage_opt_supported=True))
        self.assertIn("size=10g", out_supported["HostConfig"]["StorageOpt"])

        body2 = {"Name": "hiclaw-worker-fixer", "HostConfig": {}}
        out_unsupported = hp.apply_hardening(
            body2, "worker", self._cfg(storage_opt_supported=False))
        self.assertNotIn("StorageOpt", out_unsupported["HostConfig"])

    def test_labels_merged(self):
        body = {"Name": "hiclaw-worker-fixer",
                "Labels": {"app": "hiclaw"}, "HostConfig": {}}
        out = hp.apply_hardening(body, "worker", self._cfg())
        self.assertEqual(out["Labels"]["app"], "hiclaw")
        self.assertEqual(out["Labels"]["com.mergepilot.hardened"], "1")
        self.assertEqual(out["Labels"]["com.mergepilot.run_id"], "run1")

    def test_preserves_existing_tmpfs(self):
        body = {"Name": "hiclaw-worker-fixer",
                "HostConfig": {"Tmpfs": {"/existing": "rw,size=64m"}}}
        out = hp.apply_hardening(body, "worker", self._cfg())
        self.assertIn("/existing", out["HostConfig"]["Tmpfs"])
        self.assertIn("/tmp", out["HostConfig"]["Tmpfs"])

    def test_no_env_injected_for_worker(self):
        """Workers must NOT get env additions (only manager does)."""
        body = {"Name": "hiclaw-worker-fixer",
                "Env": ["PATH=/usr/bin"], "HostConfig": {}}
        out = hp.apply_hardening(body, "worker", self._cfg())
        self.assertEqual(out["Env"], ["PATH=/usr/bin"])

    def test_does_not_mutate_input(self):
        body = {"Name": "hiclaw-worker-fixer",
                "HostConfig": {"RestartPolicy": {"Name": "always"}}}
        original = {"Name": "hiclaw-worker-fixer",
                    "HostConfig": {"RestartPolicy": {"Name": "always"}}}
        hp.apply_hardening(body, "worker", self._cfg())
        self.assertEqual(body, original)


class TestApplyHardeningManager(unittest.TestCase):
    def test_redirects_caches(self):
        body = {"Name": "hiclaw-manager",
                "Env": ["PATH=/usr/bin"], "HostConfig": {}}
        out = hp.apply_hardening(body, "manager", {
            "storage_opt_supported": False, "run_id": "mgr",
            "scope": "prod"})
        env = out["Env"]
        self.assertIn("NPM_CONFIG_CACHE=" + hp.MANAGER_NPM_CACHE_PATH, env)
        self.assertIn("NODE_COMPILE_CACHE=" + hp.MANAGER_NODE_COMPILE_PATH, env)
        self.assertIn("PATH=/usr/bin", env)

    def test_overrides_existing_cache_env(self):
        body = {"Name": "hiclaw-manager",
                "Env": ["NPM_CONFIG_CACHE=/old/path"], "HostConfig": {}}
        out = hp.apply_hardening(body, "manager", {
            "storage_opt_supported": False, "run_id": "m", "scope": "p"})
        cache_entries = [e for e in out["Env"] if e.startswith("NPM_CONFIG_CACHE=")]
        self.assertEqual(len(cache_entries), 1)
        self.assertEqual(cache_entries[0],
                         "NPM_CONFIG_CACHE=" + hp.MANAGER_NPM_CACHE_PATH)


class TestProcessRequest(unittest.TestCase):
    def test_non_target_passthrough(self):
        action, body = hp.process_request(
            "POST", "/containers/create", {"name": "alice"}, {"x": 1},
            {"storage_opt_supported": False, "run_id": "r", "scope": "p"})
        self.assertEqual(action, "passthrough")
        self.assertEqual(body, {"x": 1})

    def test_worker_hardened(self):
        action, body = hp.process_request(
            "POST", "/containers/create", {"name": "hiclaw-worker-fixer"},
            {"Name": "hiclaw-worker-fixer", "HostConfig": {}},
            {"storage_opt_supported": False, "run_id": "r1", "scope": "prod"})
        self.assertEqual(action, "hardened")
        self.assertEqual(body["HostConfig"]["RestartPolicy"], {"Name": "no"})


if __name__ == "__main__":
    unittest.main()
