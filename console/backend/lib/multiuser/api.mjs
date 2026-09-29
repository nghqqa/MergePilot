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
         muTokenFromCookieHeader, muClearCookies,
         muCorrCookie, muCorrClear, muCorrFromCookieHeader, appendCorrClear } from './session.mjs';
import { ghAppConfig, listInstallationRepositories } from './ghapp.mjs';
import { verifyWebhookSignature, readRawBody } from './webhookVerify.mjs';

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

// ── Wave 2B.1：event_sync 系统事件执行器（纯服务端上下文，零用户授权面） ──
// 上下文解析：installation（active 未撤未停）→ repository_binding（active，
// installation/tenant/repo 三方一致）→ 失效即 fail-closed（区分可重试瞬态）。
async function resolveEventSyncContext(store, job) {
  const instId = Number(job.payload?.installation_id ?? 0);
  if (!instId) return { ok: false, reason: 'installation_id_missing' };
  let inst = null;
  try { inst = await store.getInstallation(instId); }
  catch { return { ok: false, reason: 'transient_read_failed' }; }
  if (!inst || inst.tenant_id !== job.tenant_id) return { ok: false, reason: 'installation_mismatch' };
  if (inst.revoked_at) return { ok: false, reason: 'installation_revoked' };
  if (inst.suspended_at) return { ok: false, reason: 'installation_suspended' };
  let binding = null;
  try { binding = await store.getBindingByRepo(job.tenant_id, job.repo_id); }
  catch { return { ok: false, reason: 'transient_read_failed' }; }
  // pg BIGINT 以字符串返回——统一 Number 比较，避免 '7001' !== 7001 恒真误判
  if (!binding || Number(binding.installation_id) !== instId) return { ok: false, reason: 'binding_mismatch' };
  if (binding.binding_state === 'revoked') return { ok: false, reason: 'binding_revoked' };
  if (binding.binding_state === 'suspended') return { ok: false, reason: 'binding_suspended' };
  if (binding.binding_state === 'error') return { ok: false, reason: 'binding_error_state' };
  return { ok: true, installation_id: instId, binding };
}

// 幂等执行：当前仅消费 pull_request 事件（PR 快照 upsert）——delivery+event+
// object id+head_sha 天然构成唯一键（PR 快照 UNIQUE 含全部四要素的等价物：
// (tenant, repo, number, head_sha)），重复执行零新增行；审计侧由
// GHAPP_EVENT_SYNC_DONE 单行记录（job 单次 CAS 消费保证不重复审计）。
async function executeEventSync(store, muPoolQuery, job, sysCtx) {
  const ev = String(job.payload?.event ?? '');
  if (ev !== 'pull_request') {
    // 订阅但未实现处理器的事件类型：明确 dead-letter（不静默吞、不误入人工路径）
    return { state: 'failed', result: { reason: 'event_kind_unsupported', event: ev } };
  }
  const prNumber = Number(job.payload?.pr_number ?? 0);
  const headSha = String(job.payload?.head_sha ?? '');
  const githubRepoId = Number(job.payload?.github_repo_id ?? 0);
  if (!prNumber || !/^[0-9a-f]{6,40}$/i.test(headSha)) {
    return { state: 'failed', result: { reason: 'event_payload_invalid' } };
  }
  await store.upsertPullRequest({ tenantId: job.tenant_id, repoId: job.repo_id,
    providerPrNumber: prNumber, headSha, branchProtectionStatus: 'unknown',
    title: null });
  await muPoolQuery('UPDATE mu.repository_binding SET last_sync_at = now() WHERE repo_id=$1 AND tenant_id=$2',
    [job.repo_id, job.tenant_id]).catch(() => {});

  // ── Wave 3 PR-E：快照后跑完整审查管线（系统主体，不伪造用户 membership）──
  // 链路：handlePullRequestEvent（幂等 run + deterministic Reviewer）→ Leader 裁定 →
  // （fix_required 时）Fixer dry-run + 独立 Verifier + 终裁。任一环节 fail-closed
  // 不影响快照同步语义（快照已 done；管线结果独立落 review_run 域）。
  const pipeline = { review: null, decision: null, fix: null };
  try {
    const { handlePullRequestEvent } = await import('./review-service.mjs');
    const { advanceAfterReview } = await import('./agents/leader.mjs');
    const cfg = ghAppConfig(process.env);
    const pool = { query: muPoolQuery }; // 适配 orchestration 的 pool 接口
    const rv = await handlePullRequestEvent(pool, cfg, { payload: {
      action: String(job.payload?.action ?? 'synchronize'), github_repo_id: githubRepoId,
      pr_number: prNumber, head_sha: headSha } });
    pipeline.review = { ok: rv.ok, status: rv.run?.status ?? null,
      findings: rv.findings_count ?? null, reason: rv.reason ?? null };
    if (rv.ok && rv.run?.status === 'REVIEWED' && !rv.idempotent) {
      const findings = (await muPoolQuery(
        `SELECT rule_id, severity, path, line_start, summary_masked FROM mu.agent_finding
          WHERE run_id=$1 ORDER BY severity`, [rv.run.run_id])).rows;
      // branch protection 探测（真实 provider 已在 review 阶段拉取；此处用其结果域。
      // 未配置/未知 → Leader fail-closed BLOCKED）
      const dec = await advanceAfterReview(pool, { runId: rv.run.run_id,
        tenantId: job.tenant_id, repoId: job.repo_id, prId: rv.run.pr_id, headSha,
        findings, protection: rv.protection ?? { configured: false } });
      pipeline.decision = dec.decision ?? dec.reason ?? null;
      if (dec.decision === 'fix_required') {
        const { fixVerifyRound } = await import('./agents/fix-orchestrator.mjs');
        const dep = await buildFixDeps(muPoolQuery, { job, sysCtx, githubRepoId });
        if (dep) {
          const fx = await fixVerifyRound(pool, { run: rv.run,
            binding: { tenantId: job.tenant_id, repoId: job.repo_id, prId: rv.run.pr_id, headSha },
            deps: dep });
          // 执行器门控失败（executor_gate_rejected/at_health_failed 等）→ skipped+reason，
          // 绝不冒充 fix 完成；成功时标注实际执行器（agentteams=正式/internal=显式降级）
          pipeline.fix = fx.ok
            ? { verdict: fx.verdict ?? null, decision: fx.decision ?? null, executor: fx.executor ?? 'internal' }
            : { skipped: fx.stage ?? 'fix_failed', reason: fx.reason ?? null };
        } else {
          pipeline.fix = { skipped: 'deps_unavailable' };
        }
      }
    }
  } catch (e) {
    // 管线异常不吞：登记独立失败结果（快照仍算 done——管线有自身死信/审计）
    pipeline.review = pipeline.review ?? { ok: false, reason: `pipeline_error:${String(e?.message ?? e).slice(0, 80)}` };
  }
  return { state: 'done', result: { pr_number: prNumber, head_sha_prefix: headSha.slice(0, 12),
    pipeline } };
}

// Fixer deps 装配（真实路径：repoUrl=公开 GitHub URL；testCmd 默认平凡——真实部署
// 须配置 MU_FXV_TEST_CMD，默认值在文档与 evidence 中如实标注）
async function buildFixDeps(muPoolQuery, { job, sysCtx, githubRepoId }) {
  const rb = await muPoolQuery(
    `SELECT owner, name, installation_id FROM mu.repository_binding
      WHERE github_repo_id=$1 AND tenant_id=$2 AND binding_state='active' LIMIT 1`,
    [githubRepoId, job.tenant_id]).catch(() => null);
  if (!rb?.rows?.length) return null;
  const row = rb.rows[0];
  const overrides = global.__WAVE3_TEST_DEPS; // 仅测试注入（E2E 用本地仓库/定制 testCmd）
  return {
    repoUrl: overrides?.repoUrl ?? `https://github.com/${row.owner}/${row.name}.git`,
    testCmd: overrides?.testCmd ?? (process.env.MU_FXV_TEST_CMD || 'node -e process.exit(0)'),
    providerCfg: ghAppConfig(process.env),
    installationId: String(row.installation_id),
    owner: row.owner, repoName: row.name, prNumber: Number(job.payload?.pr_number ?? 0),
    assertServiceChain: async () => {
      const chk = await muPoolQuery(
        `SELECT 1 FROM mu.repository_binding rb
          JOIN mu.github_app_installation i ON i.installation_id = rb.installation_id
          WHERE rb.github_repo_id=$1 AND rb.tenant_id=$2 AND rb.binding_state='active'
            AND i.revoked_at IS NULL AND i.suspended_at IS NULL`,
        [githubRepoId, job.tenant_id]).catch(() => null);
      return Boolean(chk?.rows?.length);
    },
  };
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
    res.writeHead(302, { Location: `/multiuser?mu_login_error=${r}`,
      'Set-Cookie': muCorrClear() }); // 失败路径清理 correlation cookie（一次性语义）
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
    const corr = crypto.randomBytes(32).toString('base64url'); // login-CSRF 防护：一次性 correlation
    const ttlMs = Number(env.MU_OAUTH_FLOW_TTL_MS || 10 * 60_000);
    await store.insertOAuthFlow({
      stateHash: crypto.createHash('sha256').update(state).digest('hex'),
      corrHash: crypto.createHash('sha256').update(corr).digest('hex'),
      inviteId,
      ttlMs,
    });
    await store.auditPlatform('OAUTH_FLOW_STARTED', { detail: { invite_bound: Boolean(inviteId) } });
    res.setHeader('Set-Cookie', muCorrCookie(corr, ttlMs));
    return sendJson(res, 200, { authorize_url: buildAuthorizeUrl(cfg, state) });
  }

  if (p === '/api/mu/auth/oauth/github/callback' && req.method === 'GET') {
    const cfg = oauthConfig(env);
    if (!cfg.configured) return redirectLoginError('oauth_not_configured');
    const state = String(q.state ?? '');
    const code = String(q.code ?? '');
    const stateHash = state ? crypto.createHash('sha256').update(state).digest('hex') : '';
    const flow = stateHash ? await store.consumeOAuthFlow(stateHash) : null;
    // Wave 2A.1：correlation cookie 必须与 flow 内摘要匹配（缺失/错配/跨浏览器/
    // 存量 2A 无摘要 flow → 统一 state_invalid，不泄露区分信息）
    const corr = muCorrFromCookieHeader(req.headers.cookie);
    // PR253 验收修复：登录回调只消费登录流——安装流(purpose=ghapp_install)的
    // state+corr 不得被当登录 flow 使用（对称隔离；虽无提权面仍 fail-closed）
    const corrOk = flow && flow.corr_hash && corr
      && crypto.createHash('sha256').update(corr).digest('hex') === flow.corr_hash
      && flow.purpose === 'oauth_login';
    if (!flow || !corrOk) {
      await store.auditPlatform('OAUTH_FLOW_REJECTED', { detail: { reason: 'state_invalid' } });
      return redirectLoginError('state_invalid'); // 不存在/已消费(重放)/已过期/correlation 失配/跨用途 统一同因
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
        && inv.expected_subject === identity.subject ? inv : null;
      const claim = usable ?? await store.findClaimableInvitation({ subject: identity.subject });
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
    res.setHeader('Set-Cookie', appendCorrClear(sess.setCookie)); // 成功路径清理 correlation
    res.writeHead(302, { Location: '/multiuser' }); // 固定落地，不采纳任何请求参数
    return res.end();
  }

  // mu 直查 pool（webhook 事件解析用；函数声明+let 置于使用前避免 TDZ）
  let muPool = null;
  async function muPoolQ(text, params) {
    if (!muPool) {
      const pg = await loadPg();
      muPool = new pg.Pool({ connectionString: env.CONSOLE_PG_DSN, max: 2 });
    }
    return muPool.query(text, params);
  }

  // ── GitHub App webhook（Wave 2B）：仅签名认证，不接受浏览器会话 ──
  if (p === '/api/mu/github/webhook' && req.method === 'POST') {
    const cfgGh = ghAppConfig(env);
    const raw = await readRawBody(req).catch(() => null);
    const sig = String(req.headers['x-hub-signature-256'] ?? '');
    const v = raw ? verifyWebhookSignature(raw, sig, cfgGh.webhookSecret) : { ok: false, reason: 'empty_body' };
    const deliveryId = String(req.headers['x-github-delivery'] ?? '');
    const event = String(req.headers['x-github-event'] ?? '');
    if (!v.ok) {
      // fail-closed：验签失败不解析 body（先验签原则）；delivery 仍去重登记（脱敏 reason）
      if (deliveryId) await store.claimWebhookDelivery(deliveryId, { event: event || 'unknown' }).catch(() => {});
      await store.auditPlatform('OAUTH_FLOW_REJECTED', { detail: { reason: 'webhook_rejected', sig: v.reason } }).catch(() => {});
      return sendJson(res, 401, { error: { reason: 'webhook_signature_invalid' } });
    }
    let body = null;
    try { body = JSON.parse(raw); } catch { body = null; }
    if (!body || !deliveryId || !event) return sendJson(res, 400, { error: { reason: 'bad_request' } });
    // delivery 去重（一次性）：重复即 200 幂等返回，不重复写入/入队/审计
    const claim = await store.claimWebhookDelivery(deliveryId, {
      installationId: Number(body.installation?.id ?? 0) || null, event,
    });
    if (!claim) { await store.finishWebhookDelivery(deliveryId, 'duplicate'); return sendJson(res, 200, { ok: true, duplicate: true }); }
    // 先确认 installation（验签后第一道落库前校验）
    const instId = Number(body.installation?.id ?? 0);
    const installation = instId ? await store.getInstallation(instId) : null;
    if (!installation) {
      await store.finishWebhookDelivery(deliveryId, 'rejected');
      return sendJson(res, 200, { ok: true, ignored: 'installation_unknown' });
    }
    const tenantId = installation.tenant_id;
    try {
      if (event === 'installation') {
        const action = String(body.action ?? '');
        if (action === 'deleted') {
          await store.setInstallationState(instId, { revoked: true });
          await store.setBindingsStateForInstallation(instId, 'revoked');
        } else if (action === 'suspend') {
          await store.setInstallationState(instId, { suspended: true });
          await store.setBindingsStateForInstallation(instId, 'suspended');
        } else if (action === 'unsuspend') {
          await store.setInstallationState(instId, { suspended: false });
          await store.setBindingsStateForInstallation(instId, 'active');
        }
      } else if (event === 'installation_repositories') {
        // Wave 3 PR-E：added → upsert binding（此前无写入方）；removed → revoke（原有语义）
        if (Array.isArray(body.repositories_added)) {
          for (const r of body.repositories_added) {
            const gid = Number(r.id ?? 0);
            if (!gid) continue;
            const repoRow = await store.ensureRepository({ tenantId, provider: 'github',
              providerRepoId: String(gid), owner: String(r.full_name?.split('/')[0] ?? r.name ?? ''),
              name: String(r.name ?? ''), defaultBranch: null });
            await muPoolQ(
              `INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name,
                  installation_id, default_branch, binding_state)
               VALUES ($1,$2,$3,$4,$5,$6,NULL,'active')
               ON CONFLICT (github_repo_id) DO UPDATE SET binding_state='active',
                 revoked_at=NULL, installation_id=EXCLUDED.installation_id, updated_at=now()`,
              [tenantId, repoRow.repo_id, gid,
                String(r.full_name?.split('/')[0] ?? r.name ?? ''), String(r.name ?? ''), instId]);
          }
        }
        if (Array.isArray(body.repositories_removed) && body.repositories_removed.length) {
          for (const r of body.repositories_removed) {
            const gid = Number(r.id ?? 0);
            if (!gid) continue;
            const hit = await muPoolQ(`SELECT repo_id FROM mu.repository_binding WHERE github_repo_id=$1 AND tenant_id=$2`, [gid, tenantId]);
            for (const row of hit.rows) await store.setBindingState(tenantId, row.repo_id, 'revoked', 'repository_removed');
          }
        }
      } else if (event === 'pull_request' && body.pull_request && body.repository) {
        const gid = Number(body.repository.id ?? 0);
        const hit = await muPoolQ(`SELECT repo_id FROM mu.repository_binding WHERE github_repo_id=$1 AND tenant_id=$2 AND binding_state='active'`, [gid, tenantId]);
        if (hit.rows.length) {
          // 入队前完成 tenant/repo 解析；异步 worker 执行时复查 binding/installation/membership
          await store.enqueueJob({ tenantId, repoId: hit.rows[0].repo_id,
            kind: 'event_sync', requestedBy: null, requestedRole: 'maintainer',
            payload: { event: 'pull_request', delivery_id: deliveryId,
              installation_id: instId,
              github_repo_id: gid,
              pr_number: Number(body.pull_request.number ?? 0),
              head_sha: String(body.pull_request.head?.sha ?? ''),
              action: String(body.action ?? '') } });
        }
      }
      await store.finishWebhookDelivery(deliveryId, 'processed');
      await store.audit('GHAPP_WEBHOOK_PROCESSED', { tenantId, actorUserId: null,
        detail: { event, delivery_prefix: deliveryId.slice(0, 8), installation_id: instId } });
      return sendJson(res, 200, { ok: true });
    } catch (e) {
      console.error('[mu-webhook]', String(e?.message ?? e).slice(0, 200));
      await store.finishWebhookDelivery(deliveryId, 'received'); // 可重试（幂等）
      return sendJson(res, 500, { error: { reason: 'webhook_processing_failed' } });
    }
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
      res.setHeader('Set-Cookie', appendCorrClear(muClearCookies()));
      return sendJson(res, 200, { ok: true, revoked });
    }
    if (p === '/api/mu/auth/sessions/revoke-all' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const n = await store.revokeAllSessionsForUser(mu.userId, 'revoke_all');
      await store.auditPlatform('SESSIONS_REVOKED_ALL', { actorUserId: mu.userId, detail: { count: n } });
      res.setHeader('Set-Cookie', appendCorrClear(muClearCookies()));
      return sendJson(res, 200, { ok: true, revoked: n });
    }

    // ── GitHub App 安装与仓库绑定（Wave 2B；Maintainer 门） ──
    if (p === '/api/mu/github/app/status' && req.method === 'GET') {
      const g = await guard('manage_repository_binding');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const cfgGh = ghAppConfig(env);
      return sendJson(res, 200, { configured: cfgGh.configured,
        ...(cfgGh.configured ? { app_id: cfgGh.appId, permissions: cfgGh.permissions, events: cfgGh.events } : { reason: cfgGh.reason }) });
    }
    if (p === '/api/mu/github/install/start' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_repository_binding');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const cfgGh = ghAppConfig(env);
      if (!cfgGh.configured) {
        return sendJson(res, 503, { error: { reason: cfgGh.reason ?? 'github_app_not_configured',
          detail: '需 MU_GITHUB_APP_ID/_APP_SLUG/_PRIVATE_KEY/_WEBHOOK_SECRET/_INSTALL_CALLBACK_URL 显式配置'
            + (cfgGh.reason === 'github_app_slug_not_configured'
              ? '（MU_GITHUB_APP_SLUG 缺失或为 placeholder——GitHub App URL slug，见 github.com/settings/apps/{slug}）' : '') } });
      }
      const state = crypto.randomBytes(32).toString('base64url');
      const corr = crypto.randomBytes(32).toString('base64url');
      const ttlMs = Number(env.MU_OAUTH_FLOW_TTL_MS || 10 * 60_000);
      const flow = await store.insertOAuthFlow({
        stateHash: crypto.createHash('sha256').update(state).digest('hex'),
        corrHash: crypto.createHash('sha256').update(corr).digest('hex'),
        inviteId: null, ttlMs,
      });
      await muPoolQ('UPDATE mu.oauth_flow SET purpose=$1 WHERE flow_id=$2', ['ghapp_install', flow.flow_id]);
      await store.auditPlatform('OAUTH_FLOW_STARTED', { actorUserId: mu.userId, detail: { purpose: 'ghapp_install' } });
      res.setHeader('Set-Cookie', muCorrCookie(corr, ttlMs));
      return sendJson(res, 200, { install_url: cfgGh.installUrl + '?state=' + encodeURIComponent(state)
        + '&redirect_uri=' + encodeURIComponent(cfgGh.installCallbackUrl) });
    }
    if (p === '/api/mu/github/install/callback' && req.method === 'GET') {
      const cfgGh = ghAppConfig(env);
      const failGh = (reason) => {
        res.writeHead(302, { Location: '/multiuser?ghapp_error=' + reason, 'Set-Cookie': muCorrClear() });
        res.end();
      };
      if (!cfgGh.configured) return failGh('oauth_not_configured');
      const state = String(q.state ?? '');
      const installationId = Number(q.installation_id ?? 0);
      const stateHash = state ? crypto.createHash('sha256').update(state).digest('hex') : '';
      const flow = stateHash ? await store.consumeOAuthFlow(stateHash) : null;
      const corr = muCorrFromCookieHeader(req.headers.cookie);
      const corrOk = flow && flow.corr_hash && corr
        && crypto.createHash('sha256').update(corr).digest('hex') === flow.corr_hash
        && flow.purpose === 'ghapp_install';
      if (!flow || !corrOk || !installationId || !['install', 'update'].includes(String(q.setup_action ?? ''))) {
        await store.auditPlatform('OAUTH_FLOW_REJECTED', { detail: { reason: 'state_invalid', purpose: 'ghapp_install' } });
        return failGh('state_invalid');
      }
      let repos;
      try { repos = await listInstallationRepositories(cfgGh, installationId); }
      catch (e) {
        await store.auditPlatform('OAUTH_FLOW_REJECTED', { detail: { reason: 'installation_unreadable' } });
        return failGh('installation_unreadable');
      }
      const owner0 = repos[0]?.owner_login ?? 'unknown';
      await store.upsertInstallation({ installationId, tenantId: mu.tenantId,
        accountId: repos[0]?.owner_id ?? 0, accountLogin: owner0, accountType: 'User',
        appId: cfgGh.appId });
      // Wave 3 PR-E：安装落库时同步登记 repository_binding（PR 事件入队/审查服务链
      // 的数据源——此前无任何写入方，属实现断层）。幂等 upsert；已存在则保持 active。
      for (const r of repos) {
        const repoRow = await store.ensureRepository({ tenantId: mu.tenantId, provider: 'github',
          providerRepoId: String(r.id), owner: r.owner_login, name: r.name,
          defaultBranch: r.default_branch });
        await muPoolQ(
          `INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name,
              installation_id, default_branch, binding_state, created_by)
           VALUES ($1,$2,$3,$4,$5,$6,$7,'active',$8)
           ON CONFLICT (github_repo_id) DO UPDATE SET binding_state='active',
             revoked_at=NULL, updated_at=now()`,
          [mu.tenantId, repoRow.repo_id, Number(r.id), r.owner_login, r.name,
            Number(installationId), r.default_branch, mu.userId]);
      }
      await store.audit('GHAPP_INSTALLATION_REGISTERED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { installation_id: installationId, repos_count: repos.length,
          bindings_upserted: repos.length } });
      res.setHeader('Set-Cookie', appendCorrClear([]));
      res.writeHead(302, { Location: '/multiuser' });
      return res.end();
    }
    if (p === '/api/mu/github/installations' && req.method === 'GET') {
      const g = await guard('manage_repository_binding');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rows = await store.listInstallations(mu.tenantId);
      return sendJson(res, 200, { installations: rows.map((r) => ({
        installation_id: r.installation_id, account_login: r.account_login,
        suspended: Boolean(r.suspended_at), revoked: Boolean(r.revoked_at), created_at: r.created_at })) });
    }
    const ghReposMatch = p.match(/^\/api\/mu\/github\/installations\/(\d+)\/repositories$/);
    if (ghReposMatch && req.method === 'GET') {
      const g = await guard('manage_repository_binding');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const instId = Number(ghReposMatch[1]);
      const inst = await store.getInstallation(instId);
      if (!inst || inst.tenant_id !== mu.tenantId) {
        return sendJson(res, 404, { error: { reason: 'installation_not_found' } });
      }
      if (inst.revoked_at) return sendJson(res, 409, { error: { reason: 'installation_revoked' } });
      if (inst.suspended_at) return sendJson(res, 409, { error: { reason: 'installation_suspended' } });
      const cfgGh = ghAppConfig(env);
      let repos;
      try { repos = await listInstallationRepositories(cfgGh, instId); }
      catch (e) { return sendJson(res, 502, { error: { reason: 'github_read_failed' } }); }
      return sendJson(res, 200, { repositories: repos });
    }
    const bindMatch = p.match(/^\/api\/mu\/repositories\/([^/]+)\/ghapp-binding$/);
    if (bindMatch && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_repository_binding', { repoId: bindMatch[1] });
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const instId = Number(body.installation_id ?? 0);
      const ghRepoId = Number(body.github_repo_id ?? 0);
      if (!instId || !ghRepoId) return sendJson(res, 400, { error: { reason: 'installation_id + github_repo_id required' } });
      const inst = await store.getInstallation(instId);
      if (!inst || inst.tenant_id !== mu.tenantId) return sendJson(res, 404, { error: { reason: 'installation_not_found' } });
      if (inst.revoked_at) return sendJson(res, 409, { error: { reason: 'installation_revoked' } });
      if (inst.suspended_at) return sendJson(res, 409, { error: { reason: 'installation_suspended' } });
      const cfgGh = ghAppConfig(env);
      let repos;
      try { repos = await listInstallationRepositories(cfgGh, instId); }
      catch (e) { return sendJson(res, 502, { error: { reason: 'github_read_failed' } }); }
      const target = repos.find((r) => Number(r.id) === ghRepoId);
      if (!target) return sendJson(res, 404, { error: { reason: 'repository_not_authorized' } });
      const dup = await muPoolQ("SELECT tenant_id FROM mu.repository_binding WHERE github_repo_id=$1 AND binding_state='active'", [ghRepoId]);
      if (dup.rows.length && dup.rows[0].tenant_id !== mu.tenantId) {
        return sendJson(res, 409, { error: { reason: 'repository_already_bound' } });
      }
      const repo = await store.ensureRepository({ tenantId: mu.tenantId, provider: 'github',
        providerRepoId: String(ghRepoId), owner: target.owner_login, name: target.name,
        defaultBranch: target.default_branch });
      const binding = await store.upsertRepositoryBinding({ tenantId: mu.tenantId, repoId: repo.repo_id,
        githubRepoId: ghRepoId, owner: target.owner_login, name: target.name,
        installationId: instId, defaultBranch: target.default_branch, createdBy: mu.userId });
      await store.audit('GHAPP_REPO_BOUND', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { repo_id: repo.repo_id, installation_id: instId, github_repo_id: ghRepoId } });
      return sendJson(res, 200, { ok: true, binding });
    }
    if (bindMatch && req.method === 'DELETE') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_repository_binding', { repoId: bindMatch[1] });
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const revoked = await store.setBindingState(mu.tenantId, g.repo.repo_id, 'revoked', 'manual_unbind');
      await store.audit('GHAPP_REPO_UNBOUND', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { repo_id: g.repo.repo_id } });
      return sendJson(res, 200, { ok: true, revoked: Boolean(revoked) });
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
      // Wave 2A.1：生产邀请必须绑定 GitHub 数字 user id（handle 可夺注——login 句柄
      // 仅作展示/预筛选，不可作为授权条件）。存量 login-only 邀请 fail-closed 不可认领，
      // 运营迁移=以数字 id 重建邀请（见 PR 迁移说明）。
      let expectedSubject = null;
      if (body.expected_subject) {
        const digits = String(body.expected_subject).replace(/^github-oauth:/, '');
        if (!/^\d{1,20}$/.test(digits)) {
          return sendJson(res, 400, { error: { reason: 'expected_subject 须为 GitHub 数字 id（或 github-oauth:<id>）' } });
        }
        expectedSubject = `github-oauth:${digits}`;
      } else {
        return sendJson(res, 400, { error: { reason: 'expected_subject_required',
          detail: '邀请必须绑定 GitHub 数字 user id（expected_subject）；login 句柄仅可选作展示（expected_login）' } });
      }
      const expectedLogin = body.expected_login ? String(body.expected_login) : null;
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
      // PR254 验收 P1 修复：生产路径不得依赖 fixture 开关——tick 在 MU_FIXTURES!=1
      // 时仍处理【仅系统事件 job】（event_sync，真实 store 操作零 fixture 依赖）；
      // 人工 job（review_run/repair_push，fixture 执行器）仍须显式 MU_FIXTURES=1。
      const fixturesOn = env.MU_FIXTURES === '1';
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('read_repository'); // 触发执行器须为 active 成员
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const processed = [];
      for (;;) {
        const job = await store.claimNextJob();
        if (!job) break;
        if (!fixturesOn && job.kind !== 'event_sync') {
          // 非 fixture 模式仅消费系统事件：人工 job 原样回队（不执行不审计）
          await store.requeueJob(job.job_id);
          break; // 队首为人工 job 即停（避免空转重取同一行）
        }
        // Wave 2B.1：系统事件 job（event_sync，requested_by=NULL）走独立分支——
        // 授权上下文=已验证的 installation→binding→tenant/repo 服务链，绝不伪造
        // 用户 membership（不通过 getMembership(null) 冒充未登录拒绝，也不借
        // 任何真人身份放行）。人工 job（review_run/repair_push）维持原复查路径。
        if (job.kind === 'event_sync') {
          const sysCtx = await resolveEventSyncContext(store, job);
          if (!sysCtx.ok) {
            // fail-closed：installation/binding/tenant/repo 任一失效即拒绝（可重试
            // 语义区分：binding_invalid 类不重试，transient 类回 queued 由下轮重试）
            const retryable = sysCtx.reason === 'transient_read_failed';
            await store.finishJob(job.job_id, retryable ? 'queued' : 'rejected', { reason: sysCtx.reason });
            await store.audit('GHAPP_EVENT_SYNC_REJECTED', { tenantId: job.tenant_id, actorUserId: null,
              detail: { job_id: job.job_id, delivery_prefix: String(job.payload?.delivery_id ?? '').slice(0, 8),
                reason: sysCtx.reason, retryable } });
            processed.push({ job_id: job.job_id, state: retryable ? 'requeued' : 'rejected', reason: sysCtx.reason });
            continue;
          }
          // 幂等消费：delivery_id+event+object id+head_sha 组成去重键——重复入队/
          // 重试重放不再二次落库（PR 快照 upsert 天然幂等，这里补审计侧去重）
          const r = await executeEventSync(store, muPoolQ, job, sysCtx);
          await store.finishJob(job.job_id, r.state, r.result);
          await store.audit('GHAPP_EVENT_SYNC_DONE', { tenantId: job.tenant_id, actorUserId: null,
            detail: { job_id: job.job_id, event: job.payload?.event,
              delivery_prefix: String(job.payload?.delivery_id ?? '').slice(0, 8), repo_id: job.repo_id,
              installation_id: sysCtx.installation_id, outcome: r.state } });
          processed.push({ job_id: job.job_id, state: r.state, kind: 'event_sync' });
          continue;
        }
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

    // ── Agent 运行策略（Wave 3.2；PlatformAdmin 读写；零凭据字段）──
    if (p === '/api/mu/agent-policy' && req.method === 'GET') {
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const ap = await import('./agent-policy.mjs');
      const policy = await ap.getAgentPolicy({ query: muPoolQ });
      return sendJson(res, 200, { policy: {
        mode: policy.mode, provider: policy.provider, model: policy.model,
        timeout_ms: Number(policy.timeout_ms), max_output_tokens: Number(policy.max_output_tokens),
        enabled: Boolean(policy.enabled), policy_version: Number(policy.policy_version),
        updated_at: policy.updated_at },
        deploy: ap.deployStatus(env), // 布尔+host 摘要+模型白名单（永不含完整 URL/key）
        executor: (await import('./agents/agentteams-executor.mjs')).executorStatusSummary(env),
        runtime_state: ap.runtimeState(policy, env) });
    }
    if (p === '/api/mu/agent-policy' && req.method === 'PUT') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      if (body?.confirm !== true) {
        return sendJson(res, 400, { error: { reason: 'confirm_required',
          detail: '策略变更须显式 confirm:true（仅影响新建审查任务）' } });
      }
      const patch = (({ mode, provider, model, timeout_ms, max_output_tokens, enabled }) =>
        ({ mode, provider, model, timeout_ms, max_output_tokens, enabled }))(body ?? {});
      const ap = await import('./agent-policy.mjs');
      const v = ap.validatePolicyPatch(patch, env);
      if (!v.ok) return sendJson(res, 400, { error: { reason: v.code } });
      const up = await ap.updateAgentPolicy({ query: muPoolQ }, {
        expectedVersion: Number(body.expected_version), patch, actorId: mu.userId });
      if (!up.ok) {
        if (up.code === 'version_conflict') {
          return sendJson(res, 409, { error: { reason: 'version_conflict',
            current_policy_version: Number(up.current.policy_version) } });
        }
        return sendJson(res, 400, { error: { reason: up.code } });
      }
      await store.audit('AGENT_POLICY_UPDATED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { mode: up.policy.mode, enabled: Boolean(up.policy.enabled),
          model: up.policy.model, timeout_ms: Number(up.policy.timeout_ms),
          max_output_tokens: Number(up.policy.max_output_tokens),
          policy_version: Number(up.policy.policy_version) } }); // 脱敏：只有策略字段，无 env/凭据
      return sendJson(res, 200, { ok: true, policy: {
        mode: up.policy.mode, provider: up.policy.provider, model: up.policy.model,
        timeout_ms: Number(up.policy.timeout_ms), max_output_tokens: Number(up.policy.max_output_tokens),
        enabled: Boolean(up.policy.enabled), policy_version: Number(up.policy.policy_version),
        updated_at: up.policy.updated_at } });
    }

    // ── 审查管线只读查询（Wave 3 PR-E；read_pull_request 角色）──
    // 错误不泄露 tenant/repo/PR/用户存在性：统一 not_found/unauthorized 短语。
    if (p === '/api/mu/runs' && req.method === 'GET') {
      const g = await guard('read_pull_request');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rows = await muPoolQ(
        `SELECT r.run_id, r.status, r.trigger_source, r.head_sha, r.created_at, r.updated_at,
                pr.provider_pr_number, rep.owner, rep.name repo_name,
                (SELECT count(*) FROM mu.agent_finding f WHERE f.run_id = r.run_id) findings_count,
                (SELECT max(severity) FROM mu.agent_finding f WHERE f.run_id = r.run_id) top_severity
           FROM mu.review_run r
           JOIN mu.pull_request pr ON pr.pr_id = r.pr_id
           JOIN mu.repository rep ON rep.repo_id = r.repo_id
          WHERE r.tenant_id = $1
            ${q.repo_id ? 'AND r.repo_id = $2' : ''}
          ORDER BY r.created_at DESC LIMIT 50`,
        q.repo_id ? [mu.tenantId, String(q.repo_id)] : [mu.tenantId]);
      return sendJson(res, 200, { runs: rows.rows.map((x) => ({ ...x,
        findings_count: Number(x.findings_count) })) });
    }
    const runMatch = p.match(/^\/api\/mu\/runs\/([0-9a-f-]{36})$/);
    if (runMatch && req.method === 'GET') {
      const g = await guard('read_pull_request');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const runId = runMatch[1];
      const run = await muPoolQ(
        `SELECT r.*, pr.provider_pr_number, rep.owner, rep.name repo_name
           FROM mu.review_run r JOIN mu.pull_request pr ON pr.pr_id = r.pr_id
           JOIN mu.repository rep ON rep.repo_id = r.repo_id
          WHERE r.run_id=$1 AND r.tenant_id=$2`,
        [runId, mu.tenantId]);
      if (!run.rows.length) return sendJson(res, 404, { error: { reason: 'not_found' } });
      const [attempts, findings, fixes, verifies, decisions, dlq] = await Promise.all([
        muPoolQ(`SELECT agent_role, attempt, status, provider, error_code, latency_ms,
                  input_digest, output_digest, created_at FROM mu.agent_attempt
                 WHERE run_id=$1 ORDER BY created_at`, [runId]),
        muPoolQ(`SELECT rule_id, severity, confidence, path, line_start, line_end, title,
                  summary_masked, evidence_ref, remediation FROM mu.agent_finding
                 WHERE run_id=$1 ORDER BY severity, path`, [runId]),
        muPoolQ(`SELECT attempt, status, patch_digest, artifact_ref, error_code, created_at
                 FROM mu.fix_attempt WHERE run_id=$1 ORDER BY attempt`, [runId]),
        muPoolQ(`SELECT attempt, verdict, error_code, created_at FROM mu.verification_attempt
                 WHERE run_id=$1 ORDER BY attempt`, [runId]),
        muPoolQ(`SELECT stage, decision, rationale_ref, actor_principal, policy_version, created_at
                 FROM mu.orchestration_decision WHERE run_id=$1 ORDER BY created_at`, [runId]),
        muPoolQ(`SELECT kind, reason, retry_count, payload_ref, created_at FROM mu.dead_letter
                 WHERE run_id=$1 AND resolved_at IS NULL`, [runId]),
      ]);
      return sendJson(res, 200, { run: run.rows[0], attempts: attempts.rows,
        findings: findings.rows, fixes: fixes.rows, verifications: verifies.rows,
        decisions: decisions.rows, dead_letters: dlq.rows });
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
