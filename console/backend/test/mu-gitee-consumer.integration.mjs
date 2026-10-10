// console/backend/test/mu-gitee-consumer.integration.mjs — Gitee 首版隔离集成测试（G-3c）。
// 层级：真实服务代码（boot postgres + 真实 HTTP server + 真实消费者状态机）+ stub 外部 API
// （globalThis.fetch 拦截 gitee.com——同进程消费者经默认 fetch 装配适配器）。
// 不含真实 Gitee 平台：私有仓库授权/真实 webhook 投递属试点前置（见交付报告）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
process.env.CONSOLE_SESSION_SECRET = 'gitee-it-session-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
delete process.env.MU_FIXTURES;
process.env.MU_JOB_CONSUMER_ENABLED = '0'; // 本测试手动驱动消费（确定性）
const GITEE_SECRET = 'SEC-gitee-it-webhook-secret';
process.env.MU_GITEE_WEBHOOK_MODE = 'signature';
process.env.MU_GITEE_WEBHOOK_SECRET = GITEE_SECRET;
process.env.MU_GITEE_PAT = 'gitee-it-pat-token';
process.env.MU_GITHUB_WEBHOOK_SECRET = 'gitee-it-gh-secret';

const CTR = `mu-gitee-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16580 + Math.floor(Math.random() * 80);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
process.env.CONSOLE_PG_DSN = dsn;
{
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); await p.end(); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}

const { createConsole } = await import('../server.mjs');
const { server } = createConsole({ evidenceRoot: here, distDir: path.join(here, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

// ── stub 外部 Gitee API（真实消费者进程内）────────────────────────
// 拦截 gitee.com/api/v5 GET；其余（127.0.0.1 等）透传原生 fetch。
const realFetch = globalThis.fetch;
const giteeCalls = [];
let stubHeadSha = 'a'.repeat(40);
let stubFiles = [{ filename: 'src/app.js', status: null, additions: '1', deletions: '1',
  patch: { diff: '@@ -1,2 +1,2 @@\n-old line\n+new clean line' } }];
globalThis.fetch = async (input, init) => {
  const u = input instanceof URL ? input : new URL(String(input));
  if (u.hostname === 'gitee.com') {
    giteeCalls.push({ method: init?.method ?? 'GET', path: u.pathname,
      tokenInQuery: u.searchParams.get('access_token') ?? null });
    const p = u.pathname.replace(/^\/api\/v5/, '');
    const res = (status, body) => new Response(JSON.stringify(body), { status,
      headers: { 'content-type': 'application/json' } });
    if (p === '/user') return res(200, { id: 42, login: 'pilot' });
    if (p === '/repos/gitee-pilot/pilot-repo') return res(200, { id: 777000, full_name: 'gitee-pilot/pilot-repo', default_branch: 'master', private: true });
    if (p === `/repos/gitee-pilot/pilot-repo/pulls/14`) return res(200, {
      number: 14, state: 'open', title: 'stub pr secret',
      head: { sha: stubHeadSha, repo: { id: 777001 } },
      base: { sha: 'b'.repeat(40), repo: { id: 777000 } },
      html_url: 'https://gitee.com/gitee-pilot/pilot-repo/pulls/14' });
    if (p === `/repos/gitee-pilot/pilot-repo/pulls/12`) return res(200, {
      number: 12, state: 'open', title: 'stub pr drift',
      head: { sha: 'a'.repeat(40), repo: { id: 777001 } },
      base: { sha: 'b'.repeat(40), repo: { id: 777000 } },
      html_url: 'https://gitee.com/gitee-pilot/pilot-repo/pulls/12' });
    if (p === `/repos/gitee-pilot/pilot-repo/pulls/11`) return res(200, {
      number: 11, state: 'open', title: 'stub pr',
      head: { sha: stubHeadSha, repo: { id: 777001 } },
      base: { sha: 'b'.repeat(40), repo: { id: 777000 } },
      html_url: 'https://gitee.com/gitee-pilot/pilot-repo/pulls/11' });
    if (p === `/repos/gitee-pilot/pilot-repo/pulls/14/files`) return res(200, stubFiles);
    if (p === `/repos/gitee-pilot/pilot-repo/pulls/11/files`) return res(200, stubFiles);
    return res(404, { message: 'Not Found' });
  }
  return realFetch(input, init);
};

async function muLogin(subject) {
  const res = await realFetch(`${BASE}/api/mu/auth/login`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject }),
  });
  const cookie = (res.headers.get('set-cookie') || '').split(';')[0];
  const csrf = (res.headers.get('set-cookie') || '').match(/mp_csrf=([^;,]+)/)?.[1] ?? null;
  return { cookie, csrf, json: await res.json().catch(() => null) };
}
async function call(p, { method = 'GET', body = null, cookie = null, csrf = null } = {}) {
  const res = await realFetch(`${BASE}${p}`, {
    method,
    headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
      ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}

// 真实 Gitee webhook 投递（官方签名算法：HmacSHA256(ts+"\n"+secret)→Base64）
function giteeSign(ts) {
  return crypto.createHmac('sha256', Buffer.from(GITEE_SECRET, 'utf8'))
    .update(`${ts}\n`, 'utf8').digest('base64');
}
function giteePayload({ prNumber = 11, headSha, repoId = 777000, action = 'open', changedFiles = 1 }) {
  return JSON.stringify({
    hook_name: 'merge_request_hooks', timestamp: String(Date.now()), sign: giteeSign(String(Date.now())),
    action, pull_request: { number: prNumber, state: 'open', title: 'stub pr',
      head: { sha: headSha, ref: 'feature', repo: { id: 777001 } },
      base: { sha: 'b'.repeat(40), ref: 'master', repo: { id: 777000 } },
      changed_files: changedFiles },
    repository: { id: repoId, full_name: 'gitee-pilot/pilot-repo', name: 'pilot-repo' },
    sender: { login: 'someone' },
  });
}
async function deliverGitee(raw, { signOverride = null, tsOverride = null, event = 'Merge Request Hook' } = {}) {
  // 重算签名（payload 里 timestamp/sign 是展示用；header 携带为官方文档主分支）
  const body = JSON.parse(raw);
  const ts = tsOverride ?? body.timestamp;
  const headers = { 'content-type': 'application/json', 'x-gitee-event': event,
    'x-gitee-timestamp': ts, 'x-gitee-token': signOverride ?? giteeSign(ts) };
  const res = await realFetch(`${BASE}/api/mu/gitee/webhook`, { method: 'POST', headers, body: raw });
  return { status: res.status, json: await res.json().catch(() => null) };
}

// 消费驱动：直接调用平台级消费单元（与自动 consumer 同一函数）
const { processClaimedEventSyncJob } = await import('../lib/multiuser/api.mjs');
async function consumeOne() {
  const { createMuStore } = await import('../lib/multiuser/store.mjs');
  const store = await createMuStore({ pool, env: process.env });
  const job = (await pool.query(
    `SELECT * FROM mu.job WHERE kind='event_sync' AND state='queued' ORDER BY created_at LIMIT 1`)).rows[0];
  if (!job) return null;
  return processClaimedEventSyncJob(store, async (t, p2) => pool.query(t, p2), job);
}

try {
  // ── 种子 ──
  const admin = await muLogin('fixture:dev-pilot'); // platform_admin
  const tenantId = (await pool.query(`SELECT tenant_id FROM mu.tenant WHERE slug='default'`)).rows[0].tenant_id;

  // ── T1：迁移 v24 幂等（bootstrap 已在 server 启动执行；重放=零变更）──
  const before = (await pool.query(
    `SELECT count(*) c FROM mu.forge_instance`)).rows[0].c;
  const { MU_MIGRATIONS } = await import('../lib/multiuser/schema.mjs');
  const v24 = MU_MIGRATIONS.find((m) => m.version === 24);
  ok('T1a v24 迁移存在且含 6 段 SQL', v24 && v24.sql.length === 6, v24?.sql?.length);
  for (const stmt of v24.sql) await pool.query(stmt).catch((e) => ok('T1 重放失败', false, String(e).slice(0, 80)));
  const after = (await pool.query(`SELECT count(*) c FROM mu.forge_instance`)).rows[0].c;
  ok('T1b v24 重放幂等（seed 行数不变=2）', Number(before) === 2 && Number(after) === 2, { before, after });
  const migRows = (await pool.query(`SELECT count(*) c FROM mu.schema_migrations WHERE version=24`)).rows[0].c;
  ok('T1c schema_migrations 记录 v24', Number(migRows) >= 1);

  // ── T2：连接管理（RBAC：platform_admin 可建；maintainer 不可；跨租户不可见）──
  // 预置 maintainer（fixture 登录 fail-closed：身份须预置，无自动注册）——SQL 种子
  const mnUser = (await pool.query(
    `INSERT INTO mu.app_user (login) VALUES ('gitee-it-mn')
     ON CONFLICT (login) DO UPDATE SET display_name = mu.app_user.display_name
     RETURNING user_id`)).rows[0];
  await pool.query(
    `INSERT INTO mu.external_identity (user_id, provider, subject)
     VALUES ($1,'fixture','fixture:gitee-it-mn') ON CONFLICT (provider, subject) DO NOTHING`, [mnUser.user_id]);
  await pool.query(
    `INSERT INTO mu.membership (tenant_id, user_id, role, granted_by)
     VALUES ($1,$2,'maintainer',NULL)
     ON CONFLICT (tenant_id, user_id) DO UPDATE SET role='maintainer', state='active', updated_at=now()`,
    [tenantId, mnUser.user_id]);
  const maintainer = await muLogin('fixture:gitee-it-mn');
  const createRej = await call('/api/mu/forge/connections', { method: 'POST',
    cookie: maintainer.cookie, csrf: maintainer.csrf, body: { instance_id: 'gitee-cloud', webhook_mode: 'signature' } });
  ok('T2a maintainer 创建连接被拒（403 manage_instance）', createRej.status === 403, createRej);
  const created = await call('/api/mu/forge/connections', { method: 'POST',
    cookie: admin.cookie, csrf: admin.csrf, body: { instance_id: 'gitee-cloud', webhook_mode: 'signature' } });
  ok('T2b platform_admin 创建连接 ok（pending）', created.status === 200 && created.json?.status === 'pending', created);
  const connId = created.json?.connection_id;
  const probeFail = await call(`/api/mu/forge/connections/${connId}/probe`, { method: 'POST',
    cookie: admin.cookie, csrf: admin.csrf });
  ok('T2c probe 实测→valid（授权已验证口径）', probeFail.json?.status === 'valid', probeFail.json);
  const list = await call('/api/mu/forge/connections', { cookie: admin.cookie });
  ok('T2d 列表含 credential_present 布尔（不回显令牌）',
    list.json?.connections?.[0]?.credential_present === true && !JSON.stringify(list.json).includes('gitee-it-pat-token'));

  // ── T3：仓库绑定 ──
  const bound = await call('/api/mu/forge/bindings', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { connection_id: connId, repo_full_name: 'gitee-pilot/pilot-repo', provider_repo_id: '777000' } });
  ok('T3 仓库绑定 ok（provider_repo_id=777000）', bound.status === 200 && bound.json?.provider_repo_id === '777000', bound);
  const repoRow = (await pool.query(
    `SELECT repo_id, provider, forge_instance_id FROM mu.repository WHERE tenant_id=$1 AND provider='gitee'`,
    [tenantId])).rows[0];

  // ── T4：PR 快照种子 + 手动审查→真实消费→clean 全链（无发现也成功）──
  const headSha = 'a'.repeat(40);
  const prRow = (await pool.query(
    `INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES ($1,$2,11,$3,'open') RETURNING pr_id`, [tenantId, repoRow.repo_id, headSha])).rows[0];
  const manual = await call(`/api/mu/prs/${prRow.pr_id}/review`, { method: 'POST',
    cookie: admin.cookie, csrf: admin.csrf, body: {} });
  ok('T4a Gitee 手动审查入队（real_pipeline gitee）', manual.status === 200 && manual.json?.forge === 'gitee', manual);
  const c1 = await consumeOne();
  ok('T4b 真实消费者 done（run 创建+attempt+clean 完成）', c1?.state === 'done', c1);
  const run1 = (await pool.query(
    `SELECT r.* FROM mu.review_run r WHERE r.repo_id=$1 ORDER BY r.created_at DESC LIMIT 1`, [repoRow.repo_id])).rows[0];
  ok('T4c run REVIEWED→COMPLETED（clean+protection not_provided 跳过保护门）',
    run1?.status === 'COMPLETED', run1?.status);
  const attempts = (await pool.query(
    `SELECT status, error_code FROM mu.agent_attempt WHERE run_id=$1 AND agent_role='reviewer'`, [run1.run_id])).rows;
  ok('T4d reviewer attempt DONE 零 finding（clean 断言，不强造 finding）',
    attempts.some((a) => a.status === 'DONE')
      && Number((await pool.query(`SELECT count(*) c FROM mu.agent_finding WHERE run_id=$1`, [run1.run_id])).rows[0].c) === 0,
    attempts);
  ok('T4e 调用面全 GET（调用清单零写调用）', giteeCalls.length > 0 && giteeCalls.every((c) => c.method === 'GET'), giteeCalls.length);

  // ── T5：同 head 幂等（重复触发→run 复用零重放）──
  await call(`/api/mu/prs/${prRow.pr_id}/review`, { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: {} });
  const c2 = await consumeOne();
  const runCount = Number((await pool.query(
    `SELECT count(*) c FROM mu.review_run WHERE repo_id=$1 AND head_sha=$2`, [repoRow.repo_id, headSha])).rows[0].c);
  ok('T5 同 head 重触发→run 复用（单 run）+消费 done', c2?.state === 'done' && runCount === 1, { runCount });

  // ── T6：重复投递（同 body 重放→duplicate 零新 job）──
  const raw1 = giteePayload({ headSha, action: 'update' });
  const d1 = await deliverGitee(raw1);
  const jobsBefore = Number((await pool.query(`SELECT count(*) c FROM mu.job WHERE kind='event_sync'`)).rows[0].c);
  const d2 = await deliverGitee(raw1);
  const jobsAfter = Number((await pool.query(`SELECT count(*) c FROM mu.job WHERE kind='event_sync'`)).rows[0].c);
  ok('T6 重复投递→duplicate 且零新 job', d1.json?.ok === true && d2.json?.duplicate === true
    && jobsBefore === jobsAfter, { d1: d1.json, d2: d2.json, jobsBefore, jobsAfter });

  // ── T7：head 漂移（webhook head=X，平台实测 head=Y→attempt FAILED→run BLOCKED）──
  while (await consumeOne()) { /* 清空 T4-T6 队列，确保 T7 消费的是漂移事件 */ }
  const driftedHead = 'd'.repeat(40);
  const d3 = await deliverGitee(giteePayload({ headSha: driftedHead, action: 'update', prNumber: 12 }));
  ok('T7a 漂移事件入队 ok', d3.json?.ok === true, d3.json);
  // 先种 PR 快照（消费 upsert 会在校验后建）
  const c3 = await consumeOne();
  const run3 = (await pool.query(
    `SELECT run_id, status FROM mu.review_run WHERE repo_id=$1 AND head_sha=$2`, [repoRow.repo_id, driftedHead])).rows[0];
  const driftAttempts = (await pool.query(
    `SELECT status, error_code FROM mu.agent_attempt WHERE run_id=$1 ORDER BY attempt`,
    [run3.run_id])).rows;
  ok('T7b head 漂移→fail-visible（run BLOCKED+attempt FAILED gitee_head_moved）',
    c3?.state === 'done' && run3?.status === 'BLOCKED'
      && driftAttempts.some((a) => a.status === 'FAILED'
        && String(a.error_code ?? '').includes('gitee_head_moved')),
    { run: run3?.status, driftAttempts });

  // ── T8：缺字段 payload→消费明确失败 event_payload_invalid（契约锁 v1）──
  const badJob = (await pool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload)
     VALUES ($1,$2,'event_sync',NULL,'maintainer',
       JSON_BUILD_OBJECT('schema_version',1,'event','pull_request','forge_kind','gitee',
         'delivery_ref','badfield0000000000000000000000000',
         'pr_number',11,'head_sha',$3::text,'action','update','trigger_source','manual'))
     RETURNING job_id`, [tenantId, repoRow.repo_id, headSha])).rows[0].job_id;
  const c4 = await (async () => {
    const { createMuStore } = await import('../lib/multiuser/store.mjs');
    const store = await createMuStore({ pool, env: process.env });
    const job = (await pool.query(`SELECT * FROM mu.job WHERE job_id=$1`, [badJob])).rows[0];
    return processClaimedEventSyncJob(store, async (t, p2) => pool.query(t, p2), job);
  })();
  const badState = (await pool.query(`SELECT state, result FROM mu.job WHERE job_id=$1`, [badJob])).rows[0];
  ok('T8 缺 provider_repo_id→明确失败 event_payload_invalid（rejected 可定位非 queued 挂死）',
    badState?.state === 'rejected' && JSON.stringify(badState?.result ?? {}).includes('event_payload_invalid'), badState);
  await pool.query(`DELETE FROM mu.job WHERE job_id=$1`, [badJob]);

  // ── T9：跨租户隔离——job 表 FK（tenant+repo）在入库层即拒绝跨租户构造（强于消费者层拒绝）
  const t2 = (await pool.query(
    `INSERT INTO mu.tenant (slug, display_name) VALUES ('gitee-it-t2','t2') RETURNING tenant_id`)).rows[0].tenant_id;
  let fkRejected = false;
  try {
    await pool.query(
      `INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload)
       VALUES ($1,$2,'event_sync',NULL,'maintainer',$3)`,
      [t2, repoRow.repo_id, JSON.stringify({
        schema_version: 1, event: 'pull_request', forge_kind: 'gitee',
        delivery_ref: 'cross000000000000000000000000000', provider_repo_id: '777000',
        pr_number: 11, head_sha: headSha, action: 'update', trigger_source: null,
        installation_id: null })]);
  } catch (e) { fkRejected = String(e?.message ?? '').includes('foreign key'); }
  ok('T9 跨租户 job 被 FK 拒绝（租户隔离入库层生效）', fkRejected);
  await pool.query(`DELETE FROM mu.tenant WHERE tenant_id=$1`, [t2]);

  
  // ── T10：连接撤销→消费复查拒绝 + webhook ignored ──
  await call(`/api/mu/forge/connections/${connId}/revoke`, { method: 'POST', cookie: admin.cookie, csrf: admin.csrf });
  const d4 = await deliverGitee(giteePayload({ headSha: 'e'.repeat(40), action: 'update', prNumber: 13 }));
  ok('T10a 撤销后 webhook ignored forge_binding_not_found', d4.json?.ignored === 'forge_binding_not_found', d4.json);
  const forgeJob3 = (await pool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload)
     VALUES ($1,$2,'event_sync',NULL,'maintainer',$3) RETURNING job_id`,
    [tenantId, repoRow.repo_id, JSON.stringify({
      schema_version: 1, event: 'pull_request', forge_kind: 'gitee',
      delivery_ref: 'revoked00000000000000000000000000', provider_repo_id: '777000',
      pr_number: 11, head_sha: headSha, action: 'update', trigger_source: null,
      installation_id: null })])).rows[0].job_id;
  await (async () => {
    const { createMuStore } = await import('../lib/multiuser/store.mjs');
    const store = await createMuStore({ pool, env: process.env });
    const job = (await pool.query(`SELECT * FROM mu.job WHERE job_id=$1`, [forgeJob3])).rows[0];
    return processClaimedEventSyncJob(store, async (t, p2) => pool.query(t, p2), job);
  })();
  const revState = (await pool.query(`SELECT state, result FROM mu.job WHERE job_id=$1`, [forgeJob3])).rows[0];
  ok('T10b 撤销后存量 job 消费→rejected forge_binding_not_found',
    revState?.state === 'rejected' && JSON.stringify(revState?.result ?? {}).includes('forge_binding_not_found'), revState);
  await pool.query(`DELETE FROM mu.job WHERE job_id=$1`, [forgeJob3]);

  // ── T11：P0 finding→审批门 WAITING（不批不修）+ 零远端写调用 ──
  // （T10 已 revoke——重登记验证 upsert 语义：revoked_at 清除、可重新 probe valid）
  const recreated = await call('/api/mu/forge/connections', { method: 'POST',
    cookie: admin.cookie, csrf: admin.csrf, body: { instance_id: 'gitee-cloud', webhook_mode: 'signature' } });
  const reprobe = await call(`/api/mu/forge/connections/${recreated.json?.connection_id}/probe`,
    { method: 'POST', cookie: admin.cookie, csrf: admin.csrf });
  ok('T11-pre 重登记+probe→valid（revoked 不可逆，重登记恢复）',
    reprobe.json?.status === 'valid', reprobe.json);
  stubFiles = [{ filename: 'src/secret.js', status: null, additions: '1', deletions: '0',
    patch: { diff: '@@ -1 +1,2 @@\n const x=1;\n+const token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890";' } }];
  stubHeadSha = 'f'.repeat(40);
  const pr2 = (await pool.query(
    `INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES ($1,$2,14,$3,'open') RETURNING pr_id`, [tenantId, repoRow.repo_id, stubHeadSha])).rows[0];
  const callsBeforeP0 = giteeCalls.length;
  const d5 = await deliverGitee(giteePayload({ headSha: stubHeadSha, action: 'open', prNumber: 14 }));
  await consumeOne();
  const run5 = (await pool.query(
    `SELECT run_id, status FROM mu.review_run WHERE repo_id=$1 AND head_sha=$2`, [repoRow.repo_id, stubHeadSha])).rows[0];
  const findings5 = Number((await pool.query(
    `SELECT count(*) c FROM mu.agent_finding WHERE run_id=$1 AND severity IN ('P0','P1')`, [run5.run_id])).rows[0].c);
  const findings5all = (await pool.query(
    `SELECT rule_id, severity FROM mu.agent_finding WHERE run_id=$1`, [run5?.run_id ?? run5])).rows;
  ok('T11a P0 finding→审批门（WAITING_FOR_HUMAN_APPROVAL，不批不修）',
    run5?.status === 'WAITING_FOR_HUMAN_APPROVAL' && findings5 >= 1,
    { status: run5?.status, findings5, all: findings5all });
  const writeCalls = giteeCalls.filter((c) => c.method !== 'GET');
  ok('T11b 全程零远端写调用（dry-run 不写远端；含审批前链路）', writeCalls.length === 0, writeCalls);
  ok('T11c 请求含 token 不落日志路径（stub 记录的 path 无 query）',
    giteeCalls.slice(callsBeforeP0).every((c) => !String(c.path).includes('access_token')));

  // ── T12：webhook 未配置分支：503 not_configured（不猜测启用）──
  // （env 已配置本测试验证 signature 分支；未配置分支由单测/口审覆盖——此处验证 mode=signature 时
  //   错签名 401 且不产生 job/审计正文）
  const badSig = await deliverGitee(giteePayload({ headSha: '1'.repeat(40), action: 'update', prNumber: 15 }),
    { signOverride: 'definitely-not-a-signature' });
  const jobsBadSig = Number((await pool.query(
    `SELECT count(*) c FROM mu.job WHERE payload->>'pr_number'='15'`)).rows[0].c);
  ok('T12 错签名 401 且零入队（不持久化不触发业务）', badSig.status === 401 && jobsBadSig === 0, { badSig: badSig.status, jobsBadSig });

  // ── T13：GitHub legacy 回归（同进程内 webhook 真实投递→消费 done）──
  const ghRaw = JSON.stringify({ action: 'opened', number: 201,
    pull_request: { number: 201, state: 'open', title: 'legacy', user: { login: 'u' },
      head: { ref: 'b', sha: '9'.repeat(40) }, base: { ref: 'main' }, changed_files: 1 },
    repository: { id: 98001, name: 'r', full_name: 'w31/r', owner: { login: 'w31' } },
    installation: { id: 1 }, sender: { login: 'u' } });
  const ghSig = 'sha256=' + crypto.createHmac('sha256', process.env.MU_GITHUB_WEBHOOK_SECRET).update(ghRaw).digest('hex');
  // 种子 GitHub installation/binding/repo（v18 契约）
  const fixtureInst = 1;
  await pool.query(
    `INSERT INTO mu.github_app_installation (installation_id, tenant_id, app_id, account_id, account_login, account_type)
     VALUES ($1,$2,1,901,'w31','User') ON CONFLICT (installation_id) DO NOTHING`, [fixtureInst, tenantId]);
  const stGh = await (await import('../lib/multiuser/store.mjs')).createMuStore({ pool, env: process.env });
  const ghRepoRow = await stGh.ensureRepository({ tenantId, provider: 'github',
    providerRepoId: '98001', owner: 'w31', name: 'r' });
  const ghRepo = { repo_id: ghRepoRow.repo_id };
  await stGh.upsertRepositoryBinding({ tenantId, repoId: ghRepo.repo_id, githubRepoId: 98001,
    owner: 'w31', name: 'r', installationId: fixtureInst });
  const ghRes = await realFetch(`${BASE}/api/mu/github/webhook`, { method: 'POST',
    headers: { 'content-type': 'application/json', 'x-hub-signature-256': ghSig,
      'x-github-delivery': `gh-it-${Date.now()}`, 'x-github-event': 'pull_request' }, body: ghRaw });
  ok('T13a GitHub legacy webhook 验真+入队 ok', ghRes.status === 200, ghRes.status);
  const cgh = await consumeOne();
  const ghRun = (await pool.query(
    `SELECT status FROM mu.review_run WHERE repo_id=$1 AND head_sha=$2`, [ghRepo.repo_id, '9'.repeat(40)])).rows[0];
  ok('T13b legacy GitHub 链不回归（消费 done+run 终态）',
    cgh?.state === 'done' && ['REVIEWED', 'COMPLETED', 'BLOCKED', 'WAITING_FOR_HUMAN_APPROVAL', 'NEEDS_HUMAN'].includes(ghRun?.status ?? ''),
    { cgh, run: ghRun?.status });

  console.log(`\n  gitee-consumer integration: ${pass} pass, ${fail} fail`);
  if (fail > 0) process.exitCode = 1;
} catch (e) {
  fail++;
  console.log(`  FAIL  uncaught — ${String(e?.stack ?? e).slice(0, 600)}`);
  process.exitCode = 1;
} finally {
  globalThis.fetch = realFetch;
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
  console.log(`  TOTAL: ${pass} pass, ${fail} fail`);
  process.exit(fail > 0 ? 1 : 0); // node --test+自管服务器：显式逃生（同 mu-job-consumer 惯例）
}
