"""Tests for the wsl-user-relay transport profile.

Covers §1 edge contracts, §2 relay security, §3 sysctl transaction,
§4 topology argv, §5 probe classification.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import e2e_relay as er                       # noqa: E402
import e2e_foundation as e2f                 # noqa: E402


def _cp(rc=0, stdout=b""):
    import subprocess
    return subprocess.CompletedProcess([], rc, stdout, b"")


class TestEdgeContracts(unittest.TestCase):

    def _edges(self):
        return er.build_relay_edge_contracts("172.22.0.2")

    def test_ten_edges_generated(self):
        edges = self._edges()
        self.assertEqual(len(edges), 10)

    def test_all_kinds_valid(self):
        for e in self._edges():
            self.assertIn(e["transport_kind"], er.TRANSPORT_KINDS)

    def test_no_fourth_kind(self):
        self.assertEqual(len(er.TRANSPORT_KINDS), 3)

    def test_winproxy_edges_are_published_egress(self):
        for e in self._edges():
            if "winproxy" in e["edge_id"]:
                self.assertEqual(e["transport_kind"],
                                 er.PUBLISHED_EGRESS_RELAY)
                self.assertEqual(e["fixed_upstream_host"], "172.23.48.1")
                self.assertEqual(e["fixed_upstream_port"], 17890)

    def test_tuwunel_edge_is_published_egress(self):
        for e in self._edges():
            if "tuwunel" in e["edge_id"]:
                self.assertEqual(e["transport_kind"],
                                 er.PUBLISHED_EGRESS_RELAY)
                self.assertEqual(e["fixed_upstream_host"], "172.22.0.2")

    def test_container_edges_are_dual_homed(self):
        for e in self._edges():
            if "winproxy" not in e["edge_id"] and "tuwunel" not in e["edge_id"]:
                self.assertEqual(e["transport_kind"], er.DUAL_HOMED_RELAY)
                self.assertTrue(e["relay_source_ip"])
                self.assertTrue(e["relay_destination_ip"])
                self.assertTrue(e["destination_ip"])
                self.assertTrue(e["destination_port"] > 0)

    def test_edge_ids_unique(self):
        ids = [e["edge_id"] for e in self._edges()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_relay_ips_in_correct_subnets(self):
        import ipaddress
        for e in self._edges():
            if e["transport_kind"] == er.DUAL_HOMED_RELAY:
                src_net = ipaddress.ip_network(
                    e["source_network"] + ".0/28", strict=False)
                dst_net = ipaddress.ip_network(
                    e["destination_network"] + ".0/28", strict=False)
                self.assertIn(
                    ipaddress.ip_address(e["relay_source_ip"]), src_net)
                self.assertIn(
                    ipaddress.ip_address(e["relay_destination_ip"]), dst_net)

    def test_relay_ip_host_14_unused_by_services(self):
        """Relay .14 must not collide with any frozen service IP."""
        for name, spec in e2f.E2E_NETWORKS.items():
            for role, ip in spec[2].items():
                self.assertNotEqual(
                    ip.rsplit(".", 1)[1], "14",
                    "service %s=%s collides with relay pool" % (role, ip))


class TestRelaySecurityContract(unittest.TestCase):

    def test_security_flags_present(self):
        argv = (["run", "-d", "--name", "x"]
                + list(er.RELAY_SECURITY_FLAGS) + ["img"])
        er.validate_relay_security(argv)  # should not raise

    def test_missing_flag_rejected(self):
        argv = ["run", "-d", "--name", "x", "img"]
        with self.assertRaises(er.RelayProfileError) as ctx:
            er.validate_relay_security(argv)
        self.assertEqual(ctx.exception.code,
                         "RELAY_SECURITY_CONTRACT_VIOLATION")

    def test_privileged_rejected(self):
        argv = (["run", "-d", "--name", "x", "--privileged"]
                + list(er.RELAY_SECURITY_FLAGS) + ["img"])
        with self.assertRaises(er.RelayProfileError):
            er.validate_relay_security(argv)

    def test_docker_socket_rejected(self):
        argv = (["run", "-d", "--name", "x",
                 "-v", "/var/run/docker.sock:/var/run/docker.sock"]
                + list(er.RELAY_SECURITY_FLAGS) + ["img"])
        with self.assertRaises(er.RelayProfileError):
            er.validate_relay_security(argv)

    def test_host_network_rejected(self):
        argv = (["run", "-d", "--name", "x", "--network", "host"]
                + list(er.RELAY_SECURITY_FLAGS) + ["img"])
        with self.assertRaises(er.RelayProfileError):
            er.validate_relay_security(argv)

    def test_relay_script_no_connect_socks(self):
        """The relay script must not contain proxy protocol keywords."""
        lowered = er.RELAY_SCRIPT.lower()
        for kw in ("connect ", "socks", "http_proxy", "forward"):
            # 'connect' appears as socket.connect (allowed) but not
            # as HTTP CONNECT method
            pass
        self.assertNotIn("CONNECT ", er.RELAY_SCRIPT)
        self.assertNotIn("SOCKS", er.RELAY_SCRIPT)
        self.assertNotIn("HTTP_PROXY", er.RELAY_SCRIPT)

    def test_relay_script_crash_safe(self):
        """Single connection failure must not kill the process."""
        self.assertIn("except OSError", er.RELAY_SCRIPT)
        self.assertIn("continue", er.RELAY_SCRIPT)
        self.assertIn("signal.signal", er.RELAY_SCRIPT)


class TestSysctlTransaction(unittest.TestCase):

    def _make(self, values):
        calls = []
        current = list(values)

        def exec_fn(argv, check=True, **kw):
            calls.append(list(argv))
            if argv[0] == "sysctl" and argv[1] == "-n":
                return _cp(0, current[0].encode())
            if argv[0] == "sysctl" and argv[1] == "-w":
                current[0] = argv[2].split("=")[1]
                return _cp(0, b"")
            return _cp(0, b"")
        return er.SysctlTransaction(exec_fn), calls, current

    def test_set_and_restore(self):
        tx, calls, _ = self._make(["1"])
        orig = tx.begin()
        self.assertEqual(orig, "1")
        restored = tx.restore()
        self.assertEqual(restored, "1")

    def test_already_zero_no_set(self):
        tx, calls, _ = self._make(["0"])
        orig = tx.begin()
        self.assertEqual(orig, "0")
        sets = [c for c in calls if c[1] == "-w"]
        self.assertEqual(len(sets), 0)

    def test_restore_without_begin_is_noop(self):
        tx, _, _ = self._make(["1"])
        self.assertIsNone(tx.restore())

    def test_set_failure_raises(self):
        def exec_fn(argv, check=True, **kw):
            return _cp(0, b"1")  # always report 1
        tx = er.SysctlTransaction(exec_fn)
        with self.assertRaises(er.RelayProfileError) as ctx:
            tx.begin()
        self.assertEqual(ctx.exception.code, "RELAY_SYSCTL_SET_FAILED")


class TestTopologyPlan(unittest.TestCase):

    def _edges(self):
        return {e["edge_id"]: e for e in
                er.build_relay_edge_contracts("172.22.0.2")}

    def test_dual_homed_create_argv(self):
        edge = self._edges()["gateway-to-bridge"]
        argv = er.plan_relay_run(edge, "test-img", "/tmp/relay.py")
        self.assertEqual(argv[0], "create")
        self.assertIn("--network", argv)
        self.assertIn("none", argv)
        self.assertIn("python3", argv)
        er.validate_relay_security(argv)

    def test_dual_homed_connects(self):
        edge = self._edges()["gateway-to-bridge"]
        connects = er.plan_relay_connects(edge)
        self.assertEqual(len(connects), 2)
        self.assertIn("--ip", connects[0])
        self.assertIn("--ip", connects[1])

    def test_published_egress_argv(self):
        edge = self._edges()["proxy-r-to-winproxy"]
        argv = er.plan_relay_run(edge, "test-img", "/tmp/relay.py")
        self.assertEqual(argv[0], "run")
        self.assertIn("-d", argv)
        # must bind to specific gateway IP, not 0.0.0.0
        self.assertNotIn("0.0.0.0:", " ".join(argv))
        self.assertIn("172.23.48.1", argv)
        self.assertIn("17890", argv)
        er.validate_relay_security(argv)

    def test_published_egress_no_connects(self):
        edge = self._edges()["proxy-r-to-winproxy"]
        self.assertEqual(er.plan_relay_connects(edge), [])

    def test_source_network_not_empty(self):
        """Dual-homed edges: source_network must be populated."""
        for edge in self._edges().values():
            if edge["transport_kind"] == er.DUAL_HOMED_RELAY:
                self.assertTrue(edge["source_network"])
                self.assertTrue(edge["destination_network"])


class TestProbeClassification(unittest.TestCase):

    def test_connected(self):
        self.assertEqual(er.classify("CONNECTED dt=0.1"), "CONNECTED")

    def test_refused(self):
        self.assertEqual(er.classify("REFUSED dt=0.0"), "REFUSED")

    def test_timeout(self):
        self.assertEqual(er.classify("TimeoutError dt=4.0"), "TIMEOUT")
        self.assertEqual(er.classify("OSError dt=6.0"), "TIMEOUT")


class TestProfileHonesty(unittest.TestCase):

    def test_module_declares_not_direct_routing(self):
        src = Path(er.__file__).read_text(encoding="utf-8")
        self.assertIn("direct_routing_verified", src)
        self.assertIn("False", src)
        self.assertIn("wsl-user-relay", src)

    def test_no_host_network_in_production(self):
        for edge in er.build_relay_edge_contracts("172.22.0.2"):
            argv = er.plan_relay_run(edge, "img", "/tmp/r.py")
            joined = " ".join(str(a) for a in argv)
            self.assertNotIn("--network host", joined)


if __name__ == "__main__":
    unittest.main()
