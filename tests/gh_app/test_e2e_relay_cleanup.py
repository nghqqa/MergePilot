"""E2E relay cleanup ownership tests (maintenance round §2).

Covers the run35 finding: `stop` cleaned only the default DAG
resources and left the relay system (units / aliases / INPUT rules /
sysctl) unowned. All cleanup here runs against fake executors that
record every call — the tests assert EXACT journal-driven argv,
idempotency, the three stable ownership error codes, and that
foreign relay-shaped resources are reported, never guessed-deleted.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli")):
    if p not in sys.path:
        sys.path.insert(0, p)

import e2e_lifecycle as el                      # noqa: E402


class _CP:
    def __init__(self, rc=0, stdout=b""):
        self.returncode = rc
        self.stdout = stdout


def _text(value):
    return value.encode("utf-8") if isinstance(value, str) else value


class FakeDocker:
    """Records every argv; answers the inspect/ps shapes the stop
    path and residue scan need."""

    def __init__(self, containers=(), networks=()):
        # containers: dict name -> present(bool)
        self.containers = {n: True for n in containers}
        self.networks = set(networks)
        self.calls = []

    def __call__(self, argv, check=True, timeout=60, **_):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if argv[:2] == ["ps", "-a"]:
            return _CP(0, _text("\n".join(
                n for n, present in self.containers.items() if present)))
        if argv[0] == "inspect" and "--format" in argv:
            fmt = argv[argv.index("--format") + 1]
            name = argv[1]
            if "Names" in joined and "ps" not in joined:
                pass
            if "{{.Id}}" in fmt:
                if self.containers.get(name):
                    return _CP(0, _text("sha256:" + "a" * 64))
                return _CP(1, b"no such object")
            if argv[1] == "network" or (
                    len(argv) > 2 and argv[1] in self.networks):
                pass
            return _CP(0, b"")
        if argv[:2] == ["network", "inspect"]:
            net = argv[2]
            if net in self.networks:
                return _CP(0, _text("netid123"))
            return _CP(1, b"no such network")
        if argv[0] == "rm":
            for name in argv[2:]:
                self.containers[name] = False
            return _CP(0, b"")
        return _CP(0, b"")


class FakeHost:
    def __init__(self, *, iptables_save="", units_text="",
                 addr_text="", rule_check_rc=1, sysctl_value="1"):
        self.iptables_save = iptables_save
        self.units_text = units_text
        self.addr_text = addr_text
        self.rule_check_rc = rule_check_rc
        self.sysctl_value = sysctl_value
        self.deleted_rules = []
        self.calls = []

    def __call__(self, argv, check=True, timeout=60, **_):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if joined.startswith("iptables-save"):
            return _CP(0, _text(self.iptables_save))
        if joined.startswith("iptables -C"):
            return _CP(self.rule_check_rc, b"")
        if joined.startswith("iptables -D"):
            self.deleted_rules.append(list(argv))
            return _CP(0, b"")
        if joined.startswith("systemctl list-units"):
            return _CP(0, _text(self.units_text))
        if joined.startswith("systemctl is-active"):
            unit = argv[-1]
            return _CP(0, _text("inactive"))
        if joined.startswith("ip -4 addr show"):
            return _CP(0, _text(self.addr_text))
        if joined.startswith("ip addr show"):
            return _CP(0, _text(self.addr_text))
        if joined.startswith("sysctl -n"):
            return _CP(0, _text(self.sysctl_value))
        return _CP(0, b"")


def _relay_session():
    return {
        "run_id": "b8-e2e-runX",
        "e2e_container_ids": {},
        "e2e_started": [],
        "e2e_network_ids": {},
        "default_network_ids": {},
        "e2e_runtime_journal": {},
        "relay_containers": ["mp-e2e-relay-gateway-bridge",
                             "mp-e2e-relay-reporter-proxy-r"],
        "relay_networks": ["mp-e2e-br-up", "mp-e2e-rpt-egress"],
        "relay_probe_containers": ["mp-e2e-relay-probe-gateway-bridge"],
        "relay_host_units": ["mp-e2e-host-relay-controller-tuwunel"
                             ".service"],
        "relay_host_aliases": [{"ip": "172.31.0.12",
                                "bridge": "br-abc123"}],
        "relay_input_rules": [{"source_cidr": "172.31.0.128/28",
                               "listen_ip": "172.31.0.140",
                               "port": 6167}],
        "relay_sysctl_before": "1",
    }


class TestStopRelayCleanup(unittest.TestCase):

    def test_stop_cleans_every_journal_category_exact_argv(self):
        fd = FakeDocker(containers=(
            "mp-e2e-relay-gateway-bridge",
            "mp-e2e-relay-reporter-proxy-r",
            "mp-e2e-relay-probe-gateway-bridge"))
        fh = FakeHost()
        session = _relay_session()
        result = el.run_e2e_stop(docker_executor=fd,
                                 host_executor=fh, session=session)
        actions = " ".join(result["actions"])
        for expected in (
                "relay_input_rule:172.31.0.140:6167",
                "relay_unit:mp-e2e-host-relay-controller-tuwunel"
                ".service",
                "relay_alias:172.31.0.12",
                "relay_probe:mp-e2e-relay-probe-gateway-bridge",
                "relay_container:mp-e2e-relay-gateway-bridge"):
            self.assertIn(expected, actions)
        # exact -D argv from the journal triple (no glob guessing)
        self.assertIn(
            ["iptables", "-D", "INPUT", "-s", "172.31.0.128/28",
             "-d", "172.31.0.140", "-p", "tcp", "--dport", "6167",
             "-j", "ACCEPT"],
            fh.deleted_rules)
        # sysctl restored to the journaled BEFORE value
        self.assertTrue(any(
            c[:3] == ["sysctl", "-w",
                      "net.bridge.bridge-nf-call-iptables=1"]
            for c in fh.calls))
        # journal fields consumed
        for key in ("relay_containers", "relay_networks",
                    "relay_probe_containers", "relay_host_units",
                    "relay_host_aliases", "relay_input_rules",
                    "relay_sysctl_before"):
            self.assertNotIn(key, session)
        self.assertEqual(result["diagnostics"], [])

    def test_stop_relay_cleanup_is_idempotent(self):
        fd = FakeDocker()
        fh = FakeHost()
        session = _relay_session()
        el.run_e2e_stop(docker_executor=fd, host_executor=fh,
                        session=session)
        relay_rm_calls = [c for c in fd.calls
                          if c[:2] == ["rm", "-f"]
                          and "relay" in " ".join(c)]
        self.assertEqual(len(relay_rm_calls), 3)
        # second stop: journal consumed → zero relay actions
        result2 = el.run_e2e_stop(docker_executor=fd,
                                  host_executor=fh, session=session)
        self.assertFalse([a for a in result2["actions"]
                          if a.startswith("relay_")])
        self.assertEqual(result2["diagnostics"], [])

    def test_incomplete_codes_when_resources_survive(self):
        # docker rm "succeeds" but the container stays (fake refuses
        # to mark removed); unit stays active; sysctl never restores
        fd = FakeDocker(containers=("mp-e2e-relay-gateway-bridge",))

        class StubbornDocker(FakeDocker):
            def __call__(self, argv, **kw):
                if argv[0] == "rm":
                    self.calls.append(list(argv))
                    return _CP(0, b"")   # claims success, keeps state
                return super().__call__(argv, **kw)

        fd = StubbornDocker(containers=(
            "mp-e2e-relay-gateway-bridge",
            "mp-e2e-relay-probe-gateway-bridge"))

        class StubbornHost(FakeHost):
            def __call__(self, argv, **kw):
                if " ".join(argv).startswith("systemctl is-active"):
                    self.calls.append(list(argv))
                    return _CP(0, b"active")
                if " ".join(argv).startswith("sysctl -w"):
                    self.calls.append(list(argv))
                    return _CP(0, b"")   # accepted but no effect
                return super().__call__(argv, **kw)

        fh = StubbornHost(sysctl_value="0")  # never restored
        session = _relay_session()
        result = el.run_e2e_stop(docker_executor=fd,
                                 host_executor=fh, session=session)
        joined = " ".join(result["diagnostics"])
        self.assertIn("E2E_RELAY_CLEANUP_INCOMPLETE:unit:", joined)
        self.assertIn("E2E_RELAY_CLEANUP_INCOMPLETE:container:", joined)
        self.assertIn("E2E_RELAY_CLEANUP_INCOMPLETE:probe:", joined)
        self.assertIn("E2E_RELAY_CLEANUP_INCOMPLETE:sysctl:", joined)

    def test_uncertain_codes_for_rules_and_aliases(self):
        fh = FakeHost(rule_check_rc=0,          # -C says rule still there
                      addr_text="inet 172.31.0.12/32 scope global")
        fd = FakeDocker()
        session = _relay_session()
        result = el.run_e2e_stop(docker_executor=fd,
                                 host_executor=fh, session=session)
        joined = " ".join(result["diagnostics"])
        self.assertIn("E2E_RELAY_RESOURCE_UNCERTAIN:input_rule:", joined)
        self.assertIn("E2E_RELAY_RESOURCE_UNCERTAIN:alias:", joined)

    def test_foreign_relay_resources_reported_never_deleted(self):
        # residue scan sees relay-shaped resources the journal does
        # NOT own → OWNERSHIP_MISSING in diagnostics, and no rm/stop
        # call may target them
        foreign_container = "mp-e2e-relay-gateway-bridge"
        fd = FakeDocker(containers=(foreign_container,))
        fh = FakeHost(
            units_text="mp-e2e-host-relay-foreign.service loaded "
                       "active running x\n",
            addr_text="inet 172.31.0.99/32 scope global br-xyz\n",
            iptables_save=(
                "-A INPUT -s 172.31.0.128/28 -d 172.31.0.140 "
                "-p tcp --dport 6167 -j ACCEPT\n"))
        session = _relay_session()
        # journal owns NOTHING relay-shaped here
        for key in ("relay_containers", "relay_networks",
                    "relay_probe_containers", "relay_host_units",
                    "relay_host_aliases", "relay_input_rules",
                    "relay_sysctl_before"):
            session.pop(key, None)
        result = el.run_e2e_stop(docker_executor=fd,
                                 host_executor=fh, session=session)
        joined = " ".join(result["diagnostics"])
        self.assertIn("E2E_RELAY_OWNERSHIP_MISSING", joined)
        self.assertIn(foreign_container, joined)
        # never guessed-deleted: no docker rm touched the foreign name
        self.assertFalse(any(
            c[:2] == ["rm", "-f"] and foreign_container in c
            for c in fd.calls))
        # residue list carries the entries for the exit-code mapping
        self.assertTrue(any(
            r.startswith("relay_container:") for r in result["residue"]))

    def test_no_glob_or_prefix_deletion_ever(self):
        fd = FakeDocker()
        fh = FakeHost()
        session = _relay_session()
        el.run_e2e_stop(docker_executor=fd, host_executor=fh,
                        session=session)
        # read-only listings may legitimately glob; DELETION verbs
        # must be journal-exact
        destructive_verbs = (
            "rm", "stop", "reset-failed", "del", "-D")
        for call in fd.calls + fh.calls:
            if not call:
                continue
            if call[0] in destructive_verbs or (
                    call[0] == "iptables" and " -D" in " ".join(call)):
                joined = " ".join(call)
                self.assertNotIn("*", joined, call)


class TestJournalFirstProbeOwnership(unittest.TestCase):

    def test_probe_names_journaled_before_probe_call(self):
        # source-contract guard: relay_probe_containers is written
        # BEFORE the _run_relay_probes CALL SITE and popped after
        src = (ROOT / "tools" / "cli" / "e2e_lifecycle.py").read_text(
            encoding="utf-8")
        set_at = src.index('session["relay_probe_containers"]')
        call_at = src.index("route_result = _run_relay_probes(")
        pop_at = src.index('session.pop("relay_probe_containers"')
        self.assertLess(set_at, call_at)
        self.assertLess(call_at, pop_at)


if __name__ == "__main__":
    unittest.main()
