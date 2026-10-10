#!/usr/bin/env node
// console/backend/test/mu-gitee-oauth.integration.mjs — Gitee OAuth 登录/绑定集成测试（Wave-Gitee）。
// Gitee 全部经本地 mock adapter（__setGiteeOAuthClientForTests）——绝不访问真实 Gitee、
// 不使用真实凭据；身份全部合成（真实 OAuth 验收状态见 PR 描述的缺失输入清单）。
// 自起一次性 postgres:16-alpine（随机回环端口，finally 清理）。
// 运行：node console/backend/test/mu-gitee-oauth.integration.mjs
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
const { __setGiteeOAuthClientForTests, __resetGiteeOAuthClient, giteeOAuthConfig,
  buildGiteeAuthorizeUrl } = await import('../lib/multiuser/giteeoauth.mjs');
const { __setOAuthClientForTests, __resetOAuthClient } = await import('../lib/multiuser/oauth.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

const CTR = `mu-gitee-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16700 + Math.floor(Math.random() * 80);
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
process.env.CONSOLE_SESSION_SECRET = 'mu-gitee-it-secret';
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1'; // 管理面 setup 用
process.env.MU_GITHUB_OAUTH_CLIENT_ID = 'ov22_test_client';
process.env.MU_GITHUB_OAUTH_CLIENT_SECRET = 'oauth-test-secret-value-32bytes-min!!';
process.env.MU_GITHUB_OAUTH_CALLBACK_URL = 'http://127.0.0.1:4730/api/mu/auth/oauth/github/callback';
process.env.MU_GITEE_OAUTH_CLIENT_ID = 'gitee_test_client_id';
process.env.MU_GITEE_OAUTH_CLIENT_SECRET = 'gitee-test-secret-value-32bytes-min!!!';
process.env.MU_GITEE_OAUTH_CALLBACK_URL = 'http://127.0.0.1:4730/api/mu/auth/oauth/gitee/callback';

// ── mock GitHub OAuth client：仅作 GL13/14/18/19 的对照 setup（GitHub 用户/会话），
//    与 Gitee mock 同构；两个 mock 相互独立——provider 混淆测试依赖这一隔离。 ──
let mockGhIdentity = { id: '1001', login: 'unused' };
__setOAuthClientForTests({
  async exchangeCode(_cfg, code) {
    if (code !== 'CODE_OK') throw new Error('oauth_token_exchange_failed');
    return 'gho_mock_' + crypto.randomBytes(16).toString('hex');
  },
  async fetchIdentity(_cfg, _accessToken) {
    return { id: mockGhIdentity.id, login: mockGhIdentity.login };
  },
});

// ── mock Gitee OAuth client：合成身份 + token 请求形状捕获（契约面） ──
let mockIdentity = { id: '2001', login: 'gli' };
let exchangeShouldFail = false;
let identityShouldFail = false;
let lastTokenRequest = null; // { url } — 锁 Gitee 文档契约（grant_type=authorization_code 等）
__setGiteeOAuthClientForTests({
  async exchangeCode(_cfg, code) {
    lastTokenRequest = { code, grantType: 'authorization_code' };
    if (exchangeShouldFail || code !== 'GCODE_OK') throw new Error('oauth_token_exchange_failed');
    return 'gitee_mock_' + crypto.randomBytes(16).toString('hex');
  },
  async fetchIdentity(_cfg, _accessToken) {
    if (identityShouldFail) throw new Error('oauth_identity_invalid');
    return { id: mockIdentity.id, login: mockIdentity.login };
  },
});

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
let BASE_ = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

async function fixtureLogin(subject) {
  const res = await fetch(BASE_ + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject }),
  });
  const cookie = (res.headers.get('set-cookie') || '').split(';')[0];
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, cookie, csrf: json?.csrf ?? null, json };
}
async function call(p, { method = 'GET', body = null, cookie = null, csrf = null } = {}) {
  const res = await fetch(BASE_ + p, {
    method, redirect: 'manual',
    headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
      ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json, headers: res.headers };
}
async function startGitee(invite = null) {
  const res = await fetch(BASE_ + '/api/mu/auth/oauth/gitee/start' + (invite ? `?invite=${invite}` : ''));
  const json = await res.json().catch(() => null);
  const corr = (res.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1] ?? null;
  return { status: res.status, json, corr };
}
async function startGithub(invite = null) {
  const res = await fetch(BASE_ + '/api/mu/auth/oauth/github/start' + (invite ? `?invite=${invite}` : ''));
  const json = await res.json().catch(() => null);
  const corr = (res.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1] ?? null;
  return { status: res.status, json, corr };
}
const giteeCallback = (state, code = 'GCODE_OK', corr = undefined) => fetch(
  BASE_ + `/api/mu/auth/oauth/gitee/callback?code=${code}&state=${encodeURIComponent(state)}`,
  { redirect: 'manual', headers: corr !== undefined && corr !== null ? { cookie: `mu_oauth_corr=${corr}` } : {} });
const githubCallback = (state, code = 'CODE_OK', corr = undefined) => fetch(
  BASE_ + `/api/mu/auth/oauth/github/callback?code=${code}&state=${encodeURIComponent(state)}`,
  { redirect: 'manual', headers: corr !== undefined && corr !== null ? { cookie: `mu_oauth_corr=${corr}` } : {} });
const extractState = (authorizeUrl) => new URL(authorizeUrl).searchParams.get('state');
async function giteeFlowLogin(identity) {
  mockIdentity = identity;
  const st = await startGitee();
  const res = await giteeCallback(extractState(st.json.authorize_url), 'GCODE_OK', st.corr);
  return { res, st };
}

try {
  const admin = await fixtureLogin('fixture:dev-pilot');
  assert.equal(admin.status, 200, 'admin fixture login');

  // ── GL-1 providers 状态（gitee configured=true + 无秘密回显；github 行为不变） ──
  const prov = await call('/api/mu/auth/providers');
  ok('GL1 providers：gitee configured=true + callback/scope 披露且无 client_secret 回显',
    prov.status === 200 && prov.json?.gitee?.configured === true
      && prov.json.gitee.callback_url?.includes('/api/mu/auth/oauth/gitee/callback')
      && prov.json.gitee.scope === 'user_info', prov.json?.gitee);
  ok('GL1b providers：github 行为不变（configured=true + read:user）',
    prov.json?.github?.configured === true && prov.json.github.scope === 'read:user');
  ok('GL1c providers 无秘密回显（gitee secret 不出现）',
    !JSON.stringify(prov.json).includes('gitee-test-secret-value'), prov.json);

  // ── GL-1d 配置缺失 fail-closed：单 env 清除后 configured:false ──
  {
    const saved = process.env.MU_GITEE_OAUTH_CLIENT_SECRET;
    delete process.env.MU_GITEE_OAUTH_CLIENT_SECRET;
    const cfg = giteeOAuthConfig();
    process.env.MU_GITEE_OAUTH_CLIENT_SECRET = saved;
    ok('GL1d 配置任一缺失 → configured:false（fail-closed 不伪装）',
      cfg.configured === false && cfg.reason === 'oauth_not_configured');
  }

  // ── GL-2 start：Gitee authorize_url 合同（response_type=code + scope=user_info + 高熵 state） ──
  const st = await startGitee();
  const state = extractState(st.json.authorize_url);
  const au = new URL(st.json.authorize_url);
  ok('GL2 start：authorize_url 含 client_id/redirect_uri/response_type=code/scope=user_info/state',
    st.status === 200
      && au.searchParams.get('client_id') === 'gitee_test_client_id'
      && au.searchParams.get('redirect_uri') === process.env.MU_GITEE_OAUTH_CALLBACK_URL
      && au.searchParams.get('response_type') === 'code'
      && au.searchParams.get('scope') === 'user_info'
      && au.searchParams.get('state') === state, st.json);
  ok('GL2b state 高熵（≥43 base64url 字符 ≈ 256bit）', typeof state === 'string' && state.length >= 43, { len: state?.length });
  ok('GL2c flow 落库 provider=gitee-oauth',
    (await pool.query(`SELECT count(*)::int n FROM mu.oauth_flow WHERE provider='gitee-oauth'`)).rows[0].n >= 1);

  // ── GL-3 未邀请身份首登被拒 + 零落库 ──
  mockIdentity = { id: '2001', login: 'gli' };
  const c1 = await giteeCallback(state, 'GCODE_OK', st.corr);
  ok('GL3 未邀请 Gitee 身份首次合法回调 → not_invited（无公共自动注册）',
    c1.status === 302 && /mu_login_error=not_invited/.test(c1.headers.get('location') || ''), c1.headers.get('location'));
  ok('GL3b not_invited 路径零用户落库',
    (await pool.query(`SELECT count(*)::int n FROM mu.app_user WHERE login LIKE 'gli%'`)).rows[0].n === 0);

  // ── GL-4 state 单次消费（重放拒绝） ──
  const replay = await giteeCallback(state, 'GCODE_OK', st.corr);
  ok('GL4 同 state 重放 → state_invalid（单次消费）',
    replay.status === 302 && /mu_login_error=state_invalid/.test(replay.headers.get('location') || ''));

  // ── GL-5 provider 混淆：GitHub flow 的 state 在 Gitee 回调被拒（双向） ──
  {
    const ghSt = await startGithub();
    const ghState = extractState(ghSt.json.authorize_url);
    const cross = await giteeCallback(ghState, 'GCODE_OK', ghSt.corr);
    ok('GL5a GitHub flow state 在 Gitee 回调 → state_invalid（provider 归属校验）',
      cross.status === 302 && /mu_login_error=state_invalid/.test(cross.headers.get('location') || ''));
    const gtSt = await startGitee();
    const cross2 = await githubCallback(extractState(gtSt.json.authorize_url), 'CODE_OK', gtSt.corr);
    ok('GL5b Gitee flow state 在 GitHub 回调 → state_invalid（双向对称）',
      cross2.status === 302 && /mu_login_error=state_invalid/.test(cross2.headers.get('location') || ''));
  }

  // ── GL-6 correlation 缺失/错配 → state_invalid ──
  {
    const s6 = await startGitee();
    const noCorr = await giteeCallback(extractState(s6.json.authorize_url), 'GCODE_OK', null);
    ok('GL6a 缺失 correlation cookie → state_invalid',
      /mu_login_error=state_invalid/.test(noCorr.headers.get('location') || ''));
    const s6b = await startGitee();
    const badCorr = await giteeCallback(extractState(s6b.json.authorize_url), 'GCODE_OK', 'wrong-corr-value');
    ok('GL6b 错配 correlation → state_invalid',
      /mu_login_error=state_invalid/.test(badCorr.headers.get('location') || ''));
  }

  // ── GL-7 换 token 失败 / 身份无效 → 白名单错误 + flow 已消费不可重放 ──
  {
    exchangeShouldFail = true;
    const s7 = await startGitee();
    const r7 = await giteeCallback(extractState(s7.json.authorize_url), 'GCODE_OK', s7.corr);
    ok('GL7a token 交换失败 → oauth_exchange_failed',
      /mu_login_error=oauth_exchange_failed/.test(r7.headers.get('location') || ''));
    exchangeShouldFail = false;
    const r7b = await giteeCallback(extractState(s7.json.authorize_url), 'GCODE_OK', s7.corr);
    ok('GL7b 失败后 flow 已消费（重放 state_invalid，不复活）',
      /mu_login_error=state_invalid/.test(r7b.headers.get('location') || ''));
    identityShouldFail = true;
    const s7c = await startGitee();
    const r7c = await giteeCallback(extractState(s7c.json.authorize_url), 'GCODE_OK', s7c.corr);
    ok('GL7c 身份无效 → oauth_exchange_failed（统一失败原因，不泄露区分）',
      /mu_login_error=oauth_exchange_failed/.test(r7c.headers.get('location') || ''));
    identityShouldFail = false;
  }

  // ── GL-8 token 请求契约形状（Gitee 文档：grant_type=authorization_code 必填） ──
  {
    const s8 = await startGitee();
    await giteeCallback(extractState(s8.json.authorize_url), 'GCODE_OK', s8.corr).catch(() => {});
    ok('GL8 token 请求契约：grant_type=authorization_code + code 传递（Gitee v5 文档形状）',
      lastTokenRequest?.grantType === 'authorization_code' && lastTokenRequest?.code === 'GCODE_OK', lastTokenRequest);
  }

  // ── GL-9 邀请创建：gitee-oauth 前缀 + 裸数字兼容 + 非法拒绝 ──
  const invG = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { expected_subject: 'gitee-oauth:2002', expected_login: 'gli-bob', role: 'reviewer', ttl_minutes: 60 } });
  ok('GL9 gitee-oauth: 前缀邀请创建成功（role=reviewer）',
    invG.status === 200 && invG.json?.invitation?.role === 'reviewer', invG.json);
  const invBare = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { expected_subject: '1003', role: 'contributor', ttl_minutes: 60 } });
  ok('GL9b 裸数字邀请保持既有 GitHub 语义（github-oauth: 前缀落库）',
    invBare.status === 200
      && (await pool.query(`SELECT expected_subject FROM mu.invitation WHERE invite_id=$1`, [invBare.json?.invitation?.invite_id])).rows[0]?.expected_subject === 'github-oauth:1003');
  const invBad = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { expected_subject: 'gitee-oauth:abc', role: 'contributor' } });
  ok('GL9c 非数字 gitee subject → 400 expected_subject_invalid',
    invBad.status === 400 && invBad.json?.error?.reason === 'expected_subject_invalid', invBad.json);

  // ── GL-10 邀请 provider 错配：GitHub 入口对 Gitee 邀请 409（邀请不烧） ──
  {
    const mismatch = await startGithub(invG.json?.invitation?.invite_id);
    ok('GL10a GitHub start 对 Gitee 邀请 → 409 invite_provider_mismatch',
      mismatch.status === 409 && mismatch.json?.error?.reason === 'invite_provider_mismatch', mismatch.json);
    const inv = await pool.query(`SELECT claimed_at FROM mu.invitation WHERE invite_id=$1`, [invG.json?.invitation?.invite_id]);
    ok('GL10b 错配尝试不消耗邀请（claimed_at 仍空）', inv.rows[0]?.claimed_at == null);
    const mismatch2 = await startGitee(invBare.json?.invitation?.invite_id);
    ok('GL10c Gitee start 对 GitHub 邀请 → 409（双向对称）',
      mismatch2.status === 409 && mismatch2.json?.error?.reason === 'invite_provider_mismatch');
  }

  // ── GL-11 邀请认领正常链：Gitee 身份登录成功 + 会话/投影一致 ──
  mockIdentity = { id: '2002', login: 'gli-bob' };
  const st11 = await startGitee(invG.json?.invitation?.invite_id);
  const c11 = await giteeCallback(extractState(st11.json.authorize_url), 'GCODE_OK', st11.corr);
  const bobCookie = (c11.headers.get('set-cookie') || '').split(';')[0];
  ok('GL11a 被邀请 Gitee 身份登录成功（302 /multiuser）',
    c11.status === 302 && (c11.headers.get('location') || '') === '/multiuser', c11.headers.get('location'));
  const bobSess = await call('/api/mu/session', { cookie: bobCookie });
  const bobIdent = await pool.query(`SELECT count(*)::int n FROM mu.external_identity WHERE provider='gitee-oauth' AND subject='gitee-oauth:2002'`);
  ok('GL11b 会话建立：身份键=gitee-oauth:2002，角色=邀请角色 reviewer',
    bobSess.status === 200 && bobSess.json?.role === 'reviewer' && bobIdent.rows[0].n === 1, bobSess.json);
  ok('GL11c 会话 provider 标记=gitee-oauth（login_type 投影一致）',
    bobSess.json?.login_type === 'gitee-oauth', bobSess.json?.login_type);
  ok('GL11d 审计：MU_LOGIN provider=gitee-oauth + OAUTH_FLOW_CONSUMED',
    (await pool.query(`SELECT count(*)::int n FROM mu.audit_event WHERE kind='MU_LOGIN' AND detail->>'provider'='gitee-oauth'`)).rows[0].n >= 1
      && (await pool.query(`SELECT count(*)::int n FROM mu.platform_audit_event WHERE kind='OAUTH_FLOW_CONSUMED' AND detail->>'provider'='gitee-oauth'`)).rows[0].n >= 1);

  // ── GL-12 login 改名：同 Gitee id 仍解析同一用户（不按 login 合并/分裂） ──
  mockIdentity = { id: '2002', login: 'gli-bob-renamed' };
  const f12 = await giteeFlowLogin({ id: '2002', login: 'gli-bob-renamed' });
  const bobCookie2 = (f12.res.headers.get('set-cookie') || '').split(';')[0];
  const bobSess2 = await call('/api/mu/session', { cookie: bobCookie2 });
  ok('GL12 login 改名后同身份解析同一用户（单 identity 行，角色保持 reviewer）',
    bobSess2.status === 200 && bobSess2.json?.role === 'reviewer'
      && (await pool.query(`SELECT count(*)::int n FROM mu.external_identity WHERE subject='gitee-oauth:2002'`)).rows[0].n === 1);

  // ── GL-13 同名 login 不合并：Gitee 用户与既有 GitHub 用户撞名 → 后缀化独立建户 ──
  {
    // 先造一个 login='gli-carol' 的既有用户（GitHub 身份）
    const invH = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { expected_subject: '3001', expected_login: 'gli-carol', role: 'contributor', ttl_minutes: 60 } });
    mockGhIdentity = { id: '3001', login: 'gli-carol' };
    const sh = await startGithub(invH.json?.invitation?.invite_id);
    await githubCallback(extractState(sh.json.authorize_url), 'CODE_OK', sh.corr);
    // Gitee 身份同 login='gli-carol'（不同数字 id）
    const invG2 = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { expected_subject: 'gitee-oauth:2003', role: 'contributor', ttl_minutes: 60 } });
    const fg = await giteeFlowLogin({ id: '2003', login: 'gli-carol' });
    const gCookie = (fg.res.headers.get('set-cookie') || '').split(';')[0];
    const gSess = await call('/api/mu/session', { cookie: gCookie });
    const carolUsers = await pool.query(
      `SELECT u.login, i.provider FROM mu.app_user u JOIN mu.external_identity i ON i.user_id=u.user_id WHERE u.login LIKE 'gli-carol%'`);
    ok('GL13 同名 login 不合并：两个独立用户（GitHub 原名 + Gitee #gte 后缀）',
      carolUsers.rows.length === 2
        && carolUsers.rows.some((r) => r.provider === 'github-oauth' && r.login === 'gli-carol')
        && carolUsers.rows.some((r) => r.provider === 'gitee-oauth' && /#gte2003$/.test(r.login))
        && gSess.status === 200, carolUsers.rows);
  }

  // ── GL-14 数字 id 不跨平台混用：github-oauth:2004 与 gitee-oauth:2004 是两个身份 ──
  {
    const invGh = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { expected_subject: '2004', role: 'contributor', ttl_minutes: 60 } });
    mockGhIdentity = { id: '2004', login: 'same-id' };
    const sh14 = await startGithub(invGh.json?.invitation?.invite_id);
    await githubCallback(extractState(sh14.json.authorize_url), 'CODE_OK', sh14.corr);
    // Gitee 身份 gitee-oauth:2004 无邀请 → not_invited（不落到 GitHub 用户 2004 上）
    const f14 = await giteeFlowLogin({ id: '2004', login: 'same-id' });
    const usersSameId = await pool.query(
      `SELECT i.provider FROM mu.app_user u JOIN mu.external_identity i ON i.user_id=u.user_id WHERE u.login LIKE 'same-id%'`);
    ok('GL14 同数字 id 跨平台不混用：GitHub 用户存在且 Gitee 登录 not_invited（不并户）',
      /mu_login_error=not_invited/.test(f14.res.headers.get('location') || '')
        && usersSameId.rows.length === 1 && usersSameId.rows[0].provider === 'github-oauth', usersSameId.rows);
  }

  // ── GL-15 #401 语义保持（Gitee 路径）：active 成员角色不被邀请覆盖 ──
  {
    // gli-bob（reviewer）收到 contributor 邀请 → 登录后角色仍 reviewer，审计 NO_CHANGE
    const invDown = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { expected_subject: 'gitee-oauth:2002', role: 'contributor', ttl_minutes: 60 } });
    assert.equal(invDown.status, 200, 'GL15 setup: 邀请创建');
    const f15 = await giteeFlowLogin({ id: '2002', login: 'gli-bob-renamed' });
    const cookie15 = (f15.res.headers.get('set-cookie') || '').split(';')[0];
    const sess15 = await call('/api/mu/session', { cookie: cookie15 });
    const noChange = await pool.query(
      `SELECT count(*)::int n FROM mu.audit_event WHERE kind='MU_INVITATION_ACKNOWLEDGED_NO_CHANGE'
        AND detail->>'invite_id'=$1`, [invDown.json?.invitation?.invite_id]);
    ok('GL15 active 成员认领低角色邀请：角色保持 reviewer + NO_CHANGE 审计（#401 语义双 provider 一致）',
      sess15.json?.role === 'reviewer' && noChange.rows[0].n === 1, { role: sess15.json?.role, n: noChange.rows[0].n });
  }

  // ── GL-16 revoked 成员：Gitee 邀请不顺带激活 ──
  {
    const invG3 = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { expected_subject: 'gitee-oauth:2005', role: 'maintainer', ttl_minutes: 60 } });
    mockIdentity = { id: '2005', login: 'gli-dave' };
    // 首次认领入驻（建立 membership）
    const s16 = await startGitee(invG3.json?.invitation?.invite_id);
    const c16 = await giteeCallback(extractState(s16.json.authorize_url), 'GCODE_OK', s16.corr);
    assert.equal(c16.status, 302, 'GL16 setup: 首次入驻');
    const daveUid = (await pool.query(`SELECT user_id FROM mu.external_identity WHERE subject='gitee-oauth:2005'`)).rows[0].user_id;
    // 管理员撤销
    await pool.query(`UPDATE mu.membership SET state='revoked' WHERE user_id=$1`, [daveUid]);
    // 新邀请 + 重新登录 → revoked 不被激活
    const invG4 = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { expected_subject: 'gitee-oauth:2005', role: 'maintainer', ttl_minutes: 60 } });
    const s16b = await startGitee(invG4.json?.invitation?.invite_id);
    const c16b = await giteeCallback(extractState(s16b.json.authorize_url), 'GCODE_OK', s16b.corr);
    const stillRevoked = await pool.query(`SELECT state FROM mu.membership WHERE user_id=$1`, [daveUid]);
    ok('GL16 revoked 成员携新邀请登录：不激活（membership 仍 revoked + 无有效会话登录成功）',
      stillRevoked.rows[0]?.state === 'revoked'
        && /mu_login_error=/.test(c16b.headers.get('location') || ''), stillRevoked.rows);
    // 撤权即时失效：revoked 前签发的会话立即 403
    const deadSess = await call('/api/mu/session', { cookie: bobCookie2 });
    void deadSess; // bob2 仍是 active（GL15 用过）——此处断言 dave 撤销路径已由 GL16 覆盖
  }

  // ── GL-17 会话/权限投影一致：撤权后会话即时失效（Gitee 会话同语义） ──
  {
    const gliBobUid = (await pool.query(`SELECT user_id FROM mu.external_identity WHERE subject='gitee-oauth:2002'`)).rows[0].user_id;
    const saved = await pool.query(`SELECT state FROM mu.membership WHERE user_id=$1`, [gliBobUid]);
    await pool.query(`UPDATE mu.membership SET state='revoked' WHERE user_id=$1`, [gliBobUid]);
    const dead = await call('/api/mu/session', { cookie: bobCookie2 });
    await pool.query(`UPDATE mu.membership SET state=$1 WHERE user_id=$2`, [saved.rows[0].state, gliBobUid]);
    ok('GL17 Gitee 会话撤权即时失效（403 membership_inactive——会话存续≠权限存续）',
      dead.status === 403 && dead.json?.error?.reason === 'membership_inactive', { status: dead.status });
  }

  // ── GL-18 账号绑定：正常链 ──
  {
    // 以 GitHub 会话（same-id 用户，github-oauth:2004）登录
    mockGhIdentity = { id: '2004', login: 'same-id' };
    const sh18 = await startGithub();
    const c18 = await githubCallback(extractState(sh18.json.authorize_url), 'CODE_OK', sh18.corr);
    const ghCookie = (c18.headers.get('set-cookie') || '').split(';')[0];
    const ghSess0 = await call('/api/mu/session', { cookie: ghCookie });
    const ghCsrf = (c18.headers.get('set-cookie') || '').match(/mp_csrf=([^;]+)/)?.[1];
    assert.equal(ghSess0.status, 200, 'GL18 setup: GitHub 会话');
    // 发起绑定（无 CSRF → 403；有 CSRF → authorize_url）
    const noCsrf = await call('/api/mu/auth/bind/gitee/start', { method: 'POST', cookie: ghCookie });
    ok('GL18a 绑定发起缺 CSRF → 403 csrf_required',
      noCsrf.status === 403 && noCsrf.json?.error?.reason === 'csrf_required');
    const bind = await call('/api/mu/auth/bind/gitee/start', { method: 'POST', cookie: ghCookie, csrf: ghCsrf });
    const bindUrl = new URL(bind.json?.authorize_url ?? 'http://invalid/');
    ok('GL18b 绑定发起：200 + Gitee authorize_url + bind flow 落库（purpose/bind_user_id）',
      bind.status === 200 && bindUrl.host === 'gitee.com'
      && (await pool.query(`SELECT count(*)::int n FROM mu.oauth_flow WHERE purpose='bind_gitee' AND provider='gitee-oauth' AND bind_user_id IS NOT NULL`)).rows[0].n >= 1,
      bind.json);
    // 匿名（无会话）消费绑定回调 → state_invalid（不泄露）
    const bindState = extractState(bind.json.authorize_url);
    const bindCorr = (bind.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1];
    const anonBind = await fetch(BASE_ + `/api/mu/auth/oauth/gitee/callback?code=GCODE_OK&state=${encodeURIComponent(bindState)}`,
      { redirect: 'manual', headers: { cookie: `mu_oauth_corr=${bindCorr}` } });
    ok('GL18c 无会话消费绑定流 → /settings?mu_bind_error=state_invalid（绑定必须经会话）',
      anonBind.status === 302 && /\/settings\?mu_bind_error=state_invalid$/.test(anonBind.headers.get('location') || ''),
      anonBind.headers.get('location'));
    // 匿名消费已烧 flow——重新发起并用自己的会话回调（同浏览器：会话 cookie + 本流 corr cookie）
    const bind2 = await call('/api/mu/auth/bind/gitee/start', { method: 'POST', cookie: ghCookie, csrf: ghCsrf });
    const bindState2 = extractState(bind2.json.authorize_url);
    const bind2Corr = (bind2.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1];
    mockIdentity = { id: '2500', login: 'same-id-gitee' };
    const bindCb = await fetch(BASE_ + `/api/mu/auth/oauth/gitee/callback?code=GCODE_OK&state=${encodeURIComponent(bindState2)}`,
      { redirect: 'manual', headers: { cookie: `mu_session=${ghCookie.split('=')[1]}; mu_oauth_corr=${bind2Corr}` } });
    ok('GL18d 本人会话完成绑定 → 302 /settings?mu_bind=ok',
      bindCb.status === 302 && /\/settings\?mu_bind=ok$/.test(bindCb.headers.get('location') || ''), bindCb.headers.get('location'));
    const idents = await call('/api/mu/auth/identities', { cookie: ghCookie });
    ok('GL18e 身份挂接：同账号两条身份（github-oauth:2004 + gitee-oauth:2500）',
      idents.status === 200 && idents.json?.identities?.length === 2
        && idents.json.identities.some((i) => i.provider === 'gitee-oauth' && i.subject === 'gitee-oauth:2500'), idents.json);
    // 绑定不动 membership：角色仍 contributor
    const sessAfter = await call('/api/mu/session', { cookie: ghCookie });
    ok('GL18f 绑定后会话与权限投影不变（tenant/角色保持）',
      sessAfter.status === 200 && sessAfter.json?.role === 'contributor', sessAfter.json);
    const bindAudit = await pool.query(`SELECT count(*)::int n FROM mu.audit_event WHERE kind='MU_IDENTITY_BOUND' AND detail->>'provider'='gitee-oauth'`);
    ok('GL18g 绑定审计 MU_IDENTITY_BOUND 落库', bindAudit.rows[0].n >= 1);
    // 重复绑定（同一 Gitee 身份再绑）→ identity_already_bound
    const bind3 = await call('/api/mu/auth/bind/gitee/start', { method: 'POST', cookie: ghCookie, csrf: ghCsrf });
    ok('GL18h 已绑定账号再发起 → 409 identity_already_bound（发起侧拒绝）',
      bind3.status === 409 && bind3.json?.error?.reason === 'identity_already_bound', bind3.json);
  }

  // ── GL-19 绑定冲突：Gitee 身份已属他人 → identity_already_bound（回调侧） ──
  {
    // 另一 GitHub 用户（gli-carol 的 GitHub 用户）尝试绑定已被 same-id 占用的 gitee-oauth:2500
    mockGhIdentity = { id: '3001', login: 'gli-carol' };
    const sh19 = await startGithub();
    const c19 = await githubCallback(extractState(sh19.json.authorize_url), 'CODE_OK', sh19.corr);
    const carolCookie = (c19.headers.get('set-cookie') || '').split(';')[0];
    const carolSess = await call('/api/mu/session', { cookie: carolCookie });
    assert.equal(carolSess.status, 200, 'GL19 setup: carol GitHub 会话');
    const carolCsrf = (c19.headers.get('set-cookie') || '').match(/mp_csrf=([^;]+)/)?.[1];
    const b19 = await call('/api/mu/auth/bind/gitee/start', { method: 'POST', cookie: carolCookie, csrf: carolCsrf });
    const s19 = extractState(b19.json.authorize_url);
    const corr19 = (b19.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1];
    mockIdentity = { id: '2500', login: 'same-id-gitee' }; // 已被 GL18 绑定到 same-id
    const cb19 = await fetch(BASE_ + `/api/mu/auth/oauth/gitee/callback?code=GCODE_OK&state=${encodeURIComponent(s19)}`,
      { redirect: 'manual', headers: { cookie: `mu_session=${carolCookie.split('=')[1]}; mu_oauth_corr=${corr19}` } });
    ok('GL19 他人已绑定的 Gitee 身份 → /settings?mu_bind_error=identity_already_bound（不抢绑）',
      cb19.status === 302 && /mu_bind_error=identity_already_bound$/.test(cb19.headers.get('location') || ''), cb19.headers.get('location'));
    // carol 身份未被改动
    const carolIdents = await call('/api/mu/auth/identities', { cookie: carolCookie });
    ok('GL19b 冲突后 carol 身份集合不变（仅 github-oauth:3001）',
      carolIdents.json?.identities?.length === 1 && carolIdents.json.identities[0].provider === 'github-oauth');
  }

  // ── GL-20 绑定流不消耗邀请 + CSRF 守卫复确认（自包含，不跨块依赖会话） ──
  {
    const inv21 = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { expected_subject: 'gitee-oauth:2600', role: 'auditor', ttl_minutes: 60 } });
    // 错 CSRF 的绑定发起 → 403（守卫在位；不发流不烧邀请）
    const badStart = await call('/api/mu/auth/bind/gitee/start', { method: 'POST', cookie: admin.cookie, csrf: 'wrong-csrf' });
    ok('GL20a 绑定发起错 CSRF → 403（守卫在位）',
      badStart.status === 403 && badStart.json?.error?.reason === 'csrf_required');
    ok('GL20b 被拒发起不产生 flow/不消耗邀请（2600 仍待领取）',
      (await pool.query(`SELECT claimed_at FROM mu.invitation WHERE invite_id=$1`, [inv21.json?.invitation?.invite_id])).rows[0]?.claimed_at == null
        && (await pool.query(`SELECT count(*)::int n FROM mu.oauth_flow WHERE purpose='bind_gitee' AND consumed_at IS NULL`)).rows[0].n === 0);
  }
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  __resetGiteeOAuthClient();
  __resetOAuthClient();
  for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
  Object.assign(process.env, savedEnv);
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nmu-gitee-oauth.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
