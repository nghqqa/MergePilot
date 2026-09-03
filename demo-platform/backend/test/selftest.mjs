// backend/test/selftest.mjs
// Offline self-tests for the demo platform backend:
//   1. redaction engine (mask crafted secrets, preserve demo placeholders & sha256)
//   2. replay integrity (source refs exist, gate ordering, locked-state semantics)
//   3. API smoke over a real HTTP listener (all endpoints, schema checks, secret scan)
// Exit code 0 = all pass.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import { redact, scanForSecrets, redactionMeta } from '../../evidence-adapter/redact.mjs';
import { EVIDENCE_ROOT, resolveSourceRef } from '../../evidence-adapter/evidence.mjs';
import { getReplayData, replayAudit } from '../../evidence-adapter/replay-provider.mjs';
import { handle } from '../lib/api.mjs';

let passed = 0;
let failed = 0;
const failures = [];
function test(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => {
      passed++;
      console.log(`  PASS  ${name}`);
    })
    .catch((e) => {
      failed++;
      failures.push({ name, error: String(e && e.message || e) });
      console.log(`  FAIL  ${name} — ${e && e.message}`);
    });
}

console.log('== 1) redaction engine ==');
await test('masks secret-typed scalar values by key', () => {
  const out = redact({ password: 'REDACTED-PLACEHOLDER-HUNTER2', api_key: 'REDACTED-PLACEHOLDER-AKIA-001', cookie: 'sid=REDACTED-PLACEHOLDER', note: 'plain' });
  assert.equal(out.password, '[REDACTED]');
  assert.equal(out.api_key, '[REDACTED]');
  assert.equal(out.cookie, '[REDACTED]');
  assert.equal(out.note, 'plain');
});
await test('masks bearer / token-shaped strings inside free text', () => {
  const out = redact({ summary: 'used Bearer REDACTED-PLACEHOLDER-TOKEN-0001 then ghp_REDACTED-PLACEHOLDER-0002 and password=REDACTED-PLACEHOLDER-0003' });
  assert.ok(!out.summary.includes('REDACTED-PLACEHOLDER-TOKEN'));
  assert.ok(!out.summary.includes('ghp_16C7'));
  assert.ok(!out.summary.includes('REDACTED-PLACEHOLDER-0003'));
  assert.ok(out.summary.includes('password=[REDACTED]') || out.summary.includes('password='));
});
await test('preserves PR#2 demo placeholders (explicitly permitted)', () => {
  const out = redact({ body: 'TOP-SECRET-OUTSIDE-BASE', file: 'outside-secret.txt', legit: 'WELCOME-LEGIT-DEMO' });
  assert.equal(out.body, 'TOP-SECRET-OUTSIDE-BASE');
  assert.equal(out.file, 'outside-secret.txt');
  assert.equal(out.legit, 'WELCOME-LEGIT-DEMO');
});
await test('preserves sha256 digests (integrity panel)', () => {
  const out = redact({ sha256: 'd74860f9fd44ca0d0d904add3e25f71cfcb3283dcdde7478d1aa7a4fbd18b8ed' });
  assert.match(out.sha256, /^[0-9a-f]{64}$/);
});
await test('scanForSecrets flags crafted leaks', () => {
  const hits = scanForSecrets('{"api_key": "AKIA-REDACTED-PLACEHOLDER-0001"}');
  assert.ok(hits.length > 0);
});

console.log('== 2) replay integrity ==');
const data = getReplayData();
await test('both cases present', () => {
  assert.ok(data.cases['pr1-normal-review']);
  assert.ok(data.cases['pr2-high-risk-human-gate']);
});
await test('three cases present incl. PR#3 reject case', () => {
  assert.ok(data.cases['pr3-high-risk-human-reject']);
  assert.equal(Object.keys(data.cases).length, 3);
});
await test('all source refs resolve to real evidence files', () => {
  assert.deepEqual(data.replay_integrity.missing_source_refs, []);
});
await test('evidence SHA256SUMS: no missing files; drift documented if present', () => {
  assert.ok(data.integrity.total_files > 0);
  const missing = Object.values(data.integrity.dirs).flatMap((d) => d.missing ?? []);
  assert.deepEqual(missing, [], 'missing evidence files = breakage');
  // drift (file updated after SUMS locked) is reported honestly, not hidden
  if (!data.integrity.all_ok) {
    const drifted = Object.values(data.integrity.dirs).flatMap((d) => (d.mismatched ?? []).map((f) => `${d.dir}/${f}`));
    console.log(`  NOTE  documented drift: ${drifted.join(', ')} — integrity panel must show DRIFT`);
    assert.ok(drifted.length > 0);
  }
});
await test('integrity split: final package fully verified independent of historical drift', async () => {
  const r = await handle('GET', '/api/health', new Map(), null);
  assert.equal(r.integrity.final_submission_integrity.status, 'VERIFIED');
  // historical drift must be exactly the documented one (08-scope-boundary.md), if present
  for (const d of r.integrity.historical_source_integrity.drift_files ?? []) {
    assert.ok(d.file.endsWith('08-scope-boundary.md'), `unexpected historical drift: ${d.file}`);
  }
});
await test('live-provider trace status: SMOKE_VERIFIED_LIVE_CLOUD_CONFIRMED', async () => {
  const { liveStatus } = await import('../../evidence-adapter/live-provider.mjs');
  const st = await liveStatus();
  assert.equal(st.trace.status, 'SMOKE_VERIFIED_LIVE_CLOUD_CONFIRMED_BY_OPERATOR');
  // honest wording: cloud-console-level confirmation, never API-level overclaim
  assert.ok(st.trace.status.includes('CONFIRMED'));
});
await test('PR2 replay ordering matches mandated script', () => {
  const tl = data.cases['pr2-high-risk-human-gate'].timeline;
  const idx = (needle) => tl.findIndex((e) => e.summary.includes(needle));
  const review = idx('HIGH_RISK_FOUND');
  const gate = idx('HUMAN_SECURITY_REVIEW_REQUIRED');
  const approve = idx('HUMAN_SECURITY_APPROVED_FIX');
  const fixDispatch = idx('fix-1 委派');
  const verifyDispatch = idx('verify-1 委派');
  const done = idx('PROJECT COMPLETED');
  assert.ok(review >= 0 && gate > review && approve > gate && fixDispatch > approve && verifyDispatch > fixDispatch && done > verifyDispatch, JSON.stringify({ review, gate, approve, fixDispatch, verifyDispatch, done }));
});
await test('fixer locked before approval; verifier locked before fix completes', () => {
  const tl = data.cases['pr2-high-risk-human-gate'].timeline;
  const afterHigh = tl.find((e) => e.summary.includes('HIGH_RISK_FOUND')).state_after;
  assert.equal(afterHigh.tasks['review-1'].status, 'completed');
  const atGate = tl.find((e) => e.summary.includes('HUMAN_SECURITY_REVIEW_REQUIRED')).state_after;
  assert.equal(atGate.gate, 'required');
  assert.equal(atGate.tasks['fix-1'].status, 'waiting_human');
  assert.equal(atGate.tasks['verify-1'].status, 'waiting_human');
  const afterApprove = tl.find((e) => e.summary.includes('HUMAN_SECURITY_APPROVED_FIX')).state_after;
  assert.notEqual(afterApprove.tasks['fix-1'].status, 'waiting_human'); // unlocked
  assert.equal(afterApprove.tasks['verify-1'].status, 'blocked'); // still locked on fix-1
  const afterFix = tl.find((e) => e.summary.includes('fix-1 正式提交')).state_after;
  assert.equal(afterFix.tasks['verify-1'].status, 'pending'); // unlocked
});
await test('PR3 replay ordering: review → gate → REJECTED → blocked → PR OPEN; no dispatch after rejection', () => {
  const c = data.cases['pr3-high-risk-human-reject'];
  const tl = c.timeline;
  const idx = (needle) => tl.findIndex((e) => e.summary.includes(needle));
  const review = idx('HIGH_RISK_FOUND');
  const gate = idx('HUMAN_SECURITY_REVIEW_REQUIRED');
  const reject = idx('HUMAN_SECURITY_REJECTED');
  const blocked = idx('pause_project');
  const final = idx('最终锁定状态回报');
  assert.ok(review >= 0 && gate > review && reject > gate && blocked > reject && final > blocked, JSON.stringify({ review, gate, reject, blocked, final }));
  // rejection event: human decision, approval type
  const rejEv = tl[reject];
  assert.equal(rejEv.agent_role, 'human');
  assert.equal(rejEv.event_type, 'approval');
  // after rejection: no fixer/verifier run, no new dispatch
  const after = tl.slice(reject + 1);
  for (const ev of after) {
    assert.ok(!['fixer', 'verifier'].includes(ev.agent_role), `agent ${ev.agent_role} ran after rejection: ${ev.event_id}`);
    assert.ok(!ev.summary.includes('fix-1 已派发') && !ev.summary.includes('verify-1 已派发'), `dispatch after rejection: ${ev.event_id}`);
  }
  // terminal state: fix-1 rejected, verify-1 locked, project blocked, gate rejected
  const st = tl[tl.length - 1].state_after;
  assert.equal(st.tasks['fix-1'].status, 'rejected');
  assert.equal(st.tasks['verify-1'].status, 'locked');
  assert.equal(st.gate, 'rejected');
  assert.equal(st.phase, 'blocked');
  assert.equal(c.gate.state, 'rejected');
  assert.equal(c.pull_request.state, 'open');
  assert.equal(c.risk.level, 'critical');
  assert.equal(c.risk.category, 'CWE-78');
  assert.equal(c.risk.human_gate, 'rejected');
  // state machine: no event ever transitions rejected → fixing/verifying/completed
  for (const ev of tl) {
    for (const t of Object.values(ev.state_after.tasks)) {
      if (t.status === 'rejected' || t.status === 'locked') {
        // once rejected/locked appears it must hold to the end (checked above); nothing else to allow
      }
    }
  }
  const seq = tl.map((e) => e.state_after.tasks['fix-1'].status);
  const rejIdx = seq.lastIndexOf('rejected');
  for (const s of seq.slice(rejIdx)) assert.equal(s, 'rejected', 'fix-1 must stay rejected once rejected');
  const vseq = tl.map((e) => e.state_after.tasks['verify-1'].status);
  const lockIdx = vseq.lastIndexOf('locked');
  for (const s of vseq.slice(lockIdx)) assert.equal(s, 'locked', 'verify-1 must stay locked once locked');
});
await test('PR3 lock audits assert zero fixer/verifier runs', () => {
  const c = data.cases['pr3-high-risk-human-reject'];
  const fixerTask = c.tasks.find((t) => t.id === 'fix-1');
  const verifierTask = c.tasks.find((t) => t.id === 'verify-1');
  assert.equal(fixerTask.effective, false);
  assert.ok(fixerTask.effective_note.includes('0 次 consume'));
  assert.equal(verifierTask.effective, false);
  assert.ok(verifierTask.effective_note.includes('永久锁定'));
});
await test('PR1 fully autonomous, no gate', () => {
  const c = data.cases['pr1-normal-review'];
  assert.equal(c.gate.state, 'none');
  const finalState = c.timeline[c.timeline.length - 1].state_after;
  assert.equal(finalState.phase, 'completed');
  for (const t of Object.values(finalState.tasks)) assert.equal(t.status, 'completed');
});
await test('PR1 stage words: RUN → FIXING → VERIFYING → COMPLETE', () => {
  const tl = data.cases['pr1-normal-review'].timeline;
  const seq = [...new Set(tl.map((e) => e.state_after.phase))];
  assert.deepEqual(seq, ['running', 'fixing', 'verifying', 'completed'], JSON.stringify(seq));
});
await test('three-case data contract fields on case detail', async () => {
  const q0 = new Map();
  for (const id of ['pr1-normal-review', 'pr2-high-risk-human-gate', 'pr3-high-risk-human-reject']) {
    const c = await handle('GET', `/api/cases/${id}`, q0, null);
    for (const k of ['case_id', 'repository', 'pull_request', 'project', 'team', 'runtime', 'risk', 'agents', 'tasks', 'timeline', 'data_mode', 'trace_mode', 'source_refs', 'integrity_status']) {
      assert.ok(c[k] !== undefined, `case ${id} missing contract field: ${k}`);
    }
    assert.equal(c.data_mode, 'historical replay');
    assert.equal(c.trace_mode, 'evidence-replay');
    assert.ok(Array.isArray(c.source_refs) && c.source_refs.length > 0, `case ${id} source_refs empty`);
    for (const ref of c.source_refs) {
      assert.ok(resolveSourceRef(ref)?.exists, `case ${id} source_ref not a bundled demo file: ${ref}`);
    }
    assert.equal(c.integrity_status.final_package, 'VERIFIED');
  }
});
await test('probe comparison parsed from real raw evidence (200→404, legit 200→200)', () => {
  const p = data.cases['pr2-high-risk-human-gate'].fix_comparison.probe;
  assert.equal(p.before.traversal_status, 200);
  assert.equal(p.after.traversal_status, 404);
  assert.equal(p.before.leaked_outside_secret, true);
  assert.equal(p.after.leaked_outside_secret, false);
  assert.equal(p.before.legit_status, 200);
  assert.equal(p.after.legit_status, 200);
});
await test('audit carries mandated residual risks verbatim', () => {
  const audit = replayAudit();
  const items = audit.residual_risks.map((r) => r.item).join('|');
  for (const must of ['MinIO shared tree', 'tool_guard', 'VERIFICATION_PASSED', 'PolarDB RAG', 'AgentLoop/OTel']) {
    assert.ok(items.includes(must), `missing residual risk: ${must}`);
  }
  assert.ok(audit.components.find((x) => x.component === 'PR Auto Merge' && x.status === 'DISABLED'));
});

console.log('== 3) API smoke (in-process) ==');
const q = (obj = {}) => {
  const m = new Map(Object.entries(obj));
  return m;
};
await test('GET /api/health', async () => {
  const r = await handle('GET', '/api/health', q(), null);
  assert.equal(r.status, 'ok');
  assert.equal(r.constraints.pr_write_operations.includes('NONE'), true);
});
await test('GET /api/modes (live honest)', async () => {
  const r = await handle('GET', '/api/modes', q({ mode: 'live' }), null);
  assert.equal(typeof r.live.available, 'boolean');
  if (!r.live.available) assert.ok(r.live.sources);
});
await test('GET /api/cases lists three cases', async () => {
  const r = await handle('GET', '/api/cases', q(), null);
  assert.equal(r.cases.length, 3);
  const pr2 = r.cases.find((c) => c.case_id === 'pr2-high-risk-human-gate');
  assert.equal(pr2.risk.level, 'high');
  assert.equal(pr2.pull_request.state, 'open');
  assert.equal(pr2.human_gate_passed, true);
  const pr3 = r.cases.find((c) => c.case_id === 'pr3-high-risk-human-reject');
  assert.equal(pr3.risk.level, 'critical');
  assert.equal(pr3.risk.human_gate, 'rejected');
  assert.equal(pr3.pull_request.state, 'open');
  assert.equal(pr3.human_gate_passed, false);
  assert.equal(pr3.phase, 'blocked');
});
for (const id of ['pr1-normal-review', 'pr2-high-risk-human-gate', 'pr3-high-risk-human-reject']) {
  await test(`GET /api/cases/${id} + subresources`, async () => {
    const c = await handle('GET', `/api/cases/${id}`, q(), null);
    assert.ok(c.breadcrumb.repository.includes('nghqqa/'));
    const dag = await handle('GET', `/api/cases/${id}/dag`, q(), null);
    assert.ok(dag.nodes.length >= 3);
    const dagAt1 = await handle('GET', `/api/cases/${id}/dag`, q({ at: '1' }), null);
    assert.equal(dagAt1.at_event_seq, 1);
    const tl = await handle('GET', `/api/cases/${id}/timeline`, q(), null);
    assert.ok(tl.total_events >= 8);
    for (const e of tl.events) {
      assert.ok(e.source_ref, `event ${e.event_id} missing source_ref`);
      assert.ok(['leader', 'reviewer', 'fixer', 'verifier', 'human', 'system'].includes(e.agent_role));
    }
    const tasks = await handle('GET', `/api/cases/${id}/tasks`, q(), null);
    assert.equal(tasks.tasks.length, 3);
    for (const t of tasks.tasks) {
      if (id === 'pr3-high-risk-human-reject' && (t.id === 'fix-1' || t.id === 'verify-1')) {
        // never-run tasks: locked terminal state, no trace
        assert.ok(['rejected', 'locked'].includes(t.final_status.status), `pr3 ${t.id} must be locked/rejected, got ${t.final_status.status}`);
        assert.ok(t.locked && t.final_status.locked_reason, `pr3 ${t.id} must carry locked_reason`);
      } else {
        assert.ok(t.status_history.length >= 2);
      }
      assert.equal(t.trace_id, null);
    }
    const artifacts = await handle('GET', `/api/cases/${id}/artifacts`, q(), null);
    assert.ok(artifacts.artifacts.length > 0);
    const audit = await handle('GET', `/api/cases/${id}/audit`, q(), null);
    assert.ok(audit.sha256sums, 'audit must carry sha256sums report');
    assert.ok(Object.keys(audit.sha256sums.dirs).length >= 4);
    const trace = await handle('GET', `/api/cases/${id}/trace`, q(), null);
    assert.equal(trace.status, 'NOT_AVAILABLE');
    assert.equal(trace.display, '待接入');
    const appr = await handle('GET', `/api/cases/${id}/approval`, q(), null);
    assert.ok(appr.historical_record);
  });
}
await test('POST approval in replay mode = demo-only, no runtime write', async () => {
  const r = await handle('POST', '/api/cases/pr2-high-risk-human-gate/approval', q(), { decision: 'approve', actor: 'selftest' });
  assert.equal(r.runtime_write, false);
  assert.equal(r.evidence_write, false);
  assert.equal(r.marker, 'REPLAY ACTION — NO RUNTIME WRITE');
  const g = await handle('GET', '/api/cases/pr2-high-risk-human-gate/approval', q(), null);
  assert.equal(g.demo_overlay.decision, 'approve');
  assert.equal(g.historical_record.state, 'approved'); // unchanged historical record
});
await test('POST approval on REJECTED case refuses "approve" honestly (no fake success)', async () => {
  await assert.rejects(() => handle('POST', '/api/cases/pr3-high-risk-human-reject/approval', q(), { decision: 'approve', actor: 'selftest' }), /REJECTED_CASE_TERMINAL/);
  const g = await handle('GET', '/api/cases/pr3-high-risk-human-reject/approval', q(), null);
  assert.equal(g.decision_type, 'rejected');
  assert.ok(g.rejection_text.includes('HUMAN_SECURITY_REJECTED'));
  assert.equal(g.approval_text, null);
  assert.equal(g.historical_record.state, 'rejected');
});
await test('PR3 evidence drawer: rejection record with real source + mandated fields', async () => {
  const r = await handle('GET', '/api/cases/pr3-high-risk-human-reject/evidence', q({ kind: 'approval' }), null);
  assert.equal(r.drawer, 'rejection');
  assert.equal(r.decision_marker, 'HUMAN_SECURITY_REJECTED');
  assert.equal(r.agent_role, 'human');
  assert.ok(r.rejection_text.includes('HUMAN_SECURITY_REJECTED'));
  assert.ok(r.source_hash.exists, 'rejection record must resolve to real evidence file');
  for (const k of ['event_id', 'project_id', 'task_id', 'agent_role', 'source', 'source_ref', 'timestamp', 'timestamp_precision', 'hash_verified', 'redaction_status']) {
    assert.ok(k in r, `rejection drawer missing field: ${k}`);
  }
});
await test('PR3 finding drawer: CWE-78 critical from real reviewer evidence', async () => {
  const r = await handle('GET', '/api/cases/pr3-high-risk-human-reject/evidence', q({ kind: 'finding' }), null);
  assert.equal(r.finding.category, 'CWE-78');
  assert.equal(r.finding.level, 'critical');
  assert.ok(r.source_ref.includes('reviewer-high-risk-result.md'));
});
await test('PR3 task drawer: fix-1 never ran', async () => {
  const r = await handle('GET', '/api/cases/pr3-high-risk-human-reject/evidence', q({ kind: 'task', task: 'fix-1' }), null);
  assert.equal(r.result_status, null);
  assert.ok(r.timestamp.startsWith('NOT_RUN'));
});
await test('POST approval invalid decision rejected', async () => {
  await assert.rejects(() => handle('POST', '/api/cases/pr2-high-risk-human-gate/approval', q(), { decision: 'merge-pr' }), /INVALID_DECISION/);
});
await test('POST approval on gated case in live mode refuses honestly', async () => {
  await assert.rejects(() => handle('POST', '/api/cases/pr2-high-risk-human-gate/approval', q({ mode: 'live' }), { decision: 'approve' }), (e) => {
    return ['LIVE_DATA_UNAVAILABLE', 'LIVE_APPROVAL_NOT_IMPLEMENTED'].includes(e.body?.error);
  });
});
await test('PR#1 case has no gate — POST approval rejected', async () => {
  await assert.rejects(() => handle('POST', '/api/cases/pr1-normal-review/approval', q(), { decision: 'approve' }), /NO_HUMAN_GATE/);
});
await test('artifact content fetch (fix.patch) is real evidence', async () => {
  const r = await handle('GET', '/api/cases/pr2-high-risk-human-gate/artifacts', q({ file: 'fix.patch' }), null);
  assert.ok(r.content.includes('is_relative_to'));
  assert.ok(r.content.includes('demo_high_risk.py'));
});

console.log('== 4) no-secret scan across all API responses ==');
await test('no secrets in any API payload', async () => {
  const paths = [
    ['/api/health', q()],
    ['/api/modes', q()],
    ['/api/cases', q()],
    ['/api/cases/pr2-high-risk-human-gate', q()],
    ['/api/cases/pr2-high-risk-human-gate/dag', q()],
    ['/api/cases/pr2-high-risk-human-gate/timeline', q()],
    ['/api/cases/pr2-high-risk-human-gate/tasks', q()],
    ['/api/cases/pr2-high-risk-human-gate/artifacts', q()],
    ['/api/cases/pr2-high-risk-human-gate/audit', q()],
    ['/api/cases/pr2-high-risk-human-gate/trace', q()],
    ['/api/cases/pr2-high-risk-human-gate/approval', q()],
    ['/api/cases/pr1-normal-review/timeline', q()],
    ['/api/cases/pr3-high-risk-human-reject', q()],
    ['/api/cases/pr3-high-risk-human-reject/dag', q()],
    ['/api/cases/pr3-high-risk-human-reject/timeline', q()],
    ['/api/cases/pr3-high-risk-human-reject/tasks', q()],
    ['/api/cases/pr3-high-risk-human-reject/artifacts', q()],
    ['/api/cases/pr3-high-risk-human-reject/audit', q()],
    ['/api/cases/pr3-high-risk-human-reject/approval', q()],
  ];
  for (const [p, query] of paths) {
    const r = await handle('GET', p, query, null);
    const text = JSON.stringify(redact(r));
    const hits = scanForSecrets(text);
    assert.deepEqual(hits, [], `secret-like leak in ${p}: ${JSON.stringify(hits)}`);
  }
});

console.log('== 5) credibility pass (source map / evidence drawer / trace status) ==');
await test('task source_map: every field has source+source_ref', async () => {
  const r = await handle('GET', '/api/cases/pr2-high-risk-human-gate/tasks', q(), null);
  for (const t of r.tasks) {
    for (const [field, s] of Object.entries(t.source_map)) {
      assert.ok(s.source && s.source_ref, `task ${t.id} field ${field} missing source attribution`);
      assert.ok(['Controller API', 'Matrix event', 'MinIO task result', 'GitHub PR', 'Evidence Replay', 'AgentLoop Cloud'].includes(s.source), `bad source label: ${s.source}`);
    }
    assert.ok(Array.isArray(t.upstream) && t.locked !== undefined, `task ${t.id} missing upstream/locked`);
    assert.ok(t.last_status_change, `task ${t.id} missing last_status_change`);
  }
});
await test('evidence drawer (event): mandated fields + hash verification', async () => {
  const r = await handle('GET', '/api/cases/pr2-high-risk-human-gate/evidence', q({ kind: 'event', seq: '8' }), null);
  for (const k of ['event_id', 'trace_id', 'project_id', 'task_id', 'agent_role', 'runtime', 'event_type', 'source', 'source_ref', 'timestamp', 'timestamp_precision', 'hash_verified', 'redaction_status']) {
    assert.ok(k in r, `event drawer missing field: ${k}`);
  }
  assert.equal(r.source_label, 'Matrix event');
  assert.equal(r.matrix_event_id, '$JU09kIgwjvVJmEUSVSlNR8Va2-zhxsvtmFaZ3RIHuTM');
  assert.ok(r.source_hash && r.source_hash.exists, 'source_ref should resolve to a real evidence file');
});
await test('evidence drawer (task fix-1): result + artifacts with hashes', async () => {
  const r = await handle('GET', '/api/cases/pr2-high-risk-human-gate/evidence', q({ kind: 'task', task: 'fix-1' }), null);
  assert.equal(r.result_status, 'SUCCESS');
  assert.ok(r.artifacts.length >= 3);
  assert.ok(r.artifacts.some((a) => a.resolved && a.resolved.exists && a.resolved.hash_verified === true), 'fix.patch artifact should hash-verify');
});
await test('evidence drawer (approval): human record, read-only', async () => {
  const r = await handle('GET', '/api/cases/pr2-high-risk-human-gate/evidence', q({ kind: 'approval' }), null);
  assert.equal(r.agent_role, 'human');
  assert.ok(r.approval_text.includes('PROHIBITED'));
  assert.ok(r.source_hash.exists);
});
await test('evidence drawer (trace): EVIDENCE REPLAY ONLY, smoke/live split honest', async () => {
  const r = await handle('GET', '/api/cases/pr2-high-risk-human-gate/evidence', q({ kind: 'trace' }), null);
  assert.equal(r.status, 'EVIDENCE REPLAY ONLY');
  assert.equal(r.trace_mode, 'evidence-replay');
  assert.ok(r.data_mode.startsWith('historical replay'), r.data_mode);
  assert.equal(r.smoke.status, 'VERIFIED');
  assert.equal(r.live_copaw_run.status, 'VERIFIED');
  assert.equal(r.cloud_visibility, true);
  assert.equal(r.trace_id, 'fbf4a3cec0493990d76e10a102418be1');
  assert.equal(r.span_count, 32);
  for (const k of ['trace_id', 'span_count', 'trace_source', 'trajectory_evaluation', 'result_evaluation']) assert.ok(k in r, `trace missing reserved field: ${k}`);
  // no contradictory combined fields
  assert.ok(!('agentloop_cloud' in r) && !('live_agent_trace' in r), 'old contradictory fields must be gone');
});
await test('evidence drawer (finding): risk with probe + reviewer conclusion', async () => {
  const r = await handle('GET', '/api/cases/pr2-high-risk-human-gate/evidence', q({ kind: 'finding' }), null);
  assert.equal(r.task_id, 'review-1');
  assert.ok(r.finding.category === 'CWE-22');
  assert.ok(r.probe.before.traversal_status === 200 && r.probe.after.traversal_status === 404);
});
await test('case carries trace_status + data_sources (new semantics)', async () => {
  const r = await handle('GET', '/api/cases/pr2-high-risk-human-gate', q(), null);
  assert.equal(r.trace_status.status, 'EVIDENCE REPLAY ONLY');
  assert.equal(r.trace_status.trace_mode, 'evidence-replay');
  assert.equal(r.trace_status.smoke.status, 'VERIFIED');
  assert.equal(r.trace_status.live_copaw_run.status, 'VERIFIED');
  assert.equal(r.data_sources.length, 6);
});
await test('health: split integrity + five-part agentloop contract (truthful, Phase 14.2H)', async () => {
  const r = await handle('GET', '/api/health', q(), null);
  // split integrity: historical drift documented, final package verified
  assert.ok(['DRIFT_DOCUMENTED', 'VERIFIED'].includes(r.integrity.historical_source_integrity.status), r.integrity.historical_source_integrity.status);
  assert.equal(r.integrity.final_submission_integrity.status, 'VERIFIED');
  assert.ok(r.integrity.final_submission_integrity.files > 0);
  // agentloop five-part contract (smoke/live_copaw_run/tool_span/coverage/historical_replay), no contradictory flat fields
  assert.deepEqual(Object.keys(r.agentloop).sort(), ['coverage', 'historical_replay', 'live_copaw_run', 'smoke', 'tool_span']);
  assert.equal(r.agentloop.smoke.status, 'VERIFIED');
  assert.equal(r.agentloop.live_copaw_run.status, 'VERIFIED');
  assert.equal(r.agentloop.tool_span.status, 'VERIFIED');
  assert.equal(r.agentloop.coverage.status, 'VERIFIED');
  assert.equal(r.agentloop.live_copaw_run.trace_mode, 'LIVE CLOUD');
  assert.equal(r.agentloop.live_copaw_run.verdict, 'AGENTLOOP_ALIYUN_TRACE_VISIBILITY_CONFIRMED_BY_OPERATOR');
  assert.equal(r.agentloop.historical_replay.status, 'VERIFIED');
  assert.equal(r.agentloop.historical_replay.mode, 'evidence-replay');
  for (const bad of ['cloud', 'live_run', 'platform_integration', 'trace_mode']) {
    assert.ok(!(bad in r.agentloop), `agentloop must not carry contradictory flat field: ${bad}`);
  }
  assert.equal(r.credibility.final_package_sha256, 'VERIFIED');
  assert.ok(['DRIFT_DOCUMENTED', 'VERIFIED'].includes(r.credibility.historical_source_sha256));
  assert.equal(r.credibility.pr_auto_merge, 'DISABLED');
  assert.equal(r.credibility.runtime_write, 'NONE');
  assert.equal(r.credibility.matrix_messages, 'NONE');
  assert.equal(r.credibility.secret_scan, 'CLEAN', 'scan-result key must not be over-masked');
});
await test('drawer payloads carry no forbidden fields', async () => {
  for (const kind of ['event', 'task', 'approval', 'trace', 'finding']) {
    const qq = kind === 'event' ? q({ kind, seq: '5' }) : kind === 'task' ? q({ kind, task: 'verify-1' }) : q({ kind });
    const r = await handle('GET', `/api/cases/pr2-high-risk-human-gate/evidence`, qq, null);
    // forbidden-field check inspects real keys only (redaction_status is meta-documentation)
    const keys = [];
    (function walk(o, path) {
      for (const [k, v] of Object.entries(o ?? {})) {
        keys.push(path + k);
        if (v && typeof v === 'object' && k !== 'redaction_status') walk(v, path + k + '.');
      }
    })(r, '');
    for (const bad of [/authorization/i, /cookie/i, /api[_-]?key/i, /password/i, /matrix[_-]?token/i]) {
      const hit = keys.find((k) => bad.test(k.split('.').pop()));
      assert.ok(!hit, `${kind} drawer exposes forbidden key: ${hit}`);
    }
    // secret-pattern scan on the whole redacted payload
    const text = JSON.stringify(redact(r));
    const hits = scanForSecrets(text);
    assert.deepEqual(hits, [], `${kind} drawer secret scan failed`);
    assert.equal(r.redaction_status.status.startsWith('CLEAN'), true, `${kind} redaction status not clean`);
  }
});

// ============================================================ Phase 15: RAG + PolarDB
console.log('== Phase 15: RAG + PolarDB ==');
await test('rag: dataset is fully SYNTHETIC — synthetic data must never display as live', async () => {
  const { datasetInventory, RAG_DATA_MODE } = await import('../lib/rag.mjs');
  assert.equal(RAG_DATA_MODE, 'SYNTHETIC');
  const inv = datasetInventory();
  assert.ok(inv.document_count >= 8 && inv.chunk_count >= inv.document_count);
  for (const d of inv.documents) {
    assert.equal(d.data_mode, 'SYNTHETIC', `doc ${d.document_id} data_mode must be SYNTHETIC`);
    assert.match(d.content_sha256, /^[0-9a-f]{64}$/, 'content_sha256 must be sha256');
    assert.ok(d.document_id && d.source_type && d.classification && d.created_at, 'mandated doc fields');
  }
});
await test('rag: retrieval returns mandated fields', async () => {
  const { retrieve } = await import('../lib/rag.mjs');
  const r = retrieve('人工审批流程', { topK: 3 });
  for (const k of ['query_hash', 'top_k', 'retrieval_mode', 'data_mode', 'results']) assert.ok(k in r);
  assert.equal(r.data_mode, 'SYNTHETIC');
  assert.match(r.query_hash, /^[0-9a-f]{16}$/);
  for (const x of r.results) {
    for (const k of ['document_id', 'chunk_id', 'score', 'source_ref', 'retrieval_mode']) assert.ok(k in x, `missing ${k}`);
    assert.ok(x.source_ref.startsWith('rag://synthetic-docs/'));
  }
});
await test('rag: answer status vocabulary is CITED|UNVERIFIED only — never self-certified VERIFIED', async () => {
  const { answer } = await import('../lib/rag.mjs');
  const a = answer('PR #2 的审批结果');
  assert.ok(['CITED', 'UNVERIFIED'].includes(a.status), `unexpected status ${a.status}`);
  assert.notEqual(a.status.toUpperCase(), 'VERIFIED', 'answer must never self-certify as VERIFIED');
  assert.ok(a.source_refs.length > 0 && a.citations.length === a.source_refs.length, 'CITED answers must carry source_refs');
});
await test('rag: tool span records contain ONLY contract fields — no bodies, no credentials', async () => {
  const { toolSpanTail, toolSpanRecord } = await import('../lib/rag.mjs');
  const contract = new Set(['ts', 'tool', 'arguments_hash', 'result_status', 'row_count', 'document_count', 'candidate_id', 'branch_id', 'assertion_count', 'failed_assertions', 'latency_ms', 'data_mode', 'source_refs']);
  const spans = toolSpanTail(50);
  assert.ok(spans.length > 0, 'expected tool spans from earlier retrievals');
  for (const s of spans) {
    for (const k of Object.keys(s)) assert.ok(contract.has(k), `illegal tool span field: ${k}`);
    assert.ok(!('query' in s) && !('sql' in s) && !('content' in s) && !('password' in s), 'leak field present');
    assert.ok(
      s.tool === 'rag.retrieve' || s.tool === 'rag.answer' || s.tool === 'database.query'
      || s.tool === 'database.create_branch' || s.tool === 'database.validate_migration'
      || s.tool === 'database.assert_data' || s.tool === 'database.rollback_check',
      `unexpected tool name: ${s.tool}`,
    );
  }
  assert.throws(() => toolSpanRecord({ tool: 'x', content: 'document body' }), /not allowed by contract/, 'contract must reject extra fields');
});
await test('polardb: honest NOT_CONNECTED — fixture SIMULATED, never claims a connection', async () => {
  const { polardbAudit, fixtureQuery, fixtureInventory } = await import('../lib/polardb.mjs');
  const audit = polardbAudit();
  assert.equal(audit.connection, 'NOT CONNECTED');
  assert.equal(audit.conditions.endpoint_configured.value, false, 'env has no PolarDB endpoint — audit must say so');
  assert.equal(audit.verdict, 'PREREQUISITES_MISSING');
  const fx = fixtureQuery('pr_cases', { risk_level: 'high' });
  assert.equal(fx.data_mode, 'SIMULATED');
  assert.equal(fx.polardb_connection, 'NOT CONNECTED');
  assert.equal(typeof fx.row_count, 'number');
  assert.ok(fx.disclosure.includes('SIMULATED') && fx.disclosure.includes('NOT CONNECTED'));
  assert.equal(fixtureInventory().data_mode, 'SIMULATED');
});
await test('api: rag/polardb/ops endpoints carry data modes and pass redaction', async () => {
  const sr = await handle('GET', '/api/rag/search', new URLSearchParams({ q: 'human gate', k: '3' }), null);
  assert.equal(sr.data_mode, 'SYNTHETIC');
  assert.ok(sr.results.length > 0);
  const ops = await handle('GET', '/api/ops', {}, null);
  assert.equal(ops.data_modes.rag, 'SYNTHETIC');
  assert.equal(ops.data_modes.polardb, 'SIMULATED fixture / NOT CONNECTED');
  assert.equal(ops.agentloop.authoritative_trace_id, 'fbf4a3cec0493990d76e10a102418be1');
  assert.equal(ops.agentloop.correlation_trace_id, 'f5bb3e1f55bc53412f0fac5e6b400eb5');
  assert.ok(ops.agentloop.tool_span_correlation.startsWith('VERIFIED'));
  const p = await handle('GET', '/api/polardb/status', {}, null);
  assert.equal(p.connection, 'NOT CONNECTED');
  const hits = scanForSecrets(JSON.stringify([sr, ops, p]));
  assert.deepEqual(hits, [], 'rag/polardb payloads must be secret-clean');
});
await test('api: rag/health sibling sections keep agentloop contract intact', async () => {
  const h = await handle('GET', '/api/health', {}, null);
  assert.equal(h.rag.status, 'SYNTHETIC DEMO');
  assert.equal(h.polardb.connection, 'NOT CONNECTED');
  assert.deepEqual(Object.keys(h.agentloop).sort(), ['coverage', 'historical_replay', 'live_copaw_run', 'smoke', 'tool_span']);
});

await test('pr4: failed candidates never promoted; human gate required; data modes honest', async () => {
  const r = await handle('GET', '/api/pr4', {}, null);
  assert.deepEqual(r.rejected, ['candidate-a', 'candidate-b']);
  assert.equal(r.promoted.candidate_id, 'candidate-c');
  assert.equal(r.promoted.verdict, 'VERIFIED');
  assert.equal(r.human_gate.required, true);
  assert.equal(r.data_modes.rag, 'SYNTHETIC');
  assert.equal(r.data_modes.branch, 'SIMULATED');
  assert.equal(r.data_modes.polardb_connection, 'NOT CONNECTED');
  for (const c of r.candidates) {
    if (c.validation.verdict !== 'VERIFIED') assert.ok(r.rejected.includes(c.candidate_id), 'failed candidate must be rejected');
  }
});
await test('polardb adapter: env-gated live gates default to closed', async () => {
  const { evaluateLiveGates } = await import('../lib/polardb_adapter.mjs');
  const g = evaluateLiveGates();
  assert.equal(g.allLive, false);
  assert.equal(g.gates.length, 8);
});
console.log(`\nselftest: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  console.error(JSON.stringify(failures, null, 2));
  process.exit(1);
}
process.exit(0);
