// console/backend/test/mu-eager-init.integration.mjs — Wave 3.9 Beta 硬化：
// schema 初始化从「首个 /api/mu 请求惰性触发」改为启动阶段显式执行（readiness gate）。
// 回归：fresh DB 上 console 一启动即迁移——health 披露 mu_schema_ready，
// 不经任何业务请求 schema 就绪；facade（独立 pool）首查不 relation-not-exist。
// 2026-10-16 增 B 系列：bootstrap 授权幂等——启动仅缺失时创建，不覆盖已有
// 成员关系角色/状态，revoked 不复活，并发安全；显式管理员授权契约不变。
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

const CTR = `mu-eager-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16800 + Math.floor(Math.random() * 60);
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
process.env.CONSOLE_SESSION_SECRET = 'eager-init-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';

const preTables = (await adminPool.query(
  `SELECT count(*)::int c FROM information_schema.tables WHERE table_schema='mu'`)).rows[0].c;

try {
  // 关键顺序：createConsole 后【不经过任何 /api/mu 业务请求】直接看 health/DB
  const { createConsole } = await import('../server.mjs');
  const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const BASE = `http://127.0.0.1:${server.address().port}`;

  // E1：ready 轮询（readiness gate 语义——启动即迁移，最迟数秒就绪）
  let health = null, readySeen = false;
  for (let i = 0; i < 30; i++) {
    const res = await fetch(BASE + '/api/health');
    health = await res.json().catch(() => null);
    if (health?.mu_schema_ready === true) { readySeen = true; break; }
    await new Promise((r) => setTimeout(r, 500));
  }
  ok('E1 启动即迁移：health.mu_schema_ready=true（零业务请求前置）', readySeen, health?.mu_schema_note);
  ok('E2 health 为 200 且带披露字段', health?.ok === true && 'mu_schema_ready' in (health ?? {}));

  const postTables = (await adminPool.query(
    `SELECT count(*)::int c FROM information_schema.tables WHERE table_schema='mu'`)).rows[0].c;
  ok('E3 mu schema 已在库中创建（表数量显著增长）', postTables > preTables + 10, { preTables, postTables });

  // E4：facade 首查（独立 pool 路径）——登录后 /api/overview 立即可用（不 relation-not-exist）
  const loginRes = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject: 'fixture:dev-pilot' }) });
  const cookie = (loginRes.headers.get('set-cookie') || '').split(';')[0];
  const ov = await fetch(BASE + '/api/overview', { headers: { cookie } });
  const ovBody = await ov.json().catch(() => null);
  ok('E4 facade 首查 200 + MU_CANONICAL_LIVE（独立 pool 不再绕过 readiness）',
    ov.status === 200 && ovBody?.data_source === 'MU_CANONICAL_LIVE', { status: ov.status });

  // E5：重复启动（新 server 实例同库）幂等——迁移重放零错误
  server.close();
  await new Promise((r) => setTimeout(r, 300));
  const { createConsole: cc2 } = await import('../server.mjs');
  const s2 = cc2({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
  await new Promise((r) => s2.server.listen(0, '127.0.0.1', r));
  let ready2 = false;
  const BASE2 = `http://127.0.0.1:${s2.server.address().port}`;
  for (let i = 0; i < 30; i++) {
    const h2 = await (await fetch(BASE2 + '/api/health')).json().catch(() => null);
    if (h2?.mu_schema_ready === true) { ready2 = true; break; }
    await new Promise((r) => setTimeout(r, 500));
  }
  ok('E5 重复启动/迁移重放幂等（第二次实例同样就绪）', ready2);
  s2.server.close();

  // ── B*：bootstrap 授权幂等（2026-10-16 修复回归：启动改为「仅缺失时创建」）──
  // 事故语义：bootstrap→ensureMembership ON CONFLICT DO UPDATE 会覆盖已有 role 并把
  // revoked 复活为 active（18700 试用栈重建实证：maintainer 演示态被重置 platform_admin）。
  // 修复后契约：首次初始化仍建 platform_admin；已有成员关系角色/状态保持；revoked 不
  // 复活；并发/重复启动不覆盖；显式管理员授权（含降级/复活）语义不变。
  const { createMuStore } = await import('../lib/multiuser/store.mjs');
  const bstore = await createMuStore({ pool: adminPool, env: process.env });
  const defTenant = await bstore.getTenantBySlug('default');
  const pilotU = await bstore.getUserByLogin('dev-pilot');
  const mrow = async () => (await adminPool.query(
    `SELECT role, state FROM mu.membership WHERE tenant_id=$1 AND user_id=$2`,
    [defTenant.tenant_id, pilotU.user_id])).rows[0] ?? null;

  // B1 空库首次初始化（E1 启动已完成 bootstrap）→ platform_admin + active
  const m1 = await mrow();
  ok('B1 空库首次初始化：pilot → platform_admin + active',
    m1?.role === 'platform_admin' && m1?.state === 'active', m1);

  // B2 已有 maintainer：重复 bootstrap（同进程两次 = 重复启动路径）保持角色与状态
  await bstore.ensureMembership({ tenantId: defTenant.tenant_id, userId: pilotU.user_id, role: 'maintainer' });
  await bstore.bootstrap();
  await bstore.bootstrap();
  const m2 = await mrow();
  ok('B2 已有 maintainer 重复初始化保持（角色+active 不被覆盖）',
    m2?.role === 'maintainer' && m2?.state === 'active', m2);

  // B3 reviewer/auditor 同契约
  for (const role of ['reviewer', 'auditor']) {
    await bstore.ensureMembership({ tenantId: defTenant.tenant_id, userId: pilotU.user_id, role });
    await bstore.bootstrap();
    const m = await mrow();
    ok(`B3 ${role} 角色启动后保持`, m?.role === role && m?.state === 'active', m);
  }

  // B4 revoked 成员不得因启动复活
  await bstore.revokeMembership(defTenant.tenant_id, pilotU.user_id);
  const m4a = await mrow();
  await bstore.bootstrap();
  await bstore.bootstrap();
  const m4 = await mrow();
  ok('B4 revoked 重复初始化后仍 revoked（state 不复活，角色保留）',
    m4a?.state === 'revoked' && m4?.state === 'revoked' && m4?.role === 'auditor', { m4a, m4 });

  // B5 并发初始化不覆盖已有授权（三个 bootstrap 同时跑）
  await bstore.ensureMembership({ tenantId: defTenant.tenant_id, userId: pilotU.user_id, role: 'maintainer' });
  await Promise.all([bstore.bootstrap(), bstore.bootstrap(), bstore.bootstrap()]);
  const m5 = await mrow();
  ok('B5 并发 bootstrap 不覆盖已有授权（仍 maintainer/active）',
    m5?.role === 'maintainer' && m5?.state === 'active', m5);

  // B6 恢复管理员访问走显式授权（管理员路径契约不变：改写为期望态，revoked→active）
  await bstore.ensureMembership({ tenantId: defTenant.tenant_id, userId: pilotU.user_id, role: 'platform_admin' });
  const m6 = await mrow();
  ok('B6 显式授权仍可改写并复活（platform_admin/active 恢复）',
    m6?.role === 'platform_admin' && m6?.state === 'active', m6);

  // B7 显式成员管理经 HTTP 仍可用（鉴权+审计），且其授权在「重启」后保持
  const { createConsole: cc3 } = await import('../server.mjs');
  const s3 = cc3({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
  await new Promise((r) => s3.server.listen(0, '127.0.0.1', r));
  const BASE3 = `http://127.0.0.1:${s3.server.address().port}`;
  let cookie3 = null, csrf3 = null;
  for (let i = 0; i < 30; i++) {
    try {
      const lr = await fetch(BASE3 + '/api/mu/auth/login', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ provider: 'fixture', subject: 'fixture:dev-pilot' }) });
      if (lr.ok) {
        cookie3 = (lr.headers.get('set-cookie') || '').split(';')[0];
        csrf3 = (await lr.json().catch(() => null))?.csrf ?? null;
        break;
      }
    } catch { /* 未就绪重试 */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  const addRes = await fetch(BASE3 + '/api/mu/members', {
    method: 'POST',
    headers: { 'content-type': 'application/json', cookie: cookie3,
      'x-csrf-token': csrf3 ?? '' },
    body: JSON.stringify({ login: 'trial-maintainer', role: 'maintainer' }) });
  ok('B7 显式加成员 200（管理员路径功能/鉴权保持）', addRes.status === 200,
    { status: addRes.status, body: await addRes.json().catch(() => null) });
  const auditAdd = (await adminPool.query(
    `SELECT count(*)::int c FROM mu.audit_event WHERE kind='MU_MEMBER_ADDED'`)).rows[0].c;
  ok('B7b MU_MEMBER_ADDED 审计在案', auditAdd >= 1, auditAdd);
  await bstore.bootstrap(); // 重启等价
  const tm = (await adminPool.query(
    `SELECT m.role, m.state FROM mu.membership m JOIN mu.app_user u ON u.user_id=m.user_id
      WHERE u.login='trial-maintainer'`)).rows[0] ?? null;
  ok('B7c 显式授权成员经重启保持（trial-maintainer=maintainer/active）',
    tm?.role === 'maintainer' && tm?.state === 'active', tm);
  s3.server.close();
} finally {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await adminPool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
