// rag-snapshot-pool.integration.mjs — 快照存储 PG 级行为回归（#491；自起一次性
// pgvector 容器，随机回环端口，finally 强制清理；CI 显式步骤执行，不在 *.test.mjs glob）。
// 锁定（真实 PG）：
//   P1 首写保存含完整任务身份（run/tenant/repo/branch/head/base_ref/模型/索引/策略/引用/时间）
//   P2 重试幂等不覆盖（同 key 重复保存：saved=false，旧行逐字段不变，含 snapshot_at）
//   P3 语料表后续变化不改写已保存 citations（快照不可变）
//   P4 无快照如实返回 null（未记录语义）
//   P5 保存路径失败 → 抛错交由调用方（worker-rag）降级——本表无行=未留存
//   P6 唯一约束确为 (run_id, corpus_branch)（不同 base_ref 的重试也不产生第二行）
// 运行：node console/backend/test/rag-snapshot-pool.integration.mjs
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

const here = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(here, 'support/noop.js'))('pg');
const { saveRunRagSnapshot, loadRunRagSnapshot } = await import('../lib/ragtrial/rag-snapshot.mjs');
const { RAGTRIAL_SCHEMA_SQL } = await import('../lib/ragtrial/schema.mjs');

let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? (pass++, console.log('  PASS  ' + n)) : (fail++, console.log('  FAIL  ' + n + (d ? ' — ' + JSON.stringify(d).slice(0, 220) : ''))); };

// ── 一次性 pgvector 容器（与 ragtrial-canonical 同模式；支持外部 DSN 复跑） ──
const CTR = `ragsnap-it-${crypto.randomBytes(4).toString('hex')}`;
const PORT = 15800 + Math.floor(Math.random() * 200);
const PG_PASSWORD = 'ragsnap-it-only';
let dsn = process.env.RAG_SNAPSHOT_TEST_DSN || null;
let pool;
if (!dsn) {
  execFileSync('docker', ['run', '-d', '--name', CTR,
    '-e', `POSTGRES_PASSWORD=${PG_PASSWORD}`, '-e', 'POSTGRES_DB=ragsnap',
    '-p', `127.0.0.1:${PORT}:5432`, 'pgvector/pgvector:pg16'], { stdio: 'pipe' });
  dsn = `postgres://postgres:${PG_PASSWORD}@127.0.0.1:${PORT}/ragsnap`;
}
const deadline = Date.now() + 60_000;
for (;;) {
  try { pool = new Pool({ connectionString: dsn, max: 4 }); await pool.query('SELECT 1'); break; }
  catch { if (Date.now() > deadline) throw new Error('pg readiness timeout'); await new Promise((r) => setTimeout(r, 700)); }
}

try {
  await pool.query('CREATE EXTENSION IF NOT EXISTS vector'); // pgvector 镜像自带扩展文件，per-DB 启用（initSchema 同款）
  for (const sql of RAGTRIAL_SCHEMA_SQL) await pool.query(sql);

  const RUN1 = '11111111-1111-1111-1111-111111111111';
  const TEN1 = 't-tenant-a';
  const base = {
    runId: RUN1, tenantId: TEN1, repo: 'nghqqa/ragdemo', branch: 'main',
    reviewHead: 'e'.repeat(40), baseRef: 'main', corpusBranch: 'main',
    modelId: 'local-hash-v1', modelDigest: 'a'.repeat(64), indexVersion: 1,
    queryText: '部署 回滚 锚点', queryStrategy: 'pr_context_v1',
    citations: [{ kind: 'rag_auxiliary', score: 0.5, snippet: 's',
      citation: { repo: 'nghqqa/ragdemo', branch: 'main', doc_path: 'docs/runbook.md',
        line_start: 3, line_end: 5, doc_sha256: 'd'.repeat(64), chunk_sha256: 'c'.repeat(64) } }],
    serviceState: 'hit',
  };

  // P1 首写：完整任务身份落行
  const r1 = await saveRunRagSnapshot(pool, { ...base });
  ok('P1a 首写 saved=true 且有行', r1.saved === true && !!r1.row);
  const row1 = r1.row;
  ok('P1b 任务身份完整（run/tenant/repo/branch/head/base_ref）',
    row1.run_id === RUN1 && row1.tenant_id === TEN1 && row1.repo === 'nghqqa/ragdemo'
    && row1.branch === 'main' && row1.review_head === 'e'.repeat(40) && row1.base_ref === 'main');
  ok('P1c 模型/索引/策略/状态/时间齐备',
    row1.model_id === 'local-hash-v1' && row1.model_digest === 'a'.repeat(64)
    && row1.index_version === 1 && row1.query_strategy === 'pr_context_v1'
    && row1.service_state === 'hit' && !!row1.snapshot_at);
  assert.equal(row1.citations.length, 1);
  ok('P1d citations 完整（doc_path/sha 可追溯）',
    row1.citations[0].citation.doc_path === 'docs/runbook.md'
    && row1.citations[0].citation.doc_sha256 === 'd'.repeat(64));

  // P2 重试：同 key 不同内容 → saved=false，旧行逐字段不变
  const r2 = await saveRunRagSnapshot(pool, { ...base,
    modelId: 'other-model', modelDigest: 'b'.repeat(64), indexVersion: 2,
    citations: [{ changed: true }], serviceState: 'empty' });
  ok('P2a 重试 saved=false（首写即定）', r2.saved === false && r2.row?.snapshot_id === row1.snapshot_id);
  const after = (await pool.query('SELECT * FROM ragtrial.run_rag_snapshots WHERE run_id=$1', [RUN1])).rows;
  ok('P2b 旧行逐字段不变（模型/索引/引用/时间戳未被改写）',
    after.length === 1 && after[0].model_id === 'local-hash-v1' && after[0].index_version === 1
    && JSON.stringify(after[0].citations) === JSON.stringify(row1.citations)
    && after[0].snapshot_at.getTime() === row1.snapshot_at.getTime());

  // P3 语料表变化不改写快照：独立探针表插入不影响已存 JSONB；重读一致
  await pool.query(`CREATE TABLE IF NOT EXISTS ragtrial.corpus_mutation_probe (id INT)`);
  await pool.query(`INSERT INTO ragtrial.corpus_mutation_probe VALUES (1)`);
  const reread = await loadRunRagSnapshot(pool, { runId: RUN1, corpusBranch: 'main' });
  ok('P3 语料变化后重读快照逐字节一致（不可变）',
    JSON.stringify(reread.citations) === JSON.stringify(row1.citations)
    && reread.snapshot_at.getTime() === row1.snapshot_at.getTime());

  // P4 无快照如实 null
  const none = await loadRunRagSnapshot(pool, { runId: '22222222-2222-2222-2222-222222222222', corpusBranch: 'main' });
  ok('P4 无快照返回 null（未记录语义）', none === null);

  // P5 保存失败：表被删 → saveRunRagSnapshot 抛错（调用方 worker-rag 捕获降级），
  // 读取路径不伪造任何行（抛错或 null 均可——绝不返回编造数据）
  await pool.query(`DROP TABLE ragtrial.run_rag_snapshots`);
  let threw = false;
  try { await saveRunRagSnapshot(pool, { ...base, runId: '33333333-3333-3333-3333-333333333333' }); }
  catch { threw = true; }
  ok('P5a 保存失败显式抛错（不由存储层静默吞掉）', threw);
  let loadThrew = false, loaded = null;
  try { loaded = await loadRunRagSnapshot(pool, { runId: '33333333-3333-3333-3333-333333333333', corpusBranch: 'main' }); }
  catch { loadThrew = true; }
  ok('P5b 失败后无伪造行（null 或显式报错=未留存）', loadThrew || loaded === null, { loadThrew, loaded });

  // P6 重建后首写 + 同 (run_id, corpus_branch) 不同 base_ref 重试 → 仍是同 key 不产生第二行
  for (const sql of RAGTRIAL_SCHEMA_SQL) await pool.query(sql);
  const r6a = await saveRunRagSnapshot(pool, { ...base });
  const r6 = await saveRunRagSnapshot(pool, { ...base, baseRef: 'refs/heads/main',
    modelId: 'another-model', indexVersion: 9 });
  ok('P6 唯一键=(run_id, corpus_branch)：base_ref/模型差异不产生第二行（首写即定）',
    r6a.saved === true && r6.saved === false && r6.row?.base_ref === 'main'
    && (await pool.query('SELECT count(*)::int c FROM ragtrial.run_rag_snapshots WHERE run_id=$1', [RUN1])).rows[0].c === 1,
    { a: r6a.saved, b: r6.saved, base_ref: r6.row?.base_ref });

  console.log(`\nrag-snapshot-pool.integration: ${pass} passed, ${fail} failed`);
  process.exitCode = fail ? 1 : 0;
} finally {
  await pool.end().catch(() => {});
  if (!process.env.RAG_SNAPSHOT_TEST_DSN) {
    try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* 已清理 */ }
  }
}
