"""Read-only E2E console status tests (maintenance §7).

The console's /api/e2e/status serves a DERIVED, whitelisted
projection written by the journal's single writer; these tests pin
the schema, the sanitization, the honest absent/stale semantics,
and the endpoint behavior (present / absent / unparseable) against
a REAL in-process HTTP server.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "demo_console")):
    if p not in sys.path:
        sys.path.insert(0, p)

import mergepilot as mp                       # noqa: E402
import serve as console_serve                 # noqa: E402


def _e2e_session():
    return {
        "schema_version": 1,
        "run_id": "b8-e2e-runX",
        "github_e2e": True,
        "e2e_stage": "complete",
        "transport_profile": "wsl-user-relay",
        "direct_routing_verified": False,
        "receipt_verified": True,
        "matrix_verified": True,
        "prerequisite_summary": {"checks_passed": 16, "verified": True},
        "route_probe_results": {
            "controller-to-tuwunel": {
                "verified": True, "vantage": "relay:published_egress"},
            "bridge-to-proxy-b": {
                "verified": False, "vantage": "relay:dual_homed_relay",
                "error": "TCP_TIMEOUT", "detail": "x" * 90},
        },
        "e2e_started": ["postgres", "controller"],
        "relay_containers": ["r1", "r2", "r3"],
        "relay_host_units": ["u1", "u2", "u3"],
        "relay_probe_containers": ["p1"],
        "firewall_state": "installed",
        # secret-adjacent journal fields that must NEVER surface
        "relay_script_path": "D:\\goai\\repo\\.mergepilot\\secrets\\relay.py",
        "hiclaw_receipt_path": "D:\\receipts\\receipt.json",
        "e2e_runtime_journal": {"controller": {"file": "x.env"}},
    }


class TestPublicStatusPayload(unittest.TestCase):

    def test_schema_and_whitelist(self):
        payload = mp.public_status_payload(_e2e_session())
        self.assertLessEqual(set(payload),
                             set(mp._PUBLIC_STATUS_KEYS))
        self.assertEqual(payload["run_id"], "b8-e2e-runX")
        self.assertTrue(payload["journal_complete"])
        self.assertEqual(payload["transport_profile"],
                         "wsl-user-relay")
        self.assertIs(payload["direct_routing_verified"], False)
        self.assertTrue(payload["receipt_verified"])
        self.assertEqual(
            payload["prerequisite_summary"],
            {"checks_passed": 16, "verified": True})
        self.assertEqual(payload["relay_resources"],
                         {"containers": 3, "host_units": 3,
                          "probe_containers": 1})
        self.assertEqual(
            sorted(payload["route_probes"]),
            ["bridge-to-proxy-b", "controller-to-tuwunel"])

    def test_no_paths_or_secret_adjacent_values_leak(self):
        payload = mp.public_status_payload(_e2e_session())
        blob = json.dumps(payload)
        for forbidden in ("relay.py", ".mergepilot", "receipt.json",
                          "x.env", "D:", "TCP_TIMEOUT", "error"):
            self.assertNotIn(forbidden, blob)

    def test_stale_stage_is_verbatim_never_complete(self):
        for stage in ("init", "route_probes", "gateway_start",
                      "failed_rolled_back"):
            session = _e2e_session()
            session["e2e_stage"] = stage
            payload = mp.public_status_payload(session)
            self.assertEqual(payload["e2e_stage"], stage)
            self.assertFalse(payload["journal_complete"])

    def test_default_mode_session_gets_minimal_projection(self):
        session = {"run_id": "demo-run", "stage": "healthy",
                   "github_e2e": False}
        payload = mp.public_status_payload(session)
        self.assertNotIn("e2e_stage", payload)
        self.assertNotIn("journal_complete", payload)


class TestWriteSessionDerivesProjection(unittest.TestCase):

    def test_write_session_writes_public_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            paths = {"state": state,
                     "session": state / "session.json"}
            mp.write_session(paths, _e2e_session())
            self.assertTrue((state / "session.json").exists())
            public = state / "public" / "status.json"
            self.assertTrue(public.exists())
            payload = json.loads(public.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "b8-e2e-runX")


class TestE2eStatusEndpoint(unittest.TestCase):
    """REAL in-process HTTP server exercising the handler."""

    def _serve(self, e2e_status_path):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), console_serve.LiveApiHandler)
        server.e2e_status_path = str(e2e_status_path)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()

        def _stop():
            # shutdown BEFORE join (addCleanup is LIFO: registering
            # join separately would run it while serve_forever still
            # loops and block forever)
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.addCleanup(_stop)
        return server

    def _get(self, server, path):
        url = "http://127.0.0.1:%d%s" % (server.server_address[1],
                                         path)
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_present_serves_projection_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            status.write_text(json.dumps(
                mp.public_status_payload(_e2e_session())),
                encoding="utf-8")
            server = self._serve(status)
            code, payload = self._get(server, "/api/e2e/status")
        self.assertEqual(code, 200)
        self.assertEqual(payload["run_id"], "b8-e2e-runX")
        self.assertTrue(payload["journal_complete"])

    def test_absent_reports_unavailable_never_synthesized(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            server = self._serve(missing)
            code, payload = self._get(server, "/api/e2e/status")
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"available": False})

    def test_unparseable_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "status.json"
            bad.write_text("not json", encoding="utf-8")
            server = self._serve(bad)
            code, payload = self._get(server, "/api/e2e/status")
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"available": False})

    def test_container_path_constant_frozen(self):
        self.assertEqual(
            console_serve.E2E_STATUS_CONTAINER_PATH,
            "/run/mergepilot/public/status.json")


if __name__ == "__main__":
    unittest.main()
