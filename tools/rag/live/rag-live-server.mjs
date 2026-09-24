#!/usr/bin/env node
// rag-live-server.mjs — LIVE-RUN RAG service for the AgentLoop traced runs (2026-09-17).
// Same wire contract as the demo platform's /api/rag/* endpoints, but backed by a
// KNOWLEDGE-ONLY corpus (organization security standards; no case findings, no repo
// facts) so agent reviewers keep full independence. Runs on the host; worker
// containers reach it at http://host.docker.internal:4174 (P14-verified path).
//
//   GET  /health
//   GET  /api/rag/search?q=<query>&k=<topK>     -> {query_hash, top_k, retrieval_mode,
//                                                   retrieval_strategy, data_mode, results[], latency_ms}
//   POST /api/rag/toolspan-audit                -> append-audit to rag-tool-spans.jsonl (evidence)
//   GET  /api/rag/toolspans                     -> recent audit tail
//
// Zero dependencies (node >= 18). Audit file: only hashes/ids/counts — no query text.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.RAG_LIVE_PORT || 4174);
const CORPUS = process.env.RAG_LIVE_CORPUS || path.join(__dirname, 'rag-live-corpus.json');
const AUDIT = process.env.RAG_LIVE_AUDIT || path.join(__dirname, 'rag-tool-spans.jsonl');

const corpus = JSON.parse(fs.readFileSync(CORPUS, 'utf8'));
const CORPUS_FILE_SHA256 = crypto.createHash('sha256')
  .update(fs.readFileSync(CORPUS)).digest('hex');   // 原始字节哈希:部署核对用
const chunks = [];
for (const doc of corpus.documents) {
  for (const ch of doc.chunks) {
    chunks.push({
      document_id: doc.document_id,
      chunk_id: ch.chunk_id,
      text: ch.text,
      source_ref: ch.source_ref,
    });
  }
}
const sha16 = (s) => crypto.createHash('sha256').update(String(s)).digest('hex').slice(0, 16);
const tokenize = (s) => {
  const out = [];
  const lower = String(s).toLowerCase();
  for (const m of lower.matchAll(/[a-z0-9_]+|[\u4e00-\u9fff]/g)) out.push(m[0]);
  // CJK bigrams for better zh matching
  const zh = lower.match(/[\u4e00-\u9fff]{2,}/g) || [];
  for (const w of zh) for (let i = 0; i + 1 < w.length; i++) out.push(w.slice(i, i + 2));
  return out;
};
const DF = new Map();
for (const c of chunks) for (const t of new Set(tokenize(c.text))) DF.set(t, (DF.get(t) || 0) + 1);

function score(query) {
  const qts = tokenize(query);
  if (!qts.length) return [];
  const tf = new Map();
  for (const t of qts) tf.set(t, (tf.get(t) || 0) + 1);
  const N = chunks.length;
  return chunks
    .map((c) => {
      const ctoks = tokenize(c.text);
      const clen = ctoks.length || 1;
      let s = 0;
      for (const [t, f] of tf) {
        const body = ctoks.filter((x) => x === t).length;
        if (!body) continue;
        const idf = Math.log(1 + (N - (DF.get(t) || 0) + 0.5) / ((DF.get(t) || 0) + 0.5));
        s += idf * ((body * 2.2) / (body + 1.2 * (0.25 + 0.75 * (clen / 220))));
      }
      return { c, score: s };
    })
    .filter((x) => x.score > 0)
    .sort((a, b) => b.score - a.score);
}

function retrieve(query, topK, runId) {
  const t0 = Date.now();
  const query_hash = sha16(query);
  const k = Math.max(1, Math.min(Number(topK) || 3, 10));
  const results = score(query).slice(0, k).map(({ c, score: s }) => ({
    document_id: c.document_id,
    chunk_id: c.chunk_id,
    score: Number(s.toFixed(4)),
    source_ref: c.source_ref,
    retrieval_mode: corpus.retrieval_mode,
    data_mode: corpus.data_mode,
  }));
  const latency_ms = Date.now() - t0;
  const auditRec = {
    tool: 'rag.retrieve', arguments_hash: query_hash,
    result_status: results.length ? 'OK' : 'EMPTY', document_count: results.length,
    latency_ms, data_mode: corpus.data_mode, source_refs: results.map((r) => r.source_ref),
    corpus_file_sha256: CORPUS_FILE_SHA256,
  };
  if (runId) auditRec.run_id = String(runId).slice(0, 80);  // 审计关联 run(可选透传)
  writeAudit(auditRec);
  return {
    query_hash, top_k: results.length, retrieval_mode: corpus.retrieval_mode,
    retrieval_strategy: corpus.strategy_id, data_mode: corpus.data_mode, results, latency_ms,
  };
}

function writeAudit(rec) {
  try {
    fs.appendFileSync(AUDIT, JSON.stringify({ ...rec, ts: new Date().toISOString() }) + '\n');
  } catch (e) {
    console.error('audit append failed:', e.message);
  }
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const send = (status, obj) => {
    const body = JSON.stringify(obj, null, 2);
    res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
    res.end(body);
  };
  try {
    if (url.pathname === '/health') return send(200, { ok: true, service: 'rag-live', corpus: path.basename(CORPUS), chunks: chunks.length, data_mode: corpus.data_mode, corpus_file_sha256: CORPUS_FILE_SHA256 });
    if (url.pathname === '/api/rag/search' && req.method === 'GET') {
      const q = (url.searchParams.get('q') || '').trim();
      if (!q) return send(400, { error: 'EMPTY_QUERY' });
      return send(200, retrieve(q, url.searchParams.get('k'),
                               url.searchParams.get('run_id')));
    }
    if (url.pathname === '/api/rag/toolspan-audit' && req.method === 'POST') {
      const chunks2 = [];
      req.on('data', (c) => chunks2.push(c));
      return req.on('end', () => {
        try {
          const body = JSON.parse(Buffer.concat(chunks2).toString('utf8') || '{}');
          if (typeof body !== 'object' || !body.tool) throw new Error('invalid record');
          writeAudit(body);
          send(200, { ok: true, data_mode: corpus.data_mode });
        } catch (e) { send(400, { ok: false, error: String(e.message || e) }); }
      });
    }
    if (url.pathname === '/api/rag/toolspans' && req.method === 'GET') {
      const lines = fs.existsSync(AUDIT) ? fs.readFileSync(AUDIT, 'utf8').split('\n').filter(Boolean) : [];
      return send(200, { data_mode: corpus.data_mode, recent: lines.slice(-12).map((l) => JSON.parse(l)) });
    }
    send(404, { error: 'NOT_FOUND' });
  } catch (e) {
    send(500, { error: String(e.message || e) });
  }
});

server.listen(PORT, '0.0.0.0', () => console.log(`rag-live listening on 0.0.0.0:${PORT}, corpus=${CORPUS} chunks=${chunks.length}, audit=${AUDIT}`));
