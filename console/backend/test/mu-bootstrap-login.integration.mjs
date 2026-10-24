#!/usr/bin/env node
// console/backend/test/mu-bootstrap-login.integration.mjs — 首管 bootstrap 身份契约集成测试
// （#516 后续波，2026-10-24 契约复核）。
//
// 核心契约：OAuth 身份键 = (provider, subject)，subject 【保留完整 provider 前缀】——
// exchangeForIdentity 返回 'github-oauth:<id>'（oauth.mjs 实读），bootstrap 不得去前缀。
// 本测试走真实 HTTP 回调路径（真实 server + 真实 PG + 真实身份解析/库查询/会话签发），
// 仅外部 OAuth 服务经官方注入口 __setOAuthClientForTests / __setGiteeOAuthClientForTests mock。
// fixture 全程关闭（生产模式口径）；不访问真实 GitHub/Gitee，零真实凭据。
// 运行：node console/backend/test/mu-bootstrap-login.integration.mjs
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
const { __setOAuthClientForTests, __resetOAuthClient } = await import('../lib/multiuser/oauth.mjs');
const { __setGiteeOAuthClientForTests, __resetGiteeOAuthClient } = await import('../lib/multiuser/giteeoauth.mjs');
const { createMuStore } = await import('../lib/multiuser/store.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

const CTR = `mu-btlogin-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16780 + Math.floor(Math.random() * 60);
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
process.env.CONSOLE_SESSION_SECRET = 'mu-btlogin-it-secret';
// 生产模式口径：fixture 【不】设置（关闭）；LLM 等可选键一律不设
delete process.env.MU_ALLOW_FIXTURE_LOGIN;
process.env.MU_GITHUB_OAUTH_CLIENT_ID = 'btlogin_test_client';
process.env.MU_GITHUB_OAUTH_CLIENT_SECRET = 'btlogin-test-secret-value-32bytes-min!';
process.env.MU_GITHUB_OAUTH_CALLBACK_URL = 'http://127.0.0.1:4730/api/mu/auth/oauth/github/callback';
process.env.MU_GITEE_OAUTH_CLIENT_ID = 'btlogin_gitee_client';
process.env.MU_GITEE_OAUTH_CLIENT_SECRET = 'btlogin-gitee-secret-value-32bytes-min';
process.env.MU_GITEE_OAUTH_CALLBACK_URL = 'http://127.0.0.1:4730/api/mu/auth/oauth/gitee/callback';
// 首管身份：OAuth 前缀形态（与 GitHub mock 同 id；Gitee mock 用相同数字 id 做隔离对照）
process.env.MU_BOOTSTRAP_ADMIN_LOGIN = 'pilot-admin';
process.env.MU_BOOTSTRAP_ADMIN_SUBJECT = 'github-oauth:150000001';

// ── mock 外部 OAuth 服务（仅 HTTP 层；身份解析/库/会话全走真实代码）──
let mockGhIdentity = { id: '150000001', login: 'pilot-admin-gh' };
__setOAuthClientForTests({
  async exchangeCode(_cfg, code) {
    if (code !== 'CODE_OK') throw new Error('oauth_token_exchange_failed');
    return 'gho_mock_' + crypto.randomBytes(16).toString('hex');
  },
  async fetchIdentity(_cfg, _t) { return mockGhIdentity; },
});
let mockGiteeIdentity = { id: '150000001', login: 'pilot-admin-gte' }; // 相同数字 id=隔离对照
__setGiteeOAuthClientForTests({
  async exchangeCode(_cfg, code) {
    if (code !== 'GCODE_OK') throw new Error('oauth_token_exchange_failed');
    return 'gitee_mock_' + crypto.randomBytes(16).toString('hex');
  },
  async fetchIdentity(_cfg, _t) { return mockGiteeIdentity; },
});

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE_ = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

const startOauth = (provider, invite = null) => fetch(
  BASE_ + `/api/mu/auth/oauth/${provider}/start` + (invite ? `?invite=${invite}` : ''))
  .then(async (res) => ({
    status: res.status,
    json: await res.json().catch(() => null),
    corr: (res.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1] ?? null,
  }));
const callback = (provider, state, code, corr) => fetch(
  BASE_ + `/api/mu/auth/oauth/${provider}/callback?code=${code}&state=${encodeURIComponent(state)}`,
  { redirect: 'manual', headers: corr != null ? { cookie: `mu_oauth_corr=${corr}` } : {} });
const extractState = (authorizeUrl) => new URL(authorizeUrl).searchParams.get('state');
const setCookiesOf = (res) => (res.headers.getSetCookie?.() ?? [res.headers.get('set-cookie')].filter(Boolean))
  .map((c) => c.split(';')[0]);
const muSessionOf = (res) => setCookiesOf(res).find((c) => c.startsWith('mu_session=')) ?? null;

let mainError = null;
try {
  // 等 boot 期异步 schema init 就绪（/api/health mu_schema_ready=true 再直查库）
  {
    let ready = false;
    for (let i = 0; i < 60 && !ready; i++) {
      try {
        const h = await fetch(BASE_ + '/api/health').then((r) => r.json()).catch(() => null);
        ready = h?.mu_schema_ready === true;
      } catch { /* retry */ }
      if (!ready) await new Promise((r) => setTimeout(r, 1000));
    }
    if (!ready) throw new Error('schema init 未在 60s 内就绪');
  }

  const admin = await pool.query(
    `SELECT u.user_id, u.login, i.provider, i.subject FROM mu.app_user u
     JOIN mu.external_identity i ON i.user_id = u.user_id WHERE u.login = 'pilot-admin'`);
  const bootRow = admin.rows[0];
  ok('BL0 bootstrap 落行：admin=pilot-admin + (github-oauth, github-oauth:150000001) 带前缀身份',
    bootRow?.login === 'pilot-admin' && bootRow?.provider === 'github-oauth'
      && bootRow?.subject === 'github-oauth:150000001', admin.rows);
  ok('BL0b bootstrap 唯一用户（无第二身份/第二用户）', admin.rows.length === 1, admin.rows.length);

  // ── BL1 fixture 关闭（生产模式口径）──
  {
    const res = await fetch(BASE_ + '/api/mu/auth/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ provider: 'fixture', subject: 'fixture:pilot-admin' }),
    });
    const j = await res.json().catch(() => null);
    ok('BL1 fixture 登录 → 403 fixture_login_disabled（生产模式未设开关）',
      res.status === 403 && j?.error?.reason === 'fixture_login_disabled', { status: res.status, j });
  }

  // ── BL2 真实 HTTP 回调登录同一管理员（GitHub，mock 仅外部服务）──
  {
    const st = await startOauth('github');
    const res = await callback('github', extractState(st.json.authorize_url), 'CODE_OK', st.corr);
    const loc = res.headers.get('location') || '';
    const sess = muSessionOf(res);
    ok('BL2 回调 302 落 /multiuser 无错误码 + mu_session 签发',
      res.status === 302 && loc === '/multiuser' && typeof sess === 'string' && sess.length > 0,
      { status: res.status, loc, hasSession: Boolean(sess) });
    // 会话真身=bootstrap 管理员（经真实身份解析+库查询）：manage 审计面 200
    const who = await fetch(BASE_ + '/api/mu/audit', { headers: { cookie: sess } });
    ok('BL2b 会话持 read_audit 能力（=platform_admin 真身；未新建用户）',
      who.status === 200, { status: who.status });
    const users = (await pool.query(`SELECT count(*)::int n FROM mu.app_user WHERE login LIKE 'pilot-admin%'`)).rows[0].n;
    ok('BL2c 登录未新建用户（仍只有 bootstrap 管理员一行）', users === 1, users);
    const idrow = (await pool.query(
      `SELECT provider, subject FROM mu.external_identity WHERE user_id = $1`, [bootRow.user_id])).rows;
    ok('BL2d 身份行仍为 (github-oauth, github-oauth:150000001) 原值（回写未漂移）',
      idrow.length === 1 && idrow[0].provider === 'github-oauth' && idrow[0].subject === 'github-oauth:150000001', idrow);
  }

  // ── BL3 相同数字 id 跨 provider 互不混用（Gitee 150000001 ≠ GitHub 150000001）──
  {
    const st = await startOauth('gitee');
    const res = await callback('gitee', extractState(st.json.authorize_url), 'GCODE_OK', st.corr);
    const loc = res.headers.get('location') || '';
    ok('BL3 Gitee 同数字 id 回调 → not_invited（不认领 GitHub bootstrap 管理员）',
      res.status === 302 && /mu_login_error=not_invited/.test(loc), { status: res.status, loc });
    ok('BL3b 零 Gitee 身份行落到 bootstrap 管理员',
      (await pool.query(`SELECT count(*)::int n FROM mu.external_identity
         WHERE user_id = $1 AND provider = 'gitee-oauth'`, [bootRow.user_id])).rows[0].n === 0);
    ok('BL3c 零新建用户（跨 provider 隔离=不产生 ' + 'pilot-admin-gte' + ' 行）',
      (await pool.query(`SELECT count(*)::int n FROM mu.app_user WHERE login LIKE 'pilot-admin-gte%'`)).rows[0].n === 0);
  }

  // ── BL4 重跑 bootstrap 不覆盖已有角色/撤销状态 ──
  {
    const store = await createMuStore({ pool, env: process.env });
    const mem0 = (await pool.query(
      `SELECT membership_id, role, state FROM mu.membership WHERE user_id = $1`, [bootRow.user_id])).rows[0];
    await pool.query(`UPDATE mu.membership SET role = 'maintainer' WHERE membership_id = $1`, [mem0.membership_id]);
    await store.bootstrap();
    const mem1 = (await pool.query(
      `SELECT membership_id, role, state FROM mu.membership WHERE membership_id = $1`, [mem0.membership_id])).rows[0];
    ok('BL4 已改角色 maintainer 经重跑 bootstrap 保持（不覆盖角色）',
      mem1.role === 'maintainer' && mem1.membership_id === mem0.membership_id, mem1);
    await pool.query(`UPDATE mu.membership SET state = 'revoked' WHERE membership_id = $1`, [mem0.membership_id]);
    await store.bootstrap();
    const mem2 = (await pool.query(
      `SELECT role, state FROM mu.membership WHERE membership_id = $1`, [mem0.membership_id])).rows[0];
    ok('BL4b 已撤销 revoked 经重跑 bootstrap 不复活', mem2.state === 'revoked', mem2);
  }
} catch (e) {
  // 意外异常/初始化超时走这里——不得被 finally 的清理吞掉
  mainError = e;
} finally {
  // 清理通道：每项独立 try——单项清理失败既不得掩盖主流程原始失败，
  // 也不得把失败转成成功（退出码恒非零）
  const cleanupErrors = [];
  try { __resetOAuthClient(); } catch (e) { cleanupErrors.push(e); }
  try { __resetGiteeOAuthClient(); } catch (e) { cleanupErrors.push(e); }
  try { await new Promise((r) => server.close(() => r())); } catch (e) { cleanupErrors.push(e); }
  try { await pool.end(); } catch (e) { cleanupErrors.push(e); }
  Object.assign(process.env, savedEnv);
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch (e) { cleanupErrors.push(e); }

  console.log(`mu-bootstrap-login.integration: ${pass} pass / ${fail} fail`);
  if (mainError) console.error('MAIN-FAILURE: ' + (mainError && mainError.stack ? mainError.stack : String(mainError)));
  for (const e of cleanupErrors) console.error('CLEANUP-FAILURE: ' + (e && e.message ? e.message : String(e)));
  // process.exitCode（非 process.exit）：不吞待写输出；断言失败/意外异常/超时=1，
  // 仅清理失败=2（仍非零），全过=0
  process.exitCode = (mainError || fail > 0) ? 1 : (cleanupErrors.length ? 2 : 0);
}
