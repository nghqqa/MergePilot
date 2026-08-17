/* ISOLATED_LIVE dynamic refresh engine (Phase 1-E; UI Phase 1-F; showcase PR-V1).
 *
 * Mirrors the Python contract in tools/demo_console/live_refresh.py
 * (serve.py fail-closed-verifies this file against that contract at
 * startup — see verify_js_contract).
 *
 * Hard invariants (unchanged by all UI work):
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

  /* ── Per-page metadata (eyebrow, title, description) ────────────────── */
  var PAGE_META = {
    overview: {
      eyebrow: 'Run Summary',
      title: 'Overview',
      desc: 'Current run identity, PR context, final status and agent execution summary.'
    },
    timeline: {
      eyebrow: 'Workflow Progress',
      title: 'Timeline',
      desc: 'Ordered workflow stages with status, ownership and timing for this run.'
    },
    findings: {
      eyebrow: 'Code Review',
      title: 'Findings',
      desc: 'Issues identified by the review pipeline, sorted by severity with affected files.'
    },
    rag: {
      eyebrow: 'Retrieval Advisory',
      title: 'RAG Advisory',
      desc: 'Retrieval-augmented suggestions with adoption and trust boundary annotations.'
    },
    trace: {
      eyebrow: 'Observability',
      title: 'Trace Tree',
      desc: 'Distributed trace spans for gateway calls, skill executions and controller actions.'
    },
    safety: {
      eyebrow: 'Guardrails',
      title: 'Policy & Safety',
      desc: 'Environment residue, secret scan results and rollback event history.'
    },
    evidence: {
      eyebrow: 'Provenance',
      title: 'Evidence & Provenance',
      desc: 'Bundle integrity, source and verification commits, and evidence file listings.'
    },
    benchmark: {
      eyebrow: 'Capability Matrix',
      title: 'Benchmark Summary',
      desc: 'Evaluation dataset coverage, quality gate results and boundary annotations.'
    }
  };

  var state = 'INIT';
  var timer = null;               /* the ONE setInterval handle */
  var consecutiveFailures = 0;
  var snapshot = null;            /* the ONE shared store */
  var statusInfo = null;
  var lastDataTime = null;

  /* ── Showcase case labels (PR-V2; PRESENTATION METADATA ONLY) ────────
   * Maps a seeded showcase run_id to its display label. This registry
   * carries NO case facts: every run_id / PR / SHA / stage / decision /
   * status shown on the pages comes from the live snapshot API. The
   * case_id/name here must match tools/demo_console/showcase_cases.py
   * (enforced by the test suite) so the badge never mislabels a run. */
  var SHOWCASE_CASES = {
    'run-showcase-a': {
      caseId: 'case-showcase-protected-merge-success',
      name: 'Protected Merge Success'
    },
    'run-showcase-b': {
      caseId: 'case-showcase-failclosed-policy-rejection',
      name: 'Fail-Closed Policy Rejection'
    },
    'run-showcase-c': {
      caseId: 'case-showcase-revision-drift-recovery',
      name: 'Revision Drift Recovery'
    }
  };

  var SEED_DISCLOSURE = 'Deterministic showcase seed — not external ' +
    'customer data, not production evidence';

  function showcaseBadge() {
    var meta = SHOWCASE_CASES[(snapshot.run || {}).run_id];
    if (!meta) { return ''; }
    return '<div class="seed-badge" data-seed="deterministic-showcase">' +
      '<span class="seed-tag">Deterministic showcase seed</span>' +
      '<span class="seed-case"><code>' + esc(meta.caseId) + '</code> · ' +
      esc(meta.name) + '</span>' +
      '<span class="seed-note">not external customer data · not ' +
      'production evidence</span></div>';
  }

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

  /* ── Status semantics (shared visual language) ───────────────────────── */

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

  /* ── Presentation helpers ────────────────────────────────────────────── */

  function pageHeader(page) {
    var meta = PAGE_META[page] || {};
    return '<div class="page-head">' +
      '<div class="page-eyebrow">' + esc(meta.eyebrow || '') + '</div>' +
      '<h2 class="page-title" id="' + esc(page) + '-title">' +
      esc(meta.title || page) + '</h2>' +
      '<p class="page-desc">' + esc(meta.desc || '') + '</p>' +
      '</div>';
  }

  function kvCell(k, v, mono) {
    return '<div class="cell"><div class="k">' + esc(k) + '</div>' +
      '<div class="v' + (mono ? ' mono' : '') + '">' + v + '</div></div>';
  }

  function metricCell(k, v) {
    return '<div class="metric"><div class="m-k">' + esc(k) + '</div>' +
      '<div class="m-v">' + esc(v) + '</div></div>';
  }

  function evidenceRow(label, value) {
    return '<div class="evidence-row">' +
      '<span class="evidence-label">' + esc(label) + '</span>' +
      '<span class="evidence-value">' + value + '</span></div>';
  }

  function tableWrap(headers, rows) {
    var thead = '<tr>' + headers.map(function (h) {
      return '<th scope="col">' + esc(h) + '</th>';
    }).join('') + '</tr>';
    return '<div class="table-wrap"><table class="data-table">' +
      '<thead>' + thead + '</thead><tbody>' + rows + '</tbody></table></div>';
  }

  function panel(title, body) {
    return '<div class="panel">' +
      (title ? '<div class="panel-title">' + esc(title) + '</div>' : '') +
      body + '</div>';
  }

  /* ── Case-fact helpers (PR-V2; all data from the shared snapshot) ──── */

  function gatewayCalls() {
    return snapshot.gateway_calls || [];
  }

  function orderedStages(stages) {
    /* Sort by started_at when EVERY stage carries one (audit-DB runs do);
     * otherwise keep the bundle order (never a partial guess). */
    var withTime = stages.every(function (s) {
      return Boolean(s.started_at);
    });
    if (!withTime || stages.length < 2) { return stages; }
    return stages.slice().sort(function (a, b) {
      return String(a.started_at) < String(b.started_at) ? -1
        : String(a.started_at) > String(b.started_at) ? 1 : 0;
    });
  }

  /* ── Per-page renderers: all read the SAME `snapshot` store ─────────── */

  function renderOverview() {
    var run = snapshot.run || {}, pr = snapshot.pr || {};
    var stages = snapshot.workflow_stages || [];
    var agents = snapshot.agents || [];
    var fixes = snapshot.fixes || [];
    var vr = snapshot.verifier_result || {};
    /* repo is a plain string in both REPLAY and ISOLATED_LIVE bundles;
     * accept an object form defensively but never print [object Object]. */
    var repo = snapshot.repo;
    var repoLabel = typeof repo === 'string' ? repo
      : ((repo && (repo.full_name || repo.name)) || 'n/a');
    var cards = agents.map(function (a) {
      return '<div class="skill-card">' +
        '<span class="skill-name">' + esc(a.skill) + '</span>' +
        '<span class="skill-role">role: ' + esc(a.role) + '</span>' +
        stChip(a.status) + '</div>';
    }).join('');
    return pageHeader('overview') +
      showcaseBadge() +
      panel('Run Identity', '<div class="kv">' +
        kvCell('run_id', '<code>' + esc(run.run_id) + '</code>', false) +
        kvCell('PR', esc('#' + pr.number) + ' ' + esc(pr.title), false) +
        kvCell('repo', esc(repoLabel), false) +
        kvCell('final_status', stChip(snapshot.final_status), false) +
        kvCell('verifier', stChip(vr.status || 'n/a'), false) +
        kvCell('trace_id', '<code>' + esc(run.trace_id) + '</code>', false) +
        kvCell('head SHA', '<code>' + esc(pr.head_sha || 'n/a') + '</code>',
          false) +
        '</div>') +
      (snapshot.run_failure_reason
        ? panel('Failure Reason', '<p class="case-fact">' +
          esc(snapshot.run_failure_reason) + '</p>')
        : '') +
      panel('Run Metrics', '<div class="metrics">' +
        metricCell('stages', stages.length) +
        metricCell('findings', (snapshot.findings || []).length) +
        metricCell('fixes', fixes.length) +
        metricCell('skills', agents.length) +
        '</div>') +
      panel('Skill Execution',
        (cards ? '<div class="skill-grid">' + cards + '</div>'
               : '<p class="empty-note">no agents recorded</p>')) +
      '<div class="boundary-banner plain"><strong>Read-only viewer:</strong> ' +
      'this console performs no writes, no agent control and no GitHub ' +
      'operations. Data is the latest validated ISOLATED_LIVE snapshot.</div>';
  }

  function renderTimeline() {
    var stages = orderedStages(snapshot.workflow_stages || []);
    if (!stages.length) {
      return pageHeader('timeline') +
        panel(null, '<p class="empty-note">no stages recorded</p>');
    }
    var items = stages.map(function (s) {
      var cls = statusClass(s.status);
      var tl = cls === 'st-ok' ? 'tl-ok'
        : cls === 'st-err' ? 'tl-err'
        : cls === 'st-warn' ? 'tl-warn' : '';
      var when = [s.started_at, s.finished_at || s.completed_at]
        .filter(Boolean).join(' → ');
      return '<li class="' + tl + '"><div class="tl-head">' +
        '<span class="tl-stage">' + esc(s.stage) + '</span>' +
        stChip(s.status) +
        (s.verdict ? stChip(s.verdict) : '') +
        '<span class="tl-meta">' + esc(s.agent_role || '') +
        (when ? ' · ' + esc(when) : '') + '</span></div></li>';
    }).join('');
    return pageHeader('timeline') +
      panel(stages.length + ' stage' + (stages.length !== 1 ? 's' : ''),
        '<ul class="timeline">' + items + '</ul>');
  }

  function renderFindings() {
    var findings = snapshot.findings || [];
    var html = pageHeader('findings');
    if (findings.length) {
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
      html += panel(findings.length + ' finding' +
        (findings.length !== 1 ? 's' : ''),
        tableWrap(['ID', 'Category', 'Severity', 'File', 'Message'], rows));
    } else {
      html += panel(null,
        '<p class="empty-note">no inline findings recorded for this run ' +
        '(audit DB stores gateway decisions and rollback facts)</p>');
    }
    /* Case facts (PR-V2): policy rejections and revision-drift facts from
     * the immutable gateway/rollback audit trail. Only real snapshot
     * fields; empty when the run has none. */
    var blocked = gatewayCalls().filter(function (c) {
      return c.decision === 'DENY' || c.decision === 'ERROR';
    });
    if (blocked.length) {
      var brow = blocked.map(function (c) {
        return '<tr><td><code>' + esc(c.request_id) + '</code></td><td>' +
          stChip(c.decision) + '</td><td class="code">' + esc(c.tool) +
          '</td><td><code>' + esc(c.reason_code || 'n/a') + '</code>' +
          '</td><td>' + esc(c.error || 'blocked before execution') +
          '</td><td class="code">' + esc(c.git_sha || 'n/a') +
          '</td></tr>';
      }).join('');
      html += panel('Policy Rejection Facts (gateway audit)',
        tableWrap(['Request', 'Decision', 'Tool', 'Reason', 'Detail',
          'Observed SHA'], brow));
    }
    var drift = snapshot.rollback_events || [];
    if (drift.length) {
      var drow = drift.map(function (e) {
        return '<tr><td><code>' + esc(e.rollback_id) + '</code></td><td>' +
          stChip(e.status) + '</td><td class="code">' +
          esc(e.reverted_merge_sha || 'n/a') + '</td><td class="code">' +
          esc(e.revert_result_sha || 'n/a') + '</td><td>' +
          stChip(e.reverify_verdict ? 'reverify ' + e.reverify_verdict
            : 'n/a') + '</td><td>' +
          esc(e.fail_reason || 'n/a') + '</td></tr>';
      }).join('');
      html += panel('Drift & Rollback Facts (rollback_runs audit)',
        tableWrap(['Rollback', 'Status', 'Reverted SHA', 'Recovered SHA',
          'Re-verify', 'Reason'], drow));
    }
    if (snapshot.run_failure_reason) {
      html += panel('Run Failure Reason',
        '<p class="case-fact">' + esc(snapshot.run_failure_reason) +
        '</p>');
    }
    return html;
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
    return pageHeader('rag') +
      '<div class="boundary-banner"><strong>Advisory only:</strong> RAG ' +
      'output is ADVISORY, untrusted context — never a deterministic ' +
      'conclusion and never auto-applied. adopted=false means the ' +
      'recommendation was NOT accepted.</div>' +
      (rows
        ? panel(null, tableWrap(
            ['Role', 'Status', 'hit_count', 'Adoption', 'Trust'], rows))
        : panel(null, '<p class="empty-note">no rag advisories recorded</p>'));
  }

  function renderTrace() {
    var spans = snapshot.spans || [];
    if (spans.length) {
      var items = spans.map(function (s) {
        return '<li><div class="span-line">' +
          '<span class="span-name">' + esc(s.name) + '</span>' +
          stChip(s.status || 'n/a') +
          '<span class="span-time">' + esc(s.span_id) + ' · ' +
          esc(s.start_time) + ' → ' + esc(s.end_time) + '</span></div></li>';
      }).join('');
      return pageHeader('trace') +
        panel(spans.length + ' span' + (spans.length !== 1 ? 's' : ''),
          '<ul class="tree">' + items + '</ul>');
    }
    /* No OTel spans in the audit DB (honest empty). The decision and
     * execution chain for this run is the immutable gateway audit trail
     * (mcp_calls), rendered here so a case reads end-to-end. */
    var calls = gatewayCalls();
    if (!calls.length) {
      return pageHeader('trace') +
        panel(null, '<p class="empty-note">no spans recorded</p>');
    }
    var chain = calls.map(function (c) {
      return '<li><div class="span-line">' +
        '<span class="span-name">' + esc(c.phase || '-') + ' ' +
        esc(c.tool) + '</span>' +
        stChip(c.decision) +
        '<span class="span-time">' + esc(c.request_id || '') +
        (c.ts ? ' · ' + esc(c.ts) : '') +
        (c.reason_code ? ' · ' + esc(c.reason_code) : '') +
        (c.ticket_id ? ' · ticket ' + esc(c.ticket_id) : '') +
        (c.git_sha ? ' · sha ' + esc(c.git_sha) : '') +
        '</span></div></li>';
    }).join('');
    return pageHeader('trace') +
      panel('Gateway decision chain (mcp_calls audit)',
        '<ul class="tree">' + chain + '</ul>');
  }

  function renderSafety() {
    var residue = snapshot.residue || {};
    var rollbacks = snapshot.rollback_events || [];
    var leaks = snapshot.secret_leaks;
    var leakOk = leaks === 0 || leaks === '0';
    var gw = (residue.gateway_audit_summary || {});
    return pageHeader('safety') +
      panel('Policy Gateway', '<div class="kv">' +
        kvCell('gateway · ALLOW', esc(gw.allow), true) +
        kvCell('gateway · DENY', esc(gw.deny), true) +
        kvCell('gateway · ERROR', esc(gw.error), true) +
        kvCell('gateway · total calls', esc(gw.total), true) +
        '</div>') +
      panel('Environment Health', '<div class="kv">' +
        kvCell('residue · containers', esc(residue.containers), true) +
        kvCell('residue · networks', esc(residue.networks), true) +
        kvCell('residue · temp dirs', esc(residue.temp_dirs), true) +
        kvCell('secret scan', stChip(leakOk ? 'CLEAN' : 'LEAKS'), false) +
        kvCell('rollback events', esc(rollbacks.length), true) +
        '</div>') +
      panel('Rollback Events',
        (rollbacks.length
          ? '<ul class="tree">' + rollbacks.map(function (e) {
              return '<li><div class="span-line"><span class="span-name">' +
                esc(e.rollback_id || e.event || e.stage || 'rollback') +
                '</span>' +
                stChip(e.status || 'n/a') +
                (e.reverify_verdict
                  ? stChip('reverify ' + e.reverify_verdict) : '') +
                '<span class="span-time">' +
                (e.reverted_merge_sha
                  ? 'reverted ' + esc(e.reverted_merge_sha) : '') +
                (e.revert_result_sha
                  ? ' · recovered ' + esc(e.revert_result_sha) : '') +
                (e.fail_reason ? ' · ' + esc(e.fail_reason) : '') +
                '</span></div></li>';
            }).join('') + '</ul>'
          : '<p class="empty-note">0 rollback event(s)</p>'));
  }

  function renderEvidence() {
    var files = snapshot.evidence_files || [];
    var rows = files.map(function (f) {
      return '<tr><td class="code">' + esc(f.path) + '</td>' +
        '<td class="code">' + esc(f.sha256) + '</td><td>' +
        esc(f.description) + '</td></tr>';
    }).join('');
    return pageHeader('evidence') +
      panel('Bundle Identity',
        evidenceRow('bundle sha-256',
          '<code>' + esc(snapshot.bundle_sha256) + '</code>') +
        evidenceRow('source commit',
          '<code>' + esc(snapshot.source_commit) + '</code>') +
        evidenceRow('verification commit',
          '<code>' + esc(snapshot.verification_commit) + '</code>') +
        evidenceRow('generated_at',
          '<code>' + esc(snapshot.generated_at) + '</code>')) +
      panel('Evidence Files',
        (rows
          ? tableWrap(['Path', 'SHA-256', 'Description'], rows)
          : '<p class="empty-note">no evidence files recorded</p>')) +
      evidenceAuditPanel() +
      '<div class="boundary-banner plain">' +
      '<strong>Provenance only:</strong> bundle integrity is recomputed ' +
      'server-side; listing evidence here does NOT imply production ' +
      'verification (production_verified=false).</div>';
  }

  /* Audit-trail evidence (PR-V2): aggregate audit_events counts from the
   * residue summary plus L2-ticket linkage from the gateway audit rows.
   * Honest empty states when a run carries none. */
  function evidenceAuditPanel() {
    var audit = (snapshot.residue || {}).audit_events_summary;
    var l2 = gatewayCalls().filter(function (c) {
      return c.decision === 'ALLOW' && c.ticket_id;
    });
    if (!audit && !l2.length) { return ''; }
    var html = panel('Audit Trail Evidence', '<div class="kv">' +
      (audit
        ? kvCell('audit_events · total', esc(audit.total), true) +
          kvCell('audit_events · by action',
            esc(Object.keys(audit.by_action || {}).sort()
              .map(function (k) {
                return k + '×' + audit.by_action[k];
              }).join(', ') || 'n/a'), true)
        : '') +
      '</div>');
    if (l2.length) {
      var rows = l2.map(function (c) {
        return '<tr><td><code>' + esc(c.ticket_id) + '</code></td>' +
          '<td class="code">' + esc(c.tool) + '</td><td>' +
          stChip(c.decision) + '</td><td><code>' +
          esc(c.reason_code || 'n/a') + '</code></td><td class="code">' +
          esc(c.git_sha || 'n/a') + '</td></tr>';
      }).join('');
      html += panel('L2 Approval Records (gateway audit)',
        tableWrap(['Ticket', 'Tool', 'Decision', 'Reason', 'SHA'], rows));
    }
    return html;
  }

  function renderBenchmark() {
    var bs = snapshot.benchmark_summary || {};
    return pageHeader('benchmark') +
      panel('Evaluation Results', '<div class="metrics">' +
        metricCell('dataset', bs.dataset_version) +
        metricCell('unique cases', bs.unique_case_count) +
        metricCell('quality gate', bs.quality_gate_pass) +
        metricCell('confirmatory', bs.confirmatory_all_ok) +
        '</div>') +
      '<div class="boundary-banner">' +
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

  /* ── Freshness banner (state chip + data time + poll health) ────────── */

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
    el.innerHTML = '<span class="chip" aria-hidden="true"></span>' +
      '<span>' + esc(state) + '</span>' +
      '<span aria-hidden="true">·</span>' +
      '<span>data: ' + esc(freshness || 'n/a') + '</span>' +
      '<span aria-hidden="true">·</span>' +
      '<span>poll: ' + esc(poll) + '</span>' +
      '<span aria-hidden="true">·</span>' +
      '<span>failures: ' + esc(consecutiveFailures) + '</span>';
    if (btn) {
      btn.disabled = (state === 'LOADING');
      btn.textContent = (state === 'STALE')
        ? 'Resume auto-refresh' : 'Refresh now';
    }
  }

  /* ── Fetch cycle (GET only, allowlisted URLs only) ──────────────────── */

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

  /* ── Timer lifecycle: created ONCE, cleaned up on stop/unload ───────── */

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

  /* ── Boot: REPLAY (404 on /api/live/*) never starts the engine ──────── */

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
