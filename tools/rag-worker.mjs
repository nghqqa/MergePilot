#!/usr/bin/env node
// tools/rag-worker.mjs — RAG 持久队列 worker（PRODUCTION_RAG_READINESS）。
//
// PG 表 ragtrial.jobs 为唯一事实源：认领（FOR UPDATE SKIP LOCKED + 陈旧重占）、
// 心跳、超时、退避重试、死信全部落库——worker 被 kill -9 或 PG/MinIO 故障后
// 重启，任务不丢、不重复索引（ingest 幂等：同内容 sha → unchanged）。
//
// 运行：node tools/rag-worker.mjs --dsn postgres://... [--once] [--worker name]
//   [--corpus-dir DIR]（payload.corpus 引用时的语料根，越界拒绝）
// env：RAGTRIAL_EMBED_ENDPOINT/RAGTRIAL_EMBED_EXPECTED_MANIFEST/...（语义模型
// 经 attested sidecar；未配置时仅 local-hash-v1 任务可执行，语义任务如实失败重试）。

import { createRequire } from 'node:module';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const argOf = (name) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : null;
};
const DSN = argOf('dsn') ?? process.env.CONSOLE_PG_DSN;
const ONCE = args.includes('--once');
const WORKER = argOf('worker') ?? `rag-worker-${process.pid}`;
const CORPUS_ROOT = path.resolve(argOf('corpus-dir') ?? process.env.RAGTRIAL_CORPUS_DIR ?? '.');
const HEARTBEAT_MS = 15_000;

if (!DSN) { console.error('need --dsn or CONSOLE_PG_DSN'); process.exit(2); }

const require2 = createRequire(path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'console', 'backend', 'test', 'support', 'noop.js'));
const { Pool } = require2('pg');
const { createRagTrialStore, RagTrialError } = await import('../console/backend/lib/ragtrial/store.mjs');
const { createJobQueue } = await import('../console/backend/lib/ragtrial/queue.mjs');

const pool = new Pool({ connectionString: DSN, max: 4 });
pool.on('error', (e) => console.error(`[worker] pool error (kept alive): ${e.message}`));
const store = await createRagTrialStore({ pool });
await store.initSchema();
const queue = createJobQueue({ pool });
console.log(`[worker] ${WORKER} ready (corpus root: ${CORPUS_ROOT})`);

function readDocFromCorpus(rel) {
  const abs = path.resolve(CORPUS_ROOT, rel);
  if (abs !== path.resolve(CORPUS_ROOT) && !abs.startsWith(path.resolve(CORPUS_ROOT) + path.sep)) {
    throw new Error(`corpus path escape: ${rel}`);
  }
  return fs.readFileSync(abs, 'utf8');
}

async function execute(job) {
  const hb = setInterval(() => { queue.heartbeat(job.job_id, WORKER).catch(() => {}); }, HEARTBEAT_MS);
  const timed = new Promise((_, reject) =>
    setTimeout(() => reject(Object.assign(new Error(`job timeout after ${job.timeout_ms}ms`), { kind: 'timeout' })), Number(job.timeout_ms)));
  try {
    const work = (async () => {
      if (job.kind === 'ingest_doc') {
        const p = job.payload ?? {};
        const docs = [];
        if (p.corpus) {
          docs.push({ path: p.corpus, text: readDocFromCorpus(p.corpus) });
        } else if (typeof p.text === 'string' && job.doc_path) {
          docs.push({ path: job.doc_path, text: p.text });
        } else {
          throw new Error('ingest_doc payload 需要 {corpus} 或 {text}+doc_path');
        }
        const r = await store.ingestDocuments({
          docs, repo: job.repo, branch: job.branch,
          actor: `worker:${WORKER}`, modelId: job.model_id,
        });
        return { ingested: r.report?.length, report: r.report?.map((x) => ({ doc_path: x.doc_path, action: x.action, chunks: x.chunks })) };
      }
      if (job.kind === 'delete_doc') {
        const r = await store.deleteDocument({ repo: job.repo, branch: job.branch, doc_path: job.doc_path }, { actor: `worker:${WORKER}` });
        return { deleted: r.chunks_removed };
      }
      throw new Error(`unknown kind ${job.kind}`);
    })();
    const result = await Promise.race([work, timed]);
    await queue.complete(job.job_id, { worker: WORKER, result });
    console.log(`[worker] DONE ${job.job_id} kind=${job.kind} doc=${job.doc_path ?? '-'} attempts=${job.attempts}`);
    return 'done';
  } catch (e) {
    const isTimeout = e?.kind === 'timeout';
    const fr = await queue.fail(job.job_id, e, { worker: WORKER });
    console.error(`[worker] FAIL ${job.job_id} kind=${job.kind} attempts=${job.attempts} -> ${fr.state ?? '?'} ${e.message}`);
    return fr.state ?? 'failed';
  } finally {
    clearInterval(hb);
  }
}

let stopping = false;
process.on('SIGINT', () => { stopping = true; console.log('[worker] SIGINT — finishing current job'); });

// 主循环
for (;;) {
  if (stopping) { console.log('[worker] stopped'); process.exit(0); }
  let jobs;
  try {
    jobs = await queue.claim({ worker: WORKER, limit: ONCE ? 1 : 1 });
  } catch (e) {
    console.error(`[worker] claim error: ${e.message} — retry in 3s`);
    await new Promise((r) => setTimeout(r, 3000));
    if (ONCE) process.exit(1);
    continue;
  }
  if (!jobs.length) {
    if (ONCE) { console.log('[worker] no runnable job (once mode exit 0)'); process.exit(0); }
    await new Promise((r) => setTimeout(r, 1000));
    continue;
  }
  for (const job of jobs) await execute(job);
  if (ONCE) process.exit(0);
}
