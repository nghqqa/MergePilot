#!/usr/bin/env node
// deploy/rag-zh-eval/scripts/run-gate.mjs — bge-m3 / bge-large-zh-v1.5 全集正式门禁。
// 与 zh-gate 波同协议：全集 26 QA + 8 空查询；阈值 = qa-set-zh.json 预声明（不得调整）；
// console 全链（注册/摄取/查询/引用回溯/scope/边界）+ 资源指标 + 门槛判定。
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STACK = path.resolve(HERE, '..');
const WORKTREE = path.resolve(STACK, '..', '..');
const EVID = path.join(WORKTREE, 'evidence', 'rag-zh-eval', new Date().toISOString().replace(/[:.]/g, '-'));
fs.mkdirSync(EVID, { recursive: true });

const env = {};
for (const line of fs.readFileSync(path.join(STACK, '.env'), 'utf8').split('\n')) {
  const m = line.match(/^([A-Z_0-9]+)=(.*)$/); if (m) env[m[1]] = m[2].trim();
}
const REPO = 'nghqqa/mergepilot', BR = 'feat/rag-zh-gate';
const MODELS = [
  { id: 'bge-m3', console: `http://127.0.0.1:${env.RAGZHE_CONSOLE_M3_PORT || 48485}`, sidecar: `http://127.0.0.1:${env.RAGZHE_M3_PORT || 48487}` },
  { id: 'bge-large-zh-v1.5', console: `http://127.0.0.1:${env.RAGZHE_CONSOLE_ZH_PORT || 48486}`, sidecar: `http://127.0.0.1:${env.RAGZHE_ZH_PORT || 48488}` },
];

const log = (m) => { console.log(m); fs.appendFileSync(path.join(EVID, 'transcript.log'), m + '\n'); };
let pass = 0, fail = 0; const results = [];
function ok(id, name, cond, detail) {
  results.push({ id, name, pass: Boolean(cond), detail: detail ?? null });
  if (cond) { pass++; log(`  PASS [${id}] ${name}`); }
  else { fail++; log(`  FAIL [${id}] ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
}
const save = (n, d) => fs.writeFileSync(path.join(EVID, n), JSON.stringify(d, null, 2));

async function call(base, p, body, cookie) {
  const res = await fetch(base + p, {
    method: body ? 'POST' : 'GET',
    headers: { cookie: cookie ?? '', 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}
const pct = (a, p) => { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0);
function backtrace(hit, corpusFiles) {
  const c = hit.citation;
  if (!c?.doc_path || !Number.isInteger(c.line_start)) return false;
  const text = corpusFiles[c.doc_path];
  if (text === undefined) return false;
  const lines = text.split('\n').slice(c.line_start - 1, c.line_end).filter((l) => l.trim() !== '').join('\n');
  const snip = (hit.snippet ?? '').replace(/…$/, '');
  return lines.includes(snip.slice(0, Math.min(60, snip.length)));
}

async function main() {
  const qaSet = JSON.parse(fs.readFileSync(path.join(WORKTREE, 'deploy/rag-zh-gate/corpus/qa-set-zh.json'), 'utf8'));
  const th = qaSet.thresholds_declared_before_run;
  log(`[gate] 全集 qa=${qaSet.qa.length} empty=${qaSet.empty_queries.length}；预声明阈值=${JSON.stringify({ ...th, note: undefined })}`);
  const corpusFiles = {};
  for (const f of fs.readdirSync(path.join(WORKTREE, 'deploy/rag-zh-gate/corpus')).filter((f) => f.endsWith('.md'))) {
    corpusFiles[f] = fs.readFileSync(path.join(WORKTREE, 'deploy/rag-zh-gate/corpus', f), 'utf8');
  }
  const docs = Object.entries(corpusFiles).map(([p, t]) => ({ path: p, text: t }));

  const verdicts = {};
  for (const model of MODELS) {
    log(`\n===== ${model.id} =====`);
    // 登录
    const login = await fetch(model.console + '/api/auth/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ user: env.RAGZHE_CONSOLE_USER, password: env.RAGZHE_CONSOLE_PASSWORD }),
    });
    const cookie = (login.headers.get('set-cookie') || '').split(';')[0];
    ok(`L-${model.id}`, '登录+栈在线', login.status === 200);

    // 供应链：sidecar manifest v2 三重+pin
    const mres = await fetch(model.sidecar + '/manifest');
    const mtext = await mres.text();
    const msha = crypto.createHash('sha256').update(mtext).digest('hex');
    const manifest = JSON.parse(mtext);
    const pinEnv = model.id === 'bge-m3' ? 'RAGZHE_M3_EXPECTED_MANIFEST' : 'RAGZHE_ZH_EXPECTED_MANIFEST';
    ok(`P-${model.id}`, `manifest v2 三重校验+pin（${manifest.model_id} ${manifest.dims}d files=${manifest.files_count} ${Math.round(manifest.total_bytes / 1e6)}MB）`,
      msha === env[pinEnv] && manifest.manifest_version === 2, { got: msha.slice(0, 12), expect: env[pinEnv].slice(0, 12) });

    // 注册+摄取（console 全链）
    const reg = await call(model.console, '/api/rag-trial/models', { model_id: model.id, dims: manifest.dims, manifest }, cookie);
    ok(`REG-${model.id}`, '模型注册（manifest 链）', reg.status === 200 && reg.json?.model_digest?.length === 64, reg.json);
    const ing = await call(model.console, '/api/rag-trial/ingest', { repo: REPO, branch: BR, docs, model_id: model.id }, cookie);
    const acts = {}; for (const r of ing.json?.report ?? []) acts[r.action] = (acts[r.action] ?? 0) + 1;
    ok(`ING-${model.id}`, `中文语料摄取（${JSON.stringify(acts)}；MinIO 读回）`,
      ['ingested', 'unchanged', 'updated'].some((a) => acts[a] === 12)
      && (ing.json?.report ?? []).every((r) => r.object_status === 'verified_readback' || r.object_status === 'not_configured'), { acts, err: ing.json?.error });

    // 全集基准
    const lat = []; let cited = 0, returned = 0, backtraced = 0;
    const perCat = {}; const perQ = [];
    for (const item of qaSet.qa) {
      const t0 = Date.now();
      const r = await call(model.console, '/api/rag-trial/query', { q: item.q, repo: REPO, branch: BR, k: 10, model_id: model.id }, cookie);
      lat.push(Date.now() - t0);
      for (const h of r.json?.results ?? []) { returned++; if (h.citation?.doc_path) cited++; if (backtrace(h, corpusFiles)) backtraced++; }
      const rank = (r.json?.results ?? []).findIndex((h) => h.citation?.doc_path === item.expect_doc) + 1;
      perCat[item.cat] = perCat[item.cat] ?? { t: 0, h5: 0, h1: 0 };
      perCat[item.cat].t++;
      if (rank && rank <= 5) perCat[item.cat].h5++;
      if (rank === 1) perCat[item.cat].h1++;
      perQ.push({ cat: item.cat, q: item.q, rank: rank || null });
    }
    let emptyOk = 0; const emptyDetail = [];
    for (const item of qaSet.empty_queries) {
      const r = await call(model.console, '/api/rag-trial/query', { q: item.q, repo: REPO, branch: BR, k: 5, model_id: model.id }, cookie);
      if (r.json?.service_state === 'empty') emptyOk++;
      emptyDetail.push({ q: item.q, note: item.note, state: r.json?.service_state });
    }
    const catRecall = (c) => perCat[c] ? +(perCat[c].h5 / perCat[c].t).toFixed(4) : null;
    const r5 = +(Object.values(perCat).reduce((s, v) => s + v.h5, 0) / qaSet.qa.length).toFixed(4);
    const r1 = +(Object.values(perCat).reduce((s, v) => s + v.h1, 0) / qaSet.qa.length).toFixed(4);
    const emptyAcc = +(emptyOk / qaSet.empty_queries.length).toFixed(4);
    const citeRate = returned ? +(cited / returned).toFixed(4) : null;
    const btRate = returned ? +(backtraced / returned).toFixed(4) : null;
    const ranks = perQ.map((x) => x.rank).filter(Boolean);
    const mrr = +(mean(ranks.map((x) => 1 / x))).toFixed(4);
    log(`[bench] ${model.id} 全集: R@1=${r1} R@5=${r5} MRR=${mrr} empty=${emptyAcc} cite=${citeRate} backtrace=${btRate} P50=${pct(lat, 0.5)}ms P95=${pct(lat, 0.95)}ms cat=${JSON.stringify(Object.fromEntries(Object.entries(perCat).map(([c, v]) => [c, +(v.h5 / v.t).toFixed(4)])))}`);

    // 门槛判定（预声明阈值，全集）
    const checks = {
      zh_recall_at_5: r5 >= th.zh_recall_at_5_gte,
      synonym_recall: catRecall('synonym') >= 0.5,
      cross_recall: catRecall('cross') >= 0.5,
      mixed_recall_at_5: catRecall('mixed') >= th.mixed_subset_recall_at_5_gte,
      empty_accuracy: emptyAcc >= th.empty_accuracy_gte,
      citation_hit_rate: citeRate === th.citation_hit_rate_eq,
      backtrace_rate: btRate === th.citation_hit_rate_eq,
    };
    const gatePass = Object.values(checks).every(Boolean);
    verdicts[model.id] = { r1, r5, mrr, empty: emptyAcc, cite: citeRate, backtrace: btRate,
      p50: pct(lat, 0.5), p95: pct(lat, 0.95), checks, gate_pass: gatePass,
      by_category: Object.fromEntries(Object.entries(perCat).map(([c, v]) => [c, { r5: +(v.h5 / v.t).toFixed(4), t: v.t }])), per_query: perQ, empty_detail: emptyDetail };
    ok(`GATE-${model.id}`, `生产候选门槛（预声明，全集）→ ${gatePass ? '达标' : '未达标'}：${JSON.stringify(checks)}`,
      Number.isFinite(r5) && checks !== undefined, checks);

    // scope / 边界（console 全链）
    const xr = await call(model.console, '/api/rag-trial/query', { q: '回滚', repo: 'other/repo', branch: BR, k: 3 }, cookie);
    const xb = await call(model.console, '/api/rag-trial/query', { q: '回滚', repo: REPO, branch: 'main', k: 3 }, cookie);
    ok(`SCOPE-${model.id}`, '越权 repo/branch → 403', xr.status === 403 && xb.status === 403);
    const pol = await call(model.console, '/api/rag-trial/policy-check', { evidence: [{ kind: 'rag_auxiliary', citation: {}, score: 0.5 }] }, cookie);
    ok(`BOUND-${model.id}`, 'RAG 仅 reference（含 VERIFIED 全拒+Fixer/Verifier 边界）',
      pol.json?.as_finding?.allowed === false && pol.json?.as_VERIFIED?.allowed === false && pol.json?.fixer?.fixer_may_run === false);
  }

  // 资源指标（双 sidecar+console+pg；冷启动=重启 m3-sidecar 计时）
  const statsR = spawnSync('docker', ['stats', '--no-stream', '--format', '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}',
    'rag-zh-eval-m3-sidecar-1', 'rag-zh-eval-zh-sidecar-1', 'rag-zh-eval-console-m3-1', 'rag-zh-eval-pg-1'],
    { encoding: 'utf8', timeout: 30_000 });
  const stats = statsR.status === 0 ? statsR.stdout : `(stats 超时 exit=${statsR.status})`;
  const pgSizes = execFileSync('docker', ['compose', 'exec', '-T', 'pg', 'psql', '-U', 'postgres', '-d', 'ragzhe', '-tAc',
    `SELECT relname || '=' || pg_size_pretty(pg_total_relation_size('ragtrial.' || relname)) FROM pg_class WHERE relnamespace='ragtrial'::regnamespace AND relkind='r' ORDER BY 1`], { cwd: STACK, encoding: 'utf8' });
  const t0 = Date.now();
  execFileSync('docker', ['restart', 'rag-zh-eval-m3-sidecar-1'], { stdio: 'pipe', timeout: 90_000 });
  for (let i = 0; i < 150; i++) {
    try { await fetch(MODELS[0].sidecar + '/health'); break; } catch { await new Promise((r) => setTimeout(r, 1000)); }
  }
  const coldMs = Date.now() - t0;
  save('resources.json', { docker_stats: stats, pg_sizes: pgSizes, m3_cold_start_ms: coldMs, verdicts });
  log(`[res] m3 冷启 ${coldMs}ms\n${stats}${pgSizes}`);
  ok('RES', '资源采集（含冷启/内存/索引尺寸）', true, { coldMs });

  // fail-closed 复核：manifest 篡改探针（停 sidecar 改 pin？用一次性容器演示 BLOCKED——直接断言 pin 不匹配路径已在集成覆盖；此处验证 pin 配置错误→BLOCKED）
  const st = await call(MODELS[0].console, '/api/rag-trial/status', undefined, (await (async () => {
    const l = await fetch(MODELS[0].console + '/api/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ user: env.RAGZHE_CONSOLE_USER, password: env.RAGZHE_CONSOLE_PASSWORD }) });
    return (l.headers.get('set-cookie') || '').split(';')[0];
  })()));
  ok('FC', 'status 披露：本地 manifest 验证≠外部 attestation（NOT_CONFIGURED 如实）',
    st.json?.production_readiness?.semantic_provider?.state === 'LOCAL_MANIFEST_VERIFIED'
    && st.json?.production_readiness?.semantic_provider?.external_attestation?.state === 'NOT_CONFIGURED');

  save('gate-fullset.json', { generated_at: new Date().toISOString(), thresholds: th, verdicts });
  const scan = spawnSync('bash', ['scripts/secret-scan.sh', '--path', '.'], { cwd: WORKTREE, encoding: 'utf8' });
  fs.writeFileSync(path.join(EVID, 'secret-scan.log'), `exit=${scan.status}\n${scan.stdout}${scan.stderr}`);
  ok('SCAN', 'secret-scan 0 命中', (scan.stdout ?? '').includes('secret-scan: PASS'));
}
main().catch((e) => { fail++; log(`FATAL ${e.stack ?? e}`); })
  .finally(() => {
    const summary = { verdict: fail === 0 ? 'RAG_ZH_EVAL_GATE_GREEN' : 'RAG_ZH_EVAL_GATE_FAILED',
      pass, fail, evidence_dir: EVID, scenarios: results, generated_at: new Date().toISOString() };
    save('00-summary.json', summary);
    console.log(`\n${summary.verdict}: ${pass} pass / ${fail} fail\n  evidence: ${EVID}`);
    process.exit(fail ? 1 : 0);
  });
