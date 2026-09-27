#!/usr/bin/env node
// deploy/rag-zh-gate/scripts/calibrate-hybrid.mjs — 混合检索离线校准器（ZH 生产候选准备波）。
//
// 输入：zh-gate 栈 PG（已三模型摄取）+ 两个 attested sidecar；仅用 calibration 切分。
// 网格：w_vec × lex_mode{plain|idf} × floor。评分 final = w·vec + (1-w)·lex。
// lex(idf)：Σ idf(q∩d)/Σ idf(q)，idf=ln(1+N/(1+df))，df 来自该 scope 全部活跃 chunk
// （真语料驱动，非停用词硬编码）。
//
// 选择规则（预声明，反 gaming）：
//   1) 必须满足 calib empty_accuracy == 1.0（假阳不掩盖）且 synonym/cross recall@5 ≥ 0.5；
//   2) 在满足 1) 的配置中最大化 calib Recall@5；并列取更高地板，再取更高语义权重；
//   3) 若无配置满足 1) → 如实输出"未能消除假阳"，选 empty_acc 最高者并标记。
//   4) holdout 由门禁重跑验证（本脚本不看 holdout）。
import { createRequire } from 'node:module';
import path from 'node:path';
import fs from 'node:fs';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WORKTREE = path.resolve(HERE, '..', '..', '..');
const req = createRequire(path.join(WORKTREE, 'console', 'backend', 'test', 'support', 'noop.js'));
const { Pool } = req('pg');
const { pathToFileURL } = await import('node:url');
const { embedLocal, tokenize } = await import(pathToFileURL(path.join(WORKTREE, 'console', 'backend', 'lib', 'ragtrial', 'embed.mjs')).href);

const env = {};
for (const line of fs.readFileSync(path.join(HERE, '..', '.env'), 'utf8').split('\n')) {
  const m = line.match(/^([A-Z_0-9]+)=(.*)$/); if (m) env[m[1]] = m[2].trim();
}
const REPO = 'nghqqa/mergepilot', BR = 'feat/rag-zh-gate';
const DSN = `postgres://postgres:${env.RAGZH_PG_PASSWORD}@127.0.0.1:${env.RAGZH_PG_PORT}/ragzh`;
const TABLES = { 'local-hash-v1': 'ragtrial.chunks', 'bge-large-en-v1.5': 'ragtrial.chunks_semantic', 'e5-base-v2': 'ragtrial.chunks_semantic_768' };
const SIDECARS = { 'bge-large-en-v1.5': { url: `http://127.0.0.1:${env.RAGZH_BGE_PORT}`, pin: env.RAGZH_BGE_EXPECTED_MANIFEST },
                   'e5-base-v2': { url: `http://127.0.0.1:${env.RAGZH_E5_PORT}`, pin: env.RAGZH_E5_EXPECTED_MANIFEST } };

const pool = new Pool({ connectionString: DSN });

async function embedQuery(modelId, text) {
  if (modelId === 'local-hash-v1') return embedLocal(text);
  const s = SIDECARS[modelId];
  const mres = await fetch(s.url + '/manifest');
  const mt = await mres.text();
  const sha = crypto.createHash('sha256').update(mt).digest('hex');
  if (sha !== s.pin) throw new Error(`${modelId} manifest pin mismatch（fail-closed）`);
  const res = await fetch(s.url + '/embed', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ input: [text], mode: 'query' }),
  });
  return (await res.json()).data[0].embedding;
}

async function candidates(modelId, qvec, limit = 30) {
  const vec = '[' + qvec.join(',') + ']';
  const r = await pool.query(
    `SELECT c.doc_path, c.text, 1 - (c.embedding <=> $1::vector) AS vec_score
       FROM ${TABLES[modelId]} c
       JOIN ragtrial.documents d ON d.repo=c.repo AND d.branch=c.branch AND d.doc_path=c.doc_path AND d.state='active'
      WHERE c.repo=$2 AND c.branch=$3
      ORDER BY c.embedding <=> $1::vector LIMIT $4`,
    [vec, REPO, BR, limit]);
  return r.rows;
}

// 语料 DF（scope 全部活跃 chunk；true corpus IDF）
async function corpusDf(modelId) {
  const r = await pool.query(
    `SELECT c.text FROM ${TABLES[modelId]} c
      JOIN ragtrial.documents d ON d.repo=c.repo AND d.branch=c.branch AND d.doc_path=c.doc_path AND d.state='active'
     WHERE c.repo=$1 AND c.branch=$2`, [REPO, BR]);
  const df = new Map(); const N = r.rows.length;
  for (const row of r.rows) {
    for (const t of new Set(tokenize(row.text))) df.set(t, (df.get(t) ?? 0) + 1);
  }
  const idf = (t) => Math.log(1 + N / (1 + (df.get(t) ?? 0)));
  return { N, idf };
}

function scoreCandidates(cands, qTokens, cfg, idf) {
  const qArr = [...qTokens];
  const qIdfSum = qArr.reduce((s, t) => s + idf(t), 0) || 1;
  return cands.map((c) => {
    const dTokens = new Set(tokenize(c.text));
    let shared = 0, sharedIdf = 0;
    for (const t of qArr) if (dTokens.has(t)) { shared++; sharedIdf += idf(t); }
    const lex = cfg.lexMode === 'idf'
      ? sharedIdf / qIdfSum
      : (qArr.length && dTokens.size ? shared / Math.min(qArr.length, dTokens.size) : 0);
    const vec = Number(c.vec_score);
    return { doc: c.doc_path, final: cfg.w * vec + (1 - cfg.w) * lex, vec, lex };
  })
    .filter((x) => x.final >= cfg.floor)
    .sort((a, b) => b.final - a.final);
}

const recallAt = (ranked, expect, k) => ranked.findIndex((x) => x.doc === expect) < k
  && ranked.findIndex((x) => x.doc === expect) >= 0;

async function evalConfig(modelId, prepared, cfg) {
  const catHit = {}; let r5 = 0, emptyOk = 0;
  for (const item of prepared.qa) {
    const ranked = scoreCandidates(item.cands, item.qTokens, cfg, item.idf).slice(0, 5);
    const hit = ranked.some((x) => x.doc === item.expect_doc);
    if (hit) r5++;
    catHit[item.cat] = catHit[item.cat] ?? { t: 0, h: 0 };
    catHit[item.cat].t++; if (hit) catHit[item.cat].h++;
  }
  for (const item of prepared.empties) {
    if (scoreCandidates(item.cands, item.qTokens, cfg, item.idf).length === 0) emptyOk++;
  }
  return {
    recall_at_5: +(r5 / prepared.qa.length).toFixed(4),
    by_category: Object.fromEntries(Object.entries(catHit).map(([c, v]) => [c, +(v.h / v.t).toFixed(4)])),
    empty_accuracy: +(emptyOk / prepared.empties.length).toFixed(4),
  };
}

async function prepare(modelId, qaSet) {
  const { idf, N } = await corpusDf(modelId);
  const qa = [], empties = [];
  for (const item of qaSet.qa) {
    const qvec = await embedQuery(modelId, item.q);
    qa.push({ ...item, qTokens: new Set(tokenize(item.q)), cands: await candidates(modelId, qvec), idf });
  }
  for (const item of qaSet.empty_queries) {
    const qvec = await embedQuery(modelId, item.q);
    empties.push({ ...item, qTokens: new Set(tokenize(item.q)), cands: await candidates(modelId, qvec), idf });
  }
  return { qa, empties, corpus_chunks: N };
}

async function main() {
  const calib = JSON.parse(fs.readFileSync(path.join(HERE, '..', 'corpus', 'qa-set-zh-calibration.json'), 'utf8'));
  const out = { generated_at: new Date().toISOString(), selection_rule: 'empty_acc==1.0 且 synonym/cross≥0.5 前提下最大化 calib R@5（并列取高地板→高语义权重）；否则如实标记假阳未消除', models: {} };
  for (const modelId of ['local-hash-v1', 'bge-large-en-v1.5', 'e5-base-v2']) {
    const prepared = await prepare(modelId, calib);
    const grid = [];
    for (const w of [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]) {
      for (const lexMode of ['plain', 'idf']) {
        for (let floor = 0.05; floor <= 0.6001; floor += 0.05) {
          const cfg = { w, lexMode, floor: +floor.toFixed(2) };
          const m = await evalConfig(modelId, prepared, cfg);
          grid.push({ cfg, ...m });
        }
      }
    }
    const eligible = grid.filter((g) => g.empty_accuracy === 1
      && (g.by_category.synonym ?? 1) >= 0.5 && (g.by_category.cross ?? 1) >= 0.5);
    let chosen, note;
    if (eligible.length) {
      eligible.sort((a, b) => b.recall_at_5 - a.recall_at_5 || b.cfg.floor - a.cfg.floor || b.cfg.w - a.cfg.w);
      chosen = eligible[0];
      note = `满足反 gaming 规则（${eligible.length} 个合格配置）`;
    } else {
      const byEmpty = [...grid].sort((a, b) => b.empty_accuracy - a.empty_accuracy || b.recall_at_5 - a.recall_at_5);
      chosen = byEmpty[0];
      note = `⚠️ 无配置同时满足空准确=1.0 与同义/跨表述≥0.5——假阳未消除，取空准确最高者`;
    }
    // 前沿面：empty=1.0 约束下各类别可达上限（证明权衡是能力界而非调参界）
    const emptyOk = grid.filter((g) => g.empty_accuracy === 1);
    const frontier = emptyOk.length ? {
      configs_with_empty_1: emptyOk.length,
      max_recall_at_5: Math.max(...emptyOk.map((g) => g.recall_at_5)),
      max_synonym: Math.max(...emptyOk.map((g) => g.by_category.synonym ?? 0)),
      max_cross: Math.max(...emptyOk.map((g) => g.by_category.cross ?? 0)),
    } : { configs_with_empty_1: 0 };
    // holdout 验证（不参与选择）：selected / deployed / max-recall / max-empty 四短名单
    const holdoutSet = JSON.parse(fs.readFileSync(path.join(HERE, '..', 'corpus', 'qa-set-zh-holdout.json'), 'utf8'));
    const hp = await prepare(modelId, holdoutSet);
    const deployed = { 'local-hash-v1': { w: 0.5, lexMode: 'plain', floor: 0.1 },
      'bge-large-en-v1.5': { w: 0.5, lexMode: 'plain', floor: 0.52 },
      'e5-base-v2': { w: 0.5, lexMode: 'plain', floor: 0.45 } }[modelId];
    const maxRecall = [...grid].sort((a, b) => b.recall_at_5 - a.recall_at_5 || b.empty_accuracy - a.empty_accuracy)[0];
    const maxEmpty = [...grid].sort((a, b) => b.empty_accuracy - a.empty_accuracy || b.recall_at_5 - a.recall_at_5)[0];
    const shortlist = [
      { name: 'selected', cfg: chosen.cfg, calib: chosen },
      { name: 'deployed_current', cfg: deployed, calib: await evalConfig(modelId, prepared, deployed) },
      { name: 'max_recall', cfg: maxRecall.cfg, calib: maxRecall },
      { name: 'max_empty', cfg: maxEmpty.cfg, calib: maxEmpty },
    ];
    for (const item of shortlist) item.holdout = await evalConfig(modelId, hp, item.cfg);
    out.models[modelId] = {
      corpus_chunks: prepared.corpus_chunks,
      chosen: { ...chosen.cfg, ...chosen },
      note, frontier, shortlist,
      grid: grid.map((g) => ({ cfg: g.cfg, r5: g.recall_at_5, empty: g.empty_accuracy, syn: g.by_category.synonym ?? null, cross: g.by_category.cross ?? null })),
    };
    console.log(`[calib] ${modelId}: chosen=${JSON.stringify(chosen.cfg)} R@5=${chosen.recall_at_5} empty=${chosen.empty_accuracy} cat=${JSON.stringify(chosen.by_category)} — ${note}`);
    console.log(`        frontier(empty=1): ${JSON.stringify(frontier)}`);
    for (const it of shortlist) console.log(`        holdout[${it.name}] ${JSON.stringify(it.cfg)} → R@5=${it.holdout.recall_at_5} empty=${it.holdout.empty_accuracy} cat=${JSON.stringify(it.holdout.by_category)}`);
  }
  const EVID = path.join(WORKTREE, 'evidence', 'rag-zh-gate', new Date().toISOString().replace(/[:.]/g, '-'));
  fs.mkdirSync(EVID, { recursive: true });
  fs.writeFileSync(path.join(EVID, 'calibration.json'), JSON.stringify(out, null, 2));
  console.log('evidence:', EVID);
  await pool.end();
}
main().catch((e) => { console.error('FATAL', e); process.exit(1); });
