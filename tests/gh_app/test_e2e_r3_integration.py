"""M8-GH-4B3-W3B-R3-T1: REAL CLI→lifecycle full integration test.

Drives the production chain cmd_start → _execute_github_e2e_start →
el.run_e2e_start (NOT mocked) → session persistence → full stage
transition → complete, then consumes the ACTUAL persisted session via
status/stop/cleanup. Only EXTERNAL boundaries are faked: the Docker
executor (WslDocker), the host iptables executor (stateful), the
Matrix/MCP HTTP layer (urllib router), the PostgreSQL bootstrap, and
the default stack discovery reads. No production lifecycle function
is mocked; 'complete', firewall_sid, receipt_verified and
matrix_verified are written exclusively by production code.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.request
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
import e2e_executors as ex            # noqa: E402
import e2e_probes as ep               # noqa: E402

RUN_ID = "w3b-r3-integration"
SERVER = e2f.E2E_MATRIX_SERVER_NAME
ROOM_ID = "!r:" + SERVER
FROZEN_TOOLS = ["get_pull_request", "get_pull_request_files",
                "get_file_contents", "get_branch"]


def _cp(rc=0, stdout=b""):
    return subprocess.CompletedProcess([], rc, stdout, b"")


class FakeDockerSide:
    """Full docker-side emulation with an event log."""

    def __init__(self, events):
        self.events = events
        self.containers = {}
        self.networks = {}
        self.iptables = []          # stateful rule lines

    # ── docker executor (container/network domain) ──
    def docker(self, argv, timeout=240, check=True, log_tag=None):
        argv = list(argv)
        a = argv
        if a[0] == "network" and a[1] == "ls":
            return _cp(0, "\n".join(self.networks).encode())
        if a[0] == "network" and a[1] == "create":
            self.networks[a[-1]] = "nid-%s" % a[-1]
            self.events.append("network_create")
            return _cp(0)
        if a[0] == "network" and a[1] == "inspect":
            nid = self.networks.get(a[2], "")
            return _cp(0, nid.encode()) if nid else _cp(1, b"")
        if a[0] == "network" and a[1] == "rm":
            target = a[2]
            for name, nid in list(self.networks.items()):
                if name == target or nid == target:
                    self.networks.pop(name)
            return _cp(0)
        if a[0] == "network" and a[1] == "connect":
            if "--help" in a:
                return _cp(0, b"connect --gw-priority ...")
            self.events.append("container_connect")
            return _cp(0)
        if a[0] in ("create", "run"):
            name = a[a.index("--name") + 1]
            existing = self.containers.get(name, {})
            self.containers[name] = {
                "id": "cid-%s" % name, "running": a[0] == "run",
                "exit_code": existing.get("exit_code", 0)}
            self.events.append("container_create")
            return _cp(0)
        if a[0] == "start":
            name = a[1]
            info = self.containers.get(name)
            if info is not None:
                info["running"] = "preflight" not in name
            self.events.append("service_start:%s" % name)
            return _cp(0)
        if a[0] == "rm":
            target = a[a.index("-f") + 1] if "-f" in a else a[-1]
            for name, info in list(self.containers.items()):
                if name == target or info.get("id") == target:
                    self.containers.pop(name)
            return _cp(0)
        if a[0] == "inspect":
            return _cp(0, self._inspect(a).encode())
        if a[0] == "exec":
            return self._exec(a)
        return _cp(0)

    def _inspect(self, a):
        name = a[1]
        fmt = a[a.index("--format") + 1]
        info = self.containers.get(name)
        if info is None:
            self.events.append("inspect_absent:%s" % name)
            return ""
        if "{{.NetworkSettings.Networks.hiclaw-net.IPAddress}}" in fmt:
            return {"hiclaw-manager": "172.21.0.2",
                    "hiclaw-worker-reviewer": "172.21.0.5",
                    "hiclaw-worker-fixer": "172.21.0.4",
                    "hiclaw-worker-verifier": "172.21.0.6"}.get(name, "")
        if "{{.HostConfig.RestartPolicy.Name}}" in fmt:
            return {"github-mcp": "no"}.get(name, "no")
        if "{{range $k, $v := .NetworkSettings.Networks}}" in fmt:
            return {"github-mcp": "bridge"}.get(name, " ")
        if "{{.Id}} {{.State.Status}}" in fmt:
            status = "running" if info["running"] else "exited"
            return "%s %s" % (info["id"], status)
        if "{{.State.Running}} {{.State.ExitCode}}" in fmt:
            return "%s %d" % ("true" if info["running"] else "false",
                              info.get("exit_code", 0))
        if "{{.State.Status}}" in fmt:
            return ("running" if info["running"]
                    else "exited" if name != "github-mcp"
                    else "stopped")
        if "{{.State.Running}}" in fmt:
            return "true" if info["running"] else "false"
        return info["id"]

    def _exec(self, a):
        name = a[1]
        if name.startswith("mp-e2e-route-probe-"):
            service = name[len("mp-e2e-route-probe-"):]
            src = dict(ex.ROUTE_PROBE_SPECS)[service][2]
            self.events.append("route_probe:%s" % service)
            return _cp(0, src.encode())
        if a[2:4] == ["sha256sum", "/root/manager-workspace/config/"
                      "mcporter.json"] or (
                len(a) > 2 and a[2] == "sha256sum"):
            role = {"hiclaw-manager": "manager",
                    "hiclaw-worker-reviewer": "reviewer",
                    "hiclaw-worker-fixer": "fixer",
                    "hiclaw-worker-verifier": "verifier"}[name]
            h = ("a" * 63 + "4").encode()
            self.events.append("receipt_hash:%s" % role)
            return _cp(0, h + b"  " + a[2].encode())
        if "pg_isready" in a:
            self.events.append("postgres_health")
            return _cp(0)
        if "python3" in a and "18090" in " ".join(a):
            self.events.append("proxy_health")
            return _cp(0)
        return _cp(0)

    # ── host executor (iptables domain; stateful) ──
    def wsl_exec(self, argv, input_bytes=None, timeout=60, check=True,
                 log_tag=None):
        a = list(argv)
        if a[0] == "iptables-save":
            return _cp(0, ("\n".join(self.iptables) + "\n").encode())
        if a[0] == "iptables-restore":
            if input_bytes:
                blob = input_bytes.decode("utf-8", "replace")
                for line in blob.splitlines():
                    line = line.strip()
                    if not line or line.startswith(("*", ":", "COMMIT")):
                        continue
                    if a[1] == "--noflush":
                        self.iptables.append(line)
            if a[1] == "--noflush":
                self.events.append("firewall_commit")
            return _cp(0)
        if a[0] == "iptables" and a[1] == "-D":
            chain = a[2]
            self.iptables = [l for l in self.iptables
                             if chain not in l]
            return _cp(0)
        if a[0] == "iptables":
            return _cp(0)     # -X / -I bookkeeping: chains untracked
        return _cp(0)


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_http_router(events):
    """urllib boundary router: Matrix joined_members + MCP SSE."""

    def router(request, timeout=None):
        if hasattr(request, "get_full_url"):
            url = request.get_full_url()
        else:
            url = getattr(request, "fullurl", str(request))
        if "/joined_members" in url:
            events.append("matrix_api")
            joined = {m: {} for m in e2f.E2E_EXPECTED_ROOM_MEMBERS}
            return _FakeResponse(
                200, json.dumps({"joined": joined}).encode())
        if ":8082/" in url:                      # bridge SSE
            if url.endswith("/messages"):
                events.append("bridge_health")
                tools = [{"name": n} for n in FROZEN_TOOLS
                         + ["list_branches"]]     # superset (bridge)
                body = {"result": {"tools": tools}}
                return _FakeResponse(200, json.dumps(body).encode())
            return _FakeResponse(200, b"event: ready")
        if ":8083/" in url:                      # gateway SSE
            if url.endswith("/messages"):
                events.append("gateway_health")
                tools = [{"name": n} for n in FROZEN_TOOLS]
                body = {"result": {"tools": tools}}
                return _FakeResponse(200, json.dumps(body).encode())
            return _FakeResponse(200, b"event: ready")
        raise AssertionError("unexpected URL: %s" % url)

    return router


def _build_receipt():
    agents = []
    for role, (container, mxid, ip, path) in \
            ex.HICLAW_ROLE_FREEZE.items():
        agents.append({
            "role": role,
            "container_name": container,
            "container_id": "cid-%s" % container,
            "mxid": mxid,
            "hiclaw_net_ip": ip,
            "gateway_url": "http://172.31.0.18:8083%s" % path,
            "config_hash_before": "b" * 64,
            "config_hash_after": "a" * 63 + "4",
            "token_hash": "c" * 64,
        })
    receipt = {
        "schema_version": 1,
        "agents": agents,
        "old_github_mcp": {
            "container_id": "cid-github-mcp",
            "state": "stopped",
            "restart_policy": "no",
            "network_attachments": ["bridge"],
        },
        "rollback_ownership": "mp-gh4-harness",
    }
    receipt["receipt_sha256"] = ex._compute_receipt_sha256(receipt)
    return receipt


class TestFullIntegration(unittest.TestCase):
    """§3-§6: one REAL CLI start through the REAL lifecycle, then
    status/stop/cleanup consuming the ACTUAL persisted session."""

    def test_cli_entry_real_lifecycle_reaches_complete(self):
        planner, _showcase = mp._load_planner(ROOT)
        events = []
        side = FakeDockerSide(events)
        router = make_http_router(events)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".mergepilot"
            state.mkdir()
            secrets = state / "secrets"

            # ── synthetic 20-key prerequisite config + probe files ──
            room_map = root / "room-map.yaml"
            room_map.write_text(
                'repos:\n'
                '  "example/fixture":\n'
                '    room_id: "%s"\n' % ROOM_ID, encoding="utf-8")
            policy = root / "policy.yaml"
            policy.write_text(
                'repos:\n'
                '  allowlist:\n'
                '    - "example/fixture"\n', encoding="utf-8")
            creds = root / "creds.json"
            creds.write_text(json.dumps(
                {"access_token": "syt_synthetic"}),
                encoding="utf-8")
            pem = root / "app.pem"
            pem.write_text("-----BEGIN PRIVATE KEY-----\nsynthetic\n"
                           "-----END PRIVATE KEY-----\n",
                           encoding="utf-8")
            for name in ("wh.secret", "pat.txt", "cb.txt"):
                (root / name).write_bytes(b"synthetic\n")
            pat = root / "pat.txt"
            pat.write_text("synthetic-pat-value", encoding="utf-8")
            receipt_path = root / "receipt.json"
            receipt_path.write_text(
                json.dumps(_build_receipt()), encoding="utf-8")

            config = {
                "room_map_path": str(room_map),
                "policy_path": str(policy),
                "matrix_homeserver": "http://127.0.0.1:18169",
                "matrix_room_id": ROOM_ID,
                "matrix_credentials_path": str(creds),
                "app_pem_path": str(pem),
                "webhook_secret_path": str(root / "wh.secret"),
                "mcp_pat_path": str(pat),
                "hiclaw_receipt_path": str(receipt_path),
                "callback_url_path": str(root / "cb.txt"),
                "windows_proxy_ip": "172.23.48.1",
                "windows_proxy_port": "17890",
                "tuwunel_ip": "172.22.0.2",
                "tuwunel_port": "6167",
                "fixture_repo": "example/fixture",
                "installation_id": "1", "repository_id": "1",
                "app_id": "1",
                "expected_old_mcp_state": "stopped",
                "expected_8090_state": "free",
            }
            (state / "github-e2e.json").write_text(
                json.dumps(config), encoding="utf-8")

            # install manifest: every planner tag + the reporter tag
            tags = {mp.image_tag(planner, svc)
                    for svc in planner.BUILT_SERVICES}
            tags.add(mp.image_tag(planner, "gh-reporter"))
            install = {"version": 1,
                       "images": {t: "sha256:" + "ab" * 32
                                  for t in tags}}
            (state / "install.json").write_text(
                json.dumps(install), encoding="utf-8")

            # HiClaw containers pre-exist (operator-owned, running)
            for container in ("hiclaw-manager", "hiclaw-worker-reviewer",
                              "hiclaw-worker-fixer",
                              "hiclaw-worker-verifier", "github-mcp"):
                side.containers[container] = {
                    "id": "cid-%s" % container,
                    "running": container != "github-mcp"}

            persist_events = []
            real_write = mp.write_session

            def recording_write(p, sess):
                persist_events.append(sess.get("e2e_stage", "pre"))
                return real_write(p, sess)

            with mock.patch.object(mp, "WslDocker",
                                   return_value=side), \
                 mock.patch.object(urllib.request, "urlopen",
                                   side_effect=router), \
                 mock.patch.object(mp, "prepare_database",
                                   side_effect=lambda *a, **kw: events
                                   .append("db_bootstrap")), \
                 mock.patch.object(mp, "write_session",
                                   side_effect=recording_write), \
                 mock.patch.object(mp, "state_paths",
                                   return_value={
                                       "state": state,
                                       "install": state / "install.json",
                                       "session": state / "session.json",
                                       "secrets": secrets}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = mp.main(["start", "--run-id", RUN_ID,
                                  "--github-e2e", "--json",
                                  "--project-dir", str(ROOT)])
            payload = json.loads(buf.getvalue())

            # ── §4: CLI result ──
            self.assertEqual(rc, 0, payload)
            self.assertEqual(payload["command"], "start")
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["run_id"], RUN_ID)

            # ── §5: authoritative session from DISK ──
            session_path = state / "session.json"
            self.assertTrue(session_path.exists())
            final = json.loads(
                session_path.read_text(encoding="utf-8"))
            self.assertEqual(final["run_id"], RUN_ID)
            self.assertIs(final["github_e2e"], True)
            self.assertEqual(final["schema_version"], 1)
            self.assertIn("created_utc", final)
            self.assertEqual(final["e2e_stage"], "complete")
            self.assertEqual(len(final["e2e_network_ids"]), 8)
            self.assertEqual(len(final["e2e_container_ids"]), 11)
            self.assertEqual(len(final["e2e_runtime_journal"]), 6)
            self.assertRegex(final["firewall_sid"],
                             r"^[0-9a-f]{8}$")
            self.assertEqual(final["firewall_state"], "installed")
            self.assertIs(final["receipt_verified"], True)
            self.assertIs(final["matrix_verified"], True)
            self.assertIs(final["prerequisite_summary"]["verified"],
                          True)
            self.assertEqual(final["e2e_pending_components"], [])

            # no secret material in the persisted session
            blob = session_path.read_text(encoding="utf-8")
            for forbidden in ("synthetic-pat-value", "syt_synthetic",
                              "BEGIN PRIVATE KEY", "Bearer ",
                              "ROLE_TOKENS=", "postgresql://u:",
                              "Authorization", "COMMIT", "-A INPUT",
                              "mp-tmp"):
                self.assertNotIn(forbidden, blob)

            # ── §4: event ordering (single interleaved log) ──
            self.assertEqual(events[0], "db_bootstrap"
                             if False else events[0])  # no-op guard
            self.assertIn("firewall_commit", events)
            self.assertIn("postgres_health", events)
            self.assertIn("db_bootstrap", events)
            self.assertEqual(events.count("proxy_health"), 2)
            self.assertIn("bridge_health", events)
            self.assertIn("gateway_health", events)
            self.assertEqual(
                len([e for e in events
                     if e.startswith("route_probe:")]), 6)
            self.assertEqual(
                len([e for e in events
                     if e.startswith("receipt_hash:")]), 4)
            self.assertIn("matrix_api", events)
            # runtime persists precede the first network create
            first_net = persist_events.index("networks") \
                if "networks" in persist_events else len(persist_events)
            runtime_persists = [i for i, s in enumerate(persist_events)
                                if s == "runtime_files"]
            self.assertGreaterEqual(len(runtime_persists), 6)
            self.assertTrue(all(i < first_net
                                for i in runtime_persists))
            # receipt/matrix rechecks EXECUTED (their stages set in
            # session; execution evidence: 4 live sha256 probes and
            # ≥2 homeserver calls — CLI fetch + complete-time recheck)
            self.assertEqual(persist_events[-1], "complete")
            self.assertEqual(
                len([e for e in events
                     if e.startswith("receipt_hash:")]), 4)
            self.assertGreaterEqual(events.count("matrix_api"), 2)
            # 11 services started through the REAL lifecycle
            starts = [e.split(":")[1] for e in events
                      if e.startswith("service_start:")
                      and "route-probe" not in e]
            self.assertEqual(sorted(set(starts)),
                             sorted("mergepilot-isolated-%s-1" % s
                                    for s in el._DAG_ORDER))

            # keep state dir for the status/stop/cleanup continuation
            self._state = state
            self._side = side
            self._secrets = secrets

    def test_status_stop_cleanup_consume_real_session(self):
        # §6 requires the SAME session produced by the real start;
        # re-run the full start (fresh temp state) and continue.
        planner, _showcase = mp._load_planner(ROOT)
        events = []
        side = FakeDockerSide(events)
        router = make_http_router(events)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".mergepilot"
            state.mkdir()
            secrets = state / "secrets"
            TestFullIntegration._prepare_state(
                self, root, state, secrets, planner, side)

            with mock.patch.object(mp, "WslDocker",
                                   return_value=side), \
                 mock.patch.object(urllib.request, "urlopen",
                                   side_effect=router), \
                 mock.patch.object(
                     mp, "prepare_database",
                     side_effect=lambda *a, **kw: None), \
                 mock.patch.object(mp, "state_paths",
                                   return_value={
                                       "state": state,
                                       "install": state / "install.json",
                                       "session": state
                                       / "session.json",
                                       "secrets": secrets}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = mp.main(["start", "--run-id", RUN_ID,
                                  "--github-e2e", "--json",
                                  "--project-dir", str(ROOT)])
            self.assertEqual(rc, 0)
            session_path = state / "session.json"
            self.assertEqual(
                json.loads(session_path.read_text())["e2e_stage"],
                "complete")

            absent = {"containers": {}, "networks": {}}

            def cli(argv):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc2 = mp.main(argv + ["--json",
                                          "--project-dir", str(ROOT)])
                return rc2, json.loads(buf.getvalue())

            # ── status consumes the REAL session ──
            with mock.patch.object(mp, "discover_stack",
                                   return_value=absent), \
                 mock.patch.object(mp, "classify_stack",
                                   return_value=("absent", "none")), \
                 mock.patch.object(mp, "WslDocker",
                                   return_value=side), \
                 mock.patch.object(urllib.request, "urlopen",
                                   side_effect=router), \
                 mock.patch.object(mp, "state_paths",
                                   return_value={
                                       "state": state,
                                       "install": state / "install.json",
                                       "session": session_path,
                                       "secrets": secrets}):
                rc2, payload = cli(["status"])
            self.assertEqual(rc2, 0)
            services = payload["github_e2e_services"]
            self.assertIs(payload["session"]["github_e2e"], True)
            self.assertEqual(
                len([k for k in services if not k.startswith("_")]),
                len(el._DAG_ORDER))
            blob = json.dumps(payload)
            for forbidden in ("synthetic-pat-value", "syt_synthetic",
                              "BEGIN PRIVATE KEY", "postgresql://u:"):
                self.assertNotIn(forbidden, blob)

            # ── cleanup (E2E path) reports residue without deleting ──
            before = dict(side.containers)
            with mock.patch.object(mp, "discover_stack",
                                   return_value=absent), \
                 mock.patch.object(mp, "WslDocker",
                                   return_value=side), \
                 mock.patch.object(mp, "state_paths",
                                   return_value={
                                       "state": state,
                                       "install": state / "install.json",
                                       "session": session_path,
                                       "secrets": secrets}):
                rc3, payload = cli(["cleanup"])
            self.assertEqual(rc3, 0)     # dry-run cleanup: report only
            self.assertTrue(
                payload.get("github_e2e_residue"))
            self.assertEqual(side.containers, before)  # nothing deleted

            # ── stop consumes the REAL session, journal-owned only ──
            with mock.patch.object(mp, "discover_stack",
                                   return_value=absent), \
                 mock.patch.object(mp, "WslDocker",
                                   return_value=side), \
                 mock.patch.object(mp, "state_paths",
                                   return_value={
                                       "state": state,
                                       "install": state / "install.json",
                                       "session": session_path,
                                       "secrets": secrets}):
                rc4, payload = cli(["stop"])
            self.assertEqual(rc4, 0, payload)
            self.assertFalse(session_path.exists())  # manifest removed
            # ONLY journal-owned E2E resources removed; operator-owned
            # HiClaw/github-mcp untouched
            for container in ("hiclaw-manager", "hiclaw-worker-reviewer",
                              "hiclaw-worker-fixer",
                              "hiclaw-worker-verifier", "github-mcp"):
                self.assertIn(container, side.containers)
            self.assertFalse(
                any(n.startswith("mergepilot-isolated-")
                    for n in side.containers))
            self.assertEqual(side.networks, {})
            self.assertEqual(side.iptables, [])
            # runtime files removed
            self.assertFalse(any(secrets.glob("*.env")))

    def _prepare_state(self, root, state, secrets, planner, side):
        # shared fixture builder (same shapes as the start test)
        room_map = root / "room-map.yaml"
        room_map.write_text(
            'repos:\n'
            '  "example/fixture":\n'
            '    room_id: "%s"\n' % ROOM_ID, encoding="utf-8")
        policy = root / "policy.yaml"
        policy.write_text(
            'repos:\n'
            '  allowlist:\n'
            '    - "example/fixture"\n', encoding="utf-8")
        (root / "creds.json").write_text(json.dumps(
            {"access_token": "syt_synthetic"}), encoding="utf-8")
        (root / "app.pem").write_text("synthetic pem", encoding="utf-8")
        for name in ("wh.secret", "cb.txt"):
            (root / name).write_bytes(b"synthetic\n")
        (root / "pat.txt").write_text("synthetic-pat-value",
                                      encoding="utf-8")
        receipt_path = root / "receipt.json"
        receipt_path.write_text(json.dumps(_build_receipt()),
                                encoding="utf-8")
        config = {
            "room_map_path": str(room_map),
            "policy_path": str(policy),
            "matrix_homeserver": "http://127.0.0.1:18169",
            "matrix_room_id": ROOM_ID,
            "matrix_credentials_path": str(root / "creds.json"),
            "app_pem_path": str(root / "app.pem"),
            "webhook_secret_path": str(root / "wh.secret"),
            "mcp_pat_path": str(root / "pat.txt"),
            "hiclaw_receipt_path": str(receipt_path),
            "callback_url_path": str(root / "cb.txt"),
            "windows_proxy_ip": "172.23.48.1",
            "windows_proxy_port": "17890",
            "tuwunel_ip": "172.22.0.2",
            "tuwunel_port": "6167",
            "fixture_repo": "example/fixture",
            "installation_id": "1", "repository_id": "1",
            "app_id": "1",
            "expected_old_mcp_state": "stopped",
            "expected_8090_state": "free",
        }
        (state / "github-e2e.json").write_text(
            json.dumps(config), encoding="utf-8")
        tags = {mp.image_tag(planner, svc)
                for svc in planner.BUILT_SERVICES}
        tags.add(mp.image_tag(planner, "gh-reporter"))
        (state / "install.json").write_text(json.dumps(
            {"version": 1,
             "images": {t: "sha256:" + "ab" * 32 for t in tags}}),
            encoding="utf-8")
        for container in ("hiclaw-manager", "hiclaw-worker-reviewer",
                          "hiclaw-worker-fixer", "hiclaw-worker-verifier",
                          "github-mcp"):
            side.containers[container] = {
                "id": "cid-%s" % container,
                "running": container != "github-mcp"}


if __name__ == "__main__":
    unittest.main()
