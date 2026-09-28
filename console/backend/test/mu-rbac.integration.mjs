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
// PR248 复核 P1 回归基线：fixture 能力默认关闭——本套件显式开启以测正向路径，
// 默认关闭行为由 MU-A7/MU-F0 单独断言
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.MU_FIXTURES = '1';

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

  // MU-A7（PR248 复核 P1 回归）：fixture 登录默认关闭——不设 MU_ALLOW_FIXTURE_LOGIN
  // 的独立 server 实例上，已知 bootstrap subject 也被拒（fail-closed）
  {
    const savedFlag = process.env.MU_ALLOW_FIXTURE_LOGIN;
    delete process.env.MU_ALLOW_FIXTURE_LOGIN;
    const srv2 = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
    await new Promise((r) => srv2.server.listen(0, '127.0.0.1', r));
    try {
      const r2 = await fetch(`http://127.0.0.1:${srv2.server.address().port}/api/mu/auth/login`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ provider: 'fixture', subject: 'fixture:dev-pilot' }),
      });
      const j2 = await r2.json().catch(() => null);
      ok('MU-A7 fixture 登录默认关闭（未显式 MU_ALLOW_FIXTURE_LOGIN=1 → 403 fixture_login_disabled）',
        r2.status === 403 && j2?.error?.reason === 'fixture_login_disabled', j2);
    } finally { srv2.server.close(); process.env.MU_ALLOW_FIXTURE_LOGIN = savedFlag; }
  }


  const admin = await muLogin('fixture:dev-pilot');
  ok('MU-A5 bootstrap pilot 操作员经 fixture 身份登录 → platform_admin + 迁移 tenant',
    admin.status === 200 && admin.json?.role === 'platform_admin' && admin.json?.tenant?.slug === 'default',
    admin.json);

  const sessA = await call('/api/mu/session', { cookie: admin.cookie });
  ok('MU-A6 会话摘要：角色/动作面（manage_membership 在列，read_code_content 不在）',
    sessA.status === 200 && sessA.json?.role === 'platform_admin'
      && sessA.json.actions.includes('manage_membership') === true
      && sessA.json.actions.includes('read_code_content') === false, sessA.json?.actions);

  // MU-F0（PR248 复核 P1 回归）：fixtures 播种/执行器默认关闭（env 逐请求读取，可内联翻转）
  {
    const savedFixtures = process.env.MU_FIXTURES;
    delete process.env.MU_FIXTURES;
    const f0 = await call('/api/mu/fixtures/pr', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { repo_id: '00000000-0000-0000-0000-000000000000', number: 1, head_sha: 'a'.repeat(40) } });
    const t0 = await call('/api/mu/jobs/tick', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf });
    ok('MU-F0 fixtures 端点默认关闭（pr 播种与 tick 均 403 fixtures_disabled）',
      f0.status === 403 && f0.json?.error?.reason === 'fixtures_disabled'
        && t0.status === 403 && t0.json?.error?.reason === 'fixtures_disabled');
    process.env.MU_FIXTURES = savedFixtures;
  }

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
  // MU-D4（Beta Hardening W1）：成员撤销存在性侧信道消除——未知 login 与
  // "存在但属另一 tenant"（carol 仅是 B 成员）返回同形 404（逐字节一致）
  const oraUnknown = await call('/api/mu/members/ghost-user/revoke', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf });
  const oraCross = await call('/api/mu/members/carol/revoke', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf });
  const oraAgain = await call('/api/mu/members/bob/revoke', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf });
  ok('MU-D4 未知/跨租户/已撤销三态同形 404（零存在性差分；carol 的 B 成员关系不受影响）',
    oraUnknown.status === 404 && oraCross.status === 404 && oraAgain.status === 404
      && JSON.stringify(oraUnknown.json) === JSON.stringify(oraCross.json)
      && JSON.stringify(oraUnknown.json) === JSON.stringify(oraAgain.json)
      && oraCross.json?.error?.reason === 'member_not_found'
      && (await pool.query(`SELECT m.state FROM mu.membership m JOIN mu.app_user u ON u.user_id=m.user_id
          WHERE u.login='carol' AND m.tenant_id=$1`, [tenantBId])).rows[0].state === 'active',
    { unknown: oraUnknown.json, cross: oraCross.json, again: oraAgain.json });

  // ── MU-E*：审计（metadata only） ──
  const auditRows = (await pool.query(
    `SELECT kind, detail FROM mu.audit_event WHERE kind='MU_AUTH_DENIED'`)).rows;
  ok('MU-E1 拒绝审计在库（MU_AUTH_DENIED）且 detail 仅含 action/reason（无查询正文/凭据）',
    auditRows.length >= 2 && auditRows.every((r) =>
      Object.keys(r.detail).every((k) => ['action', 'reason'].includes(k))), auditRows.slice(0, 2));

  // ══ Phase 3 电池：GitHub 身份边界 + PR/ReviewRecord + 隔离矩阵 ══
  const muTenA = admin.json.tenant.tenant_id; // 本 fixture 的 tenant A = 迁移 default tenant
  // 补齐角色 fixture：erin(contributor)/frank(reviewer)/gina(auditor)（tenant A）
  for (const [login, role] of [['erin', 'contributor'], ['frank', 'reviewer'], ['gina', 'auditor']]) {
    const r = await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { login, role } });
    if (r.status !== 200) throw new Error(`member ${login} setup failed: ${JSON.stringify(r.json)}`);
  }
  const erin = await muLogin('fixture:erin');
  const frank = await muLogin('fixture:frank');
  const gina = await muLogin('fixture:gina');

  // ── MU-F*：同 PR number + 同 head_sha 双 tenant 播种 ──
  const HEAD = 'abc123def4567890abc123def4567890abc12345';
  const prA = await call('/api/mu/fixtures/pr', { method: 'POST', cookie: dana.cookie, csrf: dana.csrf,
    body: { repo_id: repoAId, number: 42, head_sha: HEAD, branch_protection_status: 'unknown', title: 'same PR in tenant A' } });
  ok('MU-F1 tenant A 播种 PR #42（protection=unknown）', prA.status === 200 && Boolean(prA.json?.pull_request?.pr_id), prA.json);
  const prAId = prA.json?.pull_request?.pr_id;
  const prB = await call('/api/mu/fixtures/pr', { method: 'POST', cookie: carol.cookie, csrf: carol.csrf,
    body: { repo_id: carolRepo.json?.repository?.repo_id, number: 42, head_sha: HEAD,
      branch_protection_status: 'known_clean', title: 'same PR in tenant B' } });
  ok('MU-F2 tenant B 播种同 number+head PR（独立行）',
    prB.status === 200 && Boolean(prB.json?.pull_request?.pr_id) && prB.json.pull_request.pr_id !== prAId);
  const prBId = prB.json?.pull_request?.pr_id;

  // ── MU-G*：跨 tenant 全动作拒绝（读/写/审查/修复/成员） ──
  ok('MU-G1 跨 tenant PR 读 → 404（tenant 收窄）',
    (await call(`/api/mu/prs/${prBId}`, { cookie: dana.cookie })).status === 404);
  ok('MU-G2 跨 tenant 审批写 → 404',
    (await call(`/api/mu/prs/${prAId}/decision`, { method: 'POST', cookie: carol.cookie, csrf: carol.csrf,
      body: { action: 'approve' } })).status === 404);
  ok('MU-G3 跨 tenant 触发审查 → 404',
    (await call(`/api/mu/prs/${prAId}/review`, { method: 'POST', cookie: carol.cookie, csrf: carol.csrf })).status === 404);
  ok('MU-G4 跨 tenant 修复 → 404',
    (await call(`/api/mu/prs/${prAId}/repair`, { method: 'POST', cookie: carol.cookie, csrf: carol.csrf })).status === 404);
  const crossMember = await call('/api/mu/members', { method: 'POST', cookie: dana.cookie, csrf: dana.csrf,
    body: { tenant_id: tenantBId, login: 'sneaky', role: 'maintainer' } });
  ok('MU-G5 跨 tenant 成员管理不可达（body tenant_id 被忽略 → 403，B 成员数不变）',
    crossMember.status === 403
      && (await pool.query(`SELECT count(*)::int n FROM mu.membership WHERE tenant_id=$1`, [tenantBId])).rows[0].n === 2,
    crossMember.json);

  // ── MU-H*：角色门（Contributor/Reviewer/Auditor/PlatformAdmin） ──
  ok('MU-H1 Contributor 不可审批', (await call(`/api/mu/prs/${prAId}/decision`, { method: 'POST',
    cookie: erin.cookie, csrf: erin.csrf, body: { action: 'approve' } })).json?.error?.reason === 'action_not_granted');
  ok('MU-H2 Contributor 不可修复', (await call(`/api/mu/prs/${prAId}/repair`, { method: 'POST',
    cookie: erin.cookie, csrf: erin.csrf })).json?.error?.reason === 'action_not_granted');
  ok('MU-H3 Contributor 不可触发审查（reviewer 起）', (await call(`/api/mu/prs/${prAId}/review`, { method: 'POST',
    cookie: erin.cookie, csrf: erin.csrf })).json?.error?.reason === 'action_not_granted');
  ok('MU-H4 Contributor 可读 PR/摘录/RAG（读面完整）',
    (await call(`/api/mu/prs/${prAId}`, { cookie: erin.cookie })).status === 200
      && (await call(`/api/mu/prs/${prAId}/changed-excerpt`, { cookie: erin.cookie })).status === 200
      && (await call(`/api/mu/repositories/${repoAId}/rag-search?q=回滚`, { cookie: erin.cookie })).status === 200);
  ok('MU-H5 Reviewer 可触发只读审查',
    (await call(`/api/mu/prs/${prAId}/review`, { method: 'POST', cookie: frank.cookie, csrf: frank.csrf })).status === 200);
  ok('MU-H6 Reviewer 不可审批', (await call(`/api/mu/prs/${prAId}/decision`, { method: 'POST',
    cookie: frank.cookie, csrf: frank.csrf, body: { action: 'approve' } })).json?.error?.reason === 'action_not_granted');
  ok('MU-H7 Auditor 不可读代码摘录', (await call(`/api/mu/prs/${prAId}/changed-excerpt`, { cookie: gina.cookie })).status === 403);
  ok('MU-H8 Auditor 不可 RAG 检索', (await call(`/api/mu/repositories/${repoAId}/rag-search?q=审计`, { cookie: gina.cookie })).status === 403);
  ok('MU-H9 Auditor 不可读 PR（只读审计元数据）', (await call(`/api/mu/prs/${prAId}`, { cookie: gina.cookie })).status === 403);
  ok('MU-H10 Auditor 可读审计（metadata only）', (await call('/api/mu/audit', { cookie: gina.cookie })).status === 200);
  ok('MU-H11 PlatformAdmin 不自动获得代码读取', (await call(`/api/mu/prs/${prAId}/changed-excerpt`, { cookie: admin.cookie })).status === 403);

  // ── MU-I*：branch protection 未知 → 禁止可合并结论；任务执行与撤销复查 ──
  const ap1 = await call(`/api/mu/prs/${prAId}/decision`, { method: 'POST', cookie: dana.cookie, csrf: dana.csrf,
    body: { action: 'approve' } });
  ok('MU-I1 protection=unknown 的 approve → 422 cannot_conclude_mergeable（fail-closed，无合并路径）',
    ap1.status === 422 && ap1.json?.error?.reason === 'cannot_conclude_mergeable', ap1.json);
  const ap2 = await call(`/api/mu/prs/${prAId}/decision`, { method: 'POST', cookie: dana.cookie, csrf: dana.csrf,
    body: { action: 'reject', note: 'not ready' } });
  ok('MU-I2 protection=unknown 的 reject 允许（拒绝不产生可合并结论）', ap2.status === 200);
  const ap3 = await call(`/api/mu/prs/${prBId}/decision`, { method: 'POST', cookie: carol.cookie, csrf: carol.csrf,
    body: { action: 'approve' } });
  ok('MU-I3 protection=known_clean 的 approve 允许（记录携带状态快照）',
    ap3.status === 200 && ap3.json?.review_record?.branch_protection_status === 'known_clean');

  // 撤销复查：frank（含 MU-H5 已入队的一单）再入队 → 撤销 frank → tick 全部拒绝
  const frJob = await call(`/api/mu/prs/${prAId}/review`, { method: 'POST', cookie: frank.cookie, csrf: frank.csrf });
  const revokedFrank = await call('/api/mu/members/frank/revoke', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf });
  ok('MU-I4 frank 入队审查 + 撤销成员', frJob.status === 200 && revokedFrank.status === 200);
  ok('MU-I5 撤销后新请求拒绝（403 membership_inactive）',
    (await call('/api/mu/session', { cookie: frank.cookie })).status === 403);
  const frankUid = (await pool.query(`SELECT user_id FROM mu.app_user WHERE login='frank'`)).rows[0].user_id;
  const frAiBefore = (await pool.query(
    `SELECT count(*)::int n FROM mu.review_record WHERE actor_user_id=$1 AND kind='ai_review'`, [frankUid])).rows[0].n;
  const tick1 = await call('/api/mu/jobs/tick', { method: 'POST', cookie: dana.cookie, csrf: dana.csrf });
  ok('MU-I6 已排队任务执行前复查成员关系 → 拒绝执行（零 provider 写入）',
    (tick1.json?.processed ?? []).filter((j) => j.state === 'rejected' && j.reason === 'membership_inactive').length === 2,
    tick1.json?.processed);
  const frAiAfter = (await pool.query(
    `SELECT count(*)::int n FROM mu.review_record WHERE actor_user_id=$1 AND kind='ai_review'`, [frankUid])).rows[0].n;
  ok('MU-I7 被拒任务未产生任何审查记录', frAiAfter === frAiBefore, { frAiBefore, frAiAfter });

  // 审查 happy path + 跨 tenant 记录隔离
  const danaReview = await call(`/api/mu/prs/${prAId}/review`, { method: 'POST', cookie: dana.cookie, csrf: dana.csrf });
  ok('MU-I8 maintainer 触发只读审查（dana）', danaReview.status === 200);
  await call('/api/mu/jobs/tick', { method: 'POST', cookie: dana.cookie, csrf: dana.csrf });
  const detailA = await call(`/api/mu/prs/${prAId}`, { cookie: dana.cookie });
  const detailB = await call(`/api/mu/prs/${prBId}`, { cookie: carol.cookie });
  ok('MU-I9 同 number+head 双 tenant 零共享：A 有 ai_review，B 零 ai_review 只有自己 approve',
    (detailA.json?.review_records ?? []).some((r) => r.kind === 'ai_review') === true
      && (detailB.json?.review_records ?? []).every((r) => r.kind !== 'ai_review') === true
      && (detailB.json?.review_records ?? []).some((r) => r.decision === 'approve') === true,
    { a: (detailA.json?.review_records ?? []).length, b: (detailB.json?.review_records ?? []).length });

  // ── MU-J*：修复（受控）+ Binding 篡改/吊销 + 分区直证 ──
  const rep1 = await call(`/api/mu/prs/${prAId}/repair`, { method: 'POST', cookie: dana.cookie, csrf: dana.csrf,
    body: { binding_id: '00000000-0000-0000-0000-000000000000' } });
  ok('MU-J1 maintainer 发起受控修复（body.binding_id 被忽略，服务端解析真 binding）', rep1.status === 200, rep1.json);
  await call('/api/mu/jobs/tick', { method: 'POST', cookie: dana.cookie, csrf: dana.csrf });
  const recA = await call(`/api/mu/prs/${prAId}`, { cookie: dana.cookie });
  ok('MU-J2 修复执行产生 repair_record（fixture 写，零真实 GitHub）',
    (recA.json?.review_records ?? []).some((r) => r.kind === 'repair_record') === true);
  await call(`/api/mu/repositories/${repoAId}/binding/revoke`, { method: 'POST', cookie: dana.cookie, csrf: dana.csrf });
  const rep2 = await call(`/api/mu/prs/${prAId}/repair`, { method: 'POST', cookie: dana.cookie, csrf: dana.csrf,
    body: { binding_id: 'forged-any-id' } });
  ok('MU-J3 Binding 吊销后修复拒绝（binding_required，伪造 binding_id 不可扩大权限）',
    rep2.status === 403 && rep2.json?.error?.reason === 'binding_required');

  const jobsA = (await pool.query(`SELECT count(*)::int n FROM mu.job WHERE tenant_id=$1`, [muTenA])).rows[0].n;
  const jobsB = (await pool.query(`SELECT count(*)::int n FROM mu.job WHERE tenant_id=$1`, [tenantBId])).rows[0].n;
  ok('MU-J4 job 表按 tenant 分区（A≥2，B=0——B 从未入队）', jobsA >= 2 && jobsB === 0, { jobsA, jobsB });
  const sharedHead = (await pool.query(`SELECT tenant_id, count(*)::int n FROM mu.review_record
      WHERE head_sha=$1 GROUP BY tenant_id`, [HEAD])).rows;
  ok('MU-J5 同 head_sha 的 ReviewRecord 按 tenant 分组（无跨 tenant 共享行）',
    sharedHead.length === 2 && sharedHead.every((r) => Number(r.n) > 0), sharedHead);

  ok('MU-J6 GitHub OAuth/App 流程状态端点如实未接入',
    (await call('/api/mu/auth/oauth/github/start')).status === 501
      && (await call('/api/mu/installations/github/status', { cookie: dana.cookie })).json?.configured === false);
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
