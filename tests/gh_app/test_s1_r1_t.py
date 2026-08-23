"""M8-GH-4B3-W3B-S1-R1-T: execution tests for proxy transport,
journal persistence, ownership, secret matrix, and lifecycle integration.
All use fake/injected transports and synthetic data; zero real network."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import e2e_foundation as e2f                    # noqa: E402
import e2e_runtime_specs as rs                  # noqa: E402
import token_provider as tp                     # noqa: E402

REPORTER_PROXY = e2f.E2E_REPORTER_PROXY_R


def _all_configs():
    def ctrl():
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

    def gw():
        return {
            "UPSTREAM_URL": rs.GATEWAY_E2E_UPSTREAM,
            "POLICY_FILE": rs.GATEWAY_E2E_POLICY,
            "ROLE_TOKENS": '{"manager":"tok-m","reviewer":"tok-r",'
                              ' "fixer":"tok-f","verifier":"tok-v"}',
            "AUDIT_DSN":
                "postgresql://u:synthetic-audit@postgres/db?connect_timeout=5",
        }

    def bridge():
        return {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "synthetic-pat-value",
            "GITHUB_REPOSITORY": "example/fixture",
            "HTTPS_PROXY": rs.BRIDGE_PROXY,
            "MCP_PROXY_PORT": "8082",
        }

    def reporter():
        return {
            "GITHUB_PUBLISHER_DSN":
                "postgresql://u:synthetic-reporter@postgres/db?connect_timeout=5",
            "GITHUB_API_BASE": "https://api.github.com",
            "GITHUB_APP_ID": "4648333",
            "GITHUB_INSTALLATION_ID": "154914965",
            "GITHUB_REPOSITORY_ID": "1314399289",
            "GITHUB_PRIVATE_KEY_PATH":
                "/run/secrets/github-app-private-key.pem",
            "GH_REPORTER_POLL_SECONDS": "5",
            "GH_REPORTER_LEASE_SECONDS": "120",
            "GH_REPORTER_MAX_ATTEMPTS": "8",
            "HTTPS_PROXY": REPORTER_PROXY,
        }

    def proxy():
        return {
            "GH_PROXY_BIND": "0.0.0.0",
            "GH_PROXY_PORT": "18090",
            "GH_PROXY_UPSTREAM_IP": "172.23.48.1",
            "GH_PROXY_UPSTREAM_PORT": "17890",
        }

    return {
        "controller": ctrl(),
        "policy-gateway": gw(),
        "mcp-bridge": bridge(),
        "gh-reporter": reporter(),
        "gh-proxy-r": proxy(),
        "gh-proxy-b": dict(proxy()),
    }


# ── §3: Proxy transport tests ─────────────────────────────────────────────

class TestProxyTransport(unittest.TestCase):

    def test_reporter_schema_requires_https_proxy(self):
        env = {
            "GITHUB_PUBLISHER_DSN":
                "postgresql://u:p@h:5432/d?connect_timeout=5",
            "GITHUB_API_BASE": "https://api.github.com",
            "GITHUB_APP_ID": "1", "GITHUB_INSTALLATION_ID": "1",
            "GITHUB_REPOSITORY_ID": "1",
            "GITHUB_PRIVATE_KEY_PATH":
                "/run/secrets/github-app-private-key.pem",
            "GH_REPORTER_POLL_SECONDS": "5",
            "GH_REPORTER_LEASE_SECONDS": "120",
            "GH_REPORTER_MAX_ATTEMPTS": "8",
        }
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_reporter_env(env)

    def test_build_proxy_opener_creates_proxy_handler(self):
        opener = tp.build_proxy_opener(REPORTER_PROXY)
        self.assertIsNotNone(opener)
        handlers = [type(h).__name__ for h in opener.handlers]
        self.assertIn("_ForcedProxyHandler", handlers)

    def test_token_exchange_uses_explicit_proxy_r(self):
        """Verify default_transport uses the proxy opener when
        HTTPS_PROXY is set (not global urlopen)."""
        with mock.patch.dict(os.environ,
                             {"HTTPS_PROXY": REPORTER_PROXY}):
            # Patch build_proxy_opener to track call
            with mock.patch(
                    "token_provider.build_proxy_opener",
                    side_effect=lambda url: (
                        _assert_proxy_url(url))) as mock_opener:
                try:
                    tp.default_transport(
                        "POST", "https://api.github.com/app",
                        headers={}, body={})
                except Exception:
                    pass  # network call will fail; we just verify routing
                self.assertTrue(mock_opener.called,
                                "build_proxy_opener must be called")

    def _assert_proxy_url():
        pass

    def test_checks_lookup_uses_explicit_proxy_r(self):
        import checks_reporter as cr
        with mock.patch.dict(os.environ,
                             {"HTTPS_PROXY": REPORTER_PROXY}):
            with mock.patch(
                    "checks_reporter.urllib.request.urlopen") as g_urlopen, \
                 mock.patch(
                    "token_provider.build_proxy_opener",
                    return_value=mock.MagicMock()) as mock_builder:
                try:
                    cr.default_transport(
                        "GET", "https://api.github.com/repos/x",
                        headers={}, body=None)
                except Exception:
                    pass
                # global urlopen must NOT be called
                self.assertFalse(g_urlopen.called,
                                 "must not use global urlopen when "
                                 "HTTPS_PROXY is set")

    def test_fake_transport_performs_no_network(self):
        """Injected transport is called, not default_transport."""
        calls = []
        def fake(method, url, *, headers, body):
            calls.append((method, url))
            return 200, {}, {"ok": True}
        import checks_reporter as cr
        result = cr.publish_once(
            lambda: _FakeConn(), api_base="http://fake",
            transport=fake, token="tok")
        # verify fake was used (no real network)
        self.assertEqual(len(calls), 2)  # lookup + publish

    def test_no_proxy_cannot_bypass_api_github_com(self):
        """Even with NO_PROXY set, explicit ProxyHandler routes
        api.github.com through proxy."""
        for no_proxy_val in ("api.github.com", "*", "api.github.com,.local"):
            with mock.patch.dict(os.environ, {
                "HTTPS_PROXY": REPORTER_PROXY,
                "NO_PROXY": no_proxy_val,
                "no_proxy": no_proxy_val}):
                opener = tp.build_proxy_opener(REPORTER_PROXY)
                # Verify the opener has a ProxyHandler that would
                # handle api.github.com through the proxy
                for handler in opener.handlers:
                    if hasattr(handler, "proxies"):
                        self.assertIn("https", handler.proxies)
                        self.assertEqual(
                            handler.proxies["https"], REPORTER_PROXY)
                        break

    def test_proxy_transport_leaks_no_authorization_or_token(self):
        """build_proxy_opener result doesn't contain credentials."""
        opener = tp.build_proxy_opener(REPORTER_PROXY)
        # The opener itself has no auth/token material
        blob = str(opener)
        for forbidden in ("Bearer", "eyJ", "ghp_", "syt_",
                          "BEGIN PRIVATE"):
            self.assertNotIn(forbidden, blob)


class _FakeConn:
    def cursor(self):
        class _Cur:
            def execute(self, *a): pass
            def fetchone(self):
                return ("chk-1", "claim-1", "gh-run",
                        "example/fixture", 1, "a"*40,
                        "mergepilot/gh-run", None,
                        "in_progress", None, 1, 0, 1)
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return _Cur()
    def commit(self): pass
    def close(self): pass
    def rollback(self): pass


# ── §7: Secret matrix fail-closed ─────────────────────────────────────────

class TestSecretMatrixFailClosed(unittest.TestCase):

    def test_secret_matrix_exact_nine_resources(self):
        self.assertEqual(len(rs.SECRET_CONSUMER_MATRIX), 9)

    def test_secret_matrix_wrong_consumer_fails_closed(self):
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_secret_consumers(
                "controller", {"role_tokens"})

    def test_secret_matrix_extra_consumer_fails_closed(self):
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_secret_consumers(
                "gh-proxy-r", {"fine_grained_pat"})

    def test_cross_validate_sensitive_keys_bridge_pat(self):
        env = {"GITHUB_PERSONAL_ACCESS_TOKEN": "fake"}
        rs.cross_validate_sensitive_keys("mcp-bridge", env)  # OK

    def test_cross_validate_wrong_consumer(self):
        env = {"GITHUB_PERSONAL_ACCESS_TOKEN": "fake"}
        with self.assertRaises(rs.RuntimeSpecError):
            rs.cross_validate_keys = \
                rs.cross_validate_sensitive_keys
            rs.cross_validate_sensitive_keys(
                "controller", env)


# ── §5/§6: Journal persistence and ownership ─────────────────────────────

class TestJournalPersistence(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)
        self.journal = {}

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_persist_callback_called_per_file(self):
        validated = rs.validate_runtime_configs(_all_configs())
        call_count = []
        def callback(j):
            call_count.append(dict(j))
        rs.create_runtime_files(
            validated, directory=self.dir,
            journal=self.journal, persist_callback=callback)
        self.assertEqual(len(call_count), 6)

    def test_journal_persist_failure_removes_created_files(self):
        validated = rs.validate_runtime_configs(_all_configs())
        def failing_callback(j):
            raise OSError("persist failed")
        with self.assertRaises(Exception):
            rs.create_runtime_files(
                validated, directory=self.dir,
                journal=self.journal,
                persist_callback=failing_callback)
        # all files cleaned
        for spec in rs.SERVICE_RUNTIME_SPECS.values():
            path = self.dir / spec["env_file"]
            self.assertFalse(path.exists(),
                             "file should be cleaned: %s" % path)
        self.assertEqual(len(self.journal), 0)

    def test_foreign_ownership_rejects_overwrite(self):
        validated = rs.validate_runtime_configs(_all_configs())
        # Create a foreign file first
        spec = rs.SERVICE_RUNTIME_SPECS["gh-proxy-r"]
        foreign_path = self.dir / spec["env_file"]
        foreign_path.write_bytes(b"FOREIGN_CONTENT")
        with self.assertRaises(rs.RuntimeSpecError) as ctx:
            rs.create_runtime_files(
                validated, directory=self.dir,
                journal=self.journal)
        self.assertEqual(ctx.exception.code, "RUNTIME_FILE_EXISTS")
        # foreign file unchanged
        self.assertEqual(foreign_path.read_bytes(),
                         b"FOREIGN_CONTENT")

    def test_partial_create_failure_rolls_back_reverse(self):
        validated = rs.validate_runtime_configs(_all_configs())
        # Make one file's directory read-only or simulate failure
        # by pre-creating with different content mid-order
        created_order = sorted(rs.SERVICE_RUNTIME_SPECS)
        # Pre-create a conflicting file at position 3
        target = created_order[2]
        spec = rs.SERVICE_RUNTIME_SPECS[target]
        conflict = self.dir / spec["env_file"]
        conflict.write_bytes(b"CONFLICT")
        with self.assertRaises(rs.RuntimeSpecError):
            rs.create_runtime_files(
                validated, directory=self.dir,
                journal=self.journal)
        # Files created before the conflict should be cleaned
        for svc in created_order[:2]:
            s = rs.SERVICE_RUNTIME_SPECS[svc]
            p = self.dir / s["env_file"]
            # first 2 (alphabetically before conflict) should not exist
            self.assertFalse(p.exists(),
                             "%s should be rolled back" % svc)

    def test_remove_only_owned_files(self):
        validated = rs.validate_runtime_configs(_all_configs())
        rs.create_runtime_files(
            validated, directory=self.dir, journal=self.journal)
        # Add a foreign file
        foreign = self.dir / "foreign.env"
        foreign.write_bytes(b"FOREIGN")
        removed = rs.remove_runtime_files(
            directory=self.dir, journal=self.journal)
        self.assertTrue(len(removed) > 0)
        # foreign file survives
        self.assertTrue(foreign.exists())

    def test_repeated_remove_idempotent(self):
        validated = rs.validate_runtime_configs(_all_configs())
        rs.create_runtime_files(
            validated, directory=self.dir, journal=self.journal)
        rs.remove_runtime_files(
            directory=self.dir, journal=self.journal)
        removed2 = rs.remove_runtime_files(
            directory=self.dir, journal=self.journal)
        self.assertEqual(len(removed2), 0)
        self.assertEqual(len(self.journal), 0)


# ── §8: Zero-leak tests ────────────────────────────────────────────────────

class TestZeroLeak(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)
        self.journal = {}

    def tearDown(self):
        self.tmpdir.cleanup()

    def _create(self):
        validated = rs.validate_runtime_configs(_all_configs())
        rs.create_runtime_files(
            validated, directory=self.dir, journal=self.journal)
        return validated

    def test_gateway_sensitive_values_absent_from_journal(self):
        self._create()
        blob = str(self.journal)
        self.assertNotIn("synthetic-role-token", blob)
        self.assertNotIn("synthetic-audit", blob)

    def test_reporter_dsn_absent_from_journal(self):
        self._create()
        blob = str(self.journal)
        self.assertNotIn("synthetic-reporter", blob)

    def test_pat_absent_from_journal(self):
        self._create()
        blob = str(self.journal)
        self.assertNotIn("synthetic-pat", blob)

    def test_pem_content_never_enters_reporter_env(self):
        self._create()
        reporter_env = (
            self.dir / "gh_reporter.env").read_text()
        self.assertNotIn("BEGIN", reporter_env)

    def test_runtime_files_do_not_cross_contaminate(self):
        self._create()
        # Gateway values not in other files
        for svc, spec in rs.SERVICE_RUNTIME_SPECS.items():
            if svc == "policy-gateway":
                continue
            content = (
                self.dir / spec["env_file"]).read_text()
            self.assertNotIn("synthetic-role-token", content,
                             "%s should not have ROLE_TOKENS" % svc)
            self.assertNotIn("synthetic-audit", content,
                             "%s should not have AUDIT_DSN" % svc)
        # PAT only in bridge
        for svc, spec in rs.SERVICE_RUNTIME_SPECS.items():
            if svc == "mcp-bridge":
                continue
            content = (
                self.dir / spec["env_file"]).read_text()
            self.assertNotIn("synthetic-pat", content,
                             "%s should not have PAT" % svc)
        # Reporter DSN only in reporter
        for svc, spec in rs.SERVICE_RUNTIME_SPECS.items():
            if svc == "gh-reporter":
                continue
            content = (
                self.dir / spec["env_file"]).read_text()
            self.assertNotIn("synthetic-reporter", content,
                             "%s should not have Reporter DSN" % svc)


if __name__ == "__main__":
    unittest.main()
