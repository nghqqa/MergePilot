"""M8-GH-4B3-W3B-S1 tests: six-service runtime specs, secret matrix,
and lifecycle API. All use synthetic data; zero real secrets."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli")):
    if p not in sys.path:
        sys.path.insert(0, p)

import e2e_foundation as e2f                    # noqa: E402
import e2e_runtime_specs as rs                  # noqa: E402


def _ctrl_env():
    return {
        "GITHUB_INGRESS_ENABLED": "1",
        "GITHUB_ROOM_MAP_PATH": "/run/mergepilot/room-map.yaml",
        "GITHUB_POLICY_PATH":
            "/run/mergepilot/policy-fixture.yaml",
        "GITHUB_DELIVERY_LEASE_SECONDS": "120",
        "GITHUB_DELIVERY_MAX_ATTEMPTS": "5",
        "MATRIX_HS": "http://matrix-hs:6167",
        "MATRIX_SERVER_NAME": e2f.E2E_MATRIX_SERVER_NAME,
        "MATRIX_USER": "m8gh4-controller",
        "CONTROLLER_CONSUMER_NAME": "m8gh4-controller",
        "M4F_ALLOWED_ROOMS": "!r:" + e2f.E2E_MATRIX_SERVER_NAME,
        "M4F_ALLOWED_SENDERS":
            "manager,reviewer,fixer,verifier",
        "M4F_RUN_PREFIX": "gh-",
        "RESERVED_RUN_PREFIXES": "",
        "GATEWAY_URL": "http://policy-gateway:8083",
        "COORDINATOR_TOKEN": "tok-" + "a" * 32,
    }


def _gw_env():
    return {
        "UPSTREAM_URL": rs.GATEWAY_E2E_UPSTREAM,
        "POLICY_FILE": rs.GATEWAY_E2E_POLICY,
        "ROLE_TOKENS": '{"reviewer":"tok-r"}',
        "AUDIT_DSN":
            "postgresql://u:p@postgres:5432/db?connect_timeout=5",
    }


def _bridge_env():
    return {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "fake-pat-for-test",
        "GITHUB_REPOSITORY": "example/fixture",
        "HTTPS_PROXY": rs.BRIDGE_PROXY,
        "MCP_PROXY_PORT": "8082",
    }


def _reporter_env():
    return {
        "GITHUB_PUBLISHER_DSN":
            "postgresql://u:p@postgres:5432/db?connect_timeout=5",
        "GITHUB_API_BASE": "https://api.github.com",
        "GITHUB_APP_ID": "4648333",
        "GITHUB_INSTALLATION_ID": "154914965",
        "GITHUB_REPOSITORY_ID": "1314399289",
        "GITHUB_PRIVATE_KEY_PATH":
            "/run/secrets/github-app-private-key.pem",
        "GH_REPORTER_POLL_SECONDS": "5",
        "GH_REPORTER_LEASE_SECONDS": "120",
        "GH_REPORTER_MAX_ATTEMPTS": "8",
        "HTTPS_PROXY": e2f.E2E_REPORTER_PROXY_R,
    }


def _proxy_env():
    return {
        "GH_PROXY_BIND": "0.0.0.0",
        "GH_PROXY_PORT": "18090",
        "GH_PROXY_UPSTREAM_IP": "172.23.48.1",
        "GH_PROXY_UPSTREAM_PORT": "17890",
    }


def _all_configs():
    return {
        "controller": _ctrl_env(),
        "policy-gateway": _gw_env(),
        "mcp-bridge": _bridge_env(),
        "gh-reporter": _reporter_env(),
        "gh-proxy-r": _proxy_env(),
        "gh-proxy-b": dict(_proxy_env()),
    }


# ── §3: runtime specs existence and schemas ────────────────────────────────

class TestRuntimeSpecs(unittest.TestCase):

    def test_six_services_defined(self):
        self.assertEqual(set(rs.SERVICE_RUNTIME_SPECS),
                         {"controller", "policy-gateway", "mcp-bridge",
                          "gh-reporter", "gh-proxy-r", "gh-proxy-b"})

    def test_controller_15_keys(self):
        spec = rs.SERVICE_RUNTIME_SPECS["controller"]
        self.assertEqual(len(spec["keys"]), 15)

    def test_reporter_10_keys(self):
        spec = rs.SERVICE_RUNTIME_SPECS["gh-reporter"]
        self.assertEqual(len(spec["keys"]), 10)

    def test_gateway_4_keys(self):
        self.assertEqual(len(rs.GATEWAY_E2E_ENV_KEYS), 4)

    def test_bridge_4_keys(self):
        self.assertEqual(len(rs.BRIDGE_ENV_KEYS), 4)

    def test_proxy_4_keys(self):
        self.assertEqual(len(rs.PROXY_ENV_KEYS), 4)

    def test_gateway_unknown_key_rejected(self):
        env = _gw_env()
        env["EXTRA"] = "x"
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_gateway_e2e_env(env)

    def test_gateway_missing_key_rejected(self):
        env = _gw_env()
        del env["UPSTREAM_URL"]
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_gateway_e2e_env(env)

    def test_gateway_wrong_upstream_rejected(self):
        env = _gw_env()
        env["UPSTREAM_URL"] = "http://127.0.0.1:8084/sse"
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_gateway_e2e_env(env)

    def test_bridge_wrong_proxy_rejected(self):
        env = _bridge_env()
        env["HTTPS_PROXY"] = "http://wrong:9999"
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_bridge_env(env)

    def test_bridge_bad_repo_rejected(self):
        env = _bridge_env()
        env["GITHUB_REPOSITORY"] = "no-slash"
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_bridge_env(env)

    def test_proxy_hostname_rejected(self):
        env = _proxy_env()
        env["GH_PROXY_UPSTREAM_IP"] = "proxy.example.com"
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_proxy_env(env)

    def test_proxy_wrong_port_rejected(self):
        env = _proxy_env()
        env["GH_PROXY_UPSTREAM_PORT"] = "8888"
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_proxy_env(env)


# ── §9: secret consumer matrix ─────────────────────────────────────────────

class TestSecretMatrix(unittest.TestCase):

    def test_pat_only_bridge(self):
        self.assertEqual(rs.SECRET_CONSUMER_MATRIX["fine_grained_pat"],
                         frozenset(("mcp-bridge",)))

    def test_pem_only_reporter(self):
        self.assertEqual(rs.SECRET_CONSUMER_MATRIX["github_app_pem"],
                         frozenset(("gh-reporter",)))

    def test_controller_no_pat_no_pem(self):
        spec = rs.SERVICE_RUNTIME_SPECS["controller"]
        self.assertIn("fine_grained_pat", spec["forbidden_secrets"])
        self.assertIn("github_app_pem", spec["forbidden_secrets"])

    def test_gateway_no_pat_no_pem(self):
        spec = rs.SERVICE_RUNTIME_SPECS["policy-gateway"]
        self.assertIn("fine_grained_pat", spec["forbidden_secrets"])
        self.assertIn("github_app_pem", spec["forbidden_secrets"])

    def test_violation_raises(self):
        with self.assertRaises(rs.RuntimeSpecError) as ctx:
            rs.validate_secret_consumers(
                "controller", {"fine_grained_pat"})
        self.assertEqual(ctx.exception.code,
                         "SECRET_CONSUMER_VIOLATION")


# ── §10: lifecycle API ─────────────────────────────────────────────────────

class TestRuntimeLifecycle(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)
        self.journal = {}

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_validate_all_pass(self):
        result = rs.validate_runtime_configs(_all_configs())
        self.assertEqual(len(result), 6)

    def test_validate_missing_service(self):
        configs = _all_configs()
        del configs["gh-proxy-b"]
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_runtime_configs(configs)

    def test_create_and_remove(self):
        validated = rs.validate_runtime_configs(_all_configs())
        created = rs.create_runtime_files(
            validated, directory=self.dir, journal=self.journal)
        self.assertEqual(len(created), 6)
        self.assertEqual(len(self.journal), 6)
        # files exist
        for service in rs.SERVICE_RUNTIME_SPECS:
            spec = rs.SERVICE_RUNTIME_SPECS[service]
            path = self.dir / spec["env_file"]
            self.assertTrue(path.exists(), service)
        # remove
        removed = rs.remove_runtime_files(
            directory=self.dir, journal=self.journal)
        self.assertEqual(len(removed), 6)
        self.assertEqual(len(self.journal), 0)

    def test_refuse_overwrite(self):
        validated = rs.validate_runtime_configs(_all_configs())
        rs.create_runtime_files(
            validated, directory=self.dir, journal=self.journal)
        # attempt to create again with DIFFERENT content
        changed = dict(validated)
        changed["gh-proxy-r"] = dict(validated["gh-proxy-r"])
        changed["gh-proxy-r"]["GH_PROXY_PORT"] = "9999"
        with self.assertRaises(rs.RuntimeSpecError):
            rs.create_runtime_files(
                changed, directory=self.dir,
                journal={})

    def test_idempotent_same_content(self):
        validated = rs.validate_runtime_configs(_all_configs())
        rs.create_runtime_files(
            validated, directory=self.dir, journal=self.journal)
        journal2 = {}
        rs.create_runtime_files(
            validated, directory=self.dir, journal=journal2)
        self.assertEqual(len(journal2), 6)

    def test_journal_no_secret_values(self):
        validated = rs.validate_runtime_configs(_all_configs())
        rs.create_runtime_files(
            validated, directory=self.dir, journal=self.journal)
        blob = str(self.journal)
        self.assertNotIn("fake-pat", blob)
        self.assertNotIn("tok-", blob)
        self.assertNotIn("postgresql://", blob)

    def test_mounts_single_file_ro(self):
        for service in ("controller", "policy-gateway",
                        "gh-reporter"):
            mounts = rs.plan_runtime_mounts(service)
            for i in range(0, len(mounts), 2):
                self.assertEqual(mounts[i], "-v")
                mount = mounts[i + 1]
                self.assertTrue(mount.endswith(":ro"),
                                "%s mount not :ro: %s"
                                % (service, mount))
                # not a directory mount
                self.assertNotIn("/:", mount)

    def test_proxy_no_mounts(self):
        for service in ("gh-proxy-r", "gh-proxy-b", "mcp-bridge"):
            mounts = rs.plan_runtime_mounts(service)
            self.assertEqual(len(mounts), 0,
                             "%s should have no mounts" % service)


# ── §5: Gateway semantic health adapter ────────────────────────────────────

class TestGatewaySemanticHealth(unittest.TestCase):

    def test_read_only_tools_frozen(self):
        expected = {"get_pull_request", "get_pull_request_files",
                    "get_file_contents", "get_branch"}
        self.assertEqual(set(rs.GATEWAY_READ_ONLY_TOOLS), expected)

    def test_stub_upstream_not_e2e(self):
        self.assertNotEqual(rs.GATEWAY_E2E_UPSTREAM,
                            "http://127.0.0.1:8084/sse")

    def test_e2e_policy_frozen(self):
        self.assertEqual(rs.GATEWAY_E2E_POLICY,
                         "/run/mergepilot/policy-fixture.yaml")


# ── §7: Reporter proxy boundary ────────────────────────────────────────────

class TestReporterBoundary(unittest.TestCase):

    def test_reporter_no_pat_in_forbidden(self):
        spec = rs.SERVICE_RUNTIME_SPECS["gh-reporter"]
        self.assertIn("fine_grained_pat", spec["forbidden_secrets"])

    def test_pem_mount_only_reporter(self):
        for service, spec in rs.SERVICE_RUNTIME_SPECS.items():
            mounts = spec.get("mounts", [])
            has_pem = any(m[0] == "github_app_pem" for m in mounts)
            if service == "gh-reporter":
                self.assertTrue(has_pem,
                                "reporter must have PEM mount")
            else:
                self.assertFalse(has_pem,
                                 "%s must NOT have PEM mount" % service)

    def test_reporter_static_token_still_forbidden(self):
        # This is a source-level check but the behavioral test
        # exists in test_checks_mapping_and_reporter.py
        source = (ROOT / "tools" / "gh-app" /
                  "checks_reporter.py").read_text(encoding="utf-8")
        self.assertIn("forbidden in production", source)


if __name__ == "__main__":
    unittest.main()
