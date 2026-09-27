#!/usr/bin/env node
// deploy/local-rag-trial/scripts/run-e2e.mjs — LOCAL_RAG_TRIAL 栈上 15 项必须测试 + 联调证据。
//
// 前置：deploy/local-rag-trial 栈已 up（docker compose up -d --build）且 .env 就绪。
// 产物：evidence/local-rag-trial/<ts>/（summary.json / manifest.json / transcript.log / *.json）
// 边界：只操作 local-rag-trial project；绝不触碰 promote*/fxv-stage/mp-stage/coreb 栈；
//       一次性本地凭据来自 .env；输出不含任何真实凭据。

import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STACK = path.resolve(HERE, '..');
const WORKTREE = path.resolve(STACK, '..', '..');
const EVID = path.join(WORKTREE, 'evidence', 'local-rag-trial', new Date().toISOString().replace(/[:.]/g, '-'));
fs.mkdirSync(EVID, { recursive: true });

// ── .env（仅本地一次性值） ──
const env = {};
for (const line of fs.readFileSync(path.join(STACK, '.env'), 'utf8').split('\n')) {
  const m = line.match(/^([A-Z_]+)=(.*)$/);
  if (m) env[m[1]] = m[2].trim();
}
const CONSOLE = `http://127.0.0.1:${env.RAGTRIAL_CONSOLE_PORT || 48440}`;
const REPO = 'nghqqa/mergepilot', BR = 'feat/local-rag-trial';

const transcript = fs.createWriteStream(path.join(EVID, 'transcript.log'), { flags: 'a' });
const log = (msg) => { console.log(msg); transcript.write(msg + '\n'); };

let pass = 0, fail = 0;
const results = [];
function ok(id, name, cond, detail) {
  const r = { id, name, pass: Boolean(cond), detail: detail ?? null };
  results.push(r);
  if (cond) { pass++; log(`  PASS [${id}] ${name}`); }
  else { fail++; log(`  FAIL [${id}] ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 300) : ''}`); }
  return r;
}

const compose = (...args) => execFileSync('docker', ['compose', ...args], { cwd: STACK, encoding: 'utf8' });
const psql = (sql) => compose('exec', '-T', 'pg', 'psql', '-U', 'postgres', '-d', 'ragtrial', '-tAc', sql);

let cookie = null;
async function call(method, p, body) {
  const res = await fetch(CONSOLE + p, {
    method,
    headers: { cookie: cookie ?? '', 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}
const api = {
  login: async () => {
    const res = await fetch(CONSOLE + '/api/auth/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ user: env.RAGTRIAL_CONSOLE_USER, password: env.RAGTRIAL_CONSOLE_PASSWORD }),
    });
    if (res.status !== 200) throw new Error(`login HTTP ${res.status}`);
    cookie = (res.headers.get('set-cookie') || '').split(';')[0];
    return cookie;
  },
  query: (body) => call('POST', '/api/rag-trial/query', body),
  ingest: (body) => call('POST', '/api/rag-trial/ingest', body),
  del: (body) => call('POST', '/api/rag-trial/delete', body),
  eval: (body) => call('POST', '/api/rag-trial/eval', body),
  status: () => call('GET', '/api/rag-trial/status'),
  metrics: () => call('GET', '/api/rag-trial/metrics'),
  invalidate: () => call('POST', '/api/rag-trial/index/invalidate', {}),
  rollback: (to) => call('POST', '/api/rag-trial/index/rollback', { to_index_version: to }),
  reviewAux: (body) => call('POST', '/api/rag-trial/review-aux', body),
  policy: (body) => call('POST', '/api/rag-trial/policy-check', body),
};

const save = (name, data) => fs.writeFileSync(path.join(EVID, name), JSON.stringify(data, null, 2));
const sha256file = (p) => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');

async function waitHealthy() {
  const deadline = Date.now() + 120_000;
  for (;;) {
    try {
      const h = await (await fetch(CONSOLE + '/api/health')).json();
      if (h) return true;
    } catch { /* not up */ }
    if (Date.now() > deadline) throw new Error('console health timeout');
    await new Promise((r) => setTimeout(r, 1500));
  }
}

try {
  // ── Phase 0：preflight ──
  await waitHealthy();
  await api.login();
  const st0 = await api.status();
  ok('P0', '栈在线 + 登录 + status（pgvector 扩展就绪）',
    st0.status === 200 && st0.json?.pgvector?.extname === 'vector',
    st0.json?.pgvector);

  // ── Phase A：数据链（ingest → 检索证据） ──
  const ing1 = await api.ingest({ repo: REPO, branch: BR, corpus_dir: '/app/rag-corpus' });
  save('01-ingest-corpus.json', ing1.json);
  const ingested = ing1.json?.report?.filter((x) => x.action === 'ingested') ?? [];
  ok('A1', `语料摄取（${ingested.length} 文档，MinIO 读回校验）`,
    ing1.status === 200 && ingested.length >= 7
      && ingested.every((x) => x.object_status === 'verified_readback' || x.object_status === 'not_configured'));

  const dup = await api.ingest({ repo: REPO, branch: BR, corpus_dir: '/app/rag-corpus' });
  ok('S5', '重复 ingest 幂等（全部 unchanged）',
    dup.json?.report?.every((x) => x.action === 'unchanged') === true);

  const hitQ = await api.query({ q: '回滚的第一步是什么', repo: REPO, branch: BR, k: 3 });
  save('02-retrieval-hit.json', hitQ.json);
  const citeOk = (hitQ.json?.results ?? []).every((h) => h.citation?.doc_path && h.citation?.line_start >= 1
    && h.citation?.line_end >= h.citation?.line_start && h.citation?.doc_sha256?.length === 64
    && h.citation?.model_digest?.length === 64 && Number.isInteger(h.citation?.index_version));
  ok('A2', '命中带完整引用链（file+line+digest+version）',
    hitQ.json?.service_state === 'hit' && (hitQ.json?.results ?? []).length > 0 && citeOk);

  const emptyQ = await api.query({ q: 'qwijbhvx zkmpf 纯乱码零重叠探测', repo: REPO, branch: BR, k: 3 });
  ok('S10', '空结果诚实 empty（score floor 生效）',
    emptyQ.json?.service_state === 'empty' && (emptyQ.json?.results ?? []).length === 0,
    { state: emptyQ.json?.service_state, floor: emptyQ.json?.score_floor });

  const emptyDoc = await api.ingest({ repo: REPO, branch: BR, docs: [{ path: 'trial/blank.md', text: '\n\n  \n' }] });
  ok('S4', '空文档摄取 0 chunk 不崩', emptyDoc.json?.report?.[0]?.chunks === 0);

  const wrongRepo = await api.query({ q: '回滚的第一步', repo: 'other/repo', branch: BR, k: 3 });
  const wrongBranch = await api.query({ q: '回滚的第一步', repo: REPO, branch: 'main', k: 3 });
  ok('S9', 'wrong repo/branch 零泄漏（scoped empty）',
    wrongRepo.json?.service_state === 'empty' && wrongBranch.json?.service_state === 'empty'
      && (wrongRepo.json?.results ?? []).length === 0 && (wrongBranch.json?.results ?? []).length === 0);

  const qaSet = JSON.parse(fs.readFileSync(path.join(STACK, 'corpus', 'qa-set.json'), 'utf8'));
  const ev = await api.eval({ ...qaSet });
  save('03-eval-recall.json', ev.json);
  ok('A3', `QA 评测 Recall@5 = ${ev.json?.recall_at_k}（${ev.json?.hit_at_k}/${ev.json?.total}）`,
    ev.status === 200 && Number.isFinite(ev.json?.recall_at_k) && ev.json.recall_at_k >= 0.7,
    { recall: ev.json?.recall_at_k });

  const mm = await api.query({ q: '回滚', repo: REPO, branch: BR, k: 3, model_id: 'ghost-model' });
  ok('S1', '模型缺失 → model_missing（不伪装）',
    mm.json?.service_state === 'model_missing' && (mm.json?.results ?? []).length === 0);

  // ── Phase A2：Review 联调（辅助证据 + 策略拒绝） ──
  const aux = await api.reviewAux({ q: '验证者接受哪些证据类型', repo: REPO, branch: BR, run_id: 'trial-run-001' });
  save('04-review-aux.json', aux.json);
  const auxItems = aux.json?.run_view?.review_context?.rag_auxiliary ?? [];
  ok('R1', 'Review 辅助证据：reference only + findings/tickets/gates 未触碰',
    aux.json?.service_state === 'hit' && auxItems.length > 0
      && auxItems.every((a) => a.usage === 'reference_only' && a.trusted === false)
      && Array.isArray(aux.json.run_view.findings) && aux.json.run_view.findings.length === 0
      && aux.json.run_view.tickets.length === 0 && aux.json.run_view.gates.length === 0);

  const ragEvidence = auxItems.slice(0, 2);
  const pol = await api.policy({ evidence: ragEvidence });
  save('05-policy-check.json', pol.json);
  ok('R2', '策略拒绝：RAG-only 建 finding/ticket/gate 全拒 + Fixer 不可启动 + Verifier 拒收',
    pol.json?.as_finding?.allowed === false && pol.json?.as_ticket?.allowed === false
      && pol.json?.as_gate?.allowed === false && pol.json?.fixer?.fixer_may_run === false
      && pol.json?.verifier?.every((v) => v.accepted === false) === true);

  // ── Phase B：篡改类（每个场景后恢复） ──
  const F64 = 'f'.repeat(64);
  psql(`UPDATE ragtrial.chunks SET model_digest='${F64}' WHERE doc_path='security-baseline.md'`);
  const driftHit = await api.query({ q: '灰度发布批次观察指标', repo: REPO, branch: BR, k: 5 });
  psql(`UPDATE ragtrial.chunks SET model_digest=(SELECT model_digest FROM ragtrial.models WHERE active)`);
  ok('S2a', 'digest 部分漂移：hit + drifted_rows 上报',
    driftHit.json?.service_state === 'hit' && driftHit.json?.drifted_rows > 0);

  psql(`UPDATE ragtrial.chunks SET model_digest='${F64}'`);
  const driftAll = await api.query({ q: '回滚的第一步是什么', repo: REPO, branch: BR, k: 3 });
  psql(`UPDATE ragtrial.chunks SET model_digest=(SELECT model_digest FROM ragtrial.models WHERE active)`);
  const recovered = await api.query({ q: '回滚的第一步是什么', repo: REPO, branch: BR, k: 3 });
  ok('S2b', 'digest 全量漂移 → index_stale；恢复后 hit 回归',
    driftAll.json?.service_state === 'index_stale' && recovered.json?.service_state === 'hit');

  psql(`UPDATE ragtrial.chunks SET line_start=NULL, line_end=NULL WHERE doc_path='api-states.md'`);
  const uncited = await api.query({ q: '六种服务状态', repo: REPO, branch: BR, k: 5 });
  psql(`UPDATE ragtrial.chunks SET line_start=1, line_end=10 WHERE doc_path='api-states.md' AND line_start IS NULL`);
  ok('S11', '引用缺失行被丢弃（dropped_uncited>0，无无引用命中）',
    uncited.json?.dropped_uncited > 0
      && (uncited.json?.results ?? []).every((h) => h.citation.doc_path !== 'api-states.md'));

  // ── Phase C：增量更新 + 删除 ──
  await api.ingest({ repo: REPO, branch: BR, docs: [{ path: 'trial/incremental.md', text: '初版甲段：冬眠河马基准。' }] });
  const inc2 = await api.ingest({ repo: REPO, branch: BR, docs: [{ path: 'trial/incremental.md', text: '二版乙段：觉醒天鹅改写。' }] });
  const incQ = await api.query({ q: '觉醒天鹅', repo: REPO, branch: BR, k: 3 });
  const incOld = await api.query({ q: '冬眠河马基准', repo: REPO, branch: BR, k: 3 });
  ok('S6', '增量更新：updated + 新内容可检索 + 旧内容出索引',
    inc2.json?.report?.[0]?.action === 'updated' && incQ.json?.service_state === 'hit'
      && (incQ.json?.results ?? []).some((h) => h.citation.doc_path === 'trial/incremental.md')
      && !(incOld.json?.results ?? []).some((h) => h.citation.doc_path === 'trial/incremental.md'),
    { action: inc2.json?.report?.[0]?.action, inc_state: incQ.json?.service_state,
      inc_docs: (incQ.json?.results ?? []).map((h) => h.citation.doc_path),
      old_docs: (incOld.json?.results ?? []).map((h) => h.citation.doc_path) });

  const delR = await api.del({ repo: REPO, branch: BR, doc_path: 'trial/incremental.md' });
  const delQ = await api.query({ q: '觉醒天鹅', repo: REPO, branch: BR, k: 5 });
  const delRows = psql(`SELECT count(*) FROM ragtrial.chunks WHERE doc_path='trial/incremental.md'`).trim();
  const delDocState = psql(`SELECT state FROM ragtrial.documents WHERE doc_path='trial/incremental.md'`).trim();
  ok('S7', '删除后不可检索（chunks=0 + 审计行 deleted）',
    delR.json?.chunks_removed > 0 && delRows === '0' && delDocState === 'deleted'
      && !(delQ.json?.results ?? []).some((h) => h.citation.doc_path === 'trial/incremental.md'));

  // ── Phase D：索引失效 / 回滚（版本演练） ──
  const inv = await api.invalidate();
  const staleQ = await api.query({ q: '回滚的第一步', repo: REPO, branch: BR, k: 3 });
  ok('S8', '索引失效 → index_stale（旧版本行不命中）',
    inv.json?.index_version === 2 && staleQ.json?.service_state === 'index_stale');

  const reing = await api.ingest({ repo: REPO, branch: BR, corpus_dir: '/app/rag-corpus' });
  const v2Q = await api.query({ q: '回滚的第一步', repo: REPO, branch: BR, k: 3 });
  ok('D1', 're-ingest 生成 v2 索引（hit 恢复，iv=2）',
    reing.status === 200 && v2Q.json?.service_state === 'hit'
      && (v2Q.json?.results ?? []).every((h) => h.citation.index_version === 2));

  const rb = await api.rollback(1);
  const v1Q = await api.query({ q: '回滚的第一步', repo: REPO, branch: BR, k: 3 });
  ok('S14a', '索引回滚 v1：保留行重新命中（iv=1）',
    rb.json?.ok === true && v1Q.json?.service_state === 'hit'
      && (v1Q.json?.results ?? []).every((h) => h.citation.index_version === 1));
  await api.rollback(2); // 回到 v2（终态=最新）
  const badRb = await api.rollback(99);
  ok('S14b', '回滚目标无保留行 → 显式拒绝', badRb.json?.error_kind === 'rollback_target_missing' || badRb.status === 409);

  // ── Phase E：provider 不可达（一次性 remote-provider console） ──
  let providerOk = false, providerDetail = null;
  try {
    execFileSync('docker', ['run', '-d', '--name', 'localragtrial-provider-probe',
      '--network', 'local-rag-trial_ragtrial-net',
      '-p', '127.0.0.1:48451:4730',
      '-e', 'CONSOLE_HOST=0.0.0.0',
      '-e', `CONSOLE_PILOT_USER=${env.RAGTRIAL_CONSOLE_USER}`,
      '-e', `CONSOLE_PILOT_PASSWORD=${env.RAGTRIAL_CONSOLE_PASSWORD}`,
      '-e', `CONSOLE_SESSION_SECRET=${env.RAGTRIAL_SESSION_SECRET}`,
      '-e', `CONSOLE_PG_DSN=postgres://postgres:${env.RAGTRIAL_PG_PASSWORD}@pg:5432/ragtrial`,
      '-e', 'RAGTRIAL_EMBED_ENDPOINT=http://192.0.2.1:9999/v1/embeddings',
      '-e', 'RAGTRIAL_EMBED_TIMEOUT_MS=1500',
      'local-rag-trial-console:local'], { stdio: 'pipe' });
    // 等探针容器起来
    await new Promise((r) => setTimeout(r, 4000));
    const probeBase = 'http://127.0.0.1:48451';
    const loginRes = await fetch(probeBase + '/api/auth/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ user: env.RAGTRIAL_CONSOLE_USER, password: env.RAGTRIAL_CONSOLE_PASSWORD }),
    });
    const probeCookie = (loginRes.headers.get('set-cookie') || '').split(';')[0];
    const pq = await (await fetch(probeBase + '/api/rag-trial/query', {
      method: 'POST', headers: { cookie: probeCookie, 'content-type': 'application/json' },
      body: JSON.stringify({ q: '回滚', repo: REPO, branch: BR, k: 3 }),
    })).json();
    providerDetail = pq;
    providerOk = pq?.service_state === 'provider_unavailable' && (pq?.results ?? []).length === 0;
  } catch (e) {
    providerDetail = String(e.message).slice(0, 120);
  } finally {
    try { execFileSync('docker', ['rm', '-f', 'localragtrial-provider-probe'], { stdio: 'pipe' }); } catch { /* */ }
  }
  save('06-provider-unavailable.json', providerDetail);
  ok('S3', 'provider 不可达 → provider_unavailable（显式降级非空成功）', providerOk);

  // ── Phase F：pg 不可用 / 重启恢复 / 栈级 down-up 回滚演练 ──
  const beforeCounts = {
    docs: psql('SELECT count(*) FROM ragtrial.documents').trim(),
    chunks: psql('SELECT count(*) FROM ragtrial.chunks').trim(),
    queries: psql('SELECT count(*) FROM ragtrial.query_log').trim(),
    audits: psql('SELECT count(*) FROM ragtrial.audit_events').trim(),
  };
  compose('stop', 'pg');
  const pgDown = await api.query({ q: '回滚', repo: REPO, branch: BR, k: 3 });
  const metricsDuringDown = await api.metrics();
  compose('start', 'pg');
  await new Promise((r) => setTimeout(r, 4000));
  ok('S12', 'pgvector/PG 不可用 → error(pg_unavailable) + 内存计数补齐，不伪装',
    pgDown.status === 503 && pgDown.json?.service_state === 'error'
      && pgDown.json?.error_kind === 'pg_unavailable'
      && (metricsDuringDown.json?.in_memory_error_states?.pg_unavailable ?? 0) >= 1,
    { body: pgDown.json, mem: metricsDuringDown.json?.in_memory_error_states });

  compose('restart', 'pg');
  compose('restart', 'console');
  await waitHealthy();
  await api.login(); // 重启后旧会话可能失效——重登
  const afterCounts = {
    docs: psql('SELECT count(*) FROM ragtrial.documents').trim(),
    chunks: psql('SELECT count(*) FROM ragtrial.chunks').trim(),
    queries: psql('SELECT count(*) FROM ragtrial.query_log').trim(),
    audits: psql('SELECT count(*) FROM ragtrial.audit_events').trim(),
  };
  const restartQ = await api.query({ q: '验证者接受哪些证据类型', repo: REPO, branch: BR, k: 3 });
  ok('S13', '重启恢复：docs/chunks/queries/audits 零丢失 + 检索恢复',
    JSON.stringify(beforeCounts) === JSON.stringify(afterCounts)
      && restartQ.json?.service_state === 'hit',
    { beforeCounts, afterCounts });

  compose('down');
  compose('up', '-d');
  await waitHealthy();
  await api.login();
  const drillQ = await api.query({ q: '回滚的第一步', repo: REPO, branch: BR, k: 3 });
  const drillCounts = psql('SELECT count(*) FROM ragtrial.chunks').trim();
  ok('S14c', '栈级 down/up 回滚演练：卷保留 → 索引完好',
    drillQ.json?.service_state === 'hit' && Number(drillCounts) === Number(afterCounts.chunks));

  // ── Phase G：终态指标 + manifest ──
  const metrics = await api.metrics();
  save('07-metrics-final.json', metrics.json);
  ok('G1', '指标齐备：六状态计数 + P50/P95 + 引用统计 + Recall 记录',
    metrics.json?.latency_ms?.p50 >= 0 && metrics.json?.latency_ms?.p95 >= metrics.json.latency_ms.p50
      && (metrics.json?.eval_runs ?? []).length >= 1
      && (metrics.json?.queries_by_state?.hit ?? 0) >= 1
      && (metrics.json?.queries_by_state?.empty ?? 0) >= 1,
    metrics.json?.latency_ms);

  const modelRow = psql("SELECT model_id || ' ' || model_digest || ' ' || index_version FROM ragtrial.models WHERE active").trim().split(' ');
  const manifest = {
    generated_at: new Date().toISOString(),
    model: { model_id: modelRow[0], model_digest: modelRow[1], index_version: Number(modelRow[2]) },
    code_sha256: Object.fromEntries([
      'console/backend/lib/ragtrial/embed.mjs',
      'console/backend/lib/ragtrial/ingest.mjs',
      'console/backend/lib/ragtrial/schema.mjs',
      'console/backend/lib/ragtrial/store.mjs',
      'console/backend/lib/ragtrial/api.mjs',
      'console/backend/lib/ragtrial/review.mjs',
    ].map((f) => [f, sha256file(path.join(WORKTREE, f))])),
    corpus_sha256: Object.fromEntries(fs.readdirSync(path.join(STACK, 'corpus')).sort()
      .map((f) => [`corpus/${f}`, sha256file(path.join(STACK, 'corpus', f))])),
    images: {
      pg: 'pgvector/pgvector:pg16 (local, pull never)',
      minio: 'elestio/minio@sha256:25348a257f1ece1b192f25f6cd9854618fa86422ac87b494b5d4e629c556d4bd (local, pull never)',
      console: 'local-rag-trial-console:local (built from docker/Dockerfile.canonical-console @ feat/local-rag-trial)',
    },
    counts: afterCounts,
  };
  save('08-manifest.json', manifest);

  // ── Phase H：secret-scan（仓库门禁同款脚本） ──
  let scan = null;
  try {
    scan = spawnSync('bash', ['scripts/secret-scan.sh', '--path', '.'], { cwd: WORKTREE, encoding: 'utf8' });
  } catch (e) { scan = { status: -1, stdout: '', stderr: String(e.message) }; }
  fs.writeFileSync(path.join(EVID, '09-secret-scan.log'),
    `exit=${scan.status}\nSTDOUT:\n${scan.stdout ?? ''}\nSTDERR:\n${scan.stderr ?? ''}`);
  ok('S15', 'secret-scan（scripts/secret-scan.sh --path .）零命中', scan.status === 0,
    { exit: scan.status, stderr: String(scan.stderr ?? '').slice(0, 200) });

} catch (e) {
  fail++;
  log(`FATAL ${e.stack ?? e}`);
  results.push({ id: 'FATAL', name: String(e.message), pass: false });
} finally {
  transcript.end();
}

const summary = {
  verdict: fail === 0 ? 'LOCAL_RAG_TRIAL_STACK_E2E_GREEN' : 'LOCAL_RAG_TRIAL_STACK_E2E_FAILED',
  pass, fail,
  evidence_dir: EVID,
  scenarios: results,
  generated_at: new Date().toISOString(),
};
save('00-summary.json', summary);
console.log(`\n${summary.verdict}: ${pass} pass / ${fail} fail\n  evidence: ${EVID}`);
process.exit(fail ? 1 : 0);
