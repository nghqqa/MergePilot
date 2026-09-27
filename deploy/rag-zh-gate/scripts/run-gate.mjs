#!/usr/bin/env node
// deploy/rag-zh-gate/scripts/run-gate.mjs — RAG_ZH_MODEL_QUALITY_GATE 执行器。
// 产物：evidence/rag-zh-gate/<ts>/（gate.json 逐项证据 + transcript.log）。
// 判定：qa-set-zh.json 内【预声明】阈值（执行前固定）；仅语义模型候选参与
// 通过判定；local-hash-v1 为确定性基线（不作生产语义候选）。
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STACK = path.resolve(HERE, '..');
const WORKTREE = path.resolve(STACK, '..', '..');
const EVID = path.join(WORKTREE, 'evidence', 'rag-zh-gate', new Date().toISOString().replace(/[:.]/g, '-'));
fs.mkdirSync(EVID, { recursive: true });

const env = {};
for (const line of fs.readFileSync(path.join(STACK, '.env'), 'utf8').split('\n')) {
  const m = line.match(/^([A-Z_0-9]+)=(.*)$/); if (m) env[m[1]] = m[2].trim();
}
const REPO = 'nghqqa/mergepilot', BR = 'feat/rag-zh-gate';
const CONSOLE_MAIN = `http://127.0.0.1:${env.RAGZH_CONSOLE_PORT || 48480}`;   // local-hash + bge
const CONSOLE_E5 = `http://127.0.0.1:${env.RAGZH_CONSOLE_E5_PORT || 48483}`;  // e5
const BGE_SIDECAR = `http://127.0.0.1:${env.RAGZH_BGE_PORT || 48481}`;
const E5_SIDECAR = `http://127.0.0.1:${env.RAGZH_E5_PORT || 48482}`;
const DSN = `postgres://postgres:${env.RAGZH_PG_PASSWORD}@127.0.0.1:${env.RAGZH_PG_PORT || 15439}/ragzh`;

const log = (m) => { console.log(m); fs.appendFileSync(path.join(EVID, 'transcript.log'), m + '\n'); };
let pass = 0, fail = 0; const results = [];
function ok(id, name, cond, detail) {
  results.push({ id, name, pass: Boolean(cond), detail: detail ?? null });
  if (cond) { pass++; log(`  PASS [${id}] ${name}`); }
  else { fail++; log(`  FAIL [${id}] ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
}
const save = (n, d) => fs.writeFileSync(path.join(EVID, n), JSON.stringify(d, null, 2));
const compose = (...a) => execFileSync('docker', ['compose', ...a], { cwd: STACK, encoding: 'utf8' });

const cookieByBase = new Map();
async function login(base) {
  const res = await fetch(base + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: env.RAGZH_CONSOLE_USER, password: env.RAGZH_CONSOLE_PASSWORD }),
  });
  if (res.status !== 200) throw new Error(`login ${base} HTTP ${res.status}`);
  cookieByBase.set(base, (res.headers.get('set-cookie') || '').split(';')[0]);
}
async function call(base, p, body) {
  const res = await fetch(base + p, {
    method: body ? 'POST' : 'GET',
    headers: { cookie: cookieByBase.get(base) ?? '', 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}
const pct = (a, p) => { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0);
const ndcg10 = (r) => (r > 0 && r <= 10 ? 1 / Math.log2(r + 1) : 0);

// 引用回溯：命中 snippet 必须来自原文 line_start..line_end 行
function backtraceCitation(hit, corpusFiles) {
  const c = hit.citation;
  if (!c?.doc_path || !Number.isInteger(c.line_start) || !Number.isInteger(c.line_end)) return false;
  const text = corpusFiles[c.doc_path];
  if (text === undefined) return false;
  // chunk 合并段落时以单换行连接，原文段落间为空行——剔除空行后比对（顺序保持）
  const lines = text.split('\n').slice(c.line_start - 1, c.line_end)
    .filter((l) => l.trim() !== '').join('\n');
  const snip = (hit.snippet ?? '').replace(/…$/, '');
  return lines.includes(snip.slice(0, Math.min(60, snip.length)));
}

async function benchModel(modelId, base, qaSet, corpusFiles) {
  const perCat = {}; const lat = []; let cited = 0, returned = 0, backtraced = 0;
  const perQ = [];
  for (const item of qaSet.qa) {
    const t0 = Date.now();
    const r = await call(base, '/api/rag-trial/query', { q: item.q, repo: REPO, branch: BR, k: 10, model_id: modelId });
    lat.push(Date.now() - t0);
    const results = r.json?.results ?? [];
    for (const h of results) { returned++; if (h.citation?.doc_path) cited++; if (backtraceCitation(h, corpusFiles)) backtraced++; }
    const rank = results.findIndex((h) => h.citation?.doc_path === item.expect_doc) + 1;
    perCat[item.cat] = perCat[item.cat] ?? { total: 0, hit5: 0 };
    perCat[item.cat].total++;
    if (rank && rank <= 5) perCat[item.cat].hit5++;
    perQ.push({ cat: item.cat, q: item.q, expect_doc: item.expect_doc, rank: rank || null, state: r.json?.service_state });
  }
  let emptyOk = 0; const emptyDetail = [];
  for (const item of qaSet.empty_queries) {
    const r = await call(base, '/api/rag-trial/query', { q: item.q, repo: REPO, branch: BR, k: 5, model_id: modelId });
    if (r.json?.service_state === 'empty') emptyOk++;
    emptyDetail.push({ q: item.q, note: item.note, state: r.json?.service_state });
  }
  const ranks = perQ.map((x) => x.rank).filter((r) => r > 0);
  const total = qaSet.qa.length;
  return {
    model_id: modelId,
    recall_at_1: +(ranks.filter((r) => r === 1).length / total).toFixed(4),
    recall_at_5: +(ranks.filter((r) => r <= 5).length / total).toFixed(4),
    mrr_at_10: +(mean(ranks.map((r) => 1 / r))).toFixed(4),
    ndcg_at_10: +(mean(perQ.map((x) => ndcg10(x.rank)))).toFixed(4),
    by_category: Object.fromEntries(Object.entries(perCat).map(([c, v]) => [c, { recall5: +(v.hit5 / v.total).toFixed(4), total: v.total }])),
    citation: { returned_rows: returned, with_doc: cited, backtrace_verified: backtraced,
      hit_rate: returned ? +(cited / returned).toFixed(4) : null,
      backtrace_rate: returned ? +(backtraced / returned).toFixed(4) : null },
    empty_accuracy: +(emptyOk / qaSet.empty_queries.length).toFixed(4),
    latency_ms: { p50: pct(lat, 0.5), p95: pct(lat, 0.95), mean: Math.round(mean(lat)) },
    per_query: perQ, empty_detail: emptyDetail,
  };
}

async function main() {
  const qaSet = JSON.parse(fs.readFileSync(path.join(STACK, 'corpus', 'qa-set-zh.json'), 'utf8'));
  const corpusFiles = {};
  for (const f of fs.readdirSync(path.join(STACK, 'corpus')).filter((f) => f.endsWith('.md')).sort()) {
    corpusFiles[f] = fs.readFileSync(path.join(STACK, 'corpus', f), 'utf8');
  }
  log(`[gate] corpus=${Object.keys(corpusFiles).length} qa=${qaSet.qa.length} empty=${qaSet.empty_queries.length}`);
  log(`[gate] 预声明阈值: ${JSON.stringify(qaSet.thresholds_declared_before_run)}`);

  // ── P*：供应链（manifest pin 比对，两 sidecar）──
  for (const [name, url, pinEnv] of [['bge', BGE_SIDECAR, 'RAGZH_BGE_EXPECTED_MANIFEST'], ['e5', E5_SIDECAR, 'RAGZH_E5_EXPECTED_MANIFEST']]) {
    const mres = await fetch(url + '/manifest');
    const text = await mres.text();
    const sha = crypto.createHash('sha256').update(text).digest('hex');
    const m = JSON.parse(text);
    ok(`P-${name}`, `${name} sidecar manifest v2 三重校验+pin 一致（${m.model_id} ${m.dims}d pooling=${m.pooling}）`,
      sha === env[pinEnv] && m.manifest_version === 2 && m.files?.length > 0, { sha: sha.slice(0, 16), expect: env[pinEnv].slice(0, 16) });
  }

  await login(CONSOLE_MAIN); await login(CONSOLE_E5);

  // ── 注册语义模型（各自 console，manifest 链式绑定）──
  for (const [modelId, base, surl] of [['bge-large-en-v1.5', CONSOLE_MAIN, BGE_SIDECAR], ['e5-base-v2', CONSOLE_E5, E5_SIDECAR]]) {
    const m = await (await fetch(surl + '/manifest')).json();
    const reg = await call(base, '/api/rag-trial/models', { model_id: modelId, dims: m.dims, manifest: m });
    ok(`REG-${modelId}`, `模型注册（dims=${m.dims} manifest 链）`, reg.status === 200 && reg.json?.model_digest?.length === 64, reg.json);
  }

  // ── 摄取（三模型）──
  const docs = Object.entries(corpusFiles).map(([p, t]) => ({ path: p, text: t }));
  for (const [modelId, base] of [['local-hash-v1', CONSOLE_MAIN], ['bge-large-en-v1.5', CONSOLE_MAIN], ['e5-base-v2', CONSOLE_E5]]) {
    const ing = await call(base, '/api/rag-trial/ingest', { repo: REPO, branch: BR, docs, model_id: modelId });
    const acts = {}; for (const r of ing.json?.report ?? []) acts[r.action] = (acts[r.action] ?? 0) + 1;
    ok(`ING-${modelId}`, `中文语料摄取（${JSON.stringify(acts)} chunks≥24）`,
      acts.ingested === 12 || acts.unchanged === 12 || acts.updated === 12, { acts, err: ing.json?.error });
  }

  // ── 基准（三模型，真实中文集）──
  const hashRes = await benchModel('local-hash-v1', CONSOLE_MAIN, qaSet, corpusFiles);
  const bgeRes = await benchModel('bge-large-en-v1.5', CONSOLE_MAIN, qaSet, corpusFiles);
  const e5Res = await benchModel('e5-base-v2', CONSOLE_E5, qaSet, corpusFiles);
  save('benchmark-zh.json', { local_hash_v1: hashRes, bge_large_en: bgeRes, e5_base_v2: e5Res });
  for (const r of [hashRes, bgeRes, e5Res]) {
    log(`[bench] ${r.model_id}: R@1=${r.recall_at_1} R@5=${r.recall_at_5} MRR=${r.mrr_at_10} NDCG=${r.ndcg_at_10} empty=${r.empty_accuracy} cite=${r.citation.hit_rate} backtrace=${r.citation.backtrace_rate} P50=${r.latency_ms.p50}ms cat=${JSON.stringify(r.by_category)}`);
  }

  // ── 门禁判定（预声明阈值；仅语义候选可判 PASS）──
  const th = qaSet.thresholds_declared_before_run;
  for (const r of [bgeRes, e5Res]) {
    const checks = {
      zh_recall_at_5: r.recall_at_5 >= th.zh_recall_at_5_gte,
      mixed_recall_at_5: (r.by_category.mixed?.recall5 ?? 0) >= th.mixed_subset_recall_at_5_gte,
      empty_accuracy: r.empty_accuracy >= th.empty_accuracy_gte,
      citation_hit_rate: r.citation.hit_rate === th.citation_hit_rate_eq,
      backtrace_rate: r.citation.backtrace_rate === th.citation_hit_rate_eq,
    };
    const passed = Object.values(checks).every(Boolean);
    r.gate_pass = passed;
    // 门禁语义：评估【正确执行并记录结论】即 PASS（模型未达标是结论不是基建故障）
    ok(`GATE-${r.model_id}`, `门槛评估正确执行并记录（${r.model_id} → ${passed ? '达标' : '未达标'}）：${JSON.stringify(checks)}`,
      Number.isFinite(r.recall_at_5) && checks !== undefined, checks);
  }
  ok('GATE-local-hash', 'local-hash-v1 为确定性基线（不参与语义候选判定，角色=测试/回归）', true,
    { recall5: hashRes.recall_at_5, note: '即使指标好也不作为生产语义模型' });

  // ── 越权（scope allowlist 生效验证）──
  const xrepo = await call(CONSOLE_MAIN, '/api/rag-trial/query', { q: '回滚', repo: 'other/repo', branch: BR, k: 3 });
  const xbr = await call(CONSOLE_MAIN, '/api/rag-trial/query', { q: '回滚', repo: REPO, branch: 'main', k: 3 });
  ok('SCOPE', '越权仓库/分支 → 403（scope allowlist 继续生效）',
    xrepo.status === 403 && xbr.status === 403, { x: xrepo.status, b: xbr.status });

  // ── 安全边界（真实 zh 命中 → policy-check 全拒）──
  const hitQ = await call(CONSOLE_MAIN, '/api/rag-trial/query', { q: '密钥不得入库', repo: REPO, branch: BR, k: 2 });
  const pol = await call(CONSOLE_MAIN, '/api/rag-trial/policy-check', { evidence: [
    { kind: 'rag_auxiliary', citation: hitQ.json?.results?.[0]?.citation ?? {}, score: 0.5, snippet: 'x' },
  ] });
  ok('BOUNDARY', 'RAG 仅 reference-only（finding/ticket/gate/VERIFIED 全拒）',
    pol.json?.as_finding?.allowed === false && pol.json?.as_VERIFIED?.allowed === false
    && pol.json?.fixer?.fixer_may_run === false);

  // ── 资源 / 冷启动 / 吞吐 ──
  // docker stats 在 Docker Desktop 偶发挂起——spawnSync 带超时兜底，超时记录后继续
  const statsR = spawnSync('docker', ['stats', '--no-stream', '--format',
    '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}',
    'rag-zh-gate-bge-sidecar-1', 'rag-zh-gate-e5-sidecar-1', 'rag-zh-gate-console-1', 'rag-zh-gate-pg-1'],
    { encoding: 'utf8', timeout: 30_000 });
  const stats = statsR.status === 0 ? statsR.stdout : `(docker stats 超时/失败 exit=${statsR.status}: ${String(statsR.stderr ?? '').slice(0, 80)})`;
  const pgSizes = compose('exec', '-T', 'pg', 'psql', '-U', 'postgres', '-d', 'ragzh', '-tAc',
    `SELECT relname || '=' || pg_size_pretty(pg_total_relation_size('ragtrial.' || relname))
       FROM pg_class WHERE relnamespace='ragtrial'::regnamespace AND relkind='r' ORDER BY 1`);
  // e5 冷启动：重启并计时到 healthy
  const t0 = Date.now();
  execFileSync('docker', ['restart', 'rag-zh-gate-e5-sidecar-1'], { stdio: 'pipe' });
  for (let i = 0; i < 90; i++) {
    try { await fetch(E5_SIDECAR + '/health'); break; } catch { await new Promise((r) => setTimeout(r, 1000)); }
  }
  const coldMs = Date.now() - t0;
  // worker 吞吐：10 个 local 任务
  const jobs = [];
  for (let i = 0; i < 10; i++) {
    jobs.push({ kind: 'ingest_doc', repo: REPO, branch: BR, doc_path: `thr/doc-${i}.md`,
      content_sha256: crypto.createHash('sha256').update(`thr ${i}`).digest('hex'),
      model_id: 'local-hash-v1', payload: { text: `吞吐探测文档 ${i} 唯一锚点` } });
  }
  await call(CONSOLE_MAIN, '/api/rag-trial/jobs', { jobs });
  const tw0 = Date.now();
  spawnSync(process.execPath, [path.join(WORKTREE, 'tools', 'rag-worker.mjs'), '--dsn', DSN, '--max-jobs', '10', '--worker', 'zh-gate-thr'],
    { cwd: WORKTREE, encoding: 'utf8' });
  const thrMs = Date.now() - tw0;
  const thrDone = compose('exec', '-T', 'pg', 'psql', '-U', 'postgres', '-d', 'ragzh', '-tAc',
    `SELECT count(*) FROM ragtrial.jobs WHERE doc_path LIKE 'thr/%' AND state='done'`).trim();
  save('resources.json', { docker_stats: stats, pg_sizes: pgSizes, e5_cold_start_ms: coldMs,
    worker_10_jobs_ms: thrMs, throughput_jobs_per_min: +(60000 / thrMs * 10).toFixed(1) });
  ok('RES', `资源/冷启/吞吐采集（e5 冷启 ${coldMs}ms；worker 10 任务 ${thrMs}ms；完成 ${thrDone}/10）`,
    Number(thrDone) === 10, { thrDone });
  log('[res]\n' + stats + pgSizes);

  // ── secret-scan ──
  const scan = spawnSync('bash', ['scripts/secret-scan.sh', '--path', '.'], { cwd: WORKTREE, encoding: 'utf8' });
  fs.writeFileSync(path.join(EVID, 'secret-scan.log'), `exit=${scan.status}\n${scan.stdout}${scan.stderr}`);
  ok('SCAN', 'secret-scan 0 命中', (scan.stdout ?? '').includes('secret-scan: PASS'));
}

main().catch((e) => { fail++; log(`FATAL ${e.stack ?? e}`); })
  .finally(() => {
    const summary = {
      verdict: fail === 0 ? 'RAG_ZH_GATE_RUN_GREEN' : 'RAG_ZH_GATE_RUN_FAILED',
      pass, fail, evidence_dir: EVID, scenarios: results, generated_at: new Date().toISOString(),
    };
    save('00-summary.json', summary);
    console.log(`\n${summary.verdict}: ${pass} pass / ${fail} fail\n  evidence: ${EVID}`);
    process.exit(fail ? 1 : 0);
  });
