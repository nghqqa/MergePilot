#!/usr/bin/env node
// console/backend/test/mu-facade-rbac.integration.mjs — rc.10 SEC-1/SEC-3/SEC-5 收敛集成测试。
// 覆盖：facade 端点（/api/overview·pulls·pending·tickets·evidence·audit·runs）按权威
// ROLE_ACTIONS 收敛的三角色矩阵；members 读=read_audit；rag-trial POST 强制 CSRF；
// v21 audit_event 不可变封印（UPDATE/DELETE 拒绝、INSERT 放行）。
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
process.env.CONSOLE_PG_DSN = process.env.CONSOLE_PG_DSN || ''; // 由下方 docker 容器填充
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
process.env.CONSOLE_SESSION_SECRET = 'facade-it-session-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
delete process.env.MU_FIXTURES; // 安全基线：不启用 fixture 执行面

const CTR = `mu-facade-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16300 + Math.floor(Math.random() * 80);
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

async function muLogin(subject) {
  const res = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject }),
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
  await new Promise((r) => setTimeout(r, 1500)); // 等 eager schema init

  const admin = await muLogin('fixture:dev-pilot');
  ok('F0 admin（platform_admin）fixture 登录', admin.status === 200 && admin.json?.role === 'platform_admin');

  // 建 contributor / auditor 成员
  await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { login: 'bob', role: 'contributor' } });
  await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { login: 'harry', role: 'auditor' } });
  const bob = await muLogin('fixture:bob');
  const harry = await muLogin('fixture:harry');
  ok('F0 contributor/auditor 就位', bob.json?.role === 'contributor' && harry.json?.role === 'auditor',
    { bob: bob.json?.role, harry: harry.json?.role });

  // ── F1：未登录 401 ──
  for (const p of ['/api/overview', '/api/pulls', '/api/pending', '/api/audit', '/api/runs']) {
    const r = await call(p);
    ok(`F1 未登录 ${p} → 401`, r.status === 401, r.status);
  }

  // ── F2：auditor 矩阵（read_audit only）──
  ok('F2a auditor /api/audit → 200', (await call('/api/audit', { cookie: harry.cookie })).status === 200);
  for (const p of ['/api/overview', '/api/pulls', '/api/pending', '/api/runs']) {
    const r = await call(p, { cookie: harry.cookie });
    ok(`F2 auditor ${p} → 403 action_not_granted（read_pull_request 不放行）`,
      r.status === 403 && r.json?.error?.reason === 'action_not_granted', { p, s: r.status, j: r.json });
  }
  const evAud = await call('/api/evidence', { cookie: harry.cookie });
  ok('F2 auditor /api/evidence → 403', evAud.status === 403, evAud.status);

  // ── F3：contributor 矩阵（read_pull_request ✓ / read_audit ✗）──
  for (const p of ['/api/overview', '/api/pulls', '/api/pending', '/api/runs', '/api/tickets', '/api/evidence']) {
    const r = await call(p, { cookie: bob.cookie });
    ok(`F3 contributor ${p} → 200`, r.status === 200, { p, s: r.status, j: r.json });
  }
  const bobAudit = await call('/api/audit', { cookie: bob.cookie });
  ok('F3 contributor /api/audit → 403 action_not_granted', bobAudit.status === 403
    && bobAudit.json?.error?.reason === 'action_not_granted', bobAudit.json);

  // ── F4：platform_admin 全通过（read_pull_request+read_audit 均在矩阵）──
  for (const p of ['/api/overview', '/api/pulls', '/api/pending', '/api/runs', '/api/audit']) {
    const r = await call(p, { cookie: admin.cookie });
    ok(`F4 platform_admin ${p} → 200`, r.status === 200, { p, s: r.status });
  }

  // ── F5：members 读收敛为 read_audit ──
  const bobMembers = await call('/api/mu/members', { cookie: bob.cookie });
  ok('F5a contributor /api/mu/members → 403 action_not_granted（SEC-5）',
    bobMembers.status === 403 && bobMembers.json?.error?.reason === 'action_not_granted', bobMembers.json);
  const harryMembers = await call('/api/mu/members', { cookie: harry.cookie });
  ok('F5b auditor /api/mu/members → 200（审计需要成员-角色映射）', harryMembers.status === 200);

  // ── F6：rag-trial POST 强制 CSRF（SEC-3；contributor 具 rag_query）──
  const body = { q: 'probe', repo: 'acme/app', branch: 'main', k: 1 };
  const noTok = await call('/api/rag-trial/query', { method: 'POST', cookie: bob.cookie, body });
  ok('F6a rag-trial POST 无 CSRF token → 403 csrf_required', noTok.status === 403
    && noTok.json?.error?.reason === 'csrf_required', noTok.json);
  const badTok = await call('/api/rag-trial/query', { method: 'POST', cookie: bob.cookie, csrf: 'wrong-token', body });
  ok('F6b rag-trial POST 错误 token → 403 csrf_required', badTok.status === 403
    && badTok.json?.error?.reason === 'csrf_required');
  const goodTok = await call('/api/rag-trial/query', { method: 'POST', cookie: bob.cookie, csrf: bob.csrf, body });
  ok('F6c rag-trial POST 正确 token → 非 403（进入后续校验面）',
    goodTok.status !== 403, { s: goodTok.status, j: goodTok.json });
  const delNoTok = await call('/api/rag-trial/delete', { method: 'POST', cookie: bob.cookie,
    body: { repo: 'acme/app', branch: 'main', doc_path: 'x.md' } });
  ok('F6d rag-trial delete 无 token → 403（破坏性端点同门）', delNoTok.status === 403
    && delNoTok.json?.error?.reason === 'csrf_required');
  const getOk = await call('/api/rag-trial/status', { cookie: bob.cookie });
  ok('F6e GET 不受 CSRF 门影响', getOk.status !== 403, getOk.status);

  // ── F7：v21 audit_event 不可变封印 ──
  const v21 = (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=21`)).rowCount;
  ok('F7a v21 mu_audit_event_seal 已应用', v21 === 1);
  const seed = (await pool.query(`SELECT seq FROM mu.audit_event ORDER BY seq DESC LIMIT 1`)).rows[0];
  ok('F7b audit 行存在（bootstrap/登录审计）', Boolean(seed), seed);
  let updErr = null;
  try { await pool.query(`UPDATE mu.audit_event SET kind='tampered' WHERE seq=$1`, [seed.seq]); } catch (e) { updErr = e; }
  ok('F7c UPDATE 被封印拒绝（audit_event_sealed）', updErr !== null && /audit_event_sealed|append-only/.test(String(updErr.message)), String(updErr?.message ?? '').slice(0, 90));
  let delErr = null;
  try { await pool.query(`DELETE FROM mu.audit_event WHERE seq=$1`, [seed.seq]); } catch (e) { delErr = e; }
  ok('F7d DELETE 被封印拒绝', delErr !== null && /audit_event_sealed|append-only/.test(String(delErr.message)), String(delErr?.message ?? '').slice(0, 90));
  const stillThere = (await pool.query(`SELECT 1 FROM mu.audit_event WHERE seq=$1`, [seed.seq])).rowCount;
  ok('F7e 被攻击行原样保留', stillThere === 1);
  // 新审计 INSERT 正常（登录产生的审计在 F0 已隐式验证；此处显式写一行再读）
  await pool.query(`INSERT INTO mu.audit_event (tenant_id, actor_user_id, kind, detail)
    SELECT tenant_id, NULL, 'FACADE_IT_PROBE', '{"probe":true}'::jsonb FROM mu.tenant LIMIT 1`);
  const ins = (await pool.query(`SELECT 1 FROM mu.audit_event WHERE kind='FACADE_IT_PROBE'`)).rowCount;
  ok('F7f append-only INSERT 正常', ins === 1);
} catch (e) {
  fail++; console.error('HARNESS ERROR', e);
} finally {
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
