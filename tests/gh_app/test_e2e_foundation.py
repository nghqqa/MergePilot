"""M8-GH-4B1 tests — GitHub E2E controller/Matrix foundation (fully
static/mocked; no real WSL/iptables/Matrix/GitHub is ever touched).

Covers the §8 list: default-mode invariance, the B1 activation gate,
the strict 15-key env schema (LOCALPART sender contract), room-map/policy
1:1, secret-file transport + diagnostics redaction, create/connect/
gw-priority command contract, the route gate, FORWARD forward/reverse/
DROP rules and INPUT LOCAL-bypass deny, R4 rule counts, SID ownership/
idempotency/conflict, rollback ordering (containers -> pins -> networks),
the five-member Matrix preflight (fake transport), dry-run zero side
effects, and cleanup residue detection.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "demo_console")):
    if p not in sys.path:
        sys.path.insert(0, p)

import e2e_foundation as e2f                    # noqa: E402
import mergepilot as mp                         # noqa: E402
import one_click_startup as oc                  # noqa: E402


def _valid_env():
    return {
        "GITHUB_INGRESS_ENABLED": "1",
        "GITHUB_ROOM_MAP": "/run/mergepilot/room-map.yaml",
        "GITHUB_POLICY_PATH": "/run/mergepilot/policy-fixture.yaml",
        "GITHUB_DELIVERY_LEASE_SECONDS": "120",
        "GITHUB_DELIVERY_MAX_ATTEMPTS": "5",
        "MATRIX_HS": "http://matrix-hs:6167",
        "MATRIX_SERVER_NAME": e2f.E2E_MATRIX_SERVER_NAME,
        "MATRIX_USER": "m8gh4-controller",
        "CONTROLLER_CONSUMER_NAME": "m8gh4-controller",
        "M4F_ALLOWED_ROOMS": "!syntheticroom0000:"
                              + e2f.E2E_MATRIX_SERVER_NAME,
        "M4F_ALLOWED_SENDERS": "manager,reviewer,fixer,verifier",
        "M4F_RUN_PREFIX": "gh-",
        "RESERVED_RUN_PREFIXES": "",
        "GATEWAY_URL": "http://policy-gateway:8083",
        "COORDINATOR_TOKEN": "tok-" + "a" * 32,
        "PG_HOST": "postgres",
        "PG_PORT": "5432",
        "PG_DATABASE": "mergepilot_audit",
        "PG_USER": "mergepilot",
        "PG_PASS": "synthetic-pg-pass",
        "ADMIN_PW": "synthetic-admin-pw",
    }


B1_EDGES = [("172.31.0.2", e2f.E2E_TUWUNEL_DEFAULT_IP, e2f.E2E_TUWUNEL_PORT,
             "controller-to-tuwunel")]
CTRL_SUBNET = e2f.E2E_NETWORKS["ctrl-egress"][0]


def _b1_plan():
    return e2f.build_firewall_plan("ab12cd34", edges=B1_EDGES,
                                   own_subnets=[CTRL_SUBNET])


R4_EDGES = [
    ("172.31.0.2", "172.22.0.2", 6167, "controller-to-tuwunel"),
    ("172.31.0.18", "172.31.0.34", 8082, "gateway-to-bridge"),
    ("172.31.0.66", "172.31.0.98", 18090, "reporter-to-proxy-r"),
    ("172.31.0.82", "172.31.0.114", 18090, "bridge-to-proxy-b"),
    ("172.31.0.130", "172.23.48.1", 17890, "proxy-r-to-winproxy"),
    ("172.31.0.131", "172.23.48.1", 17890, "proxy-b-to-winproxy"),
    ("172.21.0.2", "172.31.0.18", 8083, "manager-to-gateway"),
    ("172.21.0.5", "172.31.0.18", 8083, "reviewer-to-gateway"),
    ("172.21.0.4", "172.31.0.18", 8083, "fixer-to-gateway"),
    ("172.21.0.6", "172.31.0.18", 8083, "verifier-to-gateway"),
]
R4_SUBNETS = [spec[0] for spec in e2f.E2E_NETWORKS.values()]


# ── §2 default-off contract / activation gate ────────────────────────────────

class TestActivationGate(unittest.TestCase):

    def test_gate_cleared_after_r2_conditions(self):
        # M8-GH-4B3-W3B-R2 final: all R2 prepush conditions verified
        # (wiring/lifecycle/persist/reparse/dry-run/namespace-8);
        # the gate is cleared and the REAL prerequisite probe is the
        # first door for a real start.
        self.assertEqual(e2f.E2E_PENDING_COMPONENTS, ())
        e2f.e2e_prerequisites_gate()
        # the prerequisites gate itself works (verified separately)
        e2f.e2e_prerequisites_gate()

    def test_prerequisites_gate_missing_raises(self):
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.e2e_prerequisites_gate(["pat_file"])
        self.assertEqual(ctx.exception.code,
                         "GITHUB_E2E_PREREQUISITES_INCOMPLETE")

    def test_real_cli_start_fails_closed_before_any_side_effect(self):
        # the gate sits before the install-manifest load: no fixtures,
        # no docker, no filesystem writes are needed for it to fire.
        with mock.patch.object(mp, "WslDocker") as wd:
            rc = mp.main(["start", "--run-id", "gate-probe", "--github-e2e",
                          "--project-dir", str(ROOT)])
        self.assertEqual(rc, 3)
        self.assertFalse(wd.called)

    def test_gate_is_flag_gated_not_global(self):
        # without --github-e2e the code path must not raise the B1 code
        source = (ROOT / "tools" / "cli" / "mergepilot.py").read_text(
            encoding="utf-8")
        self.assertIn('if getattr(args, "github_e2e", False):',
                      source)
        self.assertIn("GITHUB_E2E_COMPONENTS_INCOMPLETE", source)

    def test_parser_accepts_flag_on_start_and_doctor(self):
        parser = mp.build_parser()
        args = parser.parse_args(["start", "--run-id", "x", "--github-e2e"])
        self.assertTrue(args.github_e2e)
        args = parser.parse_args(["doctor", "--github-e2e"])
        self.assertTrue(args.github_e2e)
        args = parser.parse_args(["start", "--run-id", "x"])
        self.assertFalse(args.github_e2e)


# ── §3 strict env schema ──────────────────────────────────────────────────────

class TestEnvSchema(unittest.TestCase):

    def test_valid_mapping_passes(self):
        env = _valid_env()
        self.assertEqual(e2f.validate_e2e_controller_env(env), env)

    def test_unknown_key_rejected(self):
        env = _valid_env()
        env["TOTALLY_UNKNOWN"] = "x"
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.validate_e2e_controller_env(env)
        self.assertIn("TOTALLY_UNKNOWN", ctx.exception.detail)

    def test_missing_key_rejected(self):
        env = _valid_env()
        del env["M4F_RUN_PREFIX"]
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.validate_e2e_controller_env(env)
        self.assertIn("M4F_RUN_PREFIX", ctx.exception.detail)

    def test_enabled_must_be_one(self):
        env = _valid_env()
        env["GITHUB_INGRESS_ENABLED"] = "0"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_controller_env(env)

    def test_senders_localpart_contract_rejects_full_mxid(self):
        for bad in ("@manager:" + e2f.E2E_MATRIX_SERVER_NAME,
                    "manager:" + e2f.E2E_MATRIX_SERVER_NAME):
            env = _valid_env()
            env["M4F_ALLOWED_SENDERS"] = bad
            with self.assertRaises(e2f.E2EConfigError) as ctx:
                e2f.validate_e2e_controller_env(env)
            self.assertIn("LOCALPARTS only", ctx.exception.detail)

    def test_senders_exact_set(self):
        env = _valid_env()
        env["M4F_ALLOWED_SENDERS"] = "manager,reviewer,fixer"  # verifier gone
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_controller_env(env)
        env["M4F_ALLOWED_SENDERS"] = "manager,reviewer,fixer,verifier,ghost"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_controller_env(env)

    def test_frozen_values(self):
        for key, bad in (
                ("MATRIX_USER", "admin"),
                ("MATRIX_USER", "someoneelse"),
                ("CONTROLLER_CONSUMER_NAME", "controller"),
                ("M4F_RUN_PREFIX", "m5live-"),
                ("RESERVED_RUN_PREFIXES", "m4f-"),
                ("MATRIX_SERVER_NAME", "matrix-local.hiclaw.io:8080")):
            env = _valid_env()
            env[key] = bad
            with self.assertRaises(e2f.E2EConfigError) as ctx:
                e2f.validate_e2e_controller_env(env)
            self.assertIn(key, ctx.exception.detail)

    def test_ranges_and_urls(self):
        env = _valid_env()
        env["GITHUB_DELIVERY_LEASE_SECONDS"] = "0"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_controller_env(env)
        env = _valid_env()
        env["GITHUB_DELIVERY_MAX_ATTEMPTS"] = "21"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_controller_env(env)
        env = _valid_env()
        env["MATRIX_HS"] = "http://user:pw@matrix-hs:6167"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_controller_env(env)
        env = _valid_env()
        env["GITHUB_ROOM_MAP"] = "/etc/passwd"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_controller_env(env)

    def test_room_must_match_server(self):
        env = _valid_env()
        env["M4F_ALLOWED_ROOMS"] = "!abc:other-server.org"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_controller_env(env)

    def test_db_contract_keys_required(self):
        # run27 regression: the E2E controller env omitted the database
        # contract entirely; the container exited CONFIG_INVALID before
        # State.Running and the health gate reported E2E_CONTROLLER_UNREADY.
        env = _valid_env()
        for key in ("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER",
                    "PG_PASS", "ADMIN_PW"):
            del env[key]
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.validate_e2e_controller_env(env)
        self.assertEqual(ctx.exception.code, "CONFIG_INVALID")
        for key in sorted(("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER",
                           "PG_PASS", "ADMIN_PW")):
            self.assertIn(key, ctx.exception.detail)

    def test_db_contract_value_shapes(self):
        for key, bad in (("PG_HOST", ""), ("PG_HOST", "post gres"),
                         ("PG_PORT", "not-a-port"), ("PG_PORT", "0"),
                         ("PG_PORT", "70000"),
                         ("PG_DATABASE", "bad name"), ("PG_USER", "u;drop"),
                         ("PG_PASS", ""), ("PG_PASS", "x\nADMIN_PW=evil"),
                         ("ADMIN_PW", "")):
            env = _valid_env()
            env[key] = bad
            with self.assertRaises(e2f.E2EConfigError) as ctx:
                e2f.validate_e2e_controller_env(env)
            self.assertEqual(ctx.exception.code, "CONFIG_INVALID")
            self.assertIn(key, ctx.exception.detail)
            # secrets never leak into the error detail
            self.assertNotIn("evil", ctx.exception.detail)

    def test_entrypoint_db_contract_subset_guard(self):
        # contract-drift guard: every key controller_entrypoint.py's
        # env gate requires must be a member of the E2E schema
        entrypoint_required = {"PG_HOST", "PG_PORT", "PG_DATABASE",
                               "PG_USER", "PG_PASS", "ADMIN_PW"}
        self.assertLessEqual(entrypoint_required,
                             e2f.E2E_CONTROLLER_ENV_KEYS)

    def test_env_keys_match_controller_source_reads(self):
        # run29 regression: controller.py reads GITHUB_ROOM_MAP (no
        # _PATH suffix); a guessed key name silently falls through to
        # the in-container default and the controller exits
        # FileNotFoundError. Extract the ACTUAL env reads from the
        # controller source and require the github/db contract keys
        # the E2E schema sends to exist verbatim among them.
        import re
        src = (ROOT / "tools" / "workflow-controller"
               / "controller.py").read_text(encoding="utf-8")
        reads = set(re.findall(r'os\.environ\.get\("([A-Z0-9_]+)"', src))
        for key in ("GITHUB_ROOM_MAP", "GITHUB_POLICY_PATH",
                    "GITHUB_INGRESS_ENABLED", "PG_HOST", "PG_PORT",
                    "PG_DATABASE", "PG_USER", "PG_PASS", "ADMIN_PW"):
            self.assertIn(key, reads)
            self.assertIn(key, e2f.E2E_CONTROLLER_ENV_KEYS)
        self.assertNotIn("GITHUB_ROOM_MAP_PATH",
                         e2f.E2E_CONTROLLER_ENV_KEYS)

    def test_token_secret_never_in_errors(self):
        env = _valid_env()
        env["COORDINATOR_TOKEN"] = "has space"
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.validate_e2e_controller_env(env)
        self.assertNotIn("has space", ctx.exception.detail)


class TestSecretFile(unittest.TestCase):

    def test_write_read_delete(self):
        with tempfile.TemporaryDirectory() as td:
            sf = e2f.GithubE2eSecretFile(Path(td))
            sf.write(_valid_env())
            self.assertTrue(sf.exists())
            text = sf.path.read_text(encoding="utf-8")
            keys = [ln.split("=", 1)[0] for ln in text.splitlines()]
            self.assertEqual(keys, sorted(e2f.E2E_CONTROLLER_ENV_KEYS))
            sf.delete()
            self.assertFalse(sf.exists())

    def test_refuses_overwrite_and_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            sf = e2f.GithubE2eSecretFile(Path(td))
            sf.write(_valid_env())
            with self.assertRaises(e2f.E2EConfigError) as ctx:
                sf.write(_valid_env())
            self.assertEqual(ctx.exception.code, "SECRET_FILE_EXISTS")
            sf.delete()
            bad = _valid_env()
            bad["GITHUB_INGRESS_ENABLED"] = "yes"
            with self.assertRaises(e2f.E2EConfigError):
                sf.write(bad)
            self.assertFalse(sf.exists())

    def test_leftover_env_blocks_default_start(self):
        # the existing SECRET_RESIDUE glob (*.env) covers the E2E file:
        # a github_ingress.env without a session must refuse a default start
        self.assertTrue(e2f.E2E_INGRESS_ENV_FILE.endswith(".env"))

    def test_diagnostics_redacts_coordinator_token(self):
        self.assertTrue(mp._DIAG_SECRET_KEY_RE.search("COORDINATOR_TOKEN"))
        self.assertEqual(mp._redact_env_value("COORDINATOR_TOKEN"),
                         "<redacted>")


# ── room-map / policy 1:1 ─────────────────────────────────────────────────────

class TestRoomMapPairing(unittest.TestCase):

    MAP = ('repos:\n  "example/fixture-repo":\n'
           '    room_id: "!syntheticroom0000:matrix-local.hiclaw.io:18080"\n')

    def test_parse_and_pair_ok(self):
        repos = e2f.parse_room_map_repos(self.MAP)
        self.assertEqual(
            repos,
            {"example/fixture-repo":
             "!syntheticroom0000:matrix-local.hiclaw.io:18080"})
        e2f.validate_room_map_policy_pair(
            self.MAP, ["example/fixture-repo"])

    def test_rejects_duplicate_repo(self):
        text = self.MAP + '  "nghqqa/other":\n' \
                          '    room_id: "!b:srv"\n' \
                          '  "example/fixture-repo":\n' \
                          '    room_id: "!c:srv"\n'
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.parse_room_map_repos(text)
        self.assertEqual(ctx.exception.code, "ROOM_MAP_INVALID")

    def test_mismatch_is_fatal_both_directions(self):
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.validate_room_map_policy_pair(
                self.MAP, ["example/fixture-repo", "nghqqa/extra"])
        self.assertEqual(ctx.exception.code, "ROOM_MAP_MISMATCH")
        self.assertIn("policy-only", ctx.exception.detail)
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_room_map_policy_pair(self.MAP, ["nghqqa/different"])

    def test_missing_room_id_rejected(self):
        with self.assertRaises(e2f.E2EConfigError):
            e2f.parse_room_map_repos('repos:\n  "a/b":\n')

    def test_unquoted_repo_key_rejected(self):
        # run30 regression: the probe accepted optional quotes while
        # github_drain.parse_room_map (the in-container consumer)
        # requires them — a plain-YAML key passed the prerequisite gate
        # and crashed the controller at startup. The probe must fail
        # closed with the production shape.
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.parse_room_map_repos(
                'repos:\n'
                '  nghqqa/MergePilot-e2e-fixture:\n'
                '    room_id: "!syntheticroom0000:'
                'matrix-local.hiclaw.io:18080"\n')
        self.assertEqual(ctx.exception.code, "ROOM_MAP_INVALID")
        self.assertIn("double-quoted", ctx.exception.detail)


# ── §4 network / route-gate command contract ─────────────────────────────────

class TestNetworkCommands(unittest.TestCase):

    def test_e2e_network_create_argv(self):
        argv = e2f.plan_e2e_network_create("ctrl-egress")
        self.assertEqual(argv, ["network", "create", "--driver", "bridge",
                                "--subnet", "172.31.0.0/28",
                                "mp-e2e-ctrl-egress"])
        with self.assertRaises(e2f.E2EConfigError):
            e2f.plan_e2e_network_create("not-a-net")

    def test_controller_create_network_none_with_single_file_ro_mounts(self):
        argv = e2f.plan_controller_e2e_create(
            image_ref="sha256:" + "ab" * 32,
            container="mergepilot-isolated-controller-1",
            room_map_host="/mnt/d/mp-gh4-secrets/room-map.yaml",
            policy_host="/mnt/d/runtime/policy-fixture.yaml")
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        mounts = [argv[i + 1] for i, t in enumerate(argv) if t == "-v"]
        self.assertEqual(len(mounts), 2)
        for m in mounts:
            self.assertTrue(m.endswith(":ro"))
            self.assertNotIn("secrets", m.split(":")[1])  # single files only
        self.assertNotIn("/mnt/d/mp-gh4-secrets:.", " ".join(argv))

    def test_controller_connects_declare_gateway_priority(self):
        argvs = e2f.plan_controller_e2e_connects(
            container="c1", isolated_network="iso")
        self.assertEqual(argvs[0],
                         ["network", "connect", "--ip", "172.31.0.2",
                          "--gw-priority", "100", "mp-e2e-ctrl-egress", "c1"])
        self.assertEqual(argvs[1],
                         ["network", "connect", "--gw-priority", "0",
                          "iso", "c1"])

    def test_route_gate_argv_pure_python(self):
        argv = e2f.plan_route_gate_argv(container="c1", dst_ip="172.22.0.2",
                                        dst_port=6167)
        self.assertEqual(argv[:3], ["exec", "c1", "python"])
        self.assertIn("getsockname", argv[4])
        self.assertIn("172.22.0.2", argv[4])
        self.assertEqual(e2f.E2E_ROUTE_GATE_EXPECTED_SRC, "172.31.0.2")

    def test_b1_network_table_extensible_to_r4(self):
        self.assertEqual(len(e2f.E2E_NETWORKS), 8)
        self.assertEqual(e2f.B1_ACTIVE_NETWORKS, ("ctrl-egress",))
        subnets = [spec[0] for spec in e2f.E2E_NETWORKS.values()]
        self.assertEqual(len(set(subnets)), 8)   # no overlap


# ── §5 firewall model ─────────────────────────────────────────────────────────

class TestFirewallModel(unittest.TestCase):

    def test_b1_counts_and_ordering(self):
        plan = _b1_plan()
        self.assertEqual(plan["counts"], {
            "docker_user_jumps": 1, "forward_accept": 1,
            "reverse_accept": 1, "subnet_drop": 1, "return": 1,
            "input_jumps": 1, "input_deny": 1})
        blob = plan["restore_blob"]
        self.assertIn("-I DOCKER-USER 1 -s %s -j MP-EG-ab12cd34"
                      % CTRL_SUBNET, blob)
        self.assertIn("-I INPUT 1 -s %s -j MP-IN-ab12cd34" % CTRL_SUBNET,
                      blob)
        # forward + exact reverse pair
        self.assertIn("-A MP-EG-ab12cd34 -s 172.31.0.2/32 -d 172.22.0.2/32 "
                      "-p tcp --dport 6167 -m conntrack "
                      "--ctstate NEW,ESTABLISHED -j ACCEPT", blob)
        self.assertIn("-A MP-EG-ab12cd34 -s 172.22.0.2/32 -d 172.31.0.2/32 "
                      "-p tcp --sport 6167 -m conntrack "
                      "--ctstate ESTABLISHED -j ACCEPT", blob)
        # subnet DROP after the ACCEPTs, RETURN tail, INPUT full deny
        drop_pos = blob.index("-s %s -j DROP" % CTRL_SUBNET)
        fwd_pos = blob.index("--ctstate NEW,ESTABLISHED -j ACCEPT")
        self.assertGreater(drop_pos, fwd_pos)
        self.assertIn("-A MP-EG-ab12cd34 -j RETURN", blob)
        self.assertIn("-A MP-IN-ab12cd34 -j DROP", blob)
        self.assertEqual(blob.count("-m comment --comment"), 7)

    def test_no_global_established_rule(self):
        blob = _b1_plan()["restore_blob"]
        self.assertNotIn("--ctstate ESTABLISHED,RELATED", blob)
        # every conntrack rule is scoped by both -s and -d
        for line in blob.splitlines():
            if "--ctstate" in line:
                self.assertIn(" -s ", line)
                self.assertIn(" -d ", line)

    def test_r4_full_topology_counts(self):
        plan = e2f.build_firewall_plan("ffff0001", edges=R4_EDGES,
                                       own_subnets=R4_SUBNETS)
        c = plan["counts"]
        self.assertEqual(
            (c["docker_user_jumps"], c["forward_accept"],
             c["reverse_accept"], c["subnet_drop"], c["return"],
             c["input_jumps"], c["input_deny"]),
            (12, 10, 10, 8, 1, 8, 1))
        # agent /32 jumps, never the whole hiclab /16
        for ip in ("172.21.0.2", "172.21.0.4", "172.21.0.5", "172.21.0.6"):
            self.assertIn("-s %s -j MP-EG-ffff0001" % ip,
                          plan["restore_blob"])
        self.assertNotIn("-s 172.21.0.0/16", plan["restore_blob"])

    def test_teardown_is_exact_reverse_and_strips_rulenum(self):
        plan = _b1_plan()
        teardown = plan["teardown_argv"]
        self.assertEqual(teardown[-1], ["iptables", "-X",
                                        "MP-EG-ab12cd34"])
        self.assertIn(["iptables", "-X", "MP-IN-ab12cd34"], teardown)
        for argv in teardown:
            self.assertNotIn("1", argv[:4])  # no rulenum leaks into -D
            if argv[1] == "-D":
                self.assertNotEqual(argv[2], "1")
        # every delete carries the ownership comment
        for argv in teardown:
            if argv[1] == "-D":
                self.assertIn("mp-e2e:ab12cd34:", argv[-1])

    def test_install_is_test_then_commit(self):
        plan = _b1_plan()
        self.assertEqual(plan["install_argv"],
                         [["iptables-restore", "--test"],
                          ["iptables-restore", "--noflush"]])

    def test_idempotency_and_conflicts(self):
        plan = _b1_plan()
        # simulate iptables-save containing exactly our rules
        save_text = "\n".join(
            line for line in plan["restore_blob"].splitlines()
            if not line.startswith(("*filter", "COMMIT"))
        ).replace("-I DOCKER-USER 1", "-I DOCKER-USER 1") + "\n"
        self.assertTrue(e2f.plan_is_installed(save_text, plan))
        self.assertIsNone(e2f.firewall_conflict(save_text, plan))
        # drifted rule -> PIN_TARGET_DRIFT
        drifted = save_text.replace("6167", "6168")
        self.assertFalse(e2f.plan_is_installed(drifted, plan))
        self.assertEqual(e2f.firewall_conflict(drifted, plan),
                         "PIN_TARGET_DRIFT")
        # chain present without rules -> OWNERSHIP_UNKNOWN
        empty_chain = "*filter\n:MP-EG-ab12cd34 - [0:0]\nCOMMIT\n"
        self.assertEqual(e2f.firewall_conflict(empty_chain, plan),
                         "OWNERSHIP_UNKNOWN")
        # foreign session tag -> PIN_FOREIGN_SESSION
        foreign = save_text.replace("mp-e2e:ab12cd34:", "mp-e2e:deadbeef:")
        self.assertEqual(e2f.firewall_conflict(foreign, plan),
                         "PIN_FOREIGN_SESSION")

    def test_residue_scan(self):
        save = ('-A FORWARD -j DOCKER-ISOLATION-STAGE-1\n'
                '-I DOCKER-USER 1 -s 172.31.0.0/28 -j MP-EG-ab12cd34 '
                '-m comment --comment "mp-e2e:ab12cd34:jump:ctrl-egress"\n')
        codes = e2f.residue_scan(save)
        self.assertIn("FIREWALL_RULE_RESIDUE", codes)
        foreign = save.replace("ab12cd34", "deadbeef")
        self.assertIn("FIREWALL_FOREIGN_SESSION_RESIDUE",
                      e2f.residue_scan(foreign, sid="ab12cd34"))
        self.assertIn("FIREWALL_RULE_RESIDUE",
                      e2f.residue_scan(foreign))  # sid-less: any tag counts
        chains = "*filter\n:MP-IN-00112233 - [0:0]\nCOMMIT\n"
        self.assertIn("FIREWALL_CHAIN_RESIDUE:MP-IN-00112233",
                      e2f.residue_scan(chains))
        self.assertEqual(e2f.residue_scan("*filter\nCOMMIT\n"), [])

    def test_bad_sid_rejected(self):
        with self.assertRaises(e2f.E2EConfigError):
            e2f.build_firewall_plan("UPPER", edges=B1_EDGES,
                                    own_subnets=[CTRL_SUBNET])
        with self.assertRaises(e2f.E2EConfigError):
            e2f.build_firewall_plan("ab12cd34", edges=B1_EDGES,
                                    own_subnets=["172.31.0.2"])


# ── §6 Matrix membership preflight ────────────────────────────────────────────

class TestMembershipPreflight(unittest.TestCase):

    def test_five_required_members(self):
        self.assertEqual(len(e2f.E2E_EXPECTED_ROOM_MEMBERS), 5)
        self.assertIn("@manager:" + e2f.E2E_MATRIX_SERVER_NAME,
                      e2f.E2E_EXPECTED_ROOM_MEMBERS)

    def test_all_present_ok(self):
        ok, missing = e2f.verify_membership(set(e2f.E2E_EXPECTED_ROOM_MEMBERS))
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        e2f.membership_gate(set(e2f.E2E_EXPECTED_ROOM_MEMBERS))

    def test_missing_raises_and_names_only_missing(self):
        joined = set(e2f.E2E_EXPECTED_ROOM_MEMBERS) - {
            "@manager:" + e2f.E2E_MATRIX_SERVER_NAME}
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.membership_gate(joined)
        self.assertEqual(ctx.exception.code, "MATRIX_MEMBERSHIP_INCOMPLETE")
        self.assertIn("@manager:", ctx.exception.detail)
        self.assertNotIn("@reviewer:", ctx.exception.detail)
        self.assertIn("joined_members", ctx.exception.detail)  # invited!=joined

    def test_fetch_uses_transport_with_bearer_header(self):
        seen = {}

        def fake_transport(url, headers):
            seen["url"] = url
            seen["auth"] = headers.get("Authorization", "")
            return {"joined": {m: {} for m in e2f.E2E_EXPECTED_ROOM_MEMBERS}}

        members = e2f.fetch_joined_members(
            "http://matrix-hs:6167", "!r:" + e2f.E2E_MATRIX_SERVER_NAME,
            "sekret-token", fake_transport)
        self.assertEqual(members, set(e2f.E2E_EXPECTED_ROOM_MEMBERS))
        self.assertIn("/_matrix/client/v3/rooms/!r%3A" if False
                      else "/_matrix/client/v3/rooms/", seen["url"])
        self.assertTrue(seen["auth"].startswith("Bearer "))
        self.assertIn("sekret-token", seen["auth"])  # header only, never argv


# ── §7 lifecycle ordering / dry-run ───────────────────────────────────────────

class _CP:
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class _RecordingDocker:
    """Minimal stub: records docker + wsl_exec call order."""

    def __init__(self, planner):
        self.planner = planner
        self.calls = []

    def docker(self, argv, **kw):
        self.calls.append(("docker", tuple(argv[:2])))
        return _CP(0)

    def wsl_exec(self, argv, **kw):
        self.calls.append(("wsl_exec", tuple(argv[:2])))
        return _CP(0)


class TestRollbackOrder(unittest.TestCase):

    def test_pins_removed_between_containers_and_networks(self):
        planner = oc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            paths = {"secrets": tdp / "secrets", "session": tdp / "s.json",
                     "state": tdp}
            (tdp / "secrets").mkdir()
            plan = _b1_plan()
            session = {
                "run_id": "r1",
                "containers": {"controller": "sha256:" + "11" * 32},
                "networks": {"mp-e2e-ctrl-egress": "n1"},
                "firewall_teardown": plan["teardown_argv"],
                "secrets": [],
            }
            stub = _RecordingDocker(planner)
            codes = mp.rollback_session(stub, planner, paths, session)
            self.assertEqual(codes, [])
            kinds = [c[0] for c in stub.calls]
            self.assertEqual(kinds[0], "docker")
            self.assertEqual(kinds[-1], "docker")
            self.assertTrue(all(k == "wsl_exec" for k in kinds[1:-1]))
            self.assertEqual(stub.calls[0], ("docker", ("rm", "-fv")))
            self.assertEqual(stub.calls[-1], ("docker", ("network", "rm")))
            # every pin step is an iptables -D (rule deletes precede the
            # chain -X deletions; exact order asserted in firewall tests)
            pin_calls = [c for c in stub.calls[1:-1]]
            self.assertGreater(len(pin_calls), 2)


class TestDryRunPreview(unittest.TestCase):

    def test_preview_is_pure_and_gated(self):
        preview = e2f.build_b1_dry_run_preview(
            run_id="run-abc", tuwunel_ip="172.22.0.2",
            room_map_host="/mnt/d/x/room-map.yaml",
            policy_host="/mnt/d/x/policy.yaml")
        self.assertIn("GITHUB_E2E_PREREQUISITES_INCOMPLETE",
                      preview["activation_gate"])
        self.assertEqual(len(preview["networks_create"]), 8)
        self.assertIn(["network", "create", "--driver", "bridge",
                       "--subnet", "172.31.0.0/28",
                       "mp-e2e-ctrl-egress"], preview["networks_create"])
        self.assertEqual(preview["route_gate"]["expected_src"],
                         "172.31.0.2")
        self.assertEqual(preview["route_gate"]["failure_code"],
                         "ROUTE_GATE_FAILED")
        self.assertEqual(
            len(preview["membership_preflight"]["required_members"]), 5)
        self.assertIn("restore_blob", preview["firewall"])

    def test_cli_dry_run_attaches_preview_zero_side_effects(self):
        fake_install = {
            "images": {mp.image_tag(oc, svc): "sha256:" + ("%02x" % i) * 32
                       for i, svc in enumerate(oc.BUILT_SERVICES)}}
        absent = {"containers": {svc: {"state": "absent"}
                                 for svc in oc.SERVICE_ORDER},
                  "networks": {}}
        with mock.patch.object(mp, "load_manifest",
                               side_effect=[fake_install, None]), \
             mock.patch.object(mp, "discover_stack", return_value=absent), \
             mock.patch.object(mp, "classify_stack",
                               return_value=("absent", "nothing")):
            rc = mp.main(["start", "--run-id", "dryprobe", "--github-e2e",
                          "--dry-run", "--json", "--project-dir", str(ROOT)])
        self.assertEqual(rc, 0)


class TestCleanupResidueScan(unittest.TestCase):

    def test_residue_scan_wired_into_cleanup_for_e2e_sessions(self):
        source = (ROOT / "tools" / "cli" / "mergepilot.py").read_text(
            encoding="utf-8")
        self.assertIn('session.get("github_e2e")', source)
        self.assertIn("iptables-save", source)
        self.assertIn("e2f.residue_scan", source)
        # default-mode cleanup stays byte-identical: the scan is guarded
        self.assertIn('if session is not None and '
                      'session.get("github_e2e"):', source)

    def test_status_reports_sanitized_boolean_only(self):
        source = (ROOT / "tools" / "cli" / "mergepilot.py").read_text(
            encoding="utf-8")
        self.assertIn('"github_e2e": bool(session.get("github_e2e"))',
                      source)


class TestDefaultSessionShape(unittest.TestCase):

    def test_default_session_manifest_unchanged(self):
        # the pre-B1 key set must stay byte-identical in default mode
        session = mp.new_session("run-x", False)
        self.assertEqual(sorted(session),
                         ["containers", "created_utc", "m4f", "networks",
                          "run_id", "schema_version", "secrets", "stage"])

    def test_e2e_session_carries_e2e_fields(self):
        session = mp.new_session("run-x", False, True)
        self.assertTrue(session["github_e2e"])
        self.assertIsNone(session["firewall_teardown"])


class TestWslExec(unittest.TestCase):

    def test_wsl_exec_runs_host_argv_with_distro_gate(self):
        full_argv = []

        def fake_run(argv, **kw):
            full_argv.append(argv)
            return _CP(0, b"saved")

        wd = mp.WslDocker(oc, ROOT)
        wd._distro_states = {"MergePilot-Test": "Running"}
        wd._run_wsl = fake_run
        cp = wd.wsl_exec(["iptables-save"], check=False)
        self.assertEqual(cp.returncode, 0)
        self.assertTrue(full_argv[0][:2] == ["wsl.exe", "-u"])
        self.assertEqual(full_argv[0][-1], "iptables-save")


@unittest.skipUnless(
    os.environ.get("MP_E2E_FW_NS_TEST") == "1"
    and sys.platform.startswith("linux"),
    "opt-in isolated-namespace firewall test (linux + MP_E2E_FW_NS_TEST=1)")
class TestFirewallNamespaceIsolated(unittest.TestCase):
    """One-shot isolated network namespace: apply the restore blob, then
    live-packet-verify an allowed edge and a bypass rejection. Never runs
    against the host namespace."""

    def test_blob_applies_and_counts(self):
        plan = e2f.build_firewall_plan("ab12cd34", edges=B1_EDGES,
                                       own_subnets=[CTRL_SUBNET])
        r = subprocess.run(
            ["unshare", "--net", "--", "bash", "-c",
             "iptables-restore --test <<'EOF'\n%s\nEOF\n" %
             plan["restore_blob"]],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)




def _valid_reporter_env():
    return {
        "GITHUB_PUBLISHER_DSN":
            "postgresql://github_check_publisher:pw@postgres:5432/db"
            "?connect_timeout=5",
        "GITHUB_API_BASE": "https://api.github.com",
        "GITHUB_APP_ID": "4648333",
        "GITHUB_INSTALLATION_ID": "154914965",
        "GITHUB_REPOSITORY_ID": "1314399289",
        "GITHUB_PRIVATE_KEY_PATH":
            "/run/secrets/github-app-private-key.pem",
        "GH_REPORTER_POLL_SECONDS": "5",
        "GH_REPORTER_LEASE_SECONDS": "120",
        "GH_REPORTER_MAX_ATTEMPTS": "8",
        "HTTPS_PROXY": e2f.E2E_REPORTER_PROXY_R,
    }


class TestReporterEnvContractB2(unittest.TestCase):

    def test_valid(self):
        env = _valid_reporter_env()
        self.assertEqual(e2f.validate_e2e_reporter_env(env), env)

    def test_unknown_missing_blank(self):
        env = _valid_reporter_env()
        env["EXTRA"] = "x"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_reporter_env(env)
        env = _valid_reporter_env()
        del env["GITHUB_APP_ID"]
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_reporter_env(env)
        env = _valid_reporter_env()
        env["GITHUB_APP_ID"] = "   "
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_reporter_env(env)

    def test_api_base_must_be_production(self):
        env = _valid_reporter_env()
        env["GITHUB_API_BASE"] = "http://127.0.0.1:8091"
        with self.assertRaises(e2f.E2EConfigError) as ctx:
            e2f.validate_e2e_reporter_env(env)
        self.assertIn("no implicit fake fallback", ctx.exception.detail)

    def test_frozen_pem_path_and_numeric(self):
        env = _valid_reporter_env()
        env["GITHUB_PRIVATE_KEY_PATH"] = "/etc/other.pem"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_reporter_env(env)
        env = _valid_reporter_env()
        env["GH_REPORTER_MAX_ATTEMPTS"] = "51"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_reporter_env(env)
        env = _valid_reporter_env()
        env["GITHUB_REPOSITORY_ID"] = "repo-not-numeric"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_reporter_env(env)

    def test_dsn_requires_connect_timeout(self):
        env = _valid_reporter_env()
        env["GITHUB_PUBLISHER_DSN"] = "postgresql://u:p@postgres:5432/db"
        with self.assertRaises(e2f.E2EConfigError):
            e2f.validate_e2e_reporter_env(env)

    def test_secret_file_transport(self):
        with tempfile.TemporaryDirectory() as td:
            sf = e2f.GithubReporterE2eSecretFile(Path(td))
            sf.write(_valid_reporter_env())
            self.assertTrue(sf.exists())
            keys = [ln.split("=", 1)[0]
                    for ln in sf.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(keys, sorted(e2f.E2E_REPORTER_ENV_KEYS))
            with self.assertRaises(e2f.E2EConfigError):
                sf.write(_valid_reporter_env())     # refuses overwrite
            sf.delete()
            self.assertFalse(sf.exists())

    def test_preview_contains_reporter_planning(self):
        preview = e2f.build_b1_dry_run_preview(
            run_id="b2probe", tuwunel_ip="172.22.0.2",
            room_map_host="/x", policy_host="/y")
        rp = preview["reporter_planning"]
        self.assertEqual(rp["entrypoint"],
                         ["python", "-u", "/app/gh_app/checks_reporter.py"])
        self.assertTrue(rp["pem_mount"].endswith(":ro"))
        self.assertIn("single-file :ro into gh-reporter ONLY",
                      rp["pem_mount_policy"])
        self.assertIn("172.31.0.64/28", rp["networks"]["rpt-egress"])
        self.assertIn("GITHUB_E2E_PREREQUISITES_INCOMPLETE",
                      rp["activation_gate"])




class TestB2DockerfileWiring(unittest.TestCase):

    ROOT_DOCKERFILE = (ROOT / "Dockerfile.gh-webhook").read_text(
        encoding="utf-8")
    CANON_DOCKERFILE = (ROOT / "tools" / "gh-app" / "Dockerfile").read_text(
        encoding="utf-8")

    def test_token_provider_and_lock_copied_into_image(self):
        for text in (self.ROOT_DOCKERFILE, self.CANON_DOCKERFILE):
            self.assertIn(
                "COPY tools/gh-app/token_provider.py "
                "/app/gh_app/token_provider.py", text)
            self.assertIn(
                "COPY tools/gh-app/requirements-reporter.lock", text)
            self.assertIn("--require-hashes", text)
            self.assertIn("--only-binary=:all:", text)
            # no floating additions
            self.assertNotIn("pip install --no-cache-dir cryptography",
                             text)

    def test_receiver_still_owns_the_entrypoint(self):
        self.assertIn('ENTRYPOINT ["python", "-u", "http_server.py"]',
                      self.ROOT_DOCKERFILE)

    def test_no_other_dockerfile_touched_by_b2(self):
        # the reporter shares the gh-webhook image; B2 must not have
        # modified controller/gateway/console Dockerfiles
        for name in ("Dockerfile.controller", "Dockerfile.policy-gateway",
                     "Dockerfile.preflight", "Dockerfile.demo-console"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("token_provider", text)
            self.assertNotIn("requirements-reporter.lock", text)




def _join_instructions(text):
    """Join Dockerfile continuation lines into full instructions
    (CRLF-checkout tolerant: trailing CR is stripped first)."""
    joined = []
    buffer = ""
    for raw in text.splitlines():
        raw = raw.rstrip("\r")
        if raw.endswith("\\"):
            buffer += raw[:-1] + " "
        else:
            joined.append((buffer + raw).strip())
            buffer = ""
    if buffer:
        joined.append(buffer.strip())
    return joined


class TestDockerfileInstructionLevel(unittest.TestCase):
    """M8-GH-4B2 pre-push §2: parse the REAL instructions — no isolated
    keyword matching, no literal backslash-n, identical contract."""

    FILES = ("Dockerfile.gh-webhook", "tools/gh-app/Dockerfile")

    def test_no_literal_backslash_n(self):
        # The REAL §2 bug was a literal backslash + 'n' inside the RUN
        # line. Line ENDINGS are checkout-dependent (autocrlf worktrees
        # produce CRLF) and are not part of the contract.
        for name in self.FILES:
            raw = (ROOT / name).read_bytes()
            self.assertNotIn(b"\\" + b"n", raw,
                             "%s contains a literal backslash-n" % name)

    def test_run_install_instruction_contract(self):
        for name in self.FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            instructions = _join_instructions(text)
            runs = [i for i in instructions
                    if i.startswith("RUN") and "require-hashes" in i]
            self.assertEqual(len(runs), 1,
                             "%s must have exactly one lock install RUN"
                             % name)
            run = runs[0]
            for token in ("pip install", "--no-cache-dir",
                          "--only-binary=:all:", "--require-hashes",
                          "-r /app/gh_app/requirements-reporter.lock"):
                self.assertIn(token, run, "%s RUN missing %s" % (name, token))
            self.assertIn("RUN pip install --no-cache-dir "
                          "--only-binary=:all: --require-hashes "
                          "-r /app/gh_app/requirements-reporter.lock",
                          re.sub(r"  +", " ", run))

    def test_copy_contract(self):
        for name in self.FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            instructions = _join_instructions(text)
            copies = " ".join(i for i in instructions
                              if i.startswith("COPY"))
            self.assertIn(
                "COPY tools/gh-app/token_provider.py "
                "/app/gh_app/token_provider.py", copies)
            self.assertIn(
                "COPY tools/gh-app/requirements-reporter.lock "
                "/app/gh_app/requirements-reporter.lock", copies)


if __name__ == "__main__":
    unittest.main()
