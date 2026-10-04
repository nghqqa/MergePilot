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
let muReadyState = { ready: false, error: null, started_at: null };
/** Wave 3.9（Beta 硬化）：schema 初始化状态（/api/health readiness 披露）。 */
export function muSchemaReadyState() { return { ...muReadyState }; }
/**
 * Wave 3.9（Beta 硬化）：启动阶段显式执行 schema 初始化（readiness gate）。
 *  * createConsole 在 MU_MODE=multiuser 且有 DSN 时即调用本函数（不等首个请求）；
 *  * 所有 mu 业务面（muApi/getMuConsoleApi）await 本 promise——init 完成前
 *    不接受业务任务（请求挂起至就绪，而非 503 或带病服务）；
 *  * 幂等：与 getMuStore 共享同一 memoized promise。
 */
export async function ensureMuReady(env) {
  if (!muReadyState.started_at) muReadyState.started_at = new Date().toISOString();
  try {
    const store = await getMuStore(env);
    muReadyState = { ready: true, error: null, started_at: muReadyState.started_at };
    return store;
  } catch (e) {
    muReadyState = { ready: false, error: String(e?.message ?? e).slice(0, 120), started_at: muReadyState.started_at };
    throw e;
  }
}
export function getMuStore(env) {
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
      // ── C1：进程启动一次——executor 崩溃遗留的 RUNNING 调用事件补记 INTERRUPTED
      //（best-effort：留痕恢复失败不影响 mu 面可用性）──
      const recBoot = await import('./invocation-recorder.mjs');
      await recBoot.recoverIncompleteInvocations(pool, { olderThanMs: 15 * 60_000 }).catch(() => {});
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

  // ── PR 生命周期状态收敛（Wave 3.7 数据源纯化·九）：closed/reopened 如实落库 ──
  // 此前 action 仅记档：closed 事件会再触发一轮审查管线且 state 永不更新——
  // overview/pulls 的 open 统计在 PR 关闭后不收敛。closed 不进入审查管线（关闭的
  // PR 无需审查，head 不变旧 run 保持 stale 语义）。
  const prAction = String(job.payload?.action ?? '');
  if (prAction === 'closed' || prAction === 'reopened') {
    await muPoolQuery(
      `UPDATE mu.pull_request SET state=$1, updated_at=now()
        WHERE tenant_id=$2 AND repo_id=$3 AND provider_pr_number=$4`,
      [prAction === 'closed' ? 'closed' : 'open', job.tenant_id, job.repo_id, prNumber]).catch(() => {});
    if (prAction === 'closed') {
      return { state: 'done', result: { pr_number: prNumber, head_sha_prefix: headSha.slice(0, 12),
        pipeline: { skipped: 'pr_closed' } } };
    }
  }

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
      // v16 审批门：fix_required 现→WAITING_FOR_HUMAN_APPROVAL（dec.run_status 携带）——
      // 仅当 run 实际到达 FIX_QUEUED（历史数据/测试直通）才内联修复轮；否则等人工批准
      if (dec.decision === 'fix_required' && dec.run_status === 'FIX_QUEUED') {
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

// v16 审批门：按 repo_id 装配修复轮 deps（approve 后内联启动用；
// 与 buildFixDeps 同形状——仅键不同：绑定衈按 repo_id 查而非 github_repo_id+job）
async function buildFixDepsForRepo(muPoolQuery, { tenantId, repoId, prNumber }) {
  const rb = await muPoolQuery(
    `SELECT owner, name, installation_id, github_repo_id FROM mu.repository_binding
      WHERE repo_id=$1 AND tenant_id=$2 AND binding_state='active' LIMIT 1`,
    [repoId, tenantId]).catch(() => null);
  if (!rb?.rows?.length) return null;
  const row = rb.rows[0];
  const overrides = global.__WAVE3_TEST_DEPS;
  return {
    repoUrl: overrides?.repoUrl ?? `https://github.com/${row.owner}/${row.name}.git`,
    testCmd: overrides?.testCmd ?? (process.env.MU_FXV_TEST_CMD || 'node -e process.exit(0)'),
    providerCfg: ghAppConfig(process.env),
    installationId: String(row.installation_id),
    owner: row.owner, repoName: row.name, prNumber: Number(prNumber ?? 0),
    assertServiceChain: async () => {
      const chk = await muPoolQuery(
        `SELECT 1 FROM mu.repository_binding rb
          JOIN mu.github_app_installation i ON i.installation_id = rb.installation_id
          WHERE rb.repo_id=$1 AND rb.tenant_id=$2 AND rb.binding_state='active'
            AND i.revoked_at IS NULL AND i.suspended_at IS NULL`,
        [repoId, tenantId]).catch(() => null);
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
      // 空闲连接崩溃不经过任何查询回调——无监听会以 unhandled error 拖垮进程（对齐 getMuStore 的 pool.on('error')）
      muPool.on('error', (err) => {
        console.error(`[mu:poolq] pg pool error: ${String(err?.message ?? err).slice(0, 200)}`);
      });
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

    // ── 技能版本治理（v17 mu_skill_registry；B 波：治理面只决定「哪个版本生效」）──
    // 读=任意成员（read_repository 在全成员角色基座；auditor 仅 read_audit→403 如实）；
    // 写=platform_admin（manage_instance）。写全 CSRF+审计；审计 detail 只含
    // skill_key/version/state/rollback——不含 changelog/prompt/工件内容（脱敏合同）。
    // 版本不可变合同：发布=只新增行（同版本同指纹重复发布=幂等 200；同版本不同指纹=409）；
    // 回滚/激活=仅 CAS 切 current_version 指针（同目标并发恰一赢家审计，异目标并发
    // last-write-wins 且各审计一笔），版本行永不 UPDATE/DELETE。
    if (p === '/api/mu/skills' && req.method === 'GET') {
      const g = await guard('read_repository');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rows = await muPoolQ(
        `SELECT s.skill_id, s.skill_key, s.display_name, s.description,
                s.current_version, s.state, s.updated_at,
                (SELECT count(*) FROM mu.skill_version v WHERE v.skill_id = s.skill_id) version_count
           FROM mu.skill s WHERE s.tenant_id=$1 ORDER BY s.skill_key`, [mu.tenantId]);
      return sendJson(res, 200, { skills: rows.rows });
    }
    if (p === '/api/mu/skills' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const key = String(body.skill_key || '').trim();
      if (!/^[a-z0-9][a-z0-9._-]{1,63}$/.test(key)) {
        return sendJson(res, 400, { error: { reason: 'skill_key 须为小写字母/数字/._-（2-64 位）' } });
      }
      const name = String(body.display_name || '').trim();
      if (!name) return sendJson(res, 400, { error: { reason: 'display_name required' } });
      const dup = await muPoolQ(`SELECT 1 FROM mu.skill WHERE tenant_id=$1 AND skill_key=$2`, [mu.tenantId, key]);
      if (dup.rows.length) return sendJson(res, 409, { error: { reason: 'skill_key_exists' } });
      const r = await muPoolQ(
        `INSERT INTO mu.skill (tenant_id, skill_key, display_name, description, created_by)
         VALUES ($1,$2,$3,$4,$5) RETURNING *`,
        [mu.tenantId, key, name, String(body.description || '').slice(0, 500), mu.userId]);
      await store.audit('MU_SKILL_REGISTERED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { skill_key: key } });
      return sendJson(res, 200, { ok: true, skill: r.rows[0] });
    }
    const skillDetailMatch = p.match(/^\/api\/mu\/skills\/([^/]+)$/);
    if (skillDetailMatch && req.method === 'GET') {
      const g = await guard('read_repository');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const sk = await muPoolQ(
        `SELECT s.*, (SELECT count(*) FROM mu.skill_version v WHERE v.skill_id = s.skill_id) version_count
           FROM mu.skill s WHERE s.tenant_id=$1 AND s.skill_key=$2`,
        [mu.tenantId, decodeURIComponent(skillDetailMatch[1])]);
      if (!sk.rows.length) return sendJson(res, 404, { error: { reason: 'skill_not_found' } });
      return sendJson(res, 200, { skill: sk.rows[0] });
    }
    // （2026-10 整改：删除恒成功死端点 POST /api/mu/skills/:k/versions/:v/publish——
    //  版本在 POST /versions 创建时即已发布，该端点无状态迁移、无审计且前端从未调用；
    //  删除后同形路径落入统一 404 兜底（unknown mu path），集成测试锁此行为。）
    const skillVerMatch = p.match(/^\/api\/mu\/skills\/([^/]+)\/versions$/);
    if (skillVerMatch && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const version = String(body.version || '').trim();
      const sha = String(body.manifest_sha256 || '').trim();
      if (!/^\d+\.\d+\.\d+$/.test(version)) {
        return sendJson(res, 400, { error: { reason: 'version 须为语义化版本（如 1.0.0）' } });
      }
      if (!/^[0-9a-f]{64}$/.test(sha)) {
        return sendJson(res, 400, { error: { reason: 'manifest_sha256 须为 64 位十六进制指纹' } });
      }
      const sk = await muPoolQ(`SELECT * FROM mu.skill WHERE tenant_id=$1 AND skill_key=$2`,
        [mu.tenantId, decodeURIComponent(skillVerMatch[1])]);
      if (!sk.rows.length) return sendJson(res, 404, { error: { reason: 'skill_not_found' } });
      // 响应里的 current_version 一律回读服务端真值——请求前快照在并发下可能过期，
      // 前端不得用旧快照推断「是否已自动激活」。
      const currentOf = async () => (await muPoolQ(
        `SELECT current_version FROM mu.skill WHERE skill_id=$1`, [sk.rows[0].skill_id])).rows[0]?.current_version ?? null;
      const dup = await muPoolQ(
        `SELECT manifest_sha256 FROM mu.skill_version WHERE skill_id=$1 AND version=$2`,
        [sk.rows[0].skill_id, version]);
      if (dup.rows.length) {
        // 幂等边界：同版本+同指纹=幂等成功；同版本+不同指纹=不可变冲突（防替换）
        if (dup.rows[0].manifest_sha256 === sha) {
          return sendJson(res, 200, { ok: true, idempotent: true, version,
            current_version: await currentOf(), activated: false });
        }
        return sendJson(res, 409, { error: { reason: 'version_immutable_conflict',
          detail: '该版本号已发布且指纹不同——版本发布后不可变，请递增版本号' } });
      }
      let vRow;
      try {
        const v = await muPoolQ(
          `INSERT INTO mu.skill_version (tenant_id, skill_id, version, changelog, manifest_sha256, artifact_ref, created_by)
           VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *`,
          [mu.tenantId, sk.rows[0].skill_id, version, String(body.changelog || '').slice(0, 500),
            sha, String(body.artifact_ref || '').slice(0, 300), mu.userId]);
        vRow = v.rows[0];
      } catch (e) {
        // 并发发布同版本号撞 UNIQUE(skill_id, version)：按幂等合同收口（同指纹=幂等，异指纹=409）
        if (e?.code !== '23505') throw e;
        const re = await muPoolQ(
          `SELECT manifest_sha256 FROM mu.skill_version WHERE skill_id=$1 AND version=$2`,
          [sk.rows[0].skill_id, version]);
        if (re.rows.length && re.rows[0].manifest_sha256 === sha) {
          return sendJson(res, 200, { ok: true, idempotent: true, version,
            current_version: await currentOf(), activated: false });
        }
        return sendJson(res, 409, { error: { reason: 'version_immutable_conflict',
          detail: '该版本号已发布且指纹不同——版本发布后不可变，请递增版本号' } });
      }
      // 首个版本发布即激活，但必须走 CAS（仅当此刻仍无 current_version 才写入）：
      // 并发发布多个「首版」时恰有一个赢家激活；输家的版本照常入库，activated=false，
      // 由响应真值 current_version 告知前端实际生效的是谁——绝不双重激活。
      let activated = false;
      if (!sk.rows[0].current_version) {
        const cas = await muPoolQ(
          `UPDATE mu.skill SET current_version=$1, updated_at=now()
            WHERE skill_id=$2 AND current_version IS NULL RETURNING current_version`,
          [version, sk.rows[0].skill_id]);
        activated = cas.rows.length > 0;
      }
      await store.audit('MU_SKILL_VERSION_PUBLISHED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { skill_key: sk.rows[0].skill_key, version } });
      return sendJson(res, 200, { ok: true, idempotent: false, version: vRow,
        current_version: await currentOf(), activated });
    }
    if (skillVerMatch && req.method === 'GET') {
      const g = await guard('read_repository');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const sk = await muPoolQ(`SELECT skill_id FROM mu.skill WHERE tenant_id=$1 AND skill_key=$2`,
        [mu.tenantId, decodeURIComponent(skillVerMatch[1])]);
      if (!sk.rows.length) return sendJson(res, 404, { error: { reason: 'skill_not_found' } });
      const rows = await muPoolQ(
        `SELECT v.version, v.changelog, v.manifest_sha256, v.artifact_ref, v.created_at,
                u.login AS published_by
           FROM mu.skill_version v
           LEFT JOIN mu.app_user u ON u.user_id = v.created_by
          WHERE v.skill_id=$1 ORDER BY v.created_at DESC LIMIT 50`,
        [sk.rows[0].skill_id]);
      return sendJson(res, 200, { versions: rows.rows });
    }
    const skillActivateMatch = p.match(/^\/api\/mu\/skills\/([^/]+)\/activate$/);
    if (skillActivateMatch && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const version = String(body.version || '').trim();
      const sk = await muPoolQ(`SELECT * FROM mu.skill WHERE tenant_id=$1 AND skill_key=$2`,
        [mu.tenantId, decodeURIComponent(skillActivateMatch[1])]);
      if (!sk.rows.length) return sendJson(res, 404, { error: { reason: 'skill_not_found' } });
      const v = await muPoolQ(`SELECT 1 FROM mu.skill_version WHERE skill_id=$1 AND version=$2`,
        [sk.rows[0].skill_id, version]);
      if (!v.rows.length) return sendJson(res, 404, { error: { reason: 'version_not_found' } });
      // 语义定向：切到更低 semver=回滚；切到不同且更高=前向激活；同版=幂等路径
      const semverLt = (a, b) => { const [a1, a2, a3] = a.split('.').map(Number); const [b1, b2, b3] = b.split('.').map(Number);
        return a1 !== b1 ? a1 < b1 : a2 !== b2 ? a2 < b2 : a3 < b3; };
      const prev = sk.rows[0].current_version;
      const rollback = !!prev && prev !== version && semverLt(version, prev);
      // 并发语义（按实际行为，非「并发单赢家」一概而论）：
      //  * 同目标并发：CAS（IS DISTINCT FROM 目标）恰一赢家更新+审计，输家 0 行命中走幂等 200；
      //  * 异目标并发：两个 UPDATE 均命中（指针各自不同），后提交者胜（last-write-wins），各审计一笔。
      const upd = await muPoolQ(
        `UPDATE mu.skill SET current_version=$1, updated_at=now()
          WHERE skill_id=$2 AND current_version IS DISTINCT FROM $1
          RETURNING current_version`,
        [version, sk.rows[0].skill_id]);
      if (upd.rows.length) {
        await store.audit(rollback ? 'MU_SKILL_ROLLED_BACK' : 'MU_SKILL_ACTIVATED',
          { tenantId: mu.tenantId, actorUserId: mu.userId,
            detail: { skill_key: sk.rows[0].skill_key, version, rollback } });
        return sendJson(res, 200, { ok: true, idempotent: false, current_version: version, rollback });
      }
      return sendJson(res, 200, { ok: true, idempotent: true, current_version: version, rollback: false });
    }
    const skillStateMatch = p.match(/^\/api\/mu\/skills\/([^/]+)\/(disable|enable)$/);
    if (skillStateMatch && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const state = skillStateMatch[2] === 'disable' ? 'disabled' : 'active';
      const sk = await muPoolQ(
        `UPDATE mu.skill SET state=$1, updated_at=now()
          WHERE tenant_id=$2 AND skill_key=$3 AND state IS DISTINCT FROM $1
          RETURNING skill_key, state`,
        [state, mu.tenantId, decodeURIComponent(skillStateMatch[1])]);
      if (!sk.rows.length) {
        // 幂等：目标态已达成也须区分「技能不存在」与「已是该态」
        const exist = await muPoolQ(`SELECT state FROM mu.skill WHERE tenant_id=$1 AND skill_key=$2`,
          [mu.tenantId, decodeURIComponent(skillStateMatch[1])]);
        if (!exist.rows.length) return sendJson(res, 404, { error: { reason: 'skill_not_found' } });
        return sendJson(res, 200, { ok: true, idempotent: true, state: exist.rows[0].state });
      }
      await store.audit('MU_SKILL_STATE_CHANGED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { skill_key: sk.rows[0].skill_key, state } });
      return sendJson(res, 200, { ok: true, idempotent: false, state });
    }

    // ── RAG 模型安装控制面（v19；RAG-model-install 波 PR2：下载/校验/取消/状态）──
    // 读（状态/manifest）=成员基座（read_repository）；写（安装/取消/重校验/日志）=
    // manage_instance（platform_admin）+CSRF。租户/模型键全服务端解析；未知模型 404。
    // 审计只落元数据（model_key/manifest_version/字节数/错误码/sha256 前缀/耗时）。
    const rmiStore = await import('./rag-model-install.mjs');
    const rmiDl = await import('./rag-model-download.mjs');
    await muPoolQ('SELECT 1'); // 惰性建 muPool（下方直传 store/引擎用）
    const RMI_ROOT = process.env.RAG_MODEL_ROOT || '/app/rag-models';
    const rmiAudit = (kind, detail) => store.audit(`RAG_MODEL_${kind}`, { tenantId: mu.tenantId, actorUserId: mu.userId, detail }).catch(() => {});

    if (p === '/api/mu/rag-model/install' && req.method === 'GET') {
      const g = await guard('read_repository');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const key = String(q.model_key || 'bge-m3');
      if (!rmiStore.listInstallableModels().includes(key)) {
        return sendJson(res, 404, { error: { reason: 'model_not_found' } });
      }
      const { row } = await rmiStore.ensureInstallRow(muPool, { tenantId: mu.tenantId, modelKey: key });
      return sendJson(res, 200, { install: {
        model_key: row.model_key, state: row.state, active_provider: row.active_provider,
        manifest_version: row.manifest_version, revision: row.revision, license: row.license,
        expected_files: row.expected_files, total_bytes: Number(row.total_bytes),
        downloaded_bytes: Number(row.downloaded_bytes), activated_at: row.activated_at,
        last_error_code: row.last_error_code, updated_at: row.updated_at,
        engine_busy: rmiDl.isInstalling(mu.tenantId, key) } });
    }
    if (p === '/api/mu/rag-model/manifest' && req.method === 'GET') {
      const g = await guard('read_repository');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const key = String(q.model_key || 'bge-m3');
      try { return sendJson(res, 200, { manifest: rmiStore.loadModelManifest(key) }); }
      catch { return sendJson(res, 404, { error: { reason: 'model_not_found' } }); }
    }
    if (p === '/api/mu/rag-model/install' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const key = String((await json()).model_key || 'bge-m3');
      if (!rmiStore.listInstallableModels().includes(key)) {
        return sendJson(res, 404, { error: { reason: 'model_not_found' } });
      }
      if (rmiDl.isInstalling(mu.tenantId, key)) {
        return sendJson(res, 409, { error: { reason: 'install_in_progress' } });
      }
      const cur = (await rmiStore.getInstall(muPool, { tenantId: mu.tenantId, modelKey: key }))?.state;
      if (cur === 'ACTIVE') {
        return sendJson(res, 409, { error: { reason: 'illegal_state', detail: 'ACTIVE——先回退再重装' } });
      }
      // 后台 kickoff（立即返回 DOWNLOADING；进度经 GET status/心跳落库）
      rmiDl.runInstall({ pool: muPool, tenantId: mu.tenantId, modelKey: key,
        manifest: rmiStore.loadModelManifest(key), modelRoot: RMI_ROOT,
        storeMod: rmiStore, onEvent: (kind, detail) => rmiAudit(kind, detail) })
        .catch(() => {});
      return sendJson(res, 202, { ok: true, state: 'DOWNLOADING', model_key: key });
    }
    if (p === '/api/mu/rag-model/install/verify' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const key = String((await json()).model_key || 'bge-m3');
      if (!rmiStore.listInstallableModels().includes(key)) {
        return sendJson(res, 404, { error: { reason: 'model_not_found' } });
      }
      const r = await rmiDl.runVerifyOnly({ pool: muPool, tenantId: mu.tenantId, modelKey: key,
        manifest: rmiStore.loadModelManifest(key), modelRoot: RMI_ROOT, storeMod: rmiStore,
        onEvent: (kind, detail) => rmiAudit(kind, detail) });
      if (!r.ok && r.reason?.startsWith('illegal_state')) return sendJson(res, 409, { error: { reason: r.reason } });
      return sendJson(res, r.ok ? 200 : 422, r);
    }
    if (p === '/api/mu/rag-model/install/cancel' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const key = String((await json()).model_key || 'bge-m3');
      const r = await rmiDl.cancelInstall({ pool: muPool, tenantId: mu.tenantId, modelKey: key,
        storeMod: rmiStore, onEvent: (kind, detail) => rmiAudit(kind, detail) });
      return sendJson(res, r.ok ? 200 : 404, r);
    }
    if (p === '/api/mu/rag-model/activate' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const key = String(body.model_key || 'bge-m3');
      if (!rmiStore.listInstallableModels().includes(key)) {
        return sendJson(res, 404, { error: { reason: 'model_not_found' } });
      }
      await rmiStore.ensureInstallRow(muPool, { tenantId: mu.tenantId, modelKey: key });
      const registryMod = await import('../ragtrial/store.mjs');
      const actMod = await import('./rag-model-activate.mjs');
      const r = await actMod.activateModel({ pool: muPool, tenantId: mu.tenantId, modelKey: key,
        manifest: rmiStore.loadModelManifest(key), modelRoot: RMI_ROOT, storeMod: rmiStore,
        registryMod, endpoint: env.RAGTRIAL_EMBED_ENDPOINT || null,
        onEvent: (kind, detail) => rmiAudit(kind, detail) });
      if (!r.ok && r.http) return sendJson(res, r.http, { error: { reason: r.reason, detail: r.detail ?? r.hint } });
      return sendJson(res, 200, r);
    }
    if (p === '/api/mu/rag-model/rollback' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const key = String((await json()).model_key || 'bge-m3');
      const registryMod = await import('../ragtrial/store.mjs');
      const actMod = await import('./rag-model-activate.mjs');
      const r = await actMod.rollbackToLocal({ pool: muPool, tenantId: mu.tenantId, modelKey: key,
        storeMod: rmiStore, registryMod, onEvent: (kind, detail) => rmiAudit(kind, detail) });
      return sendJson(res, r.ok ? 200 : r.http ?? 500, r);
    }
    if (p === '/api/mu/rag-model/install/log' && req.method === 'GET') {
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rows = await muPoolQ(
        `SELECT kind, detail, created_at FROM mu.audit_event
          WHERE tenant_id=$1 AND kind LIKE 'RAG_MODEL_%' ORDER BY seq DESC LIMIT 30`, [mu.tenantId]);
      return sendJson(res, 200, { entries: rows.rows, in_flight: rmiDl.installLogSummary() });
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

    // ── 成员（读=read_audit：审计需要成员-角色映射，contributor 不放行；写=manage_membership） ──
    if (p === '/api/mu/members' && req.method === 'GET') {
      const g = await guard('read_audit');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
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
      // v18 绑定统一（审计 E-2）：演示/登记路径经 store.ensureBinding 直写 mu.repository_binding
      // （kind='fixture' 以 fixture 保留 id 区间等价表达，见 schema.mjs）；body.installation_id/
      // granted_scopes 形参保留兼容但不再落库（授权快照仅审计留痕）。真实绑定以 ghapp-binding/
      // 安装回调/webhook 为权威；mu.binding 自 v18 冻结为老镜像只读兼容域。
      const installationId = body.installation_id ? String(body.installation_id)
        : `fixture-install-${crypto.randomBytes(6).toString('hex')}`;
      const grantedScopes = Array.isArray(body.granted_scopes) && body.granted_scopes.length
        ? body.granted_scopes.map(String)
        : ['pull_requests:read', 'contents:read']; // 最小权限默认快照（设计报告 §6）
      const binding = await store.ensureBinding({ tenantId: mu.tenantId, repoId: repo.repo_id,
        kind, installationId, grantedScopes, createdBy: mu.userId });
      await store.audit('MU_REPO_BOUND', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { repo: `${owner}/${name}`, kind, installation_prefix: String(installationId).slice(0, 12),
          binding_installation_id: binding?.installation_id ?? null, fixture_domain: true,
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
      // 审计 E-1：编号寻址消费 ?repo_id= ——带则先经 tenant 收窄把 PR 限定在该 repo
      // 内（repo 越界/PR 不在该 repo/PR 不存在 同为 404 pull_request_not_found，
      // 不暴露跨 tenant 存在性）；不带保持旧行为（webhook/内部调用向后兼容）。
      const pr = await store.resolvePullRequest(mu.tenantId, prMatch[1],
        { repoId: q.repo_id ? String(q.repo_id) : null });
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
        // ADR-002 PR F：最新 run 的分立结果列（review/verification/tests/merge_eligibility
        // 互不冒充；legacy run 零 v2 列→前端按"未运行"呈现，不猜值）
        const latestRun = (await muPoolQ(
          `SELECT run_id, status, architecture_version, review_mode, execution_mode,
                  review_verdict, verification_verdict, tests_status, merge_eligibility, code_egress
             FROM mu.review_run WHERE tenant_id=$1 AND pr_id=$2
            ORDER BY created_at DESC LIMIT 1`, [mu.tenantId, pr.pr_id])).rows[0] ?? null;
        // Verifier 双域原始留痕（三.3 分显）：模型 attempt 判定+工具证据（evidence_ref）
        const vaRow = latestRun ? (await muPoolQ(
          `SELECT verdict, evidence_ref FROM mu.verification_attempt WHERE run_id=$1
            ORDER BY created_at DESC LIMIT 1`, [latestRun.run_id])).rows[0] ?? null : null;
        return sendJson(res, 200, { pull_request: pr, review_records: records,
          latest_run: latestRun ? {
            run_id: latestRun.run_id, status: latestRun.status,
            architecture_version: latestRun.architecture_version ?? null,
            review_mode: latestRun.review_mode ?? null,
            execution_mode: latestRun.execution_mode ?? null,
            review_verdict: latestRun.review_verdict ?? null,
            verification_verdict: latestRun.verification_verdict ?? null,
            tests_status: latestRun.tests_status ?? null,
            merge_eligibility: latestRun.merge_eligibility ?? null,
            code_egress: Number(latestRun.code_egress ?? 0),
            model_judgment: vaRow ? { verdict: vaRow.verdict,
              input: 'digest_only', note: '模型未读取完整 patch——原始判定仅审计留痕' } : null,
            test_evidence: vaRow?.evidence_ref ?? null } : null,
          my_permissions: { actions: roleActions(liveMembership.role) ?? [] } });
      }
      if (prMatch[2] === 'fix-approvals' && req.method === 'GET') {
        // v16 审批门：该 PR 的高危审批票（rc3 试用修复：按 pr_id 收窄——原按 repo 过滤
        // 会把同仓库其他 PR 的票串页显示；read_pull_request 即可见，决定动作另需 decide_review）
        const g = await guard('read_pull_request', { repoId: pr.repo_id });
        if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
        const { sweepFixApprovals } = await import('./fix-approval.mjs');
        await sweepFixApprovals({ query: muPoolQ }).catch(() => {});
        const rows = await muPoolQ(
          `SELECT fa.approval_id, fa.run_id, fa.finding_id, fa.severity, fa.status,
                  fa.head_sha, fa.created_at, fa.expires_at, fa.decided_by, fa.decided_at,
                  fa.decision_reason, fa.requested_action, fa.pr_number,
                  f.rule_id, f.path, f.line_start, f.summary_masked, rr.status AS run_status
             FROM mu.fix_approval fa
             JOIN mu.agent_finding f ON f.finding_id = fa.finding_id
             JOIN mu.review_run rr ON rr.run_id = fa.run_id
            WHERE fa.tenant_id=$1 AND fa.repo_id=$2 AND fa.pr_id=$3
            ORDER BY fa.created_at DESC LIMIT 100`, [mu.tenantId, pr.repo_id, pr.pr_id]);
        return sendJson(res, 200, { fix_approvals: rows.rows, tenant_scope: 'self' });
      }
      return sendJson(res, 404, { error: { reason: 'unknown pr subpath' } });
    }

    // ── v16 高危修复审批门：审批票读/决定（任务书四节）──
    // 创建：仅 Leader 服务端（ensureFixApprovals）——本端点只读/决定；
    // 决定：decide_review（仅 maintainer）+ CSRF；跨租户 404 防枚举。
    if (p === '/api/mu/approvals' && req.method === 'GET') {
      const g = await guard('read_pull_request');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const { sweepFixApprovals, listFixApprovals } = await import('./fix-approval.mjs');
      const faPool = { query: muPoolQ };
      await sweepFixApprovals(faPool).catch(() => {});
      const status = ['PENDING','APPROVED','REJECTED','EXPIRED','STALE','CONSUMED']
        .includes(String(q.status ?? '')) ? String(q.status) : null;
      const rows = await listFixApprovals(faPool, { tenantId: mu.tenantId, status,
        runId: q.run_id ? String(q.run_id) : null,
        repoId: q.repo_id ? String(q.repo_id) : null,
        prId: q.pr_id ? String(q.pr_id) : null, limit: q.limit ?? 100 });
      return sendJson(res, 200, { approvals: rows, tenant_scope: 'self' });
    }
    const apprMatch = p.match(/^\/api\/mu\/approvals\/([0-9a-fA-F-]{8,64})$/);
    if (apprMatch && req.method === 'GET') {
      const g = await guard('read_pull_request');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const { sweepFixApprovals, listFixApprovals } = await import('./fix-approval.mjs');
      const faPool = { query: muPoolQ };
      await sweepFixApprovals(faPool).catch(() => {});
      const rows = await listFixApprovals(faPool, { tenantId: mu.tenantId, limit: 200 });
      const t = rows.find((x) => x.approval_id === apprMatch[1]) ?? null;
      if (!t) return sendJson(res, 404, { error: { reason: 'approval_not_found' } });
      return sendJson(res, 200, { approval: t });
    }
    const apprDecide = p.match(/^\/api\/mu\/approvals\/([0-9a-fA-F-]{8,64})\/(approve|reject)$/);
    if (apprDecide && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      // 决定权：decide_review（authz 矩阵仅 maintainer——contributor/reviewer/auditor/
      // platform_admin 均无此动作；PlatformAdmin 不能绕过租户授权）
      const g = await guard('decide_review');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const { decideFixApproval } = await import('./fix-approval.mjs');
      const faPool = { query: muPoolQ };
      const dec = await decideFixApproval(faPool, {
        approvalId: apprDecide[1], decision: apprDecide[2],
        decidedBy: `mu:${mu.login}`, decisionReason: body?.reason ?? null,
        tenantId: mu.tenantId });
      if (dec.reason === 'not_found') return sendJson(res, 404, { error: { reason: 'approval_not_found' } });
      if (!dec.ok) return sendJson(res, 409, { error: { reason: 'approval_state_conflict',
        status: dec.status ?? null } });
      // 全部批准→ 内联启动受控 DRY_RUN 修复轮（与 webhook 驱动同一
      // fixVerifyRound；失败不冒充——run 停留 FIX_QUEUED 可恢复，如实回报）
      let fixRound = null;
      if (dec.run_ready) {
        try {
          const t = dec.ticket;
          const dep = await buildFixDepsForRepo(muPoolQ, { tenantId: mu.tenantId, repoId: t.repo_id,
            prNumber: Number(t.pr_number) });
          if (dep) {
            const { fixVerifyRound } = await import('./agents/fix-orchestrator.mjs');
            const fx = await fixVerifyRound({ query: muPoolQ }, {
              run: { run_id: t.run_id },
              binding: { tenantId: t.tenant_id, repoId: t.repo_id, prId: t.pr_id, headSha: t.head_sha },
              deps: dep });
            fixRound = fx.ok
              ? { verdict: fx.verdict ?? null, decision: fx.decision ?? null, executor: fx.executor ?? 'internal' }
              : { skipped: fx.stage ?? 'fix_failed', reason: fx.reason ?? null };
          } else { fixRound = { skipped: 'deps_unavailable' }; }
        } catch (e) {
          fixRound = { skipped: 'fix_round_error', reason: String(e?.message ?? e).slice(0, 120) };
        }
      }
      return sendJson(res, 200, { ok: true, idempotent: Boolean(dec.idempotent),
        ticket: dec.ticket, run_state: dec.run_state ?? dec.ticket.run_status ?? null,
        run_ready: Boolean(dec.run_ready), run_blocked: Boolean(dec.run_blocked),
        fix_round: fixRound,
        note: '批准仅允许生成 DRY_RUN 修复建议——不写 GitHub、不自动合并、branch protection 保持有效' });
    }

    // ── 审查触发（reviewer+；只读审查 job） ──
    if (prMatch && prMatch[2] === 'review' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      // E-1 同族：写子路径编号寻址同样消费 ?repo_id=（缺省旧行为；见 GET 分支注释）
      const pr = await store.resolvePullRequest(mu.tenantId, prMatch[1],
        { repoId: q.repo_id ? String(q.repo_id) : null });
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
      const pr = await store.resolvePullRequest(mu.tenantId, prMatch[1],
        { repoId: q.repo_id ? String(q.repo_id) : null }); // E-1 同族（见 GET 分支注释）
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
      const pr = await store.resolvePullRequest(mu.tenantId, prMatch[1],
        { repoId: q.repo_id ? String(q.repo_id) : null }); // E-1 同族（见 GET 分支注释）
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
const ragMatch = p.match(new RegExp("^/api/mu/repositories/([^/]+)/rag-search$"));
    if (ragMatch && req.method === 'GET') {
      const g = await guard('rag_query', { repoId: ragMatch[1] });
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      // ── C1 RAG 检索留痕：query 只以 64-hex digest 入库（原文零落库/零日志）；
      // 命中源以稳定 digest 数组留痕；用户会话域无 run 上下文——pr/run 不虚构（NULL）。
      // 幂等键=(tenant,repo,query digest)——同租户同库同查询收敛为一条（重试语义）。
      const recRag = await import('./invocation-recorder.mjs');
      const t0Rag = Date.now();
      const ragResp = fixtureRagSearch(g.repo, q.q ?? '');
      const ragDigest = recRag.sha256Hex(q.q ?? '');
      const ragRec = await recRag.recordRagRetrieval({ query: muPoolQ }, {
        tenantId: mu.tenantId, repoId: g.repo.repo_id, agentRole: 'system',
        skillKey: 'rag.retrieve', queryDigest: ragDigest,
        resultCount: Array.isArray(ragResp.results) ? ragResp.results.length : 0,
        sourceDigestList: (ragResp.results ?? []).map((h) => recRag.sha256Hex(h.doc_path)),
        status: 'SUCCEEDED', latencyMs: Date.now() - t0Rag,
        idempotencyKey: `rag:${mu.tenantId}:${g.repo.repo_id}:${ragDigest}` });
      if (!ragRec.ok && ragRec.code === 'skill_inactive') {
        // 租户显式停用 rag.retrieve → fail-closed（未注册租户零行为变化）
        return sendJson(res, 403, { error: { reason: 'skill_inactive' } });
      }
      return sendJson(res, 200, ragResp);
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
      // Wave 3.8 队首阻塞修复（HOL，验收裁决 BLOCKED_BY_QUEUE_HEAD_OF_LINE）：
      //  * 非 fixture 模式只领取 event_sync（claim 层隔离）——人工/不可处理 job
      //    结构上不可能阻塞 webhook 消费，requeue+break 已废除；
      //  * 每 tick 有限预算（防长 tick 独占；跳过后继续扫描后续可处理 job）；
      //  * tick 开端回收孤立 running（worker 崩溃遗留 lease）；
      //  * 无可领取 event_sync 时，有限清收过期人工 job → rejected 终态+审计
      //    （reason=manual_job_expired_unconsumable；未过期人工 job 留队等待专用消费者）。
      const TICK_BUDGET = 25;
      const ORPHAN_MINUTES = 10;
      const MANUAL_EXPIRY_HOURS = 24;
      const processed = [];
      {
        const orphans = await store.requeueOrphanedJobs({ staleMinutes: ORPHAN_MINUTES, limit: 10 }).catch(() => []);
        for (const o of orphans) {
          await store.audit('MU_JOB_REQUEUED_ORPHAN', { tenantId: o.tenant_id, actorUserId: null,
            detail: { job_id: o.job_id, kind: o.kind, reason: 'orphan_running_lease_recovered' } });
          processed.push({ job_id: o.job_id, state: 'requeued', reason: 'orphan_running_lease_recovered' });
        }
      }
      let budget = TICK_BUDGET;
      for (;;) {
        if (budget <= 0) break; // 每 tick 有限预算：耗尽即停（余量留给下轮 tick）
        const job = await store.claimNextJob(fixturesOn ? {} : { kinds: ['event_sync'] });
        if (!job) {
          if (!fixturesOn) {
            const reaped = await store.rejectStaleManualJobs({ expiryHours: MANUAL_EXPIRY_HOURS, limit: 10 }).catch(() => []);
            if (reaped.length) {
              for (const j of reaped) {
                await store.audit('MU_JOB_REJECTED', { tenantId: j.tenant_id, actorUserId: null,
                  detail: { job_id: j.job_id, kind: j.kind, reason: 'manual_job_expired_unconsumable' } });
                processed.push({ job_id: j.job_id, state: 'rejected', reason: 'manual_job_expired_unconsumable' });
              }
              continue; // 清收后回头再领取（预算内）
            }
          }
          break;
        }
        budget--;
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
    // ── AgentTeams 运行状态（Wave 3.4；只读探测；脱敏——无 token/prompt/正文）──
    if (p === '/api/mu/agentteams-status' && req.method === 'GET') {
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const atMod = await import('./agents/agentteams-executor.mjs');
      const mtMod = await import('./agents/matrix-transport.mjs');
      const cfg = atMod.resolveAgentTeamsConfig(env);
      const mtCfg = mtMod.resolveMatrixConfig(env);
      const out = { executor: atMod.executorStatusSummary(env),
        controller: null, matrix_transport: mtCfg.kind === 'matrix' ? 'configured' : mtCfg.reason,
        workers_ready: 0, workers_total: 4, last_stable_reason: null };
      if (cfg.kind === 'agentteams') {
        const probe = { ...cfg, timeout: Math.min(cfg.timeout, 3_000) };
        const h = await atMod.agentTeamsHealthy(probe).catch(() => ({ ok: false, reason: 'AT_PROBE_FAILED' }));
        out.controller = h.ok ? 'reachable' : h.reason;
        if (h.ok) {
          const d = await atMod.listWorkersDetail(probe).catch(() => null);
          if (d?.ok) {
            const four = Object.values(atMod.AGENTTEAMS_WORKERS).map((w) => d.workers.get(w.name)).filter(Boolean);
            out.workers_ready = four.filter((w) => w.phase === 'Running' && w.roomID && w.matrixUserID).length;
          }
        }
        const dlq = await muPoolQ(`SELECT reason, created_at FROM mu.dead_letter
          WHERE kind IN ('mt_round_failed','at_round_failed','at_round_crashed') ORDER BY created_at DESC LIMIT 1`).catch(() => null);
        if (dlq?.rows?.length) out.last_stable_reason = String(dlq.rows[0].reason ?? '').slice(0, 60);
        const dlqN = await muPoolQ(`SELECT count(*) c FROM mu.dead_letter
          WHERE kind IN ('mt_round_failed','at_round_failed','at_round_crashed') AND resolved_at IS NULL`).catch(() => null);
        out.dead_letter_open = Number(dlqN?.rows?.[0]?.c ?? 0);
        const pend = await muPoolQ(`SELECT count(*) c FROM mu.job WHERE state='queued'`).catch(() => null);
        out.queue_backlog = Number(pend?.rows?.[0]?.c ?? 0);
        const tmo = await muPoolQ(`SELECT count(*) c FROM mu.agent_attempt
          WHERE provider='agentteams' AND status='FAILED' AND created_at > now() - interval '24 hours'`).catch(() => null);
        out.agentteams_attempts_failed_24h = Number(tmo?.rows?.[0]?.c ?? 0);
        const okN = await muPoolQ(`SELECT count(*) c, max(created_at) latest FROM mu.agent_attempt
          WHERE provider='agentteams' AND status='DONE'`).catch(() => null);
        out.agentteams_attempts_done = Number(okN?.rows?.[0]?.c ?? 0);
        out.last_success_at = okN?.rows?.[0]?.latest ?? null;
      }
      const st = out.executor.mode === 'agentteams' && out.controller === 'reachable' && out.workers_ready === 4
        ? (out.dead_letter_open > 0 ? 'degraded' : 'active')
        : out.executor.mode === 'agentteams' ? 'unavailable_fail_closed'
        : out.executor.mode === 'internal' ? 'internal_non_production' : 'not_configured_fail_closed';
      return sendJson(res, 200, { ...out, runtime_state: st });
    }
    // ── ADR-002 PR A：review architecture v2 控制面（三档/Provider/consent/snapshot）──
    // 权限：读=read_repository（租户成员可见——解释 run 模式需要）；写=manage_instance
    // （组织管理员等价——与 agent-policy 同权面，contributor/reviewer/auditor 不越权）。
    // v2 flag 关闭时：策略面只读返回（不可写），health 披露架构版本——beta.5 行为零变化。
    const archV2 = env.MU_REVIEW_ARCH === 'v2';
    if (p === '/api/mu/review-policy' && req.method === 'GET') {
      const g = await guard('read_repository');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rps = await import('./review-policy-store.mjs');
      const rpStore = rps.createReviewPolicyStore({ pool: { query: muPoolQ } });
      const policy = await rpStore.getPolicy(mu.tenantId);
      return sendJson(res, 200, {
        policy: { ...policy, architecture_version: 'v2' },
        arch_enabled: archV2, // false=控制面只读（beta.5 兼容态）
        modes: ['evidence_only', 'external_api', 'local'],
      });
    }
    if (p === '/api/mu/review-policy' && (req.method === 'PUT' || req.method === 'PATCH')) {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      if (!archV2) return sendJson(res, 409, { error: { reason: 'arch_v2_not_enabled' } });
      const body = await json();
      const rps = await import('./review-policy-store.mjs');
      const rpStore = rps.createReviewPolicyStore({ pool: { query: muPoolQ } });
      const r = await rpStore.updatePolicy(mu.tenantId, mu.userId, body, {
        expectedVersion: Number(body?.expected_policy_version ?? body?.policy_version ?? 0) });
      if (!r.ok) {
        const status = r.code === 'version_conflict' ? 409 : 422;
        return sendJson(res, status, { error: { reason: r.code, ...(r.current ? { current_policy_version: r.current.policy_version } : {}) } });
      }
      await store.audit('REVIEW_POLICY_UPDATED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { policy_version: r.policy.policy_version, review_mode: r.policy.review_mode,
          provider_id: r.policy.provider_id ?? null } });
      return sendJson(res, 200, { policy: r.policy });
    }
    if (p === '/api/mu/review-policy/history' && req.method === 'GET') {
      const g = await guard('read_audit');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rps = await import('./review-policy-store.mjs');
      const rpStore = rps.createReviewPolicyStore({ pool: { query: muPoolQ } });
      const rows = await rpStore.listPolicyHistory(mu.tenantId, Number(q.limit) || 50);
      return sendJson(res, 200, { revisions: rows });
    }
    if (p === '/api/mu/providers' && req.method === 'GET') {
      const g = await guard('read_repository');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rps = await import('./review-policy-store.mjs');
      const rpStore = rps.createReviewPolicyStore({ pool: { query: muPoolQ } });
      const providers = await rpStore.listProviders();
      // 脱敏投影：endpoint_origin 仅 host（不含 path/scheme 细节）；零凭据列本就不存在
      return sendJson(res, 200, { providers: providers.map((pr) => ({
        provider_id: pr.provider_id, display_name: pr.display_name,
        endpoint_origin: pr.endpoint_origin, policy_status: pr.policy_status,
        retention_summary: pr.retention_summary, training_summary: pr.training_summary,
        region_summary: pr.region_summary, policy_reference: pr.policy_reference,
        state: pr.state, reviewed_at: pr.reviewed_at })) });
    }
    const psMatch = p.match(/^\/api\/mu\/runs\/([0-9a-f-]{36})\/policy-snapshot$/);
    if (psMatch && req.method === 'GET') {
      const g = await guard('read_pull_request');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const rr = await muPoolQ(
        `SELECT architecture_version, review_mode, review_scope, execution_mode,
                provider_id, model_id, provider_policy_status, code_egress,
                consent_version, policy_snapshot_digest, tenant_id, head_sha
           FROM mu.review_run WHERE run_id=$1`, [psMatch[1]]);
      if (!rr.rows.length || String(rr.rows[0].tenant_id) !== String(mu.tenantId)) {
        return sendJson(res, 404, { error: { reason: 'run_not_found' } }); // 防枚举：跨租户同 404
      }
      const row = rr.rows[0];
      if (!row.architecture_version) {
        return sendJson(res, 200, { snapshot: null, note: 'legacy_run_no_snapshot' }); // 历史 run 不冒充 v2
      }
      return sendJson(res, 200, { snapshot: {
        architecture_version: row.architecture_version, review_mode: row.review_mode,
        review_scope: row.review_scope, execution_mode: row.execution_mode,
        provider_id: row.provider_id, model_id: row.model_id,
        provider_policy_status: row.provider_policy_status,
        code_egress: Number(row.code_egress ?? 0) > 0,
        consent_version: row.consent_version, snapshot_digest: row.policy_snapshot_digest,
        head_sha: String(row.head_sha ?? '').slice(0, 12) } });
    }
    if (p === '/api/mu/review-policy/consent' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      if (!archV2) return sendJson(res, 409, { error: { reason: 'arch_v2_not_enabled' } });
      const body = await json();
      const rps = await import('./review-policy-store.mjs');
      const rpStore = rps.createReviewPolicyStore({ pool: { query: muPoolQ } });
      const r = await rpStore.acceptConsent(mu.tenantId, mu.userId, {
        providerId: String(body?.provider_id ?? ''), consentVersion: String(body?.consent_version ?? ''),
        acknowledgementDigest: String(body?.acknowledgement_digest ?? ''),
        policyVersion: Number(body?.policy_version ?? 1) });
      if (!r.ok) return sendJson(res, 422, { error: { reason: r.code } });
      await store.audit('REVIEW_CONSENT_ACCEPTED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { provider_id: body?.provider_id, consent_version: body?.consent_version } });
      return sendJson(res, 200, { ok: true });
    }
    if (p === '/api/mu/review-policy/consent/revoke' && req.method === 'POST') {
      if (!csrfOk()) return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      const g = await guard('manage_instance');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const body = await json();
      const rps = await import('./review-policy-store.mjs');
      const rpStore = rps.createReviewPolicyStore({ pool: { query: muPoolQ } });
      await rpStore.revokeConsent(mu.tenantId, mu.userId, String(body?.provider_id ?? ''));
      await store.audit('REVIEW_CONSENT_REVOKED', { tenantId: mu.tenantId, actorUserId: mu.userId,
        detail: { provider_id: body?.provider_id } });
      return sendJson(res, 200, { ok: true });
    }
    // ── ADR-002 PR F：出站披露（code_egress_event 只读投影——digest/计数，零正文）──
    // 权限：read_audit（auditor+）——租户收窄；可选 run_id 过滤（run 归属校验同上防枚举）。
    if (p === '/api/mu/egress-events' && req.method === 'GET') {
      const g = await guard('read_audit');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const limit = Math.min(Math.max(Number(q.limit) || 50, 1), 200);
      const params = [mu.tenantId, limit];
      let runFilter = '';
      if (q.run_id) {
        const own = await muPoolQ(
          `SELECT 1 FROM mu.review_run WHERE run_id=$1 AND tenant_id=$2`, [String(q.run_id), mu.tenantId]);
        if (!own.rows.length) return sendJson(res, 404, { error: { reason: 'run_not_found' } });
        params.splice(1, 0, String(q.run_id));
        runFilter = 'AND e.run_id=$2';
      }
      const rows = await muPoolQ(
        `SELECT e.event_id, e.run_id, e.provider_id, e.model_id, e.head_sha, e.input_digest,
                e.response_digest, e.files, e.bytes_sent, e.tokens_sent, e.redactions_applied,
                e.policy_version, e.consent_version, e.timeout, e.retry_count, e.created_at,
                a.agent_role, a.provider AS attempt_provider, a.status AS attempt_status
           FROM mu.code_egress_event e
           LEFT JOIN mu.agent_attempt a ON a.attempt_id = e.attempt_id
          WHERE e.tenant_id=$1 ${runFilter}
          ORDER BY e.event_id DESC LIMIT $${params.length}`, params);
      return sendJson(res, 200, { events: (rows.rows ?? []).map((e) => ({
        event_id: e.event_id, run_id: e.run_id,
        agent_role: e.agent_role ?? null, attempt_provider: e.attempt_provider ?? null,
        attempt_status: e.attempt_status ?? null,
        provider_id: e.provider_id, model_id: e.model_id,
        head_sha: String(e.head_sha ?? '').slice(0, 12),
        input_digest: e.input_digest, response_digest: e.response_digest,
        files: Array.isArray(e.files) ? e.files : [], // 文件名清单（路径非正文——披露"哪些文件被送出"）
        bytes_sent: Number(e.bytes_sent ?? 0), tokens_sent: Number(e.tokens_sent ?? 0),
        redactions_applied: Number(e.redactions_applied ?? 0),
        policy_version: e.policy_version, consent_version: e.consent_version,
        timeout: Boolean(e.timeout), retry_count: Number(e.retry_count ?? 0),
        created_at: e.created_at })) });
    }

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

    // ── C1 调用留痕只读 API（复用 runs/:id 同一会话+tenant guard；跨租户/不存在同形 404）──
    // 响应白名单字段（与 recorder 同一投影函数）：绝无 prompt/query 原文/文档正文/
    // 幂等键。legacy run（v19 迁移应用前创建）不可能留痕——200 + not_available（不伪造空集）。
    const runEventsMatch = p.match(/^\/api\/mu\/runs\/([0-9a-f-]{36})\/(skill-invocations|rag-retrievals|call-summary)$/);
    if (runEventsMatch && req.method === 'GET') {
      const g = await guard('read_pull_request');
      if (g.denied) return sendJson(res, g.denied.status, g.denied.body);
      const recApi = await import('./invocation-recorder.mjs');
      const runIdQ = runEventsMatch[1];
      const view = runEventsMatch[2];
      const runRow = (await muPoolQ(
        `SELECT run_id, created_at FROM mu.review_run WHERE run_id=$1 AND tenant_id=$2`,
        [runIdQ, mu.tenantId])).rows[0];
      if (!runRow) return sendJson(res, 404, { error: { reason: 'not_found' } });
      const v19At = (await muPoolQ(
        `SELECT applied_at FROM mu.schema_migrations WHERE version=19`)).rows[0]?.applied_at ?? null;
      const isLegacy = v19At != null
        && new Date(runRow.created_at).getTime() < new Date(v19At).getTime();
      // 过滤参数（服务端枚举白名单——非法值 400，不静默吞）
      const filters = {};
      const badFields = [];
      if (q.agent_role) {
        if (recApi.AGENT_ROLES.includes(q.agent_role)) filters.agent_role = q.agent_role;
        else badFields.push('agent_role');
      }
      if (q.skill_key) {
        if (/^[a-z0-9][a-z0-9._-]{0,63}$/.test(String(q.skill_key))) filters.skill_key = String(q.skill_key);
        else badFields.push('skill_key');
      }
      if (q.status) {
        if (recApi.INVOCATION_STATUSES.includes(q.status)) filters.status = q.status;
        else badFields.push('status');
      }
      if (q.invocation_kind) {
        if (recApi.INVOCATION_KINDS.includes(q.invocation_kind)) filters.invocation_kind = q.invocation_kind;
        else badFields.push('invocation_kind');
      }
      if (badFields.length) {
        return sendJson(res, 400, { error: { reason: 'invalid_filter', fields: badFields } });
      }
      const limit = Math.min(Math.max(Number(q.limit) || 50, 1), 200);
      const offset = Math.max(Number(q.offset) || 0, 0);
      const legacyBody = { run: { run_id: runIdQ, legacy: true },
        items: [], total: 0, not_available: true };
      if (view === 'skill-invocations') {
        if (isLegacy) return sendJson(res, 200, legacyBody);
        const where = ['run_id=$1'];
        const params = [runIdQ];
        for (const k of ['agent_role', 'skill_key', 'status', 'invocation_kind']) {
          if (filters[k]) { params.push(filters[k]); where.push(`${k}=$${params.length}`); }
        }
        const rows = (await muPoolQ(
          `SELECT event_id, agent_role, skill_key, skill_version, invocation_kind, status,
                  started_at, completed_at, latency_ms, input_digest, output_digest, error_code
             FROM mu.skill_invocation_event WHERE ${where.join(' AND ')}
            ORDER BY started_at, event_id LIMIT ${limit} OFFSET ${offset}`, params)).rows;
        const total = Number((await muPoolQ(
          `SELECT count(*)::int c FROM mu.skill_invocation_event WHERE ${where.join(' AND ')}`,
          params)).rows[0].c);
        return sendJson(res, 200, { run: { run_id: runIdQ },
          items: rows.map((r) => recApi.projectSkillEvent(r)), total });
      }
      if (view === 'rag-retrievals') {
        if (isLegacy) return sendJson(res, 200, legacyBody);
        const where = ['run_id=$1'];
        const params = [runIdQ];
        for (const k of ['agent_role', 'skill_key', 'status']) {
          if (filters[k]) { params.push(filters[k]); where.push(`${k}=$${params.length}`); }
        }
        const rows = (await muPoolQ(
          `SELECT event_id, agent_role, skill_key, status, started_at, completed_at,
                  latency_ms, error_code, query_digest, result_count, source_digest_list
             FROM mu.rag_retrieval_event WHERE ${where.join(' AND ')}
            ORDER BY started_at, event_id LIMIT ${limit} OFFSET ${offset}`, params)).rows;
        const total = Number((await muPoolQ(
          `SELECT count(*)::int c FROM mu.rag_retrieval_event WHERE ${where.join(' AND ')}`,
          params)).rows[0].c);
        return sendJson(res, 200, { run: { run_id: runIdQ },
          items: rows.map((r) => recApi.projectRagEvent(r)), total });
      }
      // call-summary（仅计数；同过滤器）
      const sumWhere = ['run_id=$1'];
      const sumParams = [runIdQ];
      for (const k of ['agent_role', 'skill_key', 'status']) {
        if (filters[k]) { sumParams.push(filters[k]); sumWhere.push(`${k}=$${sumParams.length}`); }
      }
      if (filters.invocation_kind) {
        sumParams.push(filters.invocation_kind);
        sumWhere.push(`invocation_kind=$${sumParams.length}`);
      }
      const sBy = (await muPoolQ(
        `SELECT status, agent_role, count(*)::int c FROM mu.skill_invocation_event
          WHERE ${sumWhere.join(' AND ')} GROUP BY status, agent_role`, sumParams)).rows;
      const rBy = (await muPoolQ(
        `SELECT status, count(*)::int c FROM mu.rag_retrieval_event
          WHERE ${sumWhere.join(' AND ')} GROUP BY status`, sumParams)).rows;
      const skill = { total: sBy.reduce((a, b) => a + Number(b.c), 0),
        by_status: {}, by_role: {} };
      for (const r of sBy) {
        skill.by_status[r.status] = (skill.by_status[r.status] ?? 0) + Number(r.c);
        skill.by_role[r.agent_role] = (skill.by_role[r.agent_role] ?? 0) + Number(r.c);
      }
      const rag = { total: rBy.reduce((a, b) => a + Number(b.c), 0), by_status: {} };
      for (const r of rBy) rag.by_status[r.status] = Number(r.c);
      return sendJson(res, 200, isLegacy
        ? { run: { run_id: runIdQ, legacy: true },
            skill: { total: 0, by_status: {}, by_role: {} },
            rag: { total: 0, by_status: {} }, not_available: true }
        : { run: { run_id: runIdQ }, skill, rag });
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
