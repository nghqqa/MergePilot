"""Phase 1-D retry fix — demo-console container entrypoint Mock/static tests.

No WSL/Docker/PostgreSQL started; no real connection.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

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
}


# ── Validation: valid config ──────────────────────────────────────────────────

class TestValidConfig(unittest.TestCase):

    def test_valid_env_passes(self):
        c = _validate_env(GOOD_ENV)
        self.assertEqual(c["mode"], "isolated_live")
        self.assertEqual(c["source_kind"], "postgres")
        self.assertEqual(c["run_id"], "caller-provided-run-001")
        self.assertEqual(c["expected_role"], "mergepilot_reader")
        self.assertEqual(c["host"], "0.0.0.0")  # container listen default
        self.assertEqual(c["port"], 8600)

    def test_defaults_host_and_port(self):
        c = _validate_env(GOOD_ENV)
        self.assertEqual(c["host"], "0.0.0.0")  # container-internal listen
        self.assertEqual(c["port"], 8600)

    def test_container_listen_0000_allowed(self):
        env = dict(GOOD_ENV, MERGEPILOT_HOST="0.0.0.0")
        c = _validate_env(env)
        self.assertEqual(c["host"], "0.0.0.0")

    def test_loopback_listen_allowed(self):
        env = dict(GOOD_ENV, MERGEPILOT_HOST="127.0.0.1")
        c = _validate_env(env)
        self.assertEqual(c["host"], "127.0.0.1")

    def test_localhost_listen_allowed(self):
        env = dict(GOOD_ENV, MERGEPILOT_HOST="localhost")
        c = _validate_env(env)
        self.assertEqual(c["host"], "localhost")

    def test_case_insensitive_mode_and_kind(self):
        env = dict(GOOD_ENV,
                   MERGEPILOT_MODE="ISOLATED_LIVE",
                   MERGEPILOT_SOURCE_KIND="Postgres")
        c = _validate_env(env)
        self.assertEqual(c["mode"], "isolated_live")
        self.assertEqual(c["source_kind"], "postgres")

    def test_localhost_host_accepted(self):
        env = dict(GOOD_ENV, MERGEPILOT_HOST="localhost")
        c = _validate_env(env)
        self.assertEqual(c["host"], "localhost")

    def test_custom_port(self):
        env = dict(GOOD_ENV, MERGEPILOT_PORT="9000")
        c = _validate_env(env)
        self.assertEqual(c["port"], 9000)


# ── Rejection matrix ─────────────────────────────────────────────────────────

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
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_EXPECTED_ROLE="admin"),
            "mergepilot_reader")

    def test_lan_host_rejected(self):
        # LAN-specific addresses are NOT valid container listen addresses.
        # 0.0.0.0 IS allowed (container-internal listen); only LAN IPs and
        # IPv6 :: are rejected.
        for bad in ("192.168.1.5", "10.0.0.1", "172.16.0.1", "::",
                    "fd00::1"):
            _cfg(self, dict(GOOD_ENV, MERGEPILOT_HOST=bad),
                 "not valid container listen")

    def test_bad_port_rejected(self):
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PORT="not-a-number"), "integer")
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PORT="0"), "range")
        _cfg(self, dict(GOOD_ENV, MERGEPILOT_PORT="70000"), "range")

    def test_no_silent_fallback(self):
        # The error message must never suggest a fallback; it must refuse.
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

    def test_argv_no_shell_metachars(self):
        argv = build_serve_argv(self.config)
        joined = " ".join(argv)
        for ch in (";", "|", "&", "`", "$("):
            self.assertNotIn(ch, joined)

    def test_argv_passes_secret_safety(self):
        argv = build_serve_argv(self.config)
        # Would raise on any DSN/password/SQL literal/token.
        oc.assert_argv_safe(argv)

    def test_argv_no_dsn_or_password(self):
        argv = build_serve_argv(self.config)
        joined = " ".join(argv)
        self.assertNotIn("password=", joined.lower())
        self.assertNotIn("postgresql://", joined.lower())
        self.assertNotIn("dsn", joined.lower())

    def test_run_id_not_hardcoded_in_entrypoint_source(self):
        # The entrypoint module must NOT contain any hardcoded run-eph-ok.
        src = Path(dce.__file__).read_text(encoding="utf-8")
        self.assertNotIn("run-eph-ok", src)

    def test_run_id_flows_from_env_not_code(self):
        env = dict(GOOD_ENV, MERGEPILOT_RUN_ID="custom-run-xyz")
        c = _validate_env(env)
        argv = build_serve_argv(c)
        self.assertEqual(argv[argv.index("--run-id") + 1], "custom-run-xyz")

    def test_dynamic_run_id_accepted(self):
        for rid in ("my-seed-run-42", "prod_2024_x", "a", "run-with-dashes"):
            env = dict(GOOD_ENV, MERGEPILOT_RUN_ID=rid)
            c = _validate_env(env)
            self.assertEqual(c["run_id"], rid)


# ── Compose/Dockerfile contract consistency ─────────────────────────────────

class TestComposeContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.yml = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        cls.builder = oc.build_compose_config(
            demo_console_run_id="caller-provided-run-001")
        cls.dockerfile = (ROOT / "Dockerfile.demo-console").read_text(
            encoding="utf-8")

    def test_compose_demo_console_env_injects_isolated_live(self):
        env = self.yml["services"]["demo-console"]["environment"]
        self.assertEqual(env["MERGEPILOT_MODE"], "isolated_live")
        self.assertEqual(env["MERGEPILOT_SOURCE_KIND"], "postgres")
        # RUN_ID uses compose variable interpolation (caller must inject);
        # the literal value must NOT be hardcoded in the yml.
        run_id = env["MERGEPILOT_RUN_ID"]
        self.assertIn("${MERGEPILOT_RUN_ID", str(run_id))
        self.assertIn("?MERGEPILOT_RUN_ID is required", str(run_id))
        self.assertEqual(env["MERGEPILOT_EXPECTED_ROLE"], "mergepilot_reader")

    def test_compose_no_hardcoded_run_eph_ok(self):
        # The literal run-eph-ok must NOT appear in the compose yml.
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("run-eph-ok", text)

    def test_compose_demo_console_loopback_publish(self):
        # HOST-side publish must remain 127.0.0.1-only.
        ports = self.yml["services"]["demo-console"]["ports"]
        self.assertEqual(len(ports), 1)
        self.assertTrue(str(ports[0]).startswith("127.0.0.1:"))

    def test_compose_demo_console_container_listen_0000(self):
        # Container-internal listen address is 0.0.0.0 (Docker bridge).
        env = self.yml["services"]["demo-console"]["environment"]
        self.assertEqual(env["MERGEPILOT_HOST"], "0.0.0.0")

    def test_compose_no_lan_or_wildcard_publish(self):
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for bad in ("0.0.0.0:8600:8600", ":::8600:8600", "192.168.",
                    "10.0.0."):
            # 0.0.0.0 in the env block is the CONTAINER LISTEN, not a publish
            if "MERGEPILOT_HOST" in bad or bad == "0.0.0.0:8600:8600":
                # Check it doesn't appear in a ports: line
                ports_section = False
                for line in text.splitlines():
                    if "ports:" in line:
                        ports_section = True
                    if ports_section and bad in line:
                        self.fail("LAN publish found: %r in ports section" % bad)
                    if ports_section and line.strip() and not line.startswith((" ", "-", "#")):
                        ports_section = False

    def test_builder_requires_run_id(self):
        with self.assertRaises(oc.StartupGateError) as cm:
            oc.build_compose_config()
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")
        self.assertIn("required", str(cm.exception))

    def test_builder_rejects_empty_run_id(self):
        with self.assertRaises(oc.StartupGateError) as cm:
            oc.build_compose_config(demo_console_run_id="")
        self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_builder_rejects_bad_charset_run_id(self):
        for bad in ("bad;id", "bad id", "../etc"):
            with self.assertRaises(oc.StartupGateError) as cm:
                oc.build_compose_config(demo_console_run_id=bad)
            self.assertEqual(cm.exception.code, "CONFIG_INVALID")

    def test_builder_accepts_valid_run_id(self):
        cfg = oc.build_compose_config(demo_console_run_id="my-dynamic-run-42")
        env = cfg["services"]["demo-console"]["environment"]
        self.assertEqual(env["MERGEPILOT_RUN_ID"], "my-dynamic-run-42")

    def test_builder_container_listen_0000(self):
        cfg = oc.build_compose_config(demo_console_run_id="r1")
        env = cfg["services"]["demo-console"]["environment"]
        self.assertEqual(env["MERGEPILOT_HOST"], "0.0.0.0")

    def test_dockerfile_uses_entrypoint_module(self):
        self.assertIn("demo_console_entrypoint.py", self.dockerfile)
        self.assertIn("ENTRYPOINT", self.dockerfile)
        # The entrypoint must NOT directly invoke serve.py with a baked mode.
        entrypoint_line = [l for l in self.dockerfile.splitlines()
                          if l.startswith("ENTRYPOINT")]
        self.assertEqual(len(entrypoint_line), 1)
        self.assertNotIn("--mode", entrypoint_line[0])
        self.assertNotIn("replay", entrypoint_line[0].lower())

    def test_dockerfile_copies_entrypoint(self):
        self.assertIn(
            "COPY tools/demo_console_entrypoint.py /app/demo_console_entrypoint.py",
            self.dockerfile)

    def test_compose_env_keys_match_entrypoint_contract(self):
        # Every env key the entrypoint reads must be provided by compose.
        entrypoint_reads = {"MERGEPILOT_MODE", "MERGEPILOT_SOURCE_KIND",
                           "MERGEPILOT_RUN_ID", "MERGEPILOT_EXPECTED_ROLE"}
        compose_env = set(self.yml["services"]["demo-console"]["environment"])
        for key in entrypoint_reads:
            self.assertIn(key, compose_env, key)

    def test_entrypoint_validates_compose_env(self):
        # The compose env (with the variable-interpolated run_id resolved to
        # a concrete value) must pass entrypoint validation.
        env = dict(self.yml["services"]["demo-console"]["environment"])
        # Resolve the compose variable to a caller-provided value.
        env["MERGEPILOT_RUN_ID"] = "caller-provided-run-001"
        c = _validate_env(env)
        self.assertEqual(c["mode"], "isolated_live")
        self.assertEqual(c["host"], "0.0.0.0")  # container listen
        self.assertEqual(c["port"], 8600)

    def test_builder_env_matches_compose_keys(self):
        # The builder and yml must provide the SAME env keys (values may
        # differ for RUN_ID since the yml uses variable interpolation).
        yml_env = set(self.yml["services"]["demo-console"]["environment"])
        builder_env = set(self.builder["services"]["demo-console"]["environment"])
        for key in ("MERGEPILOT_MODE", "MERGEPILOT_SOURCE_KIND",
                     "MERGEPILOT_RUN_ID", "MERGEPILOT_EXPECTED_ROLE",
                     "MERGEPILOT_HOST", "MERGEPILOT_PORT"):
            self.assertIn(key, yml_env, "yml missing %s" % key)
            self.assertIn(key, builder_env, "builder missing %s" % key)

    def test_builder_env_matches_compose_static_values(self):
        # Static (non-run_id) env values must match between yml and builder.
        yml_env = self.yml["services"]["demo-console"]["environment"]
        builder_env = self.builder["services"]["demo-console"]["environment"]
        for key in ("MERGEPILOT_MODE", "MERGEPILOT_SOURCE_KIND",
                     "MERGEPILOT_EXPECTED_ROLE", "MERGEPILOT_HOST",
                     "MERGEPILOT_PORT"):
            self.assertEqual(yml_env[key], builder_env[key], key)


if __name__ == "__main__":
    unittest.main()
