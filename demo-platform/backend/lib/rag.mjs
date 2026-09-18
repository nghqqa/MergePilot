// backend/lib/rag.mjs — Phase 15 synthetic RAG minimal loop.
// Honest by construction:
//   - dataset is SYNTHETIC demo documentation (backend/rag-data/dataset.json),
//     never described as enterprise/production data
//   - embedding is a local deterministic hashed bag-of-words vector — labeled as
//     such; not a neural embedding service
//   - retrieval persists query_hash only, never the query text
//   - every retrieval/answer carries data_mode + source_refs; answers without
//     citations are status UNVERIFIED and can never be reported as verified
//   - tool-span audit records (rag.retrieve) contain ONLY the allowed contract
//     fields — no prompt/completion content, no document bodies

import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = path.resolve(__dirname, '..', 'rag-data');
const DATASET_FILE = path.join(DATA_DIR, 'dataset.json');
const TOOL_SPAN_LOG = path.join(DATA_DIR, 'tool-spans.jsonl');

export const RAG_DATA_MODE = 'SYNTHETIC';
export const RETRIEVAL_MODE = 'local-hash-vector-cosine';
const CHUNK_MAX = 420;
const CHUNK_OVERLAP = 60;

// ------------------------------------------------------------ strategies
// A retrieval strategy is a small frozen record of the tokenizer/weighting
// knobs. Every knob is deterministic and offline. `v1` is the historical
// Phase 15 behaviour (kept byte-identical so recorded spans stay
// reproducible); later versions come from the RAG optimisation loop
// (experiments/rag-loop) and must only be promoted after a held-out
// backtest is recorded in evidence.
export const STRATEGIES = Object.freeze({
  'v1-unigram-hash256': Object.freeze({
    id: 'v1-unigram-hash256', dim: 256, cjk_bigram: false, idf: false,
  }),
  // Promoted 2026-09-14 by the offline loop in experiments/rag-loop: picked on
  // the tuning families, then backtested once on the held-out families
  // (evidence/FINALS-RAG-LOOP-20260914/report.json — held-out hit@1 75.0% →
  // 91.7%, MRR 0.861 → 0.958, 1 regression). Set RAG_STRATEGY=v1-unigram-hash256
  // to reproduce the historical behaviour.
  'v2-bigram-idf-hash4096': Object.freeze({
    id: 'v2-bigram-idf-hash4096', dim: 4096, cjk_bigram: true, idf: true,
  }),
});
export const DEFAULT_STRATEGY_ID = 'v2-bigram-idf-hash4096';
export const ACTIVE_STRATEGY = STRATEGIES[process.env.RAG_STRATEGY || DEFAULT_STRATEGY_ID]
  || STRATEGIES[DEFAULT_STRATEGY_ID];
export const EMBEDDING_BACKEND =
  `local-hash-bow-${ACTIVE_STRATEGY.dim} (deterministic, SYNTHETIC, strategy=${ACTIVE_STRATEGY.id})`;

// ---------------------------------------------------------------- ingestion

function sha256(s) {
  return createHash('sha256').update(s, 'utf8').digest('hex');
}

export function splitSentences(text) {
  return text.split(/(?<=[。．.!?！？\n])/).map((s) => s.trim()).filter(Boolean);
}

export function chunkText(text) {
  const sentences = splitSentences(text);
  const chunks = [];
  let cur = '';
  for (const s of sentences) {
    if (cur && (cur + s).length > CHUNK_MAX) {
      chunks.push(cur);
      cur = cur.slice(Math.max(0, cur.length - CHUNK_OVERLAP)) + s;
    } else {
      cur += s;
    }
  }
  if (cur.trim()) chunks.push(cur);
  return chunks;
}

// Hashed bag-of-words vector: deterministic, offline, audit-friendly.
function hashToken(t, dim) {
  let h = 2166136261;
  for (let i = 0; i < t.length; i++) {
    h ^= t.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h) % dim;
}

export function tokenize(text, strategy = ACTIVE_STRATEGY) {
  const lower = text.toLowerCase();
  const words = lower.match(/[a-z0-9_]+/g) || [];
  const cjk = lower.match(/[\u4e00-\u9fff]/g) || [];
  if (!strategy.cjk_bigram) return [...words, ...cjk];
  // Character bigrams over each contiguous CJK run (never across latin
  // words or punctuation), in addition to the unigrams.
  const bigrams = [];
  for (const run of lower.match(/[\u4e00-\u9fff]+/g) || []) {
    for (let i = 0; i + 1 < run.length; i++) bigrams.push(run.slice(i, i + 2));
  }
  return [...words, ...cjk, ...bigrams];
}

// Inverse document frequency over the chunk corpus (unsupervised corpus
// statistic; it never sees query labels). Smoothed so unseen query tokens
// still get a finite weight.
function buildIdf(chunkTexts, strategy) {
  const df = new Map();
  for (const text of chunkTexts) {
    for (const tok of new Set(tokenize(text, strategy))) df.set(tok, (df.get(tok) || 0) + 1);
  }
  const n = chunkTexts.length;
  return (tok) => Math.log((n + 1) / ((df.get(tok) || 0) + 1)) + 1;
}

export function embed(text, strategy = ACTIVE_STRATEGY, idf = null) {
  const vec = new Float64Array(strategy.dim);
  for (const tok of tokenize(text, strategy)) {
    vec[hashToken(tok, strategy.dim)] += strategy.idf && idf ? idf(tok) : 1;
  }
  let norm = 0;
  for (let i = 0; i < strategy.dim; i++) norm += vec[i] * vec[i];
  norm = Math.sqrt(norm) || 1;
  for (let i = 0; i < strategy.dim; i++) vec[i] /= norm;
  return vec;
}

export function loadDataset() {
  return JSON.parse(fs.readFileSync(DATASET_FILE, 'utf8'));
}

// Build a complete, self-contained index for one strategy. Pure: no I/O
// besides reading the dataset, no span writes — the optimisation loop calls
// this for every candidate strategy without touching the audit log.
export function buildIndex(strategy = ACTIVE_STRATEGY, raw = loadDataset()) {
  const chunks = [];
  const docs = raw.documents.map((d) => {
    const content_sha256 = sha256(d.text);
    const parts = chunkText(d.text);
    parts.forEach((text, i) => {
      chunks.push({
        chunk_id: `${d.document_id}#c${i}`,
        document_id: d.document_id,
        position: i,
        text,
        content_sha256: sha256(text),
        source_ref: `rag://synthetic-docs/${d.document_id}#${d.document_id}#c${i}`,
      });
    });
    return {
      document_id: d.document_id,
      title: d.title,
      source_type: d.source_type,
      classification: d.classification,
      created_at: d.created_at,
      content_sha256,
      data_mode: d.data_mode_override || raw.data_mode,
      char_length: d.text.length,
      chunk_count: parts.length,
    };
  });
  const idf = strategy.idf ? buildIdf(chunks.map((c) => c.text), strategy) : null;
  for (const c of chunks) c.vector = embed(c.text, strategy, idf);
  return { strategy, data_mode: raw.data_mode, docs, chunks, idf };
}

const INDEX = buildIndex(ACTIVE_STRATEGY);

// ---------------------------------------------------------------- retrieval

export function datasetInventory() {
  return {
    data_mode: INDEX.data_mode,
    embedding_backend: EMBEDDING_BACKEND,
    retrieval_strategy: INDEX.strategy.id,
    document_count: INDEX.docs.length,
    chunk_count: INDEX.chunks.length,
    documents: INDEX.docs.map(({ vector, ...rest }) => rest),
  };
}

// Score every chunk of `index` against `query`; pure and span-free. This is
// the single scoring path shared by production retrieval and the
// optimisation loop, so an offline backtest measures exactly what runs.
export function scoreChunks(index, query) {
  const qv = embed(query, index.strategy, index.idf);
  return index.chunks
    .map((c) => ({ chunk: c, score: dotOf(qv, c.vector, index.strategy.dim) }))
    .sort((a, b) => b.score - a.score);
}

export function retrieve(query, { topK = 4 } = {}) {
  const t0 = Date.now();
  const query_hash = sha256(query).slice(0, 16);
  const scored = scoreChunks(INDEX, query).slice(0, Math.max(1, Math.min(topK, 10)));
  const results = scored.map(({ chunk, score }) => ({
    document_id: chunk.document_id,
    chunk_id: chunk.chunk_id,
    score: Number(score.toFixed(4)),
    source_ref: chunk.source_ref,
    retrieval_mode: RETRIEVAL_MODE,
    data_mode: INDEX.data_mode,
  }));
  const latency_ms = Date.now() - t0;
  writeToolSpan({
    tool: 'rag.retrieve',
    arguments_hash: query_hash,
    result_status: results.length ? 'OK' : 'EMPTY',
    document_count: results.length,
    latency_ms,
    data_mode: INDEX.data_mode,
    source_refs: results.map((r) => r.source_ref),
  });
  return {
    query_hash, top_k: results.length, retrieval_mode: RETRIEVAL_MODE,
    retrieval_strategy: INDEX.strategy.id, data_mode: INDEX.data_mode, results, latency_ms,
  };
}

function dotOf(a, b, dim) {
  let d = 0;
  for (let i = 0; i < dim; i++) d += a[i] * b[i];
  return d;
}

// Extractive answer over the top chunks. Citations are mandatory for a
// verified status — no citation ⇒ UNVERIFIED, by construction.
export function answer(question) {
  const t0 = Date.now();
  const r = retrieve(question, { topK: 3 });
  const best = r.results[0];
  if (!best) {
    return { status: 'UNVERIFIED', answer_text: '', citations: [], data_mode: INDEX.data_mode, source_refs: [] };
  }
  const chunk = INDEX.chunks.find((c) => c.chunk_id === best.chunk_id);
  const qTokens = new Set(tokenize(question, INDEX.strategy));
  const sentence = splitSentences(chunk.text)
    .map((s) => {
      const toks = tokenize(s);
      const hit = toks.filter((t) => qTokens.has(t)).length;
      return { s, hit };
    })
    .sort((a, b) => b.hit - a.hit)[0];
  const citations = r.results.map((x) => ({ source_ref: x.source_ref, document_id: x.document_id, chunk_id: x.chunk_id, score: x.score }));
  writeToolSpan({
    tool: 'rag.answer',
    arguments_hash: r.query_hash,
    result_status: 'CITED',
    document_count: citations.length,
    latency_ms: Date.now() - t0,
    data_mode: INDEX.data_mode,
    source_refs: citations.map((c) => c.source_ref),
  });
  return {
    status: 'CITED',
    answer_text: sentence ? sentence.s : chunk.text.slice(0, 160),
    citations,
    query_hash: r.query_hash,
    data_mode: INDEX.data_mode,
    source_refs: citations.map((c) => c.source_ref),
    note: 'extractive answer over SYNTHETIC demo docs; not an LLM completion',
  };
}

// ------------------------------------------------------- tool-span contract

// Phase 16 extends the contract with candidate/branch/assertion fields for
// database migration validation tools. Still forbidden: query/sql/content/
// passwords/connection strings/prompt or completion text.
const ALLOWED_SPAN_KEYS = new Set(['ts', 'tool', 'arguments_hash', 'result_status', 'row_count', 'document_count', 'candidate_id', 'branch_id', 'assertion_count', 'failed_assertions', 'latency_ms', 'data_mode', 'source_refs']);

// Audit record per AgentLoop tool-span contract (Phase 15 §四):
// tool name, arguments_hash, result status, row/document count, latency,
// data_mode, source_refs — and nothing else.
export function toolSpanRecord(rec) {
  const clean = {};
  for (const k of Object.keys(rec)) {
    if (!ALLOWED_SPAN_KEYS.has(k)) throw new Error(`tool span field not allowed by contract: ${k}`);
    clean[k] = rec[k];
  }
  return clean;
}

function writeToolSpan(rec) {
  try {
    fs.appendFileSync(TOOL_SPAN_LOG, JSON.stringify(toolSpanRecord({ ts: new Date().toISOString(), ...rec })) + '\n');
  } catch {
    // audit log best-effort; retrieval itself must not fail the demo
  }
}

// Shared sink for all AgentLoop-contract tool spans (rag.* and database.query).
export { writeToolSpan as appendToolSpan };

// Ingest an externally-produced tool span (e.g. from the in-container MCP
// server used by a real CoPaw agent run). Contract-validated, server-timestamped.
export function ingestExternalToolSpan(rec) {
  // strictly contract-shaped — provenance is documented in evidence, not in the record
  const clean = toolSpanRecord(rec);
  clean.ts = new Date().toISOString();
  try {
    fs.appendFileSync(TOOL_SPAN_LOG, JSON.stringify(clean) + "\n");
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export function toolSpanTail(n = 10) {
  try {
    const lines = fs.readFileSync(TOOL_SPAN_LOG, 'utf8').trim().split('\n').filter(Boolean);
    return lines.slice(-n).map((l) => JSON.parse(l));
  } catch {
    return [];
  }
}

export function lastQueryMeta() {
  const spans = toolSpanTail(1);
  const s = spans[0];
  return s ? { ts: s.ts, tool: s.tool, arguments_hash: s.arguments_hash, data_mode: s.data_mode } : null;
}
