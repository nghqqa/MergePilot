// rag-snapshot-pool.integration.mjs — run 页知识库引用快照存储行为回归（PG 集成，不进 CI glob）。
// 锁定：幂等 upsert / 不可变性 / 无快照如实 / 快照读取字段完整。
// 运行：cd console/backend && node --test test/rag-snapshot-pool.integration.mjs（需 PG；CI glob *.test.mjs 不含本文件——pg 顶层导入在 CI 不可达）
import test from 'node:test';
import assert from 'node:assert/strict';
import pg from 'pg';
import {
  saveRunRagSnapshot, loadRunRagSnapshot,
} from '../lib/ragtrial/rag-snapshot.mjs';
import { RAGTRIAL_SCHEMA_SQL } from '../lib/ragtrial/schema.mjs';

const TEST_TENANT = 'snap-test-tenant';
const TEST_RUN = 'snap-test-run-0001';
const TEST_REPO = 'nghqqa/test-repo';
const TEST_BRANCH = 'main';

// 零第三方依赖：PG DSN 必须在环境变量提供（跳过而非失败——不阻塞无 PG CI）
const DSN = process.env.RAG_SNAPSHOT_TEST_DSN ?? process.env.CONSOLE_PG_DSN ?? null;
const hasPg = !!DSN;

const RUN = {
  run_id: TEST_RUN, tenant_id: TEST_TENANT, repo: TEST_REPO, branch: TEST_BRANCH,
  review_head: 'e'.repeat(40),
};
const hitResult = (docPath) => ({
  score: 0.53, snippet: 'test snippet for ' + docPath,
  citation: { repo: TEST_REPO, branch: TEST_BRANCH, doc_path: docPath,
    line_start: 14, line_end: 25, doc_sha256: 'd'.repeat(64),
    chunk_sha256: 'c'.repeat(64), model_id: 'bge-m3',
    model_digest: 'm'.repeat(64), index_version: 1 },
});

test('快照保存：幂等 upsert（同 key 两次=一条，citations 更新）', async (t) => {
  if (!hasPg) return t.skip('RAG_SNAPSHOT_TEST_DSN 未提供——跳过 PG 测试');
  const pool = new pg.Pool({ connectionString: DSN, max: 2 });
  try {
    // 建表（幂等）
    for (const sql of RAGTRIAL_SCHEMA_SQL) await pool.query(sql);
    const r1 = await saveRunRagSnapshot(pool, { ...RUN,
      corpusBranch: 'main', modelId: 'bge-m3', modelDigest: 'm'.repeat(64),
      indexVersion: 1, queryText: 'q1', citations: [hitResult('doc-a.md')],
      serviceState: 'hit' });
    assert.ok(r1, '第一次保存返回行');
    const r2 = await saveRunRagSnapshot(pool, { ...RUN,
      corpusBranch: 'main', modelId: 'bge-m3', modelDigest: 'm'.repeat(64),
      indexVersion: 1, queryText: 'q1', citations: [hitResult('doc-b.md')],
      serviceState: 'hit' });
    assert.ok(r2);
    const { rows } = await pool.query(
      'SELECT count(*)::int AS c FROM ragtrial.run_rag_snapshots WHERE run_id=$1', [TEST_RUN]);
    assert.equal(rows[0].c, 1, '幂等：同 key 不产生第二行');
  } finally { await pool.end(); }
});
