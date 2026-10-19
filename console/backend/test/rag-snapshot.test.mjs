// rag-snapshot.test.mjs — run 页知识库引用快照行为回归（RAG V2，#491 复核定案）。
// 锁定（无数据库纯函数层）：worker 查询构造 / 首写即定保存语义（SQL 形状）/
// 授权纯函数 / 引用规范化 / reference-only 契约。
// PG 级行为（幂等不覆盖/不可变/保存失败）由 rag-snapshot-pool.integration.mjs
// 与 rag-worker-snapshot.integration.mjs 锁定（CI 显式步骤）。
// 运行：cd console/backend && node --test test/rag-snapshot.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  saveRunRagSnapshot, loadRunRagSnapshot, authorizeSnapshotRead,
  toSnapshotCitations, RAG_EVIDENCE_KIND,
} from '../lib/ragtrial/rag-snapshot.mjs';
import { buildWorkerRagQuery, WORKER_QUERY_STRATEGY } from '../lib/ragtrial/worker-rag.mjs';

test('worker 查询构造（pr_context_v1）：PR 标题 + 发现标题拼接，400 字符截断', () => {
  assert.equal(WORKER_QUERY_STRATEGY, 'pr_context_v1');
  const q = buildWorkerRagQuery({ prTitle: 'fix docs', findingTitles: ['finding-A', 'finding-B'] });
  assert.equal(q, 'fix docs；finding-A；finding-B');
});

test('worker 查询构造：空上下文 → null（如实无可检索内容）；无标题 finding 被过滤', () => {
  assert.equal(buildWorkerRagQuery({ prTitle: '', findingTitles: [] }), null);
  assert.equal(buildWorkerRagQuery({}), null);
  assert.equal(buildWorkerRagQuery({ prTitle: 't', findingTitles: [null, '', 'ok'] }), 't；ok');
});

test('worker 查询构造：超长截断至 400', () => {
  const q = buildWorkerRagQuery({ prTitle: 'x'.repeat(500), findingTitles: [] });
  assert.ok(q.length <= 400);
});

// 保存语义（stub pool 锁 SQL 形状：ON CONFLICT (run_id, corpus_branch) DO NOTHING——
// 首写即定，重试不覆盖；真实 PG 行为在 pool 集成测试）。
function stubPool({ insertRows, existingRow, capture }) {
  return {
    async query(text, params) {
      if (capture) capture.push({ text: text.replace(/\s+/g, ' ').trim(), params });
      if (/INSERT INTO ragtrial.run_rag_snapshots/.test(text)) return { rows: insertRows ?? [] };
      return { rows: existingRow ? [existingRow] : [] };
    },
  };
}
const BASE = {
  runId: '0'.repeat(8) + '-0000-0000-0000-000000000000', tenantId: 't-uuid',
  repo: 'nghqqa/ragdemo', branch: 'main', reviewHead: 'e'.repeat(40), baseRef: 'main',
  corpusBranch: 'main', modelId: 'local-hash-v1', modelDigest: 'm'.repeat(64),
  indexVersion: 1, queryText: 'q', queryStrategy: 'pr_context_v1',
  citations: [], serviceState: 'hit',
};

test('快照保存：INSERT 形状=ON CONFLICT (run_id, corpus_branch) DO NOTHING（重试不覆盖）', async () => {
  const capture = [];
  await saveRunRagSnapshot(stubPool({ insertRows: [{ snapshot_id: 's1' }], capture }), { ...BASE });
  const sql = capture[0].text;
  assert.ok(sql.includes('ON CONFLICT (run_id, corpus_branch) DO NOTHING'), sql);
  assert.ok(sql.includes('base_ref'), 'base_ref 落列');
  assert.equal(capture[0].params[6], 'main', 'corpus_branch 参数');
});

test('快照保存：首写成功 → saved=true；冲突 → saved=false 且返回既有行（不覆盖）', async () => {
  const existing = { snapshot_id: 's0', citations: [{ old: true }], snapshot_at: new Date() };
  const r1 = await saveRunRagSnapshot(stubPool({ insertRows: [{ snapshot_id: 's1' }] }), { ...BASE });
  assert.equal(r1.saved, true);
  const r2 = await saveRunRagSnapshot(
    stubPool({ insertRows: [], existingRow: existing }), { ...BASE, citations: [{ new: true }] });
  assert.equal(r2.saved, false, '冲突=非本次写入');
  assert.equal(r2.row.snapshot_id, 's0');
  assert.deepEqual(r2.row.citations, [{ old: true }], '返回既有 citations（不被新值覆盖）');
});

test('快照保存：写入即无行（DO NOTHING 且无既有）→ saved=false row=null（未留存语义）', async () => {
  const r = await saveRunRagSnapshot(stubPool({ insertRows: [], existingRow: null }), { ...BASE });
  assert.equal(r.saved, false);
  assert.equal(r.row, null);
});

test('快照读取：无行如实返回 null（不用当前查询补造历史）', async () => {
  const pool = stubPool({ insertRows: [], existingRow: null });
  assert.equal(await loadRunRagSnapshot(pool, { runId: 'r1', corpusBranch: 'main' }), null);
});

test('快照读取授权：租户一致 + repo 在授权面 → ok；租户不一致/越权 repo → 拒绝', () => {
  const snap = { tenant_id: 't-1', repo: 'nghqqa/ragdemo' };
  const authOk = { _mu: { tenantId: 't-1' }, repos: ['nghqqa/ragdemo'] };
  assert.deepEqual(authorizeSnapshotRead(authOk, snap), { ok: true });
  assert.equal(authorizeSnapshotRead({ _mu: { tenantId: 't-2' }, repos: ['nghqqa/ragdemo'] }, snap).ok, false);
  assert.equal(authorizeSnapshotRead({ _mu: { tenantId: 't-1' }, repos: ['other/repo'] }, snap).reason, 'repo_not_in_allowlist');
  assert.equal(authorizeSnapshotRead(null, snap).ok, false);
  assert.equal(authorizeSnapshotRead(authOk, null).ok, false);
});

test('toSnapshotCitations：保留 citation + 补 corpus_branch + reference-only', () => {
  const results = [{
    score: 0.53,
    snippet: 'test snippet',
    citation: { repo: 'nghqqa/ragdemo', branch: 'main', doc_path: 'rag-design.md',
      line_start: 14, line_end: 25, doc_sha256: 'd'.repeat(64),
      chunk_sha256: 'c'.repeat(64), model_id: 'bge-m3',
      model_digest: 'm'.repeat(64), index_version: 1 },
  }];
  const out = toSnapshotCitations(results);
  assert.equal(out.length, 1);
  assert.equal(out[0].kind, RAG_EVIDENCE_KIND);
  assert.equal(out[0].usage, 'reference_only');
  assert.equal(out[0].trusted, false);
  assert.equal(out[0].citation.corpus_branch, 'main');
  assert.equal(out[0].citation.doc_path, 'rag-design.md');
  assert.equal(out[0].citation.line_start, 14);
  assert.equal(out[0].citation.index_version, 1);
});
