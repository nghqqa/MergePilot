"""Phase 1-D retry v2 — demo-console container entrypoint Mock/static tests.

Covers BOTH retry-v2 fixes:
  Fix 1 — MERGEPILOT_BIND_CONTEXT (host vs container) validation;
  Fix 2 — the five PostgreSQL expected identity params.

No WSL/Docker/PostgreSQL started; no real connection.
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

import demo_console_entrypoint as dce  # noqa: E402
from demo_console_entrypoint import (  # noqa: E402
    EntrypointConfigError,
    _validate_env,
    build_serve_argv,
)

import yaml  # noqa: E402
import one_click_startup as oc  # noqa: E402


def _cfg(testcase, env, detail_substr=""):
    with testcase.assertRaises(EntrypointConfigError) as cm:
        _validate_env(env)
    testcase.assertEqual(cm.exception.code, "CONFIG_INVALID")
    if detail_substr:
        testcase.assertIn(detail_substr, str(cm.exception))


GOOD_ENV = {
    "MERGEPILOT_MODE": "isolated_live",
    "MERGEPILOT_SOURCE_KIND": "postgres",
    "MERGEPILOT_RUN_ID": "caller-provided-run-001",
    "MERGEPILOT_EXPECTED_ROLE": "mergepilot_reader",
    # Fix 1: explicit bind context; the container listens on 0.0.0.0.
    "MERGEPILOT_BIND_CONTEXT": "container",
    "MERGEPILOT_HOST": "0.0.0.0",
    "MERGEPILOT_PORT": "8600",
    # Fix 2: the five PostgreSQL expected identity params.
    "MERGEPILOT_PG_EXPECTED_DATABASE": "mergepilot_audit",
    "MERGEPILOT_PG_ENVIRONMENT_ID": "mergepilot-test-ephemeral",
    "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES": "172.18.0.2",
    "MERGEPILOT_PG_EXPECTED_SERVER_PORT": "5432",
    "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME":
        "mergepilot_isolated_live_reader",
}


# ── Validation: valid config ──────────────────────────────────────────────────

class TestValidConfig(unittest.TestCase):

    def test_valid_env_passes(self):
        c = _validate_env(GOOD_ENV)
        self.assertEqual(c["mode"], "isolated_live")
        self.assertEqual(c["source_kind"], "postgres")
        self.assertEqual(c["run_id"], "caller-provided-run-001")
        self.assertEqual(c["expected_role"], "mergepilot_reader")
        self.assertEqual(c["bind_context"], "container")
        self.assertEqual(c["host"], "0.0.0.0")  # container listen
        self.assertEqual(c["port"], 8600)
        self.assertEqual(c["pg_expected_database"], "mergepilot_audit")
        self.assertEqual(c["pg_environment_id"], "mergepilot-test-ephemeral")
        self.assertEqual(c["pg_expected_server_addresses"], "172.18.0.2")
        self.assertEqual(c["pg_expected_server_port"], 5432)
        self.assertEqual(c["pg_expected_application_name"],
                         "mergepilot_isolated_live_reader")

    def test_container_listen_0000_allowed(self):
        env = dict(GOOD_ENV, MERGEPILOT_HOST="0.0.0.0")
        c = _validate_env(env)
        self.assertEqual(c["host"], "0.0.0.0")

    def test_container_mode_loopback_listen_allowed(self):
        # 127.0.0.1 remains valid in container mode (stricter than required).
        for h in ("127.0.0.1", "localhost"):
            c = _validate_env(dict(GOOD_ENV, MERGEPILOT_HOST=h))
            self.assertEqual(c["host"], h)

    def test_host_mode_loopback_allowed(self):
        env = dict(GOOD_ENV, MERGEPILOT_BIND_CONTEXT="host",
                   MERGEPILOT_HOST="127.0.0.1")
        c = _validate_env(env)
        self.assertEqual(c["host"], "127.0.0.1")

    def test_case_insensitive_mode_kind_and_context(self):
        env = dict(GOOD_ENV,
                   MERGEPILOT_MODE="ISOLATED_LIVE",
                   MERGEPILOT_SOURCE_KIND="Postgres",
                   MERGEPILOT_BIND_CONTEXT="Container")
        c = _validate_env(env)
        self.assertEqual(c["mode"], "isolated_live")
        self.assertEqual(c["source_kind"], "postgres")
        self.assertEqual(c["bind_context"], "container")

    def test_custom_port(self):
        env = dict(GOOD_ENV, MERGEPILOT_PORT="9000")
        c = _validate_env(env)
        self.assertEqual(c["port"], 9000)

    def test_multiple_server_addresses_allowed(self):
        env = dict(GOOD_ENV,
                   MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES="172.18.0.2, 172.18.0.3")
        c = _validate_env(env)
        # v3 Fix 1: canonical form (bare IPv4, comma-joined without spaces)
        self.assertEqual(c["pg_expected_server_addresses"],
                         "172.18.0.2,172.18.0.3")


# ── Fix 1: bind context rejections ───────────────────────────────────────────

class TestBindContextRejections(unittest.TestCase):

    def test_missing_context_rejected(self):
        env = dict(GOOD_ENV); del env["MERGEPILOT_BIND_CONTEXT"]
        _cfg(self, env, "BIND_CONTEXT")

    def test_empty_context_rejected(self):
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_BIND_CONTEXT="  "), "BIND_CONTEXT")

    def test_invalid_context_rejected(self):
        for bad in ("docker", "k8s", "auto", "1"):
            _cfg(self, dict(GOOD_ENV, MERGEPILOT_BIND_CONTEXT=bad),
                 "'host' or 'container'")

    def test_context_not_inferred_from_host(self):
        # A 0.0.0.0 host WITHOUT an explicit container context must FAIL —
        # the context is never inferred from the host value.
        env = dict(GOOD_ENV); del env["MERGEPILOT_BIND_CONTEXT"]
        _cfg(self, env, "BIND_CONTEXT is not set")

    def test_host_mode_rejects_0000(self):
        # host context + 0.0.0.0 is contradictory -> CONFIG_INVALID.
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_BIND_CONTEXT="host"),
             "host mode")

    def test_container_mode_rejects_lan(self):
        for bad in ("192.168.1.5", "10.0.0.1", "172.16.0.1", "::",
                    "fd00::1", "::1"):
            _cfg(self, dict(GOOD_ENV, MERGEPILOT_HOST=bad),
                 "container mode")

    def test_host_mode_rejects_lan_and_ipv6(self):
        for bad in ("192.168.1.5", "10.0.0.1", "0.0.0.0", "::", "::1"):
            _cfg(self, dict(GOOD_ENV, MERGEPILOT_BIND_CONTEXT="host",
                            MERGEPILOT_HOST=bad), "host mode")


# ── Fix 2: PG expected identity rejections ───────────────────────────────────

class TestPgExpectedRejections(unittest.TestCase):

    def test_missing_database_rejected(self):
        env = dict(GOOD_ENV); del env["MERGEPILOT_PG_EXPECTED_DATABASE"]
        _cfg(self, env, "EXPECTED_DATABASE")

    def test_wrong_database_rejected(self):
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PG_EXPECTED_DATABASE="postgres"),
             "mergepilot_audit")
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PG_EXPECTED_DATABASE="prod"),
             "mergepilot_audit")

    def test_missing_environment_id_rejected(self):
        env = dict(GOOD_ENV); del env["MERGEPILOT_PG_ENVIRONMENT_ID"]
        _cfg(self, env, "ENVIRONMENT_ID")

    def test_missing_server_addresses_rejected(self):
        env = dict(GOOD_ENV)
        del env["MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES"]
        _cfg(self, env, "SERVER_ADDRESSES")

    def test_non_ip_server_addresses_rejected(self):
        # The network alias is NOT an address — hardcoding it is forbidden.
        for bad in ("postgres", "localhost", "abc", "172.18.x.2"):
            _cfg(self, dict(GOOD_ENV,
                            MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES=bad),
                 "IPv4")

    def test_missing_server_port_rejected(self):
        env = dict(GOOD_ENV); del env["MERGEPILOT_PG_EXPECTED_SERVER_PORT"]
        _cfg(self, env, "SERVER_PORT")

    def test_bad_server_port_rejected(self):
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PG_EXPECTED_SERVER_PORT="x"),
             "integer")
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PG_EXPECTED_SERVER_PORT="0"),
             "range")
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PG_EXPECTED_SERVER_PORT="70000"),
             "range")

    def test_missing_application_name_rejected(self):
        env = dict(GOOD_ENV)
        del env["MERGEPILOT_PG_EXPECTED_APPLICATION_NAME"]
        _cfg(self, env, "APPLICATION_NAME")

    def test_wrong_application_name_rejected(self):
        _cfg(self, dict(GOOD_ENV,
                        MERGEPILOT_PG_EXPECTED_APPLICATION_NAME="other_app"),
             "mergepilot_isolated_live_reader")


# ── Earlier rejections stay intact ───────────────────────────────────────────

class TestRejections(unittest.TestCase):

    def test_replay_mode_rejected(self):
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_MODE="replay"), "REJECTED")

    def test_missing_mode_rejected(self):
        env = dict(GOOD_ENV); del env["MERGEPILOT_MODE"]
        _cfg(self, env, "not set")

    def test_wrong_mode_rejected(self):
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_MODE="hybrid"), "isolated_live")

    def test_missing_run_id_rejected(self):
        env = dict(GOOD_ENV); del env["MERGEPILOT_RUN_ID"]
        _cfg(self, env, "RUN_ID")

    def test_empty_run_id_rejected(self):
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_RUN_ID="  "), "RUN_ID")

    def test_run_id_bad_charset_rejected(self):
        for bad in ("bad;id", "bad id", "../etc", "bad\x00id"):
            _cfg(self, dict(GOOD_ENV, MERGEPILOT_RUN_ID=bad), "must match")

    def test_missing_source_kind_rejected(self):
        env = dict(GOOD_ENV); del env["MERGEPILOT_SOURCE_KIND"]
        _cfg(self, env, "SOURCE_KIND")

    def test_wrong_source_kind_rejected(self):
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_SOURCE_KIND="file"), "postgres")
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_SOURCE_KIND="sqlite"), "postgres")

    def test_missing_role_rejected(self):
        env = dict(GOOD_ENV); del env["MERGEPILOT_EXPECTED_ROLE"]
        _cfg(self, env, "EXPECTED_ROLE")

    def test_wrong_role_rejected(self):
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_EXPECTED_ROLE="postgres"),
            "mergepilot_reader")

    def test_bad_console_port_rejected(self):
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PORT="not-a-number"), "integer")
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PORT="0"), "range")
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PORT="70000"), "range")

    def test_no_silent_fallback(self):
        env = dict(GOOD_ENV); del env["MERGEPILOT_RUN_ID"]
        with self.assertRaises(EntrypointConfigError) as cm:
            _validate_env(env)
        msg = str(cm.exception).lower()
        self.assertNotIn("falling back", msg)
        self.assertNotIn("defaulting to replay", msg)
        self.assertNotIn("continuing with", msg)


# ── argv construction ────────────────────────────────────────────────────────

class TestArgvConstruction(unittest.TestCase):

    def setUp(self):
        self.config = _validate_env(GOOD_ENV)

    def test_argv_is_list_of_str(self):
        argv = build_serve_argv(self.config)
        self.assertIsInstance(argv, list)
        for tok in argv:
            self.assertIsInstance(tok, str)

    def test_argv_contains_required_flags(self):
        argv = build_serve_argv(self.config)
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "isolated_live")
        self.assertIn("--source-kind", argv)
        self.assertEqual(argv[argv.index("--source-kind") + 1], "postgres")
        self.assertIn("--run-id", argv)
        self.assertEqual(argv[argv.index("--run-id") + 1],
                         "caller-provided-run-001")
        self.assertIn("--expected-role", argv)
        self.assertEqual(argv[argv.index("--expected-role") + 1],
                         "mergepilot_reader")
        self.assertIn("--host", argv)
        self.assertEqual(argv[argv.index("--host") + 1], "0.0.0.0")
        self.assertIn("--port", argv)
        self.assertEqual(argv[argv.index("--port") + 1], "8600")

    def test_argv_omits_pg_expected_and_context(self):
        # The five PG expected fields and the bind context travel via their
        # env-var contract, NOT argv (argv stays minimal and secret-safe).
        argv = build_serve_argv(self.config)
        joined = " ".join(argv)
        for key in ("EXPECTED_DATABASE", "ENVIRONMENT_ID",
                    "SERVER_ADDRESSES", "SERVER_PORT", "APPLICATION_NAME",
                    "BIND_CONTEXT"):
            self.assertNotIn(key, joined)
        self.assertNotIn("172.18.0.2", joined)
        self.assertNotIn("mergepilot-test-ephemeral", joined)

    def test_argv_no_shell_metachars(self):
        argv = build_serve_argv(self.config)
        joined = " ".join(argv)
        for ch in (";", "|", "&", "`", "$("):
            self.assertNotIn(ch, joined)

    def test_argv_passes_secret_safety(self):
        argv = build_serve_argv(self.config)
        oc.assert_argv_safe(argv)

    def test_argv_no_dsn_or_password(self):
        argv = build_serve_argv(self.config)
        joined = " ".join(argv)
        self.assertNotIn("password=", joined.lower())
        self.assertNotIn("postgresql://", joined.lower())
        self.assertNotIn("dsn", joined.lower())

    def test_run_id_not_hardcoded_in_entrypoint_source(self):
        src = Path(dce.__file__).read_text(encoding="utf-8")
        self.assertNotIn("run-eph-ok", src)

    def test_run_id_flows_from_env_not_code(self):
        env = dict(GOOD_ENV, MERGEPILOT_RUN_ID="custom-run-xyz")
        c = _validate_env(env)
        argv = build_serve_argv(c)
        self.assertEqual(argv[argv.index("--run-id") + 1], "custom-run-xyz")

    def test_dynamic_run_id_accepted(self):
        for rid in ("my-seed-run-42", "prod_2024_x", "a", "run-with-dashes"):
            c = _validate_env(dict(GOOD_ENV, MERGEPILOT_RUN_ID=rid))
            self.assertEqual(c["run_id"], rid)


# ── Compose/Dockerfile contract consistency ─────────────────────────────────

class TestComposeContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.yml = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        cls.builder = oc.build_compose_config(
            demo_console_run_id="caller-provided-run-001",
            demo_console_pg_server_addresses="172.18.0.2")
        cls.dockerfile = (ROOT / "Dockerfile.demo-console").read_text(
            encoding="utf-8")

    def _compose_env(self):
        return self.yml["services"]["demo-console"]["environment"]

    def test_compose_demo_console_env_injects_isolated_live(self):
        env = self._compose_env()
        self.assertEqual(env["MERGEPILOT_MODE"], "isolated_live")
        self.assertEqual(env["MERGEPILOT_SOURCE_KIND"], "postgres")
        run_id = env["MERGEPILOT_RUN_ID"]
        self.assertIn("${MERGEPILOT_RUN_ID", str(run_id))
        self.assertIn("?MERGEPILOT_RUN_ID is required", str(run_id))
        self.assertEqual(env["MERGEPILOT_EXPECTED_ROLE"], "mergepilot_reader")

    def test_compose_declares_container_bind_context(self):
        self.assertEqual(self._compose_env()["MERGEPILOT_BIND_CONTEXT"],
                         "container")

    def test_compose_pg_expected_static_values(self):
        env = self._compose_env()
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_DATABASE"],
                         "mergepilot_audit")
        self.assertEqual(env["MERGEPILOT_PG_ENVIRONMENT_ID"],
                         "mergepilot-test-ephemeral")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_SERVER_PORT"], "5432")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_APPLICATION_NAME"],
                         "mergepilot_isolated_live_reader")

    def test_compose_server_addresses_required_interpolation(self):
        # NOT hardcoded: compose requires the caller to inject the MEASURED
        # bridge IP via variable interpolation (same pattern as RUN_ID).
        val = str(self._compose_env()
                  ["MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES"])
        self.assertIn("${MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES", val)
        self.assertIn("is required", val)

    def test_compose_no_hardcoded_run_eph_ok(self):
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("run-eph-ok", text)

    def test_compose_demo_console_loopback_publish(self):
        # 1-G network design: the console is UNPUBLISHED (internal-only);
        # the loopback publish lives on the secretless console-edge.
        self.assertIsNone(self.yml["services"]["demo-console"].get("ports"))
        ports = self.yml["services"]["console-edge"]["ports"]
        self.assertEqual(len(ports), 1)
        self.assertEqual(str(ports[0]), "127.0.0.1:8600:8600")

    def test_compose_demo_console_container_listen_0000(self):
        self.assertEqual(self._compose_env()["MERGEPILOT_HOST"], "0.0.0.0")

    def test_builder_requires_run_id(self):
        with self.assertRaises(oc.StartupGateError) as cm:
            oc.build_compose_config(
                demo_console_pg_server_addresses="172.18.0.2")
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")
        self.assertIn("required", str(cm.exception))

    def test_builder_requires_server_addresses(self):
        with self.assertRaises(oc.StartupGateError) as cm:
            oc.build_compose_config(demo_console_run_id="r1")
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")
        self.assertIn("server_addresses", str(cm.exception).lower())

    def test_builder_rejects_empty_server_addresses(self):
        with self.assertRaises(oc.StartupGateError) as cm:
            oc.build_compose_config(demo_console_run_id="r1",
                                    demo_console_pg_server_addresses="")
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_builder_rejects_alias_as_server_addresses(self):
        # "postgres" is the network alias, not a measured bridge IP.
        for bad in ("postgres", "localhost", "172.18.x.2"):
            with self.assertRaises(oc.StartupGateError) as cm:
                oc.build_compose_config(demo_console_run_id="r1",
                                        demo_console_pg_server_addresses=bad)
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_builder_rejects_bad_charset_run_id(self):
        for bad in ("bad;id", "bad id", "../etc"):
            with self.assertRaises(oc.StartupGateError) as cm:
                oc.build_compose_config(demo_console_run_id=bad,
                                        demo_console_pg_server_addresses="172.18.0.2")
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_builder_accepts_valid_inputs(self):
        cfg = oc.build_compose_config(
            demo_console_run_id="my-dynamic-run-42",
            demo_console_pg_server_addresses="172.18.0.7")
        env = cfg["services"]["demo-console"]["environment"]
        self.assertEqual(env["MERGEPILOT_RUN_ID"], "my-dynamic-run-42")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES"],
                         "172.18.0.7")

    def test_builder_env_has_bind_context_and_pg_params(self):
        env = self.builder["services"]["demo-console"]["environment"]
        self.assertEqual(env["MERGEPILOT_BIND_CONTEXT"], "container")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_DATABASE"], "mergepilot_audit")
        self.assertEqual(env["MERGEPILOT_PG_ENVIRONMENT_ID"],
                         "mergepilot-test-ephemeral")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES"],
                         "172.18.0.2")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_SERVER_PORT"], "5432")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_APPLICATION_NAME"],
                         "mergepilot_isolated_live_reader")

    def test_dockerfile_uses_entrypoint_module(self):
        self.assertIn("demo_console_entrypoint.py", self.dockerfile)
        self.assertIn("ENTRYPOINT", self.dockerfile)
        entrypoint_line = [l for l in self.dockerfile.splitlines()
                          if l.startswith("ENTRYPOINT")]
        self.assertEqual(len(entrypoint_line), 1)
        self.assertNotIn("--mode", entrypoint_line[0])
        self.assertNotIn("replay", entrypoint_line[0].lower())

    def test_dockerfile_copies_entrypoint(self):
        self.assertIn(
            "COPY tools/demo_console_entrypoint.py /app/demo_console_entrypoint.py",
            self.dockerfile)

    def test_dockerfile_discloses_pg_expected_contract(self):
        for token in ("MERGEPILOT_PG_EXPECTED_DATABASE",
                      "MERGEPILOT_PG_ENVIRONMENT_ID",
                      "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES",
                      "MERGEPILOT_PG_EXPECTED_SERVER_PORT",
                      "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME",
                      "MERGEPILOT_BIND_CONTEXT"):
            self.assertIn(token, self.dockerfile)
        # No image-level defaults for the validated vars (fail-closed).
        self.assertNotIn("ENV MERGEPILOT_BIND_CONTEXT", self.dockerfile)
        self.assertNotIn("ENV MERGEPILOT_PG_EXPECTED", self.dockerfile)

    def test_compose_env_keys_match_entrypoint_contract(self):
        # Every env key the entrypoint reads must be provided by compose.
        entrypoint_reads = {
            "MERGEPILOT_MODE", "MERGEPILOT_SOURCE_KIND",
            "MERGEPILOT_RUN_ID", "MERGEPILOT_EXPECTED_ROLE",
            "MERGEPILOT_BIND_CONTEXT", "MERGEPILOT_HOST", "MERGEPILOT_PORT",
            "MERGEPILOT_PG_EXPECTED_DATABASE",
            "MERGEPILOT_PG_ENVIRONMENT_ID",
            "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES",
            "MERGEPILOT_PG_EXPECTED_SERVER_PORT",
            "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME",
        }
        compose_env = set(self._compose_env())
        for key in entrypoint_reads:
            self.assertIn(key, compose_env, key)

    def test_entrypoint_validates_compose_env(self):
        # The compose env (with variable-interpolated values resolved to
        # concrete caller-provided ones) must pass entrypoint validation.
        env = dict(self._compose_env())
        env["MERGEPILOT_RUN_ID"] = "caller-provided-run-001"
        env["MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES"] = "172.18.0.2"
        c = _validate_env(env)
        self.assertEqual(c["mode"], "isolated_live")
        self.assertEqual(c["bind_context"], "container")
        self.assertEqual(c["host"], "0.0.0.0")
        self.assertEqual(c["port"], 8600)
        self.assertEqual(c["pg_expected_server_addresses"], "172.18.0.2")

    def test_builder_env_matches_compose_keys(self):
        yml_env = set(self._compose_env())
        builder_env = set(self.builder["services"]["demo-console"]["environment"])
        self.assertEqual(yml_env, builder_env)

    def test_builder_env_matches_compose_static_values(self):
        yml_env = self._compose_env()
        builder_env = self.builder["services"]["demo-console"]["environment"]
        static_keys = [k for k in yml_env
                       if not str(yml_env[k]).startswith("${")]
        for key in static_keys:
            self.assertEqual(yml_env[key], builder_env[key], key)


if __name__ == "__main__":
    unittest.main()
