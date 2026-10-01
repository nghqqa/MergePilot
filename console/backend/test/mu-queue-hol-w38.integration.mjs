// console/backend/test/mu-queue-hol-w38.integration.mjs — Wave 3.8 队首阻塞（HOL）回归门槛。
//
// 背景：独立验收（2dbda30）裁决 BLOCKED_BY_QUEUE_HEAD_OF_LINE——队首单个过期人工
// review_run job 令正式 tick 永久 processed=[]，其后同租户+跨租户合法 event_sync
// 永不被领取，重启不自愈。本套件把验收探针转为正式回归，并覆盖任务书十场景
// 与状态不变量。全部经由正式 tick 端点（认证会话+CSRF），零手工改库修复。
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
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 220) : ''}`); }
};

const CTR = `mu-qhol-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16700 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const adminPool = new Pool({ connectionString: dsn });
{
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { await adminPool.query('SELECT 1'); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'x-test-password';
process.env.CONSOLE_SESSION_SECRET = 'qhol-regression-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
// MU_FIXTURES 不设——生产非 fixture 模式（人工 job 无消费者）

const { createMuStore } = await import('../lib/multiuser/store.mjs');
const store = await createMuStore({ pool: adminPool });
await store.initSchema();
await store.bootstrap();
const { createConsole } = await import('../server.mjs');

let server = null, BASE = null;
async function bootServer() {
  const { server: s } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
  await new Promise((r) => s.listen(0, '127.0.0.1', r));
  return s;
}
server = await bootServer();
BASE = `http://127.0.0.1:${server.address().port}`;

async function login() {
  const res = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject: 'fixture:dev-pilot' }) });
  return { cookie: (res.headers.get('set-cookie') || '').split(';')[0],
    csrf: (await res.json().catch(() => null))?.csrf ?? '' };
}
let SESSION = await login();
const tick = async () => {
  const res = await fetch(BASE + '/api/mu/jobs/tick', { method: 'POST',
    headers: { cookie: SESSION.cookie, 'content-type': 'application/json', 'x-csrf-token': SESSION.csrf },
    body: '{}' });
  return { status: res.status, body: await res.json().catch(() => null) };
};
const jobState = async (del) => (await adminPool.query(
  `SELECT state, result::text result FROM mu.job WHERE payload->>'delivery_id'=$1`, [del])).rows[0] ?? null;
const enqueueEventSync = async (tenantId, repoId, delivery, instId, gid, prNumber, headSha, action = 'opened', createdSql = 'now()') => {
  await adminPool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, state, requested_by, requested_role, payload, created_at)
     VALUES ($1,$2,'event_sync','queued',NULL,'maintainer',
     jsonb_build_object('event','pull_request','action',$3::text,'delivery_id',$4::text,'installation_id',$5::bigint,
       'github_repo_id',$6::bigint,'pr_number',$7::bigint,'head_sha',$8::text), ${createdSql})`,
    [tenantId, repoId, action, delivery, instId, gid, prNumber, headSha]);
};
const enqueueManual = async (tenantId, repoId, ageSql) => {
  await adminPool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, state, requested_by, requested_role, payload, created_at)
     VALUES ($1,$2,'review_run','queued',
       (SELECT user_id FROM mu.app_user LIMIT 1),'maintainer',
       '{"head_sha":"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"}'::jsonb, ${ageSql})`,
    [tenantId, repoId]);
};
const auditCount = async (kind, reasonLike) => (await adminPool.query(
  `SELECT count(*)::int c FROM mu.audit_event WHERE kind=$1 AND detail::text LIKE $2`,
  [kind, `%${reasonLike}%`])).rows[0].c;

try {
  // ── 基础数据：两租户、installation、binding ──
  const T1 = (await adminPool.query(`SELECT tenant_id FROM mu.tenant ORDER BY created_at LIMIT 1`)).rows[0].tenant_id;
  const T2 = (await adminPool.query(
    `INSERT INTO mu.tenant (slug, display_name) VALUES ('tenant2-hol','Tenant Two HOL') RETURNING tenant_id`)).rows[0].tenant_id;
  const U = (await adminPool.query(`SELECT user_id FROM mu.app_user LIMIT 1`)).rows[0].user_id;
  await adminPool.query(
    `INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id)
     VALUES (880001,$1,1,'acct1',1), (880002,$2,2,'acct2',1) ON CONFLICT DO NOTHING`, [T1, T2]);
  const repos = {};
  for (const [t, iid, gid] of [[T1, 880001, 660101], [T2, 880002, 660201]]) {
    const repo = await store.ensureRepository({ tenantId: t, provider: 'github', providerRepoId: String(gid),
      owner: 'hol', name: 'repo-' + String(gid).slice(-2), defaultBranch: 'main' });
    await adminPool.query(
      `INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, installation_id, binding_state, owner, name)
       VALUES ($1,$2,$3,$4,'active','hol',$5) ON CONFLICT DO NOTHING`,
      [t, repo.repo_id, gid, iid, 'repo-' + String(gid).slice(-2)]);
    repos[`${t}`] = { repo_id: repo.repo_id, iid, gid };
  }
  const R1 = repos[T1], R2 = repos[T2];

  // ══ S1 HOL 核心：A=过期人工队首，B/C=两租户合法 event_sync ══
  await enqueueManual(T1, R1.repo_id, `now() - interval '25 hours'`);           // A：过期人工队首
  await enqueueEventSync(T1, R1.repo_id, 'hol-b-1', R1.iid, R1.gid, 11, 'a'.repeat(40));
  await enqueueEventSync(T2, R2.repo_id, 'hol-c-1', R2.iid, R2.gid, 22, 'b'.repeat(40));
  const r1 = await tick();
  const B1 = await jobState('hol-b-1'), C1 = await jobState('hol-c-1'), A1 = (await adminPool.query(
    `SELECT state, result->>'reason' reason FROM mu.job WHERE kind='review_run' AND state='rejected'
       AND created_at < now() - interval '24 hours' LIMIT 1`)).rows[0];
  ok('S1a B(tenant1) event_sync 被消费（不再永久 queued）',
    ['done', 'rejected', 'failed'].includes(B1?.state) && B1?.state !== 'queued', B1);
  ok('S1b C(tenant2) event_sync 被消费（跨租户不被阻塞）',
    ['done', 'rejected', 'failed'].includes(C1?.state) && C1?.state !== 'queued', C1);
  ok('S1c 过期人工 job 转 rejected 终态 + reason=manual_job_expired_unconsumable',
    A1?.state === 'rejected' && A1?.reason === 'manual_job_expired_unconsumable', A1);
  ok('S1d 过期人工 job 清收落审计（MU_JOB_REJECTED+reason）',
    (await auditCount('MU_JOB_REJECTED', 'manual_job_expired_unconsumable')) >= 1);
  ok('S1e tick 响应非空（不再 processed=[]）', (r1.body?.processed ?? []).length >= 3, r1.body?.processed);

  // ══ S2 未过期人工 job 在队首：不阻塞后续 event_sync，自身保持 queued ══
  await enqueueManual(T1, R1.repo_id, `now()`);                                  // A2：新鲜人工队首
  await enqueueEventSync(T1, R1.repo_id, 'hol-b-2', R1.iid, R1.gid, 12, 'c'.repeat(40));
  await tick();
  const B2 = await jobState('hol-b-2');
  const A2 = (await adminPool.query(
    `SELECT state FROM mu.job WHERE kind='review_run' AND state='queued' AND created_at > now() - interval '5 minutes' LIMIT 1`)).rows[0];
  ok('S2a 未过期人工 job 保持 queued（等待专用消费者，不误杀）', A2?.state === 'queued');
  ok('S2b 其后 event_sync 正常推进', B2?.state === 'done', B2);

  // ══ S3 重复 delivery 幂等：同 delivery+同 head 双 job → 单一 canonical run ══
  await enqueueEventSync(T1, R1.repo_id, 'hol-dup-1', R1.iid, R1.gid, 13, 'd'.repeat(40));
  await enqueueEventSync(T1, R1.repo_id, 'hol-dup-1', R1.iid, R1.gid, 13, 'd'.repeat(40));
  await tick();
  const dupRuns = (await adminPool.query(
    `SELECT count(*)::int c FROM mu.review_run rr JOIN mu.pull_request p ON p.pr_id=rr.pr_id
      WHERE p.provider_pr_number=13 AND rr.tenant_id=$1`, [T1])).rows[0].c;
  ok('S3 重复 delivery 幂等：同 head 只产生一个 canonical run', dupRuns === 1, { dupRuns });

  // ══ S4 重启后继续处理 ══
  server.close();
  await new Promise((r) => setTimeout(r, 300));
  server = await bootServer();
  BASE = `http://127.0.0.1:${server.address().port}`;
  SESSION = await login();
  await enqueueEventSync(T1, R1.repo_id, 'hol-b-4', R1.iid, R1.gid, 14, 'e'.repeat(40));
  const r4 = await tick();
  const B4 = await jobState('hol-b-4');
  ok('S4 server 重启后合法 queued job 继续被领取消费',
    r4.status === 200 && B4?.state === 'done' && (r4.body?.processed ?? []).some((p) => p.state === 'done'), B4);

  // ══ S5 两 worker 并发 tick：无重复领取/无重复 run/delivery 幂等 ══
  const s5del = [];
  for (let i = 0; i < 10; i++) {
    const d = `hol-s5-${i}`;
    s5del.push(d);
    await enqueueEventSync(T1, R1.repo_id, d, R1.iid, R1.gid, 100 + i, crypto.randomBytes(20).toString('hex'));
  }
  const s5b = await bootServer();
  const BASE2 = `http://127.0.0.1:${s5b.address().port}`;
  const lr2 = await fetch(BASE2 + '/api/mu/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ provider: 'fixture', subject: 'fixture:dev-pilot' }) });
  const sess2 = { cookie: (lr2.headers.get('set-cookie') || '').split(';')[0],
    csrf: (await lr2.json().catch(() => null))?.csrf ?? '' };
  const tickOn = async (base, sess) => {
    const res = await fetch(base + '/api/mu/jobs/tick', { method: 'POST',
      headers: { cookie: sess.cookie, 'content-type': 'application/json', 'x-csrf-token': sess.csrf }, body: '{}' });
    return res.json().catch(() => null);
  };
  const [w1, w2] = await Promise.all([tickOn(BASE, SESSION), tickOn(BASE2, sess2)]);
  const ids1 = (w1?.processed ?? []).map((p) => String(p.job_id));
  const ids2 = (w2?.processed ?? []).map((p) => String(p.job_id));
  const union = new Set([...ids1, ...ids2]);
  const overlap = ids1.filter((x) => ids2.includes(x));
  const runs5 = (await adminPool.query(
    `SELECT count(*)::int c FROM mu.review_run rr JOIN mu.pull_request p ON p.pr_id=rr.pr_id
      WHERE rr.tenant_id=$1 AND p.provider_pr_number BETWEEN 100 AND 109`, [T1])).rows[0].c;
  const states5 = await adminPool.query(
    `SELECT state, count(*)::int c FROM mu.job WHERE payload->>'delivery_id' LIKE 'hol-s5-%' GROUP BY state`);
  ok('S5a 并发 tick 零重复领取（两 worker processed 集合不相交）', overlap.length === 0, { overlap });
  ok('S5b 全部 10 job 恰好被消费一次', union.size === 10, { unionSize: union.size });
  ok('S5c 无重复 canonical run（10 job → 10 run）', runs5 === 10, { runs5 });
  ok('S5d 无 job 滞留 queued/running',
    !states5.rows.some((r) => ['queued', 'running'].includes(r.state)), states5.rows);
  await new Promise((r) => s5b.close(r));

  // ══ S6 租户隔离：tenant1 坏 installation 拒绝不波及 tenant2 ══
  await enqueueEventSync(T1, R1.repo_id, 'hol-bad-t1', 999999, R1.gid, 31, 'f'.repeat(40)); // installation 不存在
  await enqueueEventSync(T2, R2.repo_id, 'hol-ok-t2', R2.iid, R2.gid, 32, '1'.repeat(40));
  await tick();
  const badT1 = await jobState('hol-bad-t1');
  const okT2 = await jobState('hol-ok-t2');
  ok('S6a 坏 installation job 被拒（fail-closed 终态，非 queued 滞留）',
    badT1?.state === 'rejected' && /installation/.test(badT1?.result ?? ''), badT1);
  ok('S6b 同 tick 内 tenant2 合法 job 正常消费（跨租户零影响）', okT2?.state === 'done', okT2);

  // ══ S7 事件乱序：旧 opened 不能覆盖新 closed 状态 ══
  const head1 = '9'.repeat(40);
  await enqueueEventSync(T1, R1.repo_id, 'hol-oo-open', R1.iid, R1.gid, 41, head1);
  await tick();
  await enqueueEventSync(T1, R1.repo_id, 'hol-oo-close', R1.iid, R1.gid, 41, head1, 'closed');
  await tick();
  const afterClose = (await adminPool.query(
    `SELECT state FROM mu.pull_request WHERE tenant_id=$1 AND provider_pr_number=41`, [T1])).rows[0]?.state;
  await enqueueEventSync(T1, R1.repo_id, 'hol-oo-stale-open', R1.iid, R1.gid, 41, head1); // 旧事件重放（新 delivery）
  await tick();
  const afterStale = (await adminPool.query(
    `SELECT state FROM mu.pull_request WHERE tenant_id=$1 AND provider_pr_number=41`, [T1])).rows[0]?.state;
  const ooRuns = (await adminPool.query(
    `SELECT count(*)::int c FROM mu.review_run rr JOIN mu.pull_request p ON p.pr_id=rr.pr_id
      WHERE p.provider_pr_number=41 AND rr.tenant_id=$1`, [T1])).rows[0].c;
  ok('S7a closed 事件收敛 PR state=closed', afterClose === 'closed', afterClose);
  ok('S7b 乱序旧 opened 重放不覆盖 closed', afterStale === 'closed', afterStale);
  ok('S7c 乱序重放不产生新 run（同 head 幂等）', ooRuns === 1, { ooRuns });

  // ══ S8 终态稳定性：rejected job 反复 tick 不再被领取/改写 ══
  const rejectedBefore = (await adminPool.query(
    `SELECT job_id::text, updated_at FROM mu.job WHERE state='rejected' AND kind='event_sync'`)).rows;
  await tick(); await tick();
  const rejectedAfter = (await adminPool.query(
    `SELECT job_id::text, updated_at FROM mu.job WHERE state='rejected' AND kind='event_sync'`)).rows;
  const beforeMap = new Map(rejectedBefore.map((r) => [r.job_id, r.updated_at]));
  const touched = rejectedAfter.filter((r) => beforeMap.has(r.job_id) && beforeMap.get(r.job_id).toISOString() !== r.updated_at.toISOString());
  ok('S8 终态 job 不被重复领取/改写（updated_at 稳定）', touched.length === 0, { touched: touched.length });

  // ══ S9 队列形状矩阵 ══
  await adminPool.query(`UPDATE mu.job SET state='done' WHERE payload->>'delivery_id' LIKE 'hol-s5-%'`); // 清场（构造空队列——测试夹具编排，非修复性改库）
  {
    const e = await tick();
    ok('S9a 空队列 → 200 processed=[]', e.status === 200 && (e.body?.processed ?? []).length === 0, e.body);
  }
  {
    await enqueueManual(T1, R1.repo_id, `now()`);
    const e = await tick();
    const man = (await adminPool.query(
      `SELECT count(*)::int c FROM mu.job WHERE kind='review_run' AND state='queued' AND created_at > now() - interval '5 minutes'`)).rows[0].c;
    ok('S9b 仅未过期人工 job：不消费不阻塞（留队待专用消费者）',
      (e.body?.processed ?? []).length === 0 && man >= 1, e.body?.processed);
  }
  {
    await enqueueManual(T1, R1.repo_id, `now() - interval '30 hours'`);
    const e = await tick();
    ok('S9c 仅过期人工 job：被清收终态', (e.body?.processed ?? []).some((p) => p.reason === 'manual_job_expired_unconsumable'), e.body?.processed);
  }
  {
    await enqueueEventSync(T1, R1.repo_id, 'hol-s9-only', R1.iid, R1.gid, 51, '2'.repeat(40));
    const e = await tick();
    ok('S9d 仅 event_sync：全部消费', (e.body?.processed ?? []).some((p) => p.kind === 'event_sync'), e.body?.processed);
  }

  // ══ S10 孤立 lease 回收（worker 崩溃模拟）+ 预算上限 ══
  await adminPool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, state, requested_by, requested_role, payload, locked_at, created_at)
     VALUES ($1,$2,'event_sync','running',NULL,'maintainer',
       jsonb_build_object('event','pull_request','action','opened','delivery_id','hol-orphan-1',
         'installation_id',$3::bigint,'github_repo_id',$4::bigint,'pr_number',61::bigint,'head_sha',$5::text),
       now() - interval '15 minutes', now() - interval '15 minutes')`,
    [T1, R1.repo_id, R1.iid, R1.gid, '3'.repeat(40)]);
  await adminPool.query(
    `INSERT INTO mu.job (tenant_id, repo_id, kind, state, requested_by, requested_role, payload, locked_at, created_at)
     VALUES ($1,$2,'event_sync','running',NULL,'maintainer',
       jsonb_build_object('event','pull_request','action','opened','delivery_id','hol-orphan-fresh',
         'installation_id',$3::bigint,'github_repo_id',$4::bigint,'pr_number',62::bigint,'head_sha',$5::text),
       now(), now())`,
    [T1, R1.repo_id, R1.iid, R1.gid, '4'.repeat(40)]);
  await tick();
  const orphanBack = (await adminPool.query(
    `SELECT state FROM mu.job WHERE payload->>'delivery_id'='hol-orphan-1'`)).rows[0]?.state;
  const freshRunning = (await adminPool.query(
    `SELECT state FROM mu.job WHERE payload->>'delivery_id'='hol-orphan-fresh'`)).rows[0]?.state;
  ok('S10a 孤立 running（lease 超 10 分钟）被回收并推进到终态（同 tick 回收+消费）+ 回收审计在案',
    ['queued', 'done'].includes(orphanBack)
      && (await auditCount('MU_JOB_REQUEUED_ORPHAN', 'orphan_running_lease_recovered')) >= 1,
    { orphanBack });
  ok('S10b 活跃 running（lease 新鲜）不被误回收', freshRunning === 'running', freshRunning);
  await tick();
  const orphanProcessed = await jobState('hol-orphan-1');
  ok('S10c 回收后的 job 在后续 tick 正常消费', ['done', 'rejected', 'failed'].includes(orphanProcessed?.state), orphanProcessed);

  // ══ 状态不变量 ══
  const terminalWithRunning = (await adminPool.query(
    `SELECT count(*)::int c FROM mu.review_run rr
      WHERE rr.status IN ('COMPLETED','BLOCKED','FAILED')
        AND EXISTS (SELECT 1 FROM mu.agent_attempt a WHERE a.run_id=rr.run_id AND a.status IN ('RUNNING','QUEUED'))`)).rows[0].c;
  ok('INV-a 终态 run 零 RUNNING/QUEUED attempt 残留', terminalWithRunning === 0, { terminalWithRunning });
  const badStates = (await adminPool.query(
    `SELECT count(*)::int c FROM mu.job WHERE state NOT IN ('queued','running','done','rejected','failed')`)).rows[0].c;
  ok('INV-b job 状态域合法（CHECK 兜底）', badStates === 0);
  const runningNoLease = (await adminPool.query(
    `SELECT count(*)::int c FROM mu.job WHERE state='running' AND locked_at IS NULL`)).rows[0].c;
  ok('INV-c running 必有 locked_at（lease 记录）', runningNoLease === 0, { runningNoLease });
  const retryDup = (await adminPool.query(
    `SELECT count(*)::int c FROM (SELECT run_id, agent_role, attempt FROM mu.agent_attempt
       GROUP BY run_id, agent_role, attempt HAVING count(*) > 1) x`)).rows[0].c;
  ok('INV-d attempt 编号唯一（run+role+attempt 无重复）', retryDup === 0);
  const deadLetterRequeue = (await adminPool.query(
    `SELECT count(*)::int c FROM mu.job j
      WHERE j.state='queued' AND j.result->>'reason' IS NOT NULL
        AND j.result->>'reason' IN ('manual_job_expired_unconsumable','installation_mismatch')`)).rows[0].c;
  ok('INV-e dead-letter/rejected 不重回队首', deadLetterRequeue === 0);
  const crossTenant = (await adminPool.query(
    `SELECT count(*)::int c FROM mu.job j
      WHERE NOT EXISTS (SELECT 1 FROM mu.tenant t WHERE t.tenant_id=j.tenant_id)`)).rows[0].c;
  ok('INV-f 全部 job 有合法租户归属', crossTenant === 0);

  server.close();
} finally {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await adminPool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
