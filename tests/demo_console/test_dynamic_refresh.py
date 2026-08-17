"""ISOLATED_LIVE Productization Phase 1-E — dynamic refresh tests.

Covers the eight-page dynamic refresh engine:
  - the Python contract twin (live_refresh.LiveRefreshController) for the
    full behavior matrix (initial load / success / consecutive failure /
    recovery / no snapshot / manual refresh / single timer / cleanup /
    no-fallback / shared store);
  - verify_js_contract on the REAL tools/demo_console/live_assets/
    live-refresh.js (a NON-protected path — samples/ is frozen REPLAY);
  - negative mutations (each contract violation must be caught);
  - index.html wiring + Dockerfile shipping;
  - the serve.py fail-closed startup gate.

No WSL/Docker/PostgreSQL; no real HTTP in these tests (source + twin only).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(ROOT), str(ROOT / "tools" / "demo_console")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from live_refresh import (  # noqa: E402
    ALLOWED_URLS,
    DEFAULT_INTERVAL_MS,
    MAX_CONSECUTIVE_FAILURES,
    MAX_INTERVAL_MS,
    MIN_INTERVAL_MS,
    PAGES,
    PAGE_BUNDLE_FIELDS,
    SNAPSHOT_URL,
    STATUS_URL,
    LiveRefreshController,
    RefreshContractError,
    clamp_interval_ms,
    verify_js_contract,
)

JS_PATH = ROOT / "tools" / "demo_console" / "live_assets" / "live-refresh.js"
HTML_PATH = (ROOT / "tools" / "demo_console" / "live_assets" /
             "index.html")
DOCKERFILE = ROOT / "Dockerfile.demo-console"
SERVE_PATH = ROOT / "tools" / "demo_console" / "serve.py"

JS_SOURCE = JS_PATH.read_text(encoding="utf-8")
HTML_SOURCE = HTML_PATH.read_text(encoding="utf-8")


def _snapshot(sha="a" * 64, generated_at="2026-08-15T12:00:00Z"):
    bundle = {field: [] for field in (
        "workflow_stages", "agents", "findings", "fixes",
        "rag_advisories", "spans", "rollback_events", "evidence_files")}
    bundle.update({
        "bundle_sha256": sha,
        "generated_at": generated_at,
        "run": {"run_id": "run-1"},
        "pr": {"number": 42},
        "final_status": "PASS",
        "verifier_result": {"status": "PASS"},
        "residue": {"containers": 0, "networks": 0, "temp_dirs": 0},
        "secret_leaks": 0,
        "source_commit": "a" * 40,
        "verification_commit": "b" * 40,
        "benchmark_summary": {"dataset_version": "v3"},
    })
    return bundle


def _status():
    return {"poller_state": "LIVE", "poll_count": 7,
            "last_success_at": "2026-08-15T12:00:00Z"}


# ── Contract constants ───────────────────────────────────────────────────────

class TestContractConstants(unittest.TestCase):

    def test_allowlisted_urls_exactly_two_read_only_endpoints(self):
        self.assertEqual(ALLOWED_URLS,
                         {"/api/live/status", "/api/live/snapshot"})
        self.assertEqual(STATUS_URL, "/api/live/status")
        self.assertEqual(SNAPSHOT_URL, "/api/live/snapshot")

    def test_interval_bounds(self):
        self.assertGreaterEqual(MIN_INTERVAL_MS, 2000)
        self.assertLess(MIN_INTERVAL_MS, DEFAULT_INTERVAL_MS)
        self.assertGreater(MAX_INTERVAL_MS, DEFAULT_INTERVAL_MS)

    def test_eight_pages_and_field_map(self):
        self.assertEqual(len(PAGES), 8)
        self.assertEqual(set(PAGES), set(PAGE_BUNDLE_FIELDS))
        # Every mapped field is a real bundle field (schema contract).
        from schema import REQUIRED_FIELDS
        for page, fields in PAGE_BUNDLE_FIELDS.items():
            for field in fields:
                self.assertIn(field, REQUIRED_FIELDS, (page, field))

    def test_clamp_interval(self):
        self.assertEqual(clamp_interval_ms(1), MIN_INTERVAL_MS)
        self.assertEqual(clamp_interval_ms(MIN_INTERVAL_MS - 1),
                         MIN_INTERVAL_MS)
        self.assertEqual(clamp_interval_ms(10 ** 9), MAX_INTERVAL_MS)
        self.assertEqual(clamp_interval_ms(7000), 7000)
        for bad in (0, -5, "3000", 2.5, True, None):
            with self.assertRaises(RefreshContractError):
                clamp_interval_ms(bad)


# ── State-machine twin: behavior matrix ──────────────────────────────────────

class TestControllerBehavior(unittest.TestCase):

    def _controller(self):
        c = LiveRefreshController(interval_ms=5000)
        seen = []
        for page in PAGES:
            c.register_page(page, lambda snap, page=page: seen.append(page))
        return c, seen

    def test_initial_state_and_placeholder(self):
        c, _ = self._controller()
        self.assertEqual(c.state, "INIT")
        html = c.render_page("overview")
        self.assertIn("live-placeholder", html)
        self.assertIn("awaiting first live snapshot", html)

    def test_start_shows_loading_and_single_timer(self):
        c, _ = self._controller()
        self.assertTrue(c.start())
        self.assertEqual(c.state, "LOADING")
        self.assertEqual(c.timer_create_count, 1)
        # Second start must NOT create a second timer.
        self.assertFalse(c.start())
        self.assertEqual(c.timer_create_count, 1)

    def test_success_updates_all_pages_from_shared_store(self):
        c, seen = self._controller()
        c.start()
        seen.clear()
        c.on_success(_status(), _snapshot())
        self.assertEqual(c.state, "OK")
        self.assertEqual(seen, list(PAGES))       # all 8 pages re-rendered
        self.assertEqual(c.consecutive_failures, 0)
        self.assertEqual(c.last_success_at, "2026-08-15T12:00:00Z")

    def test_pages_share_one_store_object(self):
        holders = []
        c = LiveRefreshController()
        for page in PAGES:
            c.register_page(page, lambda snap: holders.append(snap))
        snap = _snapshot()
        c.on_success(_status(), snap)
        # Every renderer received the SAME object — no per-page copies.
        self.assertEqual(len(holders), len(PAGES))
        for h in holders:
            self.assertIs(h, snap)

    def test_http_and_json_error_transitions(self):
        c, _ = self._controller()
        c.start()
        c.on_http_error(500)
        self.assertEqual(c.state, "HTTP_ERROR")
        self.assertEqual(c.consecutive_failures, 1)
        c.on_json_error()
        self.assertEqual(c.state, "JSON_ERROR")
        self.assertEqual(c.consecutive_failures, 2)

    def test_no_snapshot_counts_as_failure(self):
        c, _ = self._controller()
        c.start()
        snap = _snapshot()
        snap["bundle_sha256"] = None
        c.on_success(_status(), snap)
        self.assertEqual(c.state, "NO_SNAPSHOT")
        self.assertEqual(c.consecutive_failures, 1)
        self.assertIsNone(c.snapshot)             # never stored

    def test_consecutive_failures_reach_stale_and_stop_timer(self):
        c, _ = self._controller()
        c.start()
        for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
            c.on_http_error(500)
        self.assertEqual(c.state, "HTTP_ERROR")
        self.assertIsNotNone(c.timer_handle)
        c.on_http_error(500)                      # the MAX-th failure
        self.assertEqual(c.state, "STALE")
        self.assertIsNone(c.timer_handle)         # timer cleaned up
        self.assertEqual(c.timer_create_count, 1)  # never re-created

    def test_recovery_resets_failure_budget(self):
        c, _ = self._controller()
        c.start()
        for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
            c.on_http_error(500)
        c.on_success(_status(), _snapshot())      # recovery
        self.assertEqual(c.state, "OK")
        self.assertEqual(c.consecutive_failures, 0)

    def test_manual_refresh_restarts_after_stale(self):
        c, _ = self._controller()
        c.start()
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            c.on_json_error()
        self.assertEqual(c.state, "STALE")
        self.assertIsNone(c.timer_handle)
        self.assertTrue(c.manual_refresh())
        self.assertEqual(c.state, "LOADING")
        self.assertEqual(c.timer_create_count, 2)  # re-created exactly once
        self.assertEqual(c.consecutive_failures, 0)

    def test_stop_cleanup_idempotent(self):
        c, _ = self._controller()
        c.start()
        self.assertTrue(c.stop())
        self.assertIsNone(c.timer_handle)
        self.assertFalse(c.stop())                # idempotent

    def test_no_fallback_store_exists(self):
        c, _ = self._controller()
        c.start()
        c.on_http_error(500)
        # The engine holds no static/REPLAY/baked store to fall back to.
        for attr in ("static", "replay", "baked", "fallback", "cached"):
            self.assertFalse(hasattr(c, attr), attr)
        # After failures with no prior success the page STILL shows the
        # placeholder (never fabricated data).
        self.assertIn("live-placeholder", c.render_page("findings"))

    def test_last_live_data_kept_on_failure_with_banner_stale(self):
        c = LiveRefreshController()
        c.register_page("evidence",
                        lambda snap: "Bundle " + snap["bundle_sha256"])
        c.start()
        c.on_success(_status(), _snapshot())
        c.on_http_error(500)
        self.assertEqual(c.state, "HTTP_ERROR")
        self.assertIsNotNone(c.snapshot)          # last LIVE data kept
        self.assertIn("Bundle " + "a" * 64, c.render_page("evidence"))
        f = c.freshness()
        self.assertEqual(f["state"], "HTTP_ERROR")
        self.assertIsNotNone(f["data_time"])
        self.assertEqual(f["snapshot_sha256"], "a" * 64)

    def test_freshness_contract(self):
        c, _ = self._controller()
        c.on_success(_status(), _snapshot())
        f = c.freshness()
        for key in ("state", "data_time", "poll_state", "poll_count",
                    "consecutive_failures", "interval_ms",
                    "snapshot_sha256"):
            self.assertIn(key, f)
        self.assertEqual(f["poll_state"], "LIVE")

    def test_page_registration_guards(self):
        c = LiveRefreshController()
        c.register_page("overview", lambda s: "")
        with self.assertRaises(RefreshContractError):
            c.register_page("nope", lambda s: "")
        with self.assertRaises(RefreshContractError):
            c.register_page("overview", lambda s: "")
        with self.assertRaises(RefreshContractError):
            c.render_page("nope")

    def test_on_success_type_guards(self):
        c = LiveRefreshController()
        with self.assertRaises(RefreshContractError):
            c.on_success(None, {})
        with self.assertRaises(RefreshContractError):
            c.on_success({}, "not-a-dict")


# ── JS contract: the real file validates; mutations are caught ─────────────

class TestJsContractRealFile(unittest.TestCase):

    def test_real_js_validates(self):
        result = verify_js_contract(JS_SOURCE)
        self.assertTrue(result["ok"])
        self.assertEqual(result["pages"], list(PAGES))
        self.assertEqual(result["min_interval_ms"], MIN_INTERVAL_MS)

    def test_single_fetch_implementation(self):
        # ONE fetch wrapper; the two allowlisted URLs are its only callers.
        self.assertEqual(JS_SOURCE.count("fetch("), 1)

    def test_getjson_only_targets_allowlisted_urls(self):
        import re
        calls = re.findall(r"(?<!function )getJson\((\w+)\)", JS_SOURCE)
        self.assertTrue(calls)
        for name in set(calls):
            definition = re.search(
                r"var %s = ('[^']*'|\"[^\"]*\")" % name, JS_SOURCE)
            self.assertIsNotNone(definition, name)
            url = definition.group(1).strip("'\"")
            self.assertIn(url, ALLOWED_URLS, name)

    def test_replay_mode_never_starts_engine(self):
        # The boot probe failure path leaves state INIT without rendering
        # anything — REPLAY (404) keeps the frozen page untouched.
        self.assertIn("state = 'INIT';", JS_SOURCE)


class TestJsContractMutations(unittest.TestCase):
    """Each mutation must be caught by verify_js_contract (fail-closed)."""

    def _reject(self, mutated):
        with self.assertRaises(RefreshContractError):
            verify_js_contract(mutated)

    def test_extra_endpoint_rejected(self):
        self._reject(JS_SOURCE.replace(
            "var STATUS_URL = '/api/live/status';",
            "var STATUS_URL = '/api/live/status';\n"
            "var X = '/api/live/other';"))

    def test_post_method_rejected(self):
        self._reject(JS_SOURCE.replace("{method: 'GET', cache: 'no-store'}",
                                       "{method: 'POST'}"))

    def test_second_setinterval_rejected(self):
        self._reject(JS_SOURCE + "\nsetInterval(function(){}, 1000);\n")

    def test_missing_clearinterval_rejected(self):
        self._reject(JS_SOURCE.replace("clearInterval(timer);", ";"))

    def test_missing_pagehide_rejected(self):
        self._reject(JS_SOURCE.replace(
            "window.addEventListener('pagehide', stopTimer);", ""))

    def test_missing_interval_minimum_rejected(self):
        self._reject(JS_SOURCE.replace("var MIN_INTERVAL_MS = 2000;",
                                       "var MIN_INTERVAL_MS = 10;"))

    def test_missing_page_rejected(self):
        # Remove EVERY occurrence of the page id (nav list, renderer map,
        # functions) so no textual remnant satisfies the binding check.
        self._reject(JS_SOURCE.replace("trace", "tracex"))

    def test_missing_bundle_field_rejected(self):
        self._reject(JS_SOURCE.replace("rag_advisories", "advisories"))

    def test_reload_marker_rejected(self):
        self._reject(JS_SOURCE + "\nwindow.location.reload();\n")

    def test_replay_literal_rejected(self):
        self._reject(JS_SOURCE + "\nvar mode = 'REPLAY';\n")

    def test_empty_source_rejected(self):
        self._reject("")


# ── index.html wiring + shipping ─────────────────────────────────────────────

class TestHtmlWiring(unittest.TestCase):

    def test_eight_sections_present(self):
        import re
        sections = re.findall(r'<section id="(\w+)" class="page', HTML_SOURCE)
        self.assertEqual(sections, list(PAGES))

    def test_engine_script_banner_button_present(self):
        self.assertIn('<script src="live-refresh.js"></script>', HTML_SOURCE)
        self.assertIn('id="live-banner"', HTML_SOURCE)
        self.assertIn('id="live-refresh-btn"', HTML_SOURCE)

    def test_placeholder_css_present(self):
        self.assertIn(".live-placeholder", HTML_SOURCE)

    def test_dockerfile_ships_live_assets_only(self):
        body = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn(
            "COPY tools/demo_console/live_assets /app/live-console", body)
        # Protected-path contract: nothing is copied from samples/.
        self.assertNotIn("COPY samples/", body)

    def test_live_assets_are_not_under_protected_prefixes(self):
        from evidence_manifest import PROTECTED_PATH_PREFIXES
        self.assertIn("samples/", PROTECTED_PATH_PREFIXES)
        for path in (JS_PATH, HTML_PATH):
            rel = path.relative_to(ROOT).as_posix()
            for prefix in PROTECTED_PATH_PREFIXES:
                self.assertFalse(
                    rel.startswith(prefix), (rel, prefix))

    def test_samples_tree_stays_replay_frozen(self):
        # The M7 REPLAY sample set carries NO dynamic-refresh artifacts.
        samples_dir = ROOT / "samples" / "demo-console"
        self.assertFalse(
            (samples_dir / "live-refresh.js").exists(),
            "live-refresh.js must not live in the protected samples/ tree")
        original = samples_dir.joinpath("index.html").read_text(
            encoding="utf-8")
        self.assertNotIn("live-refresh.js", original)
        self.assertNotIn("live-banner", original)
        # The live copy still exposes the same eight sections.
        import re
        for source in (original, HTML_SOURCE):
            self.assertEqual(
                re.findall(r'<section id="(\w+)" class="page', source),
                list(PAGES))

    def test_no_secret_shaped_patterns_in_frontend(self):
        import re
        patterns = (r"password\s*=", r"postgresql(ql)?://", r"dsn",
                    r"ghp_[0-9a-zA-Z]{36}", r"sk-[a-zA-Z0-9]{40}",
                    r"Authorization\s*:", r":\/\/[^/'\"]+:[^@/'\"]+@")
        for blob, name in ((JS_SOURCE, "js"), (HTML_SOURCE, "html")):
            for pat in patterns:
                self.assertIsNone(
                    re.search(pat, blob, re.IGNORECASE), (name, pat))


# ── UI mode semantics (Phase 1-E final fix) ─────────────────────────────────

class TestModeSemantics(unittest.TestCase):
    """The live asset must never present itself as REPLAY; the frozen M7
    REPLAY sample must keep presenting itself as REPLAY."""

    def test_live_asset_title_is_isolated_live(self):
        self.assertIn(
            "<title>MergePilot · Demo Console (ISOLATED_LIVE)</title>",
            HTML_SOURCE)

    def test_live_asset_mode_banner_is_isolated_live(self):
        # Phase 1-F markup: the mode badge is a .mode-banner element in the
        # topbar; the semantic contract (visible ISOLATED_LIVE) is exact.
        self.assertRegex(
            HTML_SOURCE,
            r'<span class="mode-banner">MODE: ISOLATED_LIVE</span>')

    def test_live_asset_never_claims_replay(self):
        self.assertNotIn("MODE: REPLAY", HTML_SOURCE)
        self.assertNotIn("Demo Console (REPLAY)", HTML_SOURCE)
        self.assertNotIn("replay payload", HTML_SOURCE)
        self.assertIn("INIT · awaiting live status", HTML_SOURCE)

    def test_frozen_sample_keeps_replay_semantics(self):
        frozen = (ROOT / "samples" / "demo-console" /
                  "index.html").read_text(encoding="utf-8")
        self.assertIn(
            "<title>MergePilot · Demo Console (REPLAY)</title>", frozen)
        self.assertIn("MODE: REPLAY", frozen)
        self.assertNotIn("live-refresh.js", frozen)

    def test_samples_tree_diff_against_origin_main_is_empty(self):
        import subprocess
        try:
            out = subprocess.run(
                ["git", "diff", "--name-only",
                 "6f51f85fe03bc8f83092c1ba40f2a4a339f116cc..HEAD",
                 "--", "samples/"],
                capture_output=True, text=True, check=True,
                cwd=str(ROOT))
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git unavailable")
        self.assertEqual(out.stdout.strip(), "",
                         "samples/ must be byte-identical to origin/main")


# ── Phase 1-F UI structure ───────────────────────────────────────────────────

class TestUiStructure(unittest.TestCase):
    """Dashboard layout contract (Phase 1-F)."""

    def test_sidebar_nav_binds_all_eight_views(self):
        import re
        targets = re.findall(
            r'class="nav-btn[^"]*" data-target="(\w+)"', HTML_SOURCE)
        self.assertEqual(targets, list(PAGES))
        # Exactly one nav button starts active (overview).
        self.assertEqual(
            HTML_SOURCE.count('class="nav-btn active"'), 1)

    def test_viewport_meta_present(self):
        self.assertIn(
            '<meta name="viewport" content="width=device-width, '
            'initial-scale=1">', HTML_SOURCE)

    def test_mobile_overflow_guards_present(self):
        # Page-level horizontal overflow is blocked; the ONLY horizontal
        # scroll surface is the collapsed nav itself on narrow screens.
        self.assertIn("overflow-x: hidden", HTML_SOURCE)
        self.assertIn("@media (max-width: 880px)", HTML_SOURCE)
        self.assertIn("minmax(0, 1fr)", HTML_SOURCE)
        # Long values (SHAs, paths, messages) wrap instead of exploding
        # the layout.
        self.assertIn("overflow-wrap: anywhere", HTML_SOURCE)

    def test_state_chip_styles_for_all_seven_states(self):
        for state, cls in (
                ("OK", "st-ok"), ("LOADING", None), ("STALE", None),
                ("HTTP_ERROR", None), ("JSON_ERROR", None),
                ("NO_SNAPSHOT", None), ("INIT", None)):
            self.assertIn('data-state="%s"' % state, HTML_SOURCE + JS_SOURCE)
        for cls in ("st-ok", "st-warn", "st-err", "st-dim"):
            self.assertIn(".%s" % cls, HTML_SOURCE)

    def test_boolean_status_chip_semantics_are_reachable(self):
        self.assertIn(
            "value === null || value === undefined ? '' : value",
            JS_SOURCE,
        )
        self.assertIn("v === 'TRUE'", JS_SOURCE)
        self.assertIn("v === 'FALSE'", JS_SOURCE)
        self.assertNotIn("String(value || '')", JS_SOURCE)
        self.assertNotIn("v === 'True'", JS_SOURCE)
        self.assertNotIn("v === 'False'", JS_SOURCE)

    def test_refresh_control_semantics_in_js(self):
        # Disabled during LOADING; STALE relabels to a resume action.
        self.assertIn("btn.disabled = (state === 'LOADING');", JS_SOURCE)
        self.assertIn("'Resume auto-refresh'", JS_SOURCE)

    def test_no_cdn_or_external_resources(self):
        import re
        for blob, name in ((HTML_SOURCE, "html"), (JS_SOURCE, "js")):
            self.assertIsNone(
                re.search(r'https?://(?!127\.0\.0\.1|localhost)',
                          blob), name)
        self.assertNotIn("@import", HTML_SOURCE)
        self.assertNotIn("fonts.googleapis", HTML_SOURCE)

    def test_all_dynamic_interpolations_are_escaped(self):
        import re
        # Any '+ identifier.chain +' string-concat interpolation that is
        # not wrapped in esc(…) is an escaping bug.
        raw = re.findall(r"\+\s*(?!esc\()[a-z_][\w.]*\.[\w]+\s*\+",
                         JS_SOURCE)
        self.assertEqual(raw, [])
        # esc() neutralizes the five HTML-significant characters.
        self.assertIn("replace(/&/g, '&amp;')", JS_SOURCE)
        self.assertIn("replace(/</g, '&lt;')", JS_SOURCE)
        self.assertIn("replace(/>/g, '&gt;')", JS_SOURCE)
        self.assertIn("&quot;", JS_SOURCE)
        self.assertIn("&#39;", JS_SOURCE)

    def test_banner_is_structured_with_role_status(self):
        self.assertIn('id="live-banner"', HTML_SOURCE)
        self.assertIn('role="status"', HTML_SOURCE)

    def test_panels_and_boundary_banners_present(self):
        for cls in ("panel", "kv", "metrics", "boundary-banner",
                    "timeline", "tree", "skill-grid", "data-table"):
            self.assertIn(cls, HTML_SOURCE)
            self.assertIn(cls, JS_SOURCE)


# ── Showcase PR-V1: operational design system tests ─────────────────────────

class TestShowcaseDesignSystem(unittest.TestCase):
    """Visual system invariants for the portfolio showcase round."""

    def test_css_token_set_exists(self):
        for token in ("--bg:", "--bg-panel:", "--bg-raised:", "--border:",
                      "--text:", "--accent:", "--ok:", "--warn:", "--err:",
                      "--mono:", "--sans:", "--radius:", "--sp-1:",
                      "--transition:", "--shadow-1:", "--shadow-focus"):
            self.assertIn(token, HTML_SOURCE, token)

    def test_eight_page_header_structure_in_js(self):
        # Every renderer emits the shared page-head scaffold.
        for page in PAGES:
            self.assertIn(
                "pageHeader('%s')" % page, JS_SOURCE, page)

    def test_page_meta_has_all_eight_pages(self):
        import re
        meta_keys = re.findall(
            r"^\s{4}(\w+):\s*\{", JS_SOURCE, re.MULTILINE)
        for page in PAGES:
            self.assertIn(page, meta_keys, page)

    def test_page_meta_fields(self):
        for field in ("eyebrow", "title", "desc"):
            self.assertIn(field + ":", JS_SOURCE)

    def test_focus_visible_styles(self):
        self.assertIn(":focus-visible", HTML_SOURCE)
        self.assertIn("outline: 2px solid var(--accent)", HTML_SOURCE)

    def test_skip_link_present(self):
        self.assertIn('class="skip-link"', HTML_SOURCE)
        self.assertIn('href="#main-content"', HTML_SOURCE)

    def test_reduced_motion_media_query(self):
        self.assertIn("@media (prefers-reduced-motion: reduce)", HTML_SOURCE)
        self.assertIn("animation-duration: 0.01ms !important", HTML_SOURCE)
        self.assertIn("transition-duration: 0.01ms !important", HTML_SOURCE)

    def test_high_contrast_media_query(self):
        self.assertIn("@media (prefers-contrast: high)", HTML_SOURCE)

    def test_aria_landmarks_present(self):
        self.assertIn('role="status"', HTML_SOURCE)
        self.assertIn('aria-live="polite"', HTML_SOURCE)
        self.assertIn('aria-label="Views"', HTML_SOURCE)
        self.assertIn('id="main-content"', HTML_SOURCE)

    def test_aria_current_page_management(self):
        self.assertIn("aria-current", HTML_SOURCE)  # inline nav script

    def test_table_wrapper_for_mobile_scroll(self):
        self.assertIn("table-wrap", HTML_SOURCE)
        self.assertIn("overflow-x: auto", HTML_SOURCE)
        self.assertIn("tableWrap(", JS_SOURCE)

    def test_sticky_table_headers(self):
        self.assertIn("position: sticky; top: 0", HTML_SOURCE)

    def test_status_chip_has_indicator_dot(self):
        # Status is not conveyed by color alone — chips carry a shape.
        self.assertIn(".st::before", HTML_SOURCE)

    def test_no_extra_fetch_calls(self):
        import re
        fetches = re.findall(r"fetch\(", JS_SOURCE)
        self.assertEqual(len(fetches), 1,
                         "exactly one fetch wrapper allowed")
        # No localStorage or sessionStorage.
        for banned in ("localStorage", "sessionStorage",
                       "location.reload", "window.open"):
            self.assertNotIn(banned, JS_SOURCE, banned)
            self.assertNotIn(banned, HTML_SOURCE, banned)

    def test_single_timer_contract(self):
        # Exactly one executable setInterval call (the comment on the
        # variable declaration is not a second timer).
        import re
        code_setintervals = re.findall(
            r"^\s*.*[^/*]\bsetInterval\(", JS_SOURCE, re.MULTILINE)
        self.assertEqual(len(code_setintervals), 1)
        self.assertEqual(JS_SOURCE.count("clearInterval"), 1)
        self.assertIn("if (timer !== null) { return; }", JS_SOURCE)

    def test_pages_order_unchanged(self):
        self.assertEqual(
            list(PAGES),
            ['overview', 'timeline', 'findings', 'rag',
             'trace', 'safety', 'evidence', 'benchmark'])

    def test_active_nav_count_in_html(self):
        self.assertEqual(
            HTML_SOURCE.count('class="nav-btn active"'), 1)

    def test_keyboard_arrow_navigation(self):
        self.assertIn("ArrowDown", HTML_SOURCE)
        self.assertIn("ArrowUp", HTML_SOURCE)
        self.assertIn("e.preventDefault()", HTML_SOURCE)

    def test_panel_title_accent_bar(self):
        self.assertIn(".panel-title::before", HTML_SOURCE)

    def test_evidence_row_component(self):
        self.assertIn("evidence-row", HTML_SOURCE)
        self.assertIn("evidenceRow(", JS_SOURCE)

    def test_metric_card_hover(self):
        self.assertIn(".metric:hover", HTML_SOURCE)

    def test_no_ninth_page(self):
        import re
        sections = re.findall(
            r'<section id="(\w+)" class="page', HTML_SOURCE)
        self.assertEqual(len(sections), 8)
        self.assertEqual(sorted(sections), sorted(PAGES))


# ── serve.py fail-closed startup gate ────────────────────────────────────────

class TestServeStartupGate(unittest.TestCase):

    def test_main_validates_js_before_serving(self):
        src = SERVE_PATH.read_text(encoding="utf-8")
        self.assertIn("verify_js_contract", src)
        # The gate runs BEFORE the server socket is created.
        self.assertLess(src.index("verify_js_contract"),
                        src.index("create_server(args.host"))

    def test_gate_fails_closed_on_missing_js(self):
        # Simulated: a serve dir without live-refresh.js → CONFIG_INVALID
        # (the exact message the gate prints).
        src = SERVE_PATH.read_text(encoding="utf-8")
        self.assertIn("CONFIG_INVALID: live-refresh.js missing", src)

    def test_gate_fails_closed_on_contract_violation(self):
        src = SERVE_PATH.read_text(encoding="utf-8")
        self.assertIn("violates the dynamic", src)


if __name__ == "__main__":
    unittest.main()
