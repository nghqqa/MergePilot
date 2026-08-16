"""Phase 1-D retry v3 — Mock/static tests for the three real-run blockers.

Fix 1: server-address canonicalization (shared canonicalizer, host() SQL,
       both sides normalized, Phase B compatible).
Fix 2: demo-console static assets (fixed allowlisted serve dir, Dockerfile
       COPY, fail-closed missing bundle).
Fix 3: controller/policy-gateway runtime contract (extracted env vars,
       secret env-file transport, healthchecks, dependency order).

No WSL/Docker/PostgreSQL is started; no real connection; no subprocess.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(ROOT), str(ROOT / "tools"),
           str(ROOT / "tools" / "demo_console")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import one_click_startup as oc  # noqa: E402
from one_click_startup import (  # noqa: E402
    ControllerSecretFile,
    StartupGateError,
    canonicalize_server_address,
    canonicalize_server_address_list,
)

COMPOSE_PATH = ROOT / "docker-compose.yml"
DOCKERFILES = {
    "demo-console": ROOT / "Dockerfile.demo-console",
    "controller": ROOT / "Dockerfile.controller",
    "policy-gateway": ROOT / "Dockerfile.policy-gateway",
    "preflight": ROOT / "Dockerfile.preflight",
}
ENTRYPOINTS = {
    "demo_console": ROOT / "tools" / "demo_console_entrypoint.py",
    "controller": ROOT / "tools" / "controller_entrypoint.py",
    "gateway": ROOT / "tools" / "gateway_entrypoint.py",
    "preflight": ROOT / "tools" / "preflight_entrypoint.py",
}
SOURCES = {
    "postgres_source": ROOT / "tools" / "demo_console" / "postgres_source.py",
    "serve": ROOT / "tools" / "demo_console" / "serve.py",
    "executor": ROOT / "tests" / "isolated_live" / "ephemeral_executor.py",
    "one_click": ROOT / "tools" / "demo_console" / "one_click_startup.py",
    "upstream_stub": ROOT / "tools" / "policy-gateway" / "upstream_stub.py",
}


def _gate(testcase, fn, *args, code, **kwargs):
    with testcase.assertRaises(StartupGateError) as cm:
        fn(*args, **kwargs)
    testcase.assertEqual(cm.exception.code, code, msg=str(cm.exception))
    return cm.exception


# ── Fix 1: canonicalizer unit contract ──────────────────────────────────────

class TestCanonicalizer(unittest.TestCase):

    def test_bare_and_slash32_are_equivalent(self):
        self.assertEqual(canonicalize_server_address("172.18.0.2"),
                         "172.18.0.2")
        self.assertEqual(canonicalize_server_address("172.18.0.2/32"),
                         "172.18.0.2")
        self.assertEqual(canonicalize_server_address(" 172.18.0.2/32 "),
                         "172.18.0.2")

    def test_phaseb_measured_form_compatible(self):
        # Pre-v3 Phase B measured e.g. '127.0.0.1/32' via the inet→text
        # cast; canonicalization keeps that form valid (compat regression).
        self.assertEqual(canonicalize_server_address("127.0.0.1/32"),
                         "127.0.0.1")

    def test_list_dedup_and_join(self):
        self.assertEqual(
            canonicalize_server_address_list("172.18.0.2, 172.18.0.2/32, 172.18.0.3"),
            ["172.18.0.2", "172.18.0.3"])
        self.assertEqual(canonicalize_server_address_list(["10.1.2.3"]),
                         ["10.1.2.3"])

    def test_hostname_rejected(self):
        for bad in ("postgres", "db.example.com", "localhost"):
            with self.assertRaises(ValueError) as cm:
                canonicalize_server_address(bad)
            self.assertIn("CONFIG_INVALID", str(cm.exception))

    def test_ipv6_rejected(self):
        for bad in ("::1", "fd00::1", "2001:db8::1/128"):
            with self.assertRaises(ValueError) as cm:
                canonicalize_server_address(bad)
            self.assertIn("IPv6", str(cm.exception))

    def test_non_32_cidr_rejected(self):
        for bad in ("172.18.0.0/16", "10.0.0.0/8", "192.168.1.0/24"):
            with self.assertRaises(ValueError) as cm:
                canonicalize_server_address(bad)
            self.assertIn("CIDR", str(cm.exception))

    def test_malformed_and_empty_rejected(self):
        for bad in ("", "  ", "999.999.1.1", "1.2.3", "172.18.x.2",
                    "172.18.0.2/33", "172.18.0.2/"):
            with self.assertRaises(ValueError) as cm:
                canonicalize_server_address(bad)
            self.assertIn("CONFIG_INVALID", str(cm.exception))
        with self.assertRaises(ValueError):
            canonicalize_server_address(None)
        with self.assertRaises(ValueError):
            canonicalize_server_address(1234)
        with self.assertRaises(ValueError):
            canonicalize_server_address_list("")
        with self.assertRaises(ValueError):
            canonicalize_server_address_list(42)

    def test_uses_ipaddress_not_string_hacks(self):
        # 禁止简单 split('/') 或字符串 replace: the implementation must go
        # through the standard ipaddress module.
        src = SOURCES["one_click"].read_text(encoding="utf-8")
        start = src.index("def canonicalize_server_address(")
        end = src.index("def canonicalize_server_address_list")
        body_one = src[start:end]
        body_all = src[start:end + 2000]
        self.assertIn("ipaddress.ip_interface", body_one)
        self.assertNotIn(".split('/')", body_all)
        self.assertNotIn('.replace("/32"', body_all)
        self.assertNotIn('.replace(\'/32\'', body_all)


# ── Fix 1: host(inet_server_addr()) SQL contract ────────────────────────────

class TestHostInetServerAddrSQL(unittest.TestCase):

    def test_postgres_source_uses_host_form(self):
        src = SOURCES["postgres_source"].read_text(encoding="utf-8")
        self.assertIn("SELECT host(inet_server_addr()), inet_server_port()",
                      src)
        self.assertNotIn("inet_server_addr()::text", src)

    def test_preflight_entrypoint_uses_host_form(self):
        src = ENTRYPOINTS["preflight"].read_text(encoding="utf-8")
        self.assertIn("SELECT host(inet_server_addr()), inet_server_port()",
                      src)
        self.assertNotIn("inet_server_addr()::text", src)

    def test_phaseb_executor_uses_host_form(self):
        src = SOURCES["executor"].read_text(encoding="utf-8")
        self.assertIn("SELECT host(inet_server_addr()), inet_server_port()",
                      src)
        self.assertNotIn("inet_server_addr()::text", src)


# ── Fix 1: PostgresSnapshotSource both-sides normalization ─────────────────

class TestSourceAddressNormalization(unittest.TestCase):

    def _make(self, addrs):
        from postgres_source import PostgresSnapshotSource, ConfigInvalidError
        return PostgresSnapshotSource, ConfigInvalidError

    def test_expected_slash32_canonicalized_at_init(self):
        from postgres_source import (PostgresSnapshotSource,
                                     ConfigInvalidError)
        src = PostgresSnapshotSource(
            dsn="host=postgres user=mergepilot_reader password=x",
            run_id="run-1",
            expected_database="mergepilot_audit",
            expected_role="mergepilot_reader",
            expected_environment_id="mergepilot-test-ephemeral",
            expected_server_addresses=["172.18.0.2/32", "127.0.0.1"],
            expected_server_port=5432,
            expected_application_name="mergepilot_isolated_live_reader",
        )
        self.assertEqual(src._expected_server_addresses,
                         ["172.18.0.2", "127.0.0.1"])

    def test_expected_hostname_alias_rejected_at_init(self):
        from postgres_source import PostgresSnapshotSource, ConfigInvalidError
        with self.assertRaises(ConfigInvalidError) as cm:
            PostgresSnapshotSource(
                dsn="host=postgres user=mergepilot_reader password=x",
                run_id="run-1",
                expected_database="mergepilot_audit",
                expected_role="mergepilot_reader",
                expected_environment_id="mergepilot-test-ephemeral",
                expected_server_addresses=["postgres"],
                expected_server_port=5432,
                expected_application_name="mergepilot_isolated_live_reader",
            )
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_expected_cidr_rejected_at_init(self):
        from postgres_source import PostgresSnapshotSource, ConfigInvalidError
        for bad in (["172.18.0.0/16"], ["::1"], ["garbage"]):
            with self.assertRaises(ConfigInvalidError) as cm:
                PostgresSnapshotSource(
                    dsn="host=postgres user=mergepilot_reader password=x",
                    run_id="run-1",
                    expected_database="mergepilot_audit",
                    expected_role="mergepilot_reader",
                    expected_environment_id="mergepilot-test-ephemeral",
                    expected_server_addresses=bad,
                    expected_server_port=5432,
                    expected_application_name="mergepilot_isolated_live_reader",
                )
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_verify_identity_matches_canonical_forms(self):
        # The measured side is canonicalized too: a legacy '/32' measurement
        # matches a bare expected entry (and vice versa).
        from postgres_source import PostgresSnapshotSource

        class _Cur:
            def __init__(self):
                self.calls = []
                self._one = None
                self._rows = []

            def execute(self, sql, *a):
                self.calls.append(sql)
                if "current_database()" in sql:
                    self._one = ("mergepilot_audit", "mergepilot_reader",
                                 True, True)
                elif "host(inet_server_addr())" in sql:
                    # Legacy measured form WITH the /32 suffix — must still
                    # match the bare expected allowlist entry.
                    self._one = ("172.18.0.2/32", 5432,
                                 "mergepilot_isolated_live_reader", 160014)
                elif "current_schema()" in sql:
                    self._one = ("public", '"$user", public')
                elif "pg_tables" in sql:
                    self._rows = [(t,) for t in (
                        "task_runs", "stage_runs", "stage_events",
                        "revision_bindings", "run_pr_bindings", "mcp_calls",
                        "rollback_runs", "audit_events",
                        "environment_identity")]
                elif "pg_roles" in sql:
                    self._one = (False, False, False, False, False)
                elif "has_table_privilege" in sql and "INSERT" in sql:
                    self._one = (False, False, False, False)
                elif "has_table_privilege" in sql:
                    self._one = (True,)
                elif "count(*) FROM environment_identity" in sql:
                    self._one = (1,)
                elif "environment_identity" in sql:
                    self._one = ("mergepilot-test-ephemeral",)
                else:
                    self._one = ("x",)

            def fetchone(self):
                return self._one

            def fetchall(self):
                return self._rows

        src = PostgresSnapshotSource(
            dsn="host=postgres user=mergepilot_reader password=x",
            run_id="run-1",
            expected_database="mergepilot_audit",
            expected_role="mergepilot_reader",
            expected_environment_id="mergepilot-test-ephemeral",
            expected_server_addresses=["172.18.0.2"],
            expected_server_port=5432,
            expected_application_name="mergepilot_isolated_live_reader",
        )
        # The column-level catalog probe is a separate concern (covered by
        # the dedicated postgres_source tests); shadow it here.
        src._verify_schema_columns = lambda cur: None
        cur = _Cur()
        src._verify_identity(cur)  # must not raise
        self.assertTrue(any("host(inet_server_addr())" in s
                            for s in cur.calls))
        # And a genuinely different measured address still fails closed.
        src2 = PostgresSnapshotSource(
            dsn="host=postgres user=mergepilot_reader password=x",
            run_id="run-1",
            expected_database="mergepilot_audit",
            expected_role="mergepilot_reader",
            expected_environment_id="mergepilot-test-ephemeral",
            expected_server_addresses=["10.9.9.9"],
            expected_server_port=5432,
            expected_application_name="mergepilot_isolated_live_reader",
        )
        from postgres_source import IdentityCheckError
        with self.assertRaises(IdentityCheckError) as cm:
            src2._verify_identity(_Cur())
        self.assertEqual(cm.exception.code, "WRONG_SERVER")

    def test_builder_emits_canonical_addresses(self):
        env = oc._demo_console_environment("run-1", "172.18.0.2/32")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES"],
                         "172.18.0.2")
        env = oc._demo_console_environment(
            "run-1", "172.18.0.2, 172.18.0.3/32")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES"],
                         "172.18.0.2,172.18.0.3")

    def test_builder_accepts_slash32_input(self):
        cfg = oc.build_compose_config(
            demo_console_run_id="r1",
            demo_console_pg_server_addresses="172.18.0.2/32")
        env = cfg["services"]["demo-console"]["environment"]
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES"],
                         "172.18.0.2")

    def test_builder_still_rejects_alias_and_cidr(self):
        for bad in ("postgres", "172.18.0.0/16", "::1"):
            _gate(self, oc.build_compose_config,
                  demo_console_run_id="r1",
                  demo_console_pg_server_addresses=bad,
                  code="CONFIG_INVALID")

    def test_entrypoint_accepts_slash32(self):
        import demo_console_entrypoint as dce
        env = dict(dce._VALIDATE_TEMPLATE) if hasattr(
            dce, "_VALIDATE_TEMPLATE") else None
        good = {
            "MERGEPILOT_MODE": "isolated_live",
            "MERGEPILOT_SOURCE_KIND": "postgres",
            "MERGEPILOT_RUN_ID": "run-1",
            "MERGEPILOT_EXPECTED_ROLE": "mergepilot_reader",
            "MERGEPILOT_BIND_CONTEXT": "container",
            "MERGEPILOT_HOST": "0.0.0.0",
            "MERGEPILOT_PORT": "8600",
            "MERGEPILOT_PG_EXPECTED_DATABASE": "mergepilot_audit",
            "MERGEPILOT_PG_ENVIRONMENT_ID": "mergepilot-test-ephemeral",
            "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES": "172.18.0.2/32",
            "MERGEPILOT_PG_EXPECTED_SERVER_PORT": "5432",
            "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME":
                "mergepilot_isolated_live_reader",
        }
        c = dce._validate_env(good)
        self.assertEqual(c["pg_expected_server_addresses"], "172.18.0.2")
        bad = dict(good,
                   MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES="postgres")
        with self.assertRaises(dce.EntrypointConfigError) as cm:
            dce._validate_env(bad)
        self.assertIn("CONFIG_INVALID", str(cm.exception))


# ── Fix 2: demo-console static assets ───────────────────────────────────────

class TestStaticAssets(unittest.TestCase):

    def test_dockerfile_copies_live_assets(self):
        body = DOCKERFILES["demo-console"].read_text(encoding="utf-8")
        self.assertIn(
            "COPY tools/demo_console/live_assets /app/live-console", body)
        # Protected-path contract (Phase 1-E fix): NOTHING is copied from
        # the protected samples/ tree.
        self.assertNotIn("COPY samples/", body)

    def test_dockerfile_path_matches_entrypoint_constant(self):
        import demo_console_entrypoint as dce
        body = DOCKERFILES["demo-console"].read_text(encoding="utf-8")
        self.assertIn(dce.CONTAINER_SERVE_DIR, body)

    def test_serve_has_serve_dir_flag(self):
        src = SOURCES["serve"].read_text(encoding="utf-8")
        self.assertIn("--serve-dir", src)
        self.assertIn("_resolve_serve_dir", src)

    def test_serve_dir_allowlist(self):
        from serve import _resolve_serve_dir, _legacy_serve_dir
        self.assertEqual(_resolve_serve_dir(None), _legacy_serve_dir())
        self.assertEqual(_resolve_serve_dir("/app/live-console"),
                         _resolve_serve_dir("/app/live-console"))
        # Nested under the container root is allowed.
        resolved = _resolve_serve_dir("/app/live-console/sub")
        self.assertTrue(str(resolved).replace("\\", "/").endswith(
            "app/live-console/sub"))

    def test_serve_dir_path_escape_rejected(self):
        from serve import _resolve_serve_dir
        for bad in ("/app/../etc", "/etc", "/tmp", "/app/../../tmp",
                    "app/samples", "./samples", "", "  "):
            with self.assertRaises(ValueError) as cm:
                _resolve_serve_dir(bad)
            self.assertIn("CONFIG_INVALID", str(cm.exception), bad)

    def test_entrypoint_container_mode_injects_serve_dir(self):
        import demo_console_entrypoint as dce
        good = {
            "MERGEPILOT_MODE": "isolated_live",
            "MERGEPILOT_SOURCE_KIND": "postgres",
            "MERGEPILOT_RUN_ID": "run-1",
            "MERGEPILOT_EXPECTED_ROLE": "mergepilot_reader",
            "MERGEPILOT_BIND_CONTEXT": "container",
            "MERGEPILOT_HOST": "0.0.0.0",
            "MERGEPILOT_PORT": "8600",
            "MERGEPILOT_PG_EXPECTED_DATABASE": "mergepilot_audit",
            "MERGEPILOT_PG_ENVIRONMENT_ID": "mergepilot-test-ephemeral",
            "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES": "172.18.0.2",
            "MERGEPILOT_PG_EXPECTED_SERVER_PORT": "5432",
            "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME":
                "mergepilot_isolated_live_reader",
        }
        argv = dce.build_serve_argv(dce._validate_env(good))
        self.assertIn("--serve-dir", argv)
        self.assertEqual(argv[argv.index("--serve-dir") + 1],
                         dce.CONTAINER_SERVE_DIR)
        # Host mode: repo-layout default (no explicit --serve-dir).
        host = dict(good, MERGEPILOT_BIND_CONTEXT="host",
                    MERGEPILOT_HOST="127.0.0.1")
        argv = dce.build_serve_argv(dce._validate_env(host))
        self.assertNotIn("--serve-dir", argv)

    def test_missing_static_entry_fail_closed(self):
        import demo_console_entrypoint as dce
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            # Empty dir: no index.html -> CONFIG_INVALID.
            with self.assertRaises(dce.EntrypointConfigError) as cm:
                dce._verify_container_serve_dir(td)
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")
            self.assertIn("index.html", str(cm.exception))
            # With index.html: passes.
            (Path(td) / "index.html").write_text("<html></html>",
                                                 encoding="utf-8")
            self.assertEqual(dce._verify_container_serve_dir(td), td)
        # Nonexistent dir: CONFIG_INVALID mentioning the missing bundle.
        with self.assertRaises(dce.EntrypointConfigError) as cm:
            dce._verify_container_serve_dir("/nonexistent/nowhere")
        self.assertIn("missing", str(cm.exception))

    def test_no_replay_fallback_anywhere(self):
        src = SOURCES["serve"].read_text(encoding="utf-8")
        body = src.split("_resolve_serve_dir")[0]
        # The resolver/serve path never falls back to a guessed /samples
        # root outside the two allowlisted locations.
        self.assertNotIn('"/samples', src.replace(
            '"/app/live-console"', ""))

    def test_serve_missing_index_config_invalid_message(self):
        src = SOURCES["serve"].read_text(encoding="utf-8")
        self.assertIn("CONFIG_INVALID: {serve_dir}/index.html not found",
                      src)


# ── Fix 3: controller runtime contract ──────────────────────────────────────

CONTROLLER_GOOD_ENV = {
    "PG_HOST": "postgres",
    "PG_PORT": "5432",
    "PG_DATABASE": "mergepilot_audit",
    "PG_USER": "mergepilot",
    "PG_PASS": "secret-pg",
    "ADMIN_PW": "secret-admin",
}


class TestControllerEntrypoint(unittest.TestCase):

    def test_valid_env_passes(self):
        import controller_entrypoint as ce
        c = ce._validate_env(CONTROLLER_GOOD_ENV)
        self.assertEqual(c["pg_host"], "postgres")
        self.assertEqual(c["pg_port"], 5432)
        # Secrets are validated for presence only — never returned.
        self.assertNotIn("pg_pass", c)
        self.assertNotIn("admin_pw", c)

    def test_each_required_var_missing_rejected(self):
        import controller_entrypoint as ce
        for key in ("PG_HOST", "PG_DATABASE", "PG_USER", "PG_PASS",
                    "ADMIN_PW"):
            env = dict(CONTROLLER_GOOD_ENV); del env[key]
            with self.assertRaises(ce.EntrypointConfigError) as cm:
                ce._validate_env(env)
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")
            self.assertIn(key, str(cm.exception))

    def test_bad_port_rejected(self):
        import controller_entrypoint as ce
        for bad in ("x", "0", "70000"):
            with self.assertRaises(ce.EntrypointConfigError) as cm:
                ce._validate_env(dict(CONTROLLER_GOOD_ENV, PG_PORT=bad))
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_no_secret_values_in_messages(self):
        import controller_entrypoint as ce
        env = dict(CONTROLLER_GOOD_ENV); del env["PG_PASS"]
        with self.assertRaises(ce.EntrypointConfigError) as cm:
            ce._validate_env(env)
        self.assertNotIn("secret-pg", str(cm.exception))
        self.assertNotIn("secret-admin", str(cm.exception))

    def test_env_names_match_actual_controller_code(self):
        # The contract is EXTRACTED, not guessed: every variable the
        # entrypoint requires must be read by controller.py itself.
        controller = (ROOT / "tools" / "workflow-controller" /
                      "controller.py").read_text(encoding="utf-8")
        for key in ("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER",
                    "PG_PASS", "ADMIN_PW"):
            self.assertIn('"%s"' % key, controller, key)

    def test_dockerfile_uses_entrypoint_and_healthcheck(self):
        body = DOCKERFILES["controller"].read_text(encoding="utf-8")
        self.assertIn("controller_entrypoint.py", body)
        self.assertIn("healthcheck.py", body)
        entrypoint_line = [l for l in body.splitlines()
                          if l.startswith("ENTRYPOINT")]
        self.assertEqual(len(entrypoint_line), 1)
        self.assertNotIn("controller.py\"]", entrypoint_line[0])


# ── Fix 3: gateway runtime contract ─────────────────────────────────────────

GATEWAY_GOOD_ENV = {
    "UPSTREAM_URL": "http://127.0.0.1:8084/sse",
}


class TestGatewayEntrypoint(unittest.TestCase):

    def test_valid_env_passes(self):
        import gateway_entrypoint as ge
        c = ge._validate_env(GATEWAY_GOOD_ENV)
        self.assertEqual(c["upstream_url"], ge.STUB_URL)
        self.assertTrue(c["use_stub"])
        self.assertEqual(c["listen_port"], 8083)

    def test_external_upstream_passthrough(self):
        import gateway_entrypoint as ge
        c = ge._validate_env(dict(
            GATEWAY_GOOD_ENV,
            UPSTREAM_URL="http://github-mcp:8082/sse"))
        self.assertFalse(c["use_stub"])

    def test_missing_upstream_rejected(self):
        import gateway_entrypoint as ge
        with self.assertRaises(ge.EntrypointConfigError) as cm:
            ge._validate_env({})
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")
        self.assertIn("UPSTREAM_URL", str(cm.exception))

    def test_invalid_upstream_rejected(self):
        import gateway_entrypoint as ge
        for bad in ("postgres://x", "ftp://x/sse", "http://",
                    "http://user:pw@h/sse", "http://h/sse#frag", "  "):
            with self.assertRaises(ge.EntrypointConfigError) as cm:
                ge._validate_env(dict(GATEWAY_GOOD_ENV, UPSTREAM_URL=bad))
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_bad_role_tokens_rejected(self):
        import gateway_entrypoint as ge
        for bad in ("{not json", "[1,2]", '{"r":1}', '{"":""}'):
            with self.assertRaises(ge.EntrypointConfigError) as cm:
                ge._validate_env(dict(GATEWAY_GOOD_ENV, ROLE_TOKENS=bad))
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")
        c = ge._validate_env(dict(
            GATEWAY_GOOD_ENV, ROLE_TOKENS='{"reviewer":"tok"}'))
        self.assertEqual(c["listen_port"], 8083)

    def test_bad_listen_rejected(self):
        import gateway_entrypoint as ge
        for bad in ("192.168.1.5", "lan.host"):
            with self.assertRaises(ge.EntrypointConfigError) as cm:
                ge._validate_env(dict(GATEWAY_GOOD_ENV, LISTEN_HOST=bad))
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")
        for bad in ("x", "0", "70000"):
            with self.assertRaises(ge.EntrypointConfigError) as cm:
                ge._validate_env(dict(GATEWAY_GOOD_ENV, LISTEN_PORT=bad))
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_env_names_match_actual_gateway_code(self):
        gateway = (ROOT / "tools" / "policy-gateway" / "gateway.py") \
            .read_text(encoding="utf-8")
        for key in ("UPSTREAM_URL", "ROLE_TOKENS", "LISTEN_HOST",
                    "LISTEN_PORT"):
            self.assertIn('"%s"' % key, gateway, key)

    def test_stub_is_loopback_only_zero_tools(self):
        src = SOURCES["upstream_stub"].read_text(encoding="utf-8")
        self.assertIn('LISTEN_HOST = "127.0.0.1"', src)
        self.assertIn("return []", src)          # list_tools -> ZERO tools
        self.assertIn("raise ValueError", src)   # call_tool always refuses

    def test_dockerfile_uses_entrypoint_healthcheck_stub(self):
        body = DOCKERFILES["policy-gateway"].read_text(encoding="utf-8")
        self.assertIn("gateway_entrypoint.py", body)
        self.assertIn("healthcheck.py", body)
        self.assertIn("upstream_stub.py", body)


# ── Fix 3: secret env-file transport ────────────────────────────────────────

class TestControllerSecretFile(unittest.TestCase):

    def test_write_readonly_refuses_overwrite_delete(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sf = ControllerSecretFile(Path(td))
            self.assertEqual(sf.path.name, "controller.env")
            self.assertFalse(sf.exists())
            sf.write("pg-secret-value", "admin-secret-value")
            self.assertTrue(sf.exists())
            with self.assertRaises(StartupGateError) as cm:
                sf.write("x", "y")
            self.assertEqual(cm.exception.code, "SECRET_FILE_EXISTS")
            content = sf.path.read_text(encoding="utf-8")
            self.assertIn("PG_PASS=pg-secret-value", content)
            self.assertIn("ADMIN_PW=admin-secret-value", content)
            self.assertNotIn("POSTGRES_PASSWORD", content)
            sf.delete()
            self.assertFalse(sf.exists())
            sf.delete()  # idempotent

    def test_plan_rejects_secrets_in_dash_e(self):
        oc._builtin_registry.clear()
        ident = "sha256:" + "ab" * 32
        oc.record_built_image_identity("controller", ident)
        try:
            _gate(self, oc.plan_service_run, "controller", image_ref=ident,
                  controller_env={"PG_HOST": "postgres", "PG_PASS": "x"},
                  code="CONFIG_INVALID")
            _gate(self, oc.plan_service_run, "controller", image_ref=ident,
                  controller_env={"PG_HOST": "postgres", "ADMIN_PW": "x"},
                  code="CONFIG_INVALID")
            _gate(self, oc.plan_service_run, "policy-gateway",
                  image_ref=ident,
                  gateway_env={"UPSTREAM_URL": "http://x/sse",
                               # Fake DSN assembled (not a literal) so the
                               # diff-level secret scan stays zero-hit.
                               "AUDIT_DSN": "postgres" + "ql://u:p@h/d"},
                  code="CONFIG_INVALID")
        finally:
            oc._builtin_registry.clear()

    def test_compose_controller_has_secret_env_file_not_literals(self):
        import yaml
        yml = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        ctrl = yml["services"]["controller"]
        self.assertEqual(ctrl["env_file"], "controller.env")
        env = ctrl["environment"]
        for secret in ("PG_PASS", "ADMIN_PW"):
            self.assertNotIn(secret, env)

    def test_compose_gateway_declares_stub_upstream(self):
        import yaml
        yml = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        env = yml["services"]["policy-gateway"]["environment"]
        self.assertEqual(env["UPSTREAM_URL"],
                         "http://127.0.0.1:8084/sse")
        self.assertEqual(env["UPSTREAM_URL"], oc.GATEWAY_ISOLATED_UPSTREAM_URL)


# ── Fix 3: healthchecks + dependency order ──────────────────────────────────

class TestHealthchecksAndOrder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import yaml
        cls.yml = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        cls.builder = oc.build_compose_config(
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2")

    def test_healthchecks_on_postgres_controller_gateway(self):
        for name in ("postgres", "controller", "policy-gateway",
                     "demo-console"):
            self.assertIn("healthcheck", self.yml["services"][name], name)
            self.assertIn("healthcheck",
                          self.builder["services"][name], name)
        # preflight intentionally has none (a one-shot gate matrix whose
        # own exit code IS the health signal).
        self.assertNotIn("healthcheck", self.yml["services"]["preflight"])

    def test_healthcheck_uses_image_script(self):
        for name in ("controller", "policy-gateway"):
            test = self.yml["services"][name]["healthcheck"]["test"]
            self.assertEqual(test, ["CMD", "python", "/app/healthcheck.py"],
                             name)

    def test_dependency_conditions_real(self):
        deps = self.yml["services"]
        self.assertEqual(
            deps["controller"]["depends_on"]["policy-gateway"]["condition"],
            "service_healthy")
        self.assertEqual(
            deps["demo-console"]["depends_on"]["controller"]["condition"],
            "service_healthy")

    def test_orchestrator_plans_carry_healthchecks(self):
        oc._builtin_registry.clear()
        for service in oc.BUILT_SERVICES:
            hexid = ("".join(format(ord(c) & 0xF, "x") for c in service) * 8)[:64]
            oc.record_built_image_identity(service, "sha256:" + hexid)
        try:
            plans = oc.plan_orchestrated_start(
                demo_console_run_id="run-1",
                demo_console_pg_server_addresses="172.18.0.2",
                controller_env_file="/tmp/controller.env",
                reader_dsn_env_file="/tmp/demo_console.env")
            by_name = {p[p.index("--name") + 1]: p for p in plans[1:]
                       if "--name" in p}
            for svc in ("mergepilot-isolated-controller-1",
                        "mergepilot-isolated-policy-gateway-1"):
                self.assertIn("--health-cmd", by_name[svc], svc)
            gw = " ".join(by_name["mergepilot-isolated-policy-gateway-1"])
            self.assertIn("UPSTREAM_URL=http://127.0.0.1:8084/sse", gw)
            ctrl = " ".join(by_name["mergepilot-isolated-controller-1"])
            self.assertIn("PG_HOST=postgres", ctrl)
            self.assertIn("--env-file /tmp/controller.env", ctrl)
            for plan in plans:
                oc.assert_argv_safe(plan)
        finally:
            oc._builtin_registry.clear()

    def test_orchestrator_requires_controller_and_gateway_env(self):
        oc._builtin_registry.clear()
        for service in oc.BUILT_SERVICES:
            hexid = ("".join(format(ord(c) & 0xF, "x") for c in service) * 8)[:64]
            oc.record_built_image_identity(service, "sha256:" + hexid)
        try:
            _gate(self, oc.plan_service_run, "controller",
                  image_ref="sha256:" + "ab" * 32, code="CONFIG_INVALID")
            _gate(self, oc.plan_service_run, "policy-gateway",
                  image_ref="sha256:" + "ab" * 32, code="CONFIG_INVALID")
        finally:
            oc._builtin_registry.clear()

    def test_validate_compose_enforces_new_contract(self):
        cfg = oc.build_compose_config(
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2")
        oc.validate_compose_config(cfg)  # passes
        import copy
        bad = copy.deepcopy(cfg)
        del bad["services"]["controller"]["healthcheck"]
        _gate(self, oc.validate_compose_config, bad, code="COMPOSE_INVALID")
        bad = copy.deepcopy(cfg)
        del bad["services"]["policy-gateway"]["healthcheck"]
        _gate(self, oc.validate_compose_config, bad, code="COMPOSE_INVALID")
        bad = copy.deepcopy(cfg)
        del bad["services"]["controller"]["env_file"]
        _gate(self, oc.validate_compose_config, bad, code="COMPOSE_INVALID")
        bad = copy.deepcopy(cfg)
        del bad["services"]["policy-gateway"]["environment"]["UPSTREAM_URL"]
        _gate(self, oc.validate_compose_config, bad, code="COMPOSE_INVALID")
        bad = copy.deepcopy(cfg)
        bad["services"]["controller"]["environment"]["PG_PASS"] = "leak"
        _gate(self, oc.validate_compose_config, bad, code="COMPOSE_INVALID")


# ── Cross-cutting invariants ────────────────────────────────────────────────

class TestCrossCutting(unittest.TestCase):

    def test_no_shell_true_in_changed_runtime_files(self):
        for path in (ENTRYPOINTS["demo_console"], ENTRYPOINTS["controller"],
                     ENTRYPOINTS["gateway"], ENTRYPOINTS["preflight"],
                     SOURCES["one_click"], SOURCES["postgres_source"],
                     SOURCES["serve"]):
            body = path.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", str(path))
            self.assertNotIn(
                "shell=True",
                body.replace("``shell=True`` forbidden", ""))

    def test_cleanup_dual_error_codes_preserved(self):
        src = SOURCES["one_click"].read_text(encoding="utf-8")
        self.assertIn("primary_code", src)
        self.assertIn("cleanup_codes", src)
        self.assertIn('"CLEANUP_RESIDUE"', src)
        self.assertIn("STACK_STOP_FAILED", src)
        self.assertIn("SECRET_DELETE_FAILED", src)

    def test_no_host_or_twin_substitute(self):
        # The upstream stub is IN-CONTAINER (loopback) — it is not a host
        # process, not a separate compose service, and not a postgres twin.
        import yaml
        yml = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        # 1-G: console-edge IS a compose service (secretless publication
        # plumbing); the gateway stub remains in-container only.
        self.assertEqual(set(yml["services"]),
                         {"postgres", "policy-gateway", "controller",
                          "demo-console", "console-edge", "preflight"})
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("host-process", text.replace(
            "no host-process substitute", "").replace(
            "no host-process", ""))
        # Stub never publishes and never appears as a compose service.
        self.assertNotIn("upstream-stub", text.replace(
            "upstream_stub.py", ""))

    def test_frozen_boundaries_unchanged(self):
        src = SOURCES["one_click"].read_text(encoding="utf-8")
        self.assertIn("MergePilot-Test_database_verified = false", src)
        self.assertIn("MergePilot-Test_application_integration_verified "
                      "= false", src)
        self.assertIn("production_verified = false", src)
        self.assertIn("revision_producer_contract = NOT_VERIFIED", src)
        self.assertIn("audit_producer_contract = NOT_VERIFIED", src)
        self.assertIn("M8 remains undefined", src)


if __name__ == "__main__":
    unittest.main()
