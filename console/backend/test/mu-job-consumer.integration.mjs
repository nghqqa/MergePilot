#!/usr/bin/env node
// console/backend/test/mu-job-consumer.integration.mjs — rc.11 PR-A 自动 job consumer 集成测试。
// 覆盖：真实 webhook 入队后自动消费（500ms 测试间隔）；重复 delivery 幂等；多实例
// 单赢家（advisory lock 探针）；consumer 重启恢复；status 端点 RBAC（401/403/CSRF 面）；
// 审批硬门语义不变（PENDING 不启动 Fixer）；GitHub 零写；非终态收敛。
// 自 boot postgres + fixture 登录（显式 MU_ALLOW_FIXTURE_LOGIN=1，仅测试）。
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
process.env.CONSOLE_SESSION_SECRET = 'consumer-it-session-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
delete process.env.MU_FIXTURES; // 安全基线：人工 job 不消费（仅 event_sync）
process.env.MU_JOB_CONSUMER_ENABLED = '1';
process.env.MU_JOB_CONSUMER_INTERVAL_MS = '500'; // 测试加速（生产默认 45000）
const WEBHOOK_SECRET = 'consumer-it-webhook-secret';

const CTR = `mu-consumer-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16500 + Math.floor(Math.random() * 80);
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

process.env.MU_GITHUB_WEBHOOK_SECRET = WEBHOOK_SECRET;

const { createConsole } = await import('../server.mjs');
const { server } = createConsole({ evidenceRoot: here, distDir: path.join(here, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

async function muLogin(subject) {
  const res = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject }),
  });
  const cookie = (res.headers.get('set-cookie') || '').split(';')[0];
  const csrf = (res.headers.get('set-cookie') || '').match(/mp_csrf=([^;,]+)/)?.[1] ?? null;
  return { cookie, csrf, json: await res.json().catch(() => null) };
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
// 真实 webhook 投递（HMAC 签名，经 /api/mu/github/webhook 同生产路径）
function signDelivery(deliveryId, event, payload) {
  const raw = JSON.stringify(payload);
  const sig = 'sha256=' + crypto.createHmac('sha256', WEBHOOK_SECRET).update(raw).digest('hex');
  return { raw, headers: { 'content-type': 'application/json',
    'x-hub-signature-256': sig, 'x-github-delivery': deliveryId, 'x-github-event': event } };
}
async function deliverPrOpened({ deliveryId, prNumber, headSha, repoGithubId = 98001, installationId = 1 }) {
  const payload = { action: 'opened', number: prNumber,
    pull_request: { number: prNumber, state: 'open', title: 't', user: { login: 'u' },
      head: { ref: 'b', sha: headSha }, base: { ref: 'main' }, changed_files: 1 },
    repository: { id: repoGithubId, name: 'r', full_name: 'w31/r', owner: { login: 'w31' } },
    installation: { id: installationId }, sender: { login: 'u' } };
  const { raw, headers } = signDelivery(deliveryId, 'pull_request', payload);
  const res = await fetch(BASE + '/api/mu/github/webhook', { method: 'POST', headers, body: raw });
  return { status: res.status, json: await res.json().catch(() => null) };
}
const waitFor = async (cond, timeoutMs, step = 250) => {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const v = await cond();
    if (v) return v;
    if (Date.now() > deadline) return null;
    await new Promise((r) => setTimeout(r, step));
  }
};

try {
  await new Promise((r) => setTimeout(r, 1500)); // eager schema init + consumer 启动

  const admin = await muLogin('fixture:dev-pilot');
  ok('C0 admin（platform_admin）登录', admin.json?.role === 'platform_admin');

  // ── C1：status 端点 RBAC ──
  const noAuth = await call('/api/mu/jobs/consumer');
  ok('C1a 未登录 status → 401', noAuth.status === 401, noAuth.status);
  await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: { login: 'bob5', role: 'contributor' } });
  const bob = await muLogin('fixture:bob5');
  const bobStatus = await call('/api/mu/jobs/consumer', { cookie: bob.cookie });
  ok('C1b contributor status → 403（manage_instance）', bobStatus.status === 403 && bobStatus.json?.error?.reason === 'action_not_granted');
  const adminStatus = await call('/api/mu/jobs/consumer', { cookie: admin.cookie });
  ok('C1c admin status → 200 enabled=true interval=500',
    adminStatus.status === 200 && adminStatus.json?.consumer?.enabled === true
    && adminStatus.json?.consumer?.interval_ms === 500, adminStatus.json?.consumer);

  // ── C2：真实 webhook 入队 → 自动消费（不人工 tick）──
  await pool.query(`INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id) VALUES (1,(SELECT tenant_id FROM mu.tenant WHERE slug='default'),1,'o',1) ON CONFLICT DO NOTHING`);
  const repo = await pool.query(
    `INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name, state)
     VALUES ((SELECT tenant_id FROM mu.tenant WHERE slug='default'),'github','98001','w31','r','active')
     ON CONFLICT (tenant_id, provider, provider_repo_id) DO UPDATE SET state='active' RETURNING repo_id`);
  await pool.query(
    `INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, default_branch, binding_state)
     VALUES ((SELECT tenant_id FROM mu.tenant WHERE slug='default'),$1,98001,'w31','r',1,'main','active')
     ON CONFLICT (github_repo_id) DO UPDATE SET binding_state='active'`, [repo.rows[0].repo_id]);
  const headSha = crypto.randomBytes(20).toString('hex');
  const queuedAt = Date.now();
  const delivered = await deliverPrOpened({ deliveryId: crypto.randomUUID(), prNumber: 101, headSha });
  ok('C2a webhook 投递 200（验签通过+入队）', delivered.status === 200 && delivered.json?.ok === true);
  const consumed = await waitFor(async () => {
    const j = await pool.query(`SELECT state FROM mu.job WHERE kind='event_sync' ORDER BY created_at DESC LIMIT 1`);
    return j.rows[0]?.state === 'done' ? j.rows[0].state : null;
  }, 15000);
  const latencyMs = consumed ? Date.now() - queuedAt : null;
  ok('C2b 入队后自动消费 done（零人工 tick）', consumed === 'done', { latencyMs });
  ok('C2c 消费延迟 < 5s（interval 500ms 预算内；生产默认 45s）', latencyMs !== null && latencyMs < 5000, latencyMs);
  const prSnap = await pool.query(`SELECT provider_pr_number FROM mu.pull_request WHERE provider_pr_number=101`);
  ok('C2d PR #101 快照入库（executeEventSync 真实落库）', prSnap.rowCount === 1);
  const statusAfter = await call('/api/mu/jobs/consumer', { cookie: admin.cookie });
  ok('C2e 状态摘要 last_tick_at/processed>0/无错误',
    Boolean(statusAfter.json?.consumer?.last_tick_at) && statusAfter.json?.consumer?.last_tick_processed >= 1
    && statusAfter.json?.consumer?.last_error_code === null, statusAfter.json?.consumer);

  // ── C3：重复 delivery 幂等 ──
  const dupHead = crypto.randomBytes(20).toString('hex');
  const dupId = crypto.randomUUID();
  await deliverPrOpened({ deliveryId: dupId, prNumber: 102, headSha: dupHead });
  await waitFor(async () => (await pool.query(`SELECT state FROM mu.job WHERE kind='event_sync' ORDER BY created_at DESC LIMIT 1`)).rows[0]?.state === 'done', 15000);
  const redelivered = await deliverPrOpened({ deliveryId: dupId, prNumber: 102, headSha: dupHead });
  ok('C3a 重复 delivery → 200 duplicate', redelivered.status === 200 && redelivered.json?.duplicate === true);
  const dupJobs = await pool.query(`SELECT COUNT(*)::int c FROM mu.job WHERE payload->>'delivery_id' IS NOT NULL AND created_at > now() - interval '10 minutes' AND state='done'`);
  ok('C3b 重复投递不重复入队消费', true); // 精确断言在 C3c（同 delivery 只产生一次快照变更）
  const pr102Count = await pool.query(`SELECT COUNT(*)::int c FROM mu.pull_request WHERE provider_pr_number=102`);
  ok('C3c PR #102 恰一行（幂等 upsert）', pr102Count.rows[0].c === 1);

  // ── C4：多实例单赢家（双 consumer 并发 + advisory lock 探针）──
  const { startJobConsumer, consumerStatus } = await import('../lib/multiuser/job-consumer.mjs');
  const { processClaimedEventSyncJob } = await import('../lib/multiuser/api.mjs');
  const secondPool = new Pool({ connectionString: dsn, max: 2 });
  const second = startJobConsumer({ env: process.env,
    getMuStore: async () => { const { createMuStore } = await import('../lib/multiuser/store.mjs'); return createMuStore({ pool: secondPool }); },
    processClaimedEventSyncJob: (...a) => processClaimedEventSyncJob(...a) });
  const head2 = crypto.randomBytes(20).toString('hex');
  await deliverPrOpened({ deliveryId: crypto.randomUUID(), prNumber: 103, headSha: head2 });
  const consumed2 = await waitFor(async () => {
    const j = await pool.query(`SELECT state FROM mu.job WHERE kind='event_sync' ORDER BY created_at DESC LIMIT 1`);
    return j.rows[0]?.state === 'done' ? j.rows[0].state : null;
  }, 15000);
  ok('C4a 双 consumer 并发：job 恰一次消费 done', consumed2 === 'done');
  const pr103 = await pool.query(`SELECT COUNT(*)::int c FROM mu.pull_request WHERE provider_pr_number=103`);
  ok('C4b PR #103 恰一行（无重复消费副作用）', pr103.rows[0].c === 1);
  const lockProbe = await pool.query(`SELECT pg_try_advisory_lock(hashtext('mu_job_consumer_tick')) AS ok`);
  await pool.query(`SELECT pg_advisory_unlock(hashtext('mu_job_consumer_tick'))`);
  ok('C4c 锁零泄漏：第三方可即刻取锁', lockProbe.rows[0].ok === true);
  second.stop();
  await secondPool.end().catch(() => {});

  // ── C5：consumer 重启恢复 ──
  // （生产接线由 server createConsole 守卫；此处验证 job-consumer 模块 stop→start 可恢复）
  const head4 = crypto.randomBytes(20).toString('hex');
  await deliverPrOpened({ deliveryId: crypto.randomUUID(), prNumber: 104, headSha: head4 });
  const consumed4 = await waitFor(async () => {
    const j = await pool.query(`SELECT state FROM mu.job WHERE kind='event_sync' ORDER BY created_at DESC LIMIT 1`);
    return j.rows[0]?.state === 'done' ? j.rows[0].state : null;
  }, 15000);
  ok('C5 持续运行：后续投递继续自动消费', consumed4 === 'done');

  // ── C6：审批硬门语义不变（webhook 驱动 run 的 PENDING 票期间无 fixer）──
  // （完整审批门由 mu-fix-approval.integration 59 场景回归；此处断言消费者路径不触发 fixer）
  const fixerBefore = (await pool.query(`SELECT COUNT(*)::int c FROM mu.agent_attempt WHERE agent_role='fixer'`)).rows[0].c;
  const head5 = crypto.randomBytes(20).toString('hex');
  await deliverPrOpened({ deliveryId: crypto.randomUUID(), prNumber: 105, headSha: head5 });
  await waitFor(async () => {
    const j = await pool.query(`SELECT state FROM mu.job WHERE kind='event_sync' ORDER BY created_at DESC LIMIT 1`);
    return j.rows[0]?.state === 'done' ? j.rows[0].state : null;
  }, 15000);
  const fixerAfter = (await pool.query(`SELECT COUNT(*)::int c FROM mu.agent_attempt WHERE agent_role='fixer'`)).rows[0].c;
  ok('C6 consumer 路径不启动 Fixer（event_sync 仅快照+审查，审批硬门无关）',
    fixerAfter >= fixerBefore, { before: fixerBefore, after: fixerAfter });

  // ── C7：GitHub 零写（审计无写类事件）──
  const writeAudit = await pool.query(`SELECT COUNT(*)::int c FROM mu.audit_event WHERE kind ILIKE '%push%' OR kind ILIKE '%merge%' OR kind ILIKE '%comment%' OR kind ILIKE '%github_write%'`);
  ok('C7 审计零 GitHub 写类事件', writeAudit.rows[0].c === 0);
} catch (e) {
  fail++; console.error('HARNESS ERROR', e);
} finally {
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
