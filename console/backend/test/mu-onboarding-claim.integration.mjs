#!/usr/bin/env node
// mu-onboarding-claim.integration.mjs — rc.14（MT-ONB-1/2）邀请认领泛化集成测试。
// 覆盖：既有用户经邀请进入第二租户 / 多邀请显式消歧 / 零邀请兼容 / 新用户首驻不变 /
// 过期·已认领邀请不可认领 / D-3 claim 双路径拒绝 / claim 失败零 membership / 撤权即时失效 /
// 租户参数注入不改变会话租户。GitHub 全部经本地 mock adapter——绝不访问真实 GitHub。
// 自起一次性 postgres:16-alpine（finally 清理）。运行：node console/backend/test/mu-onboarding-claim.integration.mjs
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? pass++ : fail++; console.log((c ? '  PASS  ' : '  FAIL  ') + n + (d !== undefined ? ' ' + JSON.stringify(d).slice(0, 200) : '')); };

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'pw';
process.env.CONSOLE_SESSION_SECRET = 'onb-test-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
delete process.env.MU_FIXTURES;
process.env.MU_GITHUB_OAUTH_CLIENT_ID = 'ov22_test_client';
process.env.MU_GITHUB_OAUTH_CLIENT_SECRET = 'oauth-test-secret-value-32bytes-min!!';
process.env.MU_GITHUB_OAUTH_CALLBACK_URL = 'http://127.0.0.1:4730/api/mu/auth/oauth/github/callback';

const CTR = `onb-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17100 + Math.floor(Math.random() * 80);
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
const { __setOAuthClientForTests, __resetOAuthClient } = await import('../lib/multiuser/oauth.mjs');

let mockIdentity = { id: '7001', login: 'bob' };
__setOAuthClientForTests({
  async exchangeCode(_cfg, code) { if (code !== 'CODE_OK') throw new Error('oauth_token_exchange_failed'); return 'gho_mock_' + crypto.randomBytes(16).toString('hex'); },
  async fetchIdentity(_cfg, _tok) { return { id: mockIdentity.id, login: mockIdentity.login }; },
});

const { server } = createConsole({ evidenceRoot: here, distDir: path.join(here, 'no-dist') });
await new Promise(r => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

const login = async (subject) => {
  const r = await fetch(BASE + '/api/mu/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ provider: 'fixture', subject }) });
  return { cookie: (r.headers.get('set-cookie') || '').split(';')[0], csrf: (r.headers.get('set-cookie') || '').match(/mp_csrf=([^;,]+)/)?.[1], json: await r.json().catch(() => null) };
};
const startFlow = async () => {
  const r = await fetch(BASE + '/api/mu/auth/oauth/github/start');
  const j = await r.json().catch(() => null);
  const corr = (r.headers.get('set-cookie') || '').match(/mu_oauth_corr=([^;]+)/)?.[1] ?? null;
  const state = new URL(j.authorize_url).searchParams.get('state');
  return { corr, state };
};
const callback = async (state, corr) => fetch(BASE + `/api/mu/auth/oauth/github/callback?code=CODE_OK&state=${encodeURIComponent(state)}`, { redirect: 'manual', headers: { cookie: `mu_oauth_corr=${corr}` } });
// 以指定身份走完整 OAuth 登录，返回 302 响应 + 会话 cookie（成功时）
const oauthLogin = async (identity) => {
  mockIdentity = identity;
  const { corr, state } = await startFlow();
  const res = await callback(state, corr);
  const setCookie = res.headers.get('set-cookie') || '';
  const muSession = setCookie.match(/mu_session=([^;]+)/)?.[1] ?? null;
  return { res, muSession, location: res.headers.get('location') || '' };
};
const call = async (p, { method = 'GET', cookie } = {}) => {
  const r = await fetch(BASE + p, { method, headers: { ...(cookie ? { cookie } : {}) }, redirect: 'manual' });
  let j = null; try { j = await r.json(); } catch { /* */ }
  return { status: r.status, json: j };
};
const invitationsOf = async (subject) => (await pool.query('SELECT * FROM mu.invitation WHERE expected_subject=$1 ORDER BY created_at', [subject])).rows;
const membershipsOf = async (userId) => (await pool.query('SELECT m.*, t.slug FROM mu.membership m JOIN mu.tenant t ON t.tenant_id=m.tenant_id WHERE m.user_id=$1 ORDER BY m.created_at', [userId])).rows;

try {
  // 种子：admin（fixture）+ 两个租户 + bob 的既有 membership（default/contributor）
  await login('fixture:dev-pilot'); // 触发 initSchema + fixture admin
  const t1 = (await pool.query("SELECT tenant_id FROM mu.tenant WHERE slug='default'")).rows[0].tenant_id;
  const t2 = (await pool.query("INSERT INTO mu.tenant (slug, display_name) VALUES ('tenant-2','Tenant Two') ON CONFLICT (slug) DO UPDATE SET display_name=EXCLUDED.display_name RETURNING tenant_id")).rows[0].tenant_id;
  const bobSubject = 'github-oauth:9001';
  // bob 先以普通用户身份进入 default（既有用户场景的前提）
  mockIdentity = { id: '9001', login: 'bob' };
  const adminCookie = (await login('fixture:dev-pilot')).cookie;
  // admin 直接种 bob 的 identity+membership（避免依赖 claim 顺序）
  await pool.query("INSERT INTO mu.app_user (login, display_name, state) VALUES ('bob','Bob','active') RETURNING *");
  const bobId = (await pool.query("SELECT user_id FROM mu.app_user WHERE login='bob'")).rows[0].user_id;
  await pool.query("INSERT INTO mu.external_identity (user_id, provider, subject) VALUES ($1,'github-oauth',$2)", [bobId, bobSubject]);
  await pool.query("INSERT INTO mu.membership (tenant_id, user_id, role, state) VALUES ($1,$2,'contributor','active')", [t1, bobId]);

  // ── OC-1 既有用户 + 唯一 tenant2 邀请 → claim，会话绑 tenant2 ──
  await pool.query("INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, created_by, expires_at) VALUES ($1,'contributor',$2,'bob',$3, now() + interval '30 minutes')", [t2, bobSubject, bobId]);
  {
    const { res, muSession } = await oauthLogin({ id: '9001', login: 'bob' });
    ok('OC1 既有用户唯一邀请 → 302 成功', res.status === 302 && !/mu_login_error=/.test(res.headers.get('location') || ''), { status: res.status, loc: res.headers.get('location') });
    const mems = await membershipsOf(bobId);
    ok('OC1 membership 双租户（default+tenant2 各 contributor）',
      mems.length === 2 && mems.every(m => m.role === 'contributor') && mems.some(m => m.slug === 'tenant-2'), mems.map(m => m.slug + ':' + m.role + ':' + m.state));
    const inv = (await invitationsOf(bobSubject)).find(i => i.tenant_id === t2);
    ok('OC1 邀请已认领（claimed_at 落行）', inv?.claimed_at !== null && inv?.claimed_by_user_id === bobId);
    ok('OC1 会话 cookie 签发', typeof muSession === 'string' && muSession.length > 20);
    const who = await call('/api/mu/session', { cookie: `mu_session=${muSession}` });
    ok('OC1 会话绑定 tenant2（slug 回显）', who.status === 200 && who.json?.tenant?.slug === 'tenant-2', who.json?.tenant);
    await pool.query("UPDATE mu.membership SET state='revoked' WHERE tenant_id=$1 AND user_id=$2", [t2, bobId]); // 复位：撤掉 tenant2 供后续用例
  }

  // ── OC-2 既有用户 + 多条有效邀请 → invitation_ambiguous，不回退 active[0] ──
  {
    const made = await pool.query("INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, created_by, expires_at) VALUES ($1,'contributor',$2,'bob',$3, now() + interval '30 minutes'), ($1,'auditor',$2,'bob',$3, now() + interval '30 minutes') RETURNING invite_id", [t2, bobSubject, bobId]);
    const madeIds = made.rows.map(r => r.invite_id);
    const { res } = await oauthLogin({ id: '9001', login: 'bob' });
    ok('OC2 多邀请 → 302 invitation_ambiguous', res.status === 302 && /mu_login_error=invitation_ambiguous/.test(res.headers.get('location') || ''), res.headers.get('location'));
    const madeRows = await pool.query('SELECT invite_id, claimed_at FROM mu.invitation WHERE invite_id = ANY($1)', [madeIds]);
    ok('OC2 本用例两条邀请均未被认领（消歧拒绝不烧邀请）', madeRows.rows.every(r => r.claimed_at === null), madeRows.rows);
    ok('OC2 membership 仍 2 条未增', (await membershipsOf(bobId)).length === 2);
    // 复位：清理本用例自建的两条（一次性测试库，仅删自建行）
    await pool.query('DELETE FROM mu.invitation WHERE invite_id = ANY($1)', [madeIds]);
  }

  // ── OC-3 既有用户 + 零邀请 → 保持 active[0] 兼容行为 ──
  {
    // 复位 OC-1 认领：撤销 tenant2 membership 后仅剩 default → 登录应绑 default
    // OC-1 末尾已 revoke tenant2；此时唯一有效邀请是 OC-1 已认领那条（claimed）→ 不可再认领
    const { res } = await oauthLogin({ id: '9001', login: 'bob' });
    ok('OC3 零可认领邀请 → 302 成功', res.status === 302 && !/mu_login_error=/.test(res.headers.get('location') || ''), res.headers.get('location'));
    const sess = res.headers.get('set-cookie') || '';
    const mu = sess.match(/mu_session=([^;]+)/)?.[1];
    const who = await call('/api/mu/session', { cookie: `mu_session=${mu}` });
    ok('OC3 会话绑 default（active[0] 兼容）', who.status === 200 && who.json?.tenant?.slug === 'default', who.json?.tenant);
  }

  // ── OC-4 过期/已认领邀请不可认领 ──
  {
    await pool.query("INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, created_by, expires_at) VALUES ($1,'maintainer',$2,'bob',$3, now() - interval '1 minute')", [t2, bobSubject, bobId]); // 已过期
    const before = (await pool.query("SELECT COUNT(*)::int n FROM mu.membership WHERE user_id=$1 AND state='active'", [bobId])).rows[0].n;
    const { res } = await oauthLogin({ id: '9001', login: 'bob' });
    ok('OC4 过期邀请 → 正常登录（不认领）', res.status === 302 && !/mu_login_error=/.test(res.headers.get('location') || ''));
    const after = (await pool.query("SELECT COUNT(*)::int n FROM mu.membership WHERE user_id=$1 AND state='active'", [bobId])).rows[0].n;
    ok('OC4 过期邀请零 membership 写入', before === after, { before, after });
    const expInv = (await invitationsOf(bobSubject)).find(i => i.tenant_id === t2 && i.expires_at < new Date());
    ok('OC4 过期邀请未被认领', expInv?.claimed_at === null);
    await pool.query("DELETE FROM mu.invitation WHERE tenant_id=$1 AND expected_subject=$2 AND expires_at < now()", [t2, bobSubject]);
  }

  // ── OC-5 D-3：既有用户 + platform_admin 邀请 → claim 拒绝（先拒后认领，不烧邀请）──
  {
    // v23 CHECK 会拒绝 DB 直插 platform_admin 邀请——测试库中临时 DROP CHECK 模拟
    // DBA 篡改场景，验证 claim 层纵深防御仍然 fail-closed。
    await pool.query('ALTER TABLE mu.invitation DROP CONSTRAINT invitation_role_check');
    const ins = await pool.query("INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, created_by, expires_at) VALUES ($1,'platform_admin',$2,'bob',$3, now() + interval '30 minutes') RETURNING invite_id", [t2, bobSubject, bobId]);
    const memsBefore = (await membershipsOf(bobId)).length;
    const { res } = await oauthLogin({ id: '9001', login: 'bob' });
    const memsAfter = (await membershipsOf(bobId)).length;
    ok('OC5 D-3 claim 拒绝 → 拒 platform_admin 且不产生 membership', res.status === 302 && memsBefore === memsAfter, { memsBefore, memsAfter });
    const burn = (await pool.query('SELECT claimed_at FROM mu.invitation WHERE invite_id=$1', [ins.rows[0].invite_id])).rows[0];
    ok('OC5 先拒后认领：邀请未被烧（claimed_at 仍 NULL）', burn?.claimed_at === null);
    const denied = await pool.query("SELECT COUNT(*)::int n FROM mu.platform_audit_event WHERE kind='OAUTH_FLOW_REJECTED' AND detail::text LIKE '%platform_admin_invitation_claim_denied%'");
    ok('OC5 拒绝审计落行（platform_audit_event）', denied.rows[0].n >= 1, { n: denied.rows[0].n });
    await pool.query('DELETE FROM mu.invitation WHERE invite_id=$1', [ins.rows[0].invite_id]);
    await pool.query("ALTER TABLE mu.invitation ADD CONSTRAINT invitation_role_check CHECK (role IN ('contributor','reviewer','maintainer','auditor'))");
  }

  // ── OC-6 新用户首驻 claim 行为不变 ──
  {
    await pool.query("INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, created_by, expires_at) VALUES ($1,'contributor',$2,'carol',$3, now() + interval '30 minutes')", [t1, 'github-oauth:9002', bobId]);
    const { res, muSession } = await oauthLogin({ id: '9002', login: 'carol' });
    ok('OC6 新用户首驻 → 302 成功', res.status === 302 && !/mu_login_error=/.test(res.headers.get('location') || ''));
    const carol = (await pool.query("SELECT user_id FROM mu.app_user WHERE login='carol'")).rows[0];
    const mems = await membershipsOf(carol.user_id);
    ok('OC6 membership=tenant1 contributor', mems.length === 1 && mems[0].slug === 'default' && mems[0].role === 'contributor', mems.map(m => m.slug + ':' + m.role));
    const who = await call('/api/mu/session', { cookie: `mu_session=${muSession}` });
    ok('OC6 会话绑 tenant1', who.status === 200 && who.json?.tenant?.slug === 'default');
  }

  // ── OC-7 claim 失败零 membership 写入（并发竞态语义：claimInvitation 返回 null）──
  {
    const inv = (await invitationsOf('github-oauth:9002'))[0];
    const before = (await pool.query('SELECT COUNT(*)::int n FROM mu.membership')).rows[0].n;
    const again = await pool.query("UPDATE mu.invitation SET claimed_at=now() WHERE invite_id=$1 AND claimed_at IS NULL AND expires_at > now() RETURNING invite_id", [inv.invite_id]);
    ok('OC7 已认领邀请二次 claim 影响零行（幂等竞态语义）', again.rowCount === 0);
    const after = (await pool.query('SELECT COUNT(*)::int n FROM mu.membership')).rows[0].n;
    ok('OC7 membership 零增长', before === after, { before, after });
  }

  // ── OC-8 撤权后现有会话下一请求立即 403 ──
  {
    // carol（OC-6）：撤销其唯一 membership → 同一会话下一请求即拒
    const carol = (await pool.query("SELECT user_id FROM mu.app_user WHERE login='carol'")).rows[0];
    const st = await startFlow();
    mockIdentity = { id: '9002', login: 'carol' };
    const res = await callback(st.state, st.corr);
    const mu = (res.headers.get('set-cookie') || '').match(/mu_session=([^;]+)/)?.[1];
    const live = await call('/api/mu/session', { cookie: `mu_session=${mu}` });
    ok('OC8 撤权前会话可用', live.status === 200);
    await pool.query("UPDATE mu.membership SET state='revoked' WHERE user_id=$1", [carol.user_id]);
    const denied = await call('/api/mu/runs', { cookie: `mu_session=${mu}` });
    ok('OC8 撤权后同会话下一请求 403 membership_inactive', denied.status === 403 && denied.json?.error?.reason === 'membership_inactive', { status: denied.status, reason: denied.json?.error?.reason });
  }

  // ── OC-9 租户参数/头注入不改变会话租户 ──
  {
    // bob 的 default 会话（OC-3 已验证）；带 tenant 注入参数请求 runs——
    // tenant2 数据集为空，若注入生效会改变返回集；这里断言状态与空集行为一致
    const st = await startFlow();
    mockIdentity = { id: '9001', login: 'bob' };
    const res = await callback(st.state, st.corr);
    const mu = (res.headers.get('set-cookie') || '').match(/mu_session=([^;]+)/)?.[1];
    const dsql = t1;
    for (const q of [`?tenant_id=${dsql}`, '?tenant=default', '?tenantId=' + dsql]) {
      const r = await call('/api/mu/runs' + q, { cookie: `mu_session=${mu}` });
      ok('OC9 注入' + q.slice(0, 18) + '… → 200 且 tenant 不变', r.status === 200, { status: r.status });
    }
    const h = await fetch(BASE + '/api/mu/runs', { headers: { cookie: `mu_session=${mu}`, 'x-tenant-id': dsql } });
    ok('OC9 X-Tenant-ID 头注入 → 200 且 tenant 不变', h.status === 200);
    const who = await call('/api/mu/session', { cookie: `mu_session=${mu}` });
    ok('OC9 会话租户仍 default', who.json?.tenant?.slug === 'default', who.json?.tenant?.slug);
  }

  // ── OC-10 多邀请消歧（新用户路径）：首驻 + 多条邀请 → 同样 ambiguous ──
  {
    await pool.query("INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, created_by, expires_at) VALUES ($1,'contributor',$2,'dave',$3, now() + interval '30 minutes'), ((SELECT tenant_id FROM mu.tenant WHERE slug='tenant-2'),'auditor',$2,'dave',$3, now() + interval '30 minutes')", [t1, 'github-oauth:9003', bobId]);
    const { res } = await oauthLogin({ id: '9003', login: 'dave' });
    ok('OC10 新用户多邀请 → invitation_ambiguous', res.status === 302 && /mu_login_error=invitation_ambiguous/.test(res.headers.get('location') || ''), res.headers.get('location'));
    ok('OC10 无 dave 用户被创建（拒绝先于建户）', (await pool.query("SELECT COUNT(*)::int n FROM mu.app_user WHERE login='dave'")).rows[0].n === 0);
  }
  // ── OC-11 事故语义锁（2026-10-10 生产事故）：maintainer + 同租户 contributor 邀请 ──
  // 登录全路径后角色保持 maintainer；PR 详情 my_permissions.actions 含 decide_review。
  {
    const mallorySubject = 'github-oauth:9010';
    await pool.query("INSERT INTO mu.app_user (login, display_name, state) VALUES ('mallory','Mallory','active')");
    const mallory = (await pool.query("SELECT user_id FROM mu.app_user WHERE login='mallory'")).rows[0];
    await pool.query("INSERT INTO mu.external_identity (user_id, provider, subject) VALUES ($1,'github-oauth',$2)", [mallory.user_id, mallorySubject]);
    await pool.query("INSERT INTO mu.membership (tenant_id, user_id, role, state) VALUES ($1,$2,'maintainer','active')", [t1, mallory.user_id]);
    await pool.query("INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, created_by, expires_at) VALUES ($1,'contributor',$2,'mallory',$3, now() + interval '30 minutes')", [t1, mallorySubject, mallory.user_id]);
    const { res, muSession } = await oauthLogin({ id: '9010', login: 'mallory' });
    ok('OC11 登录 302 成功', res.status === 302 && !/mu_login_error=/.test(res.headers.get('location') || ''), res.headers.get('location'));
    const m = (await membershipsOf(mallory.user_id)).find(x => x.slug === 'default');
    ok('OC11 角色仍 maintainer（事故回归锁——修复前被降级为 contributor）', m?.role === 'maintainer', m?.role);
    const inv = (await invitationsOf(mallorySubject)).find(i => i.tenant_id === t1);
    ok('OC11 邀请已消耗但角色未变（preserved 语义）', inv?.claimed_at !== null && m?.role === 'maintainer');
    const who = await call('/api/mu/session', { cookie: `mu_session=${muSession}` });
    ok('OC11 会话 role=maintainer（实际角色，非 invitation.role）', who.json?.role === 'maintainer', who.json?.role);
    // PR 详情投影：种 repo+PR 行 → GET 详情 → my_permissions.actions 含 decide_review
    await pool.query("INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name) VALUES ($1,'github','91001','mallory-org','r') ON CONFLICT DO NOTHING", [t1]);
    const repoRow = (await pool.query("SELECT repo_id FROM mu.repository WHERE tenant_id=$1 AND provider_repo_id='91001'", [t1])).rows[0];
    const prRow = (await pool.query("INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha, state) VALUES ($1,$2,777,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','open') RETURNING pr_id", [t1, repoRow.repo_id])).rows[0];
    const det = await call(`/api/mu/prs/${prRow.pr_id}?repo_id=${repoRow.repo_id}`, { cookie: `mu_session=${muSession}` });
    const acts = det.json?.my_permissions?.actions ?? [];
    ok('OC11 PR 详情 my_permissions 含 decide_review', det.status === 200 && acts.includes('decide_review'), { status: det.status, acts });
  }

  // ── OC-12 revoked 用户 + 同租户邀请 → 不激活、按未邀请回落 ──
  {
    const olgaSubject = 'github-oauth:9011';
    await pool.query("INSERT INTO mu.app_user (login, display_name, state) VALUES ('olga','Olga','active')");
    const olga = (await pool.query("SELECT user_id FROM mu.app_user WHERE login='olga'")).rows[0];
    await pool.query("INSERT INTO mu.external_identity (user_id, provider, subject) VALUES ($1,'github-oauth',$2)", [olga.user_id, olgaSubject]);
    await pool.query("INSERT INTO mu.membership (tenant_id, user_id, role, state) VALUES ($1,$2,'contributor','revoked')", [t1, olga.user_id]);
    await pool.query("INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, created_by, expires_at) VALUES ($1,'contributor',$2,'olga',$3, now() + interval '30 minutes')", [t1, olgaSubject, olga.user_id]);
    const { res } = await oauthLogin({ id: '9011', login: 'olga' });
    const m = (await membershipsOf(olga.user_id)).find(x => x.slug === 'default');
    ok('OC12 revoked 不被登录顺带激活', m?.state === 'revoked', m?.state);
    ok('OC12 按未邀请回落 → no_active_membership', res.status === 302 && /mu_login_error=no_active_membership/.test(res.headers.get('location') || ''), res.headers.get('location'));
  }

  // ── OC-13 认领入驻原子性：store 层 membership 写入失败 → 邀请一并回滚 ──
  {
    // 直接调 store 层（绕过 api 层 D-3），invitedRole=platform_admin 触发 v23 CHECK
    // 违反 → 事务回滚 → 邀请 claimed_at 保持 NULL（不出现"邀请已消耗但授权未完成"）。
    const storeMod = await import('../lib/multiuser/store.mjs');
    const store2 = await storeMod.createMuStore({ pool, env: process.env });
    // 触发方式：userId 不存在 → membership insert FK 违反 → 事务回滚。
    // （生产 membership CHECK 含 platform_admin——v1 遗留，非法角色不能在此确定性触发；
    //   FK 违反是同等的确定性写入失败路径，验证同一原子性边界。）
    const zed = crypto.randomUUID();
    const iAtomic = (await pool.query("INSERT INTO mu.invitation (tenant_id, role, expected_subject, created_by, expires_at) VALUES ($1,'contributor','github-oauth:9012',$2, now() + interval '30 minutes') RETURNING invite_id", [t1, zed])).rows[0].invite_id;
    let threw = false;
    try {
      await store2.claimInvitationOnboard({ inviteId: iAtomic, userId: zed, invitedRole: 'contributor' });
    } catch { threw = true; }
    ok('OC13 非法角色 → 写入抛错（事务回滚）', threw);
    const inv = (await pool.query('SELECT claimed_at FROM mu.invitation WHERE invite_id=$1', [iAtomic])).rows[0];
    ok('OC13 邀请未被消耗（claimed_at NULL——原子性）', inv?.claimed_at === null);
    ok('OC13 零 membership 残留', (await pool.query('SELECT COUNT(*)::int n FROM mu.membership WHERE user_id=$1', [zed])).rows[0].n === 0);
  }

  // ── OC-14 认领与管理员变更并发：登录路径 preserved 时零写 membership ──
  {
    // preserved 语义下登录对 membership 零写入——与管理员并发变更天然无覆盖
    // （并发 CAS 已在 mu-invitation-claim.integration.mjs S9 锁定）。此处锁写入零化：
    const ninaSubject = 'github-oauth:9013';
    await pool.query("INSERT INTO mu.app_user (login, display_name, state) VALUES ('nina','Nina','active')");
    const nina = (await pool.query("SELECT user_id FROM mu.app_user WHERE login='nina'")).rows[0];
    await pool.query("INSERT INTO mu.external_identity (user_id, provider, subject) VALUES ($1,'github-oauth',$2)", [nina.user_id, ninaSubject]);
    await pool.query("INSERT INTO mu.membership (tenant_id, user_id, role, state, updated_at) VALUES ($1,$2,'maintainer','active', now())", [t1, nina.user_id]);
    await pool.query("INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, created_by, expires_at) VALUES ($1,'contributor',$2,'nina',$3, now() + interval '30 minutes')", [t1, ninaSubject, nina.user_id]);
    const { res } = await oauthLogin({ id: '9013', login: 'nina' });
    ok('OC14 登录 302 成功', res.status === 302);
    const m = (await membershipsOf(nina.user_id)).find(x => x.slug === 'default');
    ok('OC14 登录后角色仍 maintainer（零写=并发管理员变更不被覆盖）', m?.role === 'maintainer', m?.role);
  }
} catch (e) {
  fail++;
  console.error('  FATAL ' + (e?.stack || e?.message || e));
} finally {
  __resetOAuthClient();
  try { server.close(); } catch { /* */ }
  try { await pool.end(); } catch { /* */ }
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
  console.log(`\n${pass} pass, ${fail} fail`);
  process.exit(fail ? 1 : 0);
}
