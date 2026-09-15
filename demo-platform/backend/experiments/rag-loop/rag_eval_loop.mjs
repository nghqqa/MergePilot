#!/usr/bin/env node
// experiments/rag-loop/rag_eval_loop.mjs — the RAG observation → evaluation →
// dataset → optimisation → held-out backtest loop, runnable end to end offline.
//
// Contract (fixed before any number was measured — see queries.v1.json):
//   1. OBSERVE   read the real tool-span audit log and report how many
//                retrieval records exist and how many DISTINCT queries they
//                represent. Span rows are never counted as evaluation samples.
//   2. DATASET   load the authored, family-split query set; assert the split is
//                family-disjoint and measure lexical overlap between held-out
//                and tuning queries (leakage check).
//   3. BASELINE  score the production strategy (v1) on both splits.
//   4. TUNE      evaluate every candidate strategy on TUNING families only and
//                pick the best by (MRR, hit@1, hit@3, smaller dim).
//   5. BACKTEST  evaluate the picked strategy on HELD-OUT families exactly once;
//                list every regression; apply the pre-declared promotion rule.
//   6. REPORT    write report.json / report.md / run-meta.json / SHA256SUMS.
//
// Usage:  node demo-platform/backend/experiments/rag-loop/rag_eval_loop.mjs [--out DIR]
// Corpus stays SYNTHETIC/REDACTED; this measures a retrieval strategy, not data.

import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';
import { performance } from 'node:perf_hooks';

import {
  buildIndex, scoreChunks, tokenize, loadDataset, STRATEGIES,
} from '../../lib/rag.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const QUERY_FILE = path.join(__dirname, 'queries.v1.json');
const SPAN_LOG = path.resolve(__dirname, '..', '..', 'rag-data', 'tool-spans.jsonl');

const BASELINE_ID = 'v1-unigram-hash256';

// Candidate grid. Every knob is deterministic; nothing here reads a label.
const CANDIDATES = [];
for (const cjk_bigram of [false, true]) {
  for (const idf of [false, true]) {
    for (const dim of [256, 1024, 4096]) {
      CANDIDATES.push({
        id: `cand-${cjk_bigram ? 'bigram' : 'unigram'}-${idf ? 'idf' : 'tf'}-hash${dim}`,
        dim, cjk_bigram, idf,
      });
    }
  }
}

// Promotion rule, declared before the backtest: the tuned strategy replaces
// the production default only if held-out MRR strictly improves AND held-out
// hit@1 does not drop. Anything else is reported as "no promotion".
const PROMOTION_RULE = 'heldout.mrr > baseline.heldout.mrr && heldout.hit_at_1 >= baseline.heldout.hit_at_1';

function sha256(text) {
  return createHash('sha256').update(text, 'utf8').digest('hex');
}

function round(x, d = 4) {
  return Number(x.toFixed(d));
}

// ── 1. OBSERVE ──────────────────────────────────────────────────────────────
function observe() {
  const lines = fs.existsSync(SPAN_LOG)
    ? fs.readFileSync(SPAN_LOG, 'utf8').split('\n').filter(Boolean)
    : [];
  const byTool = {};
  const retrieveHashes = new Set();
  const answerHashes = new Set();
  const latencies = [];
  const statuses = {};
  for (const line of lines) {
    const rec = JSON.parse(line);
    byTool[rec.tool] = (byTool[rec.tool] || 0) + 1;
    if (rec.tool === 'rag.retrieve') {
      retrieveHashes.add(rec.arguments_hash);
      if (typeof rec.latency_ms === 'number') latencies.push(rec.latency_ms);
      statuses[rec.result_status] = (statuses[rec.result_status] || 0) + 1;
    }
    if (rec.tool === 'rag.answer') answerHashes.add(rec.arguments_hash);
  }
  latencies.sort((a, b) => a - b);
  const pct = (p) => (latencies.length ? latencies[Math.min(latencies.length - 1, Math.floor(p * latencies.length))] : null);
  return {
    span_log: path.relative(REPO_ROOT, SPAN_LOG).replace(/\\/g, '/'),
    total_span_rows: lines.length,
    rows_by_tool: byTool,
    rag_retrieve_rows: byTool['rag.retrieve'] || 0,
    rag_retrieve_distinct_query_hashes: retrieveHashes.size,
    rag_answer_rows: byTool['rag.answer'] || 0,
    rag_answer_distinct_query_hashes: answerHashes.size,
    rag_retrieve_result_status: statuses,
    rag_retrieve_latency_ms: { p50: pct(0.5), p95: pct(0.95), max: latencies.length ? latencies[latencies.length - 1] : null },
    note: 'Span rows are audit records of repeated demo runs, NOT independent evaluation samples. '
      + 'The log stores query_hash only, so query text cannot be recovered from it; the labelled '
      + 'query set below was authored separately and is the evaluation sample.',
  };
}

// ── 2. DATASET ──────────────────────────────────────────────────────────────
function loadQuerySet() {
  const qs = JSON.parse(fs.readFileSync(QUERY_FILE, 'utf8'));
  const heldout = new Set(qs.heldout_families);
  const families = qs.families;
  const ids = new Set();
  for (const f of families) {
    if (ids.has(f.family_id)) throw new Error(`duplicate family ${f.family_id}`);
    ids.add(f.family_id);
    if (!f.expected_document_id || !Array.isArray(f.queries) || f.queries.length < 2) {
      throw new Error(`family ${f.family_id} malformed`);
    }
  }
  for (const h of heldout) if (!ids.has(h)) throw new Error(`heldout family ${h} unknown`);
  const flat = (pred) => families.filter(pred).flatMap((f) => f.queries.map((text, i) => ({
    query_id: `${f.family_id}-q${i + 1}`,
    family_id: f.family_id,
    intent: f.intent,
    text,
    expected_document_id: f.expected_document_id,
    expected_chunk_id: f.expected_chunk_id || null,
  })));
  const tuning = flat((f) => !heldout.has(f.family_id));
  const heldoutQs = flat((f) => heldout.has(f.family_id));
  // Family-disjointness is structural (a family is entirely in one split); assert it.
  const tf = new Set(tuning.map((q) => q.family_id));
  for (const q of heldoutQs) if (tf.has(q.family_id)) throw new Error('split leak: family in both splits');
  return { meta: qs, tuning, heldout: heldoutQs, families };
}

// Lexical leakage check: max/mean Jaccard over bigram+unigram token sets
// between every held-out query and every tuning query. Reported, and pairs
// above 0.5 are flagged as near-paraphrases that would weaken the split.
function leakageCheck(tuning, heldout) {
  const strat = { id: 'leak-check', dim: 1, cjk_bigram: true, idf: false };
  const toks = (t) => new Set(tokenize(t, strat));
  const jaccard = (a, b) => {
    let inter = 0;
    for (const x of a) if (b.has(x)) inter++;
    const union = a.size + b.size - inter;
    return union ? inter / union : 0;
  };
  const tSets = tuning.map((q) => ({ id: q.query_id, s: toks(q.text) }));
  const rows = [];
  for (const h of heldout) {
    const hs = toks(h.text);
    let best = { jaccard: 0, tuning_query_id: null };
    for (const t of tSets) {
      const j = jaccard(hs, t.s);
      if (j > best.jaccard) best = { jaccard: j, tuning_query_id: t.id };
    }
    rows.push({ heldout_query_id: h.query_id, max_jaccard_vs_tuning: round(best.jaccard), nearest_tuning_query_id: best.tuning_query_id });
  }
  const vals = rows.map((r) => r.max_jaccard_vs_tuning);
  return {
    threshold_flag: 0.5,
    max: round(Math.max(...vals)),
    mean: round(vals.reduce((a, b) => a + b, 0) / vals.length),
    flagged_pairs: rows.filter((r) => r.max_jaccard_vs_tuning > 0.5),
    per_heldout_query: rows,
  };
}

// ── 3/4/5. EVALUATE ────────────────────────────────────────────────────────
function evaluate(strategy, queries, raw) {
  const t0 = performance.now();
  const index = buildIndex(strategy, raw);
  const tBuild = performance.now() - t0;
  const perQuery = [];
  const t1 = performance.now();
  for (const q of queries) {
    const ranked = scoreChunks(index, q.text);
    let docRank = null;
    let chunkRank = null;
    ranked.forEach(({ chunk }, i) => {
      if (docRank === null && chunk.document_id === q.expected_document_id) docRank = i + 1;
      if (q.expected_chunk_id && chunkRank === null && chunk.chunk_id === q.expected_chunk_id) chunkRank = i + 1;
    });
    perQuery.push({
      query_id: q.query_id,
      family_id: q.family_id,
      expected_document_id: q.expected_document_id,
      expected_chunk_id: q.expected_chunk_id,
      top1_chunk_id: ranked[0].chunk.chunk_id,
      top1_score: round(ranked[0].score),
      top2_score: round(ranked[1].score),
      margin: round(ranked[0].score - ranked[1].score),
      doc_rank: docRank,
      chunk_rank: chunkRank,
    });
  }
  const tEval = performance.now() - t1;
  const n = perQuery.length;
  const withChunk = perQuery.filter((r) => r.expected_chunk_id);
  const mean = (arr, f) => (arr.length ? arr.reduce((a, r) => a + f(r), 0) / arr.length : null);
  return {
    strategy: { ...strategy },
    n_queries: n,
    n_families: new Set(perQuery.map((r) => r.family_id)).size,
    hit_at_1: round(mean(perQuery, (r) => (r.doc_rank === 1 ? 1 : 0))),
    hit_at_3: round(mean(perQuery, (r) => (r.doc_rank !== null && r.doc_rank <= 3 ? 1 : 0))),
    mrr: round(mean(perQuery, (r) => (r.doc_rank ? 1 / r.doc_rank : 0))),
    chunk_hit_at_1: withChunk.length ? round(mean(withChunk, (r) => (r.chunk_rank === 1 ? 1 : 0))) : null,
    n_chunk_labelled: withChunk.length,
    index_build_ms: round(tBuild, 2),
    eval_ms_total: round(tEval, 2),
    eval_ms_per_query: round(tEval / n, 3),
    per_query: perQuery,
  };
}

function rankKey(r) {
  // higher is better on the first three, then prefer the simpler (smaller) index
  return [r.mrr, r.hit_at_1, r.hit_at_3, -r.strategy.dim, r.strategy.cjk_bigram ? 0 : 1, r.strategy.idf ? 0 : 1];
}

function better(a, b) {
  const ka = rankKey(a); const kb = rankKey(b);
  for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] > kb[i];
  return false;
}

function badcases(baselineRun, index, queriesById) {
  // Explain each tuning failure of the baseline: which chunk won, what tokens
  // of the query matched the winner versus the expected chunk.
  const out = [];
  for (const r of baselineRun.per_query) {
    if (r.doc_rank === 1) continue;
    const q = queriesById.get(r.query_id);
    const qt = new Set(tokenize(q.text, index.strategy));
    const overlap = (chunk) => tokenize(chunk.text, index.strategy).filter((t) => qt.has(t));
    const winner = index.chunks.find((c) => c.chunk_id === r.top1_chunk_id);
    const expected = index.chunks.find((c) => c.document_id === q.expected_document_id
      && (!q.expected_chunk_id || c.chunk_id === q.expected_chunk_id));
    const ov = overlap(winner); const oe = overlap(expected);
    out.push({
      query_id: r.query_id,
      query_text: q.text,
      expected_document_id: q.expected_document_id,
      retrieved_top1_chunk_id: r.top1_chunk_id,
      expected_doc_rank: r.doc_rank,
      margin: r.margin,
      overlap_tokens_with_winner: ov.length,
      overlap_tokens_with_expected: oe.length,
      overlap_distinct_with_winner: [...new Set(ov)].sort().join(' '),
      overlap_distinct_with_expected: [...new Set(oe)].sort().join(' '),
    });
  }
  return out;
}

function compare(before, after) {
  const b = new Map(before.per_query.map((r) => [r.query_id, r]));
  const rows = after.per_query.map((r) => {
    const prev = b.get(r.query_id);
    const delta = (prev.doc_rank || 99) - (r.doc_rank || 99);
    return {
      query_id: r.query_id, family_id: r.family_id,
      doc_rank_before: prev.doc_rank, doc_rank_after: r.doc_rank,
      change: delta > 0 ? 'improved' : delta < 0 ? 'regressed' : 'unchanged',
    };
  });
  return {
    improved: rows.filter((r) => r.change === 'improved'),
    regressed: rows.filter((r) => r.change === 'regressed'),
    unchanged_count: rows.filter((r) => r.change === 'unchanged').length,
    rows,
  };
}

// ── 6. REPORT ──────────────────────────────────────────────────────────────
const TIMING_KEYS = ['index_build_ms', 'eval_ms_total', 'eval_ms_per_query'];

// Metrics are deterministic; wall-clock timings are not. Strip timings out of
// every run record so report.json is byte-reproducible, and collect them into
// a separate timings.json that is excluded from SHA256SUMS.
function stripTimings(run, timings, label) {
  const out = { ...run };
  const t = {};
  for (const k of TIMING_KEYS) { t[k] = out[k]; delete out[k]; }
  timings.push({ label, strategy_id: run.strategy.id, n_queries: run.n_queries, ...t });
  return out;
}

function slim(run) {
  const { per_query, ...rest } = run;
  return rest;
}

function fmtPct(x) {
  return x === null || x === undefined ? 'n/a' : `${(x * 100).toFixed(1)}%`;
}

function markdown(report) {
  const o = report.observation;
  const bT = report.baseline.tuning; const bH = report.baseline.heldout;
  const sT = report.tuning.selected; const sH = report.backtest.heldout;
  const lines = [];
  lines.push('# RAG 观测→评估→数据集→优化→回测 闭环报告');
  lines.push('');
  lines.push(`语料：${report.dataset.corpus}（**${report.dataset.data_mode}/REDACTED**，合成演示文档，非企业语料）。策略版本基线 \`${BASELINE_ID}\`。`);
  lines.push('');
  lines.push('## 1. 观测核验（真实审计记录）');
  lines.push('');
  lines.push(`- 审计日志 \`${o.span_log}\`：共 ${o.total_span_rows} 行 span，其中 rag.retrieve **${o.rag_retrieve_rows} 行**，但只对应 **${o.rag_retrieve_distinct_query_hashes} 个不同 query_hash**；rag.answer ${o.rag_answer_rows} 行 / ${o.rag_answer_distinct_query_hashes} 个不同 hash。`);
  lines.push(`- retrieve 结果状态：${JSON.stringify(o.rag_retrieve_result_status)}；延迟 p50=${o.rag_retrieve_latency_ms.p50}ms p95=${o.rag_retrieve_latency_ms.p95}ms。`);
  lines.push('- 结论：span 行数是重复演示的审计记录，**不是独立样本**；日志只存 query_hash，无法反推查询文本，因此评估样本必须单独标注（见 §2）。');
  lines.push('');
  lines.push('## 2. 标注数据集与切分');
  lines.push('');
  lines.push(`- 查询集 \`${report.dataset.query_file}\`（版本 ${report.dataset.version}）：${report.dataset.n_families} 个语义族（意图），${report.dataset.n_queries} 条查询；**按族切分**：调优 ${report.dataset.n_tuning_families} 族 / ${report.dataset.n_tuning_queries} 条，held-out ${report.dataset.n_heldout_families} 族 / ${report.dataset.n_heldout_queries} 条。`);
  lines.push(`- 泄漏检查（held-out 每条查询与全部调优查询的最大词元 Jaccard）：max=${report.dataset.leakage.max}，mean=${report.dataset.leakage.mean}，超过 ${report.dataset.leakage.threshold_flag} 的近似改写对：${report.dataset.leakage.flagged_pairs.length} 对。`);
  lines.push('- 指标：文档级 hit@1 / hit@3 / MRR；多 chunk 文档另计 chunk-hit@1（仅 ' + `${bT.n_chunk_labelled + bH.n_chunk_labelled}` + ' 条带 chunk 标签）。');
  lines.push('');
  lines.push('## 3. 基线（生产策略 v1）');
  lines.push('');
  lines.push('| 集合 | n | hit@1 | hit@3 | MRR | chunk-hit@1 |');
  lines.push('|---|---|---|---|---|---|');
  lines.push(`| 调优集 | ${bT.n_queries} | ${fmtPct(bT.hit_at_1)} | ${fmtPct(bT.hit_at_3)} | ${bT.mrr} | ${fmtPct(bT.chunk_hit_at_1)} (n=${bT.n_chunk_labelled}) |`);
  lines.push(`| held-out | ${bH.n_queries} | ${fmtPct(bH.hit_at_1)} | ${fmtPct(bH.hit_at_3)} | ${bH.mrr} | ${fmtPct(bH.chunk_hit_at_1)} (n=${bH.n_chunk_labelled}) |`);
  lines.push('');
  lines.push(`### 3.1 调优集 badcase（基线未命中 top-1 的 ${report.badcases.length} 条）`);
  lines.push('');
  lines.push('| query | 期望文档 | 实际 top-1 chunk | 期望文档名次 | 分差 | 与命中块重叠词元 | 与期望块重叠词元 |');
  lines.push('|---|---|---|---|---|---|---|');
  for (const b of report.badcases) {
    lines.push(`| ${b.query_id} ${b.query_text} | ${b.expected_document_id} | ${b.retrieved_top1_chunk_id} | ${b.expected_doc_rank} | ${b.margin} | ${b.overlap_tokens_with_winner} | ${b.overlap_tokens_with_expected} |`);
  }
  lines.push('');
  lines.push('## 4. 策略优化（只看调优集）');
  lines.push('');
  lines.push(`候选网格 ${report.tuning.grid.length} 个（CJK 二元组 × IDF × 哈希维度），选择规则：MRR → hit@1 → hit@3 → 更小维度。`);
  lines.push('');
  lines.push('| 策略 | hit@1 | hit@3 | MRR | chunk-hit@1 |');
  lines.push('|---|---|---|---|---|');
  for (const g of report.tuning.grid) {
    const mark = g.strategy.id === sT.strategy.id ? ' **←选中**' : '';
    lines.push(`| ${g.strategy.id}${mark} | ${fmtPct(g.hit_at_1)} | ${fmtPct(g.hit_at_3)} | ${g.mrr} | ${fmtPct(g.chunk_hit_at_1)} |`);
  }
  lines.push('');
  lines.push('耗时（非确定性，见 timings.json）：' + report.timing_summary);
  lines.push('');
  lines.push('## 5. held-out 回测（选中策略只评估一次）');
  lines.push('');
  lines.push('| 集合 | 策略 | hit@1 | hit@3 | MRR | chunk-hit@1 |');
  lines.push('|---|---|---|---|---|---|');
  lines.push(`| held-out | 基线 ${BASELINE_ID} | ${fmtPct(bH.hit_at_1)} | ${fmtPct(bH.hit_at_3)} | ${bH.mrr} | ${fmtPct(bH.chunk_hit_at_1)} |`);
  lines.push(`| held-out | 选中 ${sH.strategy.id} | ${fmtPct(sH.hit_at_1)} | ${fmtPct(sH.hit_at_3)} | ${sH.mrr} | ${fmtPct(sH.chunk_hit_at_1)} |`);
  lines.push('');
  const c = report.backtest.comparison;
  lines.push(`- 逐条变化：改善 ${c.improved.length} 条，**退化 ${c.regressed.length} 条**，不变 ${c.unchanged_count} 条。`);
  if (c.regressed.length) {
    lines.push('- 退化案例：');
    for (const r of c.regressed) lines.push(`  - ${r.query_id}（${r.family_id}）文档名次 ${r.doc_rank_before} → ${r.doc_rank_after}`);
  }
  lines.push(`- 晋级规则（回测前声明）：\`${PROMOTION_RULE}\` → **${report.backtest.promotion.decision}**。${report.backtest.promotion.reason}`);
  lines.push('');
  lines.push('## 6. 限制与诚实声明');
  lines.push('');
  lines.push(`- 样本规模小：语料 8 篇 / 9 chunk，held-out ${bH.n_queries} 条查询；单条查询名次变化即影响 ${fmtPct(1 / bH.n_queries)} 的 hit@1。结论是“该策略在此合成语料上更优”，不外推到企业语料。`);
  lines.push('- 标注由团队编写，存在标注者偏差；查询集在测量任何基线前固定，held-out 只评估一次，未针对 held-out 调参。');
  lines.push('- 语料 SYNTHETIC/REDACTED 边界不变；本实验优化的是检索策略，不改变数据模式声明。');
  lines.push('');
  lines.push('## 7. 复现');
  lines.push('');
  lines.push('```bash');
  lines.push('node demo-platform/backend/experiments/rag-loop/rag_eval_loop.mjs --out <dir>');
  lines.push('```');
  lines.push(`报告 JSON 的 sha256 见同目录 SHA256SUMS；运行环境见 run-meta.json。`);
  return lines.join('\n') + '\n';
}

function main() {
  const args = process.argv.slice(2);
  const outIdx = args.indexOf('--out');
  const outDir = outIdx >= 0 ? path.resolve(args[outIdx + 1]) : path.join(REPO_ROOT, 'evidence', 'FINALS-RAG-LOOP-20260914');

  const raw = loadDataset();
  const observation = observe();
  const ds = loadQuerySet();
  const leakage = leakageCheck(ds.tuning, ds.heldout);
  const queriesById = new Map([...ds.tuning, ...ds.heldout].map((q) => [q.query_id, q]));

  const baseline = STRATEGIES[BASELINE_ID];
  const baseTuning = evaluate(baseline, ds.tuning, raw);
  const baseHeldout = evaluate(baseline, ds.heldout, raw);
  const baseIndex = buildIndex(baseline, raw);
  const bad = badcases(baseTuning, baseIndex, queriesById);

  const grid = CANDIDATES.map((s) => evaluate(s, ds.tuning, raw));
  let selected = grid[0];
  for (const g of grid) if (better(g, selected)) selected = g;

  const heldoutRun = evaluate(selected.strategy, ds.heldout, raw);
  const comparison = compare(baseHeldout, heldoutRun);
  const promote = heldoutRun.mrr > baseHeldout.mrr && heldoutRun.hit_at_1 >= baseHeldout.hit_at_1;
  const promotion = {
    rule: PROMOTION_RULE,
    decision: promote ? 'PROMOTE' : 'NO_PROMOTION',
    reason: promote
      ? `held-out MRR ${baseHeldout.mrr} → ${heldoutRun.mrr}，hit@1 ${baseHeldout.hit_at_1} → ${heldoutRun.hit_at_1}；满足规则。`
      : `held-out MRR ${baseHeldout.mrr} → ${heldoutRun.mrr}，hit@1 ${baseHeldout.hit_at_1} → ${heldoutRun.hit_at_1}；不满足规则，生产策略保持 ${BASELINE_ID}。`,
  };

  const timings = [];
  const baseTuningR = stripTimings(baseTuning, timings, 'baseline/tuning');
  const baseHeldoutR = stripTimings(baseHeldout, timings, 'baseline/heldout');
  const gridR = grid.map((g) => stripTimings(g, timings, 'grid/tuning'));
  const selectedR = stripTimings(selected, timings, 'selected/tuning');
  const heldoutRunR = stripTimings(heldoutRun, timings, 'selected/heldout');
  const tSel = timings.find((t) => t.label === 'selected/heldout');
  const tBase = timings.find((t) => t.label === 'baseline/heldout');
  const timingSummary = `基线 held-out 每查询 ${tBase.eval_ms_per_query}ms / 建索引 ${tBase.index_build_ms}ms；选中策略 held-out 每查询 ${tSel.eval_ms_per_query}ms / 建索引 ${tSel.index_build_ms}ms（本次运行，${new Date().toISOString().slice(0, 10)}）`;

  const report = {
    report_version: 'rag-loop-report.v1',
    baseline_strategy_id: BASELINE_ID,
    observation,
    dataset: {
      query_file: path.relative(REPO_ROOT, QUERY_FILE).replace(/\\/g, '/'),
      query_file_sha256: sha256(fs.readFileSync(QUERY_FILE, 'utf8')),
      corpus: ds.meta.corpus,
      corpus_sha256: sha256(JSON.stringify(raw)),
      data_mode: ds.meta.data_mode,
      version: ds.meta.dataset_version,
      split_rule: ds.meta.split_rule,
      n_families: ds.families.length,
      n_queries: ds.tuning.length + ds.heldout.length,
      n_tuning_families: new Set(ds.tuning.map((q) => q.family_id)).size,
      n_tuning_queries: ds.tuning.length,
      n_heldout_families: new Set(ds.heldout.map((q) => q.family_id)).size,
      n_heldout_queries: ds.heldout.length,
      heldout_families: ds.meta.heldout_families,
      leakage,
    },
    baseline: { tuning: baseTuningR, heldout: baseHeldoutR },
    badcases: bad,
    tuning: { grid: gridR.map(slim), selected: slim(selectedR), selection_rule: 'MRR desc, hit@1 desc, hit@3 desc, smaller dim, fewer features' },
    backtest: { heldout: heldoutRunR, comparison, promotion },
  };
  // Markdown embeds the (non-deterministic) timing sentence; JSON does not.
  const reportMd = markdown({ ...report, timing_summary: timingSummary });

  fs.mkdirSync(outDir, { recursive: true });
  const reportJson = JSON.stringify(report, null, 2) + '\n';
  let gitSha = 'unknown';
  try { gitSha = execSync('git rev-parse HEAD', { cwd: REPO_ROOT, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim(); } catch { /* not a git checkout */ }
  const runMeta = {
    generated_at: new Date().toISOString(),
    node: process.version,
    platform: `${process.platform}-${process.arch}`,
    git_head: gitSha,
    command: 'node demo-platform/backend/experiments/rag-loop/rag_eval_loop.mjs',
    evidence_tier: 'REAL_OFFLINE_EXPERIMENT on SYNTHETIC corpus',
  };
  fs.writeFileSync(path.join(outDir, 'report.json'), reportJson);
  fs.writeFileSync(path.join(outDir, 'report.md'), reportMd);
  fs.writeFileSync(path.join(outDir, 'run-meta.json'), JSON.stringify(runMeta, null, 2) + '\n');
  fs.writeFileSync(path.join(outDir, 'timings.json'), JSON.stringify({ note: 'wall-clock, non-deterministic, excluded from SHA256SUMS', runs: timings }, null, 2) + '\n');
  fs.copyFileSync(QUERY_FILE, path.join(outDir, 'queries.v1.json'));
  // report.md carries the timing sentence, so only the deterministic files are locked.
  const sums = ['report.json', 'queries.v1.json']
    .map((f) => `${sha256(fs.readFileSync(path.join(outDir, f), 'utf8'))} *${f}`).join('\n') + '\n';
  fs.writeFileSync(path.join(outDir, 'SHA256SUMS'), sums);

  const line = (label, r) => `${label.padEnd(22)} n=${String(r.n_queries).padStart(2)} hit@1=${fmtPct(r.hit_at_1).padStart(6)} hit@3=${fmtPct(r.hit_at_3).padStart(6)} MRR=${r.mrr}`;
  console.log(`[observe] span rows=${observation.total_span_rows} rag.retrieve=${observation.rag_retrieve_rows} distinct queries=${observation.rag_retrieve_distinct_query_hashes}`);
  console.log(`[dataset] families=${report.dataset.n_families} queries=${report.dataset.n_queries} tuning=${ds.tuning.length} heldout=${ds.heldout.length} leakage max=${leakage.max} flagged=${leakage.flagged_pairs.length}`);
  console.log(line('[baseline tuning]', baseTuning));
  console.log(line('[baseline heldout]', baseHeldout));
  console.log(`[tune] selected=${selected.strategy.id} (${line('tuning', selected)})`);
  console.log(line(`[backtest heldout]`, heldoutRun));
  console.log(`[backtest] improved=${comparison.improved.length} regressed=${comparison.regressed.length} unchanged=${comparison.unchanged_count} → ${promotion.decision}`);
  console.log(`[report] ${outDir}`);
}

main();
