#!/usr/bin/env node
// console/backend/test/ragtrial-prod.integration.mjs — PRODUCTION_RAG_READINESS 集成回归。
// 运行：node console/backend/test/ragtrial-prod.integration.mjs
//   依赖：docker（pgvector/pgvector:pg16 本地镜像）。
// 覆盖：jobs 表/幂等/重试/死信/复活/陈旧重占（崩溃恢复）；维度不匹配 fail-closed；
//       attested sidecar 全链（mock sidecar）+ manifest 漂移 fail-closed；
//       worker（--once）真实执行与确定性崩溃回收。
import { execFileSync, spawn } from 'node:child_process';
import http from 'node:http';
import path from 'node:path';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..', '..');
const { createRagTrialStore, RagTrialError } = await import('../lib/ragtrial/store.mjs');
const { createJobQueue } = await import('../lib/ragtrial/queue.mjs');

let pass = 0, fail = 0;
function ok(name, cond, detail) {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 300) : ''}`); }
}

const CTR = `ragprod-it-${crypto.randomBytes(4).toString('hex')}`;
const PORT = 15600 + Math.floor(Math.random() * 200);

async function bootPg() {
  execFileSync('docker', ['run', '-d', '--name', CTR,
    '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=ragprod',
    '-p', `127.0.0.1:${PORT}:5432`, 'pgvector/pgvector:pg16'], { stdio: 'pipe' });
  const dsn = `postgres://postgres:x@127.0.0.1:${PORT}/ragprod`;
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); return { dsn, pool: p }; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}

// ── mock attested sidecar：确定性 1024 维向量 + manifest ──
function mockSidecar({ manifestOverride = null, dims = 1024 } = {}) {
  const manifest = manifestOverride ?? {
    model_id: 'mock-semantic-v1', dims, pooling: 'cls_l2', distance: 'cosine',
    runtime: 'numpy-bert-v1', files: { 'model.safetensors': 'a'.repeat(64) },
  };
  const body = JSON.stringify(manifest);
  const manifestSha = crypto.createHash('sha256').update(body).digest('hex');
  const srv = http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/manifest') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(body); return;
    }
    if (req.method === 'GET' && req.url === '/health') { res.writeHead(200); res.end('{"ok":true}'); return; }
    if (req.method === 'POST' && req.url === '/embed') {
      let buf = '';
      req.on('data', (c) => { buf += c; });
      req.on('end', () => {
        const { input } = JSON.parse(buf || '{}');
        const data = (input ?? []).map((t, i) => {
          const v = new Array(dims).fill(0);
          // 文本 token → 确定性哈希投影（同 token 同方向；不同文本可分）
          for (const tok of String(t).toLowerCase().split(/[^a-z0-9]+/).filter(Boolean)) {
            const h = crypto.createHash('sha256').update(tok).digest();
            v[h[0] % dims] += ((h[1] & 1) ? 1 : -1) * (1 + (h[2] % 8) / 8);
          }
          const n = Math.sqrt(v.reduce((s, x) => s + x * x, 0)) || 1;
          return { embedding: v.map((x) => +(x / n).toFixed(8)) };
        });
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ data }));
      });
      return;
    }
    res.writeHead(404); res.end();
  });
  return new Promise((r) => srv.listen(0, '127.0.0.1', () => r({ srv, port: srv.address().port, manifest, manifestSha })));
}

function envWith(dsn, extra) {
  return { ...process.env, CONSOLE_PG_DSN: dsn, RAGTRIAL_EMBED_ENDPOINT: null, ...extra };
}

let sidecar, pgres;
try {
  sidecar = await mockSidecar();
  pgres = await bootPg();
  const { dsn, pool } = pgres;
  const q = (text, params) => pool.query(text, params);

  const store = await createRagTrialStore({ pool, env: envWith(dsn, { RAGTRIAL_EMBED_ENDPOINT: undefined }) });
  await store.initSchema();
  ok('schema 扩展：models.dims / jobs 表就绪',
    (await q(`SELECT column_name FROM information_schema.columns WHERE table_name='models' AND column_name='dims'`)).rows.length === 1
    && (await q(`SELECT to_regclass('ragtrial.jobs')`)).rows[0].to_regclass !== null);

  const queue = createJobQueue({ pool });
  const REPO = 'nghqqa/mergepilot', BR = 'feat/rag-prod-readiness';
  const doc = (p, t) => ({ path: p, text: t });

  // ── 队列：幂等入队 ──
  const j1 = await queue.enqueue({ kind: 'ingest_doc', repo: REPO, branch: BR, doc_path: 'docs/a.md',
    content_sha256: 'a'.repeat(64), model_id: 'local-hash-v1', payload: { text: 'hello world' } });
  const j2 = await queue.enqueue({ kind: 'ingest_doc', repo: REPO, branch: BR, doc_path: 'docs/a.md',
    content_sha256: 'a'.repeat(64), model_id: 'local-hash-v1', payload: { text: 'changed but same dedupe key parts' } });
  ok('入队幂等：同 dedupe_key 返回既有 job', j1.created === true && j2.created === false && j1.job_id === j2.job_id);

  // ── 队列：认领/完成 ──
  const claimed = await queue.claim({ worker: 'w1' });
  ok('认领 queued 任务（attempts=1）', claimed.length === 1 && claimed[0].job_id === j1.job_id && claimed[0].attempts === 1);
  const c1 = await queue.complete(j1.job_id, { worker: 'w1', result: { ingested: 1 } });
  ok('完成任务落库', c1.ok === true);
  const again = await queue.claim({ worker: 'w1' });
  ok('done 任务不再认领', again.length === 0);

  // ── 队列：失败重试 → 死信 → 复活 ──
  const j3 = await queue.enqueue({ kind: 'ingest_doc', repo: REPO, branch: BR, doc_path: 'docs/b.md',
    content_sha256: 'b'.repeat(64), model_id: 'local-hash-v1', payload: {}, max_attempts: 2 });
  await queue.claim({ worker: 'w1' });
  await q(`UPDATE ragtrial.jobs SET next_run_at=now() WHERE job_id=$1`, [j3.job_id]);
  const f1 = await queue.fail(j3.job_id, new Error('minio down'), { worker: 'w1' });
  ok('失败一次 → queued 重试（指数退避）', f1.state === 'queued' && f1.attempts === 1);
  await q(`UPDATE ragtrial.jobs SET next_run_at=now() WHERE job_id=$1`, [j3.job_id]);
  await queue.claim({ worker: 'w2' });
  const f2 = await queue.fail(j3.job_id, new Error('minio still down'), { worker: 'w2' });
  ok('attempts 用尽 → dead 死信', f2.state === 'dead' && f2.attempts === 2);
  const rq = await queue.requeueDead(j3.job_id, { actor: 'operator' });
  const j3row = (await q(`SELECT state FROM ragtrial.jobs WHERE job_id=$1`, [j3.job_id])).rows[0];
  ok('死信复活 → queued（attempts 清零）', rq.ok === true && j3row.state === 'queued');

  // ── 队列：陈旧重占（崩溃恢复，确定性模拟）──
  const j4 = await queue.enqueue({ kind: 'ingest_doc', repo: REPO, branch: BR, doc_path: 'docs/c.md',
    content_sha256: 'c'.repeat(64), model_id: 'local-hash-v1', payload: { text: 'crash recovery probe' } });
  await queue.claim({ worker: 'crashed-worker' }); // 认领后"崩溃"：不再心跳
  await q(`UPDATE ragtrial.jobs SET heartbeat_at = now() - interval '10 minutes' WHERE job_id=$1`, [j4.job_id]);
  const reclaimed = await queue.claim({ worker: 'rescuer' });
  ok('陈旧 running 被重占（JOB_RECLAIM 路径）',
    reclaimed.length === 1 && reclaimed[0].job_id === j4.job_id && reclaimed[0].locked_by === 'rescuer' && reclaimed[0].was_stale === true);
  await queue.complete(j4.job_id, { worker: 'rescuer' });
  const reclaimAudit = (await q(`SELECT count(*)::int n FROM ragtrial.audit_events WHERE kind='JOB_RECLAIM'`)).rows[0].n;
  const jobAudits = (await q(`SELECT count(*)::int n FROM ragtrial.audit_events WHERE kind LIKE 'JOB_%'`)).rows[0].n;
  ok('任务生命周期审计（ENQUEUE/START/DONE/RETRY/DEAD/REQUEUE/RECLAIM）', reclaimAudit >= 1 && jobAudits >= 8, { jobAudits, reclaimAudit });

  // ── 维度不匹配 fail-closed ──
  // 注册 1024 维语义模型但 provider 是 local（256 维输出）→ 必须拒绝
  const reg = await store.registerModel({ model_id: 'dim-probe', dims: 1024, provider: 'probe' }, 'remote', { dims: 1024, silent: true });
  const dimErr = await store.ingestDocuments({
    docs: [doc('docs/x.md', 'dimension mismatch probe')], repo: REPO, branch: BR, modelId: 'dim-probe',
  }).catch((e) => e);
  ok('维度不匹配 → dimension_mismatch fail-closed（422）',
    dimErr instanceof RagTrialError && dimErr.kind === 'dimension_mismatch' && dimErr.status === 422);

  // ── attested sidecar 全链（mock）──
  const embed = `http://127.0.0.1:${sidecar.port}/embed`;
  const attestedEnv = envWith(dsn, {
    RAGTRIAL_EMBED_ENDPOINT: embed,
    RAGTRIAL_EMBED_MODEL_ID: 'mock-semantic-v1',
    RAGTRIAL_EMBED_DIMS: '1024',
    RAGTRIAL_EMBED_TIMEOUT_MS: '4000',
    RAGTRIAL_EMBED_EXPECTED_MANIFEST: sidecar.manifestSha,
  });
  const sstore = await createRagTrialStore({ pool, env: attestedEnv });
  await sstore.registerModel({ model_id: 'mock-semantic-v1', dims: 1024, provider: 'remote-attested',
    manifest_sha256: sidecar.manifestSha }, 'remote', { dims: 1024, manifest: sidecar.manifest, silent: true });
  const ing = await sstore.ingestDocuments({
    docs: [doc('sem/one.md', 'semantic provider probe with several english tokens about rollback anchors and canary batches')],
    repo: REPO, branch: BR, modelId: 'mock-semantic-v1',
  });
  const semRows = (await q(`SELECT count(*)::int n FROM ragtrial.chunks_semantic`)).rows[0].n;
  ok('attested 语义摄取落 chunks_semantic（1024 维表）', ing.ok === true && semRows >= 1);
  const semSearch = await sstore.search({ q: 'rollback anchors canary batches', repo: REPO, branch: BR, k: 3, modelId: 'mock-semantic-v1' });
  ok('语义检索命中（引用链完整）', semSearch.service_state === 'hit'
    && semSearch.results.every((h) => h.citation.model_id === 'mock-semantic-v1' && h.citation.doc_path === 'sem/one.md'));

  // ── manifest 漂移 → fail-closed（BLOCKED，非降级）──
  const driftedEnv = envWith(dsn, {
    RAGTRIAL_EMBED_ENDPOINT: embed,
    RAGTRIAL_EMBED_DIMS: '1024',
    RAGTRIAL_EMBED_EXPECTED_MANIFEST: '0'.repeat(64), // 错误 pin
  });
  const dstore = await createRagTrialStore({ pool, env: driftedEnv });
  const blocked = await dstore.search({ q: 'rollback anchors', repo: REPO, branch: BR, k: 3, modelId: 'mock-semantic-v1' }).catch((e) => e);
  ok('manifest 摘要不匹配 → fail-closed（model_attestation_failed，绝不降级空结果）',
    blocked instanceof RagTrialError && blocked.kind === 'model_attestation_failed');

  // ── provider 维度不符（manifest 说 1024，返回 768）→ blocked ──
  const wrongDimsSidecar = await mockSidecar({ dims: 768, manifestOverride: {
    model_id: 'mock-semantic-v1', dims: 1024, pooling: 'cls_l2',
    files: [{ name: 'weights.bin', sha256: 'a'.repeat(64), bytes: 16 }], runtime: 'x',
  } });
  // 同一 manifest 语义但 embed 返回 768 维
  const wdEnv = envWith(dsn, {
    RAGTRIAL_EMBED_ENDPOINT: `http://127.0.0.1:${wrongDimsSidecar.port}/embed`,
    RAGTRIAL_EMBED_DIMS: '1024',
    RAGTRIAL_EMBED_EXPECTED_MANIFEST: wrongDimsSidecar.manifestSha,
  });
  const wstore = await createRagTrialStore({ pool, env: wdEnv });
  const wdErr = await wstore.search({ q: 'probe', repo: REPO, branch: BR, k: 3, modelId: 'mock-semantic-v1' }).catch((e) => e);
  ok('provider 输出维度不匹配 → dimension_mismatch fail-closed',
    wdErr instanceof RagTrialError && wdErr.kind === 'dimension_mismatch');
  wrongDimsSidecar.srv.close();

  // ── worker（--once）真实执行：入队真实 local 任务并由子进程 worker 完成 ──
  const jw = await queue.enqueue({ kind: 'ingest_doc', repo: REPO, branch: BR, doc_path: 'docs/worker.md',
    content_sha256: 'd'.repeat(64), model_id: 'local-hash-v1', payload: { text: 'worker execution probe with distinctive tokens' } });
  const workerOut = await new Promise((resolve) => {
    const p = spawn(process.execPath, [path.join(REPO_ROOT, 'tools', 'rag-worker.mjs'),
      '--dsn', dsn, '--once', '--worker', 'it-worker'], { cwd: REPO_ROOT, encoding: 'utf8' });
    let out = '';
    p.stdout.on('data', (c) => { out += c; });
    p.stderr.on('data', (c) => { out += c; });
    p.on('exit', (code) => resolve({ code, out }));
  });
  const jwRow = (await q(`SELECT state, result FROM ragtrial.jobs WHERE job_id=$1`, [jw.job_id])).rows[0];
  const workerChunks = (await q(`SELECT count(*)::int n FROM ragtrial.chunks WHERE doc_path='docs/worker.md'`)).rows[0].n;
  ok('worker 子进程执行任务 → done + 索引落库', workerOut.code === 0 && jwRow.state === 'done' && workerChunks >= 1, workerOut.out.slice(-200));

  // ── 队列指标 ──
  const stats = await queue.stats();
  ok('队列指标：by_state 含 done/dead + 死信列表',
    stats.by_state.done >= 2 && Array.isArray(stats.dead_letters));

} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  if (sidecar) sidecar.srv.close();
  if (pgres) await pgres.pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nragtrial-prod.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
