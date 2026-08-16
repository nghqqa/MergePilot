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
import copy
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
                  controller_env_file=bad,
                  reader_dsn_env_file="demo_console.env",
                  code="CONFIG_INVALID")

    def test_failure_precedes_any_plan_generation(self):
        # plan_network_create must NEVER be reached when the env-file is
        # invalid — patch it to fail loudly if called.
        def _must_not_run():
            raise AssertionError("plan generated before validation")
        with mock.patch.object(oc, "plan_network_create", _must_not_run), \
                mock.patch.object(oc, "_demo_console_environment",
                                  _must_not_run):
            _gate(self, oc.plan_orchestrated_start,
                  controller_env_file=None,
                  reader_dsn_env_file="demo_console.env",
                  code="CONFIG_INVALID")
            _gate(self, oc.plan_orchestrated_start,
                  controller_env_file="  ",
                  reader_dsn_env_file="demo_console.env",
                  code="CONFIG_INVALID")

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
            reader_dsn_env_file="demo_console.env",
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2")
        by_name = {p[p.index("--name") + 1]: p for p in plans[1:]}
        ctrl = by_name["mergepilot-isolated-controller-1"]
        self.assertEqual(ctrl.count("--env-file"), 1)

    def test_error_messages_do_not_echo_the_path(self):
        exc = _gate(self, oc.plan_orchestrated_start,
                    controller_env_file="/very/secret/place/x.env",
                    reader_dsn_env_file="demo_console.env",
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
                reader_dsn_env_file="demo_console.env",
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
        # The requests HTTP library must not be imported; match import
        # forms precisely (starlette.requests is a framework module, not
        # the requests library).
        self.assertNotIn("import requests", src)
        self.assertNotIn("from requests", src)
        self.assertNotIn("urllib.request", src)  # no outbound HTTP
        yml = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(yml["services"]),
                         {"postgres", "policy-gateway", "controller",
                          "demo-console", "preflight"})
        # Boundary language preserved verbatim in the truth-source module.
        ocsrc = (ROOT / "tools" / "demo_console" /
                 "one_click_startup.py").read_text(encoding="utf-8")
        self.assertIn("application_integration_verified = false", ocsrc)


# ── Phase 1-G retry: fail-closed Dockerfile syntax guard ────────────────────
#
# Root cause of the 1-G BLOCKED round: Dockerfile.controller carried a
# LITERAL backslash-n inside an ENV instruction ("ENV A=1 \n    B=2"),
# which the Docker daemon rejects with 'can't find = in "\\n"' BEFORE any
# build step. No static test parsed the deliverable Dockerfiles, so the
# defect merged. The guard below parses logical instructions the way the
# daemon does — whole-line comments stripped, trailing-backslash
# continuations joined — so a literal \n inside an instruction is caught
# while a \n inside a COMMENT is not (a plain full-text grep would fail
# both directions). Pure file parsing: no Docker daemon, no WSL.

DELIVERABLE_DOCKERFILES = [
    ROOT / "Dockerfile.controller",
    ROOT / "Dockerfile.policy-gateway",
    ROOT / "Dockerfile.demo-console",
    ROOT / "Dockerfile.preflight",
]


def _dockerfile_logical_lines(text):
    """Yield (first_lineno, instruction) logical Dockerfile lines.

    Mirrors the daemon's pre-parse rules closely enough for guarding:
      - a physical line whose first non-space char is ``#`` is a comment
        and is dropped BEFORE any content check;
      - a line ending with a backslash (immediately before the newline)
        continues onto the next physical line (joined with one space);
      - blank lines are dropped.
    A file ending mid-continuation still yields its partial line so the
    caller can judge it (the daemon would reject such a tail anyway).
    """
    logical = []
    buf = None
    buf_line = 0
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\r")
        stripped = line.lstrip()
        if buf is None:
            if not stripped or stripped.startswith("#"):
                continue
            buf = stripped
            buf_line = lineno
        else:
            buf += " " + stripped
        if buf.endswith("\\"):
            buf = buf[:-1]
            continue
        logical.append((buf_line, buf.strip()))
        buf = None
    if buf is not None:
        logical.append((buf_line, buf.strip()))
    return logical


def _dockerfile_guard_violations(text):
    """Return human-readable violations for one Dockerfile's text."""
    problems = []
    for lineno, instruction in _dockerfile_logical_lines(text):
        if "\\n" in instruction:
            problems.append(
                "line %d: literal \\n inside instruction %r"
                % (lineno, instruction[:60]))
        parts = instruction.split(None, 1)
        verb = parts[0].upper()
        if verb == "ENV" and len(parts) == 2:
            for token in parts[1].split():
                if "=" not in token:
                    problems.append(
                        "line %d: ENV token without '=': %r"
                        % (lineno, token))
                    continue
                key, _, value = token.partition("=")
                if not key:
                    problems.append(
                        "line %d: ENV token with empty key: %r"
                        % (lineno, token))
                if " " in value:
                    problems.append(
                        "line %d: ENV value contains space (unclosed "
                        "continuation?): %r" % (lineno, token))
    return problems


class TestDockerfileSyntaxGuard(unittest.TestCase):
    """Fail-closed static parse of the four deliverable Dockerfiles."""

    def test_no_literal_backslash_n_in_any_deliverable_dockerfile(self):
        for path in DELIVERABLE_DOCKERFILES:
            text = path.read_text(encoding="utf-8")
            problems = _dockerfile_guard_violations(text)
            self.assertEqual(
                [], problems,
                msg="%s: %s" % (path.name, problems))

    def test_env_tokens_are_key_value_in_all_dockerfiles(self):
        for path in DELIVERABLE_DOCKERFILES:
            text = path.read_text(encoding="utf-8")
            envs = [ins for _ln, ins in _dockerfile_logical_lines(text)
                    if ins.split(None, 1)[0].upper() == "ENV"]
            self.assertTrue(envs, msg="%s has no ENV" % path.name)
            for ins in envs:
                for token in ins.split(None, 1)[1].split():
                    self.assertIn("=", token,
                                  msg="%s: %r" % (path.name, ins))
                    key, _, value = token.partition("=")
                    self.assertTrue(key and " " not in value,
                                    msg="%s: %r" % (path.name, token))

    def test_controller_env_exact_contents(self):
        text = (ROOT / "Dockerfile.controller").read_text(encoding="utf-8")
        envs = [ins for _ln, ins in _dockerfile_logical_lines(text)
                if ins.split(None, 1)[0].upper() == "ENV"]
        self.assertEqual(1, len(envs), msg=envs)
        tokens = envs[0].split()[1:]
        self.assertEqual(
            {"PYTHONUNBUFFERED=1",
             "CONTROLLER_READY_SENTINEL=/tmp/mergepilot-controller.ready"},
            set(tokens))
        # The repair itself: a REAL newline continuation, not a literal \n.
        normalized = text.replace("\r\n", "\n")
        self.assertIn(
            "ENV PYTHONUNBUFFERED=1 \\\n"
            "    CONTROLLER_READY_SENTINEL=/tmp/mergepilot-controller.ready",
            normalized)

    def test_guard_rejects_mutated_literal_newline(self):
        # Mutate the now-legal continuation into the exact 1-G defect:
        # a single physical line carrying a literal backslash-n.
        good = ("WORKDIR /app\n"
                "ENV PYTHONUNBUFFERED=1 \\\n"
                "    CONTROLLER_READY_SENTINEL=/tmp/x.ready\n"
                "ENTRYPOINT [\"python\", \"/app/e.py\"]\n")
        self.assertEqual([], _dockerfile_guard_violations(good))
        bad = good.replace("1 \\\n    ", "1 \\n    ")
        problems = _dockerfile_guard_violations(bad)
        self.assertTrue(problems, msg="guard accepted literal \\n")
        self.assertIn("literal \\n", problems[0])

    def test_guard_rejects_env_token_without_equals(self):
        bad = "ENV BARE_TOKEN PYTHONUNBUFFERED=1\n"
        problems = _dockerfile_guard_violations(bad)
        self.assertTrue(any("without '='" in p for p in problems), problems)

    def test_guard_ignores_comments_proving_not_a_grep(self):
        # A literal \n inside a COMMENT must NOT be flagged; a full-text
        # grep guard would fail this (over-reject) — and the mirrored
        # case below proves comment-stripping cannot hide a real defect.
        commented = ("# ENV PYTHONUNBUFFERED=1 \\n    SENTINEL=x\n"
                     "ENV PYTHONUNBUFFERED=1\n")
        self.assertEqual([], _dockerfile_guard_violations(commented))
        hidden = ("# harmless comment\n"
                  "ENV PYTHONUNBUFFERED=1 \\n    SENTINEL=x\n")
        problems = _dockerfile_guard_violations(hidden)
        self.assertTrue(problems, msg="comment hid a real defect")
        self.assertTrue(any("literal \\n" in p for p in problems),
                        problems)


# ── Phase 1-G retry 2: SSE stub send-callable behavior tests ────────────────
#
# Real-run root cause (1-G retry BLOCKED): handle_sse read
# request.scope["send"]; the ASGI scope carries no "send" key, so every
# GET /sse raised KeyError and returned HTTP 500, and the gateway lifespan
# exited after 30 failed connect attempts. The fix passes the callable
# Starlette actually provides (request._send) after an explicit
# existence/callability check.
#
# Hosts running this suite have no mcp/starlette/uvicorn installed (they
# exist only in the container images), so the loader below injects
# minimal stand-ins into sys.modules and then imports the REAL
# upstream_stub.py: the module's own code — including the real handle_sse
# — executes unmodified against the fakes. The fake Request scope
# deliberately omits "send", mirroring the real Starlette scope, so these
# tests would fail against the old implementation with the very KeyError
# observed in the container. No Docker, no WSL, no network.

class _FakeSseRequest:
    """Minimal stand-in for starlette.requests.Request.

    The scope intentionally has NO "send" key (true in real Starlette);
    ``_send`` is attached only when the caller wants a valid callable.
    """

    def __init__(self, *, with_send=True, send_value=None):
        self.scope = {"type": "http", "method": "GET", "path": "/sse"}

        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        self.receive = _receive
        if with_send:
            self._send = send_value


def _load_upstream_stub(source_text=None):
    """Import the real upstream_stub.py under fake mcp/starlette deps.

    Returns (module, recorded) where ``recorded`` captures every
    interaction: connect_sse arguments, the yielded stream pair,
    server.run arguments, initialization options, registered handlers,
    and route wiring. ``source_text`` (optional) imports that text
    instead of the repo file — used by the negative mutation test; the
    text goes to a temp file, never the worktree.
    """
    import asyncio  # noqa: F401  (kept local: only these tests need it)
    import importlib.util
    import tempfile
    import types

    recorded = {}

    class _FakeServer:
        def __init__(self, name):
            recorded["server_name"] = name

        def list_tools(self):
            def deco(fn):
                recorded["list_tools_handler"] = fn
                return fn
            return deco

        def call_tool(self):
            def deco(fn):
                recorded["call_tool_handler"] = fn
                return fn
            return deco

        async def run(self, read_stream, write_stream, init_options):
            recorded["server_run"] = (read_stream, write_stream,
                                      init_options)

        def create_initialization_options(self):
            recorded["init_options"] = object()
            return recorded["init_options"]

    class _FakeConnectSSE:
        """Mirrors mcp 1.28.1 (measured in the image): the context manager
        YIELDS the (read_stream, write_stream) pair.

        The fake transport deliberately does NOT implement
        get_read_stream()/get_write_stream() — the 1-G retry 3 real-run
        AttributeError must be reproducible against this fake."""

        def __init__(self, transport):
            self._transport = transport

        async def __aenter__(self):
            recorded["connect_sse_args"] = self._transport.connect_args
            recorded["yielded_streams"] = (
                self._transport.read_stream, self._transport.write_stream)
            return (self._transport.read_stream,
                    self._transport.write_stream)

        async def __aexit__(self, *exc):
            recorded["connect_sse_exited"] = True
            return False

    class _FakeSseTransport:
        def __init__(self, messages_path):
            recorded["messages_path"] = messages_path
            self.connect_args = None
            self.read_stream = object()
            self.write_stream = object()

        def connect_sse(self, scope, receive, send):
            self.connect_args = (scope, receive, send)
            return _FakeConnectSSE(self)

        async def handle_post_message(self, request):
            return None

    class _Route:
        def __init__(self, path, endpoint=None, **kw):
            recorded.setdefault("routes", []).append(("Route", path))
            recorded.setdefault("route_endpoints", []).append(endpoint)

    class _Mount:
        def __init__(self, path, app=None, **kw):
            recorded.setdefault("routes", []).append(("Mount", path))

    class _Starlette:
        def __init__(self, routes=None, **kw):
            recorded["app_routes_count"] = len(routes or [])

    stub_path = ROOT / "tools" / "policy-gateway" / "upstream_stub.py"
    injected = {}
    try:
        mcp_mod = types.ModuleType("mcp")
        mcp_server_mod = types.ModuleType("mcp.server")
        mcp_sse_mod = types.ModuleType("mcp.server.sse")
        mcp_server_mod.Server = _FakeServer
        mcp_sse_mod.SseServerTransport = _FakeSseTransport
        mcp_mod.server = mcp_server_mod
        mcp_server_mod.sse = mcp_sse_mod

        starlette_mod = types.ModuleType("starlette")
        st_app_mod = types.ModuleType("starlette.applications")
        st_routing_mod = types.ModuleType("starlette.routing")
        st_requests_mod = types.ModuleType("starlette.requests")

        class _FakeRequest:
            """Mirrors pinned Starlette Request.__init__: stores scope,
            receive and the send callable as ``_send``."""

            def __init__(self, scope, receive=None, send=None):
                self.scope = scope
                self.receive = receive
                self._send = send

        st_app_mod.Starlette = _Starlette
        st_routing_mod.Route = _Route
        st_routing_mod.Mount = _Mount
        st_requests_mod.Request = _FakeRequest
        starlette_mod.requests = st_requests_mod

        uvicorn_mod = types.ModuleType("uvicorn")
        uvicorn_mod.run = lambda *a, **k: None

        for name, mod in (
                ("mcp", mcp_mod), ("mcp.server", mcp_server_mod),
                ("mcp.server.sse", mcp_sse_mod),
                ("starlette", starlette_mod),
                ("starlette.applications", st_app_mod),
                ("starlette.routing", st_routing_mod),
                ("starlette.requests", st_requests_mod),
                ("uvicorn", uvicorn_mod)):
            sys.modules[name] = mod
            injected[name] = mod

        if source_text is None:
            import_path = stub_path
        else:
            import os
            import pathlib
            with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".py", delete=False,
                    encoding="utf-8") as tmp:
                tmp.write(source_text)
            import_path = pathlib.Path(tmp.name)
        try:
            spec = importlib.util.spec_from_file_location(
                "upstream_stub_under_test", import_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module, recorded
        finally:
            if source_text is not None:
                import os
                os.unlink(str(import_path))
    finally:
        for name in injected:
            sys.modules.pop(name, None)


class TestUpstreamStubSendCallable(unittest.TestCase):
    """The 1-G retry 2 fix: handle_sse must pass a REAL send callable."""

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_handle_sse_passes_request_send_to_connect_sse(self):
        module, recorded = _load_upstream_stub()

        async def send_callable(message):
            return None

        request = _FakeSseRequest(with_send=True, send_value=send_callable)
        # Precondition mirroring real Starlette: no "send" in the scope.
        self.assertNotIn("send", request.scope)

        self._run(module.handle_sse(request))   # no KeyError possible now

        args = recorded["connect_sse_args"]
        self.assertIs(request.scope, args[0])
        self.assertIs(request.receive, args[1])
        self.assertIs(send_callable, args[2])
        self.assertTrue(recorded["connect_sse_exited"])

        # server.run must receive EXACTLY the streams the transport's
        # context manager yielded (mcp 1.28.1 contract), plus the
        # initialization options object.
        read_stream, write_stream, init_options = recorded["server_run"]
        yielded_read, yielded_write = recorded["yielded_streams"]
        self.assertIs(yielded_read, read_stream)
        self.assertIs(yielded_write, write_stream)
        self.assertIs(recorded["init_options"], init_options)

    def test_handle_sse_fails_closed_when_send_missing(self):
        module, _recorded = _load_upstream_stub()
        request = _FakeSseRequest(with_send=False)
        self.assertFalse(hasattr(request, "_send"))
        with self.assertRaises(RuntimeError) as cm:
            self._run(module.handle_sse(request))
        message = str(cm.exception).lower()
        self.assertIn("send callable", message)
        # The error is a static string: no request data can leak.
        for forbidden in ("authorization", "token", "header", "bearer"):
            self.assertNotIn(forbidden, message)

    def test_handle_sse_fails_closed_when_send_not_callable(self):
        module, _recorded = _load_upstream_stub()
        request = _FakeSseRequest(with_send=True, send_value="not-callable")
        with self.assertRaises(RuntimeError):
            self._run(module.handle_sse(request))

    def test_stub_source_no_longer_reads_scope_send(self):
        source = (ROOT / "tools" / "policy-gateway" /
                  "upstream_stub.py").read_text(encoding="utf-8")
        self.assertNotIn('request.scope["send"]', source)
        self.assertNotIn("request.scope['send']", source)
        self.assertIn("_send", source)

    def test_stub_source_has_no_stream_accessors(self):
        # The pinned mcp 1.28.1 transport has no get_read_stream /
        # get_write_stream; their absence in source is the retry-4 fix.
        source = (ROOT / "tools" / "policy-gateway" /
                  "upstream_stub.py").read_text(encoding="utf-8")
        self.assertNotIn("get_read_stream(", source)
        self.assertNotIn("get_write_stream(", source)
        self.assertIn("as (read_stream, write_stream)", source)

    def test_mutation_old_stream_accessors_reproduce_attributeerror(self):
        # Negative mutation, fully in-memory (worktree untouched): revert
        # the shipped handler to the retry-3 shape (call the nonexistent
        # get_*_stream accessors) and run it through the SAME harness.
        # The fake transport — mirroring mcp 1.28.1 — has no such methods,
        # so the mutated handler must raise AttributeError. The shipped
        # handler under the identical harness must pass.
        source = (ROOT / "tools" / "policy-gateway" /
                  "upstream_stub.py").read_text(encoding="utf-8")
        start = source.find("async def handle_sse")
        end = source.find("app = Starlette")
        old_body = (
            "async def handle_sse(request):\n"
            "    send = getattr(request, \"_send\", None)\n"
            "    if not callable(send):\n"
            "        raise RuntimeError(\"no send\")\n"
            "    async with sse.connect_sse(request.scope, "
            "request.receive, send):\n"
            "        await server.run(sse.get_read_stream(),\n"
            "                         sse.get_write_stream(),\n"
            "                         server.create_initialization_options())"
            "\n\n\n"
            "class SSEEndpoint:\n"
            "    def __init__(self, scope, receive, send):\n"
            "        self.scope = scope\n"
            "        self.receive = receive\n"
            "        self.send = send\n"
            "    def __await__(self):\n"
            "        return self.dispatch().__await__()\n"
            "    async def dispatch(self):\n"
            "        await handle_sse(Request(self.scope, self.receive, "
            "self.send))\n"
            "\n\n\n"
        )
        mutant = source[:start] + old_body + source[end:]
        self.assertIn("get_read_stream(", mutant)

        async def send_callable(message):
            return None

        # Shipped handler: same harness, clean run (also re-proves the
        # Retry-2 send-callable guarantees alongside the stream unpack).
        good_module, good_recorded = _load_upstream_stub()
        good_request = _FakeSseRequest(with_send=True,
                                       send_value=send_callable)
        self._run(good_module.handle_sse(good_request))
        self.assertIs(send_callable, good_recorded["connect_sse_args"][2])
        self.assertIsNotNone(good_recorded["server_run"])

        # Mutant: identical harness, must fail with AttributeError.
        mutant_module, _ = _load_upstream_stub(source_text=mutant)
        mutant_request = _FakeSseRequest(with_send=True,
                                         send_value=send_callable)
        with self.assertRaises(AttributeError) as cm:
            self._run(mutant_module.handle_sse(mutant_request))
        self.assertIn("get_read_stream", str(cm.exception))

    def test_sse_route_endpoint_is_asgi_class(self):
        # Pinned Starlette: a FUNCTION Route endpoint gets wrapped as
        # func(request)->response (None return after SSE completion -> the
        # retry-5 'NoneType not callable' traceback); a CLASS endpoint is
        # used as the raw ASGI app.
        import inspect
        module, recorded = _load_upstream_stub()
        sse_endpoints = [e for kind, e in zip(
            (r[0] for r in recorded["routes"]),
            recorded["route_endpoints"]) if kind == "Route"]
        self.assertEqual(1, len(sse_endpoints))
        endpoint = sse_endpoints[0]
        self.assertTrue(inspect.isclass(endpoint),
                        msg="endpoint must be a class (raw ASGI)")
        self.assertFalse(inspect.isfunction(endpoint))

    def test_asgi_app_passes_send_through_request(self):
        # Invoke the shipped ASGI endpoint directly, following the pinned
        # HTTPEndpoint calling convention (await Endpoint(scope, receive,
        # send)): the Request constructed in dispatch must carry the ASGI
        # send callable through to connect_sse (identity), with
        # scope/receive intact.
        module, recorded = _load_upstream_stub()
        endpoint_cls = recorded["route_endpoints"][0]

        async def send(message):
            return None

        async def receive():
            return {"type": "http.request", "body": b"",
                    "more_body": False}

        scope = {"type": "http", "method": "GET", "path": "/sse"}

        async def drive():
            await endpoint_cls(scope, receive, send)

        self._run(drive())

        args = recorded["connect_sse_args"]
        self.assertIs(scope, args[0])
        self.assertIs(receive, args[1])
        self.assertIs(send, args[2])   # request._send IS the ASGI send
        self.assertTrue(recorded["connect_sse_exited"])

    def test_zero_tool_contract_via_loaded_module(self):
        module, recorded = _load_upstream_stub()
        self.assertEqual("mergepilot-isolated-upstream-stub",
                         recorded["server_name"])
        self.assertEqual([], self._run(recorded["list_tools_handler"]()))
        with self.assertRaises(ValueError):
            self._run(recorded["call_tool_handler"]("anything", {}))
        # /sse route plus the /messages/ mount are both wired.
        self.assertEqual([("Route", "/sse"), ("Mount", "/messages/")],
                         recorded["routes"])


# ── 1-G stabilization sweep: container delivery-closure guard ───────────────
#
# The retry-5 real run crashed because Dockerfile.demo-console did not COPY
# tools/demo_console/live_refresh.py (serve.py imports it at startup, inside
# a function). No static test compared each image's AST import closure with
# its Dockerfile COPY list, so the gap merged. The guard below performs that
# comparison for ALL FOUR deliverable images: it walks the repo-local import
# closure (top-level, nested, and static importlib.import_module calls) of
# every shipped entrypoint/main/healthcheck module and requires a matching
# COPY line. NOT a grep. Mutations that delete any required COPY (one per
# image) must fail the audit.

# The module search space for each image: the repo directories whose
# copied files land FLAT in /app (mirrors the container's import truth).
# Declared explicitly — NOT derived from the COPY list — so that removing
# a COPY line in a mutation cannot make the import unresolvable (and the
# gap invisible).
_DELIVERY_IMAGES = {
    "controller": {
        "dockerfile": "Dockerfile.controller",
        "dirs": ["tools", "tools/workflow-controller",
                 "tools/demo_console"],
        "entrypoints": [
            "tools/controller_entrypoint.py",
            "tools/workflow-controller/controller.py",
            "tools/workflow-controller/healthcheck.py",
            "tools/workflow-controller/readiness.py",
        ],
    },
    "policy-gateway": {
        "dockerfile": "Dockerfile.policy-gateway",
        "dirs": ["tools", "tools/policy-gateway",
                 "tools/demo_console"],
        "entrypoints": [
            "tools/gateway_entrypoint.py",
            "tools/policy-gateway/gateway.py",
            "tools/policy-gateway/healthcheck.py",
            "tools/policy-gateway/upstream_stub.py",
        ],
    },
    "demo-console": {
        "dockerfile": "Dockerfile.demo-console",
        "dirs": ["tools", "tools/demo_console"],
        "entrypoints": [
            "tools/demo_console_entrypoint.py",
            "tools/demo_console/serve.py",
            "tools/demo_console/console_healthcheck.py",
            "tools/demo_console/preflight.py",
        ],
    },
    "preflight": {
        "dockerfile": "Dockerfile.preflight",
        "dirs": ["tools", "tools/demo_console"],
        "entrypoints": [
            "tools/preflight_entrypoint.py",
            "tools/demo_console/preflight.py",
        ],
    },
}


def _dockerfile_copy_pairs(dockerfile_text):
    """(src, dst) pairs from COPY instructions, continuations joined."""
    pairs = []
    logical = []
    buf = None
    for raw in dockerfile_text.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        if buf is None:
            if not stripped or stripped.startswith("#"):
                continue
            buf = stripped
        else:
            buf += " " + stripped
        if buf.endswith("\\"):
            buf = buf[:-1]
            continue
        logical.append(buf.strip())
        buf = None
    for ins in logical:
        if not ins.upper().startswith("COPY"):
            continue
        args = ins.split()[1:]
        if len(args) < 2:
            continue
        srcs, dst = args[:-1], args[-1]
        for src in srcs:
            pairs.append((src, dst))
    return pairs


def _ast_local_import_names(path):
    """Repo-local top-level module names imported anywhere in the file
    (module level, function level, conditionals, static importlib calls)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module.split(".")[0])
            elif node.level > 0:
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and func.attr == "import_module"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "importlib"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)):
                names.add(str(node.args[0].value).split(".")[0])
    return names


def _delivery_closure_audit(dockerfile_text=None, image=None):
    """Run the closure audit across all four images.

    ``dockerfile_text``/``image`` let mutation tests substitute ONE
    Dockerfile's content. Returns (gaps, per_image) where gaps are
    human-readable missing-file strings and per_image maps the image name
    to (copied_py_files, copy_pairs, required_closure_files)."""
    image = image or "demo-console"
    texts = {}
    for name, spec in _DELIVERY_IMAGES.items():
        path = ROOT / spec["dockerfile"]
        texts[name] = (dockerfile_text
                       if (name == image and dockerfile_text is not None)
                       else path.read_text(encoding="utf-8"))

    gaps = []
    per_image = {}
    for name, spec in _DELIVERY_IMAGES.items():
        copies = _dockerfile_copy_pairs(texts[name])
        copied = set()
        for src, _dst in copies:
            p = ROOT / src
            if p.suffix == ".py":
                copied.add(p.resolve())
        per_image[name] = None   # filled after the walk below

        required = set()
        queue = []
        broken_entry = False
        for e in spec["entrypoints"]:
            p = (ROOT / e).resolve()
            if not p.is_file():
                gaps.append("%s: entrypoint missing on disk: %s" % (name, e))
                broken_entry = True
                continue
            queue.append(p)
            required.add(p)
        if broken_entry:
            continue

        # Flat /app layout: an import may resolve in ANY declared source
        # directory of this image (tools/ root entrypoints import modules
        # from tools/demo_console/, etc.). Declared dirs, not COPY-derived,
        # so a mutated COPY list cannot hide a gap by breaking resolution.
        search_dirs = {(ROOT / d).resolve() for d in spec["dirs"]}
        for e in spec["entrypoints"]:
            search_dirs.add((ROOT / e).parent.resolve())

        while queue:
            path = queue.pop()
            same_dir = path.parent
            for mod in _ast_local_import_names(path):
                cands = [(same_dir / (mod + ".py")).resolve()]
                cands += [d / (mod + ".py") for d in sorted(search_dirs)
                          if d != same_dir]
                cand = next((c for c in cands if c.is_file()), None)
                if cand is None or cand in required:
                    continue
                required.add(cand)
                queue.append(cand)

        per_image[name] = (copied, copies, required)
        missing = required - copied
        # Host-only renderers are deliberately NOT shipped (REPLAY is
        # refused in containers); if one ever enters a shipped closure
        # that is a REAL finding, so no exclusion is applied here.
        for m in sorted(missing, key=str):
            gaps.append("%s: %s needed by import closure but NOT copied"
                        % (name, m.relative_to(ROOT)))
    return gaps, per_image


class TestContainerDeliveryClosure(unittest.TestCase):
    """AST import closure vs Dockerfile COPY list, all four images."""

    def test_all_four_images_have_closed_delivery(self):
        gaps, _per = _delivery_closure_audit()
        self.assertEqual([], gaps, msg=gaps)

    def test_copied_sources_exist_on_disk(self):
        _gaps, per_image = _delivery_closure_audit()
        missing = []
        for name, (_copied, copies, _req) in per_image.items():
            for src, _dst in copies:
                if not (ROOT / src).exists():
                    missing.append("%s: COPY source missing: %s"
                                   % (name, src))
        self.assertEqual([], missing, msg=missing)

    def test_live_refresh_copy_is_present_in_demo_console(self):
        text = (ROOT / "Dockerfile.demo-console").read_text(
            encoding="utf-8")
        self.assertIn(
            "COPY tools/demo_console/live_refresh.py /app/live_refresh.py",
            text)

    def test_mutation_removing_live_refresh_copy_fails_audit(self):
        text = (ROOT / "Dockerfile.demo-console").read_text(
            encoding="utf-8")
        line = ("COPY tools/demo_console/live_refresh.py"
                " /app/live_refresh.py")
        self.assertIn(line, text)
        mutated = text.replace(line + "\n", "")
        gaps, _per = _delivery_closure_audit(
            dockerfile_text=mutated, image="demo-console")
        self.assertTrue(any("live_refresh.py" in g for g in gaps), gaps)

    def test_mutation_removing_one_copy_per_other_image_fails_audit(self):
        _g, per_image = _delivery_closure_audit()
        for image in ("controller", "policy-gateway", "preflight"):
            spec = _DELIVERY_IMAGES[image]
            text = (ROOT / spec["dockerfile"]).read_text(encoding="utf-8")
            copied, _pairs, required = per_image[image]
            # Remove the COPY of one non-entrypoint file that IS in the
            # import closure (self-maintaining as the closure evolves).
            victim = None
            for f in sorted(required & copied, key=str):
                rel = f.relative_to(ROOT).as_posix()
                if rel in spec["entrypoints"]:
                    continue
                victim = rel
                break
            self.assertIsNotNone(victim, msg=image)
            copy_line = next(
                (ln for ln in text.splitlines()
                 if ln.strip().startswith("COPY %s " % victim)), None)
            self.assertIsNotNone(copy_line, msg=(image, victim))
            mutated = text.replace(copy_line + "\n", "")
            gaps, _per = _delivery_closure_audit(
                dockerfile_text=mutated, image=image)
            self.assertTrue(
                any(victim.rsplit("/", 1)[-1] in g for g in gaps),
                msg=(image, victim, gaps))

    def test_mutation_copying_nonexistent_source_fails(self):
        text = (ROOT / "Dockerfile.preflight").read_text(encoding="utf-8")
        mutated = text + (
            "\nCOPY tools/demo_console/no_such_module.py"
            " /app/no_such_module.py\n")
        _gaps, per_image = _delivery_closure_audit(
            dockerfile_text=mutated, image="preflight")
        missing = []
        for name, (_copied, copies, _req) in per_image.items():
            for src, _dst in copies:
                if not (ROOT / src).exists():
                    missing.append("%s: %s" % (name, src))
        self.assertTrue(missing, msg="nonexistent COPY source accepted")# ── 1-G stabilization sweep: reader-DSN secret-file delivery ────────────────
#
# The retry-5 component smoke exposed that NEITHER compose nor the
# Docker-CLI orchestration attached the reader-DSN secret env-file to its
# two consumers: serve.py (demo-console) and preflight_entrypoint.py both
# read MERGEPILOT_PG_DSN and exit without it, and the DSN may never ride
# -e argv. The wiring now requires ReaderDsnSecretFile transport on both
# services in every layer (compose dict, compose YAML, Docker-CLI plan).

class TestReaderDsnDelivery(unittest.TestCase):

    def setUp(self):
        _record_identities()

    def tearDown(self):
        oc._builtin_registry.clear()

    def test_secret_file_lifecycle_and_content(self):
        with tempfile.TemporaryDirectory() as td:
            sf = oc.ReaderDsnSecretFile(Path(td))
            self.assertEqual("demo_console.env", sf.path.name)
            dsn = ("postgresql://mergepilot_reader:pw@postgres:5432/"
                   "mergepilot_audit?application_name=x")
            sf.write(dsn)
            self.assertTrue(sf.exists())
            self.assertEqual("MERGEPILOT_PG_DSN=%s\n" % dsn,
                             sf.path.read_text(encoding="utf-8"))
            _gate(self, sf.write, dsn, code="SECRET_FILE_EXISTS")
            sf.delete()
            self.assertFalse(sf.exists())
            sf.delete()   # idempotent

    def test_secret_file_validates_before_any_residue(self):
        bad = (None, 42, "", "   ", "mysql://x", "postgresql://a b@c/d",
               "postgresql://u:p@h/d\nPG_PASS=leak",
               "postgresql://u\t@h/d")
        with tempfile.TemporaryDirectory() as td:
            sf = oc.ReaderDsnSecretFile(Path(td) / "nested")
            for value in bad:
                _gate(self, sf.write, value, code="CONFIG_INVALID")
                # zero residue: neither the file nor its (not-yet-created)
                # parent directory
                self.assertFalse(sf.exists(), value)
                self.assertFalse(sf.path.parent.exists(), value)

    def test_secret_file_errors_never_carry_the_value(self):
        with tempfile.TemporaryDirectory() as td:
            sf = oc.ReaderDsnSecretFile(Path(td))
            exc = _gate(self, sf.write,
                        "postgresql://u:leakme\n@h/d",
                        code="CONFIG_INVALID")
            self.assertNotIn("leakme", str(exc))

    def test_demo_console_and_preflight_plans_carry_env_file_once(self):
        demo = oc.plan_service_run(
            "demo-console",
            image_ref=oc.get_built_image_identity("demo-console"),
            demo_console_env=oc._demo_console_environment(
                "run-1", "172.18.0.2"),
            reader_dsn_env_file="/secrets/demo_console.env")
        self.assertEqual(1, demo.count("--env-file"))
        self.assertEqual("/secrets/demo_console.env",
                         demo[demo.index("--env-file") + 1])
        pf = oc.plan_service_run(
            "preflight",
            image_ref=oc.get_built_image_identity("preflight"),
            declared_pg_image=oc.PGVECTOR_IMAGE_DIGEST,
            reader_dsn_env_file="/secrets/demo_console.env")
        self.assertEqual(1, pf.count("--env-file"))
        self.assertEqual("/secrets/demo_console.env",
                         pf[pf.index("--env-file") + 1])

    def test_service_run_rejects_missing_reader_dsn_file(self):
        for bad in (None, "", "   ", 42):
            for service in ("demo-console", "preflight"):
                kwargs = {"reader_dsn_env_file": bad}
                if service == "demo-console":
                    kwargs["demo_console_env"] = oc._demo_console_environment(
                        "run-1", "172.18.0.2")
                _gate(self, oc.plan_service_run, service,
                      image_ref=oc.get_built_image_identity(service),
                      code="CONFIG_INVALID", **kwargs)

    def test_orchestrated_start_requires_reader_dsn_file_first(self):
        def _must_not_run():
            raise AssertionError("plan generated before validation")
        with mock.patch.object(oc, "plan_network_create", _must_not_run),                 mock.patch.object(oc, "_demo_console_environment",
                                  _must_not_run):
            for bad in (None, "", "  ", 42):
                _gate(self, oc.plan_orchestrated_start,
                      controller_env_file="controller.env",
                      reader_dsn_env_file=bad, code="CONFIG_INVALID")
        plans = oc.plan_orchestrated_start(
            controller_env_file="controller.env",
            reader_dsn_env_file="demo_console.env",
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2")
        by_name = {p[p.index("--name") + 1]: p for p in plans[1:]}
        for svc in ("demo-console", "preflight"):
            plan = by_name["mergepilot-isolated-%s-1" % svc]
            self.assertEqual(1, plan.count("--env-file"), svc)
            self.assertEqual("demo_console.env",
                             plan[plan.index("--env-file") + 1], svc)

    def test_compose_validator_requires_dsn_env_file(self):
        cfg = oc.build_compose_config(
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2")
        oc.validate_compose_config(cfg)
        for svc in ("demo-console", "preflight"):
            self.assertTrue(cfg["services"][svc].get("env_file"), svc)
        # negative mutation 1: strip the env_file -> COMPOSE_INVALID
        bad1 = copy.deepcopy(cfg)
        del bad1["services"]["demo-console"]["env_file"]
        _gate(self, oc.validate_compose_config, bad1, code="COMPOSE_INVALID")
        # negative mutation 2: DSN as compose literal -> COMPOSE_INVALID
        bad2 = copy.deepcopy(cfg)
        bad2["services"]["preflight"]["environment"][
            "MERGEPILOT_PG_DSN"] = "postgresql://leak"
        _gate(self, oc.validate_compose_config, bad2, code="COMPOSE_INVALID")

    def test_compose_yaml_matches_env_file_wiring(self):
        yml = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        for svc in ("demo-console", "preflight"):
            self.assertEqual("demo_console.env",
                             yml["services"][svc].get("env_file"), svc)
            self.assertNotIn("MERGEPILOT_PG_DSN",
                             yml["services"][svc].get("environment") or {})


if __name__ == "__main__":
    unittest.main()
