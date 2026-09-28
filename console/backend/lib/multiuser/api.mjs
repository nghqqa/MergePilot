// console/backend/lib/multiuser/api.mjs — /api/mu/* HTTP 路由（Developer Edition 多用户面）。
//
// 合同（对齐设计报告 + Phase 0 复核不变式）：
//  * MU_MODE=multiuser 且 CONSOLE_PG_DSN 就绪才启用；否则如实 not_enabled（不伪装）；
//  * 身份：ExternalIdentity（provider+subject）→ user；正式多用户模式下 legacy 共享
//    账号登录被 session.mjs 拒绝（迁移例外 MU_LEGACY_LOGIN=1）；
//  * tenant_id 永远取自服务端会话；repo_id 一律经 tenant 收窄解析（跨 tenant 必 miss）；
//    请求体携带的 tenant_id 被忽略不信任；
//  * 动作判定唯一入口 authorize()（lib/multiuser/authz.mjs），默认拒绝；
//  * 变更方法（POST）一律 X-CSRF-Token（timing-safe）；
//  * 拒绝写 mu.audit_event（metadata only：action/reason/actor，无查询正文/凭据）；
//  * 本切片不实现自动 approve、不实现任何绕过 branch protection 的合并。
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { createMuStore } from './store.mjs';
import { authorize, roleActions, MU_ROLES } from './authz.mjs';
import { githubAppStatus,
  fixtureChangedExcerpt, fixtureRagSearch, fixtureReviewRun, fixtureRepairPush } from './provider.mjs';
import { safeEqual } from '../session.mjs';
import { oauthConfig, newState, buildAuthorizeUrl, exchangeForIdentity } from './oauth.mjs';
import { issueMuSession, resolveMuSession, muCsrfOk, rotateMuSession,
         muTokenFromCookieHeader, muClearCookies } from './session.mjs';

// pg 解析：容器/镜像内走标准 node_modules；CI/主机测试进程回退到 test/support
// 安装的 dev-only pg（与 cchain/wiring.mjs loadPg 同一惯例——CI runner 无根 junction）。
let muPgMod = undefined;
async function loadPg() {
  if (muPgMod !== undefined) return muPgMod;
  try { muPgMod = await import('pg'); return muPgMod; } catch { /* fall through */ }
  try {
    muPgMod = createRequire(new URL('../../test/support/noop.js', import.meta.url))('pg');
  } catch { muPgMod = null; }
  return muPgMod;
}

let muStorePromise = null;
function getMuStore(env) {
  if (!env.CONSOLE_PG_DSN) return null;
  if (!muStorePromise) {
    muStorePromise = (async () => {
      const pg = await loadPg();
      if (!pg) throw new Error('pg module unavailable');
      const pool = new pg.Pool({ connectionString: env.CONSOLE_PG_DSN, max: 4 });
      pool.on('error', () => { /* 连接错误由查询路径如实上报（S12 语义） */ });
      const store = await createMuStore({ pool, env });
      await store.initSchema();
      await store.bootstrap();
      return store;
    })().catch((e) => { muStorePromise = null; throw e; });
  }
  return muStorePromise;
}

export async function muApi(req, res, ctx) {
  const { p, q, sendJson, readJsonBody, requireSession } = ctx;
  const env = process.env;
  if (env.MU_MODE !== 'multiuser' || !env.CONSOLE_PG_DSN) {
    return sendJson(res, 200, { service_state: 'multiuser_not_enabled',
      note: 'MU_MODE != multiuser 或 CONSOLE_PG_DSN 未配置——多用户面未启用（如实不伪装）' });
  }
  let store;
  try { store = await getMuStore(env); }
  catch (e) {
    return sendJson(res, 503, { service_state: 'error', error_kind: 'mu_store_unavailable',
      error: String(e?.message || e).slice(0, 160) });
  }
  if (!store) return sendJson(res, 200, { service_state: 'multiuser_not_enabled' });

  const json = async () => readJsonBody(req) ?? {};

  // ── 登录（身份提供商；无密码/无 token——只认 ExternalIdentity）──
  if (p === '/api/mu/auth/login' && req.method === 'POST') {
    const body = await json();
    const provider = String(body.provider || 'fixture');
    const subject = String(body.subject || '');
    if (!subject) return sendJson(res, 400, { error: { reason: 'subject required' } });
    if (provider === 'fixture') {
      // PR248 复核 P1 修复：fixture 身份登录默认【关闭】——须显式 MU_ALLOW_FIXTURE_LOGIN=1
      // （fail-closed，对齐 FXV_REPO_ALLOWLIST/scope 门惯例；防止 multiuser 部署忘配时
      // 可猜测的 bootstrap subject（fixture:<pilot 操作员名>）被用于登录）
      if (env.MU_ALLOW_FIXTURE_LOGIN !== '1') {
        return sendJson(res, 403, { error: { reason: 'fixture_login_disabled',
          detail: 'fixture 身份提供商仅开发/测试用——需显式 MU_ALLOW_FIXTURE_LOGIN=1' } });
      }
      const user = await store.getUserByIdentity('fixture', subject);
      if (!user) return sendJson(res, 401, { error: { reason: 'identity_unknown',
        detail: '身份未由管理员预置（fail-closed，无自动注册）' } });
      const memberships = await store.listMembershipsOfUser(user.user_id);
      const active = memberships.filter((m) => m.state === 'active');
      let tenant = null; let membership = null;
      if (body.tenant_slug) {
        tenant = await store.getTenantBySlug(String(body.tenant_slug));
        membership = tenant ? active.find((m) => m.tenant_id === tenant.tenant_id) ?? null : null;
        if (!membership) return sendJson(res, 403, { error: { reason: 'not_a_member' } });
      } else {
        membership = active[0] ?? null;
        if (membership) tenant = await store.getTenant(membership.tenant_id);
      }
      if (!membership || !tenant) {
        return sendJson(res, 403, { error: { reason: 'no_active_membership' } });
      }
      const sess = await issueMuSession(store, { userId: user.user_id,
        tenantId: tenant.tenant_id, login: user.login, role: membership.role, provider: 'fixture' });
      res.setHeader('Set-Cookie', sess.setCookie);
      await store.audit('MU_LOGIN', { tenantId: tenant.tenant_id, actorUserId: user.user_id,
        detail: { provider: 'fixture', subject_prefix: subject.slice(0, 12) } });
      return sendJson(res, 200, { ok: true, csrf: sess.csrf,
        user: { user_id: user.user_id, login: user.login },
        tenant: { tenant_id: tenant.tenant_id, slug: tenant.slug, display_name: tenant.display_name },
        role: membership.role });
    }
    if (provider === 'github') {
      // Wave 2A：GitHub 登录走 authorization-code 浏览器流程——入口为
      // GET /api/mu/auth/oauth/github/start（本 JSON 端点不再直连）
      return sendJson(res, 409, { error: { reason: 'use_oauth_flow',
        detail: 'GitHub 登录请走 GET /api/mu/auth/oauth/github/start（authorization-code 流程）' } });
    }
    return sendJson(res, 400, { error: { reason: 'unknown_provider' } });
  }

  // ── GitHub OAuth（Wave 2A：真实 authorization-code 流程；免会话——流程起点/回跳） ──
  const LOGIN_ERROR_WHITELIST = new Set([
    'not_invited', 'no_active_membership', 'state_invalid', 'state_expired',
    'oauth_exchange_failed', 'oauth_identity_invalid', 'oauth_not_configured', 'user_disabled',
  ]);
  const redirectLoginError = (reason) => {
    const r = LOGIN_ERROR_WHITELIST.has(reason) ? reason : 'login_failed';
    res.writeHead(302, { Location: `/multiuser?mu_login_error=${r}` });
    res.end();
  };

  // 身份提供商状态（无秘密；配置缺失如实 configured:false）
  if (p === '/api/mu/auth/providers' && req.method === 'GET') {
    const cfg = oauthConfig(env);
    return sendJson(res, 200, {
      github: { configured: cfg.configured, ...(cfg.configured ? {} : { reason: cfg.reason }),
        callback_url: cfg.callbackUrl || null, scope: cfg.scope },
      fixture: { configured: env.MU_ALLOW_FIXTURE_LOGIN === '1' },
    });
  }

  if (p === '/api/mu/auth/oauth/github/start' && req.method === 'GET') {
    const cfg = oauthConfig(env);
    if (!cfg.configured) {
      return sendJson(res, 503, { error: { reason: 'oauth_not_configured',
        detail: '需 MU_GITHUB_OAUTH_CLIENT_ID/_CLIENT_SECRET/_CALLBACK_URL 三项显式配置（fail-closed）' } });
    }
    let inviteId = null;
    if (q.invite) {
      const inv = await store.getInvitation(String(q.invite));
      if (!inv || inv.claimed_at || inv.expires_at <= new Date()) {
        return sendJson(res, 404, { error: { reason: 'invitation_not_found' } });
      }
      inviteId = inv.invite_id;
    }
    const state = newState();
    await store.insertOAuthFlow({
      stateHash: crypto.createHash('sha256').update(state).digest('hex'),
      inviteId,
      ttlMs: Number(env.MU_OAUTH_FLOW_TTL_MS || 10 * 60_000),
    });
    await store.auditPlatform('OAUTH_FLOW_STARTED', { detail: { invite_bound: Boolean(inviteId) } });
    return sendJson(res, 200, { authorize_url: buildAuthorizeUrl(cfg, state) });
  }

  if (p === '/api/mu/auth/oauth/github/callback' && req.method === 'GET') {
    const cfg = oauthConfig(env);
    if (!cfg.configured) return redirectLoginError('oauth_not_configured');
    const state = String(q.state ?? '');
    const code = String(q.code ?? '');
    const stateHash = state ? crypto.createHash('sha256').update(state).digest('hex') : '';
    const flow = stateHash ? await store.consumeOAuthFlow(stateHash) : null;
    if (!flow) {
      await store.auditPlatform('OAUTH_FLOW_REJECTED', { detail: { reason: 'state_invalid' } });
      return redirectLoginError('state_invalid'); // 不存在/已消费(重放)/已过期 统一同因
    }
    let identity;
    try {
      if (!code) throw new Error('missing_code');
      identity = await exchangeForIdentity(cfg, code); // token 即弃
    } catch (e) {
      await store.auditPlatform('OAUTH_FLOW_REJECTED', { detail: { reason: 'oauth_exchange_failed' } });
      return redirectLoginError('oauth_exchange_failed');
    }
    // 身份解析：既有用户（身份键=数字 id，login 改名不影响）或邀请认领（唯一注册通道）
    let user = await store.getUserByIdentity('github-oauth', identity.subject);
    let grantedTenantId = null; let grantedRole = null;
    if (!user) {
      const inv = flow.invite_id ? await store.getInvitation(flow.invite_id) : null;
      const usable = inv && !inv.claimed_at && inv.expires_at > new Date()
        && (inv.expected_subject === identity.subject || inv.expected_login === identity.login) ? inv : null;
      const claim = usable ?? await store.findClaimableInvitation({ subject: identity.subject, login: identity.login });
      if (!claim) {
        await store.auditPlatform('OAUTH_FLOW_REJECTED', { detail: { reason: 'not_invited' } });
        return redirectLoginError('not_invited'); // 无公共自动注册
      }
      // 不按 login 合并：login 撞名时后缀化（身份绑定只认 subject）
      const existingByLogin = await store.getUserByLogin(identity.login);
      const newLogin = existingByLogin ? `${identity.login}#gh${identity.subject.split(':').pop()}` : identity.login;
      user = await store.ensureUser({ login: newLogin, displayName: identity.login });
      await store.ensureIdentity({ userId: user.user_id, provider: 'github-oauth', subject: identity.subject });
      const claimed = await store.claimInvitation(claim.invite_id, user.user_id);
      if (!claimed) { // 并发认领竞态失败——按未邀请处理
        await store.auditPlatform('OAUTH_FLOW_REJECTED', { detail: { reason: 'not_invited' } });
        return redirectLoginError('not_invited');
      }
      await store.ensureMembership({ tenantId: claim.tenant_id, userId: user.user_id, role: claim.role });
      await store.audit('MU_MEMBER_ONBOARDED', { tenantId: claim.tenant_id, actorUserId: user.user_id,
        detail: { via: 'invitation', invite_id: claim.invite_id, role: claim.role } });
      grantedTenantId = claim.tenant_id; grantedRole = claim.role;
    }
    const memberships = await store.listMembershipsOfUser(user.user_id);
    const active = memberships.filter((m) => m.state === 'active');
    let tenantId = grantedTenantId;
    if (!tenantId) tenantId = active[0]?.tenant_id ?? null;
    const membership = grantedTenantId ? { role: grantedRole } : active[0] ?? null;
    if (!tenantId || !membership) {
      await store.auditPlatform('OAUTH_FLOW_REJECTED', { detail: { reason: 'no_active_membership' } });
      return redirectLoginError('no_active_membership');
    }
    const sess = await issueMuSession(store, { userId: user.user_id, tenantId,
      login: user.login, role: membership.role, provider: 'github-oauth' });
    res.setHeader('Set-Cookie', sess.setCookie);
    await store.auditPlatform('OAUTH_FLOW_CONSUMED', { actorUserId: user.user_id,
      detail: { flow_id: flow.flow_id } });
    await store.audit('MU_LOGIN', { tenantId, actorUserId: user.user_id,
      detail: { provider: 'github-oauth', subject_prefix: identity.subject.split(':').pop().slice(0, 8) } });
    res.writeHead(302, { Location: '/multiuser' }); // 固定落地，不采纳任何请求参数
    return res.end();
  }

  // ── 以下全部需要多用户会话（Wave 2A 起为 DB 持久会话：重启可恢复、撤销即时生效） ──
  const muSession = await resolveMuSession(store, req);
  if (!muSession) return sendJson(res, 401, { error: { reason: 'unauthorized' } });
  const mu = { userId: muSession.user_id, tenantId: muSession.tenant_id,
    login: muSession.login, sessionId: muSession.session_id };

  // 会话快照的 tenant 可能已被撤销/变更——每次请求现查 live membership
  const liveMembership = await store.getMembership(mu.tenantId, mu.userId);
  if (!liveMembership || liveMembership.state !== 'active') {
    return sendJson(res, 403, { error: { reason: 'membership_inactive',
      detail: '会话绑定的成员关系已失效——重新登录/切换 tenant' } });
  }

  const csrfOk = () => muCsrfOk(muSession, req);

  // 统一授权 guard：动作判定 + tenant 收窄 repo 解析 + Binding 要求（默认拒绝）
  async function guard(action, { repoId = null, needBinding = false } = {}) {
    const membership = await store.getMembership(mu.tenantId, mu.userId);
    let repo = null; let binding = null;
    if (repoId) {
      repo = await store.resolveRepository(mu.tenantId, String(repoId));
      if (!repo) {
        await store.audit('MU_AUTH_DENIED', { tenantId: mu.tenantId, actorUserId: mu.userId,
          detail: { action, reason: 'repository_not_found' } }).catch(() => {});
        return { denied: { status: 404, body: { error: { reason: 'repository_not_found' } } } };
      }
    }
    if (needBinding && repo) binding = await store.getBindingForRepo(mu.tenantId, repo.repo_id);
    const decision = authorize({ membership, action, binding });
    if (!decision.ok) {
      await store.audit('MU_AUTH_DENIED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { action, reason: decision.reason } }).catch(() => {});
      return { denied: { status: 403, body: { error: { reason: decision.reason }, action } } };
    }
    return { membership, repo, binding, decision };
  }

  try {
    // ── 会话生命周期（Wave 2A） ──
    if (p === '/api/mu/auth/logout' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const token = muTokenFromCookieHeader(req.headers.cookie);
      const revoked = await store.revokeSessionByToken(token, 'logout');
      await store.auditPlatform('SESSION_REVOKED', { actorUserId: mu.userId, detail: { revoked } });
      res.setHeader('Set-Cookie', muClearCookies());
      return sendJson(res, 200, { ok: true, revoked });
    }
    if (p === '/api/mu/auth/sessions/revoke-all' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const n = await store.revokeAllSessionsForUser(mu.userId, 'revoke_all');
      await store.auditPlatform('SESSIONS_REVOKED_ALL', { actorUserId: mu.userId, detail: { count: n } });
      res.setHeader('Set-Cookie', muClearCookies());
      return sendJson(res, 200, { ok: true, revoked: n });
    }

    // ── 邀请（manage_membership；唯一 onboarding 通道——短期/单次/摘要存储） ──
    if (p === '/api/mu/invitations' && req.method === 'GET') {
      const g = await guard('manage_membership');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      return sendJson(res, 200, { invitations: await store.listInvitations(mu.tenantId) });
    }
    if (p === '/api/mu/invitations' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_membership');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const role = String(body.role || '');
      if (!MU_ROLES.includes(role)) {
        return sendJson(res, 400, { error: { reason: 'role(五角色词汇) required' } });
      }
      let expectedSubject = null;
      if (body.expected_subject) {
        const digits = String(body.expected_subject).replace(/^github-oauth:/, '');
        if (!/^\d{1,20}$/.test(digits)) {
          return sendJson(res, 400, { error: { reason: 'expected_subject 须为 GitHub 数字 id（或 github-oauth:<id>）' } });
        }
        expectedSubject = `github-oauth:${digits}`;
      }
      const expectedLogin = body.expected_login ? String(body.expected_login) : null;
      if (!expectedSubject && !expectedLogin) {
        return sendJson(res, 400, { error: { reason: 'expected_subject 或 expected_login 至少一项' } });
      }
      const inv = await store.createInvitation({ tenantId: mu.tenantId, role,
        expectedSubject, expectedLogin, note: body.note ? String(body.note).slice(0, 200) : null,
        createdBy: mu.userId, ttlMs: Math.min(Number(body.ttl_minutes || 1440), 1440) * 60_000 });
      await store.audit('MU_INVITATION_CREATED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { invite_id: inv.invite_id, role,
          bind: expectedSubject ? 'subject' : 'login', ttl_minutes: Math.min(Number(body.ttl_minutes || 1440), 1440) } });
      return sendJson(res, 200, { ok: true, invitation: {
        invite_id: inv.invite_id, role: inv.role, expires_at: inv.expires_at,
        expected: expectedSubject ?? expectedLogin } });
    }

    // ── 会话摘要（前端权限态唯一来源；按钮只反映权限，授权以后端为准） ──
    if (p === '/api/mu/session' && req.method === 'GET') {
      const tenant = await store.getTenant(mu.tenantId);
      const memberships = await store.listMembershipsOfUser(mu.userId);
      return sendJson(res, 200, {
        user: { user_id: mu.userId, login: mu.login },
        tenant: tenant ? { tenant_id: tenant.tenant_id, slug: tenant.slug, display_name: tenant.display_name,
          is_migration_tenant: tenant.is_migration_tenant } : null,
        role: liveMembership.role,
        actions: roleActions(liveMembership.role) ?? [],
        memberships: memberships.map((m) => ({ tenant_id: m.tenant_id, tenant_slug: m.tenant_slug,
          role: m.role, state: m.state })),
      });
    }

    // ── tenant 切换（须为目标 tenant 的 active 成员） ──
    if (p === '/api/mu/auth/tenant' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const body = await json();
      const tenantId = String(body.tenant_id || '');
      const target = await store.getTenant(tenantId);
      const m = target ? await store.getMembership(tenantId, mu.userId) : null;
      if (!target || !m || m.state !== 'active') {
        return sendJson(res, 403, { error: { reason: 'not_a_member' } });
      }
      const sess = await rotateMuSession(store, mu.sessionId, { tenantId, role: m.role });
      if (!sess) return sendJson(res, 401, { error: { reason: 'unauthorized' } });
      res.setHeader('Set-Cookie', sess.setCookie);
      await store.audit('MU_TENANT_SWITCHED', { tenantId, actorUserId: mu.userId, detail: {} });
      return sendJson(res, 200, { ok: true, csrf: sess.csrf, role: m.role });
    }

    // ── tenant 创建（实例级；创建者自动成为新 tenant 的 platform_admin 成员） ──
    if (p === '/api/mu/tenants' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const slug = String(body.slug || '').trim();
      if (!/^[a-z0-9][a-z0-9-]{1,38}$/.test(slug)) {
        return sendJson(res, 400, { error: { reason: 'invalid_slug' } });
      }
      const tenant = await store.ensureTenant({ slug, displayName: String(body.display_name || slug) });
      await store.ensureMembership({ tenantId: tenant.tenant_id, userId: mu.userId,
        role: 'platform_admin', grantedBy: mu.userId });
      await store.audit('MU_TENANT_CREATED', { tenantId: tenant.tenant_id, actorUserId: mu.userId,
        detail: { slug } });
      return sendJson(res, 200, { ok: true, tenant: { tenant_id: tenant.tenant_id, slug: tenant.slug } });
    }

    // ── 成员（读=任意 active 成员；写=manage_membership） ──
    if (p === '/api/mu/members' && req.method === 'GET') {
      const rows = await store.listMembers(mu.tenantId);
      return sendJson(res, 200, { members: rows });
    }
    if (p === '/api/mu/members' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_membership');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const login = String(body.login || '').trim();
      const role = String(body.role || '');
      if (!login || !MU_ROLES.includes(role)) {
        return sendJson(res, 400, { error: { reason: 'login + role(五角色词汇) required' } });
      }
      const subject = String(body.fixture_subject || `fixture:${login}`);
      const existingByIdentity = await store.getUserByIdentity('fixture', subject);
      let target = await store.getUserByLogin(login);
      if (existingByIdentity && (!target || existingByIdentity.user_id !== target.user_id)) {
        return sendJson(res, 409, { error: { reason: 'identity_already_bound' } });
      }
      if (!target) target = await store.ensureUser({ login, displayName: String(body.display_name || login) });
      await store.ensureIdentity({ userId: target.user_id, provider: 'fixture', subject });
      const membership = await store.ensureMembership({ tenantId: mu.tenantId,
        userId: target.user_id, role, grantedBy: mu.userId });
      await store.audit('MU_MEMBER_ADDED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { target_login: login, role } });
      return sendJson(res, 200, { ok: true, membership: { user_id: target.user_id, login, role: membership.role } });
    }
    const revokeMatch = p.match(/^\/api\/mu\/members\/([^/]+)\/revoke$/);
    if (revokeMatch && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_membership');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const target = await store.getUserByLogin(decodeURIComponent(revokeMatch[1]));
      if (!target) return sendJson(res, 404, { error: { reason: 'member_not_found' } });
      if (target.user_id === mu.userId) {
        return sendJson(res, 400, { error: { reason: 'cannot_revoke_self' } });
      }
      const revoked = await store.revokeMembership(mu.tenantId, target.user_id);
      if (!revoked) {
        // Beta Hardening W1：login 全局不存在 / 存在但非本 tenant 成员 / 已撤销——
        // 三种情形统一同形 404（消除跨租户成员存在性侧信道；此前非成员返回 200
        // revoked:false 构成差分）。合法管理员语义保持：真实成员撤销仍 200+审计。
        return sendJson(res, 404, { error: { reason: 'member_not_found' } });
      }
      await store.audit('MU_MEMBER_REVOKED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { target_login: target.login } });
      return sendJson(res, 200, { ok: true, revoked: true });
    }

    // ── 仓库与绑定 ──
    if (p === '/api/mu/repositories' && req.method === 'GET') {
      const g = await guard('read_repository');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rows = await store.listRepositories(mu.tenantId);
      return sendJson(res, 200, { repositories: rows });
    }
    if (p === '/api/mu/repositories' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_repository_binding');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const providerRepoId = String(body.provider_repo_id || '').trim();
      const owner = String(body.owner || '').trim();
      const name = String(body.name || '').trim();
      const kind = ['fixture', 'github_app_installation'].includes(body.kind) ? body.kind : 'fixture';
      if (!providerRepoId || !owner || !name) {
        return sendJson(res, 400, { error: { reason: 'provider_repo_id/owner/name required' } });
      }
      const repo = await store.ensureRepository({ tenantId: mu.tenantId, provider: 'github',
        providerRepoId, owner, name, defaultBranch: body.default_branch ? String(body.default_branch) : null });
      // fixture 安装：合成 installation id（真实 GitHub App 接入为 PR 未完成项）
      const installationId = body.installation_id ? String(body.installation_id)
        : `fixture-install-${crypto.randomBytes(6).toString('hex')}`;
      const grantedScopes = Array.isArray(body.granted_scopes) && body.granted_scopes.length
        ? body.granted_scopes.map(String)
        : ['pull_requests:read', 'contents:read']; // 最小权限默认快照（设计报告 §6）
      const binding = await store.ensureBinding({ tenantId: mu.tenantId, repoId: repo.repo_id,
        kind, installationId, grantedScopes, createdBy: mu.userId });
      await store.audit('MU_REPO_BOUND', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { repo: `${owner}/${name}`, kind, installation_prefix: String(installationId).slice(0, 12),
          scopes_count: grantedScopes.length } });
      return sendJson(res, 200, { ok: true, repository: repo, binding });
    }
    const bindRevoke = p.match(/^\/api\/mu\/repositories\/([^/]+)\/binding\/revoke$/);
    if (bindRevoke && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_repository_binding', { repoId: bindRevoke[1] });
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const revoked = await store.revokeBinding(mu.tenantId, g.repo.repo_id);
      await store.audit('MU_BINDING_REVOKED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { repo_id: g.repo.repo_id } });
      return sendJson(res, 200, { ok: true, revoked: Boolean(revoked) });
    }

    // ── PR 快照播种（fixture 端点：仅 MU_FIXTURES 开启时可用——dev/test 数据面，
    //    非产品契约；真实 PR 数据来自 GitHub App webhook，为未完成项） ──
    if (p === '/api/mu/fixtures/pr' && req.method === 'POST') {
      // PR248 复核 P1 修复：fixture 播种端点默认【关闭】——须显式 MU_FIXTURES=1
      if (env.MU_FIXTURES !== '1') return sendJson(res, 403, { error: { reason: 'fixtures_disabled' } });
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const body = await json();
      const g = await guard('read_pull_request', { repoId: body.repo_id });
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const number = Number(body.number);
      const headSha = String(body.head_sha || '');
      const bps = ['unknown', 'known_clean', 'blocked'].includes(body.branch_protection_status)
        ? body.branch_protection_status : 'unknown';
      if (!Number.isInteger(number) || number < 1 || !/^[0-9a-f]{6,40}$/i.test(headSha)) {
        return sendJson(res, 400, { error: { reason: 'number + head_sha(6-40 hex) required' } });
      }
      const pr = await store.upsertPullRequest({ tenantId: mu.tenantId, repoId: g.repo.repo_id,
        providerPrNumber: number, headSha, headRef: body.head_ref ? String(body.head_ref) : null,
        baseRef: body.base_ref ? String(body.base_ref) : null, title: body.title ? String(body.title) : null,
        branchProtectionStatus: bps });
      return sendJson(res, 200, { ok: true, pull_request: pr });
    }

    // ── PR 读面（列表/详情：tenant 收窄 + 审查记录） ──
    if (p === '/api/mu/prs' && req.method === 'GET') {
      const g = await guard('read_pull_request');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rows = await store.findPullRequests(mu.tenantId, {
        repoId: q.repo_id ? String(q.repo_id) : null,
        number: q.number ? Number(q.number) : null });
      return sendJson(res, 200, { pull_requests: rows });
    }
    const prMatch = p.match(/^\/api\/mu\/prs\/([^/]+)(?:\/(.*))?$/);
    if (prMatch && req.method === 'GET') {
      const pr = await store.resolvePullRequest(mu.tenantId, prMatch[1]);
      if (!pr) return sendJson(res, 404, { error: { reason: 'pull_request_not_found' } });
      const sub = prMatch[2] ?? null;
      if (sub === 'changed-excerpt') {
        const g = await guard('read_code_content', { repoId: pr.repo_id });
        if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
        return sendJson(res, 200, { pr_id: pr.pr_id, head_sha: pr.head_sha,
          excerpt: fixtureChangedExcerpt(pr), note: 'fixture 合成摘录（授权边界验证用）' });
      }
      if (sub === null) {
        const g = await guard('read_pull_request', { repoId: pr.repo_id });
        if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
        const records = await store.listReviewRecords(mu.tenantId, pr.pr_id);
        return sendJson(res, 200, { pull_request: pr, review_records: records,
          my_permissions: { actions: roleActions(liveMembership.role) ?? [] } });
      }
      return sendJson(res, 404, { error: { reason: 'unknown pr subpath' } });
    }

    // ── 审查触发（reviewer+；只读审查 job） ──
    if (prMatch && prMatch[2] === 'review' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const pr = await store.resolvePullRequest(mu.tenantId, prMatch[1]);
      if (!pr) return sendJson(res, 404, { error: { reason: 'pull_request_not_found' } });
      const g = await guard('request_review', { repoId: pr.repo_id });
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const job = await store.enqueueJob({ tenantId: mu.tenantId, repoId: pr.repo_id,
        prId: pr.pr_id, kind: 'review_run', requestedBy: mu.userId,
        requestedRole: g.membership.role, payload: { head_sha: pr.head_sha } });
      await store.audit('MU_REVIEW_REQUESTED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { pr_id: pr.pr_id, job_id: job.job_id } });
      return sendJson(res, 200, { ok: true, job_id: job.job_id, state: job.state });
    }

    // ── 人工审批（maintainer+；branch protection 未知 → 禁止可合并结论） ──
    if (prMatch && prMatch[2] === 'decision' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const pr = await store.resolvePullRequest(mu.tenantId, prMatch[1]);
      if (!pr) return sendJson(res, 404, { error: { reason: 'pull_request_not_found' } });
      const g = await guard('decide_review', { repoId: pr.repo_id });
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const action = String(body.action || '');
      if (!['approve', 'reject'].includes(action)) {
        return sendJson(res, 400, { error: { reason: 'action(approve|reject) required' } });
      }
      if (action === 'approve' && pr.branch_protection_status !== 'known_clean') {
        await store.audit('MU_DECISION_BLOCKED', { tenantId: mu.tenantId, actorUserId: mu.userId,
          detail: { pr_id: pr.pr_id, branch_protection_status: pr.branch_protection_status } });
        return sendJson(res, 422, { error: { reason: 'cannot_conclude_mergeable',
          detail: `branch protection 状态为 ${pr.branch_protection_status}——禁止生成可合并结论（fail-closed；本切片无任何合并执行路径）` } });
      }
      const record = await store.insertReviewRecord({ tenantId: mu.tenantId, repoId: pr.repo_id,
        prId: pr.pr_id, kind: 'human_decision', actorUserId: mu.userId, decision: action,
        headSha: pr.head_sha, branchProtectionStatus: pr.branch_protection_status,
        payload: { note: String(body.note || '').slice(0, 200) } });
      await store.audit('MU_DECISION_RECORDED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { pr_id: pr.pr_id, decision: action } });
      return sendJson(res, 200, { ok: true, review_record: record });
    }

    // ── 受控修复（maintainer+ 且需 active Binding；执行前还会在 job 认领时复查） ──
    if (prMatch && prMatch[2] === 'repair' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const pr = await store.resolvePullRequest(mu.tenantId, prMatch[1]);
      if (!pr) return sendJson(res, 404, { error: { reason: 'pull_request_not_found' } });
      const g = await guard('request_repair', { repoId: pr.repo_id, needBinding: true });
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const job = await store.enqueueJob({ tenantId: mu.tenantId, repoId: pr.repo_id,
        prId: pr.pr_id, kind: 'repair_push', requestedBy: mu.userId,
        requestedRole: g.membership.role, payload: { head_sha: pr.head_sha } });
      await store.audit('MU_REPAIR_REQUESTED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { pr_id: pr.pr_id, job_id: job.job_id } });
      return sendJson(res, 200, { ok: true, job_id: job.job_id, state: job.state });
    }

    // ── RAG 检索面（rag_query；fixture 语料——Auditor/PlatformAdmin 默认无此动作） ──
    const ragMatch = p.match(/^\/api\/mu\/repositories\/([^/]+)\/rag-search$/);
    if (ragMatch && req.method === 'GET') {
      const g = await guard('rag_query', { repoId: ragMatch[1] });
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      return sendJson(res, 200, fixtureRagSearch(g.repo, q.q ?? ''));
    }

    // ── 任务（列表=成员可见，tenant 收窄；tick=fixture 执行器） ──
    if (p === '/api/mu/jobs' && req.method === 'GET') {
      const g = await guard('read_repository');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rows = await store.listJobs(mu.tenantId, { state: q.state ? String(q.state) : null });
      return sendJson(res, 200, { jobs: rows });
    }
    if (p === '/api/mu/jobs/tick' && req.method === 'POST') {
      // PR248 复核 P1 修复：fixture 执行器触发默认【关闭】——须显式 MU_FIXTURES=1
      if (env.MU_FIXTURES !== '1') return sendJson(res, 403, { error: { reason: 'fixtures_disabled' } });
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('read_repository'); // 触发执行器须为 active 成员
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const processed = [];
      for (;;) {
        const job = await store.claimNextJob();
        if (!job) break;
        // 执行前复查（授权快照不可信）：请求者成员关系 + 角色仍允许 + 修复需 Binding
        const m = await store.getMembership(job.tenant_id, job.requested_by);
        const action = job.kind === 'review_run' ? 'request_review' : 'request_repair';
        let binding = null;
        if (job.kind === 'repair_push') binding = await store.getBindingForRepo(job.tenant_id, job.repo_id);
        const decision = authorize({ membership: m ?? undefined, action, binding });
        if (!decision.ok) {
          await store.finishJob(job.job_id, 'rejected', { reason: decision.reason });
          await store.audit('MU_JOB_REJECTED', { tenantId: job.tenant_id, actorUserId: job.requested_by,
            detail: { job_id: job.job_id, kind: job.kind, reason: decision.reason } });
          processed.push({ job_id: job.job_id, state: 'rejected', reason: decision.reason });
          continue; // 拒绝路径绝不执行任何 provider 写入
        }
        const pr = job.pr_id ? await store.resolvePullRequest(job.tenant_id, job.pr_id) : null;
        if (job.kind === 'review_run' && pr) {
          const result = fixtureReviewRun(pr);
          await store.insertReviewRecord({ tenantId: job.tenant_id, repoId: job.repo_id,
            prId: job.pr_id, kind: 'ai_review', actorUserId: job.requested_by,
            decision: 'read_only_review', headSha: pr.head_sha,
            branchProtectionStatus: pr.branch_protection_status, payload: result });
          await store.finishJob(job.job_id, 'done', result);
          await store.audit('MU_JOB_DONE', { tenantId: job.tenant_id, actorUserId: job.requested_by,
            detail: { job_id: job.job_id, kind: job.kind, provider: 'fixture' } });
          processed.push({ job_id: job.job_id, state: 'done', kind: job.kind });
        } else if (job.kind === 'repair_push' && pr) {
          const wrote = fixtureRepairPush(pr, job);
          await store.insertReviewRecord({ tenantId: job.tenant_id, repoId: job.repo_id,
            prId: job.pr_id, kind: 'repair_record', actorUserId: job.requested_by,
            decision: 'fixture_executed', headSha: pr.head_sha,
            branchProtectionStatus: pr.branch_protection_status, payload: wrote });
          await store.finishJob(job.job_id, 'done', wrote);
          await store.audit('MU_JOB_DONE', { tenantId: job.tenant_id, actorUserId: job.requested_by,
            detail: { job_id: job.job_id, kind: job.kind, provider: 'fixture', wrote_ref: wrote.ref } });
          processed.push({ job_id: job.job_id, state: 'done', kind: job.kind });
        } else {
          await store.finishJob(job.job_id, 'failed', { reason: 'pr_not_resolvable' });
          processed.push({ job_id: job.job_id, state: 'failed' });
        }
      }
      return sendJson(res, 200, { ok: true, processed });
    }

    // ── 审计（read_audit：metadata only——Auditor/PlatformAdmin） ──
    if (p === '/api/mu/audit' && req.method === 'GET') {
      const g = await guard('read_audit');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rows = await store.listAudit(mu.tenantId, { limit: Number(q.limit || 100) });
      return sendJson(res, 200, { audit: rows });
    }

    // ── 身份流程保留位（GitHub App 安装流程状态——会话内查询） ──
    if (p === '/api/mu/installations/github/status' && req.method === 'GET') {
      const g = await guard('read_repository');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const st = githubAppStatus(env);
      return sendJson(res, 200, { flow: 'github_app_installation', configured: st.configured,
        reason: st.reason, note: st.note });
    }

    return sendJson(res, 404, { error: { reason: `unknown mu path: ${p}` } });
  } catch (e) {
    return sendJson(res, 500, { service_state: 'error', error_kind: 'internal',
      error: String(e?.message || e).slice(0, 200) });
  }
}
