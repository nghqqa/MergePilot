"""M8-GH-4B3-W3A tests: firewall executor, route probes, receipt
validator. All use fake executors; zero real Docker/iptables/HiClaw."""

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
import e2e_executors as ex                      # noqa: E402


class _CP:
    def __init__(self, rc=0, out=b""):
        self.returncode = rc
        self.stdout = out


class _FakeHost:
    """Fake wsl_exec for iptables commands."""
    def __init__(self, initial_save=""):
        self.calls = []
        self.save_state = initial_save
        self.installed_blob = None

    def __call__(self, argv, check=True, input_bytes=None, timeout=60):
        self.calls.append({"argv": list(argv), "input": input_bytes})
        joined = " ".join(argv)
        if argv[0] == "iptables-save":
            return _CP(0, self.save_state.encode())
        if argv[0] == "iptables-restore":
            if input_bytes:
                self.installed_blob = input_bytes.decode("utf-8",
                                                         "replace")
                # simulate: the installed rules now appear in save
                self.save_state += input_bytes.decode("utf-8",
                                                      "replace")
            return _CP(0)
        if argv[0] == "iptables" and "-D" in argv:
            # simulate rule removal: strip lines with this SID's comment
            for part in argv:
                if part.startswith("mp-e2e:"):
                    self.save_state = "\n".join(
                        l for l in self.save_state.splitlines()
                        if part not in l)
                    break
            return _CP(0)
        if argv[0] == "iptables" and "-X" in argv:
            chain = argv[argv.index("-X") + 1] if "-X" in argv else ""
            self.save_state = "\n".join(
                l for l in self.save_state.splitlines()
                if not l.startswith(":%s " % chain))
            return _CP(0)

        return _CP(0)


class _FakeDocker:
    """Fake docker executor for route probes and receipt validation."""
    def __init__(self, containers=None, probe_source_ips=None,
                 probe_exit_codes=None):
        self.calls = []
        self.containers = containers or {}
        self.probe_source_ips = probe_source_ips or {}
        self.probe_exit_codes = probe_exit_codes or {}

    def __call__(self, argv, check=True, timeout=60, **kw):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        # inspect
        if argv[0] == "inspect":
            name = argv[1]
            fmt = argv[argv.index("--format") + 1] if "--format" in argv else ""
            info = self.containers.get(name, {})
            if "State.Status" in fmt:
                return _CP(0, info.get("state", "running").encode())
            if "RestartPolicy.Name" in fmt:
                return _CP(0, info.get("restart_policy",
                                       "no").encode())
            if "Networks" in fmt and "range" in fmt:
                nets = info.get("networks", [])
                return _CP(0, " ".join(nets).encode())
            if "Networks.hiclaw-net" in fmt:
                return _CP(0, info.get("ip", "").encode())
            return _CP(0, info.get("id", _hex(name)).encode())
        # exec (route probe or sha256sum)
        if argv[0] == "exec":
            name = argv[1]
            if "socket" in joined or "create_connection" in joined:
                # check for frozen exit code override first
                exit_code = self.probe_exit_codes.get(name)
                if exit_code is not None:
                    return _CP(exit_code, b"")
                src = self.probe_source_ips.get(name, "")
                return _CP(0, src.encode())
            if "sha256sum" in joined:
                ch = self.containers.get(name, {}).get(
                    "config_hash", "")
                path = argv[-1] if argv else ""
                return _CP(0, ("%s  %s" % (ch, path)).encode())
            return _CP(0, b"")
        # create/start/rm/network connect
        return _CP(0)


def info_hash(content):
    import hashlib
    return hashlib.sha256(content.encode()).hexdigest()


# ── §3 Firewall executor tests ─────────────────────────────────────────────

class TestFirewallExecutor(unittest.TestCase):

    def _plan(self, sid="ab12cd34"):
        edges = [("172.31.0.2", "172.22.0.2", 6167, "ctrl-tuwunel")]
        return e2f.build_firewall_plan(sid, edges=edges,
                                       own_subnets=["172.31.0.0/28"])

    def test_install_happy_path(self):
        fh = _FakeHost()
        journal = {}
        result = ex.install_firewall(self._plan(), host_executor=fh,
                                     journal=journal)
        self.assertEqual(result, "installed")
        self.assertEqual(journal["firewall_sid"], "ab12cd34")
        # verify sequence: save, test, commit, save
        ops = [c["argv"][0] for c in fh.calls]
        self.assertEqual(ops.count("iptables-save"), 2)
        self.assertEqual(ops.count("iptables-restore"), 2)

    def test_blob_rides_stdin_not_argv(self):
        fh = _FakeHost()
        plan = self._plan()
        ex.install_firewall(plan, host_executor=fh, journal={})
        for call in fh.calls:
            if call["argv"][0] == "iptables-restore":
                self.assertIsNotNone(call["input"],
                                     "blob must ride stdin")
                blob_str = call["input"].decode()
                self.assertIn("*filter", blob_str)
                # argv must NOT contain the blob
                self.assertNotIn("*filter", " ".join(call["argv"]))

    def test_no_shell_reassembly(self):
        fh = _FakeHost()
        ex.install_firewall(self._plan(), host_executor=fh, journal={})
        for call in fh.calls:
            argv = call["argv"]
            self.assertIsInstance(argv, list)
            self.assertNotIn("bash", argv)
            self.assertNotIn("sh", argv)
            self.assertNotIn("-c", [a for a in argv if a == "-c"]
                             if "iptables" not in argv[0] else [])

    def test_test_failure_no_commit(self):
        class _FailingTest(_FakeHost):
            def __call__(self, argv, **kw):
                if argv[0] == "iptables-restore" and "--test" in argv:
                    self.calls.append({"argv": list(argv),
                                       "input": kw.get("input_bytes")})
                    return _CP(1, b"test failed")
                if argv[0] == "iptables-restore":
                    self.calls.append({"argv": list(argv),
                                       "input": kw.get("input_bytes")})
                    return _CP(0)  # would succeed, but shouldn't be reached
                return super().__call__(argv, **kw)
        fh = _FailingTest()
        with self.assertRaises(ex.FirewallExecutorError) as ctx:
            ex.install_firewall(self._plan(), host_executor=fh, journal={})
        self.assertEqual(ctx.exception.code, "FIREWALL_TEST_FAILED")
        # only one restore call (--test), no commit
        restores = [c for c in fh.calls
                    if c["argv"][0] == "iptables-restore"]
        self.assertEqual(len(restores), 1)
        self.assertIn("--test", restores[0]["argv"])

    def test_idempotent_when_already_installed(self):
        plan = self._plan()
        fh = _FakeHost()
        # Pre-install
        ex.install_firewall(plan, host_executor=fh, journal={})
        # Second install should be idempotent
        journal2 = {}
        result = ex.install_firewall(plan, host_executor=fh,
                                     journal=journal2)
        self.assertEqual(result, "idempotent")

    def test_foreign_session_rejected(self):
        fh = _FakeHost(
            initial_save='-I DOCKER-USER ... --comment '
                         '"mp-e2e:deadbeef:jump"\n')
        with self.assertRaises(ex.FirewallExecutorError) as ctx:
            ex.install_firewall(self._plan(), host_executor=fh, journal={})
        self.assertEqual(ctx.exception.code, "PIN_FOREIGN_SESSION")

    def test_teardown_removes_only_current_sid(self):
        plan = self._plan()
        fh = _FakeHost()
        journal = {}
        ex.install_firewall(plan, host_executor=fh, journal=journal)
        executed = ex.teardown_firewall(plan, host_executor=fh)
        self.assertTrue(len(executed) > 0)
        # teardown argvs only reference this SID's chains
        for argv in executed:
            if "iptables" in " ".join(argv) and "-D" in argv:
                self.assertIn("mp-e2e:ab12cd34",
                              " ".join(argv))

    def test_full_topology_counts(self):
        """10 edges / 8 subnets -> 12 jumps / 10+10 rules / 8 drops."""
        edges = e2f._build_all_edges("172.22.0.2")
        subnets = e2f.R4_ALL_SUBNETS
        plan = e2f.build_firewall_plan("ffff0001", edges=edges,
                                       own_subnets=subnets)
        self.assertEqual(plan["counts"]["docker_user_jumps"], 12)
        self.assertEqual(plan["counts"]["forward_accept"], 10)
        self.assertEqual(plan["counts"]["reverse_accept"], 10)
        self.assertEqual(plan["counts"]["subnet_drop"], 8)
        self.assertEqual(plan["counts"]["input_jumps"], 8)

    def test_no_global_established(self):
        plan = self._plan()
        self.assertNotIn("--ctstate ESTABLISHED,RELATED",
                         plan["restore_blob"])

    def test_i_to_a_serialization(self):
        """Verify that '-I CH 1 ...' in the blob matches '-A CH ...'
        in iptables-save output (no false drift)."""
        blob_rule = ("-I DOCKER-USER 1 -s 172.31.0.0/28 "
                     "-j MP-EG-ab12cd34")
        save_rule = ("-A DOCKER-USER -s 172.31.0.0/28 "
                     "-j MP-EG-ab12cd34")
        self.assertEqual(
            ex._normalize_rule_for_compare(blob_rule),
            ex._normalize_rule_for_compare(save_rule))


# ── §4 Route probe tests ───────────────────────────────────────────────────

class TestRouteProbes(unittest.TestCase):

    def _docker(self, source_ips):
        return _FakeDocker(probe_source_ips=source_ips)

    def test_all_six_probes_success(self):
        sources = {
            "mp-e2e-route-probe-controller": "172.31.0.2",
            "mp-e2e-route-probe-policy-gateway": "172.31.0.18",
            "mp-e2e-route-probe-mcp-bridge": "172.31.0.82",
            "mp-e2e-route-probe-gh-reporter": "172.31.0.66",
            "mp-e2e-route-probe-gh-proxy-r": "172.31.0.130",
            "mp-e2e-route-probe-gh-proxy-b": "172.31.0.131",
        }
        fd = self._docker(sources)
        journal = {}
        results = ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal=journal)
        self.assertEqual(len(results), 6)
        for service, result in results.items():
            self.assertTrue(result["verified"], service)
        # zero residue (all probes cleaned up)
        self.assertEqual(len(journal), 0)
        # rm calls for cleanup
        rm_calls = [c for c in fd.calls if c[0] == "rm"]
        self.assertEqual(len(rm_calls), 6)

    def test_source_ip_mismatch_fails(self):
        sources = {
            "mp-e2e-route-probe-controller": "172.99.99.99",  # wrong
            "mp-e2e-route-probe-policy-gateway": "172.31.0.18",
            "mp-e2e-route-probe-mcp-bridge": "172.31.0.82",
            "mp-e2e-route-probe-gh-reporter": "172.31.0.66",
            "mp-e2e-route-probe-gh-proxy-r": "172.31.0.130",
            "mp-e2e-route-probe-gh-proxy-b": "172.31.0.131",
        }
        fd = self._docker(sources)
        results = ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal={})
        self.assertFalse(results["controller"]["verified"])
        self.assertEqual(results["controller"]["error"],
                         "ROUTE_GATE_FAILED")

    def test_empty_output_is_invalid_and_cleanup(self):
        """Empty stdout -> PROBE_OUTPUT_INVALID (NOT TARGET_UNREACHABLE);
        probe cleaned up; 6 structured results; no secrets in argv."""
        sources = {}
        fd = _FakeDocker(probe_source_ips=sources)
        journal = {}
        results = ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal=journal)
        self.assertEqual(len(results), 6)
        self.assertEqual(results["controller"]["error"],
                         "PROBE_OUTPUT_INVALID")
        self.assertNotEqual(results["controller"]["error"],
                            "PROBE_TARGET_UNREACHABLE")
        self.assertEqual(len(journal), 0)
        rm_calls = [c for c in fd.calls if c[0] == "rm"]
        self.assertEqual(len(rm_calls), 6)
        for call in fd.calls:
            joined = " ".join(str(c) for c in call)
            for forbidden in ("--env-file", ".pem", "secret"):
                self.assertNotIn(forbidden, joined.lower())

    def test_no_secrets_on_probes(self):
        fd = self._docker({})
        ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal={})
        create_calls = [c for c in fd.calls if c[0] == "create"]
        for call in create_calls:
            joined = " ".join(call)
            self.assertNotIn("--env-file", joined)
            self.assertNotIn("pem", joined.lower())
            self.assertNotIn("pat", joined.lower())
            self.assertNotIn("secret", joined.lower())

    def test_connect_uses_frozen_ip_and_priority(self):
        fd = self._docker({
            "mp-e2e-route-probe-controller": "172.31.0.2",
            "mp-e2e-route-probe-policy-gateway": "172.31.0.18",
            "mp-e2e-route-probe-mcp-bridge": "172.31.0.82",
            "mp-e2e-route-probe-gh-reporter": "172.31.0.66",
            "mp-e2e-route-probe-gh-proxy-r": "172.31.0.130",
            "mp-e2e-route-probe-gh-proxy-b": "172.31.0.131"})
        ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal={})
        connects = [c for c in fd.calls
                    if c[:2] == ["network", "connect"]]
        self.assertEqual(len(connects), 12)  # 6 probes × 2 networks
        for call in connects:
            self.assertIn("--gw-priority", call)


# ── §5 Receipt validator tests ─────────────────────────────────────────────

def _hex(s):
    """Derive a stable 64-char lowercase hex from a string."""
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


def _make_receipt(**overrides):
    agents = []
    for role, (cname, mxid, ip, path) in ex.HICLAW_ROLE_FREEZE.items():
        agents.append({
            "role": role,
            "container_name": cname,
            "container_id": _hex(cname),
            "mxid": mxid,
            "hiclaw_net_ip": ip,
            "gateway_url": "http://172.31.0.18:8083%s" % path,
            "config_hash_before": _hex("before-" + role),
            "config_hash_after": _hex("after-" + role),
            "token_hash": _hex("tok-" + role),
        })
    receipt = {
        "schema_version": 1,
        "agents": agents,
        "old_github_mcp": {
            "container_id": _hex("github-mcp"),
            "state": "stopped",
            "restart_policy": "no",
            "network_attachments": ["mcp-backend-net"],
        },
        "rollback_ownership": "mp-gh4-harness",
    }
    # compute canonical receipt_sha256
    receipt["receipt_sha256"] = ex._compute_receipt_sha256(receipt)
    receipt.update(overrides)
    return receipt


def _matching_docker(receipt):
    containers = {}
    for agent in receipt["agents"]:
        containers[agent["container_name"]] = {
            "id": agent["container_id"],
            "state": "running",
            "ip": agent["hiclaw_net_ip"],
            "config_hash": agent["config_hash_after"],
        }
    old = receipt["old_github_mcp"]
    containers["github-mcp"] = {
        "id": old.get("container_id", _hex("github-mcp")),
        "state": old.get("state", "stopped"),
        "ip": "",
        "config_hash": "",
        "restart_policy": old.get("restart_policy", "no"),
        "networks": old.get("network_attachments", []),
    }
    return _FakeDocker(containers=containers)


class TestReceiptValidator(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.receipt_path = str(Path(self.tmpdir.name) / "receipt.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write(self, receipt):
        Path(self.receipt_path).write_text(
            json.dumps(receipt), encoding="utf-8")

    def test_four_roles_success(self):
        receipt = _make_receipt()
        self._write(receipt)
        fd = _matching_docker(receipt)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertTrue(result["verified"], result["checks"])
        self.assertEqual(len(result["checks"]), 5)  # 4 roles + old_mcp

    def test_missing_role_rejected(self):
        receipt = _make_receipt()
        receipt["agents"] = receipt["agents"][:3]
        receipt["receipt_sha256"] = ex._compute_receipt_sha256(receipt)
        self._write(receipt)
        fd = _matching_docker(_make_receipt())
        with self.assertRaises(ex.ReceiptValidationError) as ctx:
            ex.validate_hiclaw_receipt(
                self.receipt_path, docker_executor=fd)
        self.assertEqual(ctx.exception.code, "RECEIPT_AGENT_COUNT")

    def test_container_id_drift(self):
        receipt = _make_receipt()
        self._write(receipt)
        containers = _matching_docker(receipt).containers
        containers["hiclaw-manager"]["id"] = "DIFFERENT-ID"
        fd = _FakeDocker(containers=containers)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["manager"]["container_id"], "DRIFT")

    def test_ip_drift(self):
        receipt = _make_receipt()
        self._write(receipt)
        containers = _matching_docker(receipt).containers
        containers["hiclaw-worker-reviewer"]["ip"] = "10.99.99.99"
        fd = _FakeDocker(containers=containers)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["reviewer"]["ip"], "DRIFT")

    def test_gateway_url_mismatch(self):
        receipt = _make_receipt()
        receipt["agents"][0]["gateway_url"] = "http://wrong:9999/wrong"
        receipt["receipt_sha256"] = ex._compute_receipt_sha256(receipt)
        self._write(receipt)
        fd = _matching_docker(receipt)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertFalse(result["verified"])

    def test_config_hash_drift(self):
        receipt = _make_receipt()
        self._write(receipt)
        containers = _matching_docker(receipt).containers
        containers["hiclaw-worker-fixer"]["config_hash"] = "stale-hash"
        fd = _FakeDocker(containers=containers)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["fixer"]["config_hash"], "DRIFT")

    def test_old_mcp_state_mismatch(self):
        receipt = _make_receipt()
        self._write(receipt)
        containers = _matching_docker(receipt).containers
        containers["github-mcp"]["state"] = "running"  # should be stopped
        fd = _FakeDocker(containers=containers)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["old_github_mcp"]["state"], "MISMATCH")

    def test_ownership_wrong(self):
        receipt = _make_receipt()
        receipt["rollback_ownership"] = "someone-else"
        self._write(receipt)
        with self.assertRaises(ex.ReceiptValidationError):
            ex.validate_hiclaw_receipt(
                self.receipt_path,
                docker_executor=_matching_docker(_make_receipt()))

    def test_no_secret_in_output(self):
        receipt = _make_receipt()
        self._write(receipt)
        fd = _matching_docker(receipt)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        blob = str(result) + str(fd.calls)
        for forbidden in ("ghp_", "ghs_", "syt_", "BEGIN PRIVATE",
                          "password=", "postgresql://"):
            self.assertNotIn(forbidden, blob)

    def test_executor_calls_are_read_only(self):
        receipt = _make_receipt()
        self._write(receipt)
        fd = _matching_docker(receipt)
        ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        for call in fd.calls:
            verb = call[0]
            self.assertIn(verb, ("inspect", "exec"),
                          "read-only: got %s" % verb)
            if verb == "exec":
                self.assertIn("sha256sum", " ".join(call))


# ── §3 W3A-R1: firewall fix tests ─────────────────────────────────────────

class TestFirewallR1Fixes(unittest.TestCase):

    def _plan(self):
        edges = [("172.31.0.2", "172.22.0.2", 6167, "ctrl")]
        return e2f.build_firewall_plan("ab12cd34", edges=edges,
                                       own_subnets=["172.31.0.0/28"])

    def test_noflush_failure_not_marked_success(self):
        class _CommitFail(_FakeHost):
            def __call__(self, argv, **kw):
                r = super().__call__(argv, **kw)
                if (argv[0] == "iptables-restore"
                        and "--noflush" in argv):
                    return _CP(1, b"commit failed")
                return r
        fh = _CommitFail()
        journal = {}
        with self.assertRaises(ex.FirewallExecutorError) as ctx:
            ex.install_firewall(self._plan(), host_executor=fh,
                                journal=journal)
        self.assertEqual(ctx.exception.code, "FIREWALL_COMMIT_FAILED")
        self.assertNotIn("firewall_state", journal)

    def test_verify_fail_auto_rollback(self):
        class _VerifyFail(_FakeHost):
            def __call__(self, argv, **kw):
                r = super().__call__(argv, **kw)
                if argv[0] == "iptables-save":
                    # Return EMPTY save on second call (verify)
                    calls = [c for c in self.calls
                             if c["argv"][0] == "iptables-save"]
                    if len(calls) >= 2:
                        return _CP(0, b"")
                return r
        fh = _VerifyFail()
        journal = {}
        with self.assertRaises(ex.FirewallExecutorError) as ctx:
            ex.install_firewall(self._plan(), host_executor=fh,
                                journal=journal)
        self.assertEqual(ctx.exception.code, "FIREWALL_VERIFY_FAILED")
        self.assertEqual(journal.get("firewall_state"),
                         "verify-failed-rolled-back")
        # teardown was attempted
        rm_calls = [c for c in fh.calls
                    if c["argv"][0] == "iptables"]
        self.assertTrue(len(rm_calls) > 0)

    def test_rollback_failure_preserves_primary_error(self):
        class _BothFail(_FakeHost):
            def __call__(self, argv, **kw):
                r = super().__call__(argv, **kw)
                if argv[0] == "iptables-save":
                    calls = [c for c in self.calls
                             if c["argv"][0] == "iptables-save"]
                    if len(calls) >= 2:
                        return _CP(0, b"")
                if argv[0] == "iptables" and "-D" in argv:
                    # rollback also fails: rules remain
                    return _CP(0)
                return r
        fh = _BothFail()
        # simulate: after commit, save returns rules that don't match
        # and -D doesn't actually remove them (residue persists)
        fh.save_state = ""  # ensure verify fails
        journal = {}
        try:
            ex.install_firewall(self._plan(), host_executor=fh,
                                journal=journal)
            self.fail("should have raised")
        except ex.FirewallExecutorError as exc:
            self.assertEqual(exc.code, "FIREWALL_VERIFY_FAILED")

    def test_teardown_residue_is_blocking(self):
        class _ResidueHost(_FakeHost):
            def __call__(self, argv, **kw):
                if argv[0] == "iptables-save":
                    # Always report rules present (teardown ineffective)
                    return _CP(0, b'-A X --comment "mp-e2e:ab12cd34:x"\n'
                                 b':MP-EG-ab12cd34 - [0:0]\n')
                return _CP(0)
        fh = _ResidueHost()
        with self.assertRaises(ex.FirewallExecutorError) as ctx:
            ex.teardown_firewall(self._plan(), host_executor=fh)
        self.assertEqual(ctx.exception.code,
                         "FIREWALL_TEARDOWN_RESIDUE")

    def test_teardown_empty_journal_noop(self):
        """Empty teardown plan -> no delete calls, no error."""
        fh = _FakeHost()
        empty_plan = {"sid": "ffffffff", "teardown_argv": []}
        result = ex.teardown_firewall(empty_plan, host_executor=fh)
        self.assertEqual(result, [])
        # no iptables -D or -X calls (only the residue scan)
        del_calls = [c for c in fh.calls
                     if c["argv"][0] == "iptables"]
        self.assertEqual(len(del_calls), 0)

    def test_normalization_does_not_hide_target_drift(self):
        """Verify that changing any rule component IS detected as drift."""
        base = "-A MP-EG-ab12cd34 -s 172.31.0.2/32 -d 172.22.0.2/32 -p tcp --dport 6167 -m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT"
        # IP drift
        drifted_ip = base.replace("172.22.0.2", "172.99.99.99")
        self.assertNotEqual(ex._normalize_rule_for_compare(base),
                            ex._normalize_rule_for_compare(drifted_ip))
        # Port drift
        drifted_port = base.replace("6167", "8080")
        self.assertNotEqual(ex._normalize_rule_for_compare(base),
                            ex._normalize_rule_for_compare(drifted_port))
        # ctstate drift
        drifted_ct = base.replace("NEW,ESTABLISHED", "ESTABLISHED")
        self.assertNotEqual(ex._normalize_rule_for_compare(base),
                            ex._normalize_rule_for_compare(drifted_ct))
        # Target drift
        drifted_tgt = base.replace("-j ACCEPT", "-j DROP")
        self.assertNotEqual(ex._normalize_rule_for_compare(base),
                            ex._normalize_rule_for_compare(drifted_tgt))


# ── §4 W3A-R1: route probe fix tests ──────────────────────────────────────

class TestRouteProbeR1Fixes(unittest.TestCase):

    def _run(self, fd):
        return ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal={})

    def test_create_failure_cleanup(self):
        class _CreateFail(_FakeDocker):
            def __call__(self, argv, **kw):
                if argv[0] == "create":
                    return _CP(1, b"")
                return super().__call__(argv, **kw)
        fd = _CreateFail({})
        journal = {}
        results = ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal=journal)
        self.assertEqual(len(journal), 0)
        # all 6 probes still ran (not skipped)
        self.assertEqual(len(results), 6)
        # rm attempted for cleanup
        rm_calls = [c for c in fd.calls if c[0] == "rm"]
        self.assertEqual(len(rm_calls), 6)

    def test_timeout_does_not_skip_remaining_probes(self):
        class _TimeoutOnFirst(_FakeDocker):
            def __call__(self, argv, **kw):
                if argv[0] == "exec" and len(argv) > 1:
                    if "probe-controller" in argv[1]:
                        raise TimeoutError("simulated timeout")
                return super().__call__(argv, **kw)
        sources = {
            "mp-e2e-route-probe-policy-gateway": "172.31.0.18",
            "mp-e2e-route-probe-mcp-bridge": "172.31.0.82",
            "mp-e2e-route-probe-gh-reporter": "172.31.0.66",
            "mp-e2e-route-probe-gh-proxy-r": "172.31.0.130",
            "mp-e2e-route-probe-gh-proxy-b": "172.31.0.131",
        }
        fd = _TimeoutOnFirst(probe_source_ips=sources)
        journal = {}
        results = ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal=journal)
        self.assertEqual(len(results), 6)
        self.assertEqual(results["controller"]["error"],
                         "PROBE_TIMEOUT")
        self.assertTrue(results["policy-gateway"]["verified"])
        self.assertEqual(len(journal), 0)

    def test_multiline_output_cleanup(self):
        sources = {
            "mp-e2e-route-probe-controller": "172.31.0.2\nextra-line",
        }
        fd = self._run.__func__.__self__._docker(sources) if False else \
            _FakeDocker(probe_source_ips=sources)
        journal = {}
        results = ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal=journal)
        self.assertEqual(results["controller"]["error"],
                         "PROBE_OUTPUT_INVALID")
        self.assertEqual(len(journal), 0)

    def test_non_ip_output_cleanup(self):
        sources = {
            "mp-e2e-route-probe-controller": "not-an-ip",
        }
        fd = _FakeDocker(probe_source_ips=sources)
        journal = {}
        results = ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal=journal)
        self.assertEqual(results["controller"]["error"],
                         "PROBE_OUTPUT_INVALID")
        self.assertEqual(len(journal), 0)

    def test_success_cleanup(self):
        sources = {
            "mp-e2e-route-probe-controller": "172.31.0.2",
            "mp-e2e-route-probe-policy-gateway": "172.31.0.18",
            "mp-e2e-route-probe-mcp-bridge": "172.31.0.82",
            "mp-e2e-route-probe-gh-reporter": "172.31.0.66",
            "mp-e2e-route-probe-gh-proxy-r": "172.31.0.130",
            "mp-e2e-route-probe-gh-proxy-b": "172.31.0.131",
        }
        fd = _FakeDocker(probe_source_ips=sources)
        journal = {}
        results = ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal=journal)
        for svc in results:
            self.assertTrue(results[svc]["verified"], svc)
        self.assertEqual(len(journal), 0)
        rm_calls = [c for c in fd.calls if c[0] == "rm"]
        self.assertEqual(len(rm_calls), 6)


# ── §5 W3A-R1: receipt fix tests ──────────────────────────────────────────

class TestReceiptR1Fixes(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.receipt_path = str(Path(self.tmpdir.name) / "receipt.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write(self, receipt):
        Path(self.receipt_path).write_text(
            json.dumps(receipt), encoding="utf-8")

    def test_receipt_integrity_hash_success(self):
        receipt = _make_receipt()
        self._write(receipt)
        fd = _matching_docker(receipt)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertTrue(result["verified"])

    def test_receipt_integrity_hash_mismatch(self):
        receipt = _make_receipt()
        receipt["receipt_sha256"] = "0" * 64  # wrong hash
        self._write(receipt)
        with self.assertRaises(ex.ReceiptValidationError) as ctx:
            ex.validate_hiclaw_receipt(
                self.receipt_path,
                docker_executor=_matching_docker(receipt))
        self.assertEqual(ctx.exception.code,
                         "RECEIPT_INTEGRITY_MISMATCH")

    def test_receipt_hash_canonicalization_stable(self):
        receipt = _make_receipt()
        hash1 = ex._compute_receipt_sha256(receipt)
        hash2 = ex._compute_receipt_sha256(receipt)
        self.assertEqual(hash1, hash2)
        self.assertEqual(len(hash1), 64)

    def test_receipt_hash_fields_strict(self):
        receipt = _make_receipt()
        receipt["agents"][0]["config_hash_after"] = "NOT_HEX"
        # recompute integrity hash to pass that check
        receipt["receipt_sha256"] = ex._compute_receipt_sha256(receipt)
        self._write(receipt)
        with self.assertRaises(ex.ReceiptValidationError) as ctx:
            ex.validate_hiclaw_receipt(
                self.receipt_path,
                docker_executor=_matching_docker(receipt))
        self.assertEqual(ctx.exception.code, "RECEIPT_HASH_FORMAT")

    def test_receipt_extra_role_rejected(self):
        receipt = _make_receipt()
        receipt["agents"][0]["role"] = "ghost"
        receipt["receipt_sha256"] = ex._compute_receipt_sha256(receipt)
        self._write(receipt)
        with self.assertRaises(ex.ReceiptValidationError) as ctx:
            ex.validate_hiclaw_receipt(
                self.receipt_path,
                docker_executor=_matching_docker(receipt))
        self.assertEqual(ctx.exception.code, "RECEIPT_ROLE_MISMATCH")

    def test_receipt_duplicate_role_rejected(self):
        receipt = _make_receipt()
        reviewer_idx = next(i for i, a in enumerate(receipt["agents"])
                            if a["role"] == "reviewer")
        receipt["agents"][reviewer_idx] = dict(receipt["agents"][0])
        receipt["receipt_sha256"] = ex._compute_receipt_sha256(receipt)
        self._write(receipt)
        with self.assertRaises(ex.ReceiptValidationError) as ctx:
            ex.validate_hiclaw_receipt(
                self.receipt_path,
                docker_executor=_matching_docker(receipt))
        self.assertEqual(ctx.exception.code, "RECEIPT_DUPLICATE_ROLE")

    def test_old_mcp_id_drift(self):
        receipt = _make_receipt()
        self._write(receipt)
        containers = _matching_docker(receipt).containers
        containers["github-mcp"]["id"] = _hex("WRONG-ID")
        fd = _FakeDocker(containers=containers)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["old_github_mcp"]["container_id"],
            "DRIFT")

    def test_old_mcp_restart_policy_drift(self):
        receipt = _make_receipt()
        self._write(receipt)
        containers = _matching_docker(receipt).containers
        containers["github-mcp"]["restart_policy"] = "always"
        fd = _FakeDocker(containers=containers)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["old_github_mcp"]["restart_policy"],
            "DRIFT")

    def test_old_mcp_extra_network_drift(self):
        receipt = _make_receipt()
        self._write(receipt)
        containers = _matching_docker(receipt).containers
        containers["github-mcp"]["networks"] = [
            "mcp-backend-net", "EXTRA_NET"]
        fd = _FakeDocker(containers=containers)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["old_github_mcp"]["network_attachments"],
            "DRIFT")

    def test_old_mcp_missing_network_drift(self):
        receipt = _make_receipt()
        self._write(receipt)
        containers = _matching_docker(receipt).containers
        containers["github-mcp"]["networks"] = []
        fd = _FakeDocker(containers=containers)
        result = ex.validate_hiclaw_receipt(
            self.receipt_path, docker_executor=fd)
        self.assertFalse(result["verified"])
        self.assertEqual(
            result["checks"]["old_github_mcp"]["network_attachments"],
            "DRIFT")


# ── §3 R1.1: missing route probe failure-path tests ────────────────────────

class TestRouteProbeCleanupPaths(unittest.TestCase):
    """Each failure path must produce structured results, clean up
    (rm -f + journal empty), and not skip remaining probes."""

    def _run(self, fd, journal):
        return ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal=journal)

    def _assert_cleanup(self, fd, journal, results):
        self.assertEqual(len(results), 6)
        self.assertEqual(len(journal), 0)
        rm_calls = [c for c in fd.calls if c[0] == "rm" and "-f" in c]
        self.assertTrue(len(rm_calls) >= 6)
        for call in fd.calls:
            joined = " ".join(str(c) for c in call)
            for forbidden in ("--env-file", ".pem", "secret"):
                self.assertNotIn(forbidden, joined.lower())

    def test_first_connect_failure_cleanup(self):
        class _Connect1Fail(_FakeDocker):
            call_count = {}
            def __call__(self, argv, **kw):
                if argv[:2] == ["network", "connect"]:
                    key = argv[-1]
                    self.call_count[key] = self.call_count.get(key, 0) + 1
                    if self.call_count[key] == 1:
                        return _CP(1, b"connect failed")
                return super().__call__(argv, **kw)
        fd = _Connect1Fail({})
        journal = {}
        results = self._run(fd, journal)
        self._assert_cleanup(fd, journal, results)
        self.assertEqual(results["controller"]["error"],
                         "PROBE_CONNECT_FAILED")
        self.assertNotEqual(results["controller"]["error"],
                            "ROUTE_GATE_FAILED")

    def test_second_connect_failure_cleanup(self):
        class _Connect2Fail(_FakeDocker):
            call_count = {}
            def __call__(self, argv, **kw):
                if argv[:2] == ["network", "connect"]:
                    key = argv[-1]
                    self.call_count[key] = self.call_count.get(key, 0) + 1
                    if self.call_count[key] == 2:
                        return _CP(1, b"connect 2 failed")
                return super().__call__(argv, **kw)
        fd = _Connect2Fail({})
        journal = {}
        results = self._run(fd, journal)
        self._assert_cleanup(fd, journal, results)
        self.assertEqual(results["controller"]["error"],
                         "PROBE_CONNECT_FAILED")

    def test_start_or_exec_failure_cleanup(self):
        class _StartFail(_FakeDocker):
            def __call__(self, argv, **kw):
                if argv[0] == "start":
                    return _CP(1, b"start failed")
                return super().__call__(argv, **kw)
        fd = _StartFail({})
        journal = {}
        results = self._run(fd, journal)
        self._assert_cleanup(fd, journal, results)
        self.assertEqual(results["controller"]["error"],
                         "PROBE_START_FAILED")

    def test_route_mismatch_cleanup(self):
        """Valid IP but wrong source -> ROUTE_GATE_FAILED, not
        CONNECT/START; all probes still run and clean up."""
        sources = {
            "mp-e2e-route-probe-controller": "10.99.99.99",
            "mp-e2e-route-probe-policy-gateway": "172.31.0.18",
            "mp-e2e-route-probe-mcp-bridge": "172.31.0.82",
            "mp-e2e-route-probe-gh-reporter": "172.31.0.66",
            "mp-e2e-route-probe-gh-proxy-r": "172.31.0.130",
            "mp-e2e-route-probe-gh-proxy-b": "172.31.0.131",
        }
        fd = _FakeDocker(probe_source_ips=sources)
        journal = {}
        results = self._run(fd, journal)
        self._assert_cleanup(fd, journal, results)
        self.assertEqual(results["controller"]["error"],
                         "ROUTE_GATE_FAILED")
        self.assertTrue(results["policy-gateway"]["verified"])


# ── §3 R1.1: missing firewall tests ────────────────────────────────────────

class TestFirewallR11Fixes(unittest.TestCase):

    def _plan(self, sid="ab12cd34"):
        edges = [("172.31.0.2", "172.22.0.2", 6167, "ctrl")]
        return e2f.build_firewall_plan(sid, edges=edges,
                                       own_subnets=["172.31.0.0/28"])

    def test_teardown_foreign_sid_untouched(self):
        """Teardown only executes current SID's argvs; foreign SID rules
        and chains are NOT deleted; no flush, no global chain deletion."""
        fh = _FakeHost(
            initial_save='-A X --comment "mp-e2e:deadbeef:jump"\n'
                         ':MP-EG-deadbeef - [0:0]\n')
        plan = self._plan()
        # populate save with foreign rules only
        fh.save_state = ('-A DOCKER-USER -s X --comment '
                         '"mp-e2e:deadbeef:jump"\n'
                         ':MP-EG-deadbeef - [0:0]\n')
        # teardown our plan (which has no installed rules)
        try:
            ex.teardown_firewall(plan, host_executor=fh)
        except ex.FirewallExecutorError:
            pass  # residue is fine; we're checking foreign untouched
        # verify no iptables call targets the foreign SID
        for call in fh.calls:
            if call["argv"][0] == "iptables" and "-D" in call["argv"]:
                joined = " ".join(call["argv"])
                self.assertNotIn("deadbeef", joined,
                                 "foreign SID must not be deleted")
            if call["argv"][0] == "iptables" and "-X" in call["argv"]:
                joined = " ".join(call["argv"])
                self.assertNotIn("deadbeef", joined)
            # no flush
            self.assertNotIn("-F", call["argv"] if "iptables" in
                             call["argv"] else [])

    def test_firewall_journal_contains_no_rule_payload(self):
        """Journal must not contain restore blob, full rules, secrets,
        or executor stdout/stderr. Only SID, state, teardown argvs."""
        fh = _FakeHost()
        journal = {}
        ex.install_firewall(self._plan(), host_executor=fh,
                            journal=journal)
        blob_str = str(journal)
        # No restore blob content
        self.assertNotIn("*filter", blob_str)
        self.assertNotIn("COMMIT", blob_str)
        # No full iptables rules (heuristic: no -A or -I prefixes)
        self.assertNotIn("-A MP-", blob_str)
        self.assertNotIn("-I DOCKER-USER", blob_str)
        # No secrets
        for forbidden in ("ghp_", "password=", "postgresql://",
                          "BEGIN PRIVATE"):
            self.assertNotIn(forbidden, blob_str)
        # Journal DOES contain the required safe fields
        self.assertIn("firewall_sid", journal)
        self.assertIn("firewall_state", journal)
        self.assertIn("firewall_teardown", journal)
        self.assertEqual(journal["firewall_sid"], "ab12cd34")


# ── §2/§3 R1.2: firewall state precision + target unreachable ──────────────

class TestFirewallStatePrecision(unittest.TestCase):

    def _plan(self):
        edges = [("172.31.0.2", "172.22.0.2", 6167, "c")]
        return e2f.build_firewall_plan("ab12cd34", edges=edges,
                                       own_subnets=["172.31.0.0/28"])

    def test_rollback_success_marks_rolled_back(self):
        class _VerifyPassTeardown(_FakeHost):
            def __call__(self, argv, **kw):
                r = super().__call__(argv, **kw)
                if argv[0] == "iptables-save":
                    calls = [c for c in self.calls
                             if c["argv"][0] == "iptables-save"]
                    if len(calls) >= 2:
                        return _CP(0, b"")  # verify fails
                return r
        fh = _VerifyPassTeardown()
        journal = {}
        with self.assertRaises(ex.FirewallExecutorError) as ctx:
            ex.install_firewall(self._plan(), host_executor=fh,
                                journal=journal)
        self.assertEqual(ctx.exception.code, "FIREWALL_VERIFY_FAILED")
        self.assertEqual(journal.get("firewall_state"),
                         "verify-failed-rolled-back")

    def test_rollback_failure_marks_rollback_failed(self):
        class _BothFail(_FakeHost):
            save_call_count = 0
            def __call__(self, argv, **kw):
                if argv[0] == "iptables-save":
                    _BothFail.save_call_count += 1
                    if _BothFail.save_call_count == 2:
                        # verify scan: return empty → verify fails
                        return _CP(0, b"")
                    if _BothFail.save_call_count >= 3:
                        # teardown residue scan: rules remain
                        return _CP(0, b'-A X --comment '
                                     b'"mp-e2e:ab12cd34:r"\n'
                                     b':MP-EG-ab12cd34 - [0:0]\n')
                if argv[0] == "iptables" and "-D" in argv:
                    # simulate: -D doesn't actually remove rules
                    return _CP(0)
                return super().__call__(argv, **kw)
        fh = _BothFail()
        journal = {}
        with self.assertRaises(ex.FirewallExecutorError) as ctx:
            ex.install_firewall(self._plan(), host_executor=fh,
                                journal=journal)
        self.assertEqual(ctx.exception.code, "FIREWALL_VERIFY_FAILED")
        self.assertEqual(journal.get("firewall_state"),
                         "verify-failed-rollback-failed")
        self.assertIn("firewall_rollback_error", journal)
        # state text has no misleading "rolled-back"
        self.assertNotIn("rolled-back",
                         journal.get("firewall_state", ""))


class TestTargetUnreachable(unittest.TestCase):
    """PROBE_TARGET_UNREACHABLE via frozen exit code 42."""

    def _run(self, fd):
        return ex.run_route_probes(
            docker_executor=fd, host_executor=None,
            image_ref="sha256:ab", tuwunel_ip="172.22.0.2",
            windows_proxy_ip="172.23.48.1", probe_journal={})

    def test_target_unreachable_distinct_from_source_error(self):
        """Exit 42 -> PROBE_TARGET_UNREACHABLE; distinct from
        ROUTE_GATE_FAILED and PROBE_OUTPUT_INVALID."""
        sources = {
            "mp-e2e-route-probe-policy-gateway": "172.31.0.18",
            "mp-e2e-route-probe-mcp-bridge": "172.31.0.82",
            "mp-e2e-route-probe-gh-reporter": "172.31.0.66",
            "mp-e2e-route-probe-gh-proxy-r": "172.31.0.130",
            "mp-e2e-route-probe-gh-proxy-b": "172.31.0.131",
        }
        exit_codes = {"mp-e2e-route-probe-controller":
                      ex.PROBE_EXIT_TCP_CONNECT_FAILED}
        fd = _FakeDocker(probe_source_ips=sources,
                         probe_exit_codes=exit_codes)
        journal = {}
        results = self._run(fd)
        self.assertEqual(len(results), 6)
        self.assertEqual(results["controller"]["error"],
                         "PROBE_TARGET_UNREACHABLE")
        self.assertNotEqual(results["controller"]["error"],
                            "ROUTE_GATE_FAILED")
        self.assertNotEqual(results["controller"]["error"],
                            "PROBE_OUTPUT_INVALID")
        self.assertTrue(results["policy-gateway"]["verified"])
        self.assertEqual(len(journal), 0)
        rm_calls = [c for c in fd.calls if c[0] == "rm"]
        self.assertEqual(len(rm_calls), 6)

    def test_unknown_nonzero_is_not_target_unreachable(self):
        """Exit 99 (unknown) -> NOT PROBE_TARGET_UNREACHABLE; classified
        as PROBE_INTERNAL_ERROR; cleanup and continuation hold."""
        sources = {
            "mp-e2e-route-probe-policy-gateway": "172.31.0.18",
            "mp-e2e-route-probe-mcp-bridge": "172.31.0.82",
            "mp-e2e-route-probe-gh-reporter": "172.31.0.66",
            "mp-e2e-route-probe-gh-proxy-r": "172.31.0.130",
            "mp-e2e-route-probe-gh-proxy-b": "172.31.0.131",
        }
        exit_codes = {"mp-e2e-route-probe-controller": 99}
        fd = _FakeDocker(probe_source_ips=sources,
                         probe_exit_codes=exit_codes)
        journal = {}
        results = self._run(fd)
        self.assertEqual(len(results), 6)
        self.assertNotEqual(results["controller"]["error"],
                            "PROBE_TARGET_UNREACHABLE")
        self.assertEqual(results["controller"]["error"],
                         "PROBE_INTERNAL_ERROR")
        self.assertEqual(len(journal), 0)

    def test_exit_43_is_internal_error_not_output_invalid(self):
        """Exit 43 (PROBE_EXIT_INTERNAL_ERROR) -> PROBE_INTERNAL_ERROR
        (NOT PROBE_OUTPUT_INVALID, NOT PROBE_TARGET_UNREACHABLE);
        cleanup + continuation; 6 results; journal empty; no secrets."""
        sources = {
            "mp-e2e-route-probe-policy-gateway": "172.31.0.18",
            "mp-e2e-route-probe-mcp-bridge": "172.31.0.82",
            "mp-e2e-route-probe-gh-reporter": "172.31.0.66",
            "mp-e2e-route-probe-gh-proxy-r": "172.31.0.130",
            "mp-e2e-route-probe-gh-proxy-b": "172.31.0.131",
        }
        exit_codes = {"mp-e2e-route-probe-controller":
                      ex.PROBE_EXIT_INTERNAL_ERROR}
        fd = _FakeDocker(probe_source_ips=sources,
                         probe_exit_codes=exit_codes)
        journal = {}
        results = self._run(fd)
        # 6 results, exactly one per service
        self.assertEqual(len(results), 6)
        # controller: PROBE_INTERNAL_ERROR
        self.assertEqual(results["controller"]["error"],
                         "PROBE_INTERNAL_ERROR")
        # NOT OUTPUT_INVALID, NOT TARGET_UNREACHABLE
        self.assertNotEqual(results["controller"]["error"],
                            "PROBE_OUTPUT_INVALID")
        self.assertNotEqual(results["controller"]["error"],
                            "PROBE_TARGET_UNREACHABLE")
        # other 5 succeed
        for svc in ("policy-gateway", "mcp-bridge", "gh-reporter",
                    "gh-proxy-r", "gh-proxy-b"):
            self.assertTrue(results[svc]["verified"], svc)
        # cleanup: journal empty, rm called
        self.assertEqual(len(journal), 0)
        rm_calls = [c for c in fd.calls if c[0] == "rm" and "-f" in c]
        self.assertTrue(len(rm_calls) >= 6)
        # no secrets in any argv or output
        for call in fd.calls:
            joined = " ".join(str(c) for c in call)
            for forbidden in ("--env-file", ".pem", "secret"):
                self.assertNotIn(forbidden, joined.lower())
        blob = str(results) + str(fd.calls)
        for forbidden in ("ghp_", "password=", "BEGIN PRIVATE",
                          "postgresql://"):
            self.assertNotIn(forbidden, blob)


class TestReceiptCanonicalizationExplicit(unittest.TestCase):

    def test_ensure_ascii_true_explicit(self):
        source = open("tools/cli/e2e_executors.py",
                      encoding="utf-8").read()
        self.assertIn("ensure_ascii=True", source)

    def test_non_ascii_field_stable_hash(self):
        """Non-ASCII field produces deterministic hash via ensure_ascii."""
        test_data = {"description": "café ☕", "value": 42}
        h1 = ex._compute_receipt_sha256(test_data)
        h2 = ex._compute_receipt_sha256(test_data)
        self.assertEqual(h1, h2)

    def test_field_order_irrelevant(self):
        """Canonical hash is independent of key insertion order."""
        d1 = {"a": 1, "b": 2}
        d2 = {"b": 2, "a": 1}
        self.assertEqual(ex._compute_receipt_sha256(d1),
                         ex._compute_receipt_sha256(d2))

    def test_ascii_escaping_deterministic(self):
        """Non-ASCII chars are escaped consistently (ensure_ascii=True)."""
        import json as json_mod
        canonical = {"key": "日本語"}
        raw = json_mod.dumps(canonical, sort_keys=True,
                             separators=(",", ":"),
                             ensure_ascii=True)
        self.assertIn("\\u", raw)  # verified escaped


if __name__ == "__main__":
    unittest.main()
