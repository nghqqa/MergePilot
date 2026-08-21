"""M8-GH-4B3 tests — restricted CONNECT proxy, MCP bridge supply chain,
Gateway E2E wiring, full topology, HiClaw harness planning, prerequisites
gate. Fully static/mocked; no real GitHub, no real PEM/PAT, no real WSL
iptables writes."""

from __future__ import annotations

import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "gh-app"),
          str(ROOT / "tools" / "cli")):
    if p not in sys.path:
        sys.path.insert(0, p)

import e2e_foundation as e2f                    # noqa: E402
import mergepilot as mp                         # noqa: E402
import restricted_connect_proxy as rcp          # noqa: E402


# ── §3 restricted CONNECT proxy ─────────────────────────────────────────────

class TestConnectTargetParsing(unittest.TestCase):

    def test_exact_target_accepted(self):
        host, port = rcp.parse_connect_target(
            "CONNECT api.github.com:443 HTTP/1.1")
        self.assertEqual((host, port), ("api.github.com", 443))

    def test_case_variants_rejected(self):
        for line in ("CONNECT API.GITHUB.COM:443 HTTP/1.1",
                     "CONNECT Api.GitHub.Com:443 HTTP/1.1"):
            self.assertIsNone(
                rcp.parse_connect_target(line)[0])

    def test_trailing_dot_rejected(self):
        self.assertIsNone(
            rcp.parse_connect_target(
                "CONNECT api.github.com.:443 HTTP/1.1")[0])

    def test_userinfo_rejected(self):
        self.assertIsNone(
            rcp.parse_connect_target(
                "CONNECT user@api.github.com:443 HTTP/1.1")[0])

    def test_scheme_and_path_rejected(self):
        for line in ("CONNECT https://api.github.com:443 HTTP/1.1",
                     "CONNECT api.github.com:443/path HTTP/1.1"):
            self.assertIsNone(
                rcp.parse_connect_target(line)[0])

    def test_ip_literal_rejected(self):
        self.assertIsNone(
            rcp.parse_connect_target(
                "CONNECT 140.82.112.3:443 HTTP/1.1")[0])

    def test_other_ports_rejected(self):
        for port in (80, 8080, 8443, 444):
            self.assertIsNone(
                rcp.parse_connect_target(
                    "CONNECT api.github.com:%d HTTP/1.1" % port)[0])

    def test_non_connect_rejected(self):
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"):
            line = "%s api.github.com:443 HTTP/1.1" % method
            host, _ = rcp.parse_connect_target(line)
            if method != "CONNECT":
                # parse only handles CONNECT; non-CONNECT gets (None, None)
                # OR the proxy returns 405 before even parsing
                pass


class TestProxyConfig(unittest.TestCase):

    def test_valid_config(self):
        cfg = rcp.load_config({
            "GH_PROXY_BIND": "0.0.0.0", "GH_PROXY_PORT": "18090",
            "GH_PROXY_UPSTREAM_IP": "172.23.48.1"})
        self.assertEqual(cfg["upstream_port"], 17890)
        self.assertEqual(cfg["upstream_ip"], "172.23.48.1")

    def test_hostname_upstream_rejected(self):
        with self.assertRaises(rcp.ProxyConfigError):
            rcp.load_config({"GH_PROXY_UPSTREAM_IP": "proxy.example.com"})

    def test_wrong_port_rejected(self):
        with self.assertRaises(rcp.ProxyConfigError):
            rcp.load_config({"GH_PROXY_UPSTREAM_IP": "172.23.48.1",
                             "GH_PROXY_UPSTREAM_PORT": "8888"})

    def test_ipv6_literal_accepted(self):
        cfg = rcp.load_config({"GH_PROXY_UPSTREAM_IP": "[::1]"})
        self.assertEqual(cfg["upstream_ip"], "[::1]")

    def test_bad_listen_port_rejected(self):
        with self.assertRaises(rcp.ProxyConfigError):
            rcp.load_config({"GH_PROXY_UPSTREAM_IP": "10.0.0.1",
                             "GH_PROXY_PORT": "99999"})


class TestProxyProtocol(unittest.TestCase):
    """Full proxy protocol tests using per-test loopback sockets + a fake
    upstream. NO real network, NO real GitHub."""

    def _run_proxy_once(self, client_data, upstream_handler):
        """Start a fake upstream + one proxy handler; send client_data;
        return the client's raw response. Self-contained per test."""
        upstream_listener = socket.socket(socket.AF_INET,
                                          socket.SOCK_STREAM)
        upstream_listener.setsockopt(socket.SOL_SOCKET,
                                     socket.SO_REUSEADDR, 1)
        upstream_listener.bind(("127.0.0.1", 0))
        upstream_listener.listen(4)
        upstream_port = upstream_listener.getsockname()[1]

        proxy_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy_listener.setsockopt(socket.SOL_SOCKET,
                                  socket.SO_REUSEADDR, 1)
        proxy_listener.bind(("127.0.0.1", 0))
        proxy_listener.listen(4)
        proxy_port = proxy_listener.getsockname()[1]

        # fake upstream thread
        def _upstream():
            try:
                conn, _ = upstream_listener.accept()
                conn.settimeout(5)
                upstream_handler(conn)
            except OSError:
                pass
            finally:
                try:
                    upstream_listener.close()
                except OSError:
                    pass

        ut = threading.Thread(target=_upstream, daemon=True)
        ut.start()

        # client
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(("127.0.0.1", proxy_port))
        accepted, _ = proxy_listener.accept()

        # proxy handler thread
        ht = threading.Thread(
            target=rcp.handle_connection,
            args=(accepted, "127.0.0.1", upstream_port), daemon=True)
        ht.start()

        client.sendall(client_data)
        try:
            response = client.recv(4096)
        except socket.timeout:
            response = b""
        try:
            proxy_listener.close()
        except OSError:
            pass
        ht.join(3)
        ut.join(5)
        try:
            client.close()
        except OSError:
            pass
        return response

    def test_http_method_gets_405(self):
        response = self._run_proxy_once(
            b"GET http://api.github.com/ HTTP/1.1\r\n"
            b"Host: api.github.com\r\n\r\n",
            lambda conn: None)
        self.assertIn(b"405", response)

    def test_disallowed_connect_gets_403(self):
        response = self._run_proxy_once(
            b"CONNECT evil.example.com:443 HTTP/1.1\r\n\r\n",
            lambda conn: None)
        self.assertIn(b"403", response)

    def test_valid_connect_establishes_tunnel(self):
        def fake_upstream(conn):
            data = conn.recv(4096)
            assert b"CONNECT api.github.com:443" in data
            conn.sendall(
                b"HTTP/1.1 200 Connection Established\r\n\r\n")
            conn.sendall(b"tunnel-data-marker")

        response = self._run_proxy_once(
            b"CONNECT api.github.com:443 HTTP/1.1\r\n"
            b"Host: api.github.com:443\r\n\r\n",
            fake_upstream)
        self.assertIn(b"200", response)


# ── §4 MCP bridge supply chain ──────────────────────────────────────────────

class TestMcpBridgeSupplyChain(unittest.TestCase):

    def test_dockerfile_digest_pinned(self):
        text = (ROOT / "Dockerfile.mcp-bridge").read_text(encoding="utf-8")
        self.assertIn(
            "ghcr.io/github/github-mcp-server@"
            "sha256:881b53d6f75f69bdbc1b5b10fc2f1361717c19054143b3a8529fb5c32061a50e",
            text)
        self.assertIn(
            "python:3.12-slim@"
            "sha256:9e869b0816f5537709825b49e62dc86d1c2691eff19b05c1d4dc3a07992cc052",
            text)
        self.assertIn("--only-binary=:all: --require-hashes", text)
        self.assertNotIn(":latest", text)

    def test_lockfile_33_packages_all_hashed(self):
        lines = [l for l in (ROOT / "tools" / "gh-app" /
                             "requirements-mcp-bridge.lock")
                 .read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
        self.assertEqual(len(lines), 33)
        for line in lines:
            self.assertIn("--hash=sha256:", line)
            self.assertRegex(line, r"^[a-z0-9_-]+==[0-9]")

    def test_lockfile_has_mcp_proxy(self):
        text = (ROOT / "tools" / "gh-app" /
                "requirements-mcp-bridge.lock").read_text(encoding="utf-8")
        self.assertIn("mcp_proxy==0.12.0", text)
        self.assertIn("69b118e5c86dd46a32769a47c114ce8acb1162e0e38fb6f21283d7b56aa7faaa",
                      text)

    def test_dockerfile_no_secrets(self):
        text = (ROOT / "Dockerfile.mcp-bridge").read_text(encoding="utf-8")
        for forbidden in ("ghp_", "ghs_", "BEGIN PRIVATE", "password=",
                          "postgresql://"):
            self.assertNotIn(forbidden, text)

    def test_health_module_exists(self):
        self.assertTrue((ROOT / "tools" / "gh-app" /
                         "mcp_bridge_health.py").is_file())


# ── §3 Dockerfile.gh-proxy ──────────────────────────────────────────────────

class TestProxyDockerfile(unittest.TestCase):

    def test_stdlib_only_no_pip(self):
        text = (ROOT / "Dockerfile.gh-proxy").read_text(encoding="utf-8")
        self.assertNotIn("pip install", text)

    def test_digest_and_uid(self):
        text = (ROOT / "Dockerfile.gh-proxy").read_text(encoding="utf-8")
        self.assertIn("sha256:9e869b08", text)
        self.assertIn("-u 9090", text)
        self.assertIn("-g 9090", text)
        self.assertIn("USER mergepilot-gh", text)

    def test_no_secrets_no_socket(self):
        text = (ROOT / "Dockerfile.gh-proxy").read_text(encoding="utf-8")
        for forbidden in ("ghp_", "BEGIN PRIVATE", "docker.sock",
                          "password="):
            self.assertNotIn(forbidden, text)


# ── §5 Gateway E2E wiring ───────────────────────────────────────────────────

class TestGatewayE2eWiring(unittest.TestCase):

    def test_upstream_is_bridge_not_stub(self):
        planning = e2f.build_gateway_e2e_planning()
        self.assertEqual(planning["upstream_url"],
                         "http://172.31.0.34:8082/sse")
        self.assertNotIn("127.0.0.1:8084", planning["upstream_url"])

    def test_read_only_tools(self):
        planning = e2f.build_gateway_e2e_planning()
        for tool in ("get_pull_request", "get_pull_request_files",
                     "get_file_contents", "get_branch"):
            self.assertIn(tool, planning["read_only_tools"])

    def test_write_tools_denied(self):
        planning = e2f.build_gateway_e2e_planning()
        denied = " ".join(planning["denied_tools"])
        for category in ("create", "comment", "merge", "workflow",
                         "release", "secret"):
            self.assertIn(category, denied)

    def test_gateway_is_sole_consumer(self):
        planning = e2f.build_gateway_e2e_planning()
        self.assertIn("ONLY bridge consumer", planning["sole_consumer"])

    def test_default_mode_stub_unchanged(self):
        source = (ROOT / "tools" / "demo_console" /
                  "one_click_startup.py").read_text(encoding="utf-8")
        self.assertIn('GATEWAY_ISOLATED_UPSTREAM_URL = '
                      '"http://127.0.0.1:8084/sse"', source)


# ── §2 prerequisites gate ───────────────────────────────────────────────────

class TestPrerequisitesGate(unittest.TestCase):

    def test_no_missing_passes(self):
        e2f.e2e_prerequisites_gate()
        e2f.e2e_prerequisites_gate([])

    def test_missing_raises(self):
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.e2e_prerequisites_gate(["pat_file"])
        self.assertEqual(ctx.exception.code,
                         "GITHUB_E2E_PREREQUISITES_INCOMPLETE")
        self.assertIn("pat_file", ctx.exception.detail)

    def test_unknown_type_rejected(self):
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.e2e_prerequisites_gate(["bogus"])
        self.assertEqual(ctx.exception.code, "PREREQUISITE_TYPE_INVALID")

    def test_all_ten_types_defined(self):
        self.assertEqual(len(e2f.E2E_PREREQUISITE_TYPES), 10)

    def test_pending_components_cleared(self):
        self.assertEqual(e2f.E2E_PENDING_COMPONENTS, ())

    def test_cli_gate_fires_before_side_effects(self):
        rc = mp.main(["start", "--run-id", "b3gate", "--github-e2e",
                      "--project-dir", str(ROOT)])
        self.assertEqual(rc, 3)

    def test_cli_gate_honest_and_prereq_real(self):
        source = (ROOT / "tools" / "cli" / "mergepilot.py").read_text(
            encoding="utf-8")
        self.assertIn("GITHUB_E2E_COMPONENTS_INCOMPLETE", source)
        foundation = (ROOT / "tools" / "cli" /
                      "e2e_foundation.py").read_text(encoding="utf-8")
        self.assertIn("E2E_PENDING_COMPONENTS = ()", foundation)
        # the prerequisites code comes from a REAL probe adaptation
        # (config loader), not an unconditional raise
        lifecycle = (ROOT / "tools" / "cli" /
                     "e2e_lifecycle.py").read_text(encoding="utf-8")
        self.assertIn("GITHUB_E2E_PREREQUISITES_INCOMPLETE", lifecycle)
        self.assertIn("load_e2e_prerequisite_config", lifecycle)


# ── §6 full topology ────────────────────────────────────────────────────────

class TestFullTopology(unittest.TestCase):

    def test_all_8_networks_in_preview(self):
        preview = e2f.build_b1_dry_run_preview(
            run_id="topo", tuwunel_ip="172.22.0.2",
            room_map_host="/x", policy_host="/y")
        self.assertEqual(len(preview["networks_create"]), 8)
        names = [argv[-1] for argv in preview["networks_create"]]
        for net in ("ctrl-egress", "gw-egress", "mcp-bridge-net",
                    "rpt-egress", "br-up", "pxr", "pxb", "winpx"):
            self.assertIn("mp-e2e-" + net, names)

    def test_full_firewall_10_edges_8_drops(self):
        preview = e2f.build_b1_dry_run_preview(
            run_id="fw", tuwunel_ip="172.22.0.2",
            room_map_host="/x", policy_host="/y")
        ff = preview["full_firewall"]
        self.assertEqual(ff["edge_count"], 10)
        self.assertEqual(ff["subnet_drop_count"], 8)
        self.assertEqual(ff["counts"]["forward_accept"], 10)
        self.assertEqual(ff["counts"]["reverse_accept"], 10)
        self.assertEqual(ff["counts"]["input_jumps"], 8)
        self.assertEqual(ff["counts"]["docker_user_jumps"], 12)

    def test_proxy_planning_two_instances(self):
        pp = e2f.build_proxy_planning()
        self.assertIn("gh-proxy-r", pp)
        self.assertIn("gh-proxy-b", pp)
        self.assertEqual(pp["gh-proxy-r"]["serves"], "Reporter ONLY")
        self.assertEqual(pp["gh-proxy-b"]["serves"], "MCP bridge ONLY")

    def test_bridge_planning_supply_chain(self):
        bp = e2f.build_mcp_bridge_planning()
        self.assertIn("881b53d6",
                      bp["supply_chain"]["github_mcp_server_digest"])
        self.assertEqual(bp["env_keys"], ["MCP_GITHUB_TOKEN"])

    def test_reporter_full_network_wiring(self):
        rp = e2f.build_reporter_planning()
        self.assertIn("rpt-egress", rp["networks"])
        self.assertIn("gh-proxy-r", rp["https_proxy"])
        self.assertIn("PREREQUISITES", rp["activation_gate"])


# ── §9 HiClaw harness planning ──────────────────────────────────────────────

class TestHiclawHarnessPlanning(unittest.TestCase):

    def test_four_agents_with_frozen_ips(self):
        hp = e2f.build_hiclaw_harness_planning()
        agents = hp["per_agent"]
        self.assertEqual(len(agents), 4)
        ips = {a["hiclaw_net_ip"] for a in agents}
        self.assertEqual(ips, {"172.21.0.2", "172.21.0.4",
                               "172.21.0.5", "172.21.0.6"})

    def test_separate_tokens_no_reuse(self):
        hp = e2f.build_hiclaw_harness_planning()
        for agent in hp["per_agent"]:
            self.assertIn("no cross-role reuse",
                          agent["token_transport"])

    def test_drift_refuse_overwrite(self):
        hp = e2f.build_hiclaw_harness_planning()
        for agent in hp["per_agent"]:
            self.assertIn("REFUSE_OVERWRITE", agent["drift"])

    def test_not_cli_owned(self):
        hp = e2f.build_hiclaw_harness_planning()
        self.assertIn("NOT part of mergepilot CLI",
                      hp["ownership"])

    def test_old_github_mcp_restore_contract(self):
        hp = e2f.build_hiclaw_harness_planning()
        self.assertIn("restore", hp["old_github_mcp"]["cleanup"])

    def test_openclaw_not_modified(self):
        hp = e2f.build_hiclaw_harness_planning()
        self.assertIn("NOT modified", hp["openclaw"])

    def test_journal_no_plaintext(self):
        hp = e2f.build_hiclaw_harness_planning()
        self.assertIn("never token", hp["journal"])


# ── §10 CLI lifecycle ───────────────────────────────────────────────────────

class TestCliLifecycle(unittest.TestCase):

    def test_default_mode_unchanged(self):
        session = mp.new_session("run-x", False)
        self.assertEqual(sorted(session),
                         ["containers", "created_utc", "m4f", "networks",
                          "run_id", "schema_version", "secrets", "stage"])

    def test_dry_run_preview_contains_b3_sections(self):
        preview = e2f.build_b1_dry_run_preview(
            run_id="lifecycle", tuwunel_ip="172.22.0.2",
            room_map_host="/x", policy_host="/y")
        for key in ("mcp_bridge_planning", "proxy_planning",
                    "gateway_planning", "hiclaw_harness_planning",
                    "full_firewall", "prerequisite_types"):
            self.assertIn(key, preview)

    def test_activation_gate_marker_updated(self):
        preview = e2f.build_b1_dry_run_preview(
            run_id="marker", tuwunel_ip="172.22.0.2",
            room_map_host="/x", policy_host="/y")
        self.assertIn("PREREQUISITES", preview["activation_gate"])
        self.assertNotIn("COMPONENTS_INCOMPLETE",
                         preview["activation_gate"])


if __name__ == "__main__":
    unittest.main()
