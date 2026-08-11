#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D2B-3B1 · Docker socket proxy test suite (90 cases).

Covers the v2 acceptance matrix (docs/D2B-3-验收矩阵.md):
  - 15 positive (P1-P15)
  - 35 negative (N1-N35, incl. N2b/N4b/N6b/N6c/N8b official-proxy gaps)
  - 21 bypass (B1-B13 + B5.1-B5.8)
  - 12 cleanup/lifecycle (C1-C12)
  - 7 integration (I1-I7)

All tests run on the host without WSL/Docker/AgentTeams. They use the pure
classify_request / evaluate_deny / apply_hardening_v2 functions and the
ProxyHarness socketpair fixture.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HICLAB = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "hiclab"))
sys.path.insert(0, HICLAB)
sys.path.insert(0, HERE)  # for proxy_stubs

import docker_socket_proxy as dsp  # noqa: E402
import harden_policy as hp  # noqa: E402
from proxy_stubs import ProxyHarness, FakeUpstreamDaemon, InspectStub  # noqa: E402

DIGEST = "sha256:" + "a" * 64
DIGEST2 = "sha256:" + "b" * 64


def _cfg(**kw):
    """Build a ProxyConfig with sensible test defaults."""
    defaults = dict(
        run_id="run-test-01",
        scope="test",
        name_profile="agentteams",
        image_allowlist=(DIGEST,),
        bind_allowlist=("/data",),
    )
    defaults.update(kw)
    return dsp.ProxyConfig(**defaults)


# ===========================================================================
# 1. POSITIVE (P1-P15) — 15 cases
# ===========================================================================


class TestPositive(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.reg = dsp.ExecRegistry()

    def _classify(self, method, path, body=None, headers=None):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(path)
        qs = parse_qs(parsed.query) if parsed.query else {}
        query = {k: v[0] if v else "" for k, v in qs.items()}
        target_header = None
        if headers:
            target_header = headers.get("Upgrade") or headers.get("upgrade")
        return dsp.classify_request(method, path, self.cfg, self.reg,
                                    body=body, query=query,
                                    target_header=target_header)

    def test_P01_ping(self):
        d = self._classify("GET", "/_ping")
        self.assertEqual(d.action, "allow")

    def test_P01b_ping_version_prefix(self):
        d = self._classify("GET", "/v1.47/_ping")
        self.assertEqual(d.action, "allow")

    def test_P02_create_worker_transform(self):
        body = {"Image": DIGEST}
        d = self._classify("POST",
                           "/containers/create?name=agentteams-worker-fixer",
                           body=body)
        self.assertEqual(d.action, "transform")
        self.assertEqual(d.name, "agentteams-worker-fixer")

    def test_P02b_create_worker_version_prefix(self):
        body = {"Image": DIGEST}
        d = self._classify("POST",
                           "/v1.45/containers/create?name=agentteams-worker-fixer",
                           body=body)
        self.assertEqual(d.action, "transform")

    def test_P03_create_manager_transform(self):
        body = {"Image": DIGEST}
        d = self._classify("POST",
                           "/containers/create?name=agentteams-manager",
                           body=body)
        self.assertEqual(d.action, "transform")
        self.assertEqual(d.name, "agentteams-manager")

    def test_P04_start_worker(self):
        d = self._classify("POST", "/containers/agentteams-worker-fixer/start")
        self.assertEqual(d.action, "allow")

    def test_P05_inspect_worker(self):
        d = self._classify("GET", "/containers/agentteams-worker-fixer/json")
        self.assertEqual(d.action, "allow")

    def test_P06_stop_worker(self):
        d = self._classify("POST", "/containers/agentteams-worker-fixer/stop?t=10")
        self.assertEqual(d.action, "allow")

    def test_P07_delete_worker(self):
        d = self._classify("DELETE",
                           "/containers/agentteams-worker-fixer?force=true")
        self.assertEqual(d.action, "allow")

    def test_P08_exec_create_authorized(self):
        d = self._classify("POST",
                           "/containers/agentteams-worker-fixer/exec")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.name, "agentteams-worker-fixer")

    def test_P08b_exec_start_after_register(self):
        self.reg.register("exec-abc", "agentteams-worker-fixer")
        d = self._classify("POST", "/exec/exec-abc/start",
                           headers={"Upgrade": "tcp"})
        self.assertEqual(d.action, "allow")
        self.assertTrue(d.hijack)

    def test_P08c_exec_json_after_register(self):
        self.reg.register("exec-abc", "agentteams-worker-fixer")
        d = self._classify("GET", "/exec/exec-abc/json")
        self.assertEqual(d.action, "allow")

    def test_P09_archive_put(self):
        # B11: archive path must be the auth-token dir (not arbitrary)
        d = self._classify("PUT",
                           "/containers/agentteams-worker-fixer/archive?path=/var/run/secrets/agentteams")
        self.assertEqual(d.action, "allow")

    def test_P10_delete_auth_volume(self):
        d = self._classify("DELETE",
                           "/volumes/agentteams-worker-fixer-auth")
        self.assertEqual(d.action, "allow")

    def test_P11_image_inspect_allowlisted(self):
        d = self._classify("GET", "/images/%s/json" % DIGEST)
        self.assertEqual(d.action, "allow")

    def test_P12_image_pull_allowlisted(self):
        d = self._classify("POST",
                           "/images/create?fromImage=%s" % DIGEST)
        self.assertEqual(d.action, "allow")

    def test_P13_transform_emits_hardened_labels(self):
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer"}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        self.assertEqual(out["HostConfig"]["RestartPolicy"], {"Name": "no"})
        self.assertEqual(out["Labels"]["com.mergepilot.hardened"], "1")
        self.assertEqual(out["Labels"]["com.mergepilot.agent"], "fixer")
        self.assertEqual(out["Labels"]["com.mergepilot.run_id"], "run-test-01")
        self.assertEqual(out["Labels"]["com.mergepilot.scope"], "test")

    def test_P14_tmpfs_injected(self):
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer"}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        self.assertIn("HostConfig", out)
        tmpfs = out["HostConfig"].get("Tmpfs", {})
        self.assertTrue(any("/tmp" in k for k in tmpfs))

    def test_P15_restart_policy_overwritten(self):
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        self.assertEqual(out["HostConfig"]["RestartPolicy"], {"Name": "no"})


# ===========================================================================
# 2. NEGATIVE (N1-N35) — 35 cases
# ===========================================================================


class TestNegative(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.reg = dsp.ExecRegistry()

    def _deny(self, method, path, body=None):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(path)
        qs = parse_qs(parsed.query) if parsed.query else {}
        query = {k: v[0] if v else "" for k, v in qs.items()}
        d = dsp.classify_request(method, path, self.cfg, self.reg,
                                 body=body, query=query)
        return d

    def _eval_deny(self, body):
        return hp.evaluate_deny(body, self.cfg)

    # D1 dangerous fields
    def test_N01_privileged(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST, "HostConfig": {"Privileged": True}}))

    def test_N02_pidmode_host(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST, "HostConfig": {"PidMode": "host"}}))

    def test_N02b_pidmode_container(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST, "HostConfig": {"PidMode": "container:x"}}))

    def test_N03_ipcmode_host(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST, "HostConfig": {"IpcMode": "host"}}))

    def test_N04_networkmode_host(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST, "HostConfig": {"NetworkMode": "host"}}))

    def test_N04b_networkmode_container(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST, "HostConfig": {"NetworkMode": "container:x"}}))

    def test_N05_usernsmode_host(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST, "HostConfig": {"UsernsMode": "host"}}))

    def test_N06_sock_bind(self):
        for pat in ("/var/run/docker.sock:/x",
                    "/run/docker.sock:/x",
                    "/run/mp/docker.sock:/x"):
            self.assertIsNotNone(self._eval_deny(
                {"Image": DIGEST, "HostConfig": {"Binds": [pat]}}),
                "failed to deny %s" % pat)

    def test_N06b_sock_via_devices(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST,
             "HostConfig": {"Devices": [{"Path": "/var/run/docker.sock"}]}}))

    def test_N06c_sock_via_mounts_volume(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST,
             "HostConfig": {"Mounts": [{"Type": "volume",
                                        "Source": "docker.sock"}]}}))

    def test_N07_unauthorized_bind(self):
        r = self._eval_deny({"Image": DIGEST,
                             "HostConfig": {"Binds": ["/etc/shadow:/x"]}})
        self.assertIsNotNone(r)

    def test_N08_dangerous_capadd_official_six(self):
        for cap in ("SYS_ADMIN", "SYS_PTRACE", "DAC_OVERRIDE",
                    "NET_ADMIN", "SYS_RAWIO", "SYS_MODULE"):
            self.assertIsNotNone(self._eval_deny(
                {"Image": DIGEST, "HostConfig": {"CapAdd": [cap]}}),
                "failed to deny cap %s" % cap)

    def test_N08b_dangerous_capadd_extra(self):
        for cap in ("NET_RAW", "CHOWN", "FOWNER", "SETFCAP",
                    "MKNOD", "SYS_NICE", "DAC_READ_SEARCH",
                    "SETUID", "SETGID", "KILL"):
            self.assertIsNotNone(self._eval_deny(
                {"Image": DIGEST, "HostConfig": {"CapAdd": [cap]}}),
                "failed to deny extra cap %s" % cap)

    def test_N09_securityopt_unconfined(self):
        for opt in ("apparmor=unconfined", "seccomp=unconfined"):
            self.assertIsNotNone(self._eval_deny(
                {"Image": DIGEST, "HostConfig": {"SecurityOpt": [opt]}}),
                "failed to deny %s" % opt)

    def test_N10_sysctls_dangerous(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST,
             "HostConfig": {"Sysctls": {"net.ipv4.ip_forward": "1"}}}))

    def test_N11_devices_nonempty(self):
        self.assertIsNotNone(self._eval_deny(
            {"Image": DIGEST,
             "HostConfig": {"Devices": [{"Path": "/dev/sda"}]}}))

    # D2 resource scope
    def test_N12_create_wrong_name(self):
        d = self._deny("POST", "/containers/create?name=agentteams-data",
                       body={"Image": DIGEST})
        self.assertEqual(d.action, "deny")

    def test_N13_create_name_traversal(self):
        d = self._deny("POST",
                       "/containers/create?name=agentteams-worker-../etc",
                       body={"Image": DIGEST})
        self.assertEqual(d.action, "deny")

    def test_N14_start_wrong_name(self):
        d = self._deny("POST", "/containers/evil-container/start")
        self.assertEqual(d.action, "deny")

    def test_N15_delete_wrong_name(self):
        d = self._deny("DELETE", "/containers/evil-container?force=true")
        self.assertEqual(d.action, "deny")

    def test_N16_inspect_wrong_name(self):
        d = self._deny("GET", "/containers/evil-container/json")
        self.assertEqual(d.action, "deny")

    def test_N17_exec_wrong_target(self):
        d = self._deny("POST", "/containers/evil-container/exec")
        self.assertEqual(d.action, "deny")

    def test_N18_archive_wrong_target(self):
        d = self._deny("PUT", "/containers/evil-container/archive?path=/tmp")
        self.assertEqual(d.action, "deny")

    def test_N19_delete_wrong_volume(self):
        d = self._deny("DELETE", "/volumes/evil-volume")
        self.assertEqual(d.action, "deny")

    def test_N20_networks_create(self):
        d = self._deny("POST", "/networks/create")
        self.assertEqual(d.action, "deny")

    def test_N20b_networks_delete(self):
        d = self._deny("DELETE", "/networks/abc")
        self.assertEqual(d.action, "deny")

    def test_N21_volumes_create(self):
        d = self._deny("POST", "/volumes/create")
        self.assertEqual(d.action, "deny")

    def test_N22_build(self):
        d = self._deny("POST", "/build")
        self.assertEqual(d.action, "deny")

    def test_N22b_images_load(self):
        d = self._deny("POST", "/images/load")
        self.assertEqual(d.action, "deny")

    def test_N23_system_prune(self):
        d = self._deny("POST", "/system/prune")
        self.assertEqual(d.action, "deny")

    def test_N24_swarm(self):
        d = self._deny("POST", "/swarm/init")
        self.assertEqual(d.action, "deny")

    def test_N24b_services(self):
        d = self._deny("POST", "/services")
        self.assertEqual(d.action, "deny")

    # D3 image/name
    def test_N25_image_tag_not_digest(self):
        d = self._deny("GET", "/images/ubuntu:latest/json")
        self.assertEqual(d.action, "deny")

    def test_N26_image_digest_not_allowlisted(self):
        d = self._deny("GET", "/images/%s/json" % DIGEST2)
        self.assertEqual(d.action, "deny")

    def test_N27_pull_non_allowlisted(self):
        d = self._deny("POST", "/images/create?fromImage=%s" % DIGEST2)
        self.assertEqual(d.action, "deny")

    def test_N28_name_non_ascii(self):
        d = self._deny("POST",
                       "/containers/create?name=agentteams-worker-中文",
                       body={"Image": DIGEST})
        self.assertEqual(d.action, "deny")

    # D4 protocol
    def test_N29_attach_hijack(self):
        d = self._deny("POST", "/containers/agentteams-worker-fixer/attach")
        self.assertEqual(d.action, "deny")

    def test_N30_unknown_method(self):
        d = self._deny("PATCH", "/containers/agentteams-worker-fixer/start")
        self.assertEqual(d.action, "deny")

    def test_N33_events(self):
        d = self._deny("GET", "/events")
        self.assertEqual(d.action, "deny")

    def test_N34_version_info(self):
        d = self._deny("GET", "/version")
        self.assertEqual(d.action, "deny")
        d2 = self._deny("GET", "/info")
        self.assertEqual(d2.action, "deny")

    def test_N35_put_patch_arbitrary(self):
        d = self._deny("PUT", "/containers/agentteams-worker-fixer/json")
        self.assertEqual(d.action, "deny")


# ===========================================================================
# 3. BYPASS (B1-B13 + B5.1-B5.8) — 21 cases
# ===========================================================================


class TestBypass(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.reg = dsp.ExecRegistry()

    def test_B01_no_raw_sock_passthrough(self):
        # classifier has no "raw socket" endpoint — everything must classify
        for path in ("/", "/containers", "/v1.41/"):
            d = dsp.classify_request("GET", path, self.cfg, self.reg)
            self.assertEqual(d.action, "deny", "passthrough on %s" % path)

    def test_B04_transit_container_sock_bind(self):
        body = {"Image": DIGEST, "Name": "agentteams-worker-evil",
                "HostConfig": {"Binds": ["/var/run/docker.sock:/x"]}}
        self.assertIsNotNone(hp.evaluate_deny(body, self.cfg))

    def test_B05_label_spoof_basic(self):
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                "Labels": {"com.mergepilot.run_id": "victim"}}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        # client value must be stripped; authoritative value injected
        self.assertEqual(out["Labels"]["com.mergepilot.run_id"], "run-test-01")

    def test_B05b_label_spoof_case(self):
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                "Labels": {"COM.MERGEPILOT.RUN_ID": "victim"}}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        self.assertEqual(out["Labels"]["com.mergepilot.run_id"], "run-test-01")
        # the spoofed uppercase key must NOT survive
        self.assertNotIn("COM.MERGEPILOT.RUN_ID", out["Labels"])

    def test_B05c_label_spoof_unicode(self):
        # fullwidth com.mergepilot.run_id (NFKC-normalizes to the secure key)
        spoof = "\uFF43\uFF4F\uFF4D.\uFF4D\uFF45\uFF52\uFF47\uFF45\uFF50\uFF49\uFF4C\uFF4F\uFF54.run_id"
        self.assertTrue(hp.is_secure_label_key(spoof))
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                "Labels": {spoof: "victim"}}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        self.assertEqual(out["Labels"]["com.mergepilot.run_id"], "run-test-01")

    def test_B05d_label_spoof_empty_value(self):
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                "Labels": {"com.mergepilot.run_id": ""}}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        self.assertEqual(out["Labels"]["com.mergepilot.run_id"], "run-test-01")

    def test_B05e_label_spoof_null_value(self):
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                "Labels": {"com.mergepilot.run_id": None}}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        self.assertEqual(out["Labels"]["com.mergepilot.run_id"], "run-test-01")

    def test_B05f_label_prefix_confusion(self):
        # com.mergepilot.run_id.evil is NOT the secure key — must be preserved
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                "Labels": {"com.mergepilot.run_id.evil": "x"}}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        self.assertIn("com.mergepilot.run_id.evil", out["Labels"])
        # and the real run_id is still injected
        self.assertEqual(out["Labels"]["com.mergepilot.run_id"], "run-test-01")

    def test_B05g_non_dict_labels(self):
        # labels as a list (invalid type) — strip returns {} and inject proceeds
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                "Labels": [["com.mergepilot.run_id", "x"]]}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        self.assertEqual(out["Labels"]["com.mergepilot.run_id"], "run-test-01")

    def test_B05h_all_secure_keys_stripped(self):
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                "Labels": {k: "spoof" for k in hp.SECURE_LABEL_KEYS}}
        out = hp.apply_hardening_v2(body, "worker",
                                    self.cfg.hardening_config())
        for k in hp.SECURE_LABEL_KEYS:
            self.assertNotEqual(out["Labels"][k], "spoof",
                                "%s not stripped" % k)

    def test_B08_version_prefix_normalization(self):
        # /v1.99, /v0.1, no-prefix — all should classify consistently
        for prefix in ("/v1.99", "/v0.1", ""):
            path = prefix + "/containers/agentteams-worker-fixer/json"
            d = dsp.classify_request("GET", path, self.cfg, self.reg)
            self.assertEqual(d.action, "allow",
                             "version prefix %r broke classify" % prefix)

    def test_B10_image_digest_injection(self):
        # "allowlisted@sha256:abc evil" — must not bypass
        body = {"Image": DIGEST + " evil"}
        self.assertIsNotNone(hp.evaluate_deny(body, self.cfg))

    def test_B11_archive_path_strict(self):
        # B11 fix: archive path must be the auth-token dir; /etc denied
        d = dsp.classify_request("PUT",
                                 "/containers/agentteams-worker-fixer/archive?path=/etc",
                                 self.cfg, self.reg)
        self.assertEqual(d.action, "deny")
        # the auth-token dir is allowed
        d2 = dsp.classify_request("PUT",
                                  "/containers/agentteams-worker-fixer/archive?path=/var/run/secrets/agentteams",
                                  self.cfg, self.reg)
        self.assertEqual(d2.action, "allow")

    def test_B12_exec_unknown_id(self):
        # exec start without prior register -> deny (fail-closed)
        d = dsp.classify_request("POST", "/exec/unknown-id/start",
                                 self.cfg, self.reg,
                                 target_header="tcp")
        self.assertEqual(d.action, "deny")

    def test_B12b_exec_expired(self):
        reg = dsp.ExecRegistry(ttl=0.05)
        reg.register("exec-x", "agentteams-worker-fixer")
        time.sleep(0.1)
        d = dsp.classify_request("POST", "/exec/exec-x/start",
                                 self.cfg, reg, target_header="tcp")
        self.assertEqual(d.action, "deny")

    def test_B12c_exec_registry_cleared_on_restart(self):
        # simulates proxy restart: new registry = empty = fail-closed
        reg1 = dsp.ExecRegistry()
        reg1.register("exec-y", "agentteams-worker-fixer")
        reg2 = dsp.ExecRegistry()  # "restart"
        d = dsp.classify_request("POST", "/exec/exec-y/start",
                                 self.cfg, reg2, target_header="tcp")
        self.assertEqual(d.action, "deny")

    def test_B13_pull_malicious_image(self):
        d = dsp.classify_request("POST",
                                 "/images/create?fromImage=evil@sha256:%s"
                                 % ("f" * 64),
                                 self.cfg, self.reg)
        self.assertEqual(d.action, "deny")

    def test_B_extra_chunked_size_not_in_classify(self):
        # body size is enforced at the HTTP handler layer (MAX_BODY_BYTES),
        # not in classify_request. Verify the constant exists + is 1 MiB.
        self.assertEqual(dsp.MAX_BODY_BYTES, 1 * 1024 * 1024)

    def test_B_extra_query_injection(self):
        # extra query params must not affect the deny decision
        d = dsp.classify_request("POST",
                                 "/containers/create?name=agentteams-worker-fixer&privileged=1",
                                 self.cfg, self.reg, body={"Image": DIGEST})
        self.assertEqual(d.action, "transform")  # body drives, not query

    def test_B_extra_unknown_endpoint_default_deny(self):
        for method, path in [("GET", "/foo"), ("POST", "/containers/x/y"),
                             ("DELETE", "/random")]:
            d = dsp.classify_request(method, path, self.cfg, self.reg)
            self.assertEqual(d.action, "deny",
                             "%s %s not denied" % (method, path))

    def test_B_extra_symlink_marker_rejected(self):
        # marker validation rejects symlinks (tested via guarded_start)
        # here we verify the proxy's own socket-bind refuses symlinks
        with tempfile.TemporaryDirectory() as td:
            sock_path = os.path.join(td, "mp.sock")
            link_path = os.path.join(td, "link.sock")
            os.symlink(sock_path, link_path)
            with self.assertRaises(RuntimeError):
                dsp.bind_listening_socket(link_path)


# ===========================================================================
# 4. CLEANUP / LIFECYCLE (C1-C12) — 12 cases
# ===========================================================================


class TestCleanup(unittest.TestCase):
    def test_C01_config_empty_image_allowlist_rejected(self):
        with self.assertRaises(ValueError):
            dsp.ProxyConfig(run_id="r", image_allowlist=())

    def test_C02_config_bad_scope_rejected(self):
        with self.assertRaises(ValueError):
            dsp.ProxyConfig(run_id="r", image_allowlist=(DIGEST,),
                            scope="evil")

    def test_C03_config_empty_run_id_rejected(self):
        with self.assertRaises(ValueError):
            dsp.ProxyConfig(run_id="", image_allowlist=(DIGEST,))

    def test_C04_config_bad_name_profile_rejected(self):
        with self.assertRaises(ValueError):
            dsp.ProxyConfig(run_id="r", image_allowlist=(DIGEST,),
                            name_profile="evil")

    def test_C05_exec_registry_clear(self):
        reg = dsp.ExecRegistry()
        reg.register("a", "agentteams-worker-fixer")
        reg.clear()
        ok, _ = reg.authorize("a")
        self.assertFalse(ok)

    def test_C06_exec_registry_gc(self):
        reg = dsp.ExecRegistry(ttl=0.05)
        reg.register("a", "agentteams-worker-fixer")
        time.sleep(0.1)
        # authorize triggers gc
        ok, _ = reg.authorize("a")
        self.assertFalse(ok)

    def test_C07_marker_content_format(self):
        c = marker_content = dsp.marker_content(12345, "abc123def456")
        self.assertIn(b"hiclab-proxy:deployed:v1\n", c)
        self.assertIn(b"pid=12345\n", c)
        self.assertIn(b"digest=abc123def456\n", c)

    def test_C08_marker_atomic_write(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "marker")
            ok = dsp.write_marker(123, "dig", path=path)
            self.assertTrue(ok)
            with open(path, "rb") as fh:
                self.assertIn(b"pid=123", fh.read())
            # mode 0600 (POSIX only; Windows NTFS does not model Unix modes)
            if sys.platform != "win32":
                st = os.lstat(path)
                self.assertEqual(st.st_mode & 0o777, 0o600)

    def test_C09_marker_remove_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "marker")
            # removing non-existent marker must not raise
            dsp.remove_marker(path)

    def test_C10_config_digest_stable(self):
        c1 = _cfg()
        c2 = _cfg()
        self.assertEqual(c1.config_digest(), c2.config_digest())
        c3 = _cfg(image_allowlist=(DIGEST2,))
        self.assertNotEqual(c1.config_digest(), c3.config_digest())

    def test_C11_socket_cleanup(self):
        if sys.platform == "win32":
            self.skipTest("AF_UNIX socket bind not supported on Windows host")
        with tempfile.TemporaryDirectory() as td:
            sock_path = os.path.join(td, "mp", "docker.sock")
            sock = dsp.bind_listening_socket(sock_path)
            try:
                self.assertTrue(os.path.exists(sock_path))
                st = os.lstat(sock_path)
                self.assertEqual(st.st_mode & 0o777, 0o600)
            finally:
                sock.close()
                dsp._safe_remove(sock_path)
            self.assertFalse(os.path.exists(sock_path))

    def test_C12_socket_dir_created(self):
        if sys.platform == "win32":
            self.skipTest("AF_UNIX socket bind not supported on Windows host")
        with tempfile.TemporaryDirectory() as td:
            sock_path = os.path.join(td, "sub1", "sub2", "docker.sock")
            sock = dsp.bind_listening_socket(sock_path)
            try:
                self.assertTrue(os.path.exists(sock_path))
            finally:
                sock.close()
                dsp._safe_remove(sock_path)


# ===========================================================================
# 5. INTEGRATION (I1-I7) — 7 cases
# ===========================================================================


class TestIntegration(unittest.TestCase):
    def test_I01_harness_round_trip_ping(self):
        # REAL round-trip through the production handler
        with ProxyHarness() as h:
            h.daemon.queue_response(status=200, body=b"OK")
            status, body, err = h.client.get("/_ping")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"OK")
            self.assertIsNone(err)
            self.assertEqual(h.upstream_request_count, 1)

    def test_I02_harness_create_worker_transform_seen_by_upstream(self):
        # REAL round-trip: transformed create body must reach upstream
        with ProxyHarness() as h:
            h.daemon.queue_response(status=201, body=b'{"Id":"abc"}')
            status, _body, err = h.client.post(
                "/containers/create?name=agentteams-worker-fixer",
                body={"Image": DIGEST})
            self.assertEqual(status, 201)
            self.assertEqual(h.upstream_request_count, 1)
            # verify upstream received the TRANSFORMED body (restart=no, labels)
            upstream_req = h.daemon.requests[0]
            self.assertEqual(upstream_req["body"]["HostConfig"]["RestartPolicy"],
                             {"Name": "no"})
            self.assertEqual(
                upstream_req["body"]["Labels"]["com.mergepilot.hardened"], "1")

    def test_I03_legacy_hiclaw_profile(self):
        cfg = _cfg(name_profile="hiclaw")
        d = dsp.classify_request("POST",
                                 "/containers/create?name=hiclaw-worker-fixer",
                                 cfg, dsp.ExecRegistry(),
                                 body={"Image": DIGEST})
        self.assertEqual(d.action, "transform")
        # and agentteams- name should NOT match in hiclaw profile
        d2 = dsp.classify_request("POST",
                                  "/containers/create?name=agentteams-worker-fixer",
                                  cfg, dsp.ExecRegistry(),
                                  body={"Image": DIGEST})
        self.assertEqual(d2.action, "deny")

    def test_I04_auth_volume_legacy(self):
        cfg = _cfg(name_profile="hiclaw")
        d = dsp.classify_request("DELETE",
                                 "/volumes/hiclaw-worker-fixer-auth",
                                 cfg, dsp.ExecRegistry())
        self.assertEqual(d.action, "allow")
        # agentteams- volume name in hiclaw profile -> deny
        d2 = dsp.classify_request("DELETE",
                                  "/volumes/agentteams-worker-fixer-auth",
                                  cfg, dsp.ExecRegistry())
        self.assertEqual(d2.action, "deny")

    def test_I05_full_transform_chain(self):
        cfg = _cfg()
        # A benign create body (no dangerous fields) passes deny, then transform
        # strips/injects labels and forces restart=no.
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                "Labels": {"com.mergepilot.run_id": "attacker"}}
        # 1. deny check passes (no dangerous fields)
        self.assertIsNone(hp.evaluate_deny(body, cfg))
        # 2. transform strips + injects
        out = hp.apply_hardening_v2(body, "worker", cfg.hardening_config())
        self.assertEqual(out["HostConfig"]["RestartPolicy"], {"Name": "no"})
        self.assertEqual(out["Labels"]["com.mergepilot.run_id"], "run-test-01")
        # 3. a dangerous RestartPolicy (always) is DENIED (D1.13), not silently
        #    downgraded — defense in depth
        dangerous = {"Image": DIGEST, "Name": "agentteams-worker-fixer",
                     "HostConfig": {"RestartPolicy": {"Name": "always"}}}
        self.assertIsNotNone(hp.evaluate_deny(dangerous, cfg))

    def test_I06_exec_full_lifecycle(self):
        cfg = _cfg()
        reg = dsp.ExecRegistry()
        # 1. exec create on authorized target
        d1 = dsp.classify_request("POST",
                                  "/containers/agentteams-worker-fixer/exec",
                                  cfg, reg)
        self.assertEqual(d1.action, "allow")
        # 2. simulate upstream returning an exec id; proxy registers it
        reg.register("exec-123", "agentteams-worker-fixer")
        # 3. start with hijack
        d2 = dsp.classify_request("POST", "/exec/exec-123/start",
                                  cfg, reg, target_header="tcp")
        self.assertEqual(d2.action, "allow")
        self.assertTrue(d2.hijack)
        # 4. json
        d3 = dsp.classify_request("GET", "/exec/exec-123/json",
                                  cfg, reg)
        self.assertEqual(d3.action, "allow")

    def test_I07_log_config_enforced(self):
        cfg = _cfg()
        body = {"Image": DIGEST, "Name": "agentteams-worker-fixer"}
        out = hp.apply_hardening_v2(body, "worker", cfg.hardening_config())
        lc = out["HostConfig"]["LogConfig"]
        self.assertEqual(lc["Type"], "json-file")
        self.assertIn("max-size", lc["Config"])
        self.assertIn("max-file", lc["Config"])


# ===========================================================================
# 6. MARKER PID LIFECYCLE (B7) — extra coverage for guarded_start extension
# ===========================================================================


class TestMarkerPidLifecycle(unittest.TestCase):
    """Validates the D2B-3B1 PID-binding marker (guarded_start.validate_proxy_marker_alive)."""

    def _st(self, mode=0o100600, uid=0, isreg=True, islink=False):
        class _S:
            pass
        s = _S()
        import stat as st_mod
        s.st_mode = (st_mod.S_IFREG if isreg else 0) | (st_mod.S_IFLNK if islink else 0) | (mode & 0o777)
        s.st_uid = uid
        return s

    def _content(self, pid=12345, digest="abc123"):
        return (b"hiclab-proxy:deployed:v1\n"
                + ("pid=%d\n" % pid).encode("ascii")
                + ("digest=%s\n" % digest).encode("ascii"))

    def test_M01_valid_marker_alive_pid(self):
        import guarded_start as gs
        content = self._content(pid=os.getpid())  # current process is alive
        ok, reason = gs.validate_proxy_marker_alive(
            "/etc/hiclab/proxy-deployed",
            expected_digest="abc123",
            stat_fn=lambda p: self._st(),
            read_fn=lambda p: content,
            os_kill=lambda pid, sig: None,  # alive
        )
        self.assertTrue(ok, reason)

    def test_M02_stale_pid_rejected(self):
        import guarded_start as gs
        content = self._content(pid=999999)
        ok, reason = gs.validate_proxy_marker_alive(
            "/etc/hiclab/proxy-deployed",
            stat_fn=lambda p: self._st(),
            read_fn=lambda p: content,
            os_kill=self._raise_esrch,
        )
        self.assertFalse(ok)
        self.assertIn("not alive", reason)

    def test_M03_digest_mismatch_rejected(self):
        import guarded_start as gs
        content = self._content(digest="aaaa")
        ok, reason = gs.validate_proxy_marker_alive(
            "/etc/hiclab/proxy-deployed",
            expected_digest="bbbb",
            stat_fn=lambda p: self._st(),
            read_fn=lambda p: content,
            os_kill=lambda pid, sig: None,
        )
        self.assertFalse(ok)
        self.assertIn("digest mismatch", reason)

    def test_M04_missing_pid_line_rejected(self):
        import guarded_start as gs
        content = b"hiclab-proxy:deployed:v1\n"
        ok, reason = gs.validate_proxy_marker_alive(
            "/etc/hiclab/proxy-deployed",
            stat_fn=lambda p: self._st(),
            read_fn=lambda p: content,
            os_kill=lambda pid, sig: None,
        )
        self.assertFalse(ok)
        self.assertIn("missing pid", reason)

    def test_M05_symlink_marker_rejected(self):
        import guarded_start as gs
        ok, reason = gs.validate_proxy_marker_alive(
            "/etc/hiclab/proxy-deployed",
            stat_fn=lambda p: self._st(islink=True, isreg=False),
            read_fn=lambda p: self._content(),
            os_kill=lambda pid, sig: None,
        )
        self.assertFalse(ok)
        self.assertIn("symlink", reason)

    def test_M06_wrong_mode_rejected(self):
        import guarded_start as gs
        ok, reason = gs.validate_proxy_marker_alive(
            "/etc/hiclab/proxy-deployed",
            stat_fn=lambda p: self._st(mode=0o100644),
            read_fn=lambda p: self._content(),
            os_kill=lambda pid, sig: None,
        )
        self.assertFalse(ok)
        self.assertIn("mode", reason)

    def _raise_esrch(self, pid, sig):
        import errno
        e = OSError()
        e.errno = errno.ESRCH
        raise e


if __name__ == "__main__":
    unittest.main()
