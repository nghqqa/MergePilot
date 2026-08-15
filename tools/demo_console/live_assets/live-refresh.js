/* ISOLATED_LIVE dynamic refresh engine (Productization Phase 1-E).
 *
 * Mirrors the Python contract in tools/demo_console/live_refresh.py
 * (serve.py fail-closed-verifies this file against that contract at
 * startup — see verify_js_contract).
 *
 * Hard invariants:
 *   - Only GET /api/live/status and GET /api/live/snapshot are requested.
 *   - Interval is configurable (URL ?interval_ms= or default) but clamped
 *     to >= 2000 ms; auto-refresh stops after 10 consecutive failures.
 *   - Refresh failures NEVER revert to REPLAY/static/baked/fabricated
 *     data: on engine start the baked REPLAY payload is immediately
 *     replaced by placeholders; after a success the last LIVE data stays
 *     and the banner marks staleness.
 *   - All 8 pages render from ONE shared snapshot store — no per-page
 *     fetching, no cross-page drift.
 *   - In REPLAY mode /api/live/status answers 404 and the engine never
 *     starts (the static page remains exactly as served).
 */
(function () {
  'use strict';

  var STATUS_URL = '/api/live/status';
  var SNAPSHOT_URL = '/api/live/snapshot';
  var DEFAULT_INTERVAL_MS = 5000;
  var MIN_INTERVAL_MS = 2000;
  var MAX_INTERVAL_MS = 600000;
  var MAX_CONSECUTIVE_FAILURES = 10;
  var PAGES = ['overview', 'timeline', 'findings', 'rag',
               'trace', 'safety', 'evidence', 'benchmark'];

  var state = 'INIT';
  var timer = null;               /* the ONE setInterval handle */
  var consecutiveFailures = 0;
  var snapshot = null;            /* the ONE shared store */
  var statusInfo = null;
  var lastDataTime = null;

  function clampIntervalMs(value) {
    var v = parseInt(value, 10);
    if (!isFinite(v) || v < MIN_INTERVAL_MS) { v = MIN_INTERVAL_MS; }
    if (v > MAX_INTERVAL_MS) { v = MAX_INTERVAL_MS; }
    return v;
  }

  function configuredIntervalMs() {
    var q = new URLSearchParams(window.location.search);
    return clampIntervalMs(q.get('interval_ms') || DEFAULT_INTERVAL_MS);
  }

  function esc(text) {
    return String(text === null || text === undefined ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ── per-page renderers: all read the SAME `snapshot` store ─────────── */

  function renderOverview() {
    var run = snapshot.run || {}, pr = snapshot.pr || {};
    var stages = snapshot.workflow_stages || [];
    var agents = snapshot.agents || [];
    var fixes = snapshot.fixes || [];
    var vr = snapshot.verifier_result || {};
    var cards = agents.map(function (a) {
      return '<div class="skill-card"><strong>' + esc(a.skill) + '</strong><br>' +
        '<span class="skill-role">' + esc(a.role) + '</span><br>' +
        '<span class="badge">' + esc(a.status) + '</span></div>';
    }).join('');
    return '<h2>Overview</h2>' +
      '<p><strong>run_id:</strong> <code>' + esc(run.run_id) + '</code> · ' +
      '<strong>PR #' + esc(pr.number) + '</strong> ' + esc(pr.title) + '</p>' +
      '<p><strong>final_status:</strong> ' + esc(snapshot.final_status) +
      ' · <strong>stages:</strong> ' + stages.length +
      ' · <strong>fixes:</strong> ' + fixes.length + '</p>' +
      '<p><strong>verifier:</strong> ' + esc(vr.status || 'N/A') + '</p>' +
      '<div class="skill-grid">' + cards + '</div>';
  }

  function renderTimeline() {
    var rows = (snapshot.workflow_stages || []).map(function (s) {
      return '<tr><td>' + esc(s.stage) + '</td><td>' + esc(s.agent_role) +
        '</td><td>' + esc(s.status) + '</td></tr>';
    }).join('');
    return '<h2>Timeline</h2><table class="data-table">' +
      '<tr><th>Stage</th><th>Role</th><th>Status</th></tr>' + rows + '</table>';
  }

  function renderFindings() {
    var rows = (snapshot.findings || []).map(function (f) {
      return '<tr><td>' + esc(f.finding_id) + '</td><td>' + esc(f.category) +
        '</td><td>' + esc(f.severity) + '</td><td><code>' + esc(f.file) +
        '</code></td><td>' + esc(f.message) + '</td></tr>';
    }).join('');
    return '<h2>Findings</h2><table class="data-table">' +
      '<tr><th>ID</th><th>Category</th><th>Severity</th><th>File</th>' +
      '<th>Message</th></tr>' + rows + '</table>';
  }

  function renderRag() {
    var rows = (snapshot.rag_advisories || []).map(function (r) {
      return '<tr><td>' + esc(r.agent_role) + '</td><td>' + esc(r.status) +
        '</td><td>' + esc(r.hit_count) + '</td><td>' + esc(r.adopted) +
        '</td><td>' + esc(r.untrusted) + '</td></tr>';
    }).join('');
    return '<h2>RAG Advisory</h2>' +
      '<div class="boundary-banner">RAG output is ADVISORY only.</div>' +
      '<table class="data-table"><tr><th>Role</th><th>Status</th>' +
      '<th>hit_count</th><th>adopted</th><th>untrusted</th></tr>' +
      rows + '</table>';
  }

  function renderTrace() {
    var spans = snapshot.spans || [];
    var items = spans.map(function (s) {
      return '<li><code>' + esc(s.span_id) + '</code> ' + esc(s.name) +
        ' — ' + esc(s.status) + ' (' + esc(s.start_time) + ' → ' +
        esc(s.end_time) + ')</li>';
    }).join('');
    return '<h2>Trace Tree</h2><ul>' + items + '</ul>';
  }

  function renderSafety() {
    var residue = snapshot.residue || {};
    return '<h2>Policy &amp; Safety</h2><h3>Residue</h3><ul>' +
      '<li>Containers: ' + esc(residue.containers) + '</li>' +
      '<li>Networks: ' + esc(residue.networks) + '</li>' +
      '<li>Temp dirs: ' + esc(residue.temp_dirs) + '</li></ul>' +
      '<h3>Secret Scan</h3><p>secret_leaks: <strong>' +
      esc(snapshot.secret_leaks) + '</strong></p>' +
      '<h3>Rollback Events</h3><p>' +
      (snapshot.rollback_events || []).length + ' rollback event(s).</p>';
  }

  function renderEvidence() {
    var rows = (snapshot.evidence_files || []).map(function (f) {
      return '<tr><td>' + esc(f.path) + '</td><td><code>' + esc(f.sha256) +
        '</code></td><td>' + esc(f.description) + '</td></tr>';
    }).join('');
    return '<h2>Evidence &amp; Provenance</h2>' +
      '<p><strong>Bundle SHA-256:</strong> <code>' +
      esc(snapshot.bundle_sha256) + '</code></p>' +
      '<p><strong>Source commit:</strong> <code>' +
      esc(snapshot.source_commit) + '</code></p>' +
      '<p><strong>Verification commit:</strong> <code>' +
      esc(snapshot.verification_commit) + '</code></p>' +
      '<table class="data-table"><tr><th>Path</th><th>SHA-256</th>' +
      '<th>Description</th></tr>' + rows + '</table>';
  }

  function renderBenchmark() {
    var bs = snapshot.benchmark_summary || {};
    return '<h2>Benchmark Summary</h2>' +
      '<div class="boundary-banner warning"><strong>Benchmark boundary:' +
      '</strong> offline adapter only; does NOT claim Reviewer/Fixer ' +
      'accuracy improvement. <code>' +
      esc(bs.workflow_utility_status) + '</code> · runtime_consumes_rag' +
      '_context=<code>' + esc(bs.runtime_consumes_rag_context) +
      '</code></div>' +
      '<h3>' + esc(bs.benchmark_phase || '') + '</h3><ul>' +
      '<li>Dataset: ' + esc(bs.dataset_version) + '</li>' +
      '<li>Unique cases: ' + esc(bs.unique_case_count) + '</li>' +
      '<li>quality_gate_pass: <strong>' + esc(bs.quality_gate_pass) +
      '</strong></li><li>confirmatory_all_ok: <strong>' +
      esc(bs.confirmatory_all_ok) + '</strong></li></ul>';
  }

  var RENDERERS = {
    overview: renderOverview, timeline: renderTimeline,
    findings: renderFindings, rag: renderRag, trace: renderTrace,
    safety: renderSafety, evidence: renderEvidence,
    benchmark: renderBenchmark
  };

  function placeholder(page) {
    return '<div class="live-placeholder" data-page="' + esc(page) +
      '">awaiting first live snapshot…</div>';
  }

  function renderAll() {
    PAGES.forEach(function (page) {
      var el = document.getElementById(page);
      if (!el) { return; }
      /* No baked/static data is ever re-displayed: with no snapshot the
       * page shows an explicit placeholder. */
      el.innerHTML = (snapshot === null)
        ? placeholder(page)
        : RENDERERS[page]();
    });
    renderBanner();
  }

  /* ── freshness banner (data time + poll state) ──────────────────────── */

  function renderBanner() {
    var el = document.getElementById('live-banner');
    if (!el) { return; }
    var freshness = (state === 'OK' && snapshot)
      ? (snapshot.generated_at || lastDataTime)
      : lastDataTime;
    var poll = statusInfo
      ? (statusInfo.poller_state + ' · polls=' + statusInfo.poll_count)
      : 'no poll data';
    el.setAttribute('data-state', state);
    el.textContent = 'LIVE · state=' + state +
      ' · data_time=' + (freshness || 'n/a') +
      ' · poll: ' + poll +
      ' · failures=' + consecutiveFailures;
  }

  /* ── fetch cycle (GET only, allowlisted URLs only) ──────────────────── */

  function getJson(url) {
    return fetch(url, {method: 'GET', cache: 'no-store'})
      .then(function (r) {
        if (!r.ok) { throw {kind: 'http', code: r.status}; }
        return r.json();
      })
      .then(function (j) {
        if (typeof j !== 'object' || j === null) {
          throw {kind: 'json'};
        }
        return j;
      });
  }

  function registerFailure(kind) {
    consecutiveFailures += 1;
    if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
      state = 'STALE';
      stopTimer();               /* cleanup: no unbounded retrying */
    } else {
      state = (kind === 'http') ? 'HTTP_ERROR'
        : (kind === 'json') ? 'JSON_ERROR' : 'NO_SNAPSHOT';
    }
    /* The last LIVE data (if any) stays rendered and the banner marks it
     * stale — nothing is ever reverted to static/baked content. */
    renderBanner();
  }

  function refreshOnce() {
    return getJson(STATUS_URL)
      .then(function (st) {
        statusInfo = st;
        return getJson(SNAPSHOT_URL);
      })
      .then(function (snap) {
        if (!snap || !snap.bundle_sha256) {
          registerFailure('no_snapshot');
          return;
        }
        snapshot = snap;          /* the single shared store */
        consecutiveFailures = 0;
        state = 'OK';
        lastDataTime = snap.generated_at || st0();
        renderAll();
      })
      .catch(function (err) {
        if (err && err.kind === 'http') { registerFailure('http'); }
        else if (err && err.kind === 'json') { registerFailure('json'); }
        else { registerFailure('no_snapshot'); }
      });
  }

  function st0() {
    return (statusInfo && statusInfo.last_success_at) || null;
  }

  /* ── timer lifecycle: created ONCE, cleaned up on stop/unload ───────── */

  function startTimer() {
    if (timer !== null) { return; }
    timer = setInterval(refreshOnce, configuredIntervalMs());
    refreshOnce();               /* immediate first fetch */
  }

  function stopTimer() {
    if (timer === null) { return; }
    clearInterval(timer);
    timer = null;
  }

  function manualRefresh() {
    consecutiveFailures = 0;
    if (state === 'STALE') { state = 'LOADING'; }
    if (timer === null) {
      startTimer();
    } else {
      refreshOnce();
    }
    renderBanner();
  }

  /* ── boot: REPLAY (404 on /api/live/*) never starts the engine ──────── */

  function boot() {
    var btn = document.getElementById('live-refresh-btn');
    if (btn) { btn.addEventListener('click', manualRefresh); }
    window.addEventListener('pagehide', stopTimer);
    window.addEventListener('beforeunload', stopTimer);
    getJson(STATUS_URL)
      .then(function () {
        state = 'LOADING';
        renderAll();             /* placeholders replace baked content */
        startTimer();
      })
      .catch(function () {
        /* REPLAY mode (404) or unreachable: the engine stays off; the
         * statically served page is untouched. No data is fabricated. */
        state = 'INIT';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
