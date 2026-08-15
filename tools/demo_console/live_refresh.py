"""ISOLATED_LIVE dynamic-refresh contract (Productization Phase 1-E).

Two load-bearing roles:

1. The EXECUTABLE CONTRACT for the browser refresh engine. The concrete
   runtime is ``tools/demo_console/live_assets/live-refresh.js`` (a
   NON-protected path — samples/ is in PROTECTED_PATH_PREFIXES); this
   module is the
   authoritative Python twin of that state machine, exercised directly by
   the test suite (initial load / success / consecutive failure /
   recovery / no snapshot / manual refresh / single timer / cleanup /
   no-fallback). The JS must mirror these semantics exactly.

2. A fail-closed SOURCE VALIDATOR (:func:`verify_js_contract`). serve.py
   runs it at startup against the real ``live-refresh.js``: if the JS
   drifts from the contract (non-allowlisted URL, non-GET method, missing
   timer cleanup, missing page bindings, REPLAY-fallback markers), the
   console refuses to serve dynamic refresh — drift cannot ship silently.

Hard invariants (Phase 1-E requirements):
  - Only the two existing read-only endpoints are ever requested, GET only:
    /api/live/status and /api/live/snapshot.
  - The refresh interval is configurable but clamped to a minimum
    (MIN_INTERVAL_MS) — unbounded request rates are impossible.
  - A refresh failure NEVER falls back to REPLAY, static/baked data, or
    fabricated data. In live mode the baked REPLAY payload is replaced
    with an explicit loading placeholder BEFORE the first success; after a
    success the last LIVE data is kept and marked stale. The engine never
    holds or restores baked content.
  - After MAX_CONSECUTIVE_FAILURES the auto-refresh timer stops (cleanup);
    manual refresh remains available and restarts the timer.
  - All 8 pages share ONE snapshot store — no per-page fetching, no
    cross-page data drift.
  - No secrets (DSN/password/token) ever appear in URLs, the DOM, or logs.

Frozen truth boundaries are unchanged by anything here.
"""

from __future__ import annotations

import re

# ── Contract constants (mirrored verbatim in live-refresh.js) ───────────────

STATUS_URL = "/api/live/status"
SNAPSHOT_URL = "/api/live/snapshot"
ALLOWED_URLS = frozenset({STATUS_URL, SNAPSHOT_URL})

DEFAULT_INTERVAL_MS = 5000
MIN_INTERVAL_MS = 2000          # hard floor — no unbounded request rate
MAX_INTERVAL_MS = 600000        # hard ceiling — a stalled page still refreshes
MAX_CONSECUTIVE_FAILURES = 10   # mirrors the server poller's threshold

STATES = (
    "INIT",          # engine created, nothing fetched yet
    "LOADING",       # first fetch in flight (pages show placeholders)
    "OK",            # latest refresh succeeded
    "HTTP_ERROR",    # non-200 from status or snapshot
    "JSON_ERROR",    # body did not parse as JSON
    "NO_SNAPSHOT",   # snapshot unavailable (503 / no bundle_sha256)
    "STALE",         # consecutive failures exhausted; timer STOPPED
)

# The 8 pages of the live console (section.page ids, mirrored from the
# M7 REPLAY sample layout) and the
# bundle fields each page consumes (schema.py REQUIRED_FIELDS). One shared
# snapshot serves all eight — no page ever fetches on its own.
PAGES = (
    "overview", "timeline", "findings", "rag",
    "trace", "safety", "evidence", "benchmark",
)

PAGE_BUNDLE_FIELDS = {
    "overview": ("run", "pr", "final_status", "workflow_stages", "agents",
                 "fixes", "verifier_result"),
    "timeline": ("workflow_stages",),
    "findings": ("findings",),
    "rag": ("rag_advisories",),
    "trace": ("spans",),
    "safety": ("residue", "secret_leaks", "rollback_events"),
    "evidence": ("bundle_sha256", "source_commit", "verification_commit",
                 "evidence_files"),
    "benchmark": ("benchmark_summary",),
}


class RefreshContractError(Exception):
    """Stable CONFIG_INVALID-style violation of the Phase 1-E contract."""


def clamp_interval_ms(value) -> int:
    """Resolve a configured interval to a safe millisecond value.

    Non-integer or non-positive input raises (fail-closed — an ambiguous
    interval is a configuration error, never a guess). Values below
    MIN_INTERVAL_MS are clamped UP; values above MAX_INTERVAL_MS are
    clamped DOWN. The clamped value is returned.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise RefreshContractError(
            "CONFIG_INVALID: refresh interval must be an integer "
            " (got %s)" % type(value).__name__)
    if value <= 0:
        raise RefreshContractError(
            "CONFIG_INVALID: refresh interval must be positive (got %d)"
            % value)
    return max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, value))


# ── Executable state-machine twin of live-refresh.js ────────────────────────

class LiveRefreshController:
    """Pure state machine mirroring the browser engine (no I/O).

    Timer bookkeeping is represented by ``timer_handle`` (None = no timer)
    plus ``timer_create_count`` so tests can assert single creation and
    correct cleanup. All page renderers read the SAME ``snapshot`` store.
    """

    def __init__(self, interval_ms=DEFAULT_INTERVAL_MS):
        self.interval_ms = clamp_interval_ms(interval_ms)
        self.state = "INIT"
        self.timer_handle = None
        self.timer_create_count = 0
        self.consecutive_failures = 0
        self.snapshot = None            # ONE shared store for all 8 pages
        self.status = None
        self.last_success_at = None
        self._renderers = {}

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        """Start the refresh timer. Creates it exactly once."""
        if self.timer_handle is not None:
            return False
        self.timer_handle = object()    # the JS: setInterval(...)
        self.timer_create_count += 1
        self.state = "LOADING"
        # Fail-closed page init: in live mode the baked REPLAY payload is
        # replaced by placeholders BEFORE any data arrives — baked content
        # is never displayed as if it were live data.
        self._render_all()
        return True

    def stop(self):
        """Stop and clear the timer (page unload / staleness shutdown)."""
        if self.timer_handle is None:
            return False
        self.timer_handle = None
        return True

    # ── refresh outcomes ───────────────────────────────────────────────────
    def on_success(self, status: dict, snapshot: dict) -> None:
        if not isinstance(status, dict) or not isinstance(snapshot, dict):
            raise RefreshContractError(
                "CONFIG_INVALID: on_success requires dict payloads")
        if not snapshot.get("bundle_sha256"):
            self.on_no_snapshot()
            return
        self.status = status
        self.snapshot = snapshot       # the single shared store
        self.consecutive_failures = 0
        self.state = "OK"
        self.last_success_at = snapshot.get("generated_at") \
            or status.get("last_success_at")
        self._render_all()

    def on_http_error(self, _code=None) -> None:
        self._register_failure("HTTP_ERROR")

    def on_json_error(self) -> None:
        self._register_failure("JSON_ERROR")

    def on_no_snapshot(self) -> None:
        self._register_failure("NO_SNAPSHOT")

    def _register_failure(self, state: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.state = "STALE"
            self.stop()                 # cleanup: no unbounded retrying
        else:
            self.state = state
        # NOTE: on failure the LAST LIVE data (if any) stays rendered and
        # is marked stale via the banner. The engine never restores baked
        # or fabricated content — there is none to restore.

    def manual_refresh(self) -> bool:
        """User-triggered refresh: resets the failure budget and restarts
        the auto-refresh timer if staleness had stopped it."""
        self.consecutive_failures = 0
        if self.state == "STALE":
            self.state = "LOADING"
        if self.timer_handle is None:
            self.timer_handle = object()
            self.timer_create_count += 1
            return True
        return False

    # ── page rendering (shared store, no drift) ───────────────────────────
    def register_page(self, page: str, renderer) -> None:
        if page not in PAGES:
            raise RefreshContractError(
                "CONFIG_INVALID: unknown page %r (allowed: %s)"
                % (page, ",".join(PAGES)))
        if page in self._renderers:
            raise RefreshContractError(
                "CONFIG_INVALID: page %r already registered" % page)
        self._renderers[page] = renderer

    def render_page(self, page: str) -> str:
        """Render ONE page from the SHARED snapshot.

        Returns a placeholder ('' + reason) when no live snapshot exists —
        NEVER baked/static/REPLAY/fabricated content.
        """
        if page not in PAGES:
            raise RefreshContractError(
                "CONFIG_INVALID: unknown page %r" % page)
        if self.snapshot is None:
            return ("<div class=\"live-placeholder\">awaiting first live "
                    "snapshot</div>")
        return self._renderers[page](self.snapshot)

    def _render_all(self) -> None:
        for page in PAGES:
            if page in self._renderers:
                self.render_page(page)

    def freshness(self) -> dict:
        """Banner data: what the user sees about data age / poll health."""
        return {
            "state": self.state,
            "data_time": self.last_success_at,
            "poll_state": (self.status or {}).get("poller_state"),
            "poll_count": (self.status or {}).get("poll_count"),
            "consecutive_failures": self.consecutive_failures,
            "interval_ms": self.interval_ms,
            "snapshot_sha256": (self.snapshot or {}).get("bundle_sha256"),
        }


# ── Fail-closed validator for the shipped JS ───────────────────────────────

def verify_js_contract(js_source: str) -> dict:
    """Validate live-refresh.js against this contract. Raises on drift.

    Checks (source-level, all fail-closed):
      1. every fetched URL literal is one of ALLOWED_URLS;
      2. no non-GET fetch options (no ``method:`` with anything but GET,
         and no forbidden verbs anywhere);
      3. exactly ONE setInterval call site, with clearInterval cleanup;
      4. pagehide/beforeunload cleanup handlers present;
      5. the interval clamp uses the same MIN_INTERVAL_MS literal;
      6. all 8 page renderers are registered, each referencing its
         required bundle fields (PAGE_BUNDLE_FIELDS);
      7. no REPLAY-fallback / reload / baked-data-restore markers.
    """
    if not isinstance(js_source, str) or not js_source.strip():
        raise RefreshContractError(
            "CONFIG_INVALID: live-refresh.js is empty")

    # 1. URL allowlist: every string literal starting with /api/ must be
    #    one of the two allowed endpoints (catches any future drift).
    for literal in re.findall(r"['\"](/api/[^'\"]*)['\"]", js_source):
        if literal not in ALLOWED_URLS:
            raise RefreshContractError(
                "CONFIG_INVALID: JS requests non-allowlisted endpoint %r"
                % literal)

    # 2. GET-only: any explicit method option must be GET; forbidden
    #    verbs must not appear at all.
    for m in re.finditer(r"method\s*:\s*['\"](\w+)['\"]", js_source):
        if m.group(1).upper() != "GET":
            raise RefreshContractError(
                "CONFIG_INVALID: JS uses non-GET method %r" % m.group(1))
    for verb in ("'POST'", '"POST"', "'PUT'", '"PUT"', "'PATCH'", '"PATCH"',
                 "'DELETE'", '"DELETE"'):
        if verb in js_source:
            raise RefreshContractError(
                "CONFIG_INVALID: JS contains forbidden verb %s" % verb)

    # 3. Exactly one setInterval, with clearInterval present.
    if js_source.count("setInterval(") != 1:
        raise RefreshContractError(
            "CONFIG_INVALID: JS must call setInterval exactly once "
            "(got %d)" % js_source.count("setInterval("))
    if "clearInterval(" not in js_source:
        raise RefreshContractError(
            "CONFIG_INVALID: JS lacks clearInterval cleanup")

    # 4. Unload cleanup handlers.
    for event in ("pagehide", "beforeunload"):
        if event not in js_source:
            raise RefreshContractError(
                "CONFIG_INVALID: JS lacks %s cleanup handler" % event)

    # 5. Interval minimum mirrored AS AN ASSIGNMENT (a comment mentioning
    #    the number does not satisfy the contract).
    if not re.search(r"MIN_INTERVAL_MS\s*=\s*%d\b" % MIN_INTERVAL_MS,
                     js_source):
        raise RefreshContractError(
            "CONFIG_INVALID: JS does not assign MIN_INTERVAL_MS = %d"
            % MIN_INTERVAL_MS)

    # 6. All 8 pages registered with their bundle fields. Page ids must
    #    appear as QUOTED literals and bundle fields as snapshot property
    #    accesses — bare substring matches are trivially spoofed.
    for page, fields in PAGE_BUNDLE_FIELDS.items():
        if not re.search(r"['\"]%s['\"]" % re.escape(page), js_source):
            raise RefreshContractError(
                "CONFIG_INVALID: JS missing page %r" % page)
        for field in fields:
            # Property access with a word boundary — direct (snapshot.X)
            # or via a local alias of a snapshot sub-object (bs.X). A bare
            # substring (e.g. 'tracex' matching 'trace') cannot satisfy it.
            if not re.search(r"\.%s\b" % re.escape(field), js_source):
                raise RefreshContractError(
                    "CONFIG_INVALID: JS never reads bundle field %r from "
                    "the shared snapshot" % field)

    # 7. No fallback / reload / mode-switch markers. The ban targets
    #    EXECUTABLE literals ('REPLAY' as a string value, storage APIs,
    #    reload) — comments explaining the no-fallback policy stay legal.
    for marker in ("location.reload", "localStorage", "sessionStorage",
                   "demo_mode", "'REPLAY'", '"REPLAY"'):
        if marker in js_source:
            raise RefreshContractError(
                "CONFIG_INVALID: JS contains forbidden marker %r "
                "(no REPLAY/static fallback, no reload, no persistence)"
                % marker)

    return {"ok": True, "pages": list(PAGES),
            "urls": sorted(ALLOWED_URLS),
            "min_interval_ms": MIN_INTERVAL_MS}


__all__ = [
    "ALLOWED_URLS",
    "DEFAULT_INTERVAL_MS",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_INTERVAL_MS",
    "MIN_INTERVAL_MS",
    "PAGES",
    "PAGE_BUNDLE_FIELDS",
    "SNAPSHOT_URL",
    "STATES",
    "STATUS_URL",
    "LiveRefreshController",
    "RefreshContractError",
    "clamp_interval_ms",
    "verify_js_contract",
]
