// backend/lib/finals_evidence.mjs — read-only loaders over the three finals
// evidence directories (2026-09-14), each with its own honesty tier:
//
//   finalsDbLoop     REAL SQL VERIFICATION on an isolated PostgreSQL clone
//                    (NOT an Agentic Database branch; PolarDB stays NOT CONNECTED)
//   finalsReworkLoop MECHANISM VERIFICATION — real controller + real PG + real tests,
//                    agent semantic output supplied as controlled input (no LLM,
//                    no Matrix/Element handoff)
//   finalsRagLoop    REAL OFFLINE EXPERIMENT on the SYNTHETIC corpus
//
// Every loader verifies the directory's SHA256SUMS first and returns
// { available:false } instead of inventing anything when evidence is absent.
// The PR #4 human gate below is a REPLAY overlay (in-memory, no runtime write),
// version-bound the same way db_release_gate() is in the audit DB.

import { exists, readJson, readText, verifyDirIntegrity, EVIDENCE_DIRS } from '../../evidence-adapter/evidence.mjs';

const TIERS = {
  finalsDbLoop: 'REAL_SQL_VERIFICATION_ISOLATED_POSTGRES',
  finalsReworkLoop: 'MECHANISM_VERIFICATION',
  finalsRagLoop: 'REAL_OFFLINE_EXPERIMENT_SYNTHETIC_CORPUS',
  // Live AgentTeams runs (2026-09-16) — integrity-only summaries here; the
  // case payloads are built in demo_cases.mjs from the same dirs.
  finalsPr2Live: 'REAL_EXECUTED_AGENTTEAMS_LIVE_20260916',
  finalsPr3Live: 'REAL_EXECUTED_AGENTTEAMS_LIVE_20260916',
};

const cache = new Map();

function load(key) {
  if (cache.has(key)) return cache.get(key);
  let out;
  // The live-run dirs carry README.md rather than report.json.
  const marker = exists(key, 'report.json') ? 'report.json' : (exists(key, 'README.md') ? 'README.md' : null);
  if (!marker) {
    out = { available: false, key, dir: EVIDENCE_DIRS[key], tier: TIERS[key], reason: 'EVIDENCE_NOT_AVAILABLE' };
  } else {
    const integrity = verifyDirIntegrity(key);
    out = {
      available: true, key, dir: EVIDENCE_DIRS[key], tier: TIERS[key], integrity,
      report: marker === 'report.json' ? readJson(key, 'report.json') : null,
      run_meta: exists(key, 'run-meta.json') ? readJson(key, 'run-meta.json') : null,
    };
  }
  cache.set(key, out);
  return out;
}

export function finalsIntegrity() {
  const out = {};
  for (const key of Object.keys(TIERS)) {
    const e = load(key);
    out[key] = e.available
      ? { dir: e.dir, tier: e.tier, verified: e.integrity.verified, files: e.integrity.files, mismatched: e.integrity.mismatched, missing: e.integrity.missing }
      : { dir: e.dir, tier: e.tier, verified: false, reason: e.reason };
  }
  return out;
}

// ------------------------------------------------------------- DB loop (PR #4)

function step(report, name) {
  return (report.steps || []).find((s) => s.step === name) || null;
}

export function dbLoopSummary() {
  const e = load('finalsDbLoop');
  if (!e.available) return { available: false, tier: e.tier, reason: e.reason };
  const r = e.report;
  const v1 = step(r, 'verify_rev1_attempt1');
  const v2 = step(r, 'verify_rev2_attempt1');
  const late = step(r, 'verify_rev2_late_callback_attempt3');
  const t1 = step(r, 'S3_code_tests_rev1');
  const t2 = step(r, 'S3_code_tests_rev2');
  const ctx = step(r, 'S5b_context_fetch');
  const ticket = step(r, 'S7_ticket_created');
  const bind = step(r, 'S7_bind_verification');
  const approve = step(r, 'S7_l2_approve');
  const pkg = step(r, 'S11_migration_plan_package');
  const gateTimeline = (r.gate_timeline || []).map((g) => ({ label: g.label, valid: g.valid, reason: g.reason, bound_head_sha: g.bound_head_sha, current_head_sha: g.current_head_sha }));
  const negatives = r.negative_tests || [];
  return {
    available: true,
    tier: e.tier,
    not_agentic_database_branch: r.not_agentic_database_branch === true,
    polardb: r.polardb,
    data_mode: r.data_mode,
    integrity: { verified: e.integrity.verified, files: e.integrity.files },
    environment: r.environment,
    case: r.case,
    baseline: step(r, 'S1_baseline'),
    context_fetch: ctx,
    attempts: [
      v1 && {
        revision: 1, head_sha: r.case.head_rev1, script_digest: step(r, 'S2_candidate_rev1')?.script_digest,
        code_tests: t1 && { verdict: t1.outcome, tests_run: t1.tests_run, failures: t1.failures, command: t1.command },
        migration: { verdict: v1.outcome.split('/')[0], failure_class: v1.outcome.split('/')[1], error: v1.migration_error },
        assertions: { passed: v1.assertions_passed, total: v1.assertions_total, failed: v1.failed_assertions },
        verification_id: v1.verification_id, report_digest: v1.report_digest, attempt: 1,
      },
      v2 && {
        revision: 2, head_sha: r.case.head_rev2, script_digest: step(r, 'S6_candidate_rev2')?.script_digest, parent_candidate_id: step(r, 'S6_candidate_rev2')?.parent_candidate_id,
        code_tests: t2 && { verdict: t2.outcome, tests_run: t2.tests_run, failures: t2.failures, command: t2.command },
        migration: { verdict: v2.outcome.split('/')[0], failure_class: null, error: null },
        assertions: { passed: v2.assertions_passed, total: v2.assertions_total, failed: v2.failed_assertions },
        verification_id: v2.verification_id, report_digest: v2.report_digest, attempt: 1,
      },
      late && {
        revision: 2, attempt: 3, late_callback: true, migration: { verdict: late.outcome.split('/')[0] },
        verification_id: late.verification_id, note: 'recorded AFTER the follow-up commit; the gate stayed STALE',
      },
    ].filter(Boolean),
    approval: ticket && {
      ticket_id: ticket.ticket_id, bound_verification_id: bind?.verification_id, race_winner: bind?.race_winner,
      approved: approve?.approved === true, approved_by: approve?.approved_by, status: approve?.outcome,
    },
    gate_timeline: gateTimeline,
    followup: { head_sha: r.case.head_rev3_followup, change: r.case.followup_change,
      gate_after: gateTimeline.find((g) => g.label === 'after_followup_commit')?.reason || null },
    negative_tests: { total: negatives.length, ok: negatives.filter((n) => n.ok).length, names: negatives.map((n) => n.name) },
    plan_package: pkg && { path: pkg.path, files: pkg.files },
    final_disposition: r.final_disposition,
    outcome_signature: r.outcome_signature,
    run_meta: e.run_meta,
  };
}

// ---------------------------------------------------- PR #4 replay human gate

// In-memory REPLAY overlay: never a runtime write. It mirrors the audit DB's
// db_release_gate() semantics on the recorded versions: an approval is bound
// to (verification_id, head_sha); once the operator replays the recorded
// follow-up commit, the bound head is no longer the current head and the
// approval is STALE (409 on any further approve for that version).
const overlay = { approval: null, followup_applied: false };

export function pr4GateState() {
  const s = dbLoopSummary();
  if (!s.available) return { available: false, reason: s.reason };
  const verified = s.attempts.find((a) => a.revision === 2 && a.migration.verdict === 'PASS' && !a.late_callback);
  const currentHead = overlay.followup_applied ? s.followup.head_sha : verified?.head_sha;
  let reason;
  if (!overlay.approval) reason = 'AWAITING_OPERATOR_DECISION';
  else if (overlay.approval.decision === 'reject') reason = 'REJECTED_TERMINAL';
  else if (overlay.approval.decision === 'hold') reason = 'HELD_BY_OPERATOR';
  else if (overlay.followup_applied) reason = 'STALE_SUPERSEDED_BY_NEW_REVISION';
  else reason = 'OK';
  return {
    available: true,
    marker: 'REPLAY ACTION — NO RUNTIME WRITE',
    verified_version: verified && { verification_id: verified.verification_id, head_sha: verified.head_sha, script_digest: verified.script_digest, report_digest: verified.report_digest },
    current_head_sha: currentHead,
    followup_applied: overlay.followup_applied,
    recorded_gate_after_followup: s.followup.gate_after,
    approval: overlay.approval,
    valid: reason === 'OK',
    reason,
    audit_db_equivalent: 'db_release_gate(ticket, target_data_digest) in tools/audit-db/m9_migration_verification.sql',
    runtime_write: false, evidence_write: false, matrix_message_sent: false,
  };
}

export function pr4Decide({ decision, verification_id, head_sha, actor }) {
  const s = dbLoopSummary();
  if (!s.available) return { error: 'EVIDENCE_NOT_AVAILABLE', status: 503 };
  if (!['approve', 'reject', 'hold'].includes(decision)) return { error: 'INVALID_DECISION', status: 400 };
  const gate = pr4GateState();
  if (gate.approval && gate.approval.decision === 'reject') return { error: 'REJECTED_TERMINAL', status: 409, detail: 'PR #4 verified version was rejected; a new candidate revision must be verified' };
  const v = gate.verified_version;
  if (!verification_id || verification_id !== v.verification_id) return { error: 'VERIFICATION_MISMATCH', status: 409, detail: `decision must reference the verified version ${v.verification_id}` };
  if (!head_sha || head_sha !== v.head_sha) return { error: 'HEAD_MISMATCH', status: 409, detail: `decision must reference head ${v.head_sha}` };
  if (gate.followup_applied) return { error: 'STALE_VERSION', status: 409, detail: `head ${v.head_sha.slice(0, 12)} superseded by ${gate.current_head_sha.slice(0, 12)}; re-verify before approving` };
  overlay.approval = {
    decision, actor: actor || 'demo-operator', recorded_at: new Date().toISOString(),
    bound: { verification_id: v.verification_id, head_sha: v.head_sha, script_digest: v.script_digest, report_digest: v.report_digest },
    marker: 'REPLAY ACTION — NO RUNTIME WRITE',
  };
  return { ok: true, gate: pr4GateState(), runtime_write: false, evidence_write: false, matrix_message_sent: false, historical_record_unchanged: true };
}

export function pr4ApplyFollowup() {
  const s = dbLoopSummary();
  if (!s.available) return { error: 'EVIDENCE_NOT_AVAILABLE', status: 503 };
  overlay.followup_applied = true;
  return { ok: true, replayed_step: 'S9_run3_followup_commit_registered', recorded_gate_result: s.followup.gate_after, gate: pr4GateState() };
}

export function pr4Reset() {
  overlay.approval = null;
  overlay.followup_applied = false;
  return { ok: true, gate: pr4GateState() };
}

// ------------------------------------------------------- rework loop (D1)

export function reworkLoopSummary() {
  const e = load('finalsReworkLoop');
  if (!e.available) return { available: false, tier: e.tier, reason: e.reason };
  const r = e.report;
  const a = r.scenarios?.A_rework_then_pass;
  const patches = {};
  for (const att of a?.attempts || []) {
    if (att.diff_file && exists('finalsReworkLoop', att.diff_file)) patches[`attempt${att.attempt}`] = readText('finalsReworkLoop', att.diff_file);
  }
  return {
    available: true,
    tier: e.tier,
    integrity: { verified: e.integrity.verified, files: e.integrity.files },
    real: r.real, controlled_input: r.controlled_input, not_executed: r.not_executed,
    controller: r.controller,
    scenarios: Object.fromEntries(Object.entries(r.scenarios || {}).map(([k, v]) => [k, {
      run_id: v.run_id, checks: v.checks,
      final_task: v.final?.task || null,
      stage_runs: v.final?.stage_runs || [],
      dispatch_outbox: (v.final?.dispatch_outbox || []).map((o) => ({ target_agent: o.target_agent, target_stage: o.target_stage, attempt: o.attempt, body: o.body })),
      stage_events: (v.final?.stage_events || []).map((ev) => ({ event_id: ev.event_id, sender: ev.sender, event_type: ev.event_type, stage: ev.stage, status: ev.status })),
      attempts: v.attempts || null, verdict_sequence: v.verdict_sequence || null, sequence: v.sequence || null,
      observation: v.observation || null, observations: v.observations || null,
    }])),
    risk_basis: a?.risk_basis || null,
    patches,
    summary: r.summary,
    outcome_signature: r.outcome_signature,
    run_meta: e.run_meta,
  };
}

// ------------------------------------------------------------ RAG loop (D3)

export function ragLoopSummary() {
  const e = load('finalsRagLoop');
  if (!e.available) return { available: false, tier: e.tier, reason: e.reason };
  const r = e.report;
  return {
    available: true, tier: e.tier, integrity: { verified: e.integrity.verified, files: e.integrity.files },
    data_mode: r.dataset?.data_mode, observation: r.observation,
    dataset: { families: r.dataset?.n_families, queries: r.dataset?.n_queries, tuning_queries: r.dataset?.n_tuning_queries, heldout_queries: r.dataset?.n_heldout_queries, leakage_max: r.dataset?.leakage?.max },
    baseline_heldout: { hit_at_1: r.baseline?.heldout?.hit_at_1, hit_at_3: r.baseline?.heldout?.hit_at_3, mrr: r.baseline?.heldout?.mrr },
    selected: r.tuning?.selected?.strategy?.id,
    selected_knobs: r.tuning?.selected?.strategy ? { dim: r.tuning.selected.strategy.dim, cjk_bigram: r.tuning.selected.strategy.cjk_bigram, idf: r.tuning.selected.strategy.idf } : null,
    backtest_heldout: { hit_at_1: r.backtest?.heldout?.hit_at_1, hit_at_3: r.backtest?.heldout?.hit_at_3, mrr: r.backtest?.heldout?.mrr },
    regressions: (r.backtest?.comparison?.regressed || []).map((x) => x.query_id),
    promotion: r.backtest?.promotion?.decision,
    badcases: (r.badcases || []).length,
    run_meta: e.run_meta,
  };
}
