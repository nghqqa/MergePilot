"""Phase 1-D retry v3 — REVIEW-GAP fixes, Mock/static tests.

Gap 1: Docker-CLI controller secret env-file contract (required, fail-closed
       before ANY plan, exactly one --env-file, no path echo).
Gap 2: Controller readiness (sentinel AFTER startup assertions; stale
       clearing; healthcheck = sentinel + DB TCP).
Gap 3: Demo-console readiness (loopback HTTP probe) + preflight waits for
       service_healthy; three-layer consistency.
Gap 4: ControllerSecretFile injection hardening (validate-before-write,
       zero residue, no secret echo).

No WSL/Docker/PostgreSQL started; no real connection; no subprocess.
"""

from __future__ import annotations

import ast
import io
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(ROOT), str(ROOT / "tools"),
           str(ROOT / "tools" / "demo_console"),
           str(ROOT / "tools" / "workflow-controller")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import one_click_startup as oc  # noqa: E402
from one_click_startup import (  # noqa: E402
    ControllerSecretFile,
    StartupGateError,
    plan_service_run,
)

import yaml  # noqa: E402

COMPOSE_PATH = ROOT / "docker-compose.yml"
GAP_SOURCES = [
    ROOT / "tools" / "demo_console" / "one_click_startup.py",
    ROOT / "tools" / "demo_console" / "postgres_source.py",
    ROOT / "tools" / "demo_console" / "serve.py",
    ROOT / "tools" / "demo_console" / "console_healthcheck.py",
    ROOT / "tools" / "demo_console_entrypoint.py",
    ROOT / "tools" / "preflight_entrypoint.py",
    ROOT / "tools" / "controller_entrypoint.py",
    ROOT / "tools" / "gateway_entrypoint.py",
    ROOT / "tools" / "policy-gateway" / "upstream_stub.py",
    ROOT / "tools" / "policy-gateway" / "healthcheck.py",
    ROOT / "tools" / "workflow-controller" / "healthcheck.py",
    ROOT / "tools" / "workflow-controller" / "readiness.py",
    ROOT / "tests" / "isolated_live" / "ephemeral_executor.py",
]


def _gate(testcase, fn, *args, code, **kwargs):
    with testcase.assertRaises(StartupGateError) as cm:
        fn(*args, **kwargs)
    testcase.assertEqual(cm.exception.code, code, msg=str(cm.exception))
    return cm.exception


def _record_identities():
    oc._builtin_registry.clear()
    for service in oc.BUILT_SERVICES:
        hexid = ("".join(format(ord(c) & 0xF, "x") for c in service) * 8)[:64]
        oc.record_built_image_identity(service, "sha256:" + hexid)


# ── Gap 1: controller secret env-file contract ──────────────────────────────

class TestControllerEnvFileContract(unittest.TestCase):

    def setUp(self):
        _record_identities()

    def tearDown(self):
        oc._builtin_registry.clear()

    def test_orchestrator_rejects_missing_env_file_variants(self):
        for bad in (None, "", "   ", "\t", 42, [], object()):
            _gate(self, oc.plan_orchestrated_start,
                  controller_env_file=bad, code="CONFIG_INVALID")

    def test_failure_precedes_any_plan_generation(self):
        # plan_network_create must NEVER be reached when the env-file is
        # invalid — patch it to fail loudly if called.
        def _must_not_run():
            raise AssertionError("plan generated before validation")
        with mock.patch.object(oc, "plan_network_create", _must_not_run), \
                mock.patch.object(oc, "_demo_console_environment",
                                  _must_not_run):
            _gate(self, oc.plan_orchestrated_start,
                  controller_env_file=None, code="CONFIG_INVALID")
            _gate(self, oc.plan_orchestrated_start,
                  controller_env_file="  ", code="CONFIG_INVALID")

    def test_service_run_rejects_controller_without_env_file(self):
        ident = oc.get_built_image_identity("controller")
        _gate(self, plan_service_run, "controller", image_ref=ident,
              controller_env=oc._controller_environment(),
              env_file=None, code="CONFIG_INVALID")
        _gate(self, plan_service_run, "controller", image_ref=ident,
              controller_env=oc._controller_environment(),
              env_file="  ", code="CONFIG_INVALID")
        _gate(self, plan_service_run, "controller", image_ref=ident,
              controller_env=oc._controller_environment(),
              env_file=123, code="CONFIG_INVALID")

    def test_controller_plan_carries_env_file_exactly_once(self):
        plan = plan_service_run(
            "controller",
            image_ref=oc.get_built_image_identity("controller"),
            controller_env=oc._controller_environment(),
            env_file="controller.env")
        self.assertEqual(plan.count("--env-file"), 1)
        self.assertEqual(plan[plan.index("--env-file") + 1],
                         "controller.env")

    def test_orchestrated_controller_plan_env_file_once(self):
        plans = oc.plan_orchestrated_start(
            controller_env_file="controller.env",
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2")
        by_name = {p[p.index("--name") + 1]: p for p in plans[1:]}
        ctrl = by_name["mergepilot-isolated-controller-1"]
        self.assertEqual(ctrl.count("--env-file"), 1)

    def test_error_messages_do_not_echo_the_path(self):
        exc = _gate(self, oc.plan_orchestrated_start,
                    controller_env_file="/very/secret/place/x.env",
                    code="CONFIG_INVALID")
        self.assertNotIn("/very/secret/place/x.env", str(exc))
        self.assertNotIn("x.env", str(exc))


# ── Gap 2: controller readiness ─────────────────────────────────────────────

class TestControllerReadiness(unittest.TestCase):

    def setUp(self):
        import readiness
        self.readiness = readiness
        self.td = tempfile.mkdtemp()
        self.path = os.path.join(self.td, "controller.ready")

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        os.rmdir(self.td)

    def test_not_ready_before_startup_assertions(self):
        # No sentinel yet (assertions not complete) -> not ready.
        self.assertFalse(self.readiness.is_ready(self.path))

    def test_ready_after_mark(self):
        self.readiness.mark_ready(self.path)
        self.assertTrue(self.readiness.is_ready(self.path))

    def test_mark_ready_is_exclusive_create(self):
        self.readiness.mark_ready(self.path)
        with self.assertRaises(OSError):
            self.readiness.mark_ready(self.path)   # O_EXCL: no refresh

    def test_boot_clears_stale_sentinel(self):
        self.readiness.mark_ready(self.path)
        self.assertTrue(self.readiness.is_ready(self.path))
        self.assertTrue(self.readiness.clear_stale_sentinel(self.path))
        self.assertFalse(self.readiness.is_ready(self.path))
        # Idempotent when absent.
        self.assertFalse(self.readiness.clear_stale_sentinel(self.path))

    def test_invalid_sentinels_are_not_ready(self):
        # Empty file.
        open(self.path, "wb").close()
        self.assertFalse(self.readiness.is_ready(self.path))
        os.unlink(self.path)
        # Multi-line / NUL / CR content.
        for content in (b"line1\nline2\n", b"a\x00b\n", b"a\rb\n", b"no-newline"):
            with open(self.path, "wb") as fh:
                fh.write(content)
            self.assertFalse(self.readiness.is_ready(self.path), content)
            os.unlink(self.path)
        # Directory at the sentinel path is not a valid readiness file.
        os.mkdir(self.path)
        self.assertFalse(self.readiness.is_ready(self.path))
        os.rmdir(self.path)

    def test_sentinel_is_symlink_rejected(self):
        real = os.path.join(self.td, "real.ready")
        self.readiness.mark_ready(real)
        try:
            os.symlink(real, self.path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        try:
            self.assertFalse(self.readiness.is_ready(self.path))
        finally:
            os.unlink(self.path)
            os.unlink(real)

    def test_readiness_path_contract(self):
        self.assertEqual(self.readiness.readiness_path(
            {self.readiness.ENV_NAME: ""}), "")
        self.assertEqual(self.readiness.readiness_path(
            {self.readiness.ENV_NAME: "   "}), "")
        good = {self.readiness.ENV_NAME: "/tmp/x.ready"}
        self.assertEqual(self.readiness.readiness_path(good), "/tmp/x.ready")
        for bad in ("relative/path", "/a/../b", "a/b"):
            with self.assertRaises(ValueError):
                self.readiness.readiness_path(
                    {self.readiness.ENV_NAME: bad})

    def test_sentinel_content_is_non_secret(self):
        self.readiness.mark_ready(self.path)
        with open(self.path, "rb") as fh:
            data = fh.read().decode("ascii")
        self.assertNotIn("postgres", data.lower())
        self.assertNotIn("password", data.lower())
        self.assertNotIn("://", data)

    def test_controller_source_wires_clear_assert_mark(self):
        src = (ROOT / "tools" / "workflow-controller" /
               "controller.py").read_text(encoding="utf-8")
        main_block = src.split('if __name__ == "__main__":')[-1]
        self.assertIn("clear_stale_sentinel", main_block)
        # mark_ready must come AFTER startup_assert_l2 in the main flow.
        self.assertLess(main_block.index("startup_assert_l2()"),
                        main_block.index("mark_ready"))
        # run_forever comes after mark_ready (readiness precedes the loop).
        self.assertLess(main_block.index("mark_ready"),
                        main_block.index("run_forever()"))


class TestControllerHealthcheckScript(unittest.TestCase):

    def _run_main(self, sentinel, host, port):
        import healthcheck as hc  # tools/workflow-controller
        env = {"CONTROLLER_READY_SENTINEL": sentinel,
               "PG_HOST": host, "PG_PORT": str(port)}
        with mock.patch.dict(os.environ, env, clear=False):
            out = io.StringIO()
            with mock.patch("sys.stderr", out):
                return hc.main(), out.getvalue()

    def test_healthy_with_sentinel_and_live_tcp(self):
        with tempfile.TemporaryDirectory() as td:
            sentinel = os.path.join(td, "controller.ready")
            import readiness
            readiness.mark_ready(sentinel)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            try:
                rc, _ = self._run_main(sentinel, "127.0.0.1", port)
                self.assertEqual(rc, 0)
            finally:
                listener.close()

    def test_unhealthy_when_db_tcp_unreachable(self):
        with tempfile.TemporaryDirectory() as td:
            sentinel = os.path.join(td, "controller.ready")
            import readiness
            readiness.mark_ready(sentinel)
            # Port 1 on loopback: refused fast; sentinel alone is NOT enough.
            rc, err = self._run_main(sentinel, "127.0.0.1", 1)
            self.assertEqual(rc, 1)
            self.assertNotIn("password", err.lower())

    def test_unhealthy_when_sentinel_missing(self):
        # DB path fine, but assertions never completed -> unhealthy.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            rc, err = self._run_main("/nonexistent/sentinel",
                                     "127.0.0.1", port)
            self.assertEqual(rc, 1)
            self.assertIn("sentinel", err)
        finally:
            listener.close()

    def test_unhealthy_when_sentinel_unconfigured(self):
        rc, _ = self._run_main("", "127.0.0.1", 5432)
        self.assertEqual(rc, 1)


# ── Gap 3: demo-console readiness + preflight dependency ────────────────────

class TestConsoleHealthcheck(unittest.TestCase):

    def _resp(self, status=200, body=b"{}"):
        r = mock.MagicMock()
        r.status = status
        r.read.return_value = body
        r.__enter__.return_value = r
        r.__exit__.return_value = False
        return r

    def test_healthy_payload(self):
        import console_healthcheck as ch
        body = (b'{"source_read_only": true, '
                b'"source_kind": "POSTGRES_ISOLATED", '
                b'"bundle_sha256": "abc123"}')
        with mock.patch.object(ch.urllib.request, "urlopen",
                               return_value=self._resp(200, body)):
            self.assertEqual(ch.check_status(), (True, "ok"))

    def test_http_error_rejected(self):
        import console_healthcheck as ch
        import urllib.error
        with mock.patch.object(
                ch.urllib.request, "urlopen",
                side_effect=urllib.error.HTTPError(
                    "u", 500, "boom", None, None)):
            self.assertFalse(ch.check_status()[0])

    def test_timeout_rejected(self):
        import console_healthcheck as ch
        with mock.patch.object(ch.urllib.request, "urlopen",
                               side_effect=TimeoutError("t")):
            ok, reason = ch.check_status()
            self.assertFalse(ok)
            self.assertEqual(reason, "TimeoutError")

    def test_bad_json_rejected(self):
        import console_healthcheck as ch
        with mock.patch.object(ch.urllib.request, "urlopen",
                               return_value=self._resp(200, b"<html>")):
            self.assertEqual(ch.check_status(), (False, "bad_json"))

    def test_wrong_source_kind_rejected(self):
        import console_healthcheck as ch
        for kind in ("REPLAY", "FILE_FIXTURE", "PREGENERATED_BUNDLE"):
            body = ('{"source_read_only": true, "source_kind": "%s",'
                    ' "bundle_sha256": "x"}' % kind).encode()
            with mock.patch.object(ch.urllib.request, "urlopen",
                                   return_value=self._resp(200, body)):
                self.assertEqual(ch.check_status(),
                                 (False, "wrong_source_kind"), kind)

    def test_not_read_only_rejected(self):
        import console_healthcheck as ch
        body = (b'{"source_read_only": false, '
                b'"source_kind": "POSTGRES_ISOLATED", '
                b'"bundle_sha256": "x"}')
        with mock.patch.object(ch.urllib.request, "urlopen",
                               return_value=self._resp(200, body)):
            self.assertEqual(ch.check_status(), (False, "not_read_only"))

    def test_no_snapshot_rejected(self):
        import console_healthcheck as ch
        body = (b'{"source_read_only": true, '
                b'"source_kind": "POSTGRES_ISOLATED", '
                b'"bundle_sha256": null}')
        with mock.patch.object(ch.urllib.request, "urlopen",
                               return_value=self._resp(200, body)):
            self.assertEqual(ch.check_status(), (False, "no_snapshot"))

    def test_non_loopback_url_rejected_before_socket(self):
        import console_healthcheck as ch
        for url in ("http://172.18.0.2:8600/api/live/status",
                    "http://demo-console:8600/api/live/status",
                    "https://127.0.0.1:8600/api/live/status",
                    "http://user:pw@127.0.0.1:8600/api/live/status"):
            with self.assertRaises(ValueError, msg=url), \
                    mock.patch.object(ch.urllib.request, "urlopen") as up:
                ch.check_status(url=url)
            up.assert_not_called()

    def test_explicit_timeout_present(self):
        import console_healthcheck as ch
        self.assertGreater(ch.TIMEOUT_SECONDS, 0)

    def test_preflight_http_gate_still_fail_closed(self):
        src = (ROOT / "tools" / "preflight_entrypoint.py").read_text(
            encoding="utf-8")
        self.assertIn("HTTP_ENDPOINT_FAILED", src)


class TestDemoReadinessThreeLayerConsistency(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.yml = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        cls.builder = oc.build_compose_config(
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2")

    def test_compose_demo_console_healthcheck(self):
        hc = self.yml["services"]["demo-console"]["healthcheck"]
        self.assertEqual(hc["test"], ["CMD", "python",
                                      "/app/console_healthcheck.py"])

    def test_builder_matches_compose(self):
        self.assertEqual(
            self.builder["services"]["demo-console"]["healthcheck"],
            self.yml["services"]["demo-console"]["healthcheck"])

    def test_orchestrator_demo_console_plan_has_healthcheck(self):
        _record_identities()
        try:
            plans = oc.plan_orchestrated_start(
                controller_env_file="controller.env",
                demo_console_run_id="run-1",
                demo_console_pg_server_addresses="172.18.0.2")
            demo = next(p for p in plans[1:]
                        if "demo-console-1" in p[p.index("--name") + 1])
            self.assertIn("--health-cmd", demo)
            self.assertIn("console_healthcheck.py", " ".join(demo))
        finally:
            oc._builtin_registry.clear()

    def test_preflight_waits_for_healthy_in_all_layers(self):
        self.assertEqual(
            self.yml["services"]["preflight"]["depends_on"]
            ["demo-console"]["condition"], "service_healthy")
        self.assertEqual(
            self.builder["services"]["preflight"]["depends_on"]
            ["demo-console"]["condition"], "service_healthy")

    def test_dockerfile_ships_probe(self):
        body = (ROOT / "Dockerfile.demo-console").read_text(encoding="utf-8")
        self.assertIn("console_healthcheck.py", body)

    def test_validator_enforces_demo_healthcheck_and_edges(self):
        import copy
        cfg = oc.build_compose_config(
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2")
        oc.validate_compose_config(cfg)
        bad = copy.deepcopy(cfg)
        del bad["services"]["demo-console"]["healthcheck"]
        _gate(self, oc.validate_compose_config, bad, code="COMPOSE_INVALID")
        bad = copy.deepcopy(cfg)
        bad["services"]["preflight"]["depends_on"]["demo-console"][
            "condition"] = "service_started"
        _gate(self, oc.validate_compose_config, bad, code="COMPOSE_INVALID")


# ── Gap 4: ControllerSecretFile injection hardening ─────────────────────────

class TestControllerSecretFileHardening(unittest.TestCase):

    def test_injection_values_rejected_with_zero_residue(self):
        bad_values = ("with\nnewline", "with\rreturn", "with\0nul",
                      "", "   ", None, 42, [], {})
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "sub" / "controller.env"
            sf = ControllerSecretFile(target.parent)
            for bad in bad_values:
                _gate(self, sf.write, bad, "ok-pw", code="CONFIG_INVALID")
                _gate(self, sf.write, "ok-pw", bad, code="CONFIG_INVALID")
                # ZERO residue: neither the file nor its directory.
                self.assertFalse(target.exists(), bad)
                self.assertFalse(target.parent.exists(), bad)
                self.assertFalse(sf.exists(), bad)

    def test_error_messages_never_carry_the_value(self):
        with tempfile.TemporaryDirectory() as td:
            sf = ControllerSecretFile(Path(td))
            exc = _gate(self, sf.write, "leak\nme", "x",
                        code="CONFIG_INVALID")
            self.assertNotIn("leak", str(exc))
            exc = _gate(self, sf.write, "x", "leak\nme",
                        code="CONFIG_INVALID")
            self.assertNotIn("leak", str(exc))

    def test_normal_lifecycle_still_passes(self):
        with tempfile.TemporaryDirectory() as td:
            sf = ControllerSecretFile(Path(td))
            self.assertEqual(sf.path.name, "controller.env")
            sf.write("pg-secret", "admin-secret")
            self.assertTrue(sf.exists())
            _gate(self, sf.write, "a", "b", code="SECRET_FILE_EXISTS")
            content = sf.path.read_text(encoding="utf-8")
            self.assertEqual(content,
                             "PG_PASS=pg-secret\nADMIN_PW=admin-secret\n")
            sf.delete()
            self.assertFalse(sf.exists())
            sf.delete()  # idempotent


# ── Cross-cutting: AST shell=True + gateway stub plumbing-only ──────────────

class TestAstAndStubBoundaries(unittest.TestCase):

    def test_zero_shell_true_calls_at_ast_level(self):
        for path in GAP_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "shell" and isinstance(
                                kw.value, ast.Constant) and kw.value.value:
                            self.fail(
                                "shell=True call in %s at line %d"
                                % (path.name, node.lineno))

    def test_gateway_stub_is_plumbing_not_integration(self):
        # Loopback-only, zero tools, no outbound client imports, and it is
        # NOT a compose service — it must never be counted as real gateway
        # integration (that boundary stays NOT_VERIFIED).
        src = (ROOT / "tools" / "policy-gateway" /
               "upstream_stub.py").read_text(encoding="utf-8")
        self.assertIn('LISTEN_HOST = "127.0.0.1"', src)
        self.assertNotIn("sse_client", src)      # no MCP CLIENT usage
        self.assertNotIn("httpx", src)
        self.assertNotIn("requests", src)
        self.assertNotIn("urllib.request", src)  # no outbound HTTP
        yml = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(yml["services"]),
                         {"postgres", "policy-gateway", "controller",
                          "demo-console", "preflight"})
        # Boundary language preserved verbatim in the truth-source module.
        ocsrc = (ROOT / "tools" / "demo_console" /
                 "one_click_startup.py").read_text(encoding="utf-8")
        self.assertIn("application_integration_verified = false", ocsrc)


if __name__ == "__main__":
    unittest.main()
