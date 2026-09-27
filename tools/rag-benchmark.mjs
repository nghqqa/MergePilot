#!/usr/bin/env node
// tools/rag-benchmark.mjs — 双 provider 检索质量基准（PRODUCTION_RAG_READINESS）。
//
// 对同一英文语料 + QA 集，分别用 local-hash-v1（确定性回归基线）与
// bge-large-en-v1.5（真实语义，attested sidecar）跑完整检索链路（走 console
// HTTP API——与生产同代码路径，不旁路），计算：
//   Recall@1/@5、MRR@10、NDCG@10、引用命中率、空结果准确率（fluent-but-absent
//   与零重叠乱词两类）、查询延迟 P50/P95。
// 产物：evidence/rag-prod/<ts>/benchmark.json（本脚本）+ 服务端 query_log/eval_runs。
//
// 用法：node tools/rag-benchmark.mjs --console http://127.0.0.1:48470 \
//        --sidecar http://127.0.0.1:48471 --user u --password p
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const argOf = (n) => { const i = args.indexOf(`--${n}`); return i >= 0 ? args[i + 1] : null; };
const CONSOLE = argOf('console') ?? 'http://127.0.0.1:48470';
const SIDECAR = argOf('sidecar') ?? 'http://127.0.0.1:48471';
const USER = argOf('user') ?? 'ragprod-operator';
const PASSWORD = argOf('password') ?? 'ragprod-local-only-console';
const REPO = 'nghqqa/mergepilot', BRANCH = 'feat/rag-prod-readiness';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CORPUS_DIR = path.resolve(HERE, '..', 'deploy', 'rag-prod', 'corpus');
const EVID = path.resolve(HERE, '..', 'evidence', 'rag-prod', new Date().toISOString().replace(/[:.]/g, '-'));
fs.mkdirSync(EVID, { recursive: true });

let cookie = null;
async function call(method, p, body, base = CONSOLE) {
  const res = await fetch(base + p, {
    method,
    headers: { cookie: cookie ?? '', 'content-type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}

const percentile = (arr, p) => {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(p * s.length))];
};
const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0);

// —— NDCG@10（单相关文档：rel=1@rank r → NDCG = 1/log2(r+1)；未命中=0）
const ndcg10 = (rank) => (rank > 0 && rank <= 10 ? 1 / Math.log2(rank + 1) : 0);
const citationComplete = (c) => Boolean(c?.doc_path && c?.line_start >= 1 && c?.line_end >= c?.line_start
  && c?.doc_sha256?.length === 64 && c?.chunk_sha256?.length === 64 && c?.model_digest?.length === 64
  && Number.isInteger(c?.index_version));

async function benchModel(modelId, qaSet) {
  const perQ = [];
  const latencies = [];
  let cited = 0, returned = 0;
  for (const item of qaSet.qa) {
    const t0 = Date.now();
    const r = await call('POST', '/api/rag-trial/query', { q: item.q, repo: REPO, branch: BRANCH, k: 10, model_id: modelId });
    latencies.push(Date.now() - t0);
    const results = r.json?.results ?? [];
    for (const h of results) { returned++; if (citationComplete(h.citation)) cited++; }
    const rank = results.findIndex((h) => h.citation?.doc_path === item.expect_doc) + 1;
    perQ.push({ q: item.q, expect_doc: item.expect_doc, state: r.json?.service_state,
      rank: rank || null, top3: results.slice(0, 3).map((h) => [h.citation.doc_path, h.score]) });
  }
  // 空结果准确率：应答为 empty 视为正确（服务必须诚实 empty，不得低分冒充 hit）
  const emptyDetail = [];
  let emptyOk = 0;
  for (const item of qaSet.empty_queries) {
    const r = await call('POST', '/api/rag-trial/query', { q: item.q, repo: REPO, branch: BRANCH, k: 5, model_id: modelId });
    const okEmpty = r.json?.service_state === 'empty';
    if (okEmpty) emptyOk++;
    emptyDetail.push({ q: item.q, note: item.note, state: r.json?.service_state,
      scores: (r.json?.results ?? []).slice(0, 3).map((h) => h.score) });
  }
  const ranks = perQ.map((x) => x.rank).filter((r) => r > 0);
  return {
    model_id: modelId,
    total: perQ.length,
    recall_at_1: +(ranks.filter((r) => r === 1).length / perQ.length).toFixed(4),
    recall_at_5: +(ranks.filter((r) => r <= 5).length / perQ.length).toFixed(4),
    recall_at_10: +(ranks.length / perQ.length).toFixed(4),
    mrr_at_10: +(mean(ranks.map((r) => 1 / r))).toFixed(4),
    ndcg_at_10: +(mean(perQ.map((x) => ndcg10(x.rank)))).toFixed(4),
    citation_hit_rate: returned ? +(cited / returned).toFixed(4) : null,
    empty_accuracy: +(emptyOk / qaSet.empty_queries.length).toFixed(4),
    latency_ms: { p50: percentile(latencies, 0.5), p95: percentile(latencies, 0.95), mean: Math.round(mean(latencies)) },
    per_query: perQ,
    empty_detail: emptyDetail,
  };
}

async function main() {
  // 登录
  const login = await fetch(CONSOLE + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: USER, password: PASSWORD }),
  });
  if (login.status !== 200) throw new Error(`login HTTP ${login.status}`);
  cookie = (login.headers.get('set-cookie') || '').split(';')[0];

  // sidecar manifest（链式 custody：sidecar /manifest 原始字节）
  const mres = await fetch(`${SIDECAR}/manifest`);
  const manifestText = await mres.text();
  const manifest = JSON.parse(manifestText);
  const manifestSha = (await import('node:crypto')).createHash('sha256').update(manifestText).digest('hex');
  console.log(`[bench] sidecar manifest sha256=${manifestSha} model=${manifest.model_id} dims=${manifest.dims}`);

  // 注册语义模型（models 表绑定 dims+manifest）
  const reg = await call('POST', '/api/rag-trial/models', {
    model_id: manifest.model_id, dims: manifest.dims, manifest,
  });
  console.log('[bench] model register:', JSON.stringify(reg.json).slice(0, 160));

  // 语料（host 读取，docs 内联——与 corpus_dir 等价但便于复现）
  const docs = fs.readdirSync(CORPUS_DIR).filter((f) => f.endsWith('.md')).sort()
    .map((f) => ({ path: f, text: fs.readFileSync(path.join(CORPUS_DIR, f), 'utf8') }));
  console.log(`[bench] corpus: ${docs.length} docs`);

  // 双模型摄取（幂等）
  for (const modelId of ['local-hash-v1', manifest.model_id]) {
    const ing = await call('POST', '/api/rag-trial/ingest', { repo: REPO, branch: BRANCH, docs, model_id: modelId });
    const acts = {};
    for (const r of ing.json?.report ?? []) acts[r.action] = (acts[r.action] ?? 0) + 1;
    console.log(`[bench] ingest ${modelId}: ${JSON.stringify(acts)} err=${ing.json?.error ?? ''}`);
  }

  const qaSet = JSON.parse(fs.readFileSync(path.join(CORPUS_DIR, 'qa-set.json'), 'utf8'));
  const hashRes = await benchModel('local-hash-v1', qaSet);
  const semRes = await benchModel(manifest.model_id, qaSet);

  const summary = {
    generated_at: new Date().toISOString(),
    corpus: { docs: docs.length, qa: qaSet.qa.length, empty_queries: qaSet.empty_queries.length },
    manifest: { sha256: manifestSha, model_id: manifest.model_id, dims: manifest.dims, runtime: manifest.runtime },
    providers: { baseline_deterministic: hashRes, real_semantic: semRes },
  };
  fs.writeFileSync(path.join(EVID, 'benchmark.json'), JSON.stringify(summary, null, 2));
  console.log('\n=== benchmark summary ===');
  for (const [k, v] of Object.entries({ local_hash_v1: hashRes, [manifest.model_id]: semRes })) {
    console.log(`${k}: R@1=${v.recall_at_1} R@5=${v.recall_at_5} MRR@10=${v.mrr_at_10} NDCG@10=${v.ndcg_at_10} cite=${v.citation_hit_rate} empty_acc=${v.empty_accuracy} P50=${v.latency_ms.p50}ms P95=${v.latency_ms.p95}ms`);
  }
  console.log(`evidence: ${EVID}`);
}

main().catch((e) => { console.error('FATAL', e); process.exit(1); });
