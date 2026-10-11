#!/usr/bin/env node
// console/backend/test/mu-session-source.integration.mjs — 登录来源契约集成测试（登录来源波）。
// 契约：/api/auth/session（legacy 桥）与 /api/mu/session（canonical）对同一会话必须下发
// 一致的 login_type；仅白名单 provider（github-oauth/gitee-oauth）下发；未知/缺失
// （fixture/未来 provider）不得下发、不得冒充 'github-oauth'。
// 旧会话兼容依据：mu.session.provider v1 建表即 NOT NULL，存量行即历史签发值
// （GitHub 唯一签发时代='github-oauth'）——无需回填，见 lib/muLoginType.mjs。
// 全部 OAuth 走本地 mock adapter（不访问真实 GitHub/Gitee、不使用真实凭据）。
// 运行：node console/backend/test/mu-session-source.integration.mjs
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
const { __setGiteeOAuthClientForTests, __resetGiteeOAuthClient } = await import('../lib/multiuser/giteeoauth.mjs');
const { __setOAuthClientForTests, __resetOAuthClient } = await import('../lib/multiuser/oauth.mjs');
const { knownMuLoginType, invitePlatformOf } = await import('../lib/muLoginType.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

const CTR = `mu-sesssrc-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16800 + Math.floor(Math.random() * 80);
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
process.env.CONSOLE_SESSION_SECRET = 'mu-sesssrc-it-secret';
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.MU_GITHUB_OAUTH_CLIENT_ID = 'ov22_test_client';
process.env.MU_GITHUB_OAUTH_CLIENT_SECRET = 'oauth-test-secret-value-32bytes-min!!';
process.env.MU_GITHUB_OAUTH_CALLBACK_URL = 'http://127.0.0.1:4730/api/mu/auth/oauth/github/callback';
process.env.MU_GITEE_OAUTH_CLIENT_ID = 'gitee_test_client_id';
process.env.MU_GITEE_OAUTH_CLIENT_SECRET = 'gitee-test-secret-value-32bytes-min!!!';
process.env.MU_GITEE_OAUTH_CALLBACK_URL = 'http://127.0.0.1:4730/api/mu/auth/oauth/gitee/callback';

// ── mock GitHub / Gitee OAuth clients（与 mu-gitee-oauth 套件同构，互相独立）──
let mockGhIdentity = { id: '7001', login: 'src-gh' };
__setOAuthClientForTests({
  async exchangeCode(_cfg, code) {
    if (code !== 'CODE_OK') throw new Error('oauth_token_exchange_failed');
    return 'gho_mock_' + crypto.randomBytes(16).toString('hex');
  },
  async fetchIdentity(_cfg, _accessToken) { return { ...mockGhIdentity }; },
});
let mockGiteeIdentity = { id: '7002', login: 'src-gitee' };
__setGiteeOAuthClientForTests({
  async exchangeCode(_cfg, code) {
    if (code !== 'GCODE_OK') throw new Error('oauth_token_exchange_failed');
    return 'gitee_mock_' + crypto.randomBytes(16).toString('hex');
  },
  async fetchIdentity(_cfg, _accessToken) { return { ...mockGiteeIdentity }; },
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
const extractState = (authorizeUrl) => new URL(authorizeUrl).searchParams.get('state');
async function oauthLogin(platform, identity, invite = null) {
  if (platform === 'github') mockGhIdentity = identity; else mockGiteeIdentity = identity;
  const startPath = platform === 'github' ? 'github' : 'gitee';
  const res = await fetch(BASE_ + `/api/mu/auth/oauth/${startPath}/start` + (invite ? `?invite=${invite}` : ''));
  const json = await res.json().catch(() => null);
  const corr = (res.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1] ?? null;
  const cb = await fetch(BASE_ + `/api/mu/auth/oauth/${startPath}/callback?code=${platform === 'github' ? 'CODE_OK' : 'GCODE_OK'}&state=${encodeURIComponent(extractState(json.authorize_url))}`,
    { redirect: 'manual', headers: corr ? { cookie: `mu_oauth_corr=${corr}` } : {} });
  const setCookie = cb.headers.get('set-cookie') || '';
  const cookie = setCookie.split(';')[0];
  const csrf = setCookie.match(/mp_csrf=([^;]+)/)?.[1] ?? null;
  return { status: cb.status, cookie, csrf, location: cb.headers.get('location') };
}

try {
  const admin = await fixtureLogin('fixture:dev-pilot');
  assert.equal(admin.status, 200, 'admin fixture login');

  // ── SS0 helper 纯函数契约 ──
  ok('SS0a knownMuLoginType：白名单内原样返回（大小写/空白归一）',
    knownMuLoginType('github-oauth') === 'github-oauth' && knownMuLoginType(' Gitee-OAuth ') === 'gitee-oauth');
  ok('SS0b knownMuLoginType：未知/缺失/非字符串 → null（不冒充）',
    knownMuLoginType('fixture') === null && knownMuLoginType('future-oauth') === null
      && knownMuLoginType('') === null && knownMuLoginType(undefined) === null && knownMuLoginType(null) === null);
  ok('SS0c invitePlatformOf：前缀映射 + 其余 null',
    invitePlatformOf('github-oauth:123') === 'github-oauth' && invitePlatformOf('gitee-oauth:456') === 'gitee-oauth'
      && invitePlatformOf('fixture:x') === null && invitePlatformOf(null) === null);

  // ── SS1 GitHub 会话：两个 session 端点来源一致 ──
  const invGh = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { expected_subject: 'github-oauth:7001', role: 'reviewer', ttl_minutes: 60 } });
  assert.equal(invGh.status, 200, 'SS1 setup: github 邀请创建');
  const ghLogin = await oauthLogin('github', { id: '7001', login: 'src-gh' });
  assert.ok(ghLogin.cookie, 'SS1 setup: github 登录发会话');
  const ghLegacy = await call('/api/auth/session', { cookie: ghLogin.cookie });
  const ghMu = await call('/api/mu/session', { cookie: ghLogin.cookie });
  ok('SS1 GitHub 会话：legacy 桥 user.login_type=github-oauth',
    ghLegacy.status === 200 && ghLegacy.json?.user?.login_type === 'github-oauth', ghLegacy.json?.user);
  ok('SS1 GitHub 会话：canonical login_type=github-oauth（两接口一致）',
    ghMu.status === 200 && ghMu.json?.login_type === 'github-oauth' && ghMu.json?.provider === 'github-oauth',
    ghMu.json?.login_type);

  // ── SS2 GitHub 会话的租户/角色/actions 语义不变 ──
  ok('SS2 租户/角色/actions 语义不变',
    ghMu.json?.tenant?.slug === 'default' && ghMu.json?.role === 'reviewer'
      && Array.isArray(ghMu.json?.actions) && ghMu.json.actions.includes('read_repository')
      && ghLegacy.json?.role === 'reviewer',
    { role: ghMu.json?.role, actions: ghMu.json?.actions, legacyRole: ghLegacy.json?.role });

  // ── SS3 Gitee 会话：两个 session 端点来源一致 ──
  const invGitee = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { expected_subject: 'gitee-oauth:7002', role: 'reviewer', ttl_minutes: 60 } });
  assert.equal(invGitee.status, 200, 'SS3 setup: gitee 邀请创建');
  const giteeLogin = await oauthLogin('gitee', { id: '7002', login: 'src-gitee' });
  assert.ok(giteeLogin.cookie, 'SS3 setup: gitee 登录发会话');
  const giteeLegacy = await call('/api/auth/session', { cookie: giteeLogin.cookie });
  const giteeMu = await call('/api/mu/session', { cookie: giteeLogin.cookie });
  ok('SS3 Gitee 会话：legacy 桥 user.login_type=gitee-oauth（不再误标 GitHub）',
    giteeLegacy.status === 200 && giteeLegacy.json?.user?.login_type === 'gitee-oauth', giteeLegacy.json?.user);
  ok('SS3 Gitee 会话：canonical login_type=gitee-oauth（两接口一致）',
    giteeMu.status === 200 && giteeMu.json?.login_type === 'gitee-oauth' && giteeMu.json?.provider === 'gitee-oauth',
    giteeMu.json?.login_type);

  // ── SS4 同一双身份用户：标签随本次认证来源变化 ──
  // gh 用户（7001）绑定新 gitee 身份（7012；7002 已被 SS3 的独立用户占用）
  const ghCsrf = ghLogin.csrf;
  const bindSt = await call('/api/mu/auth/bind/gitee/start', { method: 'POST', cookie: ghLogin.cookie, csrf: ghCsrf });
  assert.equal(bindSt.status, 200, 'SS4 setup: 绑定发起（本人 CSRF）');
  mockGiteeIdentity = { id: '7012', login: 'src-gitee-2' };
  const bindCorr = (bindSt.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1];
  const bindCb = await fetch(BASE_ + `/api/mu/auth/oauth/gitee/callback?code=GCODE_OK&state=${encodeURIComponent(extractState(bindSt.json.authorize_url))}`,
    { redirect: 'manual', headers: { cookie: `mu_session=${ghLogin.cookie.split('=')[1]}; mu_oauth_corr=${bindCorr}` } });
  assert.ok([302, 303].includes(bindCb.status) && /mu_bind=ok$/.test(bindCb.headers.get('location') || ''), 'SS4 setup: 绑定回调 302 mu_bind=ok');
  // 绑定后：先 github 登录 → 标签 github；再 gitee 登录 → 标签 gitee（同一 user_id）
  const ghAgain = await oauthLogin('github', { id: '7001', login: 'src-gh' });
  const muAfterGh = await call('/api/mu/session', { cookie: ghAgain.cookie });
  const giteeAgain = await oauthLogin('gitee', { id: '7012', login: 'src-gitee-2' });
  const muAfterGitee = await call('/api/mu/session', { cookie: giteeAgain.cookie });
  const legacyAfterGitee = await call('/api/auth/session', { cookie: giteeAgain.cookie });
  ok('SS4a 双身份用户：本次经 GitHub 登录 → canonical 标签 github-oauth',
    muAfterGh.json?.login_type === 'github-oauth', muAfterGh.json?.login_type);
  ok('SS4b 同一账号：本次经 Gitee 登录 → canonical 标签 gitee-oauth（随认证来源变化）',
    muAfterGitee.json?.login_type === 'gitee-oauth', muAfterGitee.json?.login_type);
  ok('SS4c 同一账号：legacy 桥同步 gitee-oauth',
    legacyAfterGitee.json?.user?.login_type === 'gitee-oauth', legacyAfterGitee.json?.user);
  ok('SS4d 两次登录解析同一 user_id（绑定生效，不重复建户）',
    muAfterGh.json?.user?.user_id === muAfterGitee.json?.user?.user_id,
    { gh: muAfterGh.json?.user?.user_id, gitee: muAfterGitee.json?.user?.user_id });

  // ── SS5 未知/缺失 provider 不被标为 GitHub ──
  const sessRow = await pool.query(
    `SELECT token_hash FROM mu.session WHERE revoked_at IS NULL ORDER BY created_at DESC LIMIT 1`);
  assert.ok(sessRow.rows[0]?.token_hash, 'SS5 setup: 找到活跃会话行');
  await pool.query(`UPDATE mu.session SET provider='future-oauth' WHERE token_hash=$1`, [sessRow.rows[0].token_hash]);
  const unkLegacy = await call('/api/auth/session', { cookie: giteeAgain.cookie });
  const unkMu = await call('/api/mu/session', { cookie: giteeAgain.cookie });
  ok('SS5a 未知 provider：legacy 桥不下发 login_type（不冒充 GitHub）',
    unkLegacy.status === 200 && unkLegacy.json?.user?.login_type === undefined,
    unkLegacy.json?.user);
  ok('SS5b 未知 provider：canonical 不下发 login_type/provider 字段',
    unkMu.status === 200 && unkMu.json?.login_type === undefined && unkMu.json?.provider === undefined,
    { login_type: unkMu.json?.login_type, provider: unkMu.json?.provider });
  ok('SS5c 未知 provider：会话本身仍有效（role/tenant 语义不受影响）',
    unkMu.json?.role === 'reviewer' && unkMu.json?.tenant?.slug === 'default' && unkLegacy.json?.role === 'reviewer');
  await pool.query(`UPDATE mu.session SET provider='gitee-oauth' WHERE token_hash=$1`, [sessRow.rows[0].token_hash]);

  // ── SS6 fixture 会话不被标为 GitHub（认证行为不变，仅来源标签诚实） ──
  const fixSess = await call('/api/auth/session', { cookie: admin.cookie });
  ok('SS6 fixture 会话：legacy 桥不下发 login_type（不冒充 GitHub；fixture 登录行为本身不变）',
    fixSess.status === 200 && fixSess.json?.user?.login_type === undefined, fixSess.json?.user);

  // ── SS7 409 invite_provider：可信后端信号（邀请不消耗） ──
  const invMisG = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { expected_subject: 'gitee-oauth:7999', role: 'contributor', ttl_minutes: 60 } });
  const misG = await fetch(BASE_ + `/api/mu/auth/oauth/github/start?invite=${invMisG.json?.invitation?.invite_id}`);
  const misGJson = await misG.json().catch(() => null);
  ok('SS7a GitHub 入口对 Gitee 邀请：409 + invite_provider=gitee-oauth',
    misG.status === 409 && misGJson?.error?.reason === 'invite_provider_mismatch'
      && misGJson?.error?.invite_provider === 'gitee-oauth', misGJson?.error);
  const invMisH = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { expected_subject: 'github-oauth:7998', role: 'contributor', ttl_minutes: 60 } });
  const misH = await fetch(BASE_ + `/api/mu/auth/oauth/gitee/start?invite=${invMisH.json?.invitation?.invite_id}`);
  const misHJson = await misH.json().catch(() => null);
  ok('SS7b Gitee 入口对 GitHub 邀请：409 + invite_provider=github-oauth（双向对称）',
    misH.status === 409 && misHJson?.error?.invite_provider === 'github-oauth', misHJson?.error);
  const misCount = await pool.query(`SELECT count(*)::int n FROM mu.invitation WHERE invite_id IN ($1,$2) AND claimed_at IS NULL`,
    [invMisG.json?.invitation?.invite_id, invMisH.json?.invitation?.invite_id]);
  ok('SS7c 错配 409 不消耗邀请（claimed_at 仍 NULL）', misCount.rows[0].n === 2, misCount.rows[0]);

  // ── SS8 审计语义不变：双平台 MU_LOGIN 均落库 ──
  const audits = await pool.query(
    `SELECT detail->>'provider' AS p, count(*)::int n FROM mu.audit_event
      WHERE kind='MU_LOGIN' AND detail->>'provider' IN ('github-oauth','gitee-oauth') GROUP BY 1 ORDER BY 1`);
  const byP = Object.fromEntries(audits.rows.map(r => [r.p, r.n]));
  ok('SS8 审计语义不变：github-oauth 与 gitee-oauth 的 MU_LOGIN 均落库',
    (byP['github-oauth'] ?? 0) >= 1 && (byP['gitee-oauth'] ?? 0) >= 1, byP);
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
console.log(`\nmu-session-source.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
