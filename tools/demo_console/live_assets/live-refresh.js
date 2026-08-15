/* ISOLATED_LIVE dynamic refresh engine (Phase 1-E; UI refresh Phase 1-F).
 *
 * Mirrors the Python contract in tools/demo_console/live_refresh.py
 * (serve.py fail-closed-verifies this file against that contract at
 * startup — see verify_js_contract).
 *
 * Hard invariants (unchanged by the Phase 1-F UI work):
 *   - Only GET /api/live/status and GET /api/live/snapshot are requested.
 *   - Interval is configurable (URL ?interval_ms= or default) but clamped
 *     to >= 2000 ms; auto-refresh stops after 10 consecutive failures.
 *   - Refresh failures NEVER revert to REPLAY/static/baked/fabricated
 *     data: on engine start the baked payload is immediately replaced by
 *     placeholders; after a success the last LIVE data stays and the
 *     banner marks staleness.
 *   - All 8 pages render from ONE shared snapshot store — no per-page
 *     fetching, no cross-page drift.
 *   - In REPLAY mode /api/live/status answers 404 and the engine never
 *     starts (the static page remains exactly as served).
 *   - Every dynamic value is HTML-escaped (esc) before entering the DOM.
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

  /* ── status semantics (shared visual language) ─────────────────────── */

  function statusClass(value) {
    var v = String(value === null || value === undefined ? '' : value)
      .toUpperCase();
    if (v === 'SUCCEEDED' || v === 'SUCCESS' || v === 'PASS' ||
        v === 'PASSED' || v === 'OK' || v === 'COMPLETED' || v === 'TRUE') {
      return 'st-ok';
    }
    if (v === 'FAILED' || v === 'FAIL' || v === 'ERROR' || v === 'FALSE') {
      return 'st-err';
    }
    if (v === 'RUNNING' || v === 'RETRY' || v === 'TIMEOUT') {
      return 'st-warn';
    }
    return 'st-dim';                    /* pending / skipped / unknown */
  }

  function stChip(value) {
    return '<span class="st ' + statusClass(value) + '">' +
      esc(value === null || value === undefined || value === ''
        ? 'n/a' : value) + '</span>';
  }

  function kvCell(k, v, mono) {
    return '<div class="cell"><div class="k">' + esc(k) + '</div>' +
      '<div class="v' + (mono ? ' mono' : '') + '">' + v + '</div></div>';
  }

  function metricCell(k, v) {
    return '<div class="metric"><div class="m-k">' + esc(k) + '</div>' +
      '<div class="m-v">' + esc(v) + '</div></div>';
  }

  /* ── per-page renderers: all read the SAME `snapshot` store ─────────── */

  function renderOverview() {
    var run = snapshot.run || {}, pr = snapshot.pr || {};
    var stages = snapshot.workflow_stages || [];
    var agents = snapshot.agents || [];
    var fixes = snapshot.fixes || [];
    var vr = snapshot.verifier_result || {};
    var repo = snapshot.repo || {};
    var cards = agents.map(function (a) {
      return '<div class="skill-card">' +
        '<span class="skill-name">' + esc(a.skill) + '</span>' +
        '<span class="skill-role">role: ' + esc(a.role) + '</span>' +
        stChip(a.status) + '</div>';
    }).join('');
    return '<div class="panel"><h2>Overview</h2><div class="kv">' +
      kvCell('run_id', '<code>' + esc(run.run_id) + '</code>', false) +
      kvCell('PR', esc('#' + pr.number) + ' ' + esc(pr.title), false) +
      kvCell('repo', esc(repo.full_name || repo.name || 'n/a'), false) +
      kvCell('final_status', stChip(snapshot.final_status), false) +
      kvCell('verifier', stChip(vr.status || 'n/a'), false) +
      kvCell('trace_id', '<code>' + esc(run.trace_id) + '</code>', false) +
      '</div></div>' +
      '<div class="panel"><h3>Run metrics</h3><div class="metrics">' +
      metricCell('stages', stages.length) +
      metricCell('findings', (snapshot.findings || []).length) +
      metricCell('fixes', fixes.length) +
      metricCell('skills', agents.length) +
      '</div></div>' +
      '<div class="panel"><h3>Skill execution</h3>' +
      (cards ? '<div class="skill-grid">' + cards + '</div>'
             : '<p class="empty-note">no agents recorded</p>') +
      '</div>' +
      '<div class="boundary-banner plain"><strong>Read-only viewer:</strong> ' +
      'this console performs no writes, no agent control and no GitHub ' +
      'operations. Data is the latest validated ISOLATED_LIVE snapshot.</div>';
  }

  function renderTimeline() {
    var stages = snapshot.workflow_stages || [];
    if (!stages.length) {
      return '<div class="panel"><h2>Timeline</h2>' +
        '<p class="empty-note">no stages recorded</p></div>';
    }
    var items = stages.map(function (s) {
      var cls = statusClass(s.status);
      var tl = cls === 'st-ok' ? 'tl-ok'
        : cls === 'st-err' ? 'tl-err'
        : cls === 'st-warn' ? 'tl-warn' : '';
      var when = [s.started_at, s.finished_at].filter(Boolean).join(' → ');
      return '<li class="' + tl + '"><div class="tl-head">' +
        '<span class="tl-stage">' + esc(s.stage) + '</span>' +
        stChip(s.status) +
        '<span class="tl-meta">' + esc(s.agent_role || '') +
        (when ? ' · ' + esc(when) : '') + '</span></div></li>';
    }).join('');
    return '<div class="panel"><h2>Timeline</h2>' +
      '<ul class="timeline">' + items + '</ul></div>';
  }

  function renderFindings() {
    var findings = snapshot.findings || [];
    if (!findings.length) {
      return '<div class="panel"><h2>Findings</h2>' +
        '<p class="empty-note">no findings recorded</p></div>';
    }
    var rows = findings.map(function (f) {
      var sev = String(f.severity || '').toUpperCase();
      var rowCls = (sev === 'HIGH' || sev === 'CRITICAL')
        ? ' class="sev-high"'
        : (sev === 'MEDIUM' ? ' class="sev-medium"' : '');
      return '<tr' + rowCls + '><td><code>' + esc(f.finding_id) +
        '</code></td><td>' + esc(f.category) + '</td><td>' +
        stChip(f.severity || 'n/a') + '</td><td class="code">' +
        esc(f.file) + '</td><td>' + esc(f.message) + '</td></tr>';
    }).join('');
    return '<div class="panel"><h2>Findings</h2>' +
      '<table class="data-table"><tr><th>ID</th><th>Category</th>' +
      '<th>Severity</th><th>File</th><th>Message</th></tr>' + rows +
      '</table></div>';
  }

  function renderRag() {
    var advisories = snapshot.rag_advisories || [];
    var rows = advisories.map(function (r) {
      return '<tr><td>' + esc(r.agent_role) + '</td><td>' +
        stChip(r.status || 'n/a') + '</td><td>' + esc(r.hit_count) +
        '</td><td>' + stChip(r.adopted === false ? 'not adopted' : r.adopted) +
        '</td><td>' + stChip(r.untrusted ? 'untrusted' : 'trusted-input') +
        '</td></tr>';
    }).join('');
    return '<div class="panel"><h2>RAG Advisory</h2>' +
      '<div class="boundary-banner"><strong>Advisory only:</strong> RAG ' +
      'output is ADVISORY, untrusted context — never a deterministic ' +
      'conclusion and never auto-applied. adopted=false means the ' +
      'recommendation was NOT accepted.</div>' +
      (rows ? '<table class="data-table"><tr><th>Role</th><th>Status</th>' +
        '<th>hit_count</th><th>Adoption</th><th>Trust</th></tr>' + rows +
        '</table>' : '<p class="empty-note">no rag advisories recorded</p>') +
      '</div>';
  }

  function renderTrace() {
    var spans = snapshot.spans || [];
    if (!spans.length) {
      return '<div class="panel"><h2>Trace Tree</h2>' +
        '<p class="empty-note">no spans recorded</p></div>';
    }
    var items = spans.map(function (s) {
      return '<li><div class="span-line">' +
        '<span class="span-name">' + esc(s.name) + '</span>' +
        stChip(s.status || 'n/a') +
        '<span class="span-time">' + esc(s.span_id) + ' · ' +
        esc(s.start_time) + ' → ' + esc(s.end_time) + '</span></div></li>';
    }).join('');
    return '<div class="panel"><h2>Trace Tree</h2>' +
      '<ul class="tree">' + items + '</ul></div>';
  }

  function renderSafety() {
    var residue = snapshot.residue || {};
    var rollbacks = snapshot.rollback_events || [];
    var leaks = snapshot.secret_leaks;
    var leakOk = leaks === 0 || leaks === '0';
    return '<div class="panel"><h2>Policy &amp; Safety</h2>' +
      '<div class="kv">' +
      kvCell('residue · containers', esc(residue.containers), true) +
      kvCell('residue · networks', esc(residue.networks), true) +
      kvCell('residue · temp dirs', esc(residue.temp_dirs), true) +
      kvCell('secret scan', stChip(leakOk ? 'CLEAN' : 'LEAKS'), false) +
      kvCell('rollback events', esc(rollbacks.length), true) +
      '</div></div>' +
      '<div class="panel"><h3>Rollback events</h3>' +
      (rollbacks.length
        ? '<ul class="tree">' + rollbacks.map(function (e) {
            return '<li><div class="span-line"><span class="span-name">' +
              esc(e.event || e.stage || 'rollback') + '</span>' +
              stChip(e.status || 'n/a') + '</div></li>';
          }).join('') + '</ul>'
        : '<p class="empty-note">0 rollback event(s)</p>') +
      '</div>';
  }

  function renderEvidence() {
    var files = snapshot.evidence_files || [];
    var rows = files.map(function (f) {
      return '<tr><td class="code">' + esc(f.path) + '</td>' +
        '<td class="code">' + esc(f.sha256) + '</td><td>' +
        esc(f.description) + '</td></tr>';
    }).join('');
    return '<div class="panel"><h2>Evidence &amp; Provenance</h2>' +
      '<div class="kv">' +
      kvCell('bundle sha-256', '<code>' + esc(snapshot.bundle_sha256) +
        '</code>', true) +
      kvCell('source commit', '<code>' + esc(snapshot.source_commit) +
        '</code>', true) +
      kvCell('verification commit', '<code>' +
        esc(snapshot.verification_commit) + '</code>', true) +
      kvCell('generated_at', esc(snapshot.generated_at), true) +
      '</div></div>' +
      '<div class="panel"><h3>Evidence files</h3>' +
      (rows ? '<table class="data-table"><tr><th>Path</th><th>SHA-256</th>' +
        '<th>Description</th></tr>' + rows + '</table>'
        : '<p class="empty-note">no evidence files recorded</p>') +
      '</div>' +
      '<div class="boundary-banner plain">' +
      '<strong>Provenance only:</strong> bundle integrity is recomputed ' +
      'server-side; listing evidence here does NOT imply production ' +
      'verification (production_verified=false).</div>';
  }

  function renderBenchmark() {
    var bs = snapshot.benchmark_summary || {};
    return '<div class="panel"><h2>Benchmark Summary</h2>' +
      '<div class="metrics">' +
      metricCell('dataset', bs.dataset_version) +
      metricCell('unique cases', bs.unique_case_count) +
      metricCell('quality gate', bs.quality_gate_pass) +
      metricCell('confirmatory', bs.confirmatory_all_ok) +
      '</div></div>' +
      '<div class="boundary-banner warning">' +
      '<strong>Benchmark boundary:</strong> offline adapter only; does NOT ' +
      'claim Reviewer/Fixer accuracy improvement. ' +
      '<code>runtime_consumes_rag_context=' +
      esc(bs.runtime_consumes_rag_context) + '</code> · <code>' +
      esc(bs.workflow_utility_status) + '</code> · phase=' +
      esc(bs.benchmark_phase) + '</div>';
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

  /* ── freshness banner (state chip + data time + poll health) ────────── */

  function renderBanner() {
    var el = document.getElementById('live-banner');
    var btn = document.getElementById('live-refresh-btn');
    if (!el) { return; }
    var freshness = (state === 'OK' && snapshot)
      ? (snapshot.generated_at || lastDataTime)
      : lastDataTime;
    var poll = statusInfo
      ? (statusInfo.poller_state + ' · polls=' + statusInfo.poll_count)
      : 'no poll data';
    el.setAttribute('data-state', state);
    el.innerHTML = '<span class="chip">' + esc(state) + '</span>' +
      '<span>data_time=' + esc(freshness || 'n/a') + '</span>' +
      '<span>poll: ' + esc(poll) + '</span>' +
      '<span>failures=' + esc(consecutiveFailures) + '</span>';
    if (btn) {
      btn.disabled = (state === 'LOADING');
      btn.textContent = (state === 'STALE')
        ? 'Resume auto-refresh' : 'Refresh now';
    }
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

  function statusFallbackTime() {
    return (statusInfo && statusInfo.last_success_at) || null;
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
        lastDataTime = snap.generated_at || statusFallbackTime();
        renderAll();
      })
      .catch(function (err) {
        if (err && err.kind === 'http') { registerFailure('http'); }
        else if (err && err.kind === 'json') { registerFailure('json'); }
        else { registerFailure('no_snapshot'); }
      });
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
