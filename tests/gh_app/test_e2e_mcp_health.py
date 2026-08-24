"""B8 real-E2E-DAG round: production MCP health transport regression.

Four deterministic defects fixed together (they stack across the
same stages):

A. The bridge/gateway MCP health checks ran urllib on the WINDOWS
   host against docker-network IPs inside WSL — structurally
   unroutable (no L3 route from Windows into the WSL docker
   bridges; a system TUN proxy intercepts even no-proxy direct
   connects — verified RemoteDisconnected against a live listener,
   while in-distro fetch returned 200). The probe now runs INSIDE
   the distro via host_executor, speaking the REAL MCP SSE dialect
   (endpoint event → POST initialize/notifications/tools-list with
   JSON-RPC responses arriving on the GET stream).
B. GATEWAY_SSE_URL pointed at the BRIDGE IP :8083 with no role path
   and no bearer — the gateway is 172.31.0.18:8083/{role}/sse with
   Bearer auth (manager role per HICLAW_ROLE_FREEZE).
C. ROLE_TOKENS was a non-JSON placeholder; the gateway json.loads()
   it at startup and crashed deterministically. It is now extracted
   from the canonical mcporter store (the agents' real tokens) and
   fails closed at stage-2 schema validation.
D. Agent readiness + receipt live revalidation bound to the E2E
   distro's docker daemon, but the HiClaw stack lives in Ubuntu-
   22.04 (the rewiring harness's daemon) — now wired through a
   dedicated HiClaw executor.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import e2e_lifecycle as el                     # noqa: E402
import e2e_gateway_health as gwh               # noqa: E402
import e2e_runtime_specs as rs                 # noqa: E402
import e2e_executors as ex                     # noqa: E402


def _cp(rc=0, stdout=b""):
    return subprocess.CompletedProcess([], rc, stdout, b"")


# ── A: the probe script itself, exercised against a REAL in-process
# MCP SSE server (both SDK-style /messages/?session_id=… and
# mcp-proxy-style /message?sessionId=… endpoint dialects) ──────────

# the REAL read-only contract (single authority: e2e_gateway_health)
TOOLS = sorted(gwh.FROZEN_READ_ONLY_TOOLS)


class _SseMcpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, endpoint_path):
        super().__init__(("127.0.0.1", 0), _SseMcpHandler)
        self.endpoint_path = endpoint_path
        self.lock = threading.Lock()
        self.stream = None
        self.auth_header = None
        self.requests = []
        self.done = threading.Event()

    def write_event(self, payload):
        with self.lock:
            if self.stream is None:
                return
            self.stream.wfile.write(
                ("data: %s\n\n" % json.dumps(payload)).encode())
            self.stream.wfile.flush()


class _SseMcpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        srv = self.server
        srv.auth_header = self.headers.get("Authorization", "")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with srv.lock:
            srv.stream = self
        self.wfile.write(
            ("event: endpoint\ndata: %s\n\n"
             % srv.endpoint_path).encode())
        self.wfile.flush()
        srv.done.wait(20)

    def do_POST(self):
        srv = self.server
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        with srv.lock:
            srv.requests.append(body)
        if body.get("id") == 1:
            srv.write_event({"jsonrpc": "2.0", "id": 1,
                             "result": {"protocolVersion":
                                        "2024-11-05"}})
        elif body.get("method") == "tools/list":
            srv.write_event({
                "jsonrpc": "2.0", "id": body.get("id"),
                "result": {"tools": [{"name": t} for t in TOOLS]}})
            srv.done.set()
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestMcpSseProbeScript(unittest.TestCase):

    def test_script_compiles(self):
        compile(el._MCP_SSE_PROBE_SCRIPT, "mcp-probe", "exec")

    def _run_probe(self, server, bearer):
        url = "http://127.0.0.1:%d/reviewer/sse" % server.server_address[1]
        return subprocess.run(
            [sys.executable, "-c", el._MCP_SSE_PROBE_SCRIPT, url],
            input=(bearer + "\n").encode(), capture_output=True,
            timeout=30)

    def test_real_sse_dance_sdk_dialect(self):
        srv = _SseMcpServer("/messages/?session_id=42")
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            cp = self._run_probe(srv, "tok-reviewer-abc")
            self.assertEqual(cp.returncode, 0, cp.stderr[-400:])
            self.assertEqual(json.loads(cp.stdout)["tools"], TOOLS)
            self.assertEqual(srv.auth_header, "Bearer tok-reviewer-abc")
            methods = [r.get("method") for r in srv.requests]
            self.assertIn("initialize", methods)
            self.assertIn("notifications/initialized", methods)
            self.assertIn("tools/list", methods)
        finally:
            srv.shutdown()

    def test_real_sse_dance_mcp_proxy_dialect(self):
        srv = _SseMcpServer("/message?sessionId=7f")
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            cp = self._run_probe(srv, "")
            self.assertEqual(cp.returncode, 0, cp.stderr[-400:])
            self.assertEqual(json.loads(cp.stdout)["tools"], TOOLS)
            self.assertIsNone(srv.auth_header or None)  # no bearer sent
        finally:
            srv.shutdown()

    def test_unreachable_reports_error_json(self):
        url = "http://127.0.0.1:9/sse"  # closed port
        cp = subprocess.run(
            [sys.executable, "-c", el._MCP_SSE_PROBE_SCRIPT, url],
            input=b"\n", capture_output=True, timeout=30)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("error", json.loads(cp.stdout))


class TestExecMcpSseProbe(unittest.TestCase):

    def test_healthy_parse_and_transport_shape(self):
        seen = {}

        def host_exec(argv, check=True, timeout=60, input_bytes=None,
                      **_):
            seen["argv"] = list(argv)
            seen["input"] = input_bytes
            return _cp(0, json.dumps(
                {"tools": TOOLS}).encode())

        result = el.exec_mcp_sse_probe(host_exec, "http://x/sse",
                                       bearer="tok-m")
        self.assertTrue(result["healthy"])
        self.assertEqual(result["tools"], TOOLS)
        # the script + url ride argv; the bearer rides STDIN only
        self.assertEqual(seen["argv"][0], "python3")
        self.assertEqual(seen["argv"][1], "-c")
        self.assertEqual(seen["argv"][2], el._MCP_SSE_PROBE_SCRIPT)
        self.assertEqual(seen["argv"][3], "http://x/sse")
        self.assertNotIn("tok-m", json.dumps(seen["argv"]))
        self.assertEqual(seen["input"], b"tok-m\n")

    def test_rc_failure_collapses_to_unreachable(self):
        result = el.exec_mcp_sse_probe(
            lambda *a, **k: _cp(1, b""), "http://x/sse")
        self.assertFalse(result["healthy"])
        self.assertEqual(result["error"], "GATEWAY_UPSTREAM_UNREACHABLE")

    def test_executor_exception_is_unreachable(self):
        def boom(*a, **k):
            raise OSError("no route")

        result = el.exec_mcp_sse_probe(boom, "http://x/sse")
        self.assertEqual(result["error"], "GATEWAY_UPSTREAM_UNREACHABLE")

    def test_bad_json_is_parse_error(self):
        result = el.exec_mcp_sse_probe(
            lambda *a, **k: _cp(0, b"not-json"), "http://x/sse")
        self.assertEqual(result["error"], "GATEWAY_TOOLS_PARSE_ERROR")

    def test_probe_error_code_passthrough(self):
        result = el.exec_mcp_sse_probe(
            lambda *a, **k: _cp(1, b'{"error": "PROBE_NO_ENDPOINT"}'),
            "http://x/sse")
        self.assertEqual(result["error"], "PROBE_NO_ENDPOINT")


class TestToolContract(unittest.TestCase):

    def test_subset_satisfies_bridge(self):
        out = el._health_with_tool_contract(
            {"healthy": True, "tools": TOOLS + ["create_issue"]},
            exact=False)
        self.assertTrue(out["healthy"])

    def test_missing_tool_fails_closed(self):
        out = el._health_with_tool_contract(
            {"healthy": True, "tools": ["get_branch"]}, exact=False)
        self.assertEqual(out["error"], "GATEWAY_MISSING_TOOLS")

    def test_zero_tools(self):
        out = el._health_with_tool_contract(
            {"healthy": True, "tools": []}, exact=False)
        self.assertEqual(out["error"], "GATEWAY_ZERO_TOOLS")

    def test_extra_tool_rejected_for_gateway_exact(self):
        out = el._health_with_tool_contract(
            {"healthy": True, "tools": TOOLS + ["create_issue"]},
            exact=True)
        self.assertEqual(out["error"], "GATEWAY_EXTRA_TOOLS")


# ── B: gateway URL authority ───────────────────────────────────────────────

class TestGatewayUrlAuthority(unittest.TestCase):

    def test_gateway_url_is_reviewer_role_on_gw_egress_ip(self):
        self.assertEqual(el.GATEWAY_SSE_URL,
                         ex.hiclaw_role_gateway_url("reviewer"))
        self.assertTrue(el.GATEWAY_SSE_URL.startswith(
            "http://172.31.0.18:8083/reviewer/sse"))

    def test_frozen_set_matches_fixture_policy_read_class(self):
        """The frozen contract must equal the fixture policy's
        tool_classes.read (drift here broke the real DAG at the
        bridge gate: placeholder names that the deployed server
        never exposed)."""
        text = Path(ROOT / "tools" / "policy-gateway"
                    / "policy-e2e-fixture.yaml").read_text(
                        encoding="utf-8")
        # minimal parse: read list under tool_classes
        section = text.split("tool_classes:")[1].split("roles:")[0]
        read_block = section.split("read:")[1].split("comment:")[0]
        names = set()
        for line in read_block.splitlines():
            line = line.strip()
            if line.startswith("- "):
                names.add(line[2:].strip().strip('"'))
        self.assertEqual(set(gwh.FROZEN_READ_ONLY_TOOLS), names)

    def test_no_host_urllib_left_in_mcp_checks(self):
        """The production health path must not fall back to host-side
        urllib (structurally unreachable in this topology)."""
        src = Path(el.__file__).read_text(encoding="utf-8")
        self.assertNotIn("gwh.verify_gateway_mcp_health_required(",
                         src.split("def _health_with_tool_contract")[0]
                         .split("def production_service_health")[-1])


# ── wiring: production_service_health + _wait_mcp ─────────────────────────

class TestProductionServiceHealthWiring(unittest.TestCase):

    def test_bridge_healthy_via_in_container_probe(self):
        seen = {}

        def docker_exec(argv, check=True, timeout=60,
                        input_bytes=None, **_):
            seen["argv"] = list(argv)
            return _cp(0, json.dumps({"tools": TOOLS}).encode())

        el.production_service_health(docker_exec, "mcp-bridge")
        # probes exec INSIDE the service container (loopback; the
        # distro-host position is dropped by the §8 INPUT deny)
        self.assertEqual(seen["argv"][:3], ["exec", "-i",
                                            "mergepilot-isolated-"
                                            "mcp-bridge-1"])
        self.assertEqual(seen["argv"][3], "python3")
        self.assertIn("-c", seen["argv"])
        self.assertEqual(seen["argv"][-1],
                         "http://127.0.0.1:8082/sse")

    def test_gateway_bearer_reaches_probe_stdin(self):
        seen = {}

        def docker_exec(argv, check=True, timeout=60,
                        input_bytes=None, **_):
            seen["argv"] = list(argv)
            seen["input"] = input_bytes
            return _cp(0, json.dumps({"tools": TOOLS}).encode())

        el.production_service_health(
            docker_exec, "policy-gateway",
            gateway_bearer="tok-reviewer-xyz")
        self.assertEqual(seen["argv"][:3], ["exec", "-i",
                                            "mergepilot-isolated-"
                                            "policy-gateway-1"])
        self.assertEqual(seen["argv"][-1],
                         "http://127.0.0.1:8083/reviewer/sse")
        self.assertEqual(seen["input"], b"tok-reviewer-xyz\n")

    def test_missing_executor_is_unhealthy(self):
        check = el._bridge_mcp_check(None)
        self.assertEqual(check(el._LOOPBACK_BRIDGE_SSE_URL)["error"],
                         "GATEWAY_UPSTREAM_UNREACHABLE")

    def test_wait_mcp_failure_code(self):
        def check(url):
            return {"healthy": False, "tools": [],
                    "error": "GATEWAY_MISSING_TOOLS"}

        with self.assertRaises(el.E2ELifecycleError) as ctx:
            el._wait_mcp(check, el._LOOPBACK_BRIDGE_SSE_URL,
                         "mcp-bridge", timeout=0.2)
        self.assertEqual(ctx.exception.code, "E2E_MCP_BRIDGE_MCP_UNHEALTHY")
        self.assertEqual(ctx.exception.detail, "GATEWAY_MISSING_TOOLS")


# ── C: ROLE_TOKENS extraction + runtime config ─────────────────────────────

def _mcporter_body(token):
    return json.dumps({
        "mcpServers": {
            "mcp-github": {
                "url": "http://172.31.0.18:8083/manager/sse",
                "transport": "http",
                "headers": {"Authorization": "Bearer " + token}},
            "github": {
                "url": "http://172.31.0.18:8083/manager/sse",
                "transport": "sse"}}}).encode()


class TestRoleTokenExtraction(unittest.TestCase):

    def test_extracts_manager_from_canonical_workers_from_env(self):
        import mergepilot as mp

        calls = []

        def hiclaw_exec(argv, check=True, timeout=60, **_):
            calls.append(list(argv))
            if argv[1] == "hiclaw-controller":        # mc cat
                key = argv[-1].split("hiclaw-storage/", 1)[1]
                parts = key.split("/")
                role = parts[1] if parts[0] == "agents" else parts[0]
                if role == "manager":
                    # manager carries the bearer inline
                    return _cp(0, _mcporter_body("tok-manager"))
                # workers' rewired mcporter has NO auth header —
                # their runtime injects the env key at call time
                return _cp(0, json.dumps({
                    "mcpServers": {"github": {
                        "url": "http://172.31.0.18:8083/%s/sse" % role,
                        "transport": "sse"}}}).encode())
            if argv[2] == "printenv":
                # container env fallback (worker path)
                return _cp(0, ("tok-" + argv[1]).encode())
            raise AssertionError("unexpected argv %s" % argv)

        tokens = mp._read_hiclaw_role_tokens(hiclaw_exec)
        self.assertEqual(
            sorted(tokens), ["fixer", "manager", "reviewer", "verifier"])
        self.assertEqual(tokens["manager"], "tok-manager")
        self.assertEqual(tokens["reviewer"], "tok-hiclaw-worker-reviewer")
        self.assertEqual(tokens["fixer"], "tok-hiclaw-worker-fixer")
        self.assertEqual(tokens["verifier"], "tok-hiclaw-worker-verifier")
        # every canonical read goes through the store by role key
        mc_cats = [a for a in calls if a[:4] == ["exec",
                                                "hiclaw-controller",
                                                "mc", "cat"]]
        self.assertEqual(len(mc_cats), 4)
        printenvs = [a for a in calls if len(a) > 2 and a[2] == "printenv"]
        self.assertEqual(len(printenvs), 3)   # workers only

    def test_unreadable_canonical_and_env_fails_closed(self):
        import mergepilot as mp

        with self.assertRaises(mp.Failure) as ctx:
            mp._read_hiclaw_role_tokens(lambda *a, **k: _cp(1, b""))
        self.assertEqual(ctx.exception.code, "E2E_ROLE_TOKEN_EXTRACT_FAILED")

    def test_tokenless_config_and_env_fails_closed(self):
        import mergepilot as mp

        body = json.dumps({"mcpServers": {
            "github": {"url": "http://x/sse", "transport": "sse"}
        }}).encode()

        def hiclaw_exec(argv, check=True, timeout=60, **_):
            # canonical answers tokenless; printenv answers empty
            if len(argv) > 2 and argv[2] == "printenv":
                return _cp(0, b"")
            return _cp(0, body)

        with self.assertRaises(mp.Failure) as ctx:
            mp._read_hiclaw_role_tokens(hiclaw_exec)
        self.assertEqual(ctx.exception.code, "E2E_ROLE_TOKEN_EXTRACT_FAILED")


class TestRoleTokensRuntimeConfig(unittest.TestCase):

    def test_build_produces_valid_json_with_all_roles(self):
        import mergepilot as mp

        config = {"fixture_repo": "example/fixture",
                  "matrix_room_id": "!r:example",
                  "windows_proxy_ip": "172.23.48.1",
                  "windows_proxy_port": "17890",
                  "app_id": "123456",
                  "installation_id": "789",
                  "repository_id": "101112"}
        tokens = {"manager": "t-m", "reviewer": "t-r",
                  "fixer": "t-f", "verifier": "t-v"}
        cfg = mp._build_e2e_runtime_configs(
            config, None, "postgresql://u:s@postgres/db",
            "postgresql://u:a@postgres/db", "postgresql://u:p@postgres/db",
            "synthetic-pat-value", role_tokens=tokens,
            controller_db_env={"PG_HOST": "postgres", "PG_PORT": "5432",
                               "PG_DATABASE": "mergepilot_audit",
                               "PG_USER": "mergepilot"},
            controller_pg_pass="synthetic-pg-pass",
            controller_admin_pw="synthetic-admin-pw")
        parsed = json.loads(cfg["policy-gateway"]["ROLE_TOKENS"])
        self.assertEqual(
            sorted(parsed),
            ["coordinator", "fixer", "manager", "reviewer", "verifier"])
        self.assertEqual(parsed["manager"], "t-m")
        # the controller's COORDINATOR_TOKEN matches the gateway's
        # coordinator entry (the pair must authenticate against each other)
        self.assertEqual(parsed["coordinator"],
                         cfg["controller"]["COORDINATOR_TOKEN"])
        # passes the fail-closed stage-2 schema gate
        rs.validate_gateway_e2e_env(cfg["policy-gateway"])

    def test_controller_db_contract_required_and_carried(self):
        # run27 regression: without the six database-contract keys the
        # controller entrypoint exits CONFIG_INVALID milliseconds after
        # docker start — the container never reaches State.Running.
        import e2e_foundation as e2f
        import mergepilot as mp

        config = {"fixture_repo": "example/fixture",
                  "matrix_room_id": "!r:" + e2f.E2E_MATRIX_SERVER_NAME,
                  "windows_proxy_ip": "172.23.48.1",
                  "windows_proxy_port": "17890",
                  "app_id": "123456",
                  "installation_id": "789",
                  "repository_id": "101112"}
        db_env = {"PG_HOST": "postgres", "PG_PORT": "5432",
                  "PG_DATABASE": "mergepilot_audit",
                  "PG_USER": "mergepilot"}
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            mp._build_e2e_runtime_configs(
                config, None, "postgresql://u:s@postgres/db",
                "postgresql://u:a@postgres/db",
                "postgresql://u:p@postgres/db", "synthetic-pat-value")
        self.assertEqual(ctx.exception.code, "CONFIG_INVALID")
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            mp._build_e2e_runtime_configs(
                config, None, "postgresql://u:s@postgres/db",
                "postgresql://u:a@postgres/db",
                "postgresql://u:p@postgres/db", "synthetic-pat-value",
                controller_db_env={"PG_HOST": "postgres"},
                controller_pg_pass="synthetic-pg-pass",
                controller_admin_pw="synthetic-admin-pw")
        self.assertEqual(ctx.exception.code, "CONFIG_INVALID")
        cfg = mp._build_e2e_runtime_configs(
            config, None, "postgresql://u:s@postgres/db",
            "postgresql://u:a@postgres/db", "postgresql://u:p@postgres/db",
            "synthetic-pat-value",
            controller_db_env=db_env,
            controller_pg_pass="synthetic-pg-pass",
            controller_admin_pw="synthetic-admin-pw")
        ctrl = cfg["controller"]
        for key, value in dict(db_env, PG_PASS="synthetic-pg-pass",
                               ADMIN_PW="synthetic-admin-pw").items():
            self.assertEqual(ctrl[key], value)
        # the full mapping passes the strict 21-key schema gate
        e2f.validate_e2e_controller_env(ctrl)

    def test_placeholder_role_tokens_rejected_at_schema(self):
        env = {
            "UPSTREAM_URL": rs.GATEWAY_E2E_UPSTREAM,
            "POLICY_FILE": rs.GATEWAY_E2E_POLICY,
            "ROLE_TOKENS": "synthetic-role-token-value",
            "AUDIT_DSN": "postgresql://u:s@postgres/db",
        }
        with self.assertRaises(rs.RuntimeSpecError) as ctx:
            rs.validate_gateway_e2e_env(env)
        self.assertEqual(ctx.exception.code, "RUNTIME_CONFIG_INVALID")

    def test_non_object_role_tokens_rejected(self):
        env = {
            "UPSTREAM_URL": rs.GATEWAY_E2E_UPSTREAM,
            "POLICY_FILE": rs.GATEWAY_E2E_POLICY,
            "ROLE_TOKENS": '["manager"]',
            "AUDIT_DSN": "postgresql://u:s@postgres/db",
        }
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_gateway_e2e_env(env)

    def test_empty_token_value_rejected(self):
        env = {
            "UPSTREAM_URL": rs.GATEWAY_E2E_UPSTREAM,
            "POLICY_FILE": rs.GATEWAY_E2E_POLICY,
            "ROLE_TOKENS": '{"manager":""}',
            "AUDIT_DSN": "postgresql://u:s@postgres/db",
        }
        with self.assertRaises(rs.RuntimeSpecError):
            rs.validate_gateway_e2e_env(env)


# ── D: HiClaw distro executor wiring ───────────────────────────────────────

class TestE2EDemoConsoleMeasuredArgv(unittest.TestCase):

    def test_demo_console_argv_rebuilt_with_measured_ip(self):
        # run31 regression: the E2E path served the PLACEHOLDER bridge
        # IP (203.0.113.1) plan for demo-console, so the console's
        # expected-server-address identity check failed at startup;
        # the planner contract REQUIRES the measured postgres IP.
        import mergepilot as mp
        from unittest import mock

        docker = mock.Mock()
        docker.network_ip.return_value = "172.18.0.99"
        planner = mock.Mock()
        planner.canonicalize_server_address.side_effect = lambda s: s
        canned = [("container-run", "demo-console",
                   ["run", "--env",
                    "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES=172.18.0.99"])]
        with mock.patch.object(mp, "build_start_steps",
                               return_value=canned) as bss:
            argv = mp._e2e_demo_console_measured_argv(
                docker, planner, "run-x", False, "e", "c", "r", "g")
        self.assertIn("MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES=172.18.0.99",
                      argv)
        self.assertEqual(bss.call_args.kwargs.get("bridge_ip"),
                         "172.18.0.99")
        docker.network_ip.assert_called_once()

    def test_demo_console_run_id_is_a_seeded_showcase_case(self):
        # run32 regression: the console run id must be a key the
        # showcase seed actually contains (RUN_NOT_FOUND otherwise);
        # pin the constant to the real showcase case set.
        import importlib.util
        import mergepilot as mp

        spec = importlib.util.spec_from_file_location(
            "showcase_cases_probe",
            ROOT / "tools" / "demo_console" / "showcase_cases.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn(mp.E2E_DEMO_CONSOLE_RUN_ID,
                      mod.SHOWCASE_CASES)

    def test_death_detail_keeps_stderr_tail_separately(self):
        # run31 regression: concatenating stderr before stdout then
        # truncating from the FRONT hid the actual traceback behind
        # stdout preflight lines. The two streams now carry
        # independent tails.
        def fake_exec(argv, **kwargs):
            if argv[:1] == ["inspect"]:
                return _cp(0, b"exited exit=1")
            return subprocess.CompletedProcess(
                argv, 0,
                ("Config preflight passed: mode=ISOLATED_LIVE "
                 + "x" * 60).encode("utf-8"),
                "Traceback ... ValueError: server address mismatch".encode(
                    "utf-8"))

        detail = el._container_death_detail(fake_exec,
                                            "mergepilot-isolated-x-1")
        self.assertIn("err: Traceback ... ValueError:", detail)
        self.assertIn("out: Config preflight passed", detail)


class TestHiclawExecutorFactory(unittest.TestCase):

    def _factory(self):
        import mergepilot as mp
        recorded = []

        class FakeDocker:
            def docker(self, args, *, timeout=90, check=True,
                       log_tag=None, distro=None,
                       suppress_output_log=False):
                recorded.append({"args": list(args), "distro": distro,
                                 "suppress": suppress_output_log,
                                 "log_tag": log_tag})
                return _cp(0, b"ok")

        return mp._e2e_hiclaw_docker_exec(FakeDocker()), recorded

    def test_binds_hiclaw_distro(self):
        hc, recorded = self._factory()
        hc(["inspect", "hiclaw-manager", "--format", "{{.Id}}"])
        self.assertEqual(recorded[0]["distro"], "Ubuntu-22.04")
        self.assertFalse(recorded[0]["suppress"])

    def test_mc_cat_output_log_suppressed(self):
        hc, recorded = self._factory()
        hc(["exec", "hiclaw-controller", "mc", "cat",
            "hiclaw/hiclaw-storage/manager/config/mcporter.json"])
        self.assertTrue(recorded[0]["suppress"],
                        "canonical bodies carry agent tokens")

    def test_cli_wires_hiclaw_executor_and_bearer(self):
        import mergepilot as mp
        src = Path(mp.__file__).read_text(encoding="utf-8")
        self.assertIn("gateway_bearer=role_tokens_reviewer", src)
        self.assertIn("agents_docker_executor=hiclaw_exec", src)
        self.assertIn("docker_executor=hiclaw_exec", src)
        self.assertIn("minio_readonly_via_docker(hiclaw_exec)", src)
        self.assertIn('HICLAW_DISTRO = "Ubuntu-22.04"', src)

    def test_lifecycle_accepts_agents_executor(self):
        src = Path(el.__file__).read_text(encoding="utf-8")
        self.assertIn("agents_docker_executor: Callable = None", src)
        self.assertIn("agents_exec = agents_docker_executor or "
                      "docker_executor", src)


if __name__ == "__main__":
    unittest.main()
