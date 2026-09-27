// console/backend/test/ragtrial-review.test.mjs — Review 联动安全边界契约测试。
// 锁死四条硬边界：RAG 不自动 finding/ticket/gate；Fixer 不吃 RAG-only；
// Verifier 只认独立 harness/test 证据；attachToRun 不触碰 findings/tickets/gates。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  RAG_EVIDENCE_KIND, toAuxEvidence, canAutoPromote, fixerPatchInputs,
  verifierAccepts, promotionRequest, attachToRun,
} from '../lib/ragtrial/review.mjs';

const hit = {
  score: 0.83,
  citation: {
    repo: 'mergepilot', branch: 'feat/local-rag-trial', doc_path: 'ops/runbook.md',
    line_start: 10, line_end: 24, para_index: 2, chunk_index: 1,
    doc_sha256: 'a'.repeat(64), chunk_sha256: 'b'.repeat(64),
    model_id: 'local-hash-v1', model_digest: 'c'.repeat(64), index_version: 1,
  },
  snippet: '…runbook 摘录…',
};
const ragEv = toAuxEvidence(hit, { runId: 'run-1' });
const harnessEv = { kind: 'harness_report', path: 'evidence/harness/2026-09-27/report.json' };

test('辅助证据形状：reference only + trusted:false + 排除清单', () => {
  assert.equal(ragEv.kind, RAG_EVIDENCE_KIND);
  assert.equal(ragEv.usage, 'reference_only');
  assert.equal(ragEv.trusted, false);
  assert.deepEqual(ragEv.policy.excluded_from, ['finding', 'ticket', 'gate', 'fixer_patch_input', 'verifier_evidence']);
  assert.equal(ragEv.citation.doc_path, 'ops/runbook.md');
});

test('无引用 hit 不能转证据', () => {
  assert.throws(() => toAuxEvidence({ score: 1, snippet: 'x' }), /without citation/);
});

test('canAutoPromote：任何组合恒拒绝（含纯 harness 证据——本试验无自动晋升路径）', () => {
  assert.equal(canAutoPromote([ragEv]).allowed, false);
  assert.ok(canAutoPromote([ragEv]).reasons.includes('rag_evidence_is_reference_only'));
  assert.equal(canAutoPromote([ragEv, harnessEv]).allowed, false);
  assert.equal(canAutoPromote([harnessEv]).allowed, false, '试验栈内一律人工评审');
  assert.equal(canAutoPromote([]).allowed, false);
});

test('promotionRequest：RAG-only 证据建 finding/ticket/gate 全拒', () => {
  for (const target of ['finding', 'ticket', 'gate']) {
    const r = promotionRequest({ target, evidence: [ragEv] });
    assert.equal(r.allowed, false, `${target} 必须拒绝`);
    assert.match(r.reason, /rag_evidence_cannot_auto_promote/);
  }
  const r2 = promotionRequest({ target: 'finding', evidence: [ragEv, harnessEv] });
  assert.equal(r2.allowed, false, '混合证据也不自动晋升');
});

test('fixerPatchInputs：RAG-only 输入 → fixer 不可启动', () => {
  const r = fixerPatchInputs([ragEv, ragEv]);
  assert.equal(r.fixer_may_run, false, 'Fixer 不得只依据 RAG 文本修改代码');
  assert.equal(r.allowed.length, 0);
  assert.equal(r.rejected.length, 2);
  const r2 = fixerPatchInputs([ragEv, harnessEv]);
  assert.equal(r2.fixer_may_run, true);
  assert.equal(r2.allowed.length, 1);
});

test('verifierAccepts：只接受独立 harness/test 证据，RAG 恒拒', () => {
  assert.equal(verifierAccepts(ragEv).accepted, false);
  assert.equal(verifierAccepts(harnessEv).accepted, true);
  assert.equal(verifierAccepts({ kind: 'test_evidence' }).accepted, true);
  assert.equal(verifierAccepts({ kind: 'rag_auxiliary' }).accepted, false);
});

test('attachToRun：只新增 review_context，不触碰 findings/tickets/gates', () => {
  const runView = { run_id: 'run-1', findings: [{ id: 'f-1' }], tickets: [], gates: [{ g: 1 }] };
  const out = attachToRun(runView, [ragEv]);
  assert.deepEqual(out.findings, [{ id: 'f-1' }], 'findings 原样');
  assert.deepEqual(out.tickets, [], 'tickets 原样');
  assert.deepEqual(out.gates, [{ g: 1 }], 'gates 原样');
  assert.equal(out.review_context.rag_auxiliary.length, 1);
  assert.match(out.review_context.rag_policy_note, /reference only/);
  assert.throws(() => attachToRun(runView, [{ kind: 'harness_report' }]), /only rag_auxiliary/);
});
