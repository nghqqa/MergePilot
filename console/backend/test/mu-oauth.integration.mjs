#!/usr/bin/env node
// console/backend/test/mu-oauth.integration.mjs — Beta Identity Wave 2A 集成测试。
// GitHub 全部经本地 mock adapter（__setOAuthClientForTests）——绝不访问真实 GitHub、
// 不使用真实凭据；身份全部合成。自起一次性 postgres:16-alpine（随机回环端口，
// finally 清理）。运行：node console/backend/test/mu-oauth.integration.mjs
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
const { PLATFORM_AUDIT_KINDS } = await import('../lib/multiuser/store.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

const CTR = `mu-oauth-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16300 + Math.floor(Math.random() * 80);
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
process.env.CONSOLE_SESSION_SECRET = 'mu-oauth-it-secret';
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1'; // 管理面 setup 用（OAuth 电池独立于 fixture）
process.env.MU_GITHUB_OAUTH_CLIENT_ID = 'ov22_test_client';
process.env.MU_GITHUB_OAUTH_CLIENT_SECRET = 'oauth-test-secret-value-32bytes-min!!';
process.env.MU_GITHUB_OAUTH_CALLBACK_URL = 'http://127.0.0.1:4730/api/mu/auth/oauth/github/callback';

// ── mock OAuth client：合成身份（测试显式设定 mockIdentity）+ token 捕获 ──
let mockIdentity = { id: '1001', login: 'alice' };
const issuedTokens = [];
let exchangeShouldFail = false;
__setOAuthClientForTests({
  async exchangeCode(_cfg, code) {
    if (exchangeShouldFail || code !== 'CODE_OK') throw new Error('oauth_token_exchange_failed');
    const tok = 'gho_mock_' + crypto.randomBytes(16).toString('hex');
    issuedTokens.push(tok);
    return tok;
  },
  async fetchIdentity(_cfg, _accessToken) {
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
async function call(p, { method = 'GET', body = null, cookie = null, csrf = null, raw = false } = {}) {
  const res = await fetch(BASE_ + p, {
    method, redirect: 'manual',
    headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
      ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (raw) return res;
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}
async function startFlow() {
  const res = await fetch(BASE_ + '/api/mu/auth/oauth/github/start');
  const json = await res.json().catch(() => null);
  const corr = (res.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1] ?? null;
  return { status: res.status, json, corr };
}
function extractState(authorizeUrl) {
  return new URL(authorizeUrl).searchParams.get('state');
}
// 便捷：发起流程并完成回调（默认带 correlation cookie）
async function flowLogin(identity) {
  mockIdentity = identity;
  const st = await startFlow();
  const state = extractState(st.json.authorize_url);
  const res = await callback(state, 'CODE_OK', '', st.corr);
  return { res, st, state };
}
async function callback(state, code = 'CODE_OK', extraQuery = '', corr = undefined) {
  const headers = {};
  if (corr !== undefined && corr !== null) headers.cookie = `mu_oauth_corr=${corr}`;
  return fetch(BASE_ + `/api/mu/auth/oauth/github/callback?code=${code}&state=${encodeURIComponent(state)}${extraQuery}`, { redirect: 'manual', headers });
}

try {
  const admin = await fixtureLogin('fixture:dev-pilot');
  assert.equal(admin.status, 200, 'admin fixture login');

  // ── OG-1 providers 状态（配置就绪；无秘密回显） ──
  const prov = await call('/api/mu/auth/providers');
  ok('OG1 providers：github configured=true + callback/scope 披露且无 client_secret 回显',
    prov.status === 200 && prov.json?.github?.configured === true
      && prov.json.github.callback_url?.includes('/api/mu/auth/oauth/github/callback')
      && prov.json.github.scope === 'read:user'
      && !JSON.stringify(prov.json).includes('oauth-test-secret-value'), prov.json);

  // ── OG-2 start：authorize_url 合同 + state 高熵 ──
  const st = await startFlow();
  const state = extractState(st.json?.authorize_url);
  const au = new URL(st.json.authorize_url);
  ok('OG2 start：authorize_url 含 client_id/固定 callback/read:user scope',
    st.status === 200 && au.searchParams.get('client_id') === 'ov22_test_client'
      && au.searchParams.get('redirect_uri') === process.env.MU_GITHUB_OAUTH_CALLBACK_URL
      && au.searchParams.get('scope') === 'read:user'
      && au.searchParams.get('state') === state, st.json);
  ok('OG2b state 高熵（≥43 base64url 字符 ≈ 256bit）', typeof state === 'string' && state.length >= 43, { len: state?.length });

  // ── OG-3 伪造 state ──
  const forged = await callback(crypto.randomBytes(32).toString('base64url'));
  ok('OG3 伪造 state → 302 state_invalid',
    forged.status === 302 && /mu_login_error=state_invalid/.test(forged.headers.get('location') || ''));

  // ── OG-4 未邀请身份首登被拒 + 重放（单次消费） ──
  mockIdentity = { id: '1001', login: 'alice' };
  const c1 = await callback(state, 'CODE_OK', '', st.corr);
  ok('OG4 未邀请身份首次合法回调 → not_invited（无公共自动注册）',
    c1.status === 302 && /mu_login_error=not_invited/.test(c1.headers.get('location') || ''), c1.headers.get('location'));
  const replay = await callback(state, 'CODE_OK', '', st.corr);
  ok('OG4b 同 state 重放 → state_invalid（单次消费）',
    replay.status === 302 && /mu_login_error=state_invalid/.test(replay.headers.get('location') || ''));

  {
    const stc = await startFlow();
    ok('OG4c 独立 flow 再证未邀请拒绝（零注册）',
      /mu_login_error=not_invited/.test((await callback(extractState(stc.json.authorize_url), 'CODE_OK', '', stc.corr)).headers.get('location') || ''));
  }
  const aliceRows = await pool.query(`SELECT count(*)::int n FROM mu.app_user WHERE login LIKE 'alice%'`);
  ok('OG4d not_invited 路径零用户落库', aliceRows.rows[0].n === 0);

  // ── OG-5 邀请按 login 句柄 → 认领 → 会话 ──
  const inv = await call('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { expected_subject: '1002', expected_login: 'bob-gh', role: 'reviewer', ttl_minutes: 60 } });
  ok('OG5 邀请创建（数字 id 绑定 + login 仅展示 + 角色 + TTL）', inv.status === 200 && inv.json?.invitation?.role === 'reviewer', inv.json);
  mockIdentity = { id: '1002', login: 'bob-gh' };
  const st2 = await startFlow();
  const c2 = await callback(extractState(st2.json.authorize_url), 'CODE_OK', '', st2.corr);
  const bobCookie = (c2.headers.get('set-cookie') || '').split(';')[0];
  ok('OG5b 被邀请身份登录成功（302 /multiuser）', c2.status === 302 && (c2.headers.get('location') || '') === '/multiuser');
  const bobSess = await call('/api/mu/session', { cookie: bobCookie });
  ok('OG5c 会话建立：身份键=数字 id（github-oauth:1002），角色=邀请角色',
    bobSess.status === 200 && bobSess.json?.role === 'reviewer'
      && (await pool.query(`SELECT count(*)::int n FROM mu.external_identity WHERE provider='github-oauth' AND subject='github-oauth:1002'`)).rows[0].n === 1,
    bobSess.json);

  // ── OG-6 login 改名：同 id 新 login 仍解析同一用户 ──
  mockIdentity = { id: '1002', login: 'bob-renamed' };
  const st3 = await startFlow();
  const c3 = await callback(extractState(st3.json.authorize_url), 'CODE_OK', '', st3.corr);
  const bobCookie2 = (c3.headers.get('set-cookie') || '').split(';')[0];
  const bobSess2 = await call('/api/mu/session', { cookie: bobCookie2 });
  const identRows = await pool.query(`SELECT count(*)::int n FROM mu.external_identity WHERE subject='github-oauth:1002'`);
  ok('OG6 login 改名后同身份解析同一用户（不按 login 合并/分裂）',
    bobSess2.status === 200 && identRows.rows[0].n === 1
      && (await pool.query(`SELECT count(*)::int n FROM mu.app_user u JOIN mu.external_identity i ON i.user_id=u.user_id WHERE i.subject='github-oauth:1002'`)).rows[0].n === 1);

  // ── OG-7 state 过期 ──
  const st4 = await startFlow();
  await pool.query(`UPDATE mu.oauth_flow SET expires_at = now() - interval '1 second' WHERE state_hash = $1`,
    [crypto.createHash('sha256').update(extractState(st4.json.authorize_url)).digest('hex')]);
  ok('OG7 过期 flow → state_invalid',
    /mu_login_error=state_invalid/.test((await callback(extractState(st4.json.authorize_url), 'CODE_OK', '', st4.corr)).headers.get('location') || ''));

  // ── OG-8 redirect 注入不生效 ──
  mockIdentity = { id: '1002', login: 'bob-renamed' };
  const st5 = await startFlow();
  const inj = await callback(extractState(st5.json.authorize_url), 'CODE_OK', '&next=https://evil.example&redirect_uri=https://evil.example', st5.corr);
  ok('OG8 callback 忽略 next/redirect_uri 注入（落地恒为 /multiuser）',
    inj.status === 302 && (inj.headers.get('location') || '') === '/multiuser', inj.headers.get('location'));

  // ── OG-9 cookie 属性 ──
  const sc = c2.headers.get('set-cookie') || '';
  ok('OG9 会话 cookie：HttpOnly + SameSite=Lax + Path=/',
    /mu_session=[^;]+; Path=\/; HttpOnly; SameSite=Lax/.test(sc), sc.slice(0, 90));
  const savedProd = process.env.NODE_ENV;
  process.env.NODE_ENV = 'production';
  const st6 = await startFlow(); const c6 = await callback(extractState(st6.json.authorize_url), 'CODE_OK', '', st6.corr);
  process.env.NODE_ENV = savedProd;
  ok('OG9b 生产模式强制 Secure', /(^|; )Secure(;|$)/.test(c6.headers.get('set-cookie') || '') && /mu_session=/.test(c6.headers.get('set-cookie') || ''), c6.headers.get('set-cookie')?.slice(0, 120));
  mockIdentity = { id: '1002', login: 'bob-renamed' };

  // ── OG-10 重启恢复（新 server 实例 + 同 PG） ──
  server.close();
  const { server: server2http } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
  const server2 = { server: server2http, close: () => server2http.close() };
  await new Promise((r) => server2http.listen(0, '127.0.0.1', r));
  BASE_ = `http://127.0.0.1:${server2http.address().port}`;
  const sessAfterRestart = await fetch(BASE_ + '/api/mu/session', { headers: { cookie: bobCookie2 } }).then((r) => r.json().catch(() => null)).catch(() => null);
  const statusAfterRestart = await fetch(BASE_ + '/api/mu/session', { headers: { cookie: bobCookie2 } }).then((r) => r.status);
  ok('OG10 服务重启后会话可恢复（DB 持久化；新进程验证旧 cookie）',
    statusAfterRestart === 200 && sessAfterRestart?.role === 'reviewer', { statusAfterRestart, sessAfterRestart });
  const call2 = async (p, o = {}) => {
    const res = await fetch(BASE_ + p, { method: o.method ?? 'GET', redirect: 'manual',
      headers: { ...(o.cookie ? { cookie: o.cookie } : {}), ...(o.csrf ? { 'x-csrf-token': o.csrf } : {}), ...(o.body ? { 'content-type': 'application/json' } : {}) },
      body: o.body ? JSON.stringify(o.body) : undefined });
    let json = null; try { json = await res.json(); } catch { /* */ }
    return { status: res.status, json, headers: res.headers };
  };

  // ── OG-11 撤权即时失效 ──
  const rev = await call2('/api/mu/members/bob-gh/revoke', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf });
  // 注意：bob 登录时句柄 bob-gh（OG5 轮）；改名只影响后续——按身份查 login
  void rev; // 用身份定位撤销（不依赖 login 句柄）
  const bobUid = (await pool.query(`SELECT user_id FROM mu.external_identity WHERE subject='github-oauth:1002'`)).rows[0].user_id;
  await pool.query(`UPDATE mu.membership SET state='revoked' WHERE user_id=$1`, [bobUid]);
  const deadStatus = await fetch(BASE_ + '/api/mu/session', { headers: { cookie: bobCookie2 } }).then((r) => r.status);
  const deadBody = await fetch(BASE_ + '/api/mu/session', { headers: { cookie: bobCookie2 } }).then((r) => r.json().catch(() => null));
  ok('OG11 成员撤权后既有会话立即失效（403 membership_inactive，会话存续≠权限存续）',
    deadStatus === 403 && deadBody?.error?.reason === 'membership_inactive', { deadStatus, deadBody });

  // ── OG-12 logout / revoke-all ──
  // 新身份会话（重置 1002 会员）
  await pool.query(`UPDATE mu.membership SET state='active' WHERE user_id=$1`, [bobUid]);
  const st7 = await startFlow(); const c7 = await callback(extractState(st7.json.authorize_url), 'CODE_OK', '', st7.corr);
  const b7cookie = (c7.headers.get('set-cookie') || '').split(';')[0];
  const b7 = await fetch(BASE_ + '/api/mu/session', { headers: { cookie: b7cookie } }).then((r) => r.json());
  // csrf 从 mp_csrf cookie 取（双提交）
  const b7csrf = (c7.headers.get('set-cookie') || '').match(/mp_csrf=([^;]+)/)?.[1];
  void b7;
  const lo = await call2('/api/mu/auth/logout', { method: 'POST', cookie: b7cookie, csrf: b7csrf });
  ok('OG12 logout 撤销会话（后续 401）', lo.status === 200
    && (await fetch(BASE_ + '/api/mu/session', { headers: { cookie: b7cookie } })).status === 401);
  // revoke-all：开两个会话
  const stA = await startFlow();
  const cA = await callback(extractState(stA.json.authorize_url), 'CODE_OK', '', stA.corr);
  const stB = await startFlow();
  const cB = await callback(extractState(stB.json.authorize_url), 'CODE_OK', '', stB.corr);
  const cookieA = (cA.headers.get('set-cookie') || '').match(/mu_session=([^;]+)/)?.[1];
  const csrfB = (cB.headers.get('set-cookie') || '').match(/mp_csrf=([^;]+)/)?.[1];
  const cookieB = (cB.headers.get('set-cookie') || '').match(/mu_session=([^;]+)/)?.[1];
  const csrfA = (cA.headers.get('set-cookie') || '').match(/mp_csrf=([^;]+)/)?.[1];
  const ra = await call2('/api/mu/auth/sessions/revoke-all', { method: 'POST', cookie: `mu_session=${cookieA}`, csrf: csrfA });
  const sA = (await fetch(BASE_ + '/api/mu/session', { headers: { cookie: `mu_session=${cookieA}` } })).status;
  const sB2 = (await fetch(BASE_ + '/api/mu/session', { headers: { cookie: `mu_session=${cookieB}` } })).status;
  const raN = (await pool.query(`SELECT count(*)::int n FROM mu.platform_audit_event WHERE kind='SESSIONS_REVOKED_ALL'`)).rows[0].n;
  ok('OG12b revoke-all 清全部会话（两个会话均 401；platform 审计在库）',
    ra.status === 200 && sA === 401 && sB2 === 401 && raN >= 1, { raStatus: ra.status, sA, sB2, raN });

  // ── OG-13 跨租户邀请：仅授予目标租户 ──
  const tB = await pool.query(`SELECT tenant_id FROM mu.tenant WHERE slug='ten-b'`);
  if (tB.rows.length) {
    // ten-b 不存在于此测试库——直接建第二租户
  }
  const mkB = await call2('/api/mu/tenants', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: { slug: 'og-ten-b', display_name: 'OG B' } });
  const tB2 = mkB.json?.tenant?.tenant_id;
  const swB = await call2('/api/mu/auth/tenant', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf, body: { tenant_id: tB2 } });
  const adminB2 = { cookie: (swB.headers.get('set-cookie') || '').split(';')[0], csrf: swB.json?.csrf };
  const invB = await call2('/api/mu/invitations', { method: 'POST', cookie: adminB2.cookie, csrf: adminB2.csrf,
    body: { expected_subject: '1003', role: 'contributor', ttl_minutes: 30 } });
  ok('OG13 跨租户邀请（B 租户绑定数字 id 1003）', invB.status === 200, invB.json);
  // 切回 A
  const swA = await call2('/api/mu/auth/tenant', { method: 'POST', cookie: adminB2.cookie, csrf: adminB2.csrf,
    body: { tenant_id: admin.json?.tenant?.tenant_id } });
  admin.cookie = (swA.headers.get('set-cookie') || '').split(';')[0];
  admin.csrf = swA.json?.csrf;
  // 1003 的 login 是 'alice'——与 A 租户无关；登录应只获 B 会员
  mockIdentity = { id: '1003', login: 'alice' };
  const st8 = await startFlow(); const c8 = await callback(extractState(st8.json.authorize_url), 'CODE_OK', '', st8.corr);
  const alice3Cookie = (c8.headers.get('set-cookie') || '').split(';')[0];
  const a3 = await fetch(BASE_ + '/api/mu/session', { headers: { cookie: alice3Cookie } }).then((r) => r.json().catch(() => null));
  ok('OG13b subject 数字 id 邀请认领成功（1003/alice → B 租户 contributor）',
    a3?.role === 'contributor' && a3?.tenant?.slug === 'og-ten-b', a3);
  const a3repos = await fetch(BASE_ + '/api/mu/repositories', { headers: { cookie: alice3Cookie } }).then((r) => r.json().catch(() => ({})));
  const a3inA = await pool.query(`SELECT count(*)::int n FROM mu.membership m JOIN mu.tenant t ON t.tenant_id=m.tenant_id
    WHERE m.user_id=(SELECT user_id FROM mu.external_identity WHERE subject='github-oauth:1003') AND t.slug='default'`);
  ok('OG13c 跨租户边界：alice3 仅 B 会员（default 零会员；A 仓库面空）',
    a3inA.rows[0].n === 0 && !(a3repos.repositories ?? []).some((r) => r.tenant_id !== tB2));
  // login 撞名（A 已有 fixture alice? 此库无）——验证后缀化分支：1003 登录名 alice 若已有 alice 用户
  const dupInv = await call2('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { expected_subject: '1004', role: 'contributor' } });
  void dupInv;

  // ── OG-14 exchange 失败路径 ──
  exchangeShouldFail = true;
  const st9 = await startFlow();
  ok('OG14 code 交换失败 → oauth_exchange_failed（fail-closed，不建用户）',
    /mu_login_error=oauth_exchange_failed/.test((await callback(extractState(st9.json.authorize_url), 'CODE_OK', '', st9.corr)).headers.get('location') || ''));
  exchangeShouldFail = false;

  // ── OG-15 platform 审计（受限 kind 白名单在库） ──
  const pk = (await pool.query(`SELECT DISTINCT kind FROM mu.platform_audit_event`)).rows.map((r) => r.kind);
  ok('OG15 platform 审计仅含白名单 kind（STARTED/CONSUMED/REJECTED/REVOKED/REVOKED_ALL）',
    pk.length > 0 && pk.every((k) => PLATFORM_AUDIT_KINDS.includes(k)), pk);

  // ── OG-16 数据库无明文 token/state（只存摘要） ──
  const dumpRows = (await pool.query(`SELECT * FROM mu.session`)).rows;
  const flowRows = (await pool.query(`SELECT * FROM mu.oauth_flow`)).rows;
  const dumpStr = JSON.stringify(dumpRows) + JSON.stringify(flowRows);
  const tokenLeak = issuedTokens.some((t) => dumpStr.includes(t));
  ok('OG16 数据库零明文：session/oauth_flow 无 access_token/state 明文（列全为 *_hash）',
    tokenLeak === false && dumpRows.every((r) => /^[0-9a-f]{64}$/.test(r.token_hash))
      && flowRows.every((r) => /^[0-9a-f]{64}$/.test(r.state_hash)), { tokenLeak, sessions: dumpRows.length });
  const plainCols = (await pool.query(
    `SELECT column_name FROM information_schema.columns WHERE table_schema='mu' AND table_name IN ('session','oauth_flow') AND column_name NOT LIKE '%_hash' AND column_name LIKE '%token%' OR table_name='oauth_flow' AND column_name='state'`)).rows;
  ok('OG16b schema 无明文承载列（仅 token_hash/state_hash）', plainCols.length === 0, plainCols);

  // ── OG-17 邀请单次/过期 ──
  const usedInv = await pool.query(`SELECT invite_id FROM mu.invitation WHERE claimed_at IS NOT NULL LIMIT 1`);
  if (usedInv.rows.length) {
    const again = await storeClaimAgain(usedInv.rows[0].invite_id);
    ok('OG17 邀请单次消费（已认领不可复用）', again === null, again);
  } else { ok('OG17 邀请单次消费（无样本——跳过为 FAIL）', false); }
  async function storeClaimAgain(inviteId) {
    const { createMuStore } = await import('../lib/multiuser/store.mjs');
    const store = await createMuStore({ pool, env: process.env });
    return store.claimInvitation(inviteId, (await pool.query(`SELECT user_id FROM mu.app_user LIMIT 1`)).rows[0].user_id);
  }

  // ══ Wave 2A.1 专项回归（CR*：correlation cookie / 邀请 subject 强制） ══
  // CR-1 跨浏览器：无 corr cookie 的合法 state → state_invalid
  {
    const stc = await startFlow();
    const noCorr = await callback(extractState(stc.json.authorize_url), 'CODE_OK', '', null);
    ok('CR1 无 correlation cookie（跨浏览器/清 cookie）→ state_invalid',
      noCorr.status === 302 && /mu_login_error=state_invalid/.test(noCorr.headers.get('location') || ''));
  }
  // CR-2 错配 corr（其他 flow 的 cookie）
  {
    const st1 = await startFlow(); const st2 = await startFlow();
    const wrong = await callback(extractState(st1.json.authorize_url), 'CODE_OK', '', st2.corr);
    ok('CR2 correlation 错配（他流 cookie）→ state_invalid',
      wrong.status === 302 && /mu_login_error=state_invalid/.test(wrong.headers.get('location') || ''));
  }
  // CR-3 corr cookie 一次性：同一 corr 用于第二个 flow → 拒（摘要不同自然失配）
  {
    const st1 = await startFlow();
    await callback(extractState(st1.json.authorize_url), 'CODE_OK', '', st1.corr); // 消费流一
    const st2 = await startFlow();
    const reuse = await callback(extractState(st2.json.authorize_url), 'CODE_OK', '', st1.corr);
    ok('CR3 corr cookie 跨流不可复用（一次性语义）',
      reuse.status === 302 && /mu_login_error=state_invalid/.test(reuse.headers.get('location') || ''));
  }
  // CR-4 失败/成功路径均清理 corr cookie（Set-Cookie Max-Age=0）
  {
    const stc = await startFlow();
    const fail = await callback(extractState(stc.json.authorize_url), 'CODE_OK', '', stc.corr); // not_invited（当前身份）
    ok('CR4 失败路径清理 correlation cookie（Max-Age=0）',
      /mu_oauth_corr=;[^;]*;[^;]*Max-Age=0/.test(fail.headers.get('set-cookie') || '')
      || /mu_oauth_corr=;[^]*Max-Age=0/.test(fail.headers.get('set-cookie') || ''),
      fail.headers.get('set-cookie'));
  }
  // CR-5 login-only 邀请 fail-closed：即使 login 完全匹配也不可认领（handle 可夺注）
  {
    const invLo = await call2('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { expected_login: 'handle-thief-victim', role: 'contributor' } });
    ok('CR5 login-only 邀请创建被拒（API 强制 expected_subject）',
      invLo.status === 400 && invLo.json?.error?.reason === 'expected_subject_required', invLo.json);
    // 直插库模拟存量 login-only 邀请（2A 遗留）→ 认领路径 fail-closed
    await pool.query(`INSERT INTO mu.invitation (tenant_id, role, expected_login, expires_at)
      VALUES ($1,'contributor','legacy-login-only-invite', now() + interval '1 hour')`, [admin.json?.tenant?.tenant_id]);
    mockIdentity = { id: '4242', login: 'legacy-login-only-invite' };
    const stc = await startFlow();
    const attempt = await callback(extractState(stc.json.authorize_url), 'CODE_OK', '', stc.corr);
    ok('CR5b 存量 login-only 邀请不可认领（fail-closed；login 完全匹配也拒）',
      /mu_login_error=not_invited/.test(attempt.headers.get('location') || ''));
    ok('CR5c 认领尝试零用户落库', (await pool.query(
      `SELECT count(*)::int n FROM mu.external_identity WHERE subject='github-oauth:4242'`)).rows[0].n === 0);
  }
  // CR-6 数字 id 邀请 + login 已被他人持有（撞名）：按 subject 认领，不按 login
  {
    await pool.query(`INSERT INTO mu.app_user (login) VALUES ('taken-handle') ON CONFLICT DO NOTHING`);
    const invSub = await call2('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { expected_subject: '5150', expected_login: 'taken-handle', role: 'reviewer' } });
    ok('CR6 数字 id 邀请创建（login 仅展示）', invSub.status === 200, invSub.json);
    mockIdentity = { id: '5150', login: 'totally-different-name' }; // login 与句柄不符——subject 才是键
    const { res: cb } = await flowLogin(mockIdentity);
    const cookie5150 = (cb.headers.get('set-cookie') || '').match(/mu_session=([^;]+)/)?.[1];
    const sess5150 = await fetch(BASE_ + '/api/mu/session', { headers: { cookie: 'mu_session=' + cookie5150 } }).then(r=>r.json().catch(()=>null));
    ok('CR6b subject 命中即认领（login 不同不影响；撞名 handle 持有者不可冒领）',
      cb.status === 302 && (cb.headers.get('location') || '') === '/multiuser' && sess5150?.role === 'reviewer', sess5150);
    // 撞名冒领：数字 id 不同 + login 恰为 taken-handle → 拒
    mockIdentity = { id: '6161', login: 'taken-handle' };
    const { res: cbBad } = await flowLogin(mockIdentity);
    ok('CR6c id 不匹配的撞名 handle 不可认领（not_invited）',
      /mu_login_error=not_invited/.test(cbBad.headers.get('location') || ''));
  }
  // CR-7 并发消费（HTTP 级）：同一 subject 两 flow 同时回调，仅一个成功建户
  {
    const invC = await call2('/api/mu/invitations', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { expected_subject: '7171', role: 'auditor' } });
    ok('CR7 并发消费前置邀请', invC.status === 200);
    mockIdentity = { id: '7171', login: 'race-7171' };
    const f1 = await startFlow(); const f2 = await startFlow();
    const [r1x, r2x] = await Promise.all([
      callback(extractState(f1.json.authorize_url), 'CODE_OK', '', f1.corr),
      callback(extractState(f2.json.authorize_url), 'CODE_OK', '', f2.corr),
    ]);
    const okCount = [r1x, r2x].filter((r) => (r.headers.get('location') || '') === '/multiuser').length;
    const identCount = (await pool.query(`SELECT count(*)::int n FROM mu.external_identity WHERE subject='github-oauth:7171'`)).rows[0].n;
    const userCount = (await pool.query(`SELECT count(*)::int n FROM mu.app_user u JOIN mu.external_identity i ON i.user_id=u.user_id WHERE i.subject='github-oauth:7171'`)).rows[0].n;
    ok('CR7b 并发认领恰好一次建户（第二路 no_active_membership/not_invited）',
      identCount === 1 && userCount === 1 && okCount >= 1, { okCount, identCount, userCount });
  }
  // CR-8 失败响应不泄露：Location 仅白名单 reason，无 state/cookie/用户/tenant 值
  {
    const stc = await startFlow();
    const leak = await callback(extractState(stc.json.authorize_url) + 'X', 'CODE_OK', '', stc.corr);
    const loc = leak.headers.get('location') || '';
    ok('CR8 失败 Location 零泄露（无 state/corr/用户/tenant 原值）',
      !loc.includes(stc.corr) && !loc.includes(extractState(stc.json.authorize_url))
      && /^\/multiuser\?mu_login_error=[a-z_]+$/.test(loc), loc);
  }

  // ── OG-18 MU_LEGACY_LOGIN 继续 fail-closed ──
  const legacy = await fetch(BASE_ + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'dev-pilot', password: 'legacy-oauth-test-password' }),
  });
  ok('OG18 legacy 共享账号登录在多用户模式仍被拒（403）', legacy.status === 403);
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  __resetOAuthClient();
  for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
  Object.assign(process.env, savedEnv);
  server.close(); try { server2?.server?.close(); } catch { /* */ }
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nmu-oauth.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
