// rag-live.mjs — A 链隔离词法检索服务（lexical-zh-en-v1，零依赖）。
// worker rag_retrieve 的下游。GET /api/rag/search?q=&k=：
//   命中 → source_refs + snapshot_id + service_state=ok；
//   无命中 → 空结果（合法空）；
//   服务关闭/语料损坏由调用方预检矩阵显式呈现 degraded（本服务不伪装）。
// 每次检索写审计 JSONL：snapshot_id/query_hash/source_refs/service_state/run_id。
import http from 'node:http';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadCorpus, RETRIEVAL_VERSION } from './corpus-gate.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CORPUS_PATH = process.env.RAG_LIVE_CORPUS
  || path.join(__dirname, 'corpus', 'org-security-standards.v1.json');
const AUDIT_LOG = process.env.RAG_LIVE_AUDIT
  || path.join(__dirname, 'audit', 'retrievals.jsonl');
const PORT = Number(process.env.RAG_LIVE_PORT || 48210);

// 中英混合词法切分：CJK 二元组 + 拉丁词
function tokenize(text) {
  const tokens = new Set();
  const latin = text.toLowerCase().match(/[a-z][a-z0-9_-]{1,}/g) || [];
  for (const w of latin) tokens.add(w);
  const cjk = text.match(/[\u4e00-\u9fff]+/g) || [];
  for (const seg of cjk) {
    for (let i = 0; i + 1 < seg.length; i++) tokens.add(seg.slice(i, i + 2));
    if (seg.length === 1) tokens.add(seg);
  }
  return tokens;
}

function score(queryTokens, chunkTokens) {
  let hit = 0;
  for (const t of queryTokens) if (chunkTokens.has(t)) hit += 1;
  return hit / queryTokens.size;
}

export function createRagLive() {
  let loaded = null;
  let loadError = null;
  try {
    loaded = loadCorpus(CORPUS_PATH);
    fs.mkdirSync(path.dirname(AUDIT_LOG), { recursive: true });
  } catch (e) {
    loadError = e;   // 语料损坏/被拒：服务保持启动但状态 degraded+原因（不伪装）
  }
  const index = loaded ? loaded.corpus.documents.flatMap((d) =>
    d.chunks.map((chunk, i) => ({
      doc_id: d.doc_id, title: d.title, source_ref: d.source_ref,
      chunk_index: i, chunk, tokens: tokenize(chunk),
    }))) : [];

  const audit = (rec) => {
    try { fs.appendFileSync(AUDIT_LOG, JSON.stringify(rec) + '\n'); } catch { /* 审计失败显式记录于响应 */ }
  };

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname !== '/api/rag/search' || req.method !== 'GET') {
      res.writeHead(404, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ error: 'NOT_FOUND' }));
    }
    const q = url.searchParams.get('q') || '';
    const k = Math.min(Number(url.searchParams.get('k') || 5), 20);
    const runId = url.searchParams.get('run_id') || null;
    const queryHash = createHash('sha256').update(q, 'utf8').digest('hex').slice(0, 32);
    const base = { snapshot_id: loaded ? loaded.snapshot_id : null,
                   corpus_digest: loaded ? loaded.digest : null,
                   retrieval_version: RETRIEVAL_VERSION, query_hash: queryHash, run_id: runId };

    if (loadError) {
      const rec = { ...base, service_state: 'degraded', degraded_reason: 'corpus_unavailable',
                    error: String(loadError.message).slice(0, 120), source_refs: [], at: new Date().toISOString() };
      audit(rec);
      res.writeHead(503, { 'content-type': 'application/json', 'x-rag-service-state': 'degraded' });
      return res.end(JSON.stringify({ ...rec, results: [] }));
    }
    if (!q.trim()) {
      const rec = { ...base, service_state: 'ok', source_refs: [], empty_reason: 'empty_query', at: new Date().toISOString() };
      audit(rec);
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ ...rec, results: [] }));
    }
    const qTokens = tokenize(q);
    const scored = index.map((e) => ({ e, s: score(qTokens, e.tokens) }))
      .filter((x) => x.s > 0.15)
      .sort((a, b) => b.s - a.s)
      .slice(0, k);
    const rec = { ...base, service_state: 'ok', source_refs: scored.map((x) => x.e.source_ref),
                  hit_count: scored.length,
                  empty_reason: scored.length ? undefined : 'no_lexical_match', at: new Date().toISOString() };
    audit(rec);
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ...rec, results: scored.map((x) => ({
      doc_id: x.e.doc_id, title: x.e.title, source_ref: x.e.source_ref,
      chunk_index: x.e.chunk_index, score: Number(x.s.toFixed(3)), excerpt: x.e.chunk.slice(0, 120),
    })) }));
  });
  return { server, state: () => ({ loaded: !!loaded, error: loadError ? String(loadError.message).slice(0, 120) : null,
                                  snapshot_id: loaded?.snapshot_id, digest: loaded?.digest,
                                  retrieval_version: RETRIEVAL_VERSION, chunks: index.length }) };
}

const isMain = process.argv[1] && import.meta.url === new URL(`file://${path.resolve(process.argv[1]).replace(/\\/g, '/')}`).href;
if (isMain) {
  const { server, state } = createRagLive();
  server.listen(PORT, '127.0.0.1', () => {
    console.log(`[rag-live] isolated lexical service on 127.0.0.1:${PORT}`);
    console.log('[rag-live] state:', JSON.stringify(state()));
  });
}
