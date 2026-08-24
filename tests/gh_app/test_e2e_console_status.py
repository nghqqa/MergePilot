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
                          "x.env", "D:", "TCP_TIMEOUT", "FAILED"):
            self.assertNotIn(forbidden, blob)
        # the first stable error carries its CODE only — never the
        # lifecycle's free-form detail (which can embed log tails)
        err = payload.get("e2e_last_error") or {}
        self.assertLessEqual(set(err), {"code", "stage"})

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


class TestStageTimelineProjection(unittest.TestCase):
    """Productization round: the 17-stage timeline, the first stable
    error, and the truth boundaries are derived server-side from
    the journal; the UI renders, never re-derives."""

    def _with(self, **overrides):
        session = _e2e_session()
        for k, v in overrides.items():
            if v is None:
                session.pop(k, None)
            else:
                session[k] = v
        return mp.public_status_payload(session)

    def test_complete_run_all_stages_passed(self):
        p = self._with()
        self.assertEqual(len(p["stages"]), 17)
        self.assertTrue(all(s["status"] == "passed"
                            for s in p["stages"]))
        self.assertEqual([s["n"] for s in p["stages"]],
                         list(range(1, 18)))

    def test_failed_run_marks_only_reached_stage_failed(self):
        p = self._with(e2e_stage="route_probes",
                       e2e_last_error={
                           "code": "E2E_ROUTE_PROBE_FAILED",
                           "stage": "route_probes"})
        by_n = {s["n"]: s["status"] for s in p["stages"]}
        self.assertEqual(by_n[9], "passed")
        self.assertEqual(by_n[10], "failed")
        self.assertEqual(by_n[11], "pending")
        self.assertEqual(by_n[17], "pending")
        self.assertEqual(p["e2e_last_error"]["code"],
                         "E2E_ROUTE_PROBE_FAILED")
        self.assertFalse(p["journal_complete"])

    def test_in_flight_run_marks_reached_stage_running(self):
        p = self._with(e2e_stage="gateway_health")
        p.pop("e2e_last_error", None)
        by_n = {s["n"]: s["status"] for s in p["stages"]}
        self.assertEqual(by_n[10], "passed")
        self.assertEqual(by_n[11], "running")
        self.assertEqual(by_n[12], "pending")

    def test_unknown_marker_never_masquerades(self):
        p = self._with(e2e_stage="something_new")
        self.assertTrue(all(s["status"] == "unknown"
                            for s in p["stages"]))
        self.assertFalse(p["journal_complete"])

    def test_truth_boundaries_all_not_verified(self):
        p = self._with()
        self.assertEqual(sorted(p["truth_boundaries"]),
                         sorted(["application_integration_verified",
                                 "database_verified",
                                 "production_verified",
                                 "revision_producer_contract",
                                 "audit_producer_contract"]))
        self.assertTrue(all(
            v == "NOT_VERIFIED"
            for v in p["truth_boundaries"].values()))

    def test_route_segments_passthrough_and_legacy_none(self):
        # legacy journal (run35 shape): no segments → null passthrough
        p = self._with()
        self.assertIsNone(
            p["route_probes"]["controller-to-tuwunel"]["segment_a"])
        # newer journal carries segments dict
        session = _e2e_session()
        session["route_probe_results"]["controller-to-tuwunel"][
            "segments"] = {"segment_a": "TCP_CONNECTED",
                           "segment_b": "TCP_CONNECTED",
                           "application": "APPLICATION_VERIFIED"}
        p2 = mp.public_status_payload(session)
        edge = p2["route_probes"]["controller-to-tuwunel"]
        self.assertEqual(edge["segment_a"], "TCP_CONNECTED")
        self.assertEqual(edge["application"],
                         "APPLICATION_VERIFIED")

    def test_fail_helper_journals_first_stable_error_source(self):
        # source contract: _fail writes e2e_last_error BEFORE the
        # rollback raise (first error wins; persist precedes raise)
        src = (ROOT / "tools" / "cli" / "e2e_lifecycle.py"
               ).read_text(encoding="utf-8")
        marker_at = src.index('"e2e_last_error" not in session')
        persist_at = src.index("_persist()", marker_at)
        raise_at = src.index("diagnostics = _rollback_all(",
                             marker_at)
        self.assertLess(marker_at, persist_at)
        self.assertLess(persist_at, raise_at)


class TestConsolePageContracts(unittest.TestCase):
    """Source-level contracts for the operations page: read-only,
    no third-party code, refresh machinery, a11y primitives."""

    PAGE = (ROOT / "tools" / "demo_console" /
            "live_assets" / "e2e-status.html")

    @classmethod
    def setUpClass(cls):
        cls.src = cls.PAGE.read_text(encoding="utf-8")

    def test_csp_meta_present_and_minimal(self):
        self.assertIn('http-equiv="Content-Security-Policy"', self.src)
        self.assertIn("default-src 'none'", self.src)
        self.assertIn("connect-src 'self'", self.src)

    def test_no_third_party_or_remote_assets(self):
        # no remote src/href in the markup (inline script/style only)
        import re
        remote = re.findall(
            r'(?:src|href)\s*=\s*["\']https?://', self.src)
        self.assertEqual(remote, [])

    def test_no_write_methods_anywhere(self):
        for verb in ("method:", '"POST"', '"PUT"', '"DELETE"',
                     '"PATCH"'):
            self.assertNotIn(verb, self.src)

    def test_refresh_machinery_contracts(self):
        # dedup, bounded timeout, visibility pause/resume, stale
        self.assertIn("if (inflight) return", self.src)
        self.assertIn("AbortController", self.src)
        self.assertIn("visibilitychange", self.src)
        self.assertIn("STALE_MS", self.src)
        self.assertIn("prefers-reduced-motion", self.src)
        self.assertIn('aria-live="polite"', self.src)
        self.assertIn('role="status"', self.src)

    def test_chrome_advances_on_frozen_payload(self):
        # §8 regression: the fingerprint guard must not freeze the
        # time-dependent chrome — identical payloads still re-render
        # the verdict (stale must be able to appear) and the age
        # label; the heavy surface stays untouched (no layout jump)
        self.assertIn(
            "renderChrome(effective, verdictOf(effective))", self.src)
        self.assertIn("renderSurface(effective)", self.src)

    def test_freshness_age_is_projection_age(self):
        # §8 regression: the age label is derived from the PAYLOAD's
        # updated_utc (a frozen journal must read as aging), not from
        # the age of our fetch (which resets every tick)
        self.assertIn("Date.parse(d.updated_utc", self.src)
        self.assertNotIn("fmtAge(Date.now() - lastGoodAt)", self.src)

    def test_mobile_appbar_overflow_guards(self):
        # §8 regression: 390px viewport measured 621px scrollWidth
        # before these rules; appbar must wrap and shed chrome on
        # narrow viewports
        self.assertIn("flex-wrap: wrap", self.src)
        self.assertIn(".appbar .freshness .f-abs { display: none; }",
                      self.src)
        self.assertIn("max-width: 26vw", self.src)
        # the route table must swap to expandable cards on mobile
        self.assertIn("table.routes { display: none; }", self.src)
        self.assertIn(".edge-cards { display: block;", self.src)

    def test_dimmest_text_meets_aa_on_worst_surface(self):
        # --text-3 is the dimmest text; its worst background is
        # --surface-2 (#eceded). #656c76 computes 4.52:1 there.
        # --pending-c sits on the same inset (待执行 chips) and was
        # #8d939b (2.64:1) before the finish review.
        self.assertIn("--text-3: #656c76", self.src)
        self.assertIn("--pending-c: #656c76", self.src)

    def test_no_fabricated_prerequisite_denominator(self):
        # finish review: "/16" invented a total the projection never
        # carries (checks_passed only). The bare count must render.
        self.assertNotIn('"/16"', self.src)
        self.assertNotIn('+ "/16"', self.src)

    def test_empty_route_probes_render_explicit_na(self):
        # finish review: zero edges must never render a header-only
        # table or an empty mobile card box
        self.assertIn("路由探测未提供", self.src)
        self.assertIn("names.length === 0", self.src)

    def test_honesty_guards_present(self):
        # false direct routing renders as false, never verified;
        # unavailable renders explicitly, never complete
        self.assertIn('"false（经中继）"', self.src)
        self.assertIn("无 E2E 会话投影", self.src)
        self.assertIn("NOT_VERIFIED", self.src)
        self.assertIn("未提供", self.src)

    def test_no_secret_patterns_in_page(self):
        for forbidden in ("ghp_", "github_pat_", "BEGIN PRIVATE KEY",
                          "syt_", "access_token", "PG_PASS",
                          "ADMIN_PW", "Bearer "):
            self.assertNotIn(forbidden, self.src)

    def test_edge_allowlist_carries_console_paths(self):
        import console_edge as edge
        self.assertIn("/e2e-status.html", edge.ALLOWED_PATHS)
        self.assertIn("/api/e2e/status", edge.ALLOWED_PATHS)
        self.assertNotIn("*", edge.ALLOWED_PATHS)


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
