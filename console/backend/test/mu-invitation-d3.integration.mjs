#!/usr/bin/env node
// mu-invitation-d3.integration.mjs — D-3 修复验证：invitation_role_check 收紧 + API 403 + claim 拒绝
// 自 boot postgres + fixture 登录
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const here = path.dirname(fileURLToPath(import.meta.url));
const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? pass++ : fail++; console.log((c ? '  PASS  ' : '  FAIL  ') + n + (d ? ' ' + JSON.stringify(d).slice(0, 140) : '')); };

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'pw';
process.env.CONSOLE_SESSION_SECRET = 'd3-test-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
delete process.env.MU_FIXTURES;

const CTR = `d3-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16900 + Math.floor(Math.random() * 80);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
process.env.CONSOLE_PG_DSN = dsn;
{
  const deadline = Date.now() + 90_000;
  for (;;) {
    try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); await p.end(); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise(r => setTimeout(r, 800)); }
  }
}
const { createConsole } = await import('../server.mjs');
const { server } = createConsole({ evidenceRoot: here, distDir: path.join(here, 'no-dist') });
await new Promise(r => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

const login = async (subject) => {
  const r = await fetch(BASE + '/api/mu/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ provider: 'fixture', subject }) });
  return { cookie: (r.headers.get('set-cookie') || '').split(';')[0], csrf: (r.headers.get('set-cookie') || '').match(/mp_csrf=([^;,]+)/)?.[1], json: await r.json().catch(() => null) };
};
const call = async (p, { method = 'GET', body, cookie, csrf } = {}) => {
  const r = await fetch(BASE + p, { method, headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}), ...(body ? { 'content-type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined });
  let j = null; try { j = await r.json(); } catch {}
  return { status: r.status, json: j };
};

try {
  await new Promise(r => setTimeout(r, 3000));
  const admin = await login('fixture:dev-pilot');
  ok('D0 admin(platform_admin) 登录', admin.json?.role === 'platform_admin');

  // D-3 API 403：platform_admin 邀请
  const r1 = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: { role: 'platform_admin', expected_subject: '88888888', expected_login: 'test-admin-ext' } });
  ok('D3-API platform_admin 邀请 403', r1.status === 403 && r1.json?.error?.reason === 'platform_admin_invitation_forbidden', r1.json);
  // contributor/reviewer/maintainer/auditor 邀请正常
  for (const role of ['contributor', 'reviewer', 'maintainer', 'auditor']) {
    const r = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: { role, expected_subject: '12345678', expected_login: `test-${role}` } });
    ok(`D3-API ${role} 邀请 200`, r.status === 200 && r.json?.ok === true && r.json?.invitation?.role === role);
  }
  // v23 migration
  const v23 = (await pool.query(`SELECT version FROM mu.schema_migrations WHERE version=23`)).rowCount;
  ok('D3-DB v23 mu_invitation_role_tighten 已应用', v23 === 1);
  const checkDef = (await pool.query(`SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conrelid='mu.invitation'::regclass AND conname='invitation_role_check'`)).rows[0]?.def;
  ok('D3-DB CHECK 不含 platform_admin', !checkDef?.includes('platform_admin'), checkDef);
  const invRow = (await pool.query(`SELECT role FROM mu.invitation WHERE role='platform_admin'`)).rowCount;
  ok('D3-DB 存量 platform_admin invitation=0', invRow === 0);
} catch (e) { fail++; console.error('HARNESS ERROR', e); }
finally {
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch {}
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
