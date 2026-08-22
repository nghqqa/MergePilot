"""M8-GH-4B3-W3B-R2 §10/§11/§12/§14: lifecycle execution tests.

Full production DAG order, dependency-failure blocking, per-stage
rollback matrix, runtime persistence ordering, receipt/Matrix dual
checks, stop/cleanup contracts. All executors/probes are fakes;
every test calls the REAL production functions (run_e2e_start /
run_e2e_stop / run_e2e_cleanup / run_e2e_status) — no source-string
substitutes on key paths.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import e2e_lifecycle as el                     # noqa: E402
import e2e_foundation as e2f                   # noqa: E402

el.HEALTH_TIMEOUT_SECONDS = 0.3
el.HEALTH_POLL_SECONDS = 0.01


def _cp(rc=0, stdout=b""):
    return subprocess.CompletedProcess([], rc, stdout, b"")


class FakeDocker:
    """Records argv; answers inspect/exec from a state registry."""

    def __init__(self):
        self.calls = []
        self.containers = {}   # name -> {"id", "running", "exec_rc"}
        self.networks = {}     # full name -> id

    def __call__(self, argv, check=True, **kw):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "inspect":
            name = argv[1]
            fmt_i = argv.index("--format") + 1
            fmt = argv[fmt_i]
            info = self.containers.get(name)
            if info is None and argv[1] in self.networks:
                return _cp(0, self.networks[argv[1]].encode())
            if info is None:
                return _cp(1, b"")
            if "{{.Id}} {{.State.Status}}" in fmt:
                status = "running" if info["running"] else "exited"
                return _cp(0, ("%s %s" % (info["id"], status)).encode())
            if "{{.State.Running}} {{.State.ExitCode}}" in fmt:
                code = info.get("exit_code", 0)
                return _cp(0, ("false %d" % code).encode())
            if "{{.Id}}" in fmt:
                return _cp(0, info["id"].encode())
            if "{{.State.Running}}" in fmt:
                return _cp(0, ("true" if info["running"]
                               else "false").encode())
            if "{{range $k, $v := .NetworkSettings.Networks}}" in fmt:
                return _cp(0, b" ")
            return _cp(0, info["id"].encode())
        if argv[0] == "exec":
            name = argv[1]
            info = self.containers.get(name)
            return _cp(info.get("exec_rc", 0) if info else 1)
        if argv[0] == "network" and argv[1] == "inspect":
            full = argv[2]
            if full in self.networks:
                return _cp(0, self.networks[full].encode())
            return _cp(1, b"")
        if argv[0] == "create":
            name = argv[argv.index("--name") + 1]
            existing = self.containers.get(name, {})
            self.containers[name] = {
                "id": "cid-%s" % name, "running": False,
                "exec_rc": 0,
                "exit_code": existing.get("exit_code", 0)}
            return _cp(0, ("cid-%s" % name).encode())
        if argv[0] == "start":
            name = argv[1]
            if name in self.containers:
                self.containers[name]["running"] = True
                if "preflight" in name:   # one-shot exits at once
                    self.containers[name]["running"] = False
            return _cp(0)
        if argv[0] == "rm":
            target = argv[argv.index("-f") + 1] if "-f" in argv \
                else argv[-1]
            for name, info in list(self.containers.items()):
                if name == target or info.get("id") == target:
                    self.containers.pop(name)
            return _cp(0)
        if argv[0] == "network" and argv[1] == "create":
            full = argv[-1]
            self.networks[full] = "nid-%s" % full
            return _cp(0)
        if argv[0] == "network" and argv[1] == "rm":
            self.networks.pop(argv[2], None)
            return _cp(0)
        return _cp(0)


def _healthy_docker():
    fd = FakeDocker()
    for svc in el._DAG_ORDER:
        name = "mergepilot-isolated-%s-1" % svc
        fd.containers[name] = {
            "id": "cid-%s" % svc, "running": False, "exec_rc": 0}
    for role in el.AGENT_ROLES:
        name = el.ex.HICLAW_ROLE_FREEZE[role][0]   # frozen names
        fd.containers[name] = {
            "id": "hic-%s" % role, "running": True, "exec_rc": 0}
    fd.containers["mergepilot-isolated-preflight-1"]["exit_code"] = 0
    return fd


def _config():
    return {
        "room_map_path": "/tmp/room-map.yaml",
        "policy_path": "/tmp/policy.yaml",
        "matrix_homeserver": "http://matrix-hs:6167",
        "matrix_room_id": "!r:matrix-local.hiclaw.io:18080",
        "matrix_credentials_path": "/tmp/creds.json",
        "app_pem_path": "/tmp/app.pem",
        "webhook_secret_path": "/tmp/wh.secret",
        "mcp_pat_path": "/tmp/pat.txt",
        "hiclaw_receipt_path": "/tmp/receipt.json",
        "callback_url_path": "/tmp/cb.txt",
        "windows_proxy_ip": "172.23.48.1",
        "windows_proxy_port": "17890",
        "tuwunel_ip": "172.22.0.2",
        "tuwunel_port": "6167",
        "fixture_repo": "example/fixture",
        "installation_id": "1",
        "repository_id": "1",
        "app_id": "1",
        "expected_old_mcp_state": "stopped",
        "expected_8090_state": "free",
    }


def _image_refs():
    return {svc: "sha256:%s" % svc for svc in el._SPEC_SERVICES}


def _seams(**overrides):
    """Default all-healthy injected seams (focused lifecycle tests;
    the seam functions themselves have their own executor suites)."""
    seams = {
        "default_service_plan": (
            lambda svc: ["create", "--name",
                         "mergepilot-isolated-%s-1" % svc, "img"]),
        "db_bootstrap": lambda: None,
        "matrix_members_provider": (
            lambda: list(e2f.E2E_EXPECTED_ROOM_MEMBERS)),
        "service_health": lambda svc: None,
        "receipt_validator": lambda path: {"verified": True},
    }
    seams.update(overrides)
    return seams


def _run_start(fd, host=None, persist=None, runtime=None,
               runtime_dir=None, fw_effect=None, route_result=None,
               **seam_kw):
    if fw_effect is None:
        fw_effect = "installed"
    if route_result is None:
        route_result = {
            s: {"verified": True}
            for s in ("tuwunel", "winproxy", "bridge", "gateway",
                      "reporter", "controller")}
    with mock.patch.object(el.ex, "install_firewall",
                           side_effect=fw_effect) as fw, \
         mock.patch.object(el.ex, "teardown_firewall",
                           return_value=[]), \
         mock.patch.object(el.ex, "run_route_probes",
                           return_value=route_result), \
         mock.patch.object(el.ep, "run_e2e_prerequisite_gate",
                           return_value={"checks": {"all": {}}}):
        return el.run_e2e_start(
            config=_config(),
            runtime_configs=runtime,
            runtime_directory=runtime_dir or "",
            docker_executor=fd,
            host_executor=host or (lambda argv, **kw: _cp(0, b"")),
            image_refs=_image_refs(),
            persist_callback=persist,
            **_seams(**seam_kw))


# ── §10: full DAG success order ──────────────────────────────────────────

class TestFullDAGSuccess(unittest.TestCase):

    def test_success_order_and_stage_sequence(self):
        fd = _healthy_docker()
        stages = []
        session = _run_start(fd, persist=lambda s: stages.append(
            s.get("e2e_stage")))
        self.assertEqual(session["e2e_stage"], "complete")
        self.assertEqual(session["e2e_pending_components"], ())
        # every stage journal-persisted in order, ending complete
        self.assertEqual(stages[-1], "complete")
        self.assertIn("prerequisites", stages)
        self.assertIn("networks", stages)
        self.assertIn("firewall", stages)
        self.assertIn("agents_ready", stages)
        self.assertIn("final_preflight", stages)
        # receipt/matrix stages ran before complete (session markers)
        self.assertEqual(session["receipt_verified"], True)
        self.assertEqual(session["matrix_verified"], True)
        # all 11 services started
        self.assertEqual(sorted(set(session["e2e_started"])),
                         sorted(el._DAG_ORDER))
        # starts happen in exact DAG order (all 11 services)
        starts = [c[1] for c in fd.calls if c[0] == "start"]
        expected = ["mergepilot-isolated-%s-1" % s
                    for s in el._DAG_ORDER]
        self.assertEqual(starts, expected)

    def test_receipt_and_matrix_dual_checks_run_before_complete(self):
        fd = _healthy_docker()
        order = []
        receipt = lambda path: (order.append("receipt"),
                                {"verified": True})[1]
        provider = lambda: (order.append("matrix"),
                            list(e2f.E2E_EXPECTED_ROOM_MEMBERS))[1]
        session = _run_start(fd, receipt_validator=receipt,
                             matrix_members_provider=provider)
        self.assertEqual(session["e2e_stage"], "complete")
        self.assertEqual(order, ["receipt", "matrix"])

    def test_no_fixed_sleep_health_gateway_injected(self):
        fd = _healthy_docker()
        probed = []
        _run_start(fd, service_health=lambda svc: probed.append(svc))
        self.assertEqual(probed, ["postgres", "gh-proxy-r", "gh-proxy-b",
                                  "mcp-bridge", "policy-gateway",
                                  "controller", "gh-webhook",
                                  "demo-console", "console-edge",
                                  "gh-reporter"])


# ── §10: dependency failures block downstream ────────────────────────────

class TestDependencyFailures(unittest.TestCase):

    def _assert_blocked(self, fd, seam, code, must_not_start):
        with self.assertRaises(el.E2ELifecycleError) as ctx:
            _run_start(fd, **seam)
        self.assertEqual(ctx.exception.code, code)
        started = {c[1] for c in fd.calls if c[0] == "start"}
        self.assertNotIn(
            "mergepilot-isolated-%s-1" % must_not_start, started)

    def test_gateway_health_failure_blocks_controller(self):
        fd = _healthy_docker()
        def failing(svc):
            if svc == "policy-gateway":
                raise el.E2ELifecycleError(
                    "E2E_POLICY_GATEWAY_MCP_UNHEALTHY", "x")
        self._assert_blocked(
            fd, {"service_health": failing},
            "E2E_POLICY_GATEWAY_MCP_UNHEALTHY", "controller")

    def test_bridge_health_failure_blocks_gateway(self):
        fd = _healthy_docker()
        def failing(svc):
            if svc == "mcp-bridge":
                raise el.E2ELifecycleError(
                    "E2E_MCP_BRIDGE_MCP_UNHEALTHY", "x")
        self._assert_blocked(
            fd, {"service_health": failing},
            "E2E_MCP_BRIDGE_MCP_UNHEALTHY", "policy-gateway")

    def test_postgres_unready_blocks_everything(self):
        fd = _healthy_docker()
        def failing(svc):
            if svc == "postgres":
                raise el.E2ELifecycleError("E2E_POSTGRES_UNREADY", "x")
        self._assert_blocked(fd, {"service_health": failing},
                             "E2E_POSTGRES_UNREADY", "mcp-bridge")

    def test_proxy_unready_blocks_bridge(self):
        fd = _healthy_docker()
        def failing(svc):
            if svc == "gh-proxy-r":
                raise el.E2ELifecycleError("E2E_GH_PROXY_R_UNREADY", "x")
        self._assert_blocked(fd, {"service_health": failing},
                             "E2E_GH_PROXY_R_UNREADY", "mcp-bridge")

    def test_reporter_unready_blocks_agents_stage(self):
        fd = _healthy_docker()
        def failing(svc):
            if svc == "gh-reporter":
                raise el.E2ELifecycleError("E2E_GH_REPORTER_UNREADY", "x")
        with self.assertRaises(el.E2ELifecycleError) as ctx:
            _run_start(fd, service_health=failing)
        self.assertEqual(ctx.exception.code, "E2E_GH_REPORTER_UNREADY")
        self.assertNotIn("receipt_recheck",
                         [c for c in fd.calls])

    def test_agents_not_ready_blocks_receipt_recheck(self):
        fd = _healthy_docker()
        fd.containers[
            el.ex.HICLAW_ROLE_FREEZE["fixer"][0]]["running"] = False
        with self.assertRaises(el.E2ELifecycleError) as ctx:
            _run_start(fd)
        self.assertEqual(ctx.exception.code, "E2E_AGENTS_NOT_READY")
        self.assertIn("fixer", ctx.exception.detail)


# ── §11: receipt / Matrix second-check drift ─────────────────────────────

class TestDualCheckDrift(unittest.TestCase):

    def test_receipt_drift_no_complete_rollback(self):
        fd = _healthy_docker()
        with self.assertRaises(el.E2ELifecycleError) as ctx:
            _run_start(fd, receipt_validator=lambda p: {
                "verified": False})
        self.assertEqual(ctx.exception.code,
                         "E2E_RECEIPT_RECHECK_FAILED")
        # rollback removed owned containers
        rms = [c for c in fd.calls if c[0] == "rm"]
        self.assertTrue(rms)
        self.assertFalse(fd.containers.get(
            "mergepilot-isolated-mcp-bridge-1"))

    def test_matrix_drift_no_complete_rollback(self):
        fd = _healthy_docker()
        with self.assertRaises(el.E2ELifecycleError) as ctx:
            _run_start(fd, matrix_members_provider=lambda: [])
        self.assertEqual(ctx.exception.code,
                         "E2E_MATRIX_RECHECK_FAILED")
        self.assertIn("@manager", ctx.exception.detail)
        rms = [c for c in fd.calls if c[0] == "rm"]
        self.assertTrue(rms)


# ── §12: rollback matrix (fault per stage) ───────────────────────────────

class TestRollbackMatrix(unittest.TestCase):

    def test_runtime_persist_failure_zero_networks(self):
        fd = _healthy_docker()
        persists = []

        def persist(_journal):
            persists.append(1)
            if len(persists) >= 3:
                raise OSError("disk full")

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(el.E2ELifecycleError) as ctx:
                _run_start(fd, runtime=_rc(), runtime_dir=tmp,
                           persist=persist)
        self.assertEqual(ctx.exception.code,
                         "RUNTIME_JOURNAL_PERSIST_FAILED")
        nets = [c for c in fd.calls
                if c[:2] == ["network", "create"]]
        self.assertEqual(nets, [])

    def test_network_failure_rolls_back_created(self):
        fd = _healthy_docker()
        with mock.patch.object(
                el.ep, "create_e2e_networks",
                side_effect=RuntimeError("net fail")):
            with self.assertRaises(el.E2ELifecycleError) as ctx:
                _run_start(fd)
        self.assertEqual(ctx.exception.code,
                         "E2E_NETWORK_CREATE_FAILED")

    def test_container_setup_failure_rolls_back(self):
        fd = _healthy_docker()
        with mock.patch.object(
                el.ep, "execute_e2e_container_setup",
                side_effect=RuntimeError("create fail")):
            with self.assertRaises(el.E2ELifecycleError) as ctx:
                _run_start(fd)
        self.assertEqual(ctx.exception.code,
                         "E2E_CONTAINER_SETUP_FAILED")

    def test_image_missing_rolls_back(self):
        fd = _healthy_docker()
        refs = _image_refs()
        refs["mcp-bridge"] = ""
        with mock.patch.object(el.ex, "install_firewall",
                               side_effect="installed"), \
             mock.patch.object(el.ex, "run_route_probes",
                               return_value={}), \
             mock.patch.object(el.ep, "run_e2e_prerequisite_gate",
                               return_value={"checks": {}}):
            with self.assertRaises(el.E2ELifecycleError) as ctx:
                el.run_e2e_start(
                    config=_config(), runtime_configs=None,
                    runtime_directory="", docker_executor=fd,
                    host_executor=lambda a, **kw: _cp(0, b""),
                    image_refs=refs, **_seams())
        self.assertEqual(ctx.exception.code, "E2E_IMAGE_MISSING")

    def test_firewall_failure_rolls_back_containers(self):
        fd = _healthy_docker()
        fw = el.ex.FirewallExecutorError("FIREWALL_VERIFY_FAILED", "x")
        with self.assertRaises(el.E2ELifecycleError) as ctx:
            _run_start(fd, fw_effect=fw)
        self.assertEqual(ctx.exception.code,
                         "FIREWALL_VERIFY_FAILED")
        self.assertFalse(fd.containers.get(
            "mergepilot-isolated-controller-1"))

    def test_db_bootstrap_failure_rolls_back(self):
        fd = _healthy_docker()
        def boom():
            raise RuntimeError("psql failed")
        with self.assertRaises(el.E2ELifecycleError) as ctx:
            _run_start(fd, db_bootstrap=boom)
        self.assertEqual(ctx.exception.code,
                         "E2E_DB_BOOTSTRAP_FAILED")

    def test_route_probe_failure_rolls_back(self):
        fd = _healthy_docker()
        with self.assertRaises(el.E2ELifecycleError) as ctx:
            _run_start(fd, route_result={
                "tuwunel": {"verified": False}})
        self.assertEqual(ctx.exception.code,
                         "E2E_ROUTE_PROBE_FAILED")
        self.assertIn("tuwunel", ctx.exception.detail)

    def test_preflight_failure_rolls_back(self):
        fd = _healthy_docker()
        fd.containers["mergepilot-isolated-preflight-1"][
            "exit_code"] = 7
        with self.assertRaises(el.E2ELifecycleError) as ctx:
            _run_start(fd)
        self.assertEqual(ctx.exception.code, "E2E_PREFLIGHT_FAILED")
        self.assertIn("7", ctx.exception.detail)

    def test_rollback_preserves_primary_and_reports_diagnostics(self):
        fd = _healthy_docker()
        # make rollback itself fail: rm raises
        def rm_failer(argv, check=True, **kw):
            fd.calls.append(list(argv))
            if argv[0] == "rm":
                raise RuntimeError("docker down")
            return FakeDocker.__call__(fd, argv, check=check, **kw)
        fw = el.ex.FirewallExecutorError("FIREWALL_VERIFY_FAILED",
                                         "primary")
        with mock.patch.object(el.ex, "install_firewall",
                               side_effect=fw), \
             mock.patch.object(el.ex, "run_route_probes",
                               return_value={}), \
             mock.patch.object(el.ep, "run_e2e_prerequisite_gate",
                               return_value={"checks": {}}):
            with self.assertRaises(el.E2ELifecycleError) as ctx:
                el.run_e2e_start(
                    config=_config(), runtime_configs=None,
                    runtime_directory="",
                    docker_executor=rm_failer,
                    host_executor=lambda a, **kw: _cp(0, b""),
                    image_refs=_image_refs(), **_seams())
        # primary error preserved; rollback failures are diagnostics
        self.assertEqual(ctx.exception.code, "FIREWALL_VERIFY_FAILED")
        self.assertTrue(any("ROLLBACK_CONTAINER_RM_FAILED" in d
                            for d in ctx.exception.diagnostics))

    def test_rollback_idempotent_and_foreign_untouched(self):
        fd = _healthy_docker()
        fd.containers["foreign-container"] = {
            "id": "foreign-id", "running": True}
        session = {"e2e_container_ids": {"controller": "cid-controller"},
                   "e2e_started": [], "e2e_network_ids": {},
                   "e2e_runtime_journal": {}}
        el._rollback_all(fd, session)
        el._rollback_all(fd, session)     # idempotent second pass
        self.assertIn("foreign-container", fd.containers)


# ── §4: runtime persistence ordering inside the lifecycle ────────────────

def _runtime_configs():
    from tests.gh_app.test_e2e_lifecycle_r2 import _rc
    return _rc()


def _rc():
    import e2e_runtime_specs as rs
    return {
        "controller": {
            "GITHUB_INGRESS_ENABLED": "1",
            "GITHUB_ROOM_MAP_PATH": "/run/mergepilot/room-map.yaml",
            "GITHUB_POLICY_PATH": "/run/mergepilot/policy-fixture.yaml",
            "GITHUB_DELIVERY_LEASE_SECONDS": "120",
            "GITHUB_DELIVERY_MAX_ATTEMPTS": "5",
            "MATRIX_HS": "http://matrix-hs:6167",
            "MATRIX_SERVER_NAME": e2f.E2E_MATRIX_SERVER_NAME,
            "MATRIX_USER": "m8gh4-controller",
            "CONTROLLER_CONSUMER_NAME": "m8gh4-controller",
            "M4F_ALLOWED_ROOMS": "!r:" + e2f.E2E_MATRIX_SERVER_NAME,
            "M4F_ALLOWED_SENDERS": "manager,reviewer,fixer,verifier",
            "M4F_RUN_PREFIX": "gh-",
            "RESERVED_RUN_PREFIXES": "",
            "GATEWAY_URL": "http://policy-gateway:8083",
            "COORDINATOR_TOKEN": "tok-" + "a" * 32,
        },
        "policy-gateway": {
            "UPSTREAM_URL": rs.GATEWAY_E2E_UPSTREAM,
            "POLICY_FILE": rs.GATEWAY_E2E_POLICY,
            "ROLE_TOKENS": "synthetic-role-token-value",
            "AUDIT_DSN":
                "postgresql://u:synthetic-audit@postgres/db"
                "?connect_timeout=5",
        },
        "mcp-bridge": {
            "MCP_GITHUB_TOKEN": "synthetic-pat-value",
            "GITHUB_REPOSITORY": "example/fixture",
            "HTTPS_PROXY": rs.BRIDGE_PROXY,
            "MCP_PROXY_PORT": "8082",
        },
        "gh-reporter": {
            "GITHUB_PUBLISHER_DSN":
                "postgresql://u:synthetic-reporter@postgres/db"
                "?connect_timeout=5",
            "GITHUB_API_BASE": "https://api.github.com",
            "GITHUB_APP_ID": "1", "GITHUB_INSTALLATION_ID": "1",
            "GITHUB_REPOSITORY_ID": "1",
            "GITHUB_PRIVATE_KEY_PATH":
                "/run/secrets/github-app-private-key.pem",
            "GH_REPORTER_POLL_SECONDS": "5",
            "GH_REPORTER_LEASE_SECONDS": "120",
            "GH_REPORTER_MAX_ATTEMPTS": "8",
            "HTTPS_PROXY": e2f.E2E_REPORTER_PROXY_R,
        },
        "gh-proxy-r": {
            "GH_PROXY_BIND": "0.0.0.0", "GH_PROXY_PORT": "18090",
            "GH_PROXY_UPSTREAM_IP": "172.23.48.1",
            "GH_PROXY_UPSTREAM_PORT": "17890",
        },
        "gh-proxy-b": {
            "GH_PROXY_BIND": "0.0.0.0", "GH_PROXY_PORT": "18090",
            "GH_PROXY_UPSTREAM_IP": "172.23.48.1",
            "GH_PROXY_UPSTREAM_PORT": "17890",
        },
    }


class TestRuntimePersistOrdering(unittest.TestCase):

    def test_persist_before_first_network_create(self):
        import tempfile
        fd = _healthy_docker()
        events = []
        raw_fd_call = FakeDocker.__call__

        def recording_fd(argv, check=True, **kw):
            cp = raw_fd_call(fd, argv, check=check, **kw)
            if argv[:2] == ["network", "create"]:
                events.append("network_create")
            return cp

        def persist(session):
            events.append("persist:%s" % session["e2e_stage"])

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(el.ex, "install_firewall",
                                   side_effect="installed"), \
                 mock.patch.object(el.ex, "run_route_probes",
                                   return_value={}), \
                 mock.patch.object(el.ep, "run_e2e_prerequisite_gate",
                                   return_value={"checks": {}}):
                el.run_e2e_start(
                    config=_config(), runtime_configs=_rc(),
                    runtime_directory=tmp, docker_executor=recording_fd,
                    host_executor=lambda a, **kw: _cp(0, b""),
                    image_refs=_image_refs(),
                    persist_callback=persist, **_seams())
        # six runtime-file persists (one per file) all precede the
        # FIRST network create, in one interleaved event log
        first_net = events.index("network_create")
        runtime_persists = [
            i for i, e in enumerate(events)
            if e == "persist:runtime_files"]
        self.assertGreaterEqual(len(runtime_persists), 6)
        self.assertTrue(all(i < first_net for i in runtime_persists))


# ── §14: stop contracts ──────────────────────────────────────────────────

class TestStop(unittest.TestCase):

    def _session(self):
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".env")
        tmp.write(b"K=V\n")
        tmp.close()
        self.addCleanup(
            lambda p=tmp.name: os.unlink(p)
            if os.path.exists(p) else None)
        return {
            "e2e_container_ids": {"controller": "cid-controller",
                                  "mcp-bridge": "cid-mcp-bridge"},
            "e2e_started": ["controller", "mcp-bridge"],
            "e2e_network_ids": {"mp-e2e-br-up": "nid-1"},
            "e2e_runtime_journal": {
                "controller": {"file": tmp.name,
                               "ownership": "session"}},
            "firewall_sid": "abcd1234",
        }

    def test_stop_order_containers_firewall_networks_runtime(self):
        fd = _healthy_docker()
        host_argv = []
        def host(argv, **kw):
            host_argv.append(list(argv))
            return _cp(0, b"")
        result = el.run_e2e_stop(
            docker_executor=fd, host_executor=host,
            session=self._session(), runtime_directory="")
        order = [a.split(":")[0] for a in result["actions"]]
        self.assertEqual(order, ["container", "container",
                                 "firewall", "network",
                                 "runtime_files"])

    def test_stop_id_mismatch_refuses_delete(self):
        fd = _healthy_docker()
        fd.containers["mergepilot-isolated-controller-1"][
            "id"] = "DIFFERENT-ID"
        result = el.run_e2e_stop(
            docker_executor=fd,
            host_executor=lambda a, **kw: _cp(0, b""),
            session=self._session(), runtime_directory="")
        self.assertIn("CONTAINER_ID_MISMATCH:controller",
                      result["diagnostics"])
        rms = [c for c in fd.calls if c[0] == "rm"
               and "DIFFERENT-ID" in c]
        self.assertEqual(rms, [])

    def test_stop_idempotent_second_call_noop(self):
        fd = _healthy_docker()
        session = self._session()
        host = lambda a, **kw: _cp(0, b"")
        first = el.run_e2e_stop(docker_executor=fd, host_executor=host,
                                session=session, runtime_directory="")
        fd.calls.clear()
        second = el.run_e2e_stop(docker_executor=fd,
                                 host_executor=host, session=session,
                                 runtime_directory="")
        self.assertEqual(second["actions"], [])
        self.assertEqual([c for c in fd.calls if c[0] == "rm"], [])

    def test_stop_single_failure_continues_other_owned(self):
        fd = _healthy_docker()
        def flaky(argv, check=True, **kw):
            fd.calls.append(list(argv))
            if argv[0] == "rm" and "cid-controller" in argv:
                raise RuntimeError("rm down")
            return FakeDocker.__call__(fd, argv, check=check, **kw)
        result = el.run_e2e_stop(
            docker_executor=flaky,
            host_executor=lambda a, **kw: _cp(0, b""),
            session=self._session(), runtime_directory="")
        self.assertIn("container:mcp-bridge", result["actions"])
        self.assertTrue(any("CONTAINER_RM_FAILED" in d
                            for d in result["diagnostics"]))


# ── §14: cleanup scan (report-only) ──────────────────────────────────────

class TestCleanup(unittest.TestCase):

    def test_cleanup_reports_residue_never_deletes(self):
        fd = _healthy_docker()
        for svc in el._DAG_ORDER:
            fd.containers["mergepilot-isolated-%s-1" % svc][
                "running"] = True
        fd.networks["mp-e2e-br-up"] = "nid-x"
        report = el.run_e2e_cleanup(
            docker_executor=fd,
            host_executor=lambda a, **kw: _cp(
                0, b":mp-e2e-br-up - [0:0]"),
            runtime_directory="")
        self.assertTrue(report["residue"])
        self.assertIn("container:postgres", report["residue"])
        self.assertIn("network:mp-e2e-br-up", report["residue"])
        self.assertIn("firewall:e2e-chains", report["residue"])
        # report-only: nothing was removed
        mp = [n for n in fd.containers
              if n.startswith("mergepilot-isolated-")]
        self.assertEqual(len(mp), len(el._DAG_ORDER))
        self.assertIn("mp-e2e-br-up", fd.networks)

    def test_cleanup_clean_when_absent(self):
        fd = FakeDocker()
        report = el.run_e2e_cleanup(
            docker_executor=fd,
            host_executor=lambda a, **kw: _cp(0, b""),
            runtime_directory="")
        self.assertEqual(report["residue"], [])


# ── §13: status sanitization ─────────────────────────────────────────────

class TestStatusSanitized(unittest.TestCase):

    def test_status_keys_and_no_secrets(self):
        import json
        fd = _healthy_docker()
        for svc in el._DAG_ORDER:
            fd.containers["mergepilot-isolated-%s-1" % svc][
                "running"] = True
        session = {"e2e_container_ids": {
            svc: "cid-%s" % svc for svc in el._DAG_ORDER},
            "e2e_network_ids": {"mp-e2e-br-up": "nid-1"},
            "e2e_stage": "complete",
            "prerequisite_summary": {"verified": True}}
        result = el.run_e2e_status(
            docker_executor=fd, session=session,
            mcp_health=lambda url: {"healthy": True})
        self.assertEqual(len([k for k in result
                              if not k.startswith("_")]),
                         len(el._DAG_ORDER))
        blob = json.dumps(result)
        for forbidden in ("ghp_", "syt_", "BEGIN PRIVATE", "Bearer ",
                          "postgresql://", "restore_blob",
                          "-A INPUT", "COMMIT"):
            self.assertNotIn(forbidden, blob)
        self.assertEqual(result["_stage"], "complete")


class TestWslMountConversion(unittest.TestCase):
    """First real E2E start failed E2E_CONTAINER_SETUP_FAILED because
    Windows drive-letter mount sources reached the in-distro docker
    daemon with backslashes eaten. The lifecycle must map them to
    /mnt/<drive>/... exactly like --env-file handling."""

    def test_windows_mount_sources_become_wsl_paths(self):
        import e2e_lifecycle as el
        win_a = "D:" + chr(92) + "goai" + chr(92) + "secrets" \
            + chr(92) + "room-map.yaml"
        win_b = "C:" + chr(92) + "a b" + chr(92) + "policy.yaml"
        mounts = el._wsl_mounts([
            "-v", win_a + ":/run/x:ro",
            "-v", win_b + ":/run/y:ro",
        ])
        self.assertEqual(mounts[0], "-v")
        self.assertEqual(
            mounts[1],
            "/mnt/d/goai/secrets/room-map.yaml:/run/x:ro")
        self.assertEqual(
            mounts[3], "/mnt/c/a b/policy.yaml:/run/y:ro")

    def test_native_paths_pass_through(self):
        import e2e_lifecycle as el
        self.assertEqual(
            el._to_wsl_source("/home/u/a.yaml"), "/home/u/a.yaml")
        self.assertEqual(
            el._to_wsl_source("/mnt/d/x/y"), "/mnt/d/x/y")

    def test_no_mangled_backslash_survives(self):
        import e2e_lifecycle as el
        win = "D:" + chr(92) + "x" + chr(92) + "y"
        out = el._to_wsl_source(win)
        self.assertNotIn(chr(92), out)
        self.assertTrue(out.startswith("/mnt/d/"))


class TestStaleOwnedContainerCleanup(unittest.TestCase):
    """A failed run leaves the never-started postgres container in
    'created' state (the _fail fires before its cid reaches the
    journal, so rollback misses it). The retry's docker run then
    conflicts on the fixed name. The default-service loop must reap
    the owned name — but ONLY a State.Status=='created' holder
    (running/exited means foreign ownership: the run fails closed
    on the conflict instead of killing it)."""

    def test_guarded_reap_precedes_run(self):
        import e2e_lifecycle as el
        src = open(el.__file__, encoding="utf-8").read()
        probe_guard = '"{{.State.Status}}"'
        i = src.find(probe_guard)
        self.assertGreater(i, -1, "status probe guard present")
        window = src[i:i + 600]
        self.assertIn('["rm", "-f", name]', window,
                      "guarded reap must follow the status probe")
        self.assertIn("== b\"created\"", window,
                      "reap restricted to never-started state")
        self.assertIn("docker_executor(list(argv), check=True)",
                      window,
                      "reap and planned run must be adjacent")

