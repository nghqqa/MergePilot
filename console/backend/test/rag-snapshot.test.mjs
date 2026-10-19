// rag-snapshot.test.mjs — run 页知识库引用快照行为回归（RAG V2，2026-10-18）。
// 锁定：幂等保存 / 不可变性 / 无快照如实 / 双源区分 / reference-only 契约。
// 运行：cd console/backend && node --test test/rag-snapshot.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildSnapshotQuery, toSnapshotCitations, RAG_EVIDENCE_KIND,
} from '../lib/ragtrial/rag-snapshot.mjs';

test('快照查询构造：PR 标题 + 发现标题拼接，400 字符截断', () => {
  const q = buildSnapshotQuery('fix docs', [
    { title: 'finding-A' }, { title: 'finding-B' },
  ]);
  assert.equal(q, 'fix docs；finding-A；finding-B');
});

test('快照查询构造：空上下文 → null（如实标记无可检索内容）', () => {
  assert.equal(buildSnapshotQuery('', []), null);
  assert.equal(buildSnapshotQuery(null, null), null);
});

test('快照查询构造：超长截断至 400', () => {
  const q = buildSnapshotQuery('x'.repeat(500), []);
  assert.ok(q.length <= 400);
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
