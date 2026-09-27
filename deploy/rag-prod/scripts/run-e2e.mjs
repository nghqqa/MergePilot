#!/usr/bin/env node
// deploy/rag-prod/scripts/run-e2e.mjs — PRODUCTION_RAG_READINESS 栈级 e2e。
// 栈：rag-prod-eval（48470 console / 15438 pg / 9115 minio / 48471 sidecar）。
// 覆盖：attested 语义链路、机器端点 HMAC 矩阵（含无 keystore BLOCKED 探针）、
// worker 持久队列（执行/幂等/崩溃重占/provider 故障重试）、重启恢复、
// Review/FXV 边界（含 VERIFIED）、指标/审计。
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STACK = path.resolve(HERE, '..');
const WORKTREE = path.resolve(STACK, '..', '..');
const EVID = path.join(WORKTREE, 'evidence', 'rag-prod', new Date().toISOString().replace(/[:.]/g, '-'));
fs.mkdirSync(EVID, { recursive: true });

const env = {};
for (const line of fs.readFileSync(path.join(STACK, '.env'), 'utf8').split('\n')) {
  const m = line.match(/^([A-Z_]+)=(.*)$/); if (m) env[m[1]] = m[2].trim();
}
const CONSOLE = `http://127.0.0.1:${env.RAGPROD_CONSOLE_PORT || 48470}`;
const PG_PORT = env.RAGPROD_PG_PORT || 15438;
const REPO = 'nghqqa/mergepilot', BR = 'feat/rag-prod-readiness';
const RUN = Date.now().toString(36); // 每次运行唯一化（e2e 幂等）
const DSN = `postgres://postgres:${env.RAGPROD_PG_PASSWORD}@127.0.0.1:${PG_PORT}/ragprod`;

const log = (m) => { console.log(m); fs.appendFileSync(path.join(EVID, 'transcript.log'), m + '\n'); };
let pass = 0, fail = 0; const results = [];
function ok(id, name, cond, detail) {
  results.push({ id, name, pass: Boolean(cond), detail: detail ?? null });
  if (cond) { pass++; log(`  PASS [${id}] ${name}`); }
  else { fail++; log(`  FAIL [${id}] ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 260) : ''}`); }
}
const compose = (...a) => execFileSync('docker', ['compose', ...a], { cwd: STACK, encoding: 'utf8' });
const psql = (sql) => compose('exec', '-T', 'pg', 'psql', '-U', 'postgres', '-d', 'ragprod', '-tAc', sql);
const save = (n, d) => fs.writeFileSync(path.join(EVID, n), JSON.stringify(d, null, 2));

let cookie = null;
async function call(method, p, body, base = CONSOLE) {
  const res = await fetch(base + p, {
    method, headers: { cookie: cookie ?? '', 'content-type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}

// —— 签名工具（复用仓库 cchain 模块；合成密钥在 keystore-local）——
const keyFiles = fs.readdirSync(path.join(STACK, 'keystore-local')).filter((f) => f.endsWith('.key.json'));
const key = JSON.parse(fs.readFileSync(path.join(STACK, 'keystore-local', keyFiles[0]), 'utf8'));
const signPayload = ({ run_id, nonce, timestamp }) => crypto.createHmac('sha256', key.secret).update(`${run_id}|${nonce}|${timestamp}`).digest('hex');

async function main() {
  // ── 登录 + 状态 ──
  const login = await fetch(CONSOLE + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: env.RAGPROD_CONSOLE_USER, password: env.RAGPROD_CONSOLE_PASSWORD }),
  });
  cookie = (login.headers.get('set-cookie') || '').split(';')[0];
  ok('P0', '登录 + 栈在线', login.status === 200);

  // 清理上轮 e2e 残留任务（e2e/* 为本脚本工件；避免 --once 认领顺序被旧任务抢占）
  psql(`DELETE FROM ragtrial.jobs WHERE doc_path LIKE 'e2e/%'`);

  const st = await call('GET', '/api/rag-trial/status');
  save('01-status.json', st.json);
  ok('P1', 'status 聚合：语义 provider LOCAL_MANIFEST_VERIFIED（外部 attestation 如实 NOT_CONFIGURED）+ RUN_BINDING READY + 队列统计',
    st.json?.production_readiness?.semantic_provider?.state === 'LOCAL_MANIFEST_VERIFIED'
    && st.json?.production_readiness?.semantic_provider?.external_attestation?.state === 'NOT_CONFIGURED'
    && st.json?.production_readiness?.run_binding_auth?.state === 'READY'
    && st.json?.production_readiness?.persistent_queue?.by_state !== undefined,
    st.json?.production_readiness);

  // ── attested 语义链路（真实 bge sidecar）──
  const semQ = await call('POST', '/api/rag-trial/query', {
    q: 'what is the first step of a rollback', repo: REPO, branch: BR, k: 3, model_id: 'bge-large-en-v1.5',
  });
  save('02-semantic-query.json', semQ.json);
  ok('S1', '语义检索命中（bge sidecar attested 全链）',
    semQ.json?.service_state === 'hit' && (semQ.json?.results ?? []).length > 0
    && semQ.json.results[0].citation.model_id === 'bge-large-en-v1.5');
  const hashQ = await call('POST', '/api/rag-trial/query', {
    q: 'rollback anchor digest verification', repo: REPO, branch: BR, k: 3, model_id: 'local-hash-v1',
  });
  ok('S2', '确定性基线并行可用（local-hash-v1 仍为回归基线）', hashQ.json?.service_state === 'hit');

  // ── 机器端点 HMAC 矩阵 ──
  const run_id = 'eval-run-0001';
  const nonce = crypto.randomBytes(12).toString('hex');
  const ts = Date.now();
  const good = { run_id, nonce, timestamp: ts, signature: signPayload({ run_id, nonce, timestamp: ts }),
    q: 'who holds ticket ownership', repo: REPO, branch: BR, k: 3 };
  const m1 = await call('POST', '/api/rag-trial/machine/query', good);
  save('03-machine-good.json', m1.json);
  ok('M1', '机器端点：有效签名 → 200 + 辅助证据 + policy 块',
    m1.status === 200 && (m1.json?.auxiliary_evidence ?? []).length > 0
    && m1.json?.policy?.may_auto_promote === false
    && m1.json?.policy?.excluded_from?.includes('VERIFIED'));
  const m2 = await call('POST', '/api/rag-trial/machine/query',
    { ...good, nonce: crypto.randomBytes(12).toString('hex'), signature: 'f'.repeat(64) });
  ok('M2', '坏签名 → 401 拒绝（审计落库）', m2.status === 401 && m2.json?.ok === false);
  const m3 = await call('POST', '/api/rag-trial/machine/query', good); // 同 nonce 重放
  ok('M3', '同 nonce 重放 → 401 REPLAYED_NONCE', m3.status === 401 && m3.json?.reason === 'REPLAYED_NONCE');
  const m4 = await call('POST', '/api/rag-trial/machine/query',
    { ...good, nonce: crypto.randomBytes(12).toString('hex'), timestamp: ts - 20 * 60_000,
      signature: signPayload({ run_id, nonce: 'x', timestamp: ts - 20 * 60_000 }) });
  ok('M4', '时间窗外的旧签名 → 401 TIMESTAMP_SKEW', m4.status === 401 && m4.json?.reason === 'TIMESTAMP_SKEW');

  // M6：机器身份 scope 越权（验签通过但 repo 越界 → 403）
  {
    const mOver = await call('POST', '/api/rag-trial/machine/query',
      { run_id, nonce: crypto.randomBytes(10).toString('hex'), timestamp: Date.now(),
        q: 'rollback anchor', repo: 'other/repo', branch: BR, k: 3 });
    // 补签名
    const nb = crypto.randomBytes(10).toString('hex'); const tb = Date.now();
    const mOver2 = await call('POST', '/api/rag-trial/machine/query',
      { run_id, nonce: nb, timestamp: tb, signature: signPayload({ run_id, nonce: nb, timestamp: tb }),
        q: 'rollback anchor', repo: 'other/repo', branch: BR, k: 3 });
    ok('M6', '机器身份越权（有效签名）→ 403 scope_not_allowed',
      mOver2.status === 403 && mOver2.json?.error?.reason === 'scope_not_allowed', { s: mOver2.status, j: mOver2.json });
  }

  // 无 keystore 探针（一次性容器，端口 48472）→ BLOCKED
  let blocked = null;
  try {
    execFileSync('docker', ['run', '-d', '--name', 'ragprod-noks-probe', '--network', 'rag-prod-eval_rag-prod-net',
      '-p', '127.0.0.1:48472:4730',
      '-e', 'CONSOLE_HOST=0.0.0.0',
      '-e', `CONSOLE_PILOT_USER=${env.RAGPROD_CONSOLE_USER}`,
      '-e', `CONSOLE_PILOT_PASSWORD=${env.RAGPROD_CONSOLE_PASSWORD}`,
      '-e', `CONSOLE_SESSION_SECRET=${env.RAGPROD_SESSION_SECRET}`,
      '-e', `CONSOLE_PG_DSN=${DSN.replace('127.0.0.1', 'pg')}`,
      'rag-prod-console:local'], { stdio: 'pipe' });
    await new Promise((r) => setTimeout(r, 4000));
    blocked = await call('POST', '/api/rag-trial/machine/query', good, 'http://127.0.0.1:48472');
  } catch (e) { blocked = { status: -1, json: { error: String(e.message).slice(0, 80) } }; }
  finally { try { execFileSync('docker', ['rm', '-f', 'ragprod-noks-probe'], { stdio: 'pipe' }); } catch { /* */ } }
  ok('M5', 'keystore 缺失 → RUN_BINDING_AUTH_BLOCKED（保持 BLOCKED，不回退匿名）',
    blocked.status === 401 && blocked.json?.reason === 'RUN_BINDING_AUTH_BLOCKED', blocked.json);

  // ── worker 持久队列：正常执行 + 幂等 ──
  const jq = await call('POST', '/api/rag-trial/jobs', { jobs: [{
    kind: 'ingest_doc', repo: REPO, branch: BR, doc_path: `e2e/worker-probe-${RUN}.md`,
    content_sha256: crypto.createHash('sha256').update(`worker probe v1 ${RUN}`).digest('hex'),
    model_id: 'local-hash-v1', payload: { text: `worker probe v1 ${RUN} with distinctive lexical anchors` },
  }] });
  const jobId = jq.json?.enqueued?.[0]?.job_id;
  ok('W1', '任务入队（API）', jq.status === 200 && jq.json?.enqueued?.[0]?.created === true && jobId);
  await runWorker(['--once', '--worker', 'e2e-worker-1']);
  const row1 = JSON.parse(psql(`SELECT json_build_object('s',state,'a',attempts) FROM ragtrial.jobs WHERE job_id='${jobId}'`));
  const wchunks = psql(`SELECT count(*) FROM ragtrial.chunks WHERE doc_path='e2e/worker-probe-${RUN}.md'`).trim();
  ok('W2', 'worker 执行 → done + 索引落库', row1.s === 'done' && Number(wchunks) >= 1, { row1, wchunks });
  // 重复入队同任务 → 幂等
  const jq2 = await call('POST', '/api/rag-trial/jobs', { jobs: [{
    kind: 'ingest_doc', repo: REPO, branch: BR, doc_path: `e2e/worker-probe-${RUN}.md`,
    content_sha256: crypto.createHash('sha256').update(`worker probe v1 ${RUN}`).digest('hex'),
    model_id: 'local-hash-v1', payload: { text: `worker probe v1 ${RUN} with distinctive lexical anchors` },
  }] });
  ok('W3', '重复入队幂等（created=false）', jq2.json?.enqueued?.[0]?.created === false);

  // ── 崩溃恢复（确定性）：认领后"死亡"，心跳过期被重占 ──
  const jc = await call('POST', '/api/rag-trial/jobs', { jobs: [{
    kind: 'ingest_doc', repo: REPO, branch: BR, doc_path: `e2e/crash-probe-${RUN}.md`,
    content_sha256: crypto.createHash('sha256').update(`crash probe v2 ${RUN}`).digest('hex'),
    model_id: 'local-hash-v1', payload: { text: `crash probe v2 ${RUN} reclaimable anchors` },
  }] });
  const crashId = jc.json?.enqueued?.[0]?.job_id;
  psql(`UPDATE ragtrial.jobs SET state='running', locked_by='dead-worker', attempts=1,
        heartbeat_at=now()-interval '10 minutes' WHERE job_id='${crashId}'`);
  await runWorker(['--once', '--worker', 'e2e-rescuer']);
  const rowC = JSON.parse(psql(`SELECT json_build_object('s',state,'a',attempts,'lb',locked_by) FROM ragtrial.jobs WHERE job_id='${crashId}'`));
  const cReclaim = Number(psql(`SELECT count(*) FROM ragtrial.audit_events WHERE kind='JOB_RECLAIM'`));
  ok('W4', '崩溃任务被重占完成（JOB_RECLAIM 审计）',
    rowC.s === 'done' && rowC.lb === 'e2e-rescuer' && cReclaim >= 1, { rowC, cReclaim });

  // ── provider 故障重试：停 sidecar → 语义任务失败重排 → 恢复 sidecar → 重试成功 ──
  const jp = await call('POST', '/api/rag-trial/jobs', { jobs: [{
    kind: 'ingest_doc', repo: REPO, branch: BR, doc_path: `e2e/semantic-retry-${RUN}.md`,
    content_sha256: crypto.createHash('sha256').update(`semantic retry probe ${RUN}`).digest('hex'),
    model_id: 'bge-large-en-v1.5', payload: { text: `semantic retry probe ${RUN} with english anchors` },
  }], });
  const retryId = jp.json?.enqueued?.[0]?.job_id;
  compose('stop', 'bge-sidecar');
  await runWorker(['--once', '--worker', 'e2e-worker-nosidecar'], SIDECAR_ENV);
  const rowR1 = JSON.parse(psql(`SELECT json_build_object('s',state,'a',attempts,'e',last_error) FROM ragtrial.jobs WHERE job_id='${retryId}'`));
  compose('start', 'bge-sidecar');
  await new Promise((r) => setTimeout(r, 25000));
  psql(`UPDATE ragtrial.jobs SET next_run_at=now() WHERE job_id='${retryId}'`);
  await runWorker(['--once', '--worker', 'e2e-worker-retry'], SIDECAR_ENV);
  const rowR2 = JSON.parse(psql(`SELECT json_build_object('s',state,'a',attempts,'e',last_error) FROM ragtrial.jobs WHERE job_id='${retryId}'`));
  const semRetryChunks = Number(psql(`SELECT count(*) FROM ragtrial.chunks_semantic WHERE doc_path='e2e/semantic-retry-${RUN}.md'`).trim());
  ok('W5', 'provider 故障 → 任务重试（非死非丢）；恢复后重试成功',
    rowR1.s === 'queued' && rowR1.e && rowR2.s === 'done' && semRetryChunks >= 1,
    { rowR1, rowR2, semRetryChunks });

  // ── Review/FXV 边界（栈上复验，含 VERIFIED）──
  const pol = await call('POST', '/api/rag-trial/policy-check', { evidence: (m1.json?.auxiliary_evidence ?? []).slice(0, 2) });
  save('04-policy.json', pol.json);
  ok('B1', '边界：RAG-only 建 finding/ticket/gate/VERIFIED 全拒 + Fixer 不启动 + Verifier 拒收',
    pol.json?.as_finding?.allowed === false && pol.json?.as_ticket?.allowed === false
    && pol.json?.as_gate?.allowed === false && pol.json?.as_VERIFIED?.allowed === false
    && pol.json?.fixer?.fixer_may_run === false && pol.json?.verifier?.every((v) => !v.accepted));

  // ── 重启恢复 ──
  const before = {
    docs: psql('SELECT count(*) FROM ragtrial.documents').trim(),
    hash: psql('SELECT count(*) FROM ragtrial.chunks').trim(),
    sem: psql('SELECT count(*) FROM ragtrial.chunks_semantic').trim(),
    jobs: psql('SELECT count(*) FROM ragtrial.jobs').trim(),
  };
  compose('restart', 'pg'); compose('restart', 'console');
  for (let i = 0; i < 40; i++) {
    try { const h = await (await fetch(CONSOLE + '/api/health')).json(); if (h) break; } catch { /* */ }
    await new Promise((r) => setTimeout(r, 1500));
  }
  const relogin = await fetch(CONSOLE + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: env.RAGPROD_CONSOLE_USER, password: env.RAGPROD_CONSOLE_PASSWORD }),
  });
  cookie = (relogin.headers.get('set-cookie') || '').split(';')[0];
  const after = {
    docs: psql('SELECT count(*) FROM ragtrial.documents').trim(),
    hash: psql('SELECT count(*) FROM ragtrial.chunks').trim(),
    sem: psql('SELECT count(*) FROM ragtrial.chunks_semantic').trim(),
    jobs: psql('SELECT count(*) FROM ragtrial.jobs').trim(),
  };
  const rq2 = await call('POST', '/api/rag-trial/query', {
    q: 'rollback anchor digest', repo: REPO, branch: BR, k: 3, model_id: 'local-hash-v1',
  });
  ok('R1', '重启恢复：docs/hash/sem/jobs 零丢失 + 检索恢复',
    JSON.stringify(before) === JSON.stringify(after) && rq2.json?.service_state === 'hit', { before, after });

  // ── 指标与审计 ──
  const metrics = await call('GET', '/api/rag-trial/metrics');
  const qMetrics = await call('GET', '/api/rag-trial/queue/metrics');
  save('05-metrics.json', { query: metrics.json, queue: qMetrics.json });
  ok('X1', '指标齐备：查询状态/延迟 + 队列 by_state/死信 + JOB_* 审计',
    metrics.json?.latency_ms?.p50 >= 0 && qMetrics.json?.by_state?.done >= 2
    && Array.isArray(qMetrics.json?.dead_letters)
    && (metrics.json?.audit_events_by_kind?.JOB_DONE ?? 0) >= 2);

  // ── secret-scan ──
  const scan = spawnSyncSafe(['bash', ['scripts/secret-scan.sh', '--path', '.'], WORKTREE]);
  fs.writeFileSync(path.join(EVID, '06-secret-scan.log'), scan);
  ok('X2', 'secret-scan 零命中（--path .，keystore/.env 已 gitignore）', scan.includes('secret-scan: PASS'));
}

const SIDECAR_ENV = {
  RAGTRIAL_EMBED_ENDPOINT: `http://127.0.0.1:${env.RAGPROD_SIDECAR_PORT || 48471}/embed`,
  RAGTRIAL_EMBED_MODEL_ID: 'bge-large-en-v1.5',
  RAGTRIAL_EMBED_DIMS: '1024',
  RAGTRIAL_EMBED_TIMEOUT_MS: '30000',
  RAGTRIAL_EMBED_EXPECTED_MANIFEST: env.RAGPROD_EXPECTED_MANIFEST,
};
function spawnSyncSafe([cmd, cargs, cwd], extraEnv = {}) {
  const r = spawnSync(cmd, cargs, { cwd, encoding: 'utf8', env: { ...process.env, ...extraEnv } });
  return `exit=${r.status}\n${r.stdout ?? ''}${r.stderr ?? ''}`;
}
function runWorker(extra, extraEnv = {}) {
  const r = spawnSyncSafe([process.execPath, [path.join(WORKTREE, 'tools', 'rag-worker.mjs'), '--dsn', DSN, ...extra], WORKTREE], extraEnv);
  fs.appendFileSync(path.join(EVID, 'transcript.log'), `[worker ${extra.join(' ')}]\n${r.slice(0, 400)}\n`);
}

main().catch((e) => { fail++; log(`FATAL ${e.stack ?? e}`); })
  .finally(() => {
    const summary = {
      verdict: fail === 0 ? 'RAG_PROD_E2E_GREEN' : 'RAG_PROD_E2E_FAILED',
      pass, fail, evidence_dir: EVID, scenarios: results, generated_at: new Date().toISOString(),
    };
    save('00-summary.json', summary);
    console.log(`\n${summary.verdict}: ${pass} pass / ${fail} fail\n  evidence: ${EVID}`);
    process.exit(fail ? 1 : 0);
  });
