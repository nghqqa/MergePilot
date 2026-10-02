// console/backend/test/mu-review-arch-prf.integration.mjs — ADR-002 PR F 门槛（API 披露面）。
// PR 详情分立列 + egress 披露端点；零真实 Provider 调用（直插 egress 行测投影）。
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};

const CTR = `mu-prf-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17800 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 60; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'x-test-password';
process.env.CONSOLE_SESSION_SECRET = 'prf-it-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.MU_REVIEW_ARCH = 'v2';

const { createMuStore } = await import('../lib/multiuser/store.mjs');
const store = await createMuStore({ pool });
await store.initSchema();
await store.bootstrap();

const T1 = (await pool.query(`SELECT tenant_id FROM mu.tenant ORDER BY created_at LIMIT 1`)).rows[0].tenant_id;
const T2 = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('t2prf','T2') RETURNING tenant_id`)).rows[0].tenant_id;
const mkRepoPr = async (t, n) => {
  const repo = (await pool.query(`INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
    VALUES ($1,'github',$2,'arch','prf$n') ON CONFLICT DO NOTHING RETURNING repo_id`, [t, `prf-${n}`])).rows[0]
    ?? (await pool.query(`SELECT repo_id FROM mu.repository WHERE tenant_id=$1 LIMIT 1`, [t])).rows[0];
  const pr = (await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
    VALUES ($1,$2,$3,$4) RETURNING pr_id, provider_pr_number`, [t, repo.repo_id,
    Math.floor(Math.random() * 90000) + 1000, crypto.randomBytes(20).toString('hex')])).rows[0];
  return { repoId: repo.repo_id, prId: pr.pr_id, prNumber: pr.provider_pr_number };
};
const A = await mkRepoPr(T1, 'a');
const B = await mkRepoPr(T2, 'b');

try {
  const { createConsole } = await import('../server.mjs');
  const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const BASE = `http://127.0.0.1:${server.address().port}`;
  const login = async () => {
    const res = await fetch(BASE + '/api/mu/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ provider: 'fixture', subject: 'fixture:dev-pilot' }) });
    return { cookie: (res.headers.get('set-cookie') || '').split(';')[0],
      csrf: (await res.json().catch(() => null))?.csrf ?? '' };
  };
  const S = await login();
  const call = async (p, { method = 'GET', body, cookie = S.cookie, csrf = S.csrf } = {}) => {
    const res = await fetch(BASE + p, { method,
      headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
        ...(body ? { 'content-type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined });
    return { status: res.status, body: await res.json().catch(() => null) };
  };

  // F1 PR 详情含 latest_run 四分立列（v2 run 带判定）
  const headA = crypto.randomBytes(20).toString('hex');
  const runA = (await pool.query(
    `INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, status, architecture_version,
       review_mode, review_verdict, verification_verdict, tests_status, merge_eligibility, code_egress)
     VALUES ($1,$2,$3,$4,'VERIFIED','v2','external_api','changes_requested','passed','failed','ineligible',2)
     RETURNING run_id`, [T1, A.repoId, A.prId, headA])).rows[0];
  const d1 = await call(`/api/mu/prs/${A.prNumber}?repo_id=${A.repoId}`);
  const lr1 = d1.body?.latest_run;
  ok('F1 PR 详情 latest_run 四分立列', d1.status === 200 && lr1
    && lr1.review_verdict === 'changes_requested' && lr1.verification_verdict === 'passed'
    && lr1.tests_status === 'failed' && lr1.merge_eligibility === 'ineligible',
    { status: d1.status, lr1 });
  ok('F1b 出站计数+模式披露', lr1?.code_egress === 2 && lr1?.review_mode === 'external_api');

  // F2 legacy run（零 v2 列）→ null 不冒充
  const headL = crypto.randomBytes(20).toString('hex');
  await pool.query(
    `INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, status)
     VALUES ($1,$2,$3,$4,'COMPLETED')`, [T1, A.repoId, A.prId, headL]);
  // 最新 run=legacy（created_at 更晚）→ latest_run 列全 null
  const d2 = await call(`/api/mu/prs/${A.prNumber}?repo_id=${A.repoId}`);
  const lr2 = d2.body?.latest_run;
  ok('F2 legacy run 列=null（不猜值）', d2.status === 200 && lr2 && lr2.review_verdict === null
    && lr2.verification_verdict === null && lr2.merge_eligibility === null
    && lr2.architecture_version === null, { lr2 });

  // F3 egress 披露：投影 200+零正文（digest/计数/文件名 only）
  const att1 = (await pool.query(
    `INSERT INTO mu.agent_attempt (run_id, agent_role, attempt, provider, actor_principal,
       input_digest, tenant_id, repo_id, pr_id, head_sha)
     VALUES ($1,'reviewer',1,'external_api','system:reviewer',$2,$3,$4,$5,$6) RETURNING attempt_id`,
    [runA.run_id, 'd'.repeat(32), T1, A.repoId, A.prId, headA])).rows[0];
  await pool.query(
    `INSERT INTO mu.code_egress_event (tenant_id, repo_id, run_id, attempt_id, provider_id,
       model_id, head_sha, input_digest, files, bytes_sent, tokens_sent, redactions_applied,
       response_digest) VALUES ($1,$2,$3,$4,'deepseek','deepseek-chat',$5,$6,$7,2048,512,3,$8)`,
    [T1, A.repoId, runA.run_id, att1.attempt_id, headA, 'd'.repeat(32), ['src/a.js', 'src/b.js'], 'r'.repeat(32)]);
  const d3 = await call('/api/mu/egress-events');
  const ev3 = d3.body?.events ?? [];
  const mine3 = ev3.find((e) => e.run_id === runA.run_id);
  ok('F3 egress-events 200+事件在', d3.status === 200 && mine3 != null, { status: d3.status });
  ok('F3b 投影字段（role/provider/digest/文件名/计数）',
    mine3?.agent_role === 'reviewer' && mine3?.provider_id === 'deepseek'
      && mine3?.input_digest === 'd'.repeat(32)
      && Array.isArray(mine3?.files) && mine3.files.length === 2
      && mine3?.bytes_sent === 2048 && mine3?.tokens_sent === 512
      && mine3?.redactions_applied === 3, { mine3 });
  ok('F3c 零代码正文（键集白名单——无 content/envelope/text 键）',
    mine3 && !JSON.stringify(mine3).match(/"content"|"envelope"|"patch_text"|"diff_text"/i));

  // F4 run_id 过滤 + 跨租户 404 防枚举
  const d4 = await call(`/api/mu/egress-events?run_id=${runA.run_id}`);
  ok('F4 run_id 过滤命中', d4.status === 200 && (d4.body?.events ?? []).length >= 1
    && (d4.body?.events ?? []).every((e) => e.run_id === runA.run_id));
  const runB = (await pool.query(
    `INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, status)
     VALUES ($1,$2,$3,$4,'RECEIVED') RETURNING run_id`,
    [T2, B.repoId, B.prId, crypto.randomBytes(20).toString('hex')])).rows[0];
  const d4b = await call(`/api/mu/egress-events?run_id=${runB.run_id}`);
  ok('F4b 跨租户 run_id=404 防枚举', d4b.status === 404, { status: d4b.status });

  // F5 未登录 401/403（披露面不裸奔）
  const d5 = await call('/api/mu/egress-events', { cookie: null, csrf: null });
  ok('F5 未登录拒绝', d5.status === 401 || d5.status === 403, { status: d5.status });

  // F6 limit 收窄（≤200）
  const d6 = await call('/api/mu/egress-events?limit=10000');
  ok('F6 limit 钳制 200（不信任客户端）', d6.status === 200 && (d6.body?.events ?? []).length <= 200);

  // F7 披露端点只读（无写路径——PUT 拒绝）
  const d7 = await call('/api/mu/egress-events', { method: 'PUT', body: {} });
  ok('F7 披露端点只读（PUT 404/405）', d7.status === 404 || d7.status === 405, { status: d7.status });

  // F8 T2 的事件不在 T1 会话的列表（租户收窄）
  ok('F8 租户收窄（T1 列表零 T2 run 事件）',
    (d3.body?.events ?? []).every((e) => e.run_id !== runB.run_id));

  await new Promise((r) => server.close(r));
} finally {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
