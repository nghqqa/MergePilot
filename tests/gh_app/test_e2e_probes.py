"""M8-GH-4B3-W2 tests: prerequisite probes, 8-network executor, and
multi-homed container argv. Fully mocked (fake executors, synthetic
files); no real Docker, no real network, no real secrets."""

from __future__ import annotations

import json
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
import e2e_probes as ep                         # noqa: E402


def _valid_config(**overrides):
    base = {
        "room_map_path": "/tmp/room-map.yaml",
        "policy_path": "/tmp/policy.yaml",
        "matrix_homeserver": "http://matrix:6167",
        "matrix_room_id": "!room:server",
        "matrix_credentials_path": "/tmp/creds.json",
        "app_pem_path": "/tmp/app.pem",
        "webhook_secret_path": "/tmp/whsec",
        "mcp_pat_path": "/tmp/pat",
        "hiclaw_receipt_path": "/tmp/receipt.json",
        "callback_url_path": "/tmp/callback",
        "windows_proxy_ip": "172.23.48.1",
        "windows_proxy_port": "17890",
        "tuwunel_ip": "172.22.0.2",
        "tuwunel_port": "6167",
        "fixture_repo": "example/fixture",
        "installation_id": "154914965",
        "repository_id": "1314399289",
        "app_id": "4648333",
        "expected_old_mcp_state": "stopped",
        "expected_8090_state": "free",
    }
    base.update(overrides)
    return base


class TestPrereqConfig(unittest.TestCase):

    def test_valid(self):
        cfg = ep.validate_prereq_config(_valid_config())
        self.assertEqual(cfg["windows_proxy_port"], "17890")

    def test_unknown_key(self):
        with self.assertRaises(ep.PrereqConfigError):
            ep.validate_prereq_config(_valid_config(UNKNOWN="x"))

    def test_missing_key(self):
        cfg = _valid_config()
        del cfg["app_id"]
        with self.assertRaises(ep.PrereqConfigError):
            ep.validate_prereq_config(cfg)

    def test_blank_value(self):
        with self.assertRaises(ep.PrereqConfigError):
            ep.validate_prereq_config(_valid_config(app_id="  "))

    def test_traversal_rejected(self):
        with self.assertRaises(ep.PrereqConfigError):
            ep.validate_prereq_config(
                _valid_config(room_map_path="/tmp/../etc/passwd"))

    def test_directory_path_rejected(self):
        with self.assertRaises(ep.PrereqConfigError):
            ep.validate_prereq_config(
                _valid_config(app_pem_path="/tmp/secrets/"))

    def test_ip_literal_required(self):
        with self.assertRaises(ep.PrereqConfigError):
            ep.validate_prereq_config(
                _valid_config(windows_proxy_ip="proxy.example.com"))

    def test_port_exact(self):
        with self.assertRaises(ep.PrereqConfigError):
            ep.validate_prereq_config(_valid_config(tuwunel_port="8080"))

    def test_repo_format(self):
        with self.assertRaises(ep.PrereqConfigError):
            ep.validate_prereq_config(_valid_config(fixture_repo="no-slash"))

    def test_numeric_ids(self):
        with self.assertRaises(ep.PrereqConfigError):
            ep.validate_prereq_config(_valid_config(installation_id="abc"))

    def test_state_enums(self):
        with self.assertRaises(ep.PrereqConfigError):
            ep.validate_prereq_config(
                _valid_config(expected_old_mcp_state="paused"))


class _CP:
    def __init__(self, rc=0, out=b""):
        self.returncode = rc
        self.stdout = out


class _FakeDocker:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def __call__(self, argv, check=True, **kw):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for pattern in self.fail_on:
            if pattern in joined:
                if check:
                    raise RuntimeError("fake-docker failure: %s" % pattern)
                return _CP(1, b"")
        if argv[:2] == ["network", "inspect"]:
            return _CP(0, ("net-id-%s" % argv[2]).encode())
        if argv[0] == "inspect":
            return _CP(0, b"ctr-id-123")
        return _CP(0, b"")


class TestNetworkExecutor(unittest.TestCase):

    def test_create_all_8_networks(self):
        fd = _FakeDocker()
        journal = {}
        created = ep.create_e2e_networks(fd, journal=journal)
        self.assertEqual(len(created), 8)
        self.assertEqual(len(journal), 8)
        create_calls = [c for c in fd.calls if c[0] == "network"
                        and c[1] == "create"]
        self.assertEqual(len(create_calls), 8)
        subnets = [c[c.index("--subnet") + 1] for c in create_calls]
        self.assertIn("172.31.0.0/28", subnets)
        self.assertIn("172.31.0.128/28", subnets)

    def test_create_failure_rolls_back(self):
        fd = _FakeDocker(fail_on={"mp-e2e-pxb"})
        journal = {}
        with self.assertRaises(Exception):
            ep.create_e2e_networks(fd, journal=journal)
        self.assertEqual(len(journal), 0)
        rm_calls = [c for c in fd.calls if c[:2] == ["network", "rm"]]
        self.assertTrue(len(rm_calls) > 0, "should have cleanup calls")

    def test_remove_networks(self):
        fd = _FakeDocker()
        journal = {"mp-e2e-ctrl-egress": "id1", "mp-e2e-pxr": "id2"}
        removed = ep.remove_e2e_networks(fd, journal=journal)
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(journal), 0)


class TestContainerArgv(unittest.TestCase):

    def test_controller_create_uses_network_none(self):
        argv = ep.plan_e2e_container_create(
            "controller", image_ref="sha256:ab")
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")

    def test_controller_connects_with_ip_and_priority(self):
        connects = ep.plan_e2e_container_connects("controller")
        self.assertEqual(len(connects), 2)
        first = connects[0]
        self.assertEqual(first[first.index("--ip") + 1], "172.31.0.2")
        self.assertEqual(first[first.index("--gw-priority") + 1], "100")

    def test_all_6_containers_have_attachments(self):
        for service in ("controller", "policy-gateway", "mcp-bridge",
                        "gh-reporter", "gh-proxy-r", "gh-proxy-b"):
            attaches = ep.E2E_CONTAINER_ATTACHMENTS[service]
            self.assertEqual(len(attaches), 2, service)
            self.assertEqual(attaches[0][2], 100,
                             "%s first attach must be priority 100" % service)
            self.assertEqual(attaches[1][2], 0,
                             "%s second attach must be priority 0" % service)

    def test_pat_only_in_bridge_env(self):
        bridge = ep.plan_e2e_container_create(
            "mcp-bridge", image_ref="sha256:ab", env_file="mcp_bridge.env")
        self.assertIn("mcp_bridge.env", bridge)
        reporter = ep.plan_e2e_container_create(
            "gh-reporter", image_ref="sha256:ab",
            env_file="gh_reporter.env")
        self.assertNotIn("mcp_bridge.env", reporter)

    def test_pem_only_in_reporter_mounts(self):
        reporter = ep.plan_e2e_container_create(
            "gh-reporter", image_ref="sha256:ab",
            mounts=["-v", "/host.pem:/run/secrets/key.pem:ro"])
        mount_args = [reporter[i + 1] for i, t in enumerate(reporter)
                      if t == "-v"]
        self.assertTrue(any("key.pem" in m for m in mount_args))
        bridge = ep.plan_e2e_container_create(
            "mcp-bridge", image_ref="sha256:ab")
        bridge_mounts = [bridge[i + 1] for i, t in enumerate(bridge)
                         if t == "-v"]
        self.assertFalse(any("key.pem" in m for m in bridge_mounts))

    def test_execute_setup_journals_and_connects(self):
        fd = _FakeDocker()
        journal = {}
        cid = ep.execute_e2e_container_setup(
            fd, "gh-proxy-r", image_ref="sha256:ab",
            container_journal=journal)
        self.assertEqual(cid, "ctr-id-123")
        self.assertEqual(journal["gh-proxy-r"], "ctr-id-123")
        connect_calls = [c for c in fd.calls
                         if c[:2] == ["network", "connect"]]
        self.assertEqual(len(connect_calls), 2)

    def test_connect_failure_cleans_up(self):
        fd = _FakeDocker(fail_on={"--gw-priority 0"})
        journal = {}
        with self.assertRaises(Exception):
            ep.execute_e2e_container_setup(
                fd, "gh-proxy-b", image_ref="sha256:ab",
                container_journal=journal)
        self.assertEqual(len(journal), 0)
        rm_calls = [c for c in fd.calls if c[0] == "rm"]
        self.assertTrue(len(rm_calls) > 0)


class TestProbes(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tdp = Path(self.tmpdir.name)
        # Create synthetic files
        for name, content in (
                ("room-map.yaml", 'repos:\n  "example/fixture":\n'
                 '    room_id: "!room:server"\n'),
                ("policy.yaml", 'repos:\n  allowlist:\n'
                 '    - "example/fixture"\n'),
                ("creds.json", '{"token": "fake"}'),
                ("app.pem", "-----BEGIN FAKE-----\n-----END FAKE-----\n"),
                ("whsec", "fake-secret"),
                ("pat", "fake-pat"),
                ("callback", "https://example.com/webhook")):
            p = self.tdp / name
            p.write_text(content, encoding="utf-8")
            try:
                p.chmod(0o600)
            except OSError:
                pass  # Windows: mode is metadata-only

    def tearDown(self):
        self.tmpdir.cleanup()

    def _receipt(self):
        receipt = {
            "schema_version": 1,
            "agents": [
                {"container_id": "id-%d" % i, "mxid": "@agent%d:srv" % i,
                 "hiclaw_net_ip": "172.21.0.%d" % (i + 2),
                 "gateway_url": "http://gw/%d/sse" % i,
                 "config_hash_before": "h%d" % i,
                 "config_hash_after": "h%d" % i,
                 "token_hash": "t%d" % i}
                for i in range(4)],
            "old_github_mcp": {"state": "stopped"},
            "rollback_ownership": "mp-gh4-harness",
        }
        path = self.tdp / "receipt.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        return str(path)

    def _config(self):
        return _valid_config(
            room_map_path=str(self.tdp / "room-map.yaml"),
            policy_path=str(self.tdp / "policy.yaml"),
            matrix_credentials_path=str(self.tdp / "creds.json"),
            app_pem_path=str(self.tdp / "app.pem"),
            webhook_secret_path=str(self.tdp / "whsec"),
            mcp_pat_path=str(self.tdp / "pat"),
            hiclaw_receipt_path=self._receipt(),
            callback_url_path=str(self.tdp / "callback"))

    def test_all_probes_pass(self):
        result = ep.run_prerequisite_probes(
            self._config(),
            matrix_joined_mxids=set(e2f.E2E_EXPECTED_ROOM_MEMBERS),
            docker_gw_priority_supported=True,
            existing_network_cidrs=["172.17.0.0/16"],
            firewall_scan_text="")
        self.assertTrue(result["verified"],
                        {k: v for k, v in result["checks"].items()
                         if not v["verified"]})
        self.assertEqual(len(result["checks"]), 16)

    def test_missing_room_map_fails(self):
        config = self._config()
        config["room_map_path"] = "/nonexistent/yaml"
        result = ep.run_prerequisite_probes(config)
        self.assertFalse(result["verified"])
        self.assertFalse(result["checks"]["room_map_file"]["verified"])

    def test_membership_missing_fails(self):
        result = ep.run_prerequisite_probes(
            self._config(), matrix_joined_mxids=set())
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["matrix_membership"]["code"],
            "MATRIX_MEMBERSHIP_INCOMPLETE")

    def test_gw_priority_not_injected_fails(self):
        result = ep.run_prerequisite_probes(self._config())
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["docker_gw_priority"]["code"],
            "PROBE_NOT_INJECTED")

    def test_subnet_overlap_fails(self):
        result = ep.run_prerequisite_probes(
            self._config(), existing_network_cidrs=["172.31.0.0/16"])
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["subnet_overlap"]["code"],
            "SUBNET_OVERLAP")

    def test_foreign_firewall_fails(self):
        result = ep.run_prerequisite_probes(
            self._config(),
            firewall_scan_text='-I DOCKER-USER ... --comment "mp-e2e:dead:jump"')
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["firewall_ownership"]["code"],
            "FIREWALL_CONFLICT")

    def test_receipt_invalid_fails(self):
        config = self._config()  # creates valid receipt first
        # NOW overwrite with invalid JSON (after config's _receipt call)
        path = self.tdp / "receipt.json"
        path.write_text("not json", encoding="utf-8")
        result = ep.run_prerequisite_probes(config)
        self.assertFalse(result["checks"]["hiclaw_receipt"]["verified"])

    def test_gate_raises_on_failure(self):
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            ep.run_e2e_prerequisite_gate(
                self._config(),
                matrix_joined_mxids=set())
        self.assertEqual(ctx.exception.code,
                         "GITHUB_E2E_PREREQUISITES_INCOMPLETE")

    def test_gate_passes_on_success(self):
        result = ep.run_e2e_prerequisite_gate(
            self._config(),
            matrix_joined_mxids=set(e2f.E2E_EXPECTED_ROOM_MEMBERS),
            docker_gw_priority_supported=True,
            existing_network_cidrs=[],
            firewall_scan_text="")
        self.assertTrue(result["verified"])

    def test_no_secret_in_output(self):
        result = ep.run_prerequisite_probes(self._config())
        blob = str(result)
        for forbidden in ("fake-secret", "fake-pat", "BEGIN FAKE",
                          "fake-secret"):
            self.assertNotIn(forbidden, blob)


class TestDisconnectNoneBeforeConnects(unittest.TestCase):
    """Real E2E start failed E2E_CONTAINER_SETUP_FAILED(controller):
    the daemon refuses a SECOND network connect while the private
    'none' endpoint is still attached. The executor must detach
    'none' before the connect sequence."""

    def test_disconnect_none_precedes_connects(self):
        calls = []

        def docker_exec(argv, check=True, timeout=60, **_):
            calls.append(list(argv))
            cp = _fake_ok()
            cp.returncode = 0
            return cp

        journal = {}
        cid = ep.execute_e2e_container_setup(
            docker_exec, "controller",
            image_ref="sha256:ab", env_file="/e", mounts=[],
            container_journal=journal)
        self.assertTrue(cid)
        # find the disconnect and the first connect
        disc = next(i for i, c in enumerate(calls)
                    if c[:3] == ["network", "disconnect", "none"])
        conn = next(i for i, c in enumerate(calls)
                    if c[:2] == ["network", "connect"])
        self.assertLess(disc, conn)
        # disconnect targets the production container name
        self.assertEqual(calls[disc],
                         ["network", "disconnect", "none",
                          "mergepilot-isolated-controller-1"])

    def test_disconnect_failure_removes_container(self):
        calls = []

        def docker_exec(argv, check=True, timeout=60, **_):
            calls.append(list(argv))
            cp = _fake_ok()
            cp.returncode = 0
            if argv[:3] == ["network", "disconnect", "none"]:
                cp.returncode = 1
                if check:
                    raise RuntimeError("disconnect rc=1")
            return cp

        journal = {}
        with self.assertRaises(Exception):
            ep.execute_e2e_container_setup(
                docker_exec, "controller", image_ref="sha256:ab",
                env_file="/e", mounts=[], container_journal=journal)
        self.assertEqual(journal, {})
        self.assertIn(["rm", "-f", "id"], calls)


def _fake_ok():
    import subprocess
    cp = subprocess.CompletedProcess([], 0, b"id", b"")
    return cp


if __name__ == "__main__":
    unittest.main()
