#!/usr/bin/env node
// deploy/rag-zh-eval/scripts/eval-matrix.mjs — RAG_ZH_MODEL_RERANKER_EVALUATION 实验矩阵。
//
// 矩阵：模型 {local-hash-v1, bge-large-en(对照), e5-base-v2(对照·内部试验),
//  bge-m3(新), bge-large-zh-v1.5(新)} × 配置 {emb-only, hybrid, hybrid+rerank}。
// 分块/引用行：直接复用 zh-gate 栈 PG 已索引的 chunk 行（引用一致性）；
// 嵌入：全部 host-side（bge_embed stdin 模式，manifest fail-closed 门先行）；
// rerank：bge-reranker-v2-m3 交叉编码器（新双模型 + e5 对照组）。
// 协议：calib 选择（反 gaming 规则同前：空准确=1.0 且 syn/cross≥0.5 前提下最大 R@5），
// holdout 只验证；阈值不得事后调整。
import { createRequire } from 'node:module';
import { spawn, spawnSync } from 'node:child_process';
import path from 'node:path';
import fs from 'node:fs';
import crypto from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WORKTREE = path.resolve(HERE, '..', '..', '..');
const req = createRequire(path.join(WORKTREE, 'console', 'backend', 'test', 'support', 'noop.js'));
const { Pool } = req('pg');
const { embedLocal, tokenize } = await import(pathToFileURL(path.join(WORKTREE, 'console', 'backend', 'lib', 'ragtrial', 'embed.mjs')).href);

const MODELS_DIR = 'D:/goai/rag-zh-models';
const MODELS = [
  { id: 'local-hash-v1', kind: 'js' },
  { id: 'bge-large-en-v1.5', kind: 'py', dir: 'C:/Users/ngh/.cache/huggingface/hub/models--BAAI--bge-large-en-v1.5/snapshots/d4aa6901d3a41ba39fb536a557fa166f842b0e09', manifest: 'deploy/rag-prod/bge-large-en-v1.5.manifest.json' },
  { id: 'e5-base-v2', kind: 'py', dir: 'C:/Users/ngh/.cache/huggingface/hub/models--intfloat--e5-base-v2/snapshots/f52bf8ec8c7124536f0efb74aca902b2995e5bcd', manifest: 'deploy/rag-zh-gate/e5-base-v2.manifest.json', qPrefix: 'query: ', pPrefix: 'passage: ' },
  { id: 'bge-m3', kind: 'py', dir: `${MODELS_DIR}/bge-m3`, manifest: 'deploy/rag-zh-eval/bge-m3.manifest.json' },
  { id: 'bge-large-zh-v1.5', kind: 'py', dir: `${MODELS_DIR}/bge-large-zh`, manifest: 'deploy/rag-zh-eval/bge-large-zh.manifest.json' },
];
const RERANK = { id: 'bge-reranker-v2-m3', dir: `${MODELS_DIR}/bge-reranker-v2-m3`, manifest: 'deploy/rag-zh-eval/bge-reranker-v2-m3.manifest.json' };
const RERANK_FOR = ['e5-base-v2', 'bge-m3', 'bge-large-zh-v1.5']; // 对照组=最优旧+双新

const env = {};
for (const line of fs.readFileSync(path.join(WORKTREE, 'deploy/rag-zh-gate/.env'), 'utf8').split('\n')) {
  const m = line.match(/^([A-Z_0-9]+)=(.*)$/); if (m) env[m[1]] = m[2].trim();
}
const DSN = `postgres://postgres:${env.RAGZH_PG_PASSWORD}@127.0.0.1:${env.RAGZH_PG_PORT}/ragzh`;
const EVID = path.join(WORKTREE, 'evidence', 'rag-zh-eval', new Date().toISOString().replace(/[:.]/g, '-'));
fs.mkdirSync(EVID, { recursive: true });
const log = (m) => { console.log(m); fs.appendFileSync(path.join(EVID, 'transcript.log'), m + '\n'); };

function pyEmbed(model, reqs) { // [{id,text}] → Map id→vector
  const child = spawn('python', ['-X', 'utf8', path.join(WORKTREE, 'tools', 'bge_embed.py'),
    '--model-dir', model.dir, '--manifest', path.join(WORKTREE, model.manifest), '--max-len', '128'], { cwd: WORKTREE });
  const out = [];
  let buf = '';
  child.stdout.on('data', (c) => {
    buf += c;
    let i;
    while ((i = buf.indexOf('\n')) >= 0) { const l = buf.slice(0, i); buf = buf.slice(i + 1); if (l.trim()) out.push(JSON.parse(l)); }
  });
  const err = [];
  child.stderr.on('data', (c) => err.push(String(c)));
  const feed = async () => {
    for (const r of reqs) child.stdin.write(JSON.stringify(r) + '\n');
    child.stdin.end();
  };
  feed();
  return new Promise((resolve, reject) => {
    child.on('exit', (code) => code === 0 ? resolve(new Map(out.map((o) => [o.id, o.vector])))
      : reject(new Error(`pyEmbed ${model.id} exit ${code}: ${err.join('').slice(-300)}`)));
  });
}

function pyRerank(rows) { // [{qid,query,passage}] → Map qid→score
  const child = spawn('python', ['-X', 'utf8', path.join(WORKTREE, 'tools', 'rerank_score.py'),
    '--model-dir', RERANK.dir, '--manifest', path.join(WORKTREE, RERANK.manifest)], { cwd: WORKTREE });
  const out = []; let buf = '';
  child.stdout.on('data', (c) => {
    buf += c;
    let i;
    while ((i = buf.indexOf('\n')) >= 0) { const l = buf.slice(0, i); buf = buf.slice(i + 1); if (l.trim()) out.push(JSON.parse(l)); }
  });
  const err = [];
  child.stderr.on('data', (c) => err.push(String(c)));
  for (const r of rows) child.stdin.write(JSON.stringify(r) + '\n');
  child.stdin.end();
  return new Promise((resolve, reject) => {
    child.on('exit', (code) => code === 0 ? resolve(new Map(out.map((o) => [o.qid, o.score])))
      : reject(new Error(`rerank exit ${code}: ${err.join('').slice(-300)}`)));
  });
}

const dot = (a, b) => { let s = 0; for (let i = 0; i < a.length; i++) s += a[i] * b[i]; return s; };

async function main() {
  const pool = new Pool({ connectionString: DSN });
  // 1) chunk 行（引用一致性：复用已索引行）
  const chunks = (await pool.query(
    `SELECT doc_path, line_start, line_end, chunk_index, text FROM ragtrial.chunks
      WHERE repo='nghqqa/mergepilot' AND branch='feat/rag-zh-gate' AND doc_path NOT LIKE '%/%'
      ORDER BY doc_path, chunk_index`)).rows;
  log(`[matrix] corpus chunks: ${chunks.length}（复用 zh-gate 索引行，引用一致）`);
  const corpusFiles = {};
  for (const f of fs.readdirSync(path.join(WORKTREE, 'deploy/rag-zh-gate/corpus')).filter((f) => f.endsWith('.md'))) {
    corpusFiles[f] = fs.readFileSync(path.join(WORKTREE, 'deploy/rag-zh-gate/corpus', f), 'utf8');
  }
  // 2) 评估集
  const calib = JSON.parse(fs.readFileSync(path.join(WORKTREE, 'deploy/rag-zh-gate/corpus/qa-set-zh-calibration.json'), 'utf8'));
  const hold = JSON.parse(fs.readFileSync(path.join(WORKTREE, 'deploy/rag-zh-gate/corpus/qa-set-zh-holdout.json'), 'utf8'));
  const allQueries = [...calib.qa, ...calib.empty_queries.map((q) => ({ ...q, empty: true })),
    ...hold.qa, ...hold.empty_queries.map((q) => ({ ...q, empty: true }))];

  // 3) 语料 DF（idf 用）
  const df = new Map();
  for (const c of chunks) for (const t of new Set(tokenize(c.text))) df.set(t, (df.get(t) ?? 0) + 1);
  const N = chunks.length;
  const idf = (t) => Math.log(1 + N / (1 + (df.get(t) ?? 0)));

  const results = {};
  for (const model of MODELS) {
    const t0 = Date.now();
    // 嵌入：chunks + queries
    let chunkVec, qVec = new Map();
    if (model.kind === 'js') {
      chunkVec = chunks.map((c) => embedLocal(c.text));
      for (const q of allQueries) qVec.set(q.q, embedLocal(q.q));
    } else {
      const cMap = await pyEmbed(model, chunks.map((c, i) => ({ id: `c${i}`, text: (model.pPrefix ?? '') + c.text })));
      chunkVec = chunks.map((_, i) => cMap.get(`c${i}`));
      const qMap = await pyEmbed(model, allQueries.map((q, i) => ({ id: `q${i}`, text: (model.qPrefix ?? '') + q.q })));
      allQueries.forEach((q, i) => qVec.set(q.q, qMap.get(`q${i}`)));
    }
    const loadS = +((Date.now() - t0) / 1000).toFixed(1);
    // 每查询：候选（全部 chunk）vec 分
    const perQuery = allQueries.map((q) => {
      const v = qVec.get(q.q);
      return { q, cands: chunks.map((c, i) => ({ doc: c.doc_path, chunk: c, vec: dot(v, chunkVec[i]) })) };
    });
    const latP50 = null; // host 侧嵌入吞吐在 python stderr；查询级延迟=嵌入+打分，见资源段

    // 打分器：emb-only / hybrid / (rerank 外挂)
    const scoreHybrid = (cand, qTokens, cfg) => {
      const dTokens = new Set(tokenize(cand.chunk.text));
      let shared = 0, sharedIdf = 0;
      for (const t of qTokens) if (dTokens.has(t)) { shared++; sharedIdf += idf(t); }
      const lex = cfg.lexMode === 'idf'
        ? sharedIdf / (qTokens.reduce((s, t) => s + idf(t), 0) || 1)
        : (qTokens.length && dTokens.size ? shared / Math.min(qTokens.length, dTokens.size) : 0);
      const vec = cfg.w === 1 ? cand.vec : cfg.w * cand.vec + (1 - cfg.w) * lex;
      return cfg.lexMode === 'none' ? cand.vec : vec;
    };

    const evalCfg = (split, cfg, rerankScores) => {
      const items = split === 'calib' ? [...calib.qa, ...calib.empty_queries.map((x) => ({ ...x, empty: true }))]
        : [...hold.qa, ...hold.empty_queries.map((x) => ({ ...x, empty: true }))];
      const cat = {}; let r5 = 0, emptyOk = 0;
      const lat = [];
      for (const item of items) {
        const pq = perQuery.find((p) => p.q.q === item.q);
        const qTokens = [...new Set(tokenize(item.q))];
        const t0 = Date.now();
        let ranked = pq.cands
          .map((cand) => ({ doc: cand.doc, s: scoreHybrid(cand, qTokens, cfg) }))
          .filter((x) => x.s >= cfg.floor)
          .sort((a, b) => b.s - a.s);
        if (rerankScores) {
          // rerank 档：先取 hybrid 前 30 → 交叉编码器重排（floor 作用于 logit）
          const top30 = pq.cands
            .map((cand) => ({ doc: cand.doc, text: cand.chunk.text, s: scoreHybrid(cand, qTokens, cfg) }))
            .sort((a, b) => b.s - a.s).slice(0, 30);
          ranked = top30
            .map((c) => ({ doc: c.doc, s: rerankScores.get(`${item.q}::${c.doc}::${c.chunk?.text?.slice(0, 40)}`) ?? rerankScores.get(`${item.q}::${c.text.slice(0, 40)}`) ?? -1e9 }))
            .filter((x) => x.s >= cfg.floor)
            .sort((a, b) => b.s - a.s);
        }
        lat.push(Date.now() - t0);
        const top5 = ranked.slice(0, 5);
        if (item.empty) { if (top5.length === 0) emptyOk++; }
        else {
          const hit = top5.some((x) => x.doc === item.expect_doc);
          if (hit) r5++;
          cat[item.cat] = cat[item.cat] ?? { t: 0, h: 0 };
          cat[item.cat].t++; if (hit) cat[item.cat].h++;
        }
      }
      const qaCount = items.filter((x) => !x.empty).length;
      const emptyCount = items.filter((x) => x.empty).length;
      return {
        recall_at_5: +(r5 / qaCount).toFixed(4),
        by_category: Object.fromEntries(Object.entries(cat).map(([c, v]) => [c, +(v.h / v.t).toFixed(4)])),
        empty_accuracy: +(emptyOk / emptyCount).toFixed(4),
        score_ms_p50: lat.sort((a, b) => a - b)[Math.floor(lat.length / 2)] ?? 0,
      };
    };

    // 网格（calib 选择）
    const grids = { emb: [], hyb: [] };
    for (const lexMode of ['none']) {
      for (let floor = 0.05; floor <= 0.9501; floor += 0.05) {
        const cfg = { w: 1, lexMode: 'none', floor: +floor.toFixed(2) };
        grids.emb.push({ cfg, ...evalCfg('calib', cfg) });
      }
    }
    for (const w of [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]) {
      for (const lexMode of ['plain', 'idf']) {
        for (let floor = 0.05; floor <= 0.9501; floor += 0.05) {
          const cfg = { w, lexMode, floor: +floor.toFixed(2) };
          grids.hyb.push({ cfg, ...evalCfg('calib', cfg) });
        }
      }
    }
    const pick = (list) => {
      const ok = list.filter((g) => g.empty_accuracy === 1 && (g.by_category.synonym ?? 1) >= 0.5 && (g.by_category.cross ?? 1) >= 0.5);
      if (ok.length) {
        ok.sort((a, b) => b.recall_at_5 - a.recall_at_5 || b.cfg.floor - a.cfg.floor || b.cfg.w - a.cfg.w);
        return { chosen: ok[0], eligible: true };
      }
      const fb = [...list].sort((a, b) => b.empty_accuracy - a.empty_accuracy || b.recall_at_5 - a.recall_at_5)[0];
      return { chosen: fb, eligible: false };
    };
    const embPick = pick(grids.emb);
    const hybPick = pick(grids.hyb);

    // rerank 档（限定对照组模型）
    let rrPick = null, rrTiming = null;
    if (RERANK_FOR.includes(model.id)) {
      // 用 chosen hybrid（或 emb 若更优）取 top-30 候选，一次性批量打分
      const baseCfg = (hybPick.eligible ? hybPick.chosen : embPick.chosen).cfg;
      const rows = []; const key = (q, c) => `${q}::${c.text.slice(0, 40)}`;
      const rrMapInput = new Map(); // key→score 填充
      for (const item of allQueries) {
        const pq = perQuery.find((p) => p.q.q === item.q);
        const qTokens = [...new Set(tokenize(item.q))];
        const top30 = pq.cands
          .map((cand) => ({ doc: cand.doc, text: cand.chunk.text, s: scoreHybrid(cand, qTokens, baseCfg) }))
          .sort((a, b) => b.s - a.s).slice(0, 30);
        for (const c of top30) {
          rows.push({ qid: key(item.q, c), query: item.q, passage: c.text });
        }
      }
      const t1 = Date.now();
      const scores = await pyRerank(rows);
      const t1ms = Date.now() - t1;
      rrTiming = { pairs: rows.length, seconds: +(t1ms / 1000).toFixed(1),
        per_pair_ms: +(t1ms / rows.length).toFixed(0) };
      // G-3 单位断言：per_pair_ms 必须等于 seconds/pairs*1000（防 ×1000 单位漂移复现）
      const expectMs = +((t1ms / 1000) / rows.length * 1000).toFixed(0);
      if (Math.abs(rrTiming.per_pair_ms - expectMs) > 1) {
        throw new Error(`per_pair_ms 单位断言失败: ${rrTiming.per_pair_ms} != ${expectMs}`);
      }
      // floor 网格（logit 域）
      const rrGrid = [];
      for (let floor = -6; floor <= 6.01; floor += 1) {
        const cfg = { rerank: true, base: baseCfg, floor: +floor.toFixed(1) };
        // 简化评估：复用 evalCfg 但注入 rerank 分数映射
        const items = [...calib.qa, ...calib.empty_queries.map((x) => ({ ...x, empty: true }))];
        const cat = {}; let r5 = 0, emptyOk = 0;
        for (const item of items) {
          const pq = perQuery.find((p) => p.q.q === item.q);
          const qTokens = [...new Set(tokenize(item.q))];
          const top30 = pq.cands
            .map((cand) => ({ doc: cand.doc, text: cand.chunk.text, s: scoreHybrid(cand, qTokens, baseCfg) }))
            .sort((a, b) => b.s - a.s).slice(0, 30);
          const ranked = top30
            .map((c) => ({ doc: c.doc, s: scores.get(key(item.q, c)) ?? -1e9 }))
            .filter((x) => x.s >= cfg.floor)
            .sort((a, b) => b.s - a.s)
            .slice(0, 5);
          if (item.empty) { if (ranked.length === 0) emptyOk++; }
          else {
            const hit = ranked.some((x) => x.doc === item.expect_doc);
            if (hit) r5++;
            cat[item.cat] = cat[item.cat] ?? { t: 0, h: 0 };
            cat[item.cat].t++; if (hit) cat[item.cat].h++;
          }
        }
        rrGrid.push({ cfg, recall_at_5: +(r5 / calib.qa.length).toFixed(4),
          by_category: Object.fromEntries(Object.entries(cat).map(([c, v]) => [c, +(v.h / v.t).toFixed(4)])),
          empty_accuracy: +(emptyOk / calib.empty_queries.length).toFixed(4) });
      }
      rrPick = pick(rrGrid);
      rrPick.chosen.holdout = null; // 稍后统一填
      // holdout for rerank chosen
      {
        const cfg = rrPick.chosen.cfg;
        const items = [...hold.qa, ...hold.empty_queries.map((x) => ({ ...x, empty: true }))];
        const cat = {}; let r5 = 0, emptyOk = 0;
        for (const item of items) {
          const pq = perQuery.find((p) => p.q.q === item.q);
          const qTokens = [...new Set(tokenize(item.q))];
          const top30 = pq.cands
            .map((cand) => ({ doc: cand.doc, text: cand.chunk.text, s: scoreHybrid(cand, qTokens, baseCfg) }))
            .sort((a, b) => b.s - a.s).slice(0, 30);
          const ranked = top30
            .map((c) => ({ doc: c.doc, s: scores.get(key(item.q, c)) ?? -1e9 }))
            .filter((x) => x.s >= cfg.floor)
            .sort((a, b) => b.s - a.s).slice(0, 5);
          if (item.empty) { if (ranked.length === 0) emptyOk++; }
          else {
            const hit = ranked.some((x) => x.doc === item.expect_doc);
            if (hit) r5++;
            cat[item.cat] = cat[item.cat] ?? { t: 0, h: 0 };
            cat[item.cat].t++; if (hit) cat[item.cat].h++;
          }
        }
        rrPick.chosen.holdout = {
          recall_at_5: +(r5 / hold.qa.length).toFixed(4),
          by_category: Object.fromEntries(Object.entries(cat).map(([c, v]) => [c, +(v.h / v.t).toFixed(4)])),
          empty_accuracy: +(emptyOk / hold.empty_queries.length).toFixed(4),
        };
      }
    }

    // holdout for emb/hyb chosen
    embPick.chosen.holdout = evalCfg('hold', embPick.chosen.cfg);
    hybPick.chosen.holdout = evalCfg('hold', hybPick.chosen.cfg);

    results[model.id] = { load_s: loadS, chunks: chunkVec.length, dims: chunkVec[0]?.length,
      emb: embPick, hyb: hybPick, rerank: rrPick, rerank_timing: rrTiming };
    log(`[matrix] ${model.id} (${chunkVec[0]?.length}d, load ${loadS}s)`);
    log(`   emb-only: eligible=${embPick.eligible} calib R@5=${embPick.chosen.recall_at_5} empty=${embPick.chosen.empty_accuracy} cat=${JSON.stringify(embPick.chosen.by_category)} | holdout R@5=${embPick.chosen.holdout.recall_at_5} empty=${embPick.chosen.holdout.empty_accuracy}`);
    log(`   hybrid  : eligible=${hybPick.eligible} calib R@5=${hybPick.chosen.recall_at_5} empty=${hybPick.chosen.empty_accuracy} cat=${JSON.stringify(hybPick.chosen.by_category)} | holdout R@5=${hybPick.chosen.holdout.recall_at_5} empty=${hybPick.chosen.holdout.empty_accuracy}`);
    if (rrPick) log(`   +rerank : eligible=${rrPick.eligible} calib R@5=${rrPick.chosen.recall_at_5} empty=${rrPick.chosen.empty_accuracy} cat=${JSON.stringify(rrPick.chosen.by_category)} | holdout R@5=${rrPick.chosen.holdout.recall_at_5} empty=${rrPick.chosen.holdout.empty_accuracy} | ${rrTiming.pairs} pairs ${rrTiming.per_pair_ms}ms/对`);
  }

  fs.writeFileSync(path.join(EVID, 'matrix.json'), JSON.stringify({ generated_at: new Date().toISOString(),
    rerank_model: RERANK.id, results }, null, 2));
  log(`evidence: ${EVID}`);
  await pool.end();
}
main().catch((e) => { console.error('FATAL', e); process.exit(1); });
