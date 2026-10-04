#!/usr/bin/env node
// console/backend/test/mu-tenant-boundary.integration.mjs — rc.10 PR-E 租户边界集成测试。
// 覆盖六项（外部租户准入前收窄）：
//   TB1 撤权后会话即时失效（ISO-1：authGate 401；/api/mu 面 403 membership_inactive 不变）
//   TB2 ragtrial query_log digest-only + MU 桥会话 tenant 归属（ISO-2/SEC-4）
//   TB3 jobs/tick 按会话租户领取/回显（ISO-3）
//   TB4 agentteams-status 只回本租户统计（ISO-4）
//   TB5 webhook_delivery.tenant_id 解析后回填（ISO-5）
//   TB6 v22 迁移：skill_version 复合 FK + 预检 fail-visible + egress 归属 FK（ISO-6）
// 自起一次性 pgvector/pgvector:pg16（ragtrial 需 vector 扩展；随机回环端口，finally 清理）。
// 身份/凭据/secret 全部为合成 fixture——真实值零出现。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const HERE = path.dirname(fileURLToPath(import.meta.url));
const { createConsole } = await import('../server.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 300) : ''}`); }
};
const sha256hex = (v) => crypto.createHash('sha256').update(String(v)).digest('hex');

const CTR = `mu-tb-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17820 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'pgvector/pgvector:pg16'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
{
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); await p.end(); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}

const savedEnv = { ...process.env };
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_SESSION_SECRET = 'mu-tb-it-session-secret';
process.env.CONSOLE_PILOT_USER = 'tb-pilot';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.MU_FIXTURES = '1';
// RAG（TB2）：scope 显式白名单 + A 链内部 ragtrial 接线（系统通道 query_log 对照行）
process.env.RAGTRIAL_ALLOWED_SCOPES = 'tb/docs@main';
process.env.MERGEPILOT_ORG_RAG_A_CHAIN = '1';
process.env.MERGEPILOT_RAG_TRIAL_A_CHAIN = 'ragtrial';
process.env.RAGTRIAL_A_CHAIN_REPO = 'tb/docs';
process.env.RAGTRIAL_A_CHAIN_BRANCH = 'main';
// agentteams（TB4）：不可达 controller——健康探测 fail-closed，DB 统计面仍按租户执行
process.env.MU_EXECUTOR = 'agentteams';
process.env.MU_AGENTTEAMS_BASE_URL = 'http://127.0.0.1:1';
process.env.MU_AGENTTEAMS_TOKEN = 'tb-synthetic-token-not-a-secret';
// GitHub App（TB5 webhook 回填）：全部合成值
process.env.MU_GITHUB_APP_ID = '999902';
process.env.MU_GITHUB_APP_SLUG = 'tb-it-app';
process.env.MU_GITHUB_APP_PRIVATE_KEY = 'tb-synthetic-not-a-real-key';
process.env.MU_GITHUB_WEBHOOK_SECRET = 'tb-it-webhook-secret-synthetic';

const { server } = await Promise.resolve(createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') }));
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

async function muLogin(subject, tenantSlug = null) {
  const res = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject, ...(tenantSlug ? { tenant_slug: tenantSlug } : {}) }),
  });
  const cookie = (res.headers.get('set-cookie') || '').split(';')[0];
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, cookie, csrf: json?.csrf ?? null, json };
}
async function call(p, { method = 'GET', body = null, cookie = null, csrf = null } = {}) {
  const res = await fetch(BASE + p, {
    method,
    headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
      ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}

try {
  // ══ Setup：tenant A（default）admin + maintainer；tenant B（admin 创建并切换后加成员）══
  const admin = await muLogin('fixture:tb-pilot');
  if (admin.status !== 200) throw new Error('admin login failed');
  const tenantA = admin.json?.tenant?.tenant_id;
  await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: { login: 'tb-maint', role: 'maintainer' } });
  await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: { login: 'tb-worker', role: 'contributor' } });
  const maint = await muLogin('fixture:tb-maint');
  const worker = await muLogin('fixture:tb-worker');

  const mkB = await call('/api/mu/tenants', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { slug: 'tb-ten-b', display_name: 'TB Tenant B' } });
  const tenantB = mkB.json?.tenant?.tenant_id;
  const swB = await fetch(BASE + '/api/mu/auth/tenant', {
    method: 'POST', headers: { cookie: admin.cookie, 'content-type': 'application/json', 'x-csrf-token': admin.csrf },
    body: JSON.stringify({ tenant_id: tenantB }) });
  const adminBCookie = (swB.headers.get('set-cookie') || '').split(';')[0];
  const adminBCsrf = (await swB.json())?.csrf;
  await call('/api/mu/members', { method: 'POST', cookie: adminBCookie, csrf: adminBCsrf, body: { login: 'tb-b-op', role: 'platform_admin' } });
  // 切回 A（会话轮换——重新捕获）
  const swA = await fetch(BASE + '/api/mu/auth/tenant', {
    method: 'POST', headers: { cookie: adminBCookie, 'content-type': 'application/json', 'x-csrf-token': adminBCsrf },
    body: JSON.stringify({ tenant_id: tenantA }) });
  const adminA2 = { cookie: (swA.headers.get('set-cookie') || '').split(';')[0],
    csrf: (await swA.json())?.csrf };
  const bop = await muLogin('fixture:tb-b-op', 'tb-ten-b');
  ok('SETUP 双租户成员在位（A: admin/maint/worker；B: admin/b-op）',
    bop.status === 200 && bop.json?.role === 'platform_admin' && Boolean(tenantA && tenantB && maint.cookie && worker.cookie),
    { tenantA, tenantB, bop: bop.status });

  // ══ TB1 撤权后会话即时失效（ISO-1）══
  const ovBefore = await call('/api/overview', { cookie: worker.cookie });
  ok('TB1a 撤权前 contributor 会话经 authGate → /api/overview 200',
    ovBefore.status === 200, { status: ovBefore.status });
  const rev = await call('/api/mu/members/tb-worker/revoke', { method: 'POST', cookie: adminA2.cookie, csrf: adminA2.csrf });
  const ovAfter = await call('/api/overview', { cookie: worker.cookie });
  ok('TB1b 撤权后同 cookie /api/overview → 401（authGate 不再回退 session.role 快照）',
    rev.status === 200 && ovAfter.status === 401, { rev: rev.status, ov: ovAfter.status, body: ovAfter.json });
  const sessAfter = await call('/api/mu/session', { cookie: worker.cookie });
  ok('TB1c 撤权后 /api/mu/session → 403 membership_inactive（mu 面语义不变）',
    sessAfter.status === 403 && sessAfter.json?.error?.reason === 'membership_inactive', sessAfter.json);
  const ragAfter = await call('/api/rag-trial/status', { cookie: worker.cookie });
  ok('TB1d 撤权后 RAG 桥接面 → 401（muAuthBridge live membership 独立校验）',
    ragAfter.status === 401, { status: ragAfter.status });

  // ══ TB2 query_log digest-only + 租户归属（ISO-2/SEC-4）══
  const bind = await call('/api/mu/repositories', { method: 'POST', cookie: maint.cookie, csrf: maint.csrf,
    body: { provider_repo_id: 'TB_GH_1', owner: 'tb', name: 'docs' } });
  const repoAId = bind.json?.repository?.repo_id;
  ok('TB2a maintainer 绑定仓库（fixture 域安装）', bind.status === 200 && Boolean(repoAId), bind.json);
  const ing = await call('/api/rag-trial/ingest', { method: 'POST', cookie: maint.cookie, csrf: maint.csrf,
    body: { repo: 'tb/docs', branch: 'main', docs: [{ path: 'policy.md', text: '最小权限原则。租户边界收窄。' }] } });
  ok('TB2b MU 桥会话 ingest 成功（scope∩repos 双门通过）', ing.status === 200, ing.json);
  const QTEXT = '租户边界机密查询词';
  const qr = await call('/api/rag-trial/query', { method: 'POST', cookie: maint.cookie, csrf: maint.csrf,
    body: { q: QTEXT, repo: 'tb/docs', branch: 'main', k: 3 } });
  ok('TB2c MU 桥会话 query 成功', qr.status === 200 && qr.json?.service_state !== undefined, { status: qr.status });
  const row = (await pool.query(
    `SELECT query_text, query_digest, tenant_id, actor FROM ragtrial.query_log ORDER BY seq DESC LIMIT 1`)).rows[0];
  ok('TB2d query_log 新行：query_text 停写（NULL）+ query_digest=sha256 64hex + tenant_id=会话租户',
    row?.query_text === null && row?.query_digest === sha256hex(QTEXT)
      && /^[0-9a-f]{64}$/.test(row?.query_digest ?? '') && String(row?.tenant_id ?? '') === String(tenantA),
    row);
  const badDigest = await pool.query(
    `INSERT INTO ragtrial.query_log (actor, state, latency_ms, query_digest)
     VALUES ('tb','hit',1,'not-a-digest')`).then(() => false, () => true);
  ok('TB2e query_digest CHECK 定形（非 64hex 拒写）', badDigest === true);
  // 系统通道（A 链内部 ragtrial 接线）：tenant 保持 NULL（历史语义）
  const ach = await call('/api/rag/org-search?q=边界词', { cookie: maint.cookie });
  const achRow = (await pool.query(
    `SELECT query_text, query_digest, tenant_id, actor FROM ragtrial.query_log
      WHERE actor='a-chain-org-search' ORDER BY seq DESC LIMIT 1`)).rows[0];
  ok('TB2f 系统通道（A 链）行：tenant_id NULL（legacy 语义）+ digest-only 一致',
    ach.status === 200 && achRow?.query_text === null && /^[0-9a-f]{64}$/.test(achRow?.query_digest ?? '')
      && achRow?.tenant_id === null, { status: ach.status, row: achRow });

  // ══ TB3 jobs/tick 租户收窄（ISO-3）══
  const adminUid = (await pool.query(`SELECT user_id FROM mu.app_user WHERE login='tb-pilot'`)).rows[0].user_id;
  const repoB = (await pool.query(
    `INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
     VALUES ($1,'github','TB_B_R','tb','b-repo') RETURNING repo_id`, [tenantB])).rows[0].repo_id;
  const jobA = (await pool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload)
     VALUES ($1,$2,'review_run',$3,'maintainer',$4::jsonb) RETURNING job_id`,
    [tenantA, repoAId, adminUid, JSON.stringify({ head_sha: 'ab'.repeat(20) })])).rows[0].job_id;
  const jobB = (await pool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload)
     VALUES ($1,$2,'review_run',$3,'maintainer',$4::jsonb) RETURNING job_id`,
    [tenantB, repoB, adminUid, JSON.stringify({ head_sha: 'cd'.repeat(20) })])).rows[0].job_id;
  const tickA = await call('/api/mu/jobs/tick', { method: 'POST', cookie: maint.cookie, csrf: maint.csrf });
  const jobIdsA = (tickA.json?.processed ?? []).map((x) => String(x.job_id));
  const stateB = (await pool.query(`SELECT state FROM mu.job WHERE job_id=$1`, [jobB])).rows[0]?.state;
  ok('TB3a A 租户 tick：processed 只含本租户 job（B job 不可见）',
    tickA.status === 200 && jobIdsA.includes(String(jobA)) && !jobIdsA.includes(String(jobB)), { jobIdsA, jobA, jobB });
  ok('TB3b B 租户 queued job 未被 A tick 领取（仍 queued）', stateB === 'queued', { stateB });
  const tickB = await call('/api/mu/jobs/tick', { method: 'POST', cookie: bop.cookie, csrf: bop.csrf });
  const jobIdsB = (tickB.json?.processed ?? []).map((x) => String(x.job_id));
  ok('TB3c B 租户 tick：只领取本租户 job（A 已终态 job 不再出现）',
    tickB.status === 200 && jobIdsB.includes(String(jobB)) && !jobIdsB.includes(String(jobA)), { jobIdsB });
  const orphanA = (await pool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload, state, locked_at, created_at)
     VALUES ($1,$2,'review_run',$3,'maintainer',$4::jsonb,'running', now() - interval '30 minutes', now())
     RETURNING job_id`,
    [tenantA, repoAId, adminUid, JSON.stringify({ head_sha: 'ef'.repeat(20) })])).rows[0].job_id;
  const orphanB = (await pool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload, state, locked_at, created_at)
     VALUES ($1,$2,'review_run',$3,'maintainer',$4::jsonb,'running', now() - interval '30 minutes', now())
     RETURNING job_id`,
    [tenantB, repoB, adminUid, JSON.stringify({ head_sha: '12'.repeat(20) })])).rows[0].job_id;
  const tickOrphan = await call('/api/mu/jobs/tick', { method: 'POST', cookie: maint.cookie, csrf: maint.csrf });
  const orphanIds = (tickOrphan.json?.processed ?? []).filter((x) => x.state === 'requeued').map((x) => String(x.job_id));
  const orphanBState = (await pool.query(`SELECT state FROM mu.job WHERE job_id=$1`, [orphanB])).rows[0]?.state;
  ok('TB3d 孤立 running 回收只触本租户（A orphan 回队；B orphan 原样）',
    orphanIds.includes(String(orphanA)) && !orphanIds.includes(String(orphanB)) && orphanBState === 'running',
    { orphanIds, orphanBState });

  // ══ TB4 agentteams-status 租户收窄（ISO-4）══
  // 播种两租户各异的 queued job 数（backlog 可区分）+ 各一条本租户死信（原因标记隔离）
  for (const [tid, rid, n] of [[tenantA, repoAId, 2], [tenantB, repoB, 1]]) {
    for (let i = 0; i < n; i++) {
      await pool.query(
        `INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload)
         VALUES ($1,$2,'review_run',$3,'maintainer',$4::jsonb)`,
        [tid, rid, adminUid, JSON.stringify({ head_sha: `${i}f`.repeat(20).slice(0, 40) })]);
    }
  }
  await pool.query(
    `INSERT INTO mu.dead_letter (tenant_id, kind, reason, job_id)
     VALUES ($1,'at_round_failed','A-TENANT-ONLY-REASON-MARKER', gen_random_uuid())`, [tenantA]);
  await pool.query(
    `INSERT INTO mu.dead_letter (tenant_id, kind, reason, job_id)
     VALUES ($1,'at_round_failed','B-TENANT-ONLY-REASON-MARKER', gen_random_uuid())`, [tenantB]);
  const stA = await call('/api/mu/agentteams-status', { cookie: adminA2.cookie });
  const queuedA = Number((await pool.query(
    `SELECT count(*)::int c FROM mu.job WHERE tenant_id=$1 AND state='queued'`, [tenantA])).rows[0].c);
  const queuedB = Number((await pool.query(
    `SELECT count(*)::int c FROM mu.job WHERE tenant_id=$1 AND state='queued'`, [tenantB])).rows[0].c);
  ok('TB4a A admin：queue_backlog=本租户 queued 数（不含 B）',
    stA.status === 200 && Number(stA.json?.queue_backlog ?? -1) === queuedA && queuedA !== queuedB,
    { backlog: stA.json?.queue_backlog, queuedA, queuedB });
  const stAText = JSON.stringify(stA.json ?? {});
  ok('TB4b A admin：dead_letter_open=1 且零 B 租户死信原因泄露',
    Number(stA.json?.dead_letter_open ?? -1) === 1 && stAText.includes('A-TENANT-ONLY-REASON-MARKER')
      && !stAText.includes('B-TENANT-ONLY-REASON-MARKER'), { open: stA.json?.dead_letter_open });
  const stB = await call('/api/mu/agentteams-status', { cookie: bop.cookie });
  const stBText = JSON.stringify(stB.json ?? {});
  ok('TB4c B platform_admin：只见本租户死信（B 标记在，A 标记零泄露）',
    stB.status === 200 && Number(stB.json?.dead_letter_open ?? -1) === 1
      && stBText.includes('B-TENANT-ONLY-REASON-MARKER') && !stBText.includes('A-TENANT-ONLY-REASON-MARKER'),
    { open: stB.json?.dead_letter_open });

  // ══ TB5 webhook_delivery.tenant_id 回填（ISO-5）══
  const WEBHOOK_SECRET = process.env.MU_GITHUB_WEBHOOK_SECRET;
  const sign = (body) => 'sha256=' + crypto.createHmac('sha256', WEBHOOK_SECRET).update(body).digest('hex');
  await pool.query(
    `INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id)
     VALUES (79101, $1, 5, 'tb-org', 999902) ON CONFLICT DO NOTHING`, [tenantA]);
  const repoBind = (await pool.query(
    `INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
     VALUES ($1,'github','93001','tb','bound-repo') RETURNING repo_id`, [tenantA])).rows[0].repo_id;
  await pool.query(
    `INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id)
     VALUES ($1,$2,93001,'tb','bound-repo',79101) ON CONFLICT DO NOTHING`, [tenantA, repoBind]);
  const hook = async (delivery, payload) => {
    const raw = JSON.stringify(payload);
    return fetch(BASE + '/api/mu/github/webhook', { method: 'POST',
      headers: { 'content-type': 'application/json', 'x-github-event': 'pull_request',
        'x-github-delivery': delivery, 'x-hub-signature-256': sign(raw) },
      body: raw });
  };
  await hook('tb-d-1', { installation: { id: 79101 }, repository: { id: 93001 }, action: 'opened',
    pull_request: { number: 5, head: { sha: 'aa'.repeat(20) } } });
  const d1 = (await pool.query(
    `SELECT tenant_id, state FROM mu.webhook_delivery WHERE delivery_id='tb-d-1'`)).rows[0];
  ok('TB5a 合法 installation：delivery 行 tenant_id 由 NULL 回填为解析租户',
    String(d1?.tenant_id ?? '') === String(tenantA) && d1?.state === 'processed', d1);
  await hook('tb-d-2', { installation: { id: 424242 }, repository: { id: 1 }, action: 'opened',
    pull_request: { number: 6, head: { sha: 'bb'.repeat(20) } } });
  const d2 = (await pool.query(
    `SELECT tenant_id, state FROM mu.webhook_delivery WHERE delivery_id='tb-d-2'`)).rows[0];
  ok('TB5b 未知 installation：rejected 行 tenant_id 保持 NULL（不虚构归属）',
    d2?.tenant_id === null && d2?.state === 'rejected', d2);

  // ══ TB6 v22 迁移（ISO-6：复合 FK + 预检 + egress 归属）══
  const v22row = (await pool.query(
    `SELECT name FROM mu.schema_migrations WHERE version=22`)).rows[0];
  ok('TB6a v22（mu_tenant_boundary_pr_e）已应用', v22row?.name === 'mu_tenant_boundary_pr_e', v22row);
  const fk = (await pool.query(
    `SELECT conname, cardinality(conkey) cols FROM pg_constraint
      WHERE conname='mu_skill_version_tenant_skill_fk' AND conrelid='mu.skill_version'::regclass AND contype='f'`)).rows[0];
  ok('TB6b skill_version 复合 FK 在位（(tenant_id, skill_id) 两列）', fk?.cols === 2, fk);
  const oldFk = (await pool.query(
    `SELECT count(*)::int c FROM pg_constraint c
      WHERE c.conrelid='mu.skill_version'::regclass AND c.contype='f'
        AND c.confrelid='mu.skill'::regclass AND cardinality(c.conkey)=1`)).rows[0].c;
  ok('TB6c 旧单列 FK 已移除（skill_version 引用 mu.skill 的 FK 仅剩复合）', oldFk === 0, { oldFk });
  const uk = (await pool.query(
    `SELECT 1 FROM pg_indexes WHERE schemaname='mu' AND indexname='mu_skill_tenant_skill_uk'`)).rowCount;
  ok('TB6d mu.skill (tenant_id, skill_id) UNIQUE 索引在位（复合 FK 被引用要求）', uk === 1);
  const egressFk = (await pool.query(
    `SELECT 1 FROM pg_constraint WHERE conname='mu_code_egress_event_tenant_fk'`)).rowCount;
  ok('TB6e code_egress_event 租户归属 FK 在位（fresh 库零失配→强约束）', egressFk === 1);
  const digestChk = (await pool.query(
    `SELECT 1 FROM pg_constraint WHERE conname='ragtrial_query_log_digest_shape'`)).rowCount;
  ok('TB6f ragtrial query_log digest CHECK 在位（v22/ragtrial 双入口收敛）', digestChk === 1);
  // 复合 FK 生效：跨租户 (tenant, skill) 组合被 DB 拒绝
  const skillA = (await pool.query(
    `INSERT INTO mu.skill (tenant_id, skill_key, display_name) VALUES ($1,'tb.k','K') RETURNING skill_id`,
    [tenantA])).rows[0].skill_id;
  const crossTenantVer = await pool.query(
    `INSERT INTO mu.skill_version (tenant_id, skill_id, version, manifest_sha256)
     VALUES ($1,$2,'1.0.0',$3)`, [tenantB, skillA, 'ab'.repeat(32)]).then(() => false, () => true);
  ok('TB6g skill_version 跨租户组合被复合 FK 拒绝', crossTenantVer === true);
  const sameTenantVer = await pool.query(
    `INSERT INTO mu.skill_version (tenant_id, skill_id, version, manifest_sha256)
     VALUES ($1,$2,'1.0.0',$3) RETURNING version_id`, [tenantA, skillA, 'ab'.repeat(32)]);
  ok('TB6h 同租户组合仍可写（复合 FK 不阻断正常路径）', sameTenantVer.rowCount === 1);

  // 预检错误路径：同容器第二库——只升到 v20 → 注入跨租户失配行（单列 FK 放行）→
  // 完整 initSchema 触发 v22 预检 RAISE；修复数据后重放成功（fail-visible + 可恢复）。
  {
    await pool.query(`DROP DATABASE IF EXISTS tb_mig_precheck`).catch(() => {});
    await pool.query(`CREATE DATABASE tb_mig_precheck`);
    const dsn2 = dsn.replace(/\/mu$/, '/tb_mig_precheck');
    const pool2 = new Pool({ connectionString: dsn2 });
    try {
      const { MU_MIGRATIONS } = await import('../lib/multiuser/schema.mjs');
      for (const m of MU_MIGRATIONS) {
        if (Number(m.version) > 20) break;
        const r = await pool2.query(`SELECT 1 FROM mu.schema_migrations WHERE version=$1`, [m.version]).catch(async () => {
          await pool2.query(`CREATE SCHEMA IF NOT EXISTS mu`);
          await pool2.query(`CREATE TABLE IF NOT EXISTS mu.schema_migrations (
            version INT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
          return pool2.query(`SELECT 1 FROM mu.schema_migrations WHERE version=$1`, [m.version]);
        });
        if (r.rowCount) continue;
        for (const sql of m.sql) await pool2.query(sql);
        await pool2.query(`INSERT INTO mu.schema_migrations (version, name) VALUES ($1,$2)`, [m.version, m.name]);
      }
      const t1 = (await pool2.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('p1','P1') RETURNING tenant_id`)).rows[0].tenant_id;
      const t2 = (await pool2.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('p2','P2') RETURNING tenant_id`)).rows[0].tenant_id;
      const sk = (await pool2.query(
        `INSERT INTO mu.skill (tenant_id, skill_key, display_name) VALUES ($1,'pre.k','K') RETURNING skill_id`,
        [t1])).rows[0].skill_id;
      // v20 形状：单列 FK 放行跨租户失配行
      await pool2.query(
        `INSERT INTO mu.skill_version (tenant_id, skill_id, version, manifest_sha256)
         VALUES ($1,$2,'0.1.0',$3)`, [t2, sk, 'cd'.repeat(32)]);
      const { createMuStore } = await import('../lib/multiuser/store.mjs');
      const store2 = await createMuStore({ pool: pool2 });
      let raised = null;
      try { await store2.initSchema(); } catch (e) { raised = String(e?.message ?? e); }
      ok('TB6i 失配数据 → v22 预检 RAISE 清晰错误（initSchema fail-visible）',
        raised !== null && raised.includes('v22_precheck_failed'), raised);
      const v22NotApplied = (await pool2.query(
        `SELECT count(*)::int c FROM mu.schema_migrations WHERE version=22`)).rows[0].c;
      ok('TB6j 预检失败后 v22 版本行不落（迁移中止）', v22NotApplied === 0);
      await pool2.query(`DELETE FROM mu.skill_version WHERE tenant_id=$1 AND skill_id=$2`, [t2, sk]);
      await store2.initSchema(); // 数据修复后重放成功
      const v22Applied = (await pool2.query(
        `SELECT count(*)::int c FROM mu.schema_migrations WHERE version=22`)).rows[0].c;
      ok('TB6k 数据修复后重放 v22 成功（可恢复）', v22Applied === 1);
    } finally {
      await pool2.end().catch(() => {});
      await pool.query(`DROP DATABASE IF EXISTS tb_mig_precheck`).catch(() => {});
    }
  }
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
  Object.assign(process.env, savedEnv);
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nmu-tenant-boundary.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
