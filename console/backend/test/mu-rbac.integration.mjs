#!/usr/bin/env node
// console/backend/test/mu-rbac.integration.mjs — MU Phase 2/3 HTTP 授权矩阵集成测试。
// 运行：node console/backend/test/mu-rbac.integration.mjs（自起一次性 postgres:16-alpine，
// 随机回环端口；跑毕 docker rm -f——绝不触碰常驻栈；身份/凭据全部为合成 fixture）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
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
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

const CTR = `mu-rbac-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16100 + Math.floor(Math.random() * 80);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
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
process.env.CONSOLE_PILOT_USER = 'dev-pilot';            // bootstrap 映射源
process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
process.env.CONSOLE_SESSION_SECRET = 'mu-it-session-secret';

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

async function muLogin(subject, tenantSlug) {
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
  // ── MU-A*：模式与登录门 ──
  const legacy = await fetch(BASE + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'dev-pilot', password: 'legacy-test-password' }),
  });
  ok('MU-A1 正式多用户模式拒绝 legacy 共享账号登录（403 legacy_login_disabled_in_multiuser）',
    legacy.status === 403 && (await legacy.json())?.error?.reason === 'legacy_login_disabled_in_multiuser');

  const ghost = await muLogin('fixture:no-such-person');
  ok('MU-A2 fixture 身份未知 → 401 identity_unknown（fail-closed 无自动注册）',
    ghost.status === 401 && ghost.json?.error?.reason === 'identity_unknown');

  const unauth = await call('/api/mu/session');
  ok('MU-A3 未登录 /api/mu/session → 401', unauth.status === 401);

  const gh = await call('/api/mu/auth/login', { method: 'POST', body: { provider: 'github', subject: 'u1' } });
  ok('MU-A4 GitHub OAuth 流程保留位 → 501 oauth_provider_not_configured（不伪装已实现）',
    gh.status === 501 && gh.json?.error?.reason === 'oauth_provider_not_configured');

  const admin = await muLogin('fixture:dev-pilot');
  ok('MU-A5 bootstrap pilot 操作员经 fixture 身份登录 → platform_admin + 迁移 tenant',
    admin.status === 200 && admin.json?.role === 'platform_admin' && admin.json?.tenant?.slug === 'default',
    admin.json);

  const sessA = await call('/api/mu/session', { cookie: admin.cookie });
  ok('MU-A6 会话摘要：角色/动作面（manage_membership 在列，read_code_content 不在）',
    sessA.status === 200 && sessA.json?.role === 'platform_admin'
      && sessA.json.actions.includes('manage_membership') === true
      && sessA.json.actions.includes('read_code_content') === false, sessA.json?.actions);

  // ── MU-B*：成员管理 + 角色门 ──
  const noCsrf = await call('/api/mu/members', { method: 'POST', body: { login: 'bob', role: 'contributor' }, cookie: admin.cookie });
  ok('MU-B1 变更方法缺 X-CSRF-Token → 403 csrf_required',
    noCsrf.status === 403 && noCsrf.json?.error?.reason === 'csrf_required');

  const addBob = await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { login: 'bob', role: 'contributor' } });
  ok('MU-B2 platform_admin 添加成员（contributor）→ 200', addBob.status === 200 && addBob.json?.membership?.role === 'contributor', addBob.json);

  const badRole = await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { login: 'eve', role: 'superadmin' } });
  ok('MU-B3 非法角色词汇 → 400（role 篡改不可入）', badRole.status === 400);

  const bob = await muLogin('fixture:bob');
  ok('MU-B4 bob 登录 → contributor', bob.status === 200 && bob.json?.role === 'contributor');

  const bobMembers = await call('/api/mu/members', { cookie: bob.cookie });
  ok('MU-B5 成员列表读=任意 active 成员（contributor 可读，只读视图）',
    bobMembers.status === 200 && (bobMembers.json?.members ?? []).some((m) => m.login === 'bob'));

  const bobAdd = await call('/api/mu/members', { method: 'POST', cookie: bob.cookie, csrf: bob.csrf,
    body: { login: 'mallory', role: 'platform_admin' } });
  ok('MU-B6 Contributor 不可管理成员（role 提权篡改 → 403 action_not_granted，零副作用）',
    bobAdd.status === 403 && bobAdd.json?.error?.reason === 'action_not_granted'
      && (await pool.query(`SELECT count(*)::int n FROM mu.app_user WHERE login='mallory'`)).rows[0].n === 0);

  const bobRepo = await call('/api/mu/repositories', { method: 'POST', cookie: bob.cookie, csrf: bob.csrf,
    body: { provider_repo_id: 'R_1', owner: 'acme', name: 'app' } });
  ok('MU-B7 Contributor 不可绑定仓库 → 403', bobRepo.status === 403 && bobRepo.json?.error?.reason === 'action_not_granted');

  // ── MU-C*：仓库绑定 + 双 tenant 隔离 + 篡改 ──
  // platform_admin 无 manage_repository_binding（矩阵语义）——绑定由 maintainer 执行
  const adminBind = await call('/api/mu/repositories', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { provider_repo_id: 'R_gh_9001', owner: 'acme', name: 'app' } });
  ok('MU-C0 platform_admin 不可绑定仓库（矩阵：绑定属 maintainer）→ 403',
    adminBind.status === 403 && adminBind.json?.error?.reason === 'action_not_granted');
  await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { login: 'dana', role: 'maintainer' } });
  const dana = await muLogin('fixture:dana');
  const addRepoA = await call('/api/mu/repositories', { method: 'POST', cookie: dana.cookie, csrf: dana.csrf,
    body: { provider_repo_id: 'R_gh_9001', owner: 'acme', name: 'app' } });
  ok('MU-C1 maintainer 绑定仓库（fixture installation + 最小权限快照）→ 200',
    addRepoA.status === 200 && addRepoA.json?.binding?.kind === 'fixture'
      && Array.isArray(addRepoA.json?.binding?.granted_scopes)
      && addRepoA.json.binding.granted_scopes.includes('pull_requests:read'), addRepoA.json);

  const mkB = await call('/api/mu/tenants', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { slug: 'ten-b', display_name: 'Tenant B' } });
  ok('MU-C2 platform_admin 创建 tenant B（创建者自动成为 B 的 platform_admin）', mkB.status === 200, mkB.json);
  const tenantBId = mkB.json?.tenant?.tenant_id;

  // 切换签发新会话（新 Set-Cookie + 新 csrf）——直接捕获响应头
  const sw2 = await fetch(BASE + '/api/mu/auth/tenant', {
    method: 'POST', headers: { cookie: admin.cookie, 'content-type': 'application/json', 'x-csrf-token': admin.csrf },
    body: JSON.stringify({ tenant_id: tenantBId }),
  });
  ok('MU-C3 tenant 切换（B 成员身份 + 新会话 csrf）', sw2.status === 200);
  const adminBCookie = (sw2.headers.get('set-cookie') || '').split(';')[0];
  const adminBCsrf = (await sw2.json())?.csrf;

  const addCarol = await call('/api/mu/members', { method: 'POST', cookie: adminBCookie, csrf: adminBCsrf,
    body: { login: 'carol', role: 'maintainer' } });
  ok('MU-C4 admin（B 会话）给 B 添加 maintainer carol', addCarol.status === 200);

  const repoAId = addRepoA.json?.repository?.repo_id;
  const tamper = await call(`/api/mu/repositories/${repoAId}/binding/revoke`, {
    method: 'POST', cookie: adminBCookie, csrf: adminBCsrf });
  ok('MU-C5 跨 tenant 篡改 repo_id → 404 repository_not_found（tenant 收窄解析，不泄露存在性）',
    tamper.status === 404 && tamper.json?.error?.reason === 'repository_not_found');

  const bodyTamper = await call('/api/mu/repositories', { method: 'POST', cookie: bob.cookie, csrf: bob.csrf,
    body: { tenant_id: tenantBId, provider_repo_id: 'R_x', owner: 'x', name: 'x' } });
  ok('MU-C6 请求体携带 tenant_id 不可扩大权限（仍按会话 tenant 判定）→ 403',
    bodyTamper.status === 403, bodyTamper.json);

  const carol = await muLogin('fixture:carol', 'ten-b');
  const carolRepo = await call('/api/mu/repositories', { method: 'POST', cookie: carol.cookie, csrf: carol.csrf,
    body: { provider_repo_id: 'R_gh_9001', owner: 'acme', name: 'app' } });
  ok('MU-C7 同 provider_repo_id 在 tenant B 独立注册（不同 repo_id）',
    carolRepo.status === 200 && carolRepo.json?.repository?.repo_id !== repoAId, carolRepo.json?.repository?.repo_id);

  const bobList = await call('/api/mu/repositories', { cookie: bob.cookie });
  ok('MU-C8 bob（tenant A）仓库列表零泄露 tenant B 行',
    (bobList.json?.repositories ?? []).every((r) => r.tenant_id !== tenantBId)
      && bobList.json.repositories.some((r) => r.repo_id === repoAId));

  // ── MU-D*：成员撤销 ──
  const revokeBob = await call('/api/mu/members/bob/revoke', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf });
  ok('MU-D1 撤销 bob 成员 → 200', revokeBob.status === 200);
  const bobAfter = await call('/api/mu/session', { cookie: bob.cookie });
  ok('MU-D2 撤销后 bob 既有会话立即失效（下一请求 403 membership_inactive）',
    bobAfter.status === 403 && bobAfter.json?.error?.reason === 'membership_inactive');
  const selfRevoke = await call('/api/mu/members/dev-pilot/revoke', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf });
  ok('MU-D3 禁止自撤销（防锁死）→ 400 cannot_revoke_self',
    selfRevoke.status === 400 && selfRevoke.json?.error?.reason === 'cannot_revoke_self');

  // ── MU-E*：审计（metadata only） ──
  const auditRows = (await pool.query(
    `SELECT kind, detail FROM mu.audit_event WHERE kind='MU_AUTH_DENIED'`)).rows;
  ok('MU-E1 拒绝审计在库（MU_AUTH_DENIED）且 detail 仅含 action/reason（无查询正文/凭据）',
    auditRows.length >= 2 && auditRows.every((r) =>
      Object.keys(r.detail).every((k) => ['action', 'reason'].includes(k))), auditRows.slice(0, 2));
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
  Object.assign(process.env, savedEnv);
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nmu-rbac.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
