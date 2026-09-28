#!/usr/bin/env node
// console/backend/test/mu-ghapp.integration.mjs — Wave 2B 集成测试。
// GitHub App 全部走本地 mock（合成密钥/合成 installation/repo id）——零真实 GitHub、
// 零真实凭据。自起一次性 postgres:16-alpine（随机回环端口，finally 清理）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const HERE = path.dirname(fileURLToPath(import.meta.url));
const { createConsole } = await import('../server.mjs');
const { __setGithubAppForTests, __resetGithubApp } = await import('../lib/multiuser/ghapp.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

const CTR = `mu-ghapp-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16900 + Math.floor(Math.random() * 80);
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

const WEBHOOK_SECRET = 'whsec_test_only_synthetic';
const savedEnv = { ...process.env };
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_SESSION_SECRET = 'mu-ghapp-it-secret';
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.MU_GITHUB_APP_ID = '999001';
process.env.MU_GITHUB_APP_PRIVATE_KEY = 'synthetic-private-key-not-real';
process.env.MU_GITHUB_WEBHOOK_SECRET = WEBHOOK_SECRET;
process.env.MU_GITHUB_APP_INSTALL_CALLBACK_URL = 'http://127.0.0.1:4730/api/mu/github/install/callback';
// 登录 OAuth 同步配置（GA11 跨用途隔离测试需要登录 callback 可达配置层之后）
process.env.MU_GITHUB_OAUTH_CLIENT_ID = 'ov22_ghapp_test';
process.env.MU_GITHUB_OAUTH_CLIENT_SECRET = 'ghapp-oauth-test-secret';
process.env.MU_GITHUB_OAUTH_CALLBACK_URL = 'http://127.0.0.1:4730/api/mu/auth/oauth/github/callback';

// ── mock GitHub App adapter：合成 installation/repo 表 ──
const INSTALLATIONS = {
  7001: { owner: { id: 501, login: 'acme-org' }, repos: [
    { id: 9001, name: 'app', owner_login: 'acme-org', owner_id: 501, default_branch: 'main', private: false },
    { id: 9002, name: 'lib', owner_login: 'acme-org', owner_id: 501, default_branch: 'main', private: false },
  ] },
  7002: { owner: { id: 502, login: 'other-org' }, repos: [
    { id: 9001, name: 'app', owner_login: 'other-org', owner_id: 502, default_branch: 'main', private: false }, // 撞 id 场景
  ] },
};
let listShouldFail = false;
__setGithubAppForTests({
  async listRepositories(_cfg, installationId) {
    if (listShouldFail) throw new Error('ghapp_repos_http_502');
    const inst = INSTALLATIONS[installationId];
    if (!inst) throw new Error('ghapp_repos_http_404');
    return inst.repos.map((r) => ({ ...r }));
  },
});

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

async function fixtureLogin(subject, tenantSlug) {
  const res = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject, ...(tenantSlug ? { tenant_slug: tenantSlug } : {}) }),
  });
  const sc = res.headers.get('set-cookie') || '';
  return { status: res.status, cookie: sc.split(';')[0], csrf: sc.match(/mp_csrf=([^;]+)/)?.[1], json: await res.json().catch(() => null) };
}
async function call(p, { method = 'GET', body = null, cookie = null, csrf = null } = {}) {
  const res = await fetch(BASE + p, {
    method, headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
      ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json, headers: res.headers };
}
function sign(bodyStr) {
  return 'sha256=' + crypto.createHmac('sha256', WEBHOOK_SECRET).update(bodyStr, 'utf8').digest('hex');
}
async function hook(event, bodyObj, { sig, delivery } = {}) {
  const raw = JSON.stringify(bodyObj);
  return fetch(BASE + '/api/mu/github/webhook', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-github-event': event,
      'x-github-delivery': delivery ?? ('d-' + crypto.randomBytes(8).toString('hex')),
      'x-hub-signature-256': sig ?? sign(raw),
    },
    body: raw,
  });
}

try {
  const admin = await fixtureLogin('fixture:dev-pilot'); // platform_admin
  // dana=maintainer, erin=contributor（tenant A）
  await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: { login: 'dana', role: 'maintainer' } });
  await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: { login: 'erin', role: 'contributor' } });
  const dana = await fixtureLogin('fixture:dana');
  const erin = await fixtureLogin('fixture:erin');

  // ── GA-1 状态端点与权限矩阵 ──
  const st = await call('/api/mu/github/app/status', { cookie: dana.cookie });
  ok('GA1 app/status（maintainer）：configured + 权限/事件清单只读',
    st.status === 200 && st.json?.configured === true
      && st.json.permissions.every((x) => x.endsWith(':read')) && !st.json.permissions.some((x) => x.includes('write'))
      && st.json.events.includes('installation'), st.json);
  ok('GA1b contributor 不可访问（403）', (await call('/api/mu/github/app/status', { cookie: erin.cookie })).status === 403);
  ok('GA1c 未登录 401', (await call('/api/mu/github/app/status')).status === 401);

  // ── GA-2 安装 start/callback ──
  const noCsrf = await call('/api/mu/github/install/start', { method: 'POST', cookie: dana.cookie, body: {} });
  ok('GA2 install/start 缺 CSRF → 403', noCsrf.status === 403);
  const nonM = await call('/api/mu/github/install/start', { method: 'POST', cookie: erin.cookie, csrf: erin.csrf, body: {} });
  ok('GA2b 非 maintainer → 403', nonM.status === 403);
  const start = await fetch(BASE + '/api/mu/github/install/start', {
    method: 'POST', headers: { cookie: dana.cookie, 'x-csrf-token': dana.csrf },
  });
  const startJson = await start.json().catch(() => null);
  const corr = (start.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1];
  const state = new URL(startJson?.install_url ?? 'x://y/').searchParams.get('state');
  ok('GA2c install/start（maintainer+CSRF）：install_url+state+corr cookie',
    start.status === 200 && Boolean(state) && Boolean(corr) && startJson.install_url.includes('state='), startJson);
  // 伪造 state 的 callback
  const badCb = await fetch(BASE + '/api/mu/github/install/callback?installation_id=7001&setup_action=install&state=forged',
    { redirect: 'manual', headers: { cookie: dana.cookie + '; mu_oauth_corr=' + corr } });
  ok('GA2d 伪造 state → ghapp_error=state_invalid', badCb.status === 302 && /ghapp_error=state_invalid/.test(badCb.headers.get('location') || ''));
  // 无 corr（跨浏览器）——独立 flow（state 单次消费：负路径也消费流）
  const stE = await fetch(BASE + '/api/mu/github/install/start', { method: 'POST', headers: { cookie: dana.cookie, 'x-csrf-token': dana.csrf } });
  const stateE = new URL((await stE.json()).install_url).searchParams.get('state');
  const noCorrCb = await fetch(BASE + `/api/mu/github/install/callback?installation_id=7001&setup_action=install&state=${encodeURIComponent(stateE)}`, { redirect: 'manual', headers: { cookie: dana.cookie } });
  ok('GA2e 无 correlation（跨浏览器）→ state_invalid', noCorrCb.status === 302 && /ghapp_error=state_invalid/.test(noCorrCb.headers.get('location') || ''));
  // 合法 callback——独立 flow
  const stF = await fetch(BASE + '/api/mu/github/install/start', { method: 'POST', headers: { cookie: dana.cookie, 'x-csrf-token': dana.csrf } });
  const corrF = (stF.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1];
  const stateF = new URL((await stF.json()).install_url).searchParams.get('state');
  const okCb = await fetch(BASE + `/api/mu/github/install/callback?installation_id=7001&setup_action=install&state=${encodeURIComponent(stateF)}`,
    { redirect: 'manual', headers: { cookie: dana.cookie + '; mu_oauth_corr=' + corrF } });
  ok('GA2f 合法安装回调 → 302 /multiuser + installation 落库（tenant=A）',
    okCb.status === 302 && (okCb.headers.get('location') || '') === '/multiuser'
      && (await pool.query(`SELECT tenant_id FROM mu.github_app_installation WHERE installation_id=7001`)).rows.length === 1, okCb.headers.get('location'));
  // 伪造 installation_id（不存在的安装）
  const st2 = await fetch(BASE + '/api/mu/github/install/start', { method: 'POST', headers: { cookie: dana.cookie, 'x-csrf-token': dana.csrf } });
  const corr2 = (st2.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1];
  const state2 = new URL((await st2.json()).install_url).searchParams.get('state');
  const badInst = await fetch(BASE + `/api/mu/github/install/callback?installation_id=8888&setup_action=install&state=${encodeURIComponent(state2)}`,
    { redirect: 'manual', headers: { cookie: dana.cookie + '; mu_oauth_corr=' + corr2 } });
  ok('GA2g 伪造 installation_id（API 不可读）→ installation_unreadable 且零落库',
    /ghapp_error=installation_unreadable/.test(badInst.headers.get('location') || '')
      && (await pool.query(`SELECT count(*)::int n FROM mu.github_app_installation WHERE installation_id=8888`)).rows[0].n === 0);

  // ── GA-3 installations 列表 + repositories（服务端重读） ──
  const insts = await call('/api/mu/github/installations', { cookie: dana.cookie });
  ok('GA3 installations 列表（本租户）', insts.status === 200 && insts.json?.installations?.some((i) => Number(i.installation_id) === 7001), insts.json);
  const repos = await call('/api/mu/github/installations/7001/repositories', { cookie: dana.cookie });
  ok('GA3b 授权仓库列表（服务端经 adapter 重读）',
    repos.status === 200 && repos.json?.repositories?.length === 2 && repos.json.repositories.some((r) => r.id === 9001), repos.json);
  ok('GA3c 未知 installation → 404 installation_not_found（同因不泄露）',
    (await call('/api/mu/github/installations/9999/repositories', { cookie: dana.cookie })).json?.error?.reason === 'installation_not_found');

  // ── GA-4 绑定（数字 id 稳定键 + 授权校验 + 跨租户唯一） ──
  const bind = await call('/api/mu/repositories/00000000-0000-0000-0000-000000000000/ghapp-binding', {
    method: 'POST', cookie: dana.cookie, csrf: dana.csrf, body: { installation_id: 7001, github_repo_id: 9001 } });
  ok('GA4 未知 repo_id → 404 repository_not_found', bind.status === 404);
  // 先用现有 repository 路由建 repo？——绑定端点要求 repo 已在租户登记；改为直接绑定（服务端 ensureRepository）
  // 设计：ghapp-binding POST 需要 repo 存在。为测试，先经 fixture 建一个 repo：
  const mkRepo = await call('/api/mu/repositories', { method: 'POST', cookie: dana.cookie, csrf: dana.csrf,
    body: { provider_repo_id: '9001', owner: 'acme-org', name: 'app' } });
  const repoId = mkRepo.json?.repository?.repo_id;
  const bindOk = await call(`/api/mu/repositories/${repoId}/ghapp-binding`, {
    method: 'POST', cookie: dana.cookie, csrf: dana.csrf, body: { installation_id: 7001, github_repo_id: 9001 } });
  ok('GA4b 合法绑定（repo 在授权列表）→ 200 + binding active',
    bindOk.status === 200 && bindOk.json?.binding?.binding_state === 'active', bindOk.json);
  const bindNotAuth = await call(`/api/mu/repositories/${repoId}/ghapp-binding`, {
    method: 'POST', cookie: dana.cookie, csrf: dana.csrf, body: { installation_id: 7001, github_repo_id: 4242 } });
  ok('GA4c 未授权 repo id → 404 repository_not_authorized（服务端列表核验，不信任客户端）',
    bindNotAuth.status === 404 && bindNotAuth.json?.error?.reason === 'repository_not_authorized');
  // 改名幂等：同 numeric id 新 owner/name → 同一 repo 行
  INSTALLATIONS[7001].repos[0].name = 'app-renamed';
  INSTALLATIONS[7001].repos[0].owner_login = 'acme-renamed';
  const bindRename = await call(`/api/mu/repositories/${repoId}/ghapp-binding`, {
    method: 'POST', cookie: dana.cookie, csrf: dana.csrf, body: { installation_id: 7001, github_repo_id: 9001 } });
  const reposAfter = await pool.query(`SELECT count(*)::int n FROM mu.repository WHERE tenant_id=$1 AND provider_repo_id='9001'`, [dana.json?.tenant?.tenant_id]);
  ok('GA4d owner/name 改名不产生第二逻辑仓库（numeric id 稳定键）',
    bindRename.status === 200 && reposAfter.rows[0].n === 1);
  // 跨租户唯一：tenant B 绑同一 github_repo_id
  const mkB = await call('/api/mu/tenants', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: { slug: 'gh-b', display_name: 'B' } });
  const swB = await fetch(BASE + '/api/mu/auth/tenant', { method: 'POST', headers: { cookie: admin.cookie, 'content-type': 'application/json', 'x-csrf-token': admin.csrf }, body: JSON.stringify({ tenant_id: mkB.json?.tenant?.tenant_id }) });
  const adminB = { cookie: (swB.headers.get('set-cookie') || '').split(';')[0], csrf: (await swB.json()).csrf };
  // B 需要 maintainer：admin（B 会话）添加 becky 并以其登录
  await call('/api/mu/members', { method: 'POST', cookie: adminB.cookie, csrf: adminB.csrf, body: { login: 'becky', role: 'maintainer' } });
  const becky = await fixtureLogin('fixture:becky', 'gh-b');
  // B 认领 installation 7002
  const stB = await fetch(BASE + '/api/mu/github/install/start', { method: 'POST', headers: { cookie: becky.cookie, 'x-csrf-token': becky.csrf } });
  const stBJson = await stB.json().catch(() => null);
  if (!stBJson?.install_url) throw new Error('stB failed: ' + stB.status + ' ' + JSON.stringify(stBJson).slice(0, 120));
  const corrB = (stB.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1];
  const stateB = new URL(stBJson.install_url).searchParams.get('state');
  await fetch(BASE + `/api/mu/github/install/callback?installation_id=7002&setup_action=install&state=${encodeURIComponent(stateB)}`,
    { redirect: 'manual', headers: { cookie: becky.cookie + '; mu_oauth_corr=' + corrB } });
  const mkRepoB = await call('/api/mu/repositories', { method: 'POST', cookie: becky.cookie, csrf: becky.csrf, body: { provider_repo_id: '9001', owner: 'other-org', name: 'app' } });
  const bindB = await call(`/api/mu/repositories/${mkRepoB.json?.repository?.repo_id}/ghapp-binding`, {
    method: 'POST', cookie: becky.cookie, csrf: becky.csrf, body: { installation_id: 7002, github_repo_id: 9001 } });
  ok('GA4e 跨租户同 repo 绑定被拒（409 repository_already_bound，不泄露对方租户）',
    bindB.status === 409 && bindB.json?.error?.reason === 'repository_already_bound', bindB.json);
  // B 绑自己 installation 的 9002
  const mkRepoB2 = await call('/api/mu/repositories', { method: 'POST', cookie: becky.cookie, csrf: becky.csrf, body: { provider_repo_id: '9002-x', owner: 'acme-org', name: 'lib' } });
  const bindB2 = await call(`/api/mu/repositories/${mkRepoB2.json?.repository?.repo_id}/ghapp-binding`, {
    method: 'POST', cookie: becky.cookie, csrf: becky.csrf, body: { installation_id: 7002, github_repo_id: 4242 } });
  ok('GA4f B 用他租户 installation 视角 → repository_not_authorized（7002 列表无 4242/9002 属 A 流）',
    bindB2.status === 404, bindB2.json);

  // ── GA-5 webhook 验签 ──
  const prBody = { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened',
    pull_request: { number: 42, head: { sha: 'ab'.repeat(20) } } };
  ok('GA5 缺签名 → 401', (await hook('pull_request', prBody, { sig: '' })).status === 401);
  ok('GA5b 错签名 → 401', (await hook('pull_request', prBody, { sig: 'sha256=' + '0'.repeat(64) })).status === 401);
  const rawModified = JSON.stringify({ ...prBody, action: 'synchronize' });
  ok('GA5c body 篡改后签名失效 → 401', (await fetch(BASE + '/api/mu/github/webhook', {
    method: 'POST', headers: { 'content-type': 'application/json', 'x-github-event': 'pull_request',
      'x-github-delivery': 'd-tamper', 'x-hub-signature-256': sign(JSON.stringify(prBody)) },
    body: rawModified })).status === 401);
  const ok1 = await hook('pull_request', prBody, { delivery: 'd-pr-1' });
  ok('GA5d 正确签名 → 200 + delivery processed + event_sync 入队（tenant/repo 已解析）',
    ok1.status === 200
      && (await pool.query(`SELECT state FROM mu.webhook_delivery WHERE delivery_id='d-pr-1'`)).rows[0]?.state === 'processed'
      && (await pool.query(`SELECT count(*)::int n FROM mu.job WHERE kind='event_sync' AND payload->>'delivery_id'='d-pr-1'`)).rows[0].n === 1);
  const dup = await hook('pull_request', prBody, { delivery: 'd-pr-1' });
  ok('GA5e 重复 delivery → 200 duplicate，不重复入队',
    dup.status === 200 && (await pool.query(`SELECT count(*)::int n FROM mu.job WHERE kind='event_sync' AND payload->>'delivery_id'='d-pr-1'`)).rows[0].n === 1);
  ok('GA5f 未知 installation 事件被忽略（200 ignored）',
    (await hook('pull_request', { installation: { id: 31337 }, repository: { id: 1 }, action: 'opened', pull_request: { number: 1, head: { sha: 'a' } } })).status === 200);

  // ── GA-6 生命周期事件：suspend/revoked/repository_removed ──
  await hook('installation', { action: 'suspend', installation: { id: 7001 } }, { delivery: 'd-s-1' });
  ok('GA6 installation suspend → installation.suspended_at 非空 + 相关 binding suspended',
    (await pool.query(`SELECT suspended_at FROM mu.github_app_installation WHERE installation_id=7001`)).rows[0]?.suspended_at !== null
      && (await pool.query(`SELECT binding_state FROM mu.repository_binding WHERE github_repo_id=9001`)).rows[0]?.binding_state === 'suspended');
  const bindWhenSuspended = await call(`/api/mu/repositories/${repoId}/ghapp-binding`, {
    method: 'POST', cookie: dana.cookie, csrf: dana.csrf, body: { installation_id: 7001, github_repo_id: 9001 } });
  ok('GA6b suspended installation 拒绝新绑定（409）', bindWhenSuspended.status === 409, bindWhenSuspended.json);
  await hook('installation', { action: 'unsuspend', installation: { id: 7001 } }, { delivery: 'd-s-2' });
  await hook('installation_repositories', { action: 'removed', installation: { id: 7001 },
    repositories_removed: [{ id: 9001 }] }, { delivery: 'd-r-1' });
  ok('GA6c repository_removed → binding revoked（error_code=repository_removed）',
    (await pool.query(`SELECT binding_state, error_code FROM mu.repository_binding WHERE github_repo_id=9001`)).rows[0]?.error_code === 'repository_removed');
  await hook('installation', { action: 'deleted', installation: { id: 7001 } }, { delivery: 'd-d-1' });
  ok('GA6d installation deleted → installation.revoked_at + 全部 binding revoked',
    (await pool.query(`SELECT revoked_at FROM mu.github_app_installation WHERE installation_id=7001`)).rows[0]?.revoked_at !== null
      && (await pool.query(`SELECT count(*)::int n FROM mu.repository_binding WHERE installation_id=7001 AND binding_state='revoked'`)).rows[0].n >= 1);

  // ── GA-7 撤权后不能继续绑定/同步（SQL 直撤——admin 会话已在 B 切换轮换） ──
  await pool.query(`UPDATE mu.membership SET state='revoked' WHERE user_id=(SELECT user_id FROM mu.app_user WHERE login='dana')`);
  const afterRevoke = await call('/api/mu/github/app/status', { cookie: dana.cookie });
  ok('GA7 撤权后既有 session → 403 membership_inactive', afterRevoke.status === 403);

  // ── GA-8 DB 级约束 ──
  let rej = false;
  try { await pool.query(`INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id) VALUES ($1,$2,77777,'x','y',7002)`,
    [dana.json?.tenant?.tenant_id, repoId]); } catch { rej = true; }
  ok('GA8 跨租 installation 绑定被复合 FK 拒绝（A tenant + B installation）', rej);
  let rej2 = false;
  try { await pool.query(`INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id) VALUES ($1,$2,77777,'x','y',7001)`,
    [dana.json?.tenant?.tenant_id, repoId]); } catch { rej2 = true; }
  ok('GA8b 同租重复 (tenant, repo) 绑定被 UNIQUE 拒绝', rej2);

  // ── GA-9 错误 body 脱敏 ──
  const leak = await hook('pull_request', { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened', pull_request: { number: 1, head: { sha: 'z' } } }, { sig: 'sha256=' + 'f'.repeat(64) });
  const leakBody = JSON.stringify(await leak.json().catch(() => ({})));
  ok('GA9 错误 body 零敏感信息（无 secret/key/原始响应）',
    !leakBody.includes(WEBHOOK_SECRET) && !leakBody.includes('synthetic-private-key') && leak.status === 401, leakBody.slice(0, 80));

  // GA-11（PR253 验收修复回归）：安装 flow 不得被登录 callback 消费（purpose 对称隔离）
  {
    const stI = await fetch(BASE + '/api/mu/github/install/start', { method: 'POST', headers: { cookie: becky.cookie, 'x-csrf-token': becky.csrf } });
    const corrI = (stI.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1];
    const stateI = new URL((await stI.json()).install_url).searchParams.get('state');
    const crossUse = await fetch(BASE + '/api/mu/auth/oauth/github/callback?code=CODE_OK&state=' + encodeURIComponent(stateI),
      { redirect: 'manual', headers: { cookie: 'mu_oauth_corr=' + corrI } });
    ok('GA11 安装 flow 被登录 callback 使用 → state_invalid（purpose 隔离）',
      crossUse.status === 302 && /mu_login_error=state_invalid/.test(crossUse.headers.get('location') || ''),
      { status: crossUse.status, loc: crossUse.headers.get('location'), stI: stI.status });
  }

  // ── GA-10 迁移三条件 ──
  {
    let replayErr = null; let replayOk = false;
    try {
      const store2 = await (await import('../lib/multiuser/store.mjs')).createMuStore({ pool, env: process.env });
      replayOk = (await store2.initSchema()) === true;
    } catch (e) { replayErr = e; }
    const kept = (await pool.query(`SELECT count(*)::int n FROM mu.webhook_delivery WHERE delivery_id='d-pr-1'`)).rows[0].n;
    ok('GA10 migration v6 幂等重放（含存量行）', replayOk && kept === 1,
      { err: String(replayErr?.message ?? '').slice(0, 120), kept });
  }
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  __resetGithubApp();
  for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
  Object.assign(process.env, savedEnv);
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nmu-ghapp.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
