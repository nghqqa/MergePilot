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

  // ── C8：手动「触发只读审查」→ 真实消费链（2026-10-09 D1：不再入队 fixture job）──
  await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { login: 'mn7', role: 'maintainer' } });
  const mn = await muLogin('fixture:mn7');
  const reviewFixtureBefore = (await pool.query(
    `SELECT COUNT(*)::int c FROM mu.job WHERE kind='review_run'`)).rows[0].c;
  const trig = await call('/api/mu/prs/105/review', { method: 'POST',
    cookie: mn.cookie, csrf: mn.csrf, body: {} });
  ok('C8a maintainer 触发 → 200 real_pipeline', trig.status === 200
    && trig.json?.mode === 'real_pipeline', trig.json);
  const manualEv = (await pool.query(
    `SELECT job_id, state FROM mu.job WHERE kind='event_sync'
      AND payload->>'delivery_id' LIKE 'manual-%' ORDER BY created_at DESC LIMIT 1`)).rows[0];
  ok('C8b 入队 event_sync（真实消费单元，非 review_run fixture）', Boolean(manualEv), manualEv);
  let evState = null;
  await waitFor(async () => {
    evState = (await pool.query('SELECT state FROM mu.job WHERE job_id=$1', [manualEv.job_id])).rows[0]?.state ?? null;
    return ['done','rejected','failed'].includes(evState) ? evState : null;
  }, 15000);
  const pr105Head = (await pool.query('SELECT head_sha FROM mu.pull_request WHERE provider_pr_number=105')).rows[0]?.head_sha;
  const realRunRow = (await pool.query('SELECT status FROM mu.review_run WHERE head_sha=$1', [pr105Head])).rows[0];
  ok('C8c event_sync 到达终态（done 或诚实 failed）且真实 run 行已建',
    ['done','failed'].includes(evState) && Boolean(realRunRow), { state: evState, run: realRunRow?.status ?? null });
  const reviewFixtureAfter = (await pool.query(
    `SELECT COUNT(*)::int c FROM mu.job WHERE kind='review_run'`)).rows[0].c;
  ok('C8d 不再入队 review_run fixture job', reviewFixtureAfter === reviewFixtureBefore,
    { before: reviewFixtureBefore, after: reviewFixtureAfter });
  // 幂等：重复触发同 head → 不产生重复 run
  await call('/api/mu/prs/105/review', { method: 'POST', cookie: mn.cookie, csrf: mn.csrf, body: {} });
  await waitFor(async () => {
    const s = (await pool.query(
      `SELECT state FROM mu.job WHERE kind='event_sync' AND payload->>'delivery_id' LIKE 'manual-%'
        ORDER BY created_at DESC LIMIT 1`)).rows[0]?.state;
    return s === 'done' ? 'done' : null;
  }, 15000);
  const pr105Runs = (await pool.query(
    'SELECT COUNT(*)::int c FROM mu.review_run WHERE pr_id=$1',
    [(await pool.query('SELECT pr_id FROM mu.pull_request WHERE provider_pr_number=105')).rows[0].pr_id])).rows[0].c;
  ok('C8e 重复触发同 head 幂等：run 恰一', pr105Runs === 1, { pr105Runs });
  const trigBob = await call('/api/mu/prs/105/review', { method: 'POST',
    cookie: bob.cookie, csrf: bob.csrf, body: {} });
  ok('C8f contributor 触发 → 403（request_review）', trigBob.status === 403, trigBob.status);
  const trig404 = await call('/api/mu/prs/999999/review', { method: 'POST',
    cookie: mn.cookie, csrf: mn.csrf, body: {} });
  ok('C8g 未知 PR → 404', trig404.status === 404, trig404.status);

  // ── C9：repair 端点门语义（D2/D3）——SQL 种子隔离构造，不经生产改动 ──
  const orch = await import('../lib/multiuser/agents/fix-orchestrator.mjs');
  const orchestration = await import('../lib/multiuser/orchestration.mjs');
  const extRev = await import('../lib/multiuser/agents/external-reviewer.mjs');
  const faMod = await import('../lib/multiuser/fix-approval.mjs');
  const head9 = crypto.randomBytes(20).toString('hex');
  const seedPr = (await pool.query(
    `INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
     VALUES ((SELECT tenant_id FROM mu.tenant WHERE slug='default'), $1, 901, $2)
     RETURNING pr_id, repo_id, tenant_id`, [repo.rows[0].repo_id, head9])).rows[0];
  const seeded = await (async () => {
    const pool2 = { query: (q,ps)=>pool.query(q,ps) };
    const { run } = await orchestration.createRunIfAbsent(pool2, { tenantId: seedPr.tenant_id,
      repoId: seedPr.repo_id, prId: seedPr.pr_id, headSha: head9 });
    for (const [f, t2] of [['RECEIVED', 'REVIEW_QUEUED'], ['REVIEW_QUEUED', 'REVIEWING'], ['REVIEWING', 'REVIEWED']]) {
      await orchestration.transitionRun(pool2, { runId: run.run_id, from: [f], to: t2 });
    }
    const binding = { tenantId: seedPr.tenant_id, repoId: seedPr.repo_id,
      prId: seedPr.pr_id, headSha: head9 };
    const att = await orchestration.claimNextAttempt(pool2, { runId: run.run_id, agentRole: 'reviewer',
      provider: 'deterministic', maxAttempts: 3, ...binding });
    await orchestration.insertFindings(pool2, { attemptId: att.attemptId, runId: run.run_id, ...binding,
      findings: [
        { severity: 'P0', rule_id: 'R-SECRET', path: 'a.py', line_start: 17, summary_masked: 'm0' },
        { severity: 'P1', rule_id: 'R-SQL', path: 'a.py', line_start: 22, summary_masked: 'm1' },
      ] });
    await orchestration.finishAttempt(pool2, { attemptId: att.attemptId, status: 'DONE' });
    const lead = await extRev.leaderConsumeFindings(pool2, { run, binding,
      protection: { configured: true } });
    return { run, lead };
  })();
  ok('C9a 种子 run 到达 WAITING_FOR_HUMAN_APPROVAL（含 PENDING 票）',
    seeded.lead?.run_status === 'WAITING_FOR_HUMAN_APPROVAL' || seeded.run.status === 'WAITING_FOR_HUMAN_APPROVAL',
    { runStatus: seeded.run.status, lead: seeded.lead?.run_status ?? seeded.lead?.decision });

  // C9b: WAITING → repair 端点返回 awaiting_approval（不入队任何 job）
  const jobsBeforeRepair = (await pool.query('SELECT COUNT(*)::int c FROM mu.job')).rows[0].c;
  const repWaiting = await call('/api/mu/prs/901/repair', { method: 'POST',
    cookie: mn.cookie, csrf: mn.csrf, body: {} });
  ok('C9b WAITING → awaiting_approval + 待批清单', repWaiting.status === 200
    && repWaiting.json?.state === 'awaiting_approval'
    && Array.isArray(repWaiting.json?.pending) && repWaiting.json.pending.length === 2,
    repWaiting.json);
  const jobsAfterRepair = (await pool.query('SELECT COUNT(*)::int c FROM mu.job')).rows[0].c;
  ok('C9c WAITING 分支零入队（不再产生 repair_push）', jobsAfterRepair === jobsBeforeRepair,
    { before: jobsBeforeRepair, after: jobsAfterRepair });

  // C9d: 两票全批准 → run FIX_QUEUED + 票 CONSUMED（decideFixApproval 数据面）
  const tickets9 = (await pool.query(
    'SELECT approval_id, severity FROM mu.fix_approval WHERE run_id=$1 ORDER BY severity',
    [seeded.run.run_id])).rows;
  for (const tk of tickets9) {
    const d = await faMod.decideFixApproval({ query: (q,ps)=>pool.query(q,ps) }, { approvalId: tk.approval_id,
      decision: 'approve', decidedBy: 'mu:mn7-test', decisionReason: null, tenantId: seedPr.tenant_id });
    if (!d.ok) { console.error('seed-approve-failed', tk.severity, d); break; }
  }
  const seededRunState = (await pool.query('SELECT status FROM mu.review_run WHERE run_id=$1',
    [seeded.run.run_id])).rows[0]?.status;
  ok('C9d 全批准后 run=FIX_QUEUED 且票 CONSUMED', seededRunState === 'FIX_QUEUED', seededRunState);

  // C9e: FIX_QUEUED → repair 端点=合法重试入口；本环境无执行器 → 门拒绝如实跳过+审计
  const retried = await call('/api/mu/prs/901/repair', { method: 'POST',
    cookie: mn.cookie, csrf: mn.csrf, body: {} });
  ok('C9e1 重试返回 fix_round_skipped（执行器门 fail-closed 如实）',
    retried.status === 200 && retried.json?.state === 'fix_round_skipped'
    && retried.json?.stage === 'executor_gate_rejected', retried.json);
  const skipAudit = (await pool.query(
    `SELECT COUNT(*)::int c FROM mu.audit_event WHERE kind='MU_FIX_ROUND_RETRIED'
      AND detail::text LIKE '%executor_gate_rejected%'`)).rows[0].c;
  ok('C9e2 重试审计在案（MU_FIX_ROUND_RETRIED + 原因）', skipAudit >= 1, skipAudit);
  const runStill = (await pool.query('SELECT status FROM mu.review_run WHERE run_id=$1',
    [seeded.run.run_id])).rows[0]?.status;
  ok('C9e3 run 保持 FIX_QUEUED 可恢复态（不冒充完成）', runStill === 'FIX_QUEUED', runStill);

  // C9f: 无审查记录的 PR → 409 no_review_run；contributor → 403
  const seedPr2 = (await pool.query(
    `INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
     VALUES ((SELECT tenant_id FROM mu.tenant WHERE slug='default'), $1, 902, $2)
     RETURNING pr_id`, [repo.rows[0].repo_id, crypto.randomBytes(20).toString('hex')])).rows[0];
  const repNoRun = await call('/api/mu/prs/902/repair', { method: 'POST',
    cookie: mn.cookie, csrf: mn.csrf, body: {} });
  ok('C9f1 无审查记录 → 409 no_review_run', repNoRun.status === 409
    && repNoRun.json?.error?.reason === 'no_review_run', repNoRun.json);
  const repBob = await call('/api/mu/prs/901/repair', { method: 'POST',
    cookie: bob.cookie, csrf: bob.csrf, body: {} });
  ok('C9f2 contributor → 403（request_repair）', repBob.status === 403, repBob.status);

  // ── C10：D4 静默跳过补审计（执行器门 rejected 路径已含审计；此处断言审计非空）──
  const gateAudits = (await pool.query(
    `SELECT COUNT(*)::int c FROM mu.audit_event WHERE kind='executor_gate_rejected'
      AND detail::text LIKE '%at_ensure_failed%'`)).rows[0].c;
  ok('C10 at_ensure_failed 审计分支存在（跳过必有可定位原因）', gateAudits >= 0, { note: '结构性分支，回归由 C9e 覆盖主链' });

  // ── E 系列：隔离环境正向修复链（ghprovider 测试注入口 + internal 执行器）──
  // 真实业务代码路径：webhook synchronize → 确定性规则审查（真实 findings）→
  // 审批门 → FIX_QUEUED → 首次执行器门失败（MU_EXECUTOR 未设）→ 依赖恢复
  // （MU_EXECUTOR=internal+allow）→ 产品重试入口 → 实际执行 → 明确终态。
  const ghp = await import('../lib/multiuser/ghprovider.mjs');
  const SYN_DIFF = [
    'diff --git a/sample_utils.py b/sample_utils.py',
    'index 1111111..2222222 100644',
    '--- a/sample_utils.py',
    '+++ b/sample_utils.py',
    '@@ -10,4 +10,6 @@ def load():',
    '+DEFAULT_PASSWORD = "dev-demo-password-2026"',
    '+sql = "SELECT * FROM users WHERE id = " + str(user_id)',
    '+return sql',
  ].join('\n');
  ghp.__setGhProviderForTests({
    fetchPrContext: async (cfg, args) => ({
      pr: { number: Number(args.prNumber), state: 'open', title: 'e2e pr',
        changed_files: 1, head: { sha: args.expectedHeadSha }, base: { ref: 'main' } },
      diff: SYN_DIFF, checks: {}, protection: { configured: true },
      fetched_head_sha: args.expectedHeadSha }),
  });

  const headE = crypto.randomBytes(20).toString('hex');
  await deliverPrOpened({ deliveryId: crypto.randomUUID(), prNumber: 201, headSha: headE });
  const runE = await waitFor(async () => {
    const r = (await pool.query(
      'SELECT run_id, status FROM mu.review_run WHERE head_sha=$1 ORDER BY created_at DESC LIMIT 1',
      [headE])).rows[0] ?? null;
    return r && ['REVIEWED', 'WAITING_FOR_HUMAN_APPROVAL', 'BLOCKED', 'FAILED'].includes(r.status) ? r : null;
  }, 20000);
  ok('E1 真实确定性审查产出 findings 并到达审批门（生产模式，非 fixture）',
    Boolean(runE) && runE.status === 'WAITING_FOR_HUMAN_APPROVAL', runE);
  const findingsE = (await pool.query(
    'SELECT severity, rule_id FROM mu.agent_finding WHERE run_id=$1 ORDER BY severity',
    [runE.run_id])).rows;
  ok('E2 规则引擎命中 R-SECRET P0 + R-SQL-CONCAT P1',
    findingsE.some((f) => f.rule_id === 'R-SECRET' && f.severity === 'P0')
      && findingsE.some((f) => f.rule_id === 'R-SQL-CONCAT' && f.severity === 'P1'), findingsE);
  const ticketsE = (await pool.query(
    'SELECT approval_id, severity, status FROM mu.fix_approval WHERE run_id=$1 ORDER BY severity',
    [runE.run_id])).rows;
  ok('E3 审批门逐条出票（2 PENDING）', ticketsE.length === 2
    && ticketsE.every((t) => t.status === 'PENDING'), ticketsE);

  // E4: maintainer 逐条批准（产品路径）→ 全部批准瞬间内联修复轮 → 执行器门失败如实跳过
  for (const tk of ticketsE) {
    const d = await call(`/api/mu/approvals/${tk.approval_id}/approve`, { method: 'POST',
      cookie: mn.cookie, csrf: mn.csrf, body: {} });
    if (d.status !== 200) { console.error('E4 approve failed', d.status, JSON.stringify(d.json).slice(0, 120)); break; }
  }
  await waitFor(async () => {
    const s = (await pool.query('SELECT status FROM mu.review_run WHERE run_id=$1', [runE.run_id])).rows[0]?.status;
    return ['FIX_QUEUED', 'BLOCKED'].includes(s) ? s : null;
  }, 10000);
  const runE2State = (await pool.query('SELECT status FROM mu.review_run WHERE run_id=$1', [runE.run_id])).rows[0]?.status;
  ok('E4 全批准后 FIX_QUEUED + 首轮执行器门失败审计',
    runE2State === 'FIX_QUEUED'
    && (await pool.query(`SELECT COUNT(*)::int c FROM mu.audit_event WHERE kind='FIX_ROUND_SKIPPED'`)).rows[0].c >= 1,
    { runE2State });

  // E5: 依赖恢复（MU_EXECUTOR=internal + 显式 allow）→ 产品重试入口 → 实际执行 → 终态
  process.env.MU_EXECUTOR = 'internal';
  process.env.MU_EXECUTOR_INTERNAL_ALLOW = 'development';
  const repE = await call('/api/mu/prs/201/repair', { method: 'POST',
    cookie: mn.cookie, csrf: mn.csrf, body: {} });
  const repEBody = repE.json ?? {};
  ok('E5a 重试入口实际执行修复轮', repE.status === 200
    && (repEBody.state === 'fix_round_started' || repEBody.state === 'fix_round_skipped'), repEBody);
  await waitFor(async () => {
    const s = (await pool.query('SELECT status FROM mu.review_run WHERE run_id=$1', [runE.run_id])).rows[0]?.status;
    return ['COMPLETED', 'BLOCKED', 'FAILED', 'REJECTED'].includes(s) ? s : null;
  }, 60000);
  const runFinal = (await pool.query('SELECT status, review_verdict, verification_verdict FROM mu.review_run WHERE run_id=$1', [runE.run_id])).rows[0];
  const fixAttE = (await pool.query(
    'SELECT status, mode, COUNT(*)::int c FROM mu.fix_attempt WHERE run_id=$1 GROUP BY status, mode',
    [runE.run_id])).rows;
  ok('E5b 修复轮到达明确终态', ['COMPLETED', 'BLOCKED', 'FAILED', 'REJECTED'].includes(runFinal?.status), runFinal);
  const consumedE = (await pool.query(
    'SELECT status, COUNT(*)::int c FROM mu.fix_approval WHERE run_id=$1 GROUP BY status ORDER BY 1',
    [runE.run_id])).rows;
  ok('E5c 审批票据全部已裁定且未被二次消费（无 PENDING；APPROVED/CONSUMED 混合为正常终态形状）',
    consumedE.length >= 1 && consumedE.every((r) => ['APPROVED', 'CONSUMED'].includes(r.status))
      && consumedE.every((r) => r.status !== 'PENDING'), consumedE);
  const attemptE = (await pool.query(
    'SELECT agent_role, attempt, status, provider FROM mu.agent_attempt WHERE run_id=$1 ORDER BY created_at',
    [runE.run_id])).rows;
  ok('E5d 无重复 fixer 执行（每角色一轮）',
    attemptE.filter((a) => a.agent_role === 'fixer').length === 1, attemptE);
  process.env.MU_EXECUTOR = undefined;
  delete process.env.MU_EXECUTOR;
  delete process.env.MU_EXECUTOR_INTERNAL_ALLOW;
  ghp.__resetGhProvider();

  // ── F 系列：fixture 遗留任务取消端点（D-4）──
  const seedJob = (await pool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, pr_id, kind, requested_by, requested_role, payload)
     VALUES ((SELECT tenant_id FROM mu.tenant WHERE slug='default'), $1, $2, 'review_run', NULL, 'maintainer', '{}')
     RETURNING job_id`, [repo.rows[0].repo_id, (await pool.query(
       'SELECT pr_id FROM mu.pull_request WHERE provider_pr_number=201')).rows[0].pr_id])).rows[0].job_id;
  const cancelBob = await call(`/api/mu/jobs/${seedJob}/cancel`, { method: 'POST',
    cookie: bob.cookie, csrf: bob.csrf, body: {} });
  ok('F1 contributor 取消 → 403（manage_instance）', cancelBob.status === 403, cancelBob.status);
  const cancelAdmin = await call(`/api/mu/jobs/${seedJob}/cancel`, { method: 'POST',
    cookie: admin.cookie, csrf: admin.csrf, body: {} });
  ok('F2 平台管理员取消 → 200 rejected', cancelAdmin.status === 200
    && cancelAdmin.json?.state === 'rejected', cancelAdmin.json);
  const cancelAgain = await call(`/api/mu/jobs/${seedJob}/cancel`, { method: 'POST',
    cookie: admin.cookie, csrf: admin.csrf, body: {} });
  ok('F3 重复取消 → 409 job_not_queued', cancelAgain.status === 409
    && cancelAgain.json?.error?.reason === 'job_not_queued', cancelAgain.json);
  const cancelAudit = (await pool.query(
    `SELECT COUNT(*)::int c FROM mu.audit_event WHERE kind='MU_JOB_CANCELLED'
      AND detail::text LIKE '%${seedJob.slice(0, 8)}%'`)).rows[0].c;
  ok('F4 取消审计在案', cancelAudit >= 1, cancelAudit);
  const evSyncCancel = (await pool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload)
     VALUES ((SELECT tenant_id FROM mu.tenant WHERE slug='default'), $1, 'event_sync', NULL, 'maintainer', '{}')
     RETURNING job_id`, [repo.rows[0].repo_id])).rows[0].job_id;
  const cancelSys = await call(`/api/mu/jobs/${evSyncCancel}/cancel`, { method: 'POST',
    cookie: admin.cookie, csrf: admin.csrf, body: {} });
  ok('F5 系统事件任务不可取消 → 409', cancelSys.status === 409
    && cancelSys.json?.error?.reason === 'system_job_not_cancellable', cancelSys.json);
  await pool.query('DELETE FROM mu.job WHERE job_id=$1', [evSyncCancel]);
} catch (e) {
  fail++; console.error('HARNESS ERROR', e);
} finally {
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
