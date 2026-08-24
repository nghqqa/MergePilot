"""ISOLATED_LIVE productization Phase 1 — one-click startup Mock/static tests.

No WSL/Docker/PostgreSQL is started; no real connection is opened; the
unauthorized path performs zero real subprocess calls.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(ROOT), str(ROOT / "tools" / "demo_console")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import one_click_startup as oc  # noqa: E402
from one_click_startup import (  # noqa: E402
    PREFLIGHT_CHECKS,
    SERVICE_ORDER,
    SecretFile,
    StartupCleanupError,
    StartupGateError,
    assert_argv_safe,
    build_compose_config,
    compose_dependency_order,
    compose_ports_binding,
    one_click_cleanup,
    redact,
    run_preflight_gates,
    validate_compose_config,
)


def _gate(testcase, fn, *args, code, **kwargs):
    with testcase.assertRaises(StartupGateError) as cm:
        fn(*args, **kwargs)
    testcase.assertEqual(cm.exception.code, code, msg=str(cm.exception))
    return cm.exception


# ── Compose configuration ────────────────────────────────────────────────────

class TestComposeConfig(unittest.TestCase):

    def test_default_config_valid(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        validate_compose_config(cfg)

    def test_all_five_services_present(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        for name in SERVICE_ORDER:
            self.assertIn(name, cfg["services"])

    def test_postgres_image_digest_pinned(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        # §4 offline pin: compose declares the byte-exact config ID
        # (survives docker save/load); the manifest digest stays
        # recorded as PGVECTOR_IMAGE_DIGEST for provenance
        self.assertEqual(cfg["services"]["postgres"]["image"],
                         oc.PGVECTOR_IMAGE_ID)
        self.assertIn("sha256:", cfg["services"]["postgres"]["image"])
        digest = cfg["services"]["postgres"]["image"].split("sha256:")[1]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_no_implicit_pull_anywhere(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        for name, svc in cfg["services"].items():
            self.assertEqual(svc.get("pull_policy"), "never",
                             "service %s allows implicit pull" % name)

    def test_only_postgres_has_literal_image(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        for name, svc in cfg["services"].items():
            if name == "postgres":
                continue
            self.assertNotIn("image", svc,
                             "service %s uses an unpinned image" % name)

    def test_postgres_never_published(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        self.assertNotIn("ports", cfg["services"]["postgres"])

    def test_demo_console_loopback_only(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        # 1-G network design: demo-console is UNPUBLISHED; the loopback
        # publish lives on the secretless console-edge.
        self.assertIsNone(cfg["services"]["demo-console"].get("ports"))
        ports = cfg["services"]["console-edge"]["ports"]
        self.assertEqual(len(ports), 1)
        bind = ports[0].split(":")[0]
        # §3: the distro-side backend bind; the Windows loopback edge
        # (forwarder) is the enforcement point
        self.assertEqual(bind, oc.PUBLISH_BIND)
        # §3: the TWO publication ports bind the distro-side PUBLISH_BIND
        # backend (inside the WSL VM, reachable from the Windows host via
        # the WSL NAT); the Windows loopback edge is the enforcement
        # point. LAN/IPv6 addresses still never appear anywhere.
        for bad in ("::", "192.168.1.5", "10.0.0.1"):
            self.assertNotIn(bad, json.dumps(cfg))
        for name, svc in cfg["services"].items():
            for p in (svc.get("ports") or []):
                self.assertTrue(
                    str(p).startswith(oc.PUBLISH_BIND + ":"),
                    "publish %r outside the PUBLISH_BIND backend "
                    "contract: %s" % (p, name))

    def test_no_volumes(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        self.assertEqual(cfg.get("volumes"), {})

    def test_internal_network(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        self.assertTrue(cfg["networks"]["isolated"]["internal"])

    def test_healthcheck_present(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        hc = cfg["services"]["postgres"]["healthcheck"]
        self.assertIn("pg_isready", hc["test"][-1])
        self.assertEqual(int(hc["retries"]), 10)

    def test_dependency_order_chain(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        order = compose_dependency_order(cfg)
        self.assertEqual(order[0], "postgres")
        self.assertEqual(order[-1], "preflight")
        self.assertEqual(set(order), set(SERVICE_ORDER))
        # Each service's deps appear earlier in the order.
        services = cfg["services"]
        for name in order:
            for dep in (services[name].get("depends_on") or {}):
                self.assertLess(order.index(dep), order.index(name))

    def test_dependency_conditions(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        deps = cfg["services"]["policy-gateway"]["depends_on"]
        self.assertEqual(deps["postgres"]["condition"], "service_healthy")
        deps = cfg["services"]["controller"]["depends_on"]
        self.assertEqual(deps["postgres"]["condition"], "service_healthy")
        # v3 Fix 3: healthy, not merely started.
        self.assertEqual(deps["policy-gateway"]["condition"], "service_healthy")
        deps = cfg["services"]["demo-console"]["depends_on"]
        self.assertEqual(deps["controller"]["condition"], "service_healthy")

    def test_ports_binding_audit_hook(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        bindings = compose_ports_binding(cfg)
        # M8-GH-3: exactly two loopback publications (console-edge 8600,
        # gh-webhook 8090).
        self.assertEqual(sorted(bindings.keys()),
                         ["console-edge", "gh-webhook"])
        # §3: distro-side PUBLISH_BIND backend for BOTH publications
        self.assertTrue(all(b.startswith(oc.PUBLISH_BIND + ":")
                            for b in bindings["console-edge"]))
        self.assertEqual(bindings["gh-webhook"],
                         ["%s:8090:8090" % oc.PUBLISH_BIND])

    # ── negative config cases ───────────────────────────────────────────────

    def _mutated(self, **mutate):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        for path, value in mutate.items():
            obj = cfg
            keys = path.split(".")
            for k in keys[:-1]:
                obj = obj[k]
            obj[keys[-1]] = value
        return cfg

    def test_missing_service_rejected(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        del cfg["services"]["controller"]
        _gate(self, validate_compose_config, cfg, code="COMPOSE_INVALID")

    def test_wrong_digest_rejected(self):
        # §4: the floating TAG is rejected — only the byte-exact
        # config ID (or the recorded manifest digest) may be declared
        cfg = self._mutated(**{"services.postgres.image":
                               "pgvector/pgvector:pg16"})
        _gate(self, validate_compose_config, cfg, code="IMAGE_DIGEST_MISMATCH")
        cfg = self._mutated(**{"services.postgres.image":
                               "sha256:" + "ab" * 32})
        _gate(self, validate_compose_config, cfg, code="IMAGE_DIGEST_MISMATCH")

    def test_pull_policy_not_never_rejected(self):
        cfg = self._mutated(**{"services.controller.pull_policy": "always"})
        _gate(self, validate_compose_config, cfg, code="COMPOSE_INVALID")

    def test_unpinned_image_on_other_service_rejected(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        cfg["services"]["controller"]["image"] = "some/image:latest"
        _gate(self, validate_compose_config, cfg, code="COMPOSE_INVALID")

    def test_postgres_published_rejected(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        cfg["services"]["postgres"]["ports"] = ["5432:5432"]
        _gate(self, validate_compose_config, cfg, code="BIND_NOT_LOOPBACK")

    def test_non_loopback_bind_rejected(self):
        for bad in ("0.0.0.0:8600:8600", ":::8600:8600", "192.168.0.1:8600:8600"):
            cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
            cfg["services"]["demo-console"]["ports"] = [bad]
            _gate(self, validate_compose_config, cfg, code="BIND_NOT_LOOPBACK")

    def test_other_service_published_rejected(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        cfg["services"]["controller"]["ports"] = ["127.0.0.1:9999:9999"]
        _gate(self, validate_compose_config, cfg, code="BIND_NOT_LOOPBACK")

    def test_missing_healthcheck_rejected(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        del cfg["services"]["postgres"]["healthcheck"]
        _gate(self, validate_compose_config, cfg, code="COMPOSE_INVALID")

    def test_wrong_dependencies_rejected(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        cfg["services"]["demo-console"]["depends_on"] = {
            "postgres": {"condition": "service_healthy"}}
        _gate(self, validate_compose_config, cfg, code="COMPOSE_INVALID")

    def test_external_network_rejected(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        cfg["networks"]["isolated"]["internal"] = False
        _gate(self, validate_compose_config, cfg, code="COMPOSE_INVALID")

    def test_volumes_rejected(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        cfg["volumes"] = {"pgdata": {}}
        _gate(self, validate_compose_config, cfg, code="COMPOSE_INVALID")

    def test_bad_port_rejected(self):
        _gate(self, build_compose_config, demo_console_port=0,
              code="CONFIG_INVALID")
        _gate(self, build_compose_config, demo_console_port=70000,
              code="CONFIG_INVALID")

    def test_circular_dependency_detected(self):
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        cfg["services"]["postgres"]["depends_on"] = {
            "preflight": {"condition": "service_started"}}
        _gate(self, compose_dependency_order, cfg, code="COMPOSE_INVALID")


# ── Preflight matrix ─────────────────────────────────────────────────────────

class TestPreflightMatrix(unittest.TestCase):

    @staticmethod
    def _checks(**overrides):
        checks = {name: (lambda: True) for name in PREFLIGHT_CHECKS}
        checks.update(overrides)
        return checks

    def test_all_pass(self):
        out = run_preflight_gates(self._checks())
        self.assertTrue(out["ok"])
        self.assertEqual(out["executed"], list(PREFLIGHT_CHECKS))

    def test_missing_check_rejected(self):
        checks = self._checks()
        del checks["http_endpoint"]
        _gate(self, run_preflight_gates, checks, code="CONFIG_INVALID")

    def test_first_failure_stops_matrix(self):
        calls = []
        def fail_marker():
            calls.append("reader_acl")
            raise StartupGateError("DB_PREREQUISITE_MISSING", "acl")
        def after():
            calls.append("read_only_transaction")
            return True
        checks = self._checks(reader_acl=fail_marker,
                              read_only_transaction=after)
        out = run_preflight_gates(checks)
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_check"], "reader_acl")
        self.assertEqual(out["error_code"], "DB_PREREQUISITE_MISSING")
        self.assertNotIn("read_only_transaction", calls,
                         "matrix must stop at the first failure")

    def test_every_check_can_fail_with_its_stable_code(self):
        codes = {
            "docker_daemon_identity": "DAEMON_IDENTITY_MISMATCH",
            "image_digest_cached": "IMAGE_DIGEST_MISMATCH",
            "postgres_health": "PG_NOT_READY",
            "database_connectivity": "PG_CONNECT_FAILED",
            "server_identity": "WRONG_SERVER",
            "environment_marker": "ENVIRONMENT_ID_MISMATCH",
            "reader_acl": "DB_PREREQUISITE_MISSING",
            "read_only_transaction": "NOT_READ_ONLY",
            "source_kind": "KIND_MISMATCH",
            "http_endpoint": "HTTP_ENDPOINT_FAILED",
        }
        for check, code in codes.items():
            def fail(code=code):
                raise StartupGateError(code, "boom")
            out = run_preflight_gates(self._checks(**{check: fail}))
            self.assertFalse(out["ok"], check)
            self.assertEqual(out["failed_check"], check)
            self.assertEqual(out["error_code"], code)

    def test_unauthorized_path_zero_real_calls(self):
        # Pure config/validation performs zero subprocess/network calls.
        cfg = build_compose_config(demo_console_run_id="test-run-1",
                                demo_console_pg_server_addresses="172.18.0.2")
        validate_compose_config(cfg)
        compose_dependency_order(cfg)
        out = run_preflight_gates(self._checks())
        self.assertTrue(out["ok"])


# ── Secret file transport ────────────────────────────────────────────────────

class TestSecretFile(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_read_delete(self):
        sf = SecretFile(self.dir)
        self.assertFalse(sf.exists())
        sf.write("admin-pw-abc", "reader-pw-xyz")
        self.assertTrue(sf.exists())
        self.assertEqual(sf.path.name, "postgres.env")
        self.assertNotIn("secret", sf.path.name.lower())
        content = sf.path.read_text(encoding="utf-8")
        self.assertIn("admin-pw-abc", content)
        self.assertIn("reader-pw-xyz", content)
        sf.delete()
        self.assertFalse(sf.exists())

    def test_delete_idempotent(self):
        sf = SecretFile(self.dir)
        sf.delete()
        sf.delete()
        self.assertFalse(sf.exists())

    def test_refuses_overwrite(self):
        sf = SecretFile(self.dir)
        sf.write("a", "b")
        _gate(self, sf.write, "c", "d", code="SECRET_FILE_EXISTS")

    def test_password_never_in_filename(self):
        sf = SecretFile(self.dir)
        sf.write("supersecret-admin", "supersecret-reader")
        self.assertEqual(sf.path.name, "postgres.env")
        self.assertNotIn("supersecret", str(sf.path))


# ── Redaction + argv safety ──────────────────────────────────────────────────

class TestRedactionAndArgv(unittest.TestCase):

    def test_redact_dsn_password_literal_token(self):
        text = ("postgresql://u:pw@h/db password=hunter2abc "
                "PASSWORD 'zz' ghp_" + "a" * 36)
        out = redact(text)
        self.assertNotIn("pw@h", out)
        self.assertNotIn("hunter2abc", out)
        self.assertNotIn("'zz'", out)
        self.assertNotIn("ghp_", out)
        self.assertIn("***REDACTED***", out)

    def test_argv_rejects_secret(self):
        _gate(self, assert_argv_safe, ["cmd", "pw123"],
              ["pw123"], code="ARGV_SECRET_LEAK")

    def test_argv_rejects_dsn(self):
        _gate(self, assert_argv_safe,
              ["x", "postgresql://u:pw@h/db"], code="ARGV_SECRET_LEAK")

    def test_argv_rejects_sql_password_literal(self):
        _gate(self, assert_argv_safe,
              ["x", "PASSWORD 'abc'"], code="ARGV_SECRET_LEAK")

    def test_argv_accepts_clean(self):
        assert_argv_safe(["docker", "compose", "up", "-d"], ["absent"])


# ── Cleanup ──────────────────────────────────────────────────────────────────

class TestCleanup(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_success(self):
        sf = SecretFile(self.dir)
        sf.write("a", "b")
        stop = mock.Mock()
        one_click_cleanup(sf, stop)
        stop.assert_called_once()
        self.assertFalse(sf.exists())

    def test_retry_safe(self):
        sf = SecretFile(self.dir)
        stop = mock.Mock()
        one_click_cleanup(sf, stop)   # first
        one_click_cleanup(sf, stop)   # retry is a clean no-op
        self.assertEqual(stop.call_count, 2)

    def test_stop_failure_code(self):
        sf = SecretFile(self.dir)
        sf.write("a", "b")
        def bad_stop():
            raise RuntimeError("boom")
        with self.assertRaises(StartupCleanupError) as cm:
            one_click_cleanup(sf, bad_stop)
        self.assertIn("STACK_STOP_FAILED", cm.exception.cleanup_codes)
        # secret file still deleted despite stop failure
        self.assertFalse(sf.exists())

    def test_secret_delete_failure_code(self):
        sf = SecretFile(self.dir)
        sf.write("a", "b")
        with mock.patch.object(sf, "delete", side_effect=OSError("boom")):
            with self.assertRaises(StartupCleanupError) as cm:
                one_click_cleanup(sf, None)
        self.assertIn("SECRET_DELETE_FAILED", cm.exception.cleanup_codes)

    def test_both_codes_preserved(self):
        sf = SecretFile(self.dir)
        sf.write("a", "b")
        def bad_stop():
            raise RuntimeError("b")
        with mock.patch.object(sf, "delete", side_effect=OSError("b")):
            with self.assertRaises(StartupCleanupError) as cm:
                one_click_cleanup(sf, bad_stop)
        self.assertEqual(cm.exception.primary_code, "CLEANUP_RESIDUE")
        for code in ("STACK_STOP_FAILED", "SECRET_DELETE_FAILED"):
            self.assertIn(code, cm.exception.cleanup_codes)

    def test_primary_not_masked_by_cleanup(self):
        # A primary gate error raised BEFORE cleanup still surfaces intact
        # even when cleanup also fails.
        sf = SecretFile(self.dir)
        sf.write("a", "b")
        try:
            try:
                raise StartupGateError("IMAGE_DIGEST_MISMATCH", "preflight")
            finally:
                try:
                    one_click_cleanup(sf, None)
                except StartupCleanupError:
                    pass  # cleanup error never masks the primary
        except StartupGateError as exc:
            self.assertEqual(exc.code, "IMAGE_DIGEST_MISMATCH")


if __name__ == "__main__":
    unittest.main()
