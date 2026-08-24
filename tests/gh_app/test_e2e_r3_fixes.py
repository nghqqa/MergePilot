"""M8-GH-4B3-W3B-R3: production-reachability fixes for the four
prepush-review blockers.

1. The real prerequisite gate runs BEFORE any side effect (no
   session write, no secret generation, no PAT read) with the four
   REAL probe adapters injected (gw-priority, network CIDRs,
   iptables-save text, Matrix joined members).
2. The CLI session and the lifecycle session are ONE object: the
   final session.json carries the identity fields (run_id,
   github_e2e, schema_version) AND the lifecycle journal
   (e2e_stage/e2e_runtime_journal) simultaneously.
3. Container argv uses the ABSOLUTE env-file path of the created
   runtime file and REAL host mount sources from the validated
   config — no _host_path or <placeholder> residue.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import mergepilot as mp               # noqa: E402
import e2e_foundation as e2f          # noqa: E402
import e2e_lifecycle as el            # noqa: E402
import e2e_runtime_specs as rs        # noqa: E402


def _main_json(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mp.main(argv + ["--json", "--project-dir", str(ROOT)])
    return rc, json.loads(buf.getvalue())


def _config(tmp):
    return {
        "room_map_path": str(Path(tmp) / "room-map.yaml"),
        "policy_path": str(Path(tmp) / "policy.yaml"),
        "matrix_homeserver": "http://127.0.0.1:18169",
        "matrix_room_id": "!r:s",
        "matrix_credentials_path": str(Path(tmp) / "creds.json"),
        "app_pem_path": str(Path(tmp) / "app.pem"),
        "webhook_secret_path": str(Path(tmp) / "wh.secret"),
        "mcp_pat_path": str(Path(tmp) / "pat.txt"),
        "hiclaw_receipt_path": str(Path(tmp) / "receipt.json"),
        "callback_url_path": str(Path(tmp) / "cb.txt"),
        "windows_proxy_ip": "172.23.48.1",
        "windows_proxy_port": "17890",
        "tuwunel_ip": "172.22.0.2",
        "tuwunel_port": "6167",
        "fixture_repo": "example/fixture",
        "installation_id": "1", "repository_id": "1", "app_id": "1",
        "expected_old_mcp_state": "stopped",
        "expected_8090_state": "free",
    }


def _state_fixture(tmp, config, planner):
    state = Path(tmp) / ".mergepilot"
    state.mkdir(parents=True, exist_ok=True)
    (state / "github-e2e.json").write_text(
        json.dumps(config), encoding="utf-8")
    fake_install = {
        "version": 1,
        "images": {mp.image_tag(planner, svc): "sha256:" + "ab" * 32
                   for svc in planner.BUILT_SERVICES}}
    (state / "install.json").write_text(
        json.dumps(fake_install), encoding="utf-8")
    for key in ("room_map_path", "policy_path", "app_pem_path",
                "mcp_pat_path", "hiclaw_receipt_path",
                "callback_url_path", "webhook_secret_path",
                "matrix_credentials_path"):
        Path(config[key]).write_bytes(b"synthetic\n")
    return state


class TestGateBeforeSideEffects(unittest.TestCase):

    def setUp(self):
        # the synthetic install records sha256:ab*32 identities; a
        # suite peer that ran the real CLI recorded the REAL image
        # IDs into the process-global registry and the immutable-
        # once-recorded contract then fails here (order-dependent
        # IMAGE_DIGEST_MISMATCH — maintenance §3)
        from planner_isolation import add_planner_registry_isolation
        add_planner_registry_isolation(self)

    def test_gate_failure_zero_side_effects_with_real_adapters(self):
        planner, _ = mp._load_planner(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            state = _state_fixture(tmp, config, planner)
            writes = []
            real_write = mp.write_session

            def recording_write(p, sess):
                writes.append(dict(sess))
                return real_write(p, sess)

            # the gate FAILS: the membership adapter returns None
            # (PROBE_NOT_INJECTED fail-closed path)
            with mock.patch.object(e2f, "E2E_PENDING_COMPONENTS", ()), \
                 mock.patch.object(
                     mp, "write_session",
                     side_effect=recording_write), \
                 mock.patch.object(mp, "WslDocker"), \
                 mock.patch.object(
                     el, "fetch_matrix_joined_mxids",
                     return_value=None), \
                 mock.patch.object(
                     el, "fetch_docker_gw_priority_supported",
                     return_value=True), \
                 mock.patch.object(
                     el, "fetch_existing_network_cidrs",
                     return_value=[]), \
                 mock.patch.object(
                     el, "fetch_firewall_scan_text",
                     return_value=""), \
                 mock.patch.object(mp, "state_paths",
                                   return_value={
                                       "state": state,
                                       "install": state / "install.json",
                                       "session": state / "session.json",
                                       "secrets": state / "secrets"}):
                rc, payload = _main_json(
                    ["start", "--run-id", "r3a", "--github-e2e"])
            self.assertEqual(rc, 3)
            self.assertEqual(payload["error_code"],
                             "GITHUB_E2E_PREREQUISITES_INCOMPLETE")
            self.assertIn("matrix_membership", payload["error_detail"])
            # ZERO side effects: no session write, no session file,
            # no secrets directory (the PAT is only read AFTER the
            # gate passes — it was never reached)
            self.assertEqual(writes, [])
            self.assertFalse((state / "session.json").exists())
            self.assertFalse((state / "secrets").exists())

    def test_gate_passes_then_session_written_once(self):
        planner, _ = mp._load_planner(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            state = _state_fixture(tmp, config, planner)
            members = list(e2f.E2E_EXPECTED_ROOM_MEMBERS)

            def fake_start(**kw):
                # lifecycle writes through the persist callback
                session = kw["session"]
                session["e2e_stage"] = "complete"
                session["e2e_runtime_journal"] = {
                    "controller": {"file": "x", "ownership": "session"}}
                kw["persist_callback"](session)
                return session

            with mock.patch.object(e2f, "E2E_PENDING_COMPONENTS", ()), \
                 mock.patch.object(
                     el, "fetch_matrix_joined_mxids",
                     return_value=members), \
                 mock.patch.object(
                     el, "fetch_docker_gw_priority_supported",
                     return_value=True), \
                 mock.patch.object(
                     el, "fetch_existing_network_cidrs",
                     return_value=[]), \
                 mock.patch.object(
                     el, "fetch_firewall_scan_text",
                     return_value=""), \
                 mock.patch.object(
                     el, "run_prerequisite_gate",
                     return_value={"checks": {}}), \
                 mock.patch.object(el, "run_e2e_start",
                                   side_effect=fake_start), \
                 mock.patch.object(mp, "WslDocker"), \
                 mock.patch.object(mp, "prepare_database"), \
                 mock.patch.object(
                     mp, "_read_hiclaw_role_tokens",
                     return_value={"manager": "tok-m", "reviewer": "tok-r",
                                   "fixer": "tok-f", "verifier": "tok-v"}), \
                 mock.patch.object(mp, "_to_wsl_path",
                                   side_effect=lambda p: str(p)), \
                 mock.patch.object(mp, "state_paths",
                                   return_value={
                                       "state": state,
                                       "install": state / "install.json",
                                       "session": state / "session.json",
                                       "secrets": state / "secrets"}):
                rc, payload = _main_json(
                    ["start", "--run-id", "r3b", "--github-e2e"])
            self.assertEqual(rc, 0)
            # BLOCKER 2: identity fields AND lifecycle journal
            # coexist in the FINAL session.json
            final = json.loads(
                (state / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(final["run_id"], "r3b")
            self.assertIs(final["github_e2e"], True)
            self.assertEqual(final["schema_version"], 1)
            self.assertIn("created_utc", final)
            self.assertEqual(final["e2e_stage"], "complete")
            self.assertIn("e2e_runtime_journal", final)
            self.assertIn("controller", final["e2e_runtime_journal"])


class TestArgvRealPaths(unittest.TestCase):

    def test_env_file_absolute_and_mounts_real(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            from tests.gh_app.test_e2e_lifecycle_r2 import (
                _healthy_docker, _run_start, _rc,
                _config as lifecycle_config)
            import e2e_probes as ep
            fd = _healthy_docker()
            with mock.patch.object(
                    ep, "create_e2e_networks") as _cn:   # keep argv clean
                session = _run_start(fd, runtime=_rc(),
                                     runtime_dir=tmp)
            # inspect every create argv emitted by the lifecycle
            creates = [c for c in fd.calls if c and c[0] == "create"
                       and "--env-file" in c]
            self.assertTrue(creates)
            for argv in creates:
                i = argv.index("--env-file")
                env_path = argv[i + 1]
                # absolute path of a file that EXISTS in the runtime
                # directory (never a bare basename)
                self.assertTrue(Path(env_path).is_absolute(),
                                env_path)
                self.assertTrue(Path(env_path).exists(), env_path)
                self.assertEqual(Path(env_path).parent,
                                 Path(tmp).absolute())
                # mount sources are the REAL config paths — no
                # placeholder residue of any form
                mounts = [argv[j + 1] for j, t in enumerate(argv)
                          if t == "-v"]
                lc = lifecycle_config()
                for m in mounts:
                    source = m.split(":")[0]
                    self.assertNotIn("_host_path", m)
                    self.assertNotIn("<placeholder", m)
                    self.assertTrue(
                        source in (lc["room_map_path"],
                                   lc["policy_path"],
                                   lc["app_pem_path"]),
                        "mount source not a real config path: %s" % m)

    def test_plan_runtime_mounts_requires_real_config_sources(self):
        controller = rs.plan_runtime_mounts(
            "controller", config={"room_map_path": "/real/rm.yaml",
                                  "policy_path": "/real/p.yaml"})
        self.assertIn("-v", controller)
        self.assertTrue(any("/real/rm.yaml:/run/mergepilot/room-map.yaml:ro"
                            == m for m in controller if ":" in m))
        reporter = rs.plan_runtime_mounts(
            "gh-reporter", config={"app_pem_path": "/real/app.pem"})
        self.assertTrue(any(m.startswith("/real/app.pem:")
                            for m in reporter if ":" in m))
        # a missing real source is a fail-closed error, never a
        # placeholder
        with self.assertRaises(rs.RuntimeSpecError) as ctx:
            rs.plan_runtime_mounts(
                "controller", config={"policy_path": "/real/p.yaml"})
        self.assertEqual(ctx.exception.code,
                         "RUNTIME_MOUNT_SOURCE_MISSING")


class TestRealAdapters(unittest.TestCase):

    def test_fetchers_read_only_shapes(self):
        import subprocess

        def host_ok(argv, check=False, **kw):
            return subprocess.CompletedProcess(
                argv, 0, b"*filter\n:MP-EG-x - [0:0]\nCOMMIT\n", b"")

        self.assertEqual(
            el.fetch_firewall_scan_text(host_ok),
            "*filter\n:MP-EG-x - [0:0]\nCOMMIT\n")

        def host_fail(argv, check=False, **kw):
            return subprocess.CompletedProcess(argv, 1, b"", b"")

        self.assertEqual(el.fetch_firewall_scan_text(host_fail), "")

        def docker_gw(argv, check=False, **kw):
            return subprocess.CompletedProcess(
                argv, 0, b"connect --gw-priority ...\n", b"")

        self.assertIs(
            el.fetch_docker_gw_priority_supported(docker_gw), True)

        def docker_nets(argv, check=False, **kw):
            if argv[1] == "ls":
                return subprocess.CompletedProcess(
                    argv, 0, b"bridge\nmp-e2e-br-up\n", b"")
            return subprocess.CompletedProcess(
                argv, 0, b"172.17.0.0/16 172.31.0.80/28 ", b"")

        self.assertEqual(
            sorted(el.fetch_existing_network_cidrs(docker_nets)),
            ["172.17.0.0/16", "172.17.0.0/16",
             "172.31.0.80/28", "172.31.0.80/28"])

    def test_matrix_provider_real_api_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            creds = Path(config["matrix_credentials_path"])
            creds.write_text(json.dumps({"access_token": "x"}),
                             encoding="utf-8")
            # missing credentials file → None (fail closed)
            creds.unlink()
            self.assertIsNone(el.fetch_matrix_joined_mxids(config))
            # bad token shape → None
            Path(config["matrix_credentials_path"]).write_text(
                json.dumps({"access_token": ""}), encoding="utf-8")
            self.assertIsNone(el.fetch_matrix_joined_mxids(config))
            # real API response shape via injectable transport
            Path(config["matrix_credentials_path"]).write_text(
                json.dumps({"access_token": "syt_secret"}),
                encoding="utf-8")
            seen = {}

            def fake_transport(method, url, *, headers, body):
                seen["method"] = method
                seen["url"] = url
                seen["auth"] = headers.get("Authorization", "")
                return 200, {}, {"joined": {
                    "@manager:s": {}, "@m8gh4-controller:s": {}}}

            result = el.fetch_matrix_joined_mxids(
                config, transport=fake_transport)
            self.assertEqual(result,
                             ["@m8gh4-controller:s", "@manager:s"])
            self.assertEqual(seen["method"], "GET")
            self.assertIn("/joined_members", seen["url"])
            self.assertIn("syt_secret", seen["auth"])
            # non-200 → None
            self.assertIsNone(el.fetch_matrix_joined_mxids(
                config, transport=lambda *a, **kw: (500, {}, {})))


if __name__ == "__main__":
    unittest.main()
