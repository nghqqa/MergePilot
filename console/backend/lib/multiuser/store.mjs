// console/backend/lib/multiuser/store.mjs — MU 持久化层（Developer Edition 最小切片）。
//
// 原则：
//  * 版本化迁移（mu.schema_migrations）——只前进不重放；bootstrap 幂等（ON CONFLICT DO NOTHING）；
//  * 全部读路径按 tenant_id 收窄（resolveRepository/getBinding 等显式带 tenant 维度）；
//  * 凭据红线：本层任何 API 都不接受/不存储 token、密码、密钥。
import crypto from 'node:crypto';
import { MU_MIGRATIONS } from './schema.mjs';

// platform 域审计事件白名单（Wave 2A 首批）：OAuth 流程与会话生命周期
export const PLATFORM_AUDIT_KINDS = [
  'OAUTH_FLOW_STARTED', 'OAUTH_FLOW_CONSUMED', 'OAUTH_FLOW_REJECTED',
  'SESSION_REVOKED', 'SESSIONS_REVOKED_ALL',
];

export async function createMuStore({ pool, env = process.env } = {}) {
  if (!pool || typeof pool.query !== 'function') {
    throw new Error('createMuStore: pool with .query() required (pg Pool)');
  }
  const q = (text, params) => pool.query(text, params);

  async function initSchema() {
    await q(`CREATE SCHEMA IF NOT EXISTS mu`);
    await q(`CREATE TABLE IF NOT EXISTS mu.schema_migrations (
      version INT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
    for (const m of MU_MIGRATIONS) {
      const r = await q(`SELECT 1 FROM mu.schema_migrations WHERE version=$1`, [m.version]);
      if (r.rowCount) continue;
      // 逐条执行（语句数组非事务包裹的单个多语句串，便于定位失败语句）
      for (const sql of m.sql) await q(sql);
      await q(`INSERT INTO mu.schema_migrations (version, name) VALUES ($1,$2)`, [m.version, m.name]);
    }
    return true;
  }

  // ── bootstrap（幂等）：迁移 tenant + pilot 操作员 → PlatformAdmin 映射 ──
  async function bootstrap() {
    const adminLogin = String(env.MU_BOOTSTRAP_ADMIN_LOGIN || env.CONSOLE_PILOT_USER || 'pilot-admin');
    const adminSubject = String(env.MU_BOOTSTRAP_ADMIN_SUBJECT || `fixture:${adminLogin}`);
    const tenant = await ensureTenant({ slug: 'default', displayName: 'Migration Default Tenant', isMigrationTenant: true });
    const user = await ensureUser({ login: adminLogin, displayName: 'Pilot Admin (migrated)' });
    await ensureIdentity({ userId: user.user_id, provider: 'fixture', subject: adminSubject });
    const membership = await ensureMembership({ tenantId: tenant.tenant_id, userId: user.user_id, role: 'platform_admin' });
    return { tenant, user, membership };
  }

  // Beta Hardening W1：tenant 域审计必须归属（schema NOT NULL + 应用层前置拒绝）；
  // platform 域事件走独立表（明确 scope 建模——无 nullable 歧义、无虚假 tenant）。
  async function audit(kind, { tenantId, actorUserId = null, detail = {} } = {}) {
    if (!tenantId) {
      throw new Error('mu.audit: tenant 域事件必须携带 tenantId（platform 事件请用 auditPlatform）');
    }
    await q(`INSERT INTO mu.audit_event (tenant_id, actor_user_id, kind, detail)
             VALUES ($1,$2,$3,$4::jsonb)`,
      [tenantId, actorUserId, kind, JSON.stringify(detail)]);
  }
  // Beta Identity Wave 2A：platform 域审计首批受限事件类型（注册表制——新增类型
  // 须显式扩充此表并过评审；未知 kind 一律拒绝，防 platform 表成为通用垃圾桶）
  async function auditPlatform(kind, { actorUserId = null, detail = {} } = {}) {
    if (!PLATFORM_AUDIT_KINDS.includes(kind)) {
      throw new Error(`mu.auditPlatform: 未登记的 platform 事件类型 ${kind}（允许：${PLATFORM_AUDIT_KINDS.join(',')}）`);
    }
    await q(`INSERT INTO mu.platform_audit_event (actor_user_id, kind, detail)
             VALUES ($1,$2,$3::jsonb)`,
      [actorUserId, kind, JSON.stringify(detail)]);
  }

  // ── 实体（ensure* 幂等；返回现存或新建行） ──
  async function ensureTenant({ slug, displayName, isMigrationTenant = false }) {
    const r = await q(
      `INSERT INTO mu.tenant (slug, display_name, is_migration_tenant)
       VALUES ($1,$2,$3) ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name
       RETURNING *`, [slug, displayName, isMigrationTenant]);
    return r.rows[0];
  }
  async function getTenantBySlug(slug) {
    const r = await q(`SELECT * FROM mu.tenant WHERE slug=$1`, [slug]);
    return r.rows[0] ?? null;
  }
  async function getTenant(tenantId) {
    const r = await q(`SELECT * FROM mu.tenant WHERE tenant_id=$1 AND state='active'`, [tenantId]);
    return r.rows[0] ?? null;
  }

  async function ensureUser({ login, displayName = null }) {
    const r = await q(
      `INSERT INTO mu.app_user (login, display_name) VALUES ($1,$2)
       ON CONFLICT (login) DO UPDATE SET display_name = COALESCE(EXCLUDED.display_name, mu.app_user.display_name)
       RETURNING *`, [login, displayName]);
    return r.rows[0];
  }
  async function getUser(userId) {
    const r = await q(`SELECT * FROM mu.app_user WHERE user_id=$1 AND state='active'`, [userId]);
    return r.rows[0] ?? null;
  }
  async function getUserByLogin(login) {
    const r = await q(`SELECT * FROM mu.app_user WHERE login=$1 AND state='active'`, [login]);
    return r.rows[0] ?? null;
  }

  async function ensureIdentity({ userId, provider, subject }) {
    const r = await q(
      `INSERT INTO mu.external_identity (user_id, provider, subject) VALUES ($1,$2,$3)
       ON CONFLICT (provider, subject) DO NOTHING RETURNING *`, [userId, provider, subject]);
    if (r.rows.length) return r.rows[0];
    const cur = await q(`SELECT * FROM mu.external_identity WHERE provider=$1 AND subject=$2`, [provider, subject]);
    return cur.rows[0] ?? null;
  }
  async function getUserByIdentity(provider, subject) {
    const r = await q(
      `SELECT u.* FROM mu.app_user u
         JOIN mu.external_identity i ON i.user_id = u.user_id
        WHERE i.provider=$1 AND i.subject=$2 AND u.state='active'`, [provider, subject]);
    return r.rows[0] ?? null;
  }

  async function ensureMembership({ tenantId, userId, role, grantedBy = null }) {
    const r = await q(
      `INSERT INTO mu.membership (tenant_id, user_id, role, granted_by)
       VALUES ($1,$2,$3,$4) ON CONFLICT (tenant_id, user_id)
       DO UPDATE SET role = EXCLUDED.role, state = 'active', updated_at = now()
       RETURNING *`, [tenantId, userId, role, grantedBy]);
    return r.rows[0];
  }
  async function getMembership(tenantId, userId) {
    const r = await q(
      `SELECT * FROM mu.membership WHERE tenant_id=$1 AND user_id=$2`, [tenantId, userId]);
    return r.rows[0] ?? null; // 含 state——active 判定交给 authz（fail-closed）
  }
  async function revokeMembership(tenantId, userId) {
    const r = await q(
      `UPDATE mu.membership SET state='revoked', updated_at=now()
        WHERE tenant_id=$1 AND user_id=$2 AND state='active' RETURNING *`, [tenantId, userId]);
    return r.rows[0] ?? null;
  }
  async function listMembers(tenantId) {
    const r = await q(
      `SELECT m.membership_id, m.user_id, m.role, m.state, m.created_at, u.login, u.display_name
         FROM mu.membership m JOIN mu.app_user u ON u.user_id = m.user_id
        WHERE m.tenant_id=$1 ORDER BY m.created_at`, [tenantId]);
    return r.rows;
  }
  async function listMembershipsOfUser(userId) {
    const r = await q(
      `SELECT m.*, t.slug AS tenant_slug, t.display_name AS tenant_display_name
         FROM mu.membership m JOIN mu.tenant t ON t.tenant_id = m.tenant_id
        WHERE m.user_id=$1 ORDER BY m.created_at`, [userId]);
    return r.rows;
  }

  async function ensureRepository({ tenantId, provider, providerRepoId, owner, name, defaultBranch = null }) {
    const r = await q(
      `INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name, default_branch)
       VALUES ($1,$2,$3,$4,$5,$6)
       ON CONFLICT (tenant_id, provider, provider_repo_id)
       DO UPDATE SET owner = EXCLUDED.owner, name = EXCLUDED.name, state='active'
       RETURNING *`, [tenantId, provider, providerRepoId, owner, name, defaultBranch]);
    return r.rows[0];
  }
  // tenant 收窄解析：跨 tenant 的 repo_id 永远 miss（防篡改关联）
  async function resolveRepository(tenantId, repoId) {
    const r = await q(
      `SELECT * FROM mu.repository WHERE tenant_id=$1 AND repo_id=$2 AND state='active'`,
      [tenantId, repoId]);
    return r.rows[0] ?? null;
  }
  async function listRepositories(tenantId) {
    const r = await q(
      `SELECT r.*, b.binding_id, b.kind AS binding_kind, b.installation_id, b.installation_state,
              b.granted_scopes, b.state AS binding_state,
              (SELECT count(*) FROM mu.pull_request p
                WHERE p.repo_id = r.repo_id AND p.tenant_id = r.tenant_id) AS pr_count
         FROM mu.repository r
         LEFT JOIN mu.binding b ON b.repo_id = r.repo_id AND b.state='active'
        WHERE r.tenant_id=$1 AND r.state='active' ORDER BY r.created_at`, [tenantId]);
    return r.rows;
  }

  async function ensureBinding({ tenantId, repoId, kind, installationId = null, installationState = 'active', grantedScopes = [], createdBy = null }) {
    const r = await q(
      `INSERT INTO mu.binding (tenant_id, repo_id, kind, installation_id, installation_state, granted_scopes, created_by)
       VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)
       ON CONFLICT (tenant_id, repo_id, kind)
       DO UPDATE SET installation_id = EXCLUDED.installation_id,
                     installation_state = EXCLUDED.installation_state,
                     granted_scopes = EXCLUDED.granted_scopes,
                     state = 'active', updated_at = now()
       RETURNING *`, [tenantId, repoId, kind, installationId, installationState, JSON.stringify(grantedScopes), createdBy]);
    return r.rows[0];
  }
  async function getBindingForRepo(tenantId, repoId) {
    const r = await q(
      `SELECT * FROM mu.binding WHERE tenant_id=$1 AND repo_id=$2 AND state='active' AND installation_state='active'
       ORDER BY created_at LIMIT 1`, [tenantId, repoId]);
    return r.rows[0] ?? null;
  }
  async function revokeBinding(tenantId, repoId) {
    const r = await q(
      `UPDATE mu.binding SET state='revoked', updated_at=now()
        WHERE tenant_id=$1 AND repo_id=$2 AND state='active' RETURNING *`, [tenantId, repoId]);
    return r.rows[0] ?? null;
  }

  // ── PR / ReviewRecord / Job（migration 0002）──
  async function upsertPullRequest({ tenantId, repoId, providerPrNumber, headSha, headRef = null, baseRef = null, title = null, branchProtectionStatus = 'unknown' }) {
    const r = await q(
      `INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha, head_ref, base_ref, title, branch_protection_status)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
       ON CONFLICT (tenant_id, repo_id, provider_pr_number, head_sha)
       DO UPDATE SET title = EXCLUDED.title, branch_protection_status = EXCLUDED.branch_protection_status,
                     updated_at = now()
       RETURNING *`,
      [tenantId, repoId, providerPrNumber, headSha, headRef, baseRef, title, branchProtectionStatus]);
    return r.rows[0];
  }
  async function resolvePullRequest(tenantId, prId, { repoId = null } = {}) {
    const COL = 'p.*, r.owner AS repo_owner, r.name AS repo_name, r.provider AS repo_provider';
    const FROM = 'FROM mu.pull_request p JOIN mu.repository r ON r.repo_id = p.repo_id';
    // 双寻址（Wave 3.15 拆页后路由携带 GitHub 编号）：先按形状分派——
    // 数字直接走编号查询（非 UUID 值塞给 uuid 列会 22P02 500，而非空结果）
    const n = Number(prId);
    const isNumber = Number.isInteger(n) && n > 0 && String(prId).trim() !== '';
    // 审计 E-1：调用方显式携带 repo_id 时按 repo 二次收窄（tenant 收窄之内的
    // repo 归属锚定）——repo 越界（跨 tenant）/PR 不在该 repo 与"PR 不存在"同为
    // 空结果（调用方同形 404，不泄露跨 tenant 存在性）；repo_id 形状非法亦视为
    // miss（不把垃圾参数喂给 uuid 列吃 22P02 500）。缺省（null）保持旧行为——
    // webhook/内部调用不带 repo_id 不受影响。
    const repoNarrow = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(repoId ?? ''))
      ? String(repoId) : null;
    if (repoId != null && repoNarrow === null) return null;
    const repoClause = repoNarrow ? ' AND p.repo_id=$3' : '';
    const repoParams = repoNarrow ? [repoNarrow] : [];
    if (!isNumber) {
      const r = await q(
        `SELECT ${COL} ${FROM} WHERE p.tenant_id=$1 AND p.pr_id=$2${repoClause}`,
        [tenantId, prId, ...repoParams]);
      if (r.rows[0]) return r.rows[0];
      return null;
    }
    // 编号在 tenant 内跨仓库可重号，取最近更新行；仍严格 tenant 收窄
    const r2 = await q(
      `SELECT ${COL} ${FROM} WHERE p.tenant_id=$1 AND p.provider_pr_number=$2${repoClause}
        ORDER BY p.updated_at DESC LIMIT 1`, [tenantId, n, ...repoParams]);
    return r2.rows[0] ?? null;
  }
  async function findPullRequests(tenantId, { repoId = null, number = null } = {}) {
    const params = [tenantId];
    let where = `p.tenant_id=$1`;
    if (repoId) { params.push(repoId); where += ` AND p.repo_id=$${params.length}`; }
    if (number) { params.push(number); where += ` AND p.provider_pr_number=$${params.length}`; }
    const r = await q(
      `SELECT p.*, r.owner AS repo_owner, r.name AS repo_name
         FROM mu.pull_request p JOIN mu.repository r ON r.repo_id = p.repo_id
        WHERE ${where} ORDER BY p.updated_at DESC LIMIT 50`, params);
    return r.rows;
  }
  async function insertReviewRecord({ tenantId, repoId, prId, kind, actorUserId = null, decision = null, headSha, branchProtectionStatus = 'unknown', payload = null, detail = {} }) {
    const payloadSha = payload ? crypto.createHash('sha256').update(JSON.stringify(payload)).digest('hex') : null;
    const r = await q(
      `INSERT INTO mu.review_record (tenant_id, repo_id, pr_id, kind, actor_user_id, decision, head_sha, branch_protection_status, payload_sha256, detail)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb) RETURNING *`,
      [tenantId, repoId, prId, kind, actorUserId, decision, headSha, branchProtectionStatus, payloadSha, JSON.stringify(detail)]);
    return r.rows[0];
  }
  async function listReviewRecords(tenantId, prId) {
    const r = await q(
      `SELECT v.*, u.login AS actor_login FROM mu.review_record v
         LEFT JOIN mu.app_user u ON u.user_id = v.actor_user_id
        WHERE v.tenant_id=$1 AND v.pr_id=$2 ORDER BY v.created_at`, [tenantId, prId]);
    return r.rows;
  }

  async function enqueueJob({ tenantId, repoId, prId = null, kind, requestedBy, requestedRole, payload = {} }) {
    const r = await q(
      `INSERT INTO mu.job (tenant_id, repo_id, pr_id, kind, requested_by, requested_role, payload)
       VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) RETURNING *`,
      [tenantId, repoId, prId, kind, requestedBy, requestedRole, JSON.stringify(payload)]);
    return r.rows[0];
  }
  async function listJobs(tenantId, { state = null } = {}) {
    const params = [tenantId];
    let where = `tenant_id=$1`;
    if (state) { params.push(state); where += ` AND state=$${params.length}`; }
    const r = await q(
      `SELECT j.*, u.login AS requested_by_login FROM mu.job j
         LEFT JOIN mu.app_user u ON u.user_id = j.requested_by
        WHERE ${where} ORDER BY j.created_at DESC LIMIT 100`, params);
    return r.rows;
  }
  // 认领（单进程 dev worker）：CAS queued→running，带回请求者上下文供执行前复查
  // Wave 3.8 队首阻塞修复（HOL）：支持 kind 过滤领取（FOR UPDATE SKIP LOCKED
  // 保并发唯一领取）+ locked_at lease 记录（孤立 running 可回收）。
  // 非 fixture 模式 tick 以 kinds=['event_sync'] 领取——人工 job 在 claim 层隔离，
  // 结构上不可能阻塞 webhook 消费（requeue+break 已废除）。
  async function claimNextJob({ kinds = null } = {}) {
    const r = await q(
      `UPDATE mu.job SET state='running', locked_at=now(), updated_at=now()
        WHERE job_id = (SELECT job_id FROM mu.job WHERE state='queued'
          ${kinds ? 'AND kind = ANY($1::text[])' : ''}
          ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
       RETURNING *`, kinds ? [kinds] : []);
    return r.rows[0] ?? null;
  }
  // Wave 2B.1：人工 job 原样回队（fixture 路径保留；locked_at 一并清空）
  async function requeueJob(jobId) {
    const r = await q(`UPDATE mu.job SET state='queued', locked_at=NULL, updated_at=now()
       WHERE job_id=$1 RETURNING job_id`, [jobId]);
    return r.rows.length > 0;
  }
  // Wave 3.8（HOL）：worker 崩溃遗留的孤立 running job（locked_at 超时）回收回队
  // ——重启后合法 job 继续被领取；有限批量，调用方负责审计。
  async function requeueOrphanedJobs({ staleMinutes = 10, limit = 10 } = {}) {
    const r = await q(
      `UPDATE mu.job SET state='queued', locked_at=NULL, updated_at=now()
        WHERE job_id IN (SELECT job_id FROM mu.job
          WHERE state='running' AND locked_at IS NOT NULL
            AND locked_at < now() - make_interval(mins => $1::int)
          ORDER BY locked_at LIMIT $2)
       RETURNING job_id, tenant_id, kind`, [staleMinutes, limit]);
    return r.rows;
  }
  // Wave 3.8（HOL）：非 fixture 模式人工 job 无消费者——超过宽限期的孤立人工 job
  // 转终态 rejected（有限批量；reason 写入 result，审计由调用方落）。
  async function rejectStaleManualJobs({ expiryHours = 24, limit = 10 } = {}) {
    const r = await q(
      `UPDATE mu.job SET state='rejected',
          result = jsonb_build_object('reason','manual_job_expired_unconsumable',
            'detail','manual jobs have no consumer without MU_FIXTURES=1 or a dedicated worker; expired after grace period'),
          updated_at=now()
        WHERE job_id IN (SELECT job_id FROM mu.job
          WHERE state='queued' AND kind <> 'event_sync'
            AND created_at < now() - make_interval(hours => $1::int)
          ORDER BY created_at LIMIT $2)
       RETURNING job_id, tenant_id, kind, created_at`, [expiryHours, limit]);
    return r.rows;
  }

  async function finishJob(jobId, state, result = {}) {
    const r = await q(
      `UPDATE mu.job SET state=$2, result=$3::jsonb, updated_at=now() WHERE job_id=$1 RETURNING *`,
      [jobId, state, JSON.stringify(result)]);
    return r.rows[0] ?? null;
  }

  // ── Beta Identity Wave 2A：OAuth flow / 持久会话 / 邀请（全部摘要存储） ──
  const sha256Of = (v) => crypto.createHash('sha256').update(String(v)).digest('hex');

  async function insertOAuthFlow({ stateHash, corrHash = null, inviteId = null, ttlMs = 10 * 60_000 }) {
    const r = await q(
      `INSERT INTO mu.oauth_flow (state_hash, corr_hash, invite_id, expires_at)
       VALUES ($1,$2,$3, now() + ($4 || ' milliseconds')::interval) RETURNING *`,
      [stateHash, corrHash, inviteId, String(ttlMs)]);
    return r.rows[0];
  }
  // 单次消费：consumed_at CAS——0 行=不存在/已消费/已过期
  async function consumeOAuthFlow(stateHash) {
    const r = await q(
      `UPDATE mu.oauth_flow SET consumed_at = now()
        WHERE state_hash=$1 AND consumed_at IS NULL AND expires_at > now()
       RETURNING *`, [stateHash]);
    return r.rows[0] ?? null;
  }

  async function createSession({ userId, tenantId, login, role, provider, ttlMs, csrfHash }) {
    const token = crypto.randomBytes(32).toString('base64url');
    const r = await q(
      `INSERT INTO mu.session (token_hash, user_id, tenant_id, csrf_hash, login, role_snapshot, provider, expires_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7, now() + ($8 || ' milliseconds')::interval)
       RETURNING session_id, expires_at`,
      [sha256Of(token), userId, tenantId, csrfHash, login, role, provider, String(ttlMs)]);
    return { token, sessionId: r.rows[0].session_id, expiresAt: r.rows[0].expires_at };
  }
  async function findSessionByToken(token) {
    const r = await q(
      `SELECT s.*, u.state AS user_state FROM mu.session s
         JOIN mu.app_user u ON u.user_id = s.user_id
        WHERE s.token_hash=$1 AND s.revoked_at IS NULL AND s.expires_at > now()
          AND u.state='active'`,
      [sha256Of(token)]);
    if (!r.rows.length) return null;
    await q(`UPDATE mu.session SET last_seen_at=now() WHERE session_id=$1`, [r.rows[0].session_id]).catch(() => {});
    return r.rows[0];
  }
  async function rotateSession(sessionId, { tenantId, role }) {
    // 轮换：作废旧 token、签发新 token（tenant 切换时使用；行保持延续性）
    const token = crypto.randomBytes(32).toString('base64url');
    const csrf = crypto.randomBytes(24).toString('base64url');
    const r = await q(
      `UPDATE mu.session SET token_hash=$2, csrf_hash=$3, tenant_id=$4, role_snapshot=$5, last_seen_at=now()
        WHERE session_id=$1 AND revoked_at IS NULL AND expires_at > now()
       RETURNING expires_at`,
      [sessionId, sha256Of(token), sha256Of(csrf), tenantId, role]);
    if (!r.rows.length) return null;
    return { token, csrf, expiresAt: r.rows[0].expires_at };
  }
  async function revokeSessionByToken(token, reason = 'logout') {
    const r = await q(
      `UPDATE mu.session SET revoked_at=now(), revoke_reason=$2
        WHERE token_hash=$1 AND revoked_at IS NULL RETURNING session_id`,
      [sha256Of(token), reason]);
    return r.rows.length > 0;
  }
  async function revokeAllSessionsForUser(userId, reason = 'revoke_all') {
    const r = await q(
      `UPDATE mu.session SET revoked_at=now(), revoke_reason=$2
        WHERE user_id=$1 AND revoked_at IS NULL AND expires_at > now()
       RETURNING session_id`, [userId, reason]);
    return r.rows.length;
  }

  async function createInvitation({ tenantId, role, expectedSubject = null, expectedLogin = null, note = null, createdBy = null, ttlMs = 24 * 3600_000 }) {
    if (!expectedSubject && !expectedLogin) throw new Error('invitation 需要 expected_subject 或 expected_login 之一');
    const r = await q(
      `INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, note, created_by, expires_at)
       VALUES ($1,$2,$3,$4,$5,$6, now() + ($7 || ' milliseconds')::interval)
       RETURNING *`,
      [tenantId, role, expectedSubject, expectedLogin, note, createdBy, String(ttlMs)]);
    return r.rows[0];
  }
  async function listInvitations(tenantId) {
    const r = await q(
      `SELECT i.*, t.slug AS tenant_slug FROM mu.invitation i
         JOIN mu.tenant t ON t.tenant_id = i.tenant_id
        WHERE i.tenant_id=$1 ORDER BY i.created_at DESC LIMIT 100`, [tenantId]);
    return r.rows;
  }
  async function getInvitation(inviteId) {
    const r = await q(
      `SELECT i.*, t.slug AS tenant_slug FROM mu.invitation i
         JOIN mu.tenant t ON t.tenant_id = i.tenant_id
        WHERE i.invite_id=$1`, [inviteId]);
    return r.rows[0] ?? null;
  }
  // Wave 2A.1：认领只按 GitHub 数字 id（expected_subject）——login 句柄不可作为
  // 授权条件（GitHub handle 可夺注：原持有人改名后他人可注册同名，构成认领竞态）。
  // expected_login 仅保留为展示/预筛选字段。存量 login-only 邀请 fail-closed 不可认领。
  async function findClaimableInvitation({ subject = null }) {
    if (!subject) return null;
    const r = await q(
      `SELECT * FROM mu.invitation
        WHERE claimed_at IS NULL AND expires_at > now() AND expected_subject = $1
        ORDER BY created_at LIMIT 1`, [subject]);
    return r.rows[0] ?? null;
  }
  async function claimInvitation(inviteId, userId) {
    const r = await q(
      `UPDATE mu.invitation SET claimed_at=now(), claimed_by_user_id=$2
        WHERE invite_id=$1 AND claimed_at IS NULL AND expires_at > now()
       RETURNING *`, [inviteId, userId]);
    return r.rows[0] ?? null;
  }

  // ── Wave 2B：GitHub App installation / repository binding / webhook delivery ──
  async function upsertInstallation({ installationId, tenantId, accountId, accountLogin, accountType = 'User', appId }) {
    const r = await q(
      `INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, account_type, app_id)
       VALUES ($1,$2,$3,$4,$5,$6)
       ON CONFLICT (installation_id) DO UPDATE SET
         account_id = EXCLUDED.account_id, account_login = EXCLUDED.account_login,
         account_type = EXCLUDED.account_type, updated_at = now()
       RETURNING *`,
      [installationId, tenantId, accountId, accountLogin, accountType, appId]);
    return r.rows[0];
  }
  async function getInstallation(installationId) {
    const r = await q(`SELECT * FROM mu.github_app_installation WHERE installation_id=$1`, [installationId]);
    return r.rows[0] ?? null;
  }
  async function listInstallations(tenantId) {
    const r = await q(
      `SELECT * FROM mu.github_app_installation WHERE tenant_id=$1 ORDER BY created_at`, [tenantId]);
    return r.rows;
  }
  async function setInstallationState(installationId, { suspended = null, revoked = null }) {
    // 布尔语义：true→now()，false→NULL（清除）；无变化跳过
    const sets = ['updated_at = now()'];
    const params = [installationId];
    if (suspended === true) sets.push('suspended_at = now()');
    else if (suspended === false) sets.push('suspended_at = NULL');
    if (revoked === true) sets.push('revoked_at = now()');
    else if (revoked === false) sets.push('revoked_at = NULL');
    const r = await q(`UPDATE mu.github_app_installation SET ${sets.join(', ')} WHERE installation_id=$1 RETURNING *`, params);
    return r.rows[0] ?? null;
  }
  async function upsertRepositoryBinding({ tenantId, repoId, githubRepoId, owner, name, installationId, defaultBranch = null, createdBy = null }) {
    const r = await q(
      `INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, default_branch, created_by)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
       ON CONFLICT (tenant_id, repo_id) DO UPDATE SET
         github_repo_id = EXCLUDED.github_repo_id, owner = EXCLUDED.owner, name = EXCLUDED.name,
         installation_id = EXCLUDED.installation_id, default_branch = EXCLUDED.default_branch,
         binding_state = 'active', revoked_at = NULL, error_code = NULL, updated_at = now()
       RETURNING *`,
      [tenantId, repoId, githubRepoId, owner, name, installationId, defaultBranch, createdBy]);
    return r.rows[0];
  }
  async function getBindingByRepo(tenantId, repoId) {
    const r = await q(
      `SELECT * FROM mu.repository_binding WHERE tenant_id=$1 AND repo_id=$2`, [tenantId, repoId]);
    return r.rows[0] ?? null;
  }
  async function setBindingState(tenantId, repoId, state, errorCode = null) {
    const r = await q(
      `UPDATE mu.repository_binding SET binding_state=$3, error_code=$4,
         revoked_at = CASE WHEN $3='revoked' THEN now() ELSE revoked_at END, updated_at=now()
        WHERE tenant_id=$1 AND repo_id=$2 RETURNING *`,
      [tenantId, repoId, state, errorCode]);
    return r.rows[0] ?? null;
  }
  async function setBindingsStateForInstallation(installationId, state) {
    const r = await q(
      `UPDATE mu.repository_binding SET binding_state=$2,
         revoked_at = CASE WHEN $2='revoked' THEN now() ELSE revoked_at END, updated_at=now()
        WHERE installation_id=$1 RETURNING repo_id`, [installationId, state]);
    return r.rows.length;
  }
  async function claimWebhookDelivery(deliveryId, { tenantId = null, installationId = null, event }) {
    // 幂等去重：首次插入=received；冲突即重复
    const r = await q(
      `INSERT INTO mu.webhook_delivery (delivery_id, tenant_id, installation_id, event)
       VALUES ($1,$2,$3,$4) ON CONFLICT (delivery_id) DO NOTHING RETURNING *`,
      [deliveryId, tenantId, installationId, event]);
    return r.rows[0] ?? null;
  }
  async function finishWebhookDelivery(deliveryId, state) {
    await q(`UPDATE mu.webhook_delivery SET state=$2, processed_at=now() WHERE delivery_id=$1`,
      [deliveryId, state]);
  }

  async function listAudit(tenantId, { limit = 100 } = {}) {
    const r = await q(
      `SELECT a.seq, a.kind, a.created_at, u.login AS actor_login, a.detail
         FROM mu.audit_event a LEFT JOIN mu.app_user u ON u.user_id = a.actor_user_id
        WHERE a.tenant_id=$1 ORDER BY a.seq DESC LIMIT $2`, [tenantId, Math.min(limit, 500)]);
    return r.rows;
  }

  return {
    initSchema, bootstrap, audit,
    ensureTenant, getTenantBySlug, getTenant,
    ensureUser, getUser, getUserByLogin,
    ensureIdentity, getUserByIdentity,
    ensureMembership, getMembership, revokeMembership, listMembers, listMembershipsOfUser,
    ensureRepository, resolveRepository, listRepositories,
    ensureBinding, getBindingForRepo, revokeBinding,
    upsertPullRequest, resolvePullRequest, findPullRequests,
    insertReviewRecord, listReviewRecords,
    enqueueJob, listJobs, claimNextJob, finishJob, requeueOrphanedJobs, rejectStaleManualJobs,
    listAudit, auditPlatform,
    insertOAuthFlow, consumeOAuthFlow,
    createSession, findSessionByToken, rotateSession, revokeSessionByToken, revokeAllSessionsForUser,
    createInvitation, listInvitations, getInvitation, findClaimableInvitation, claimInvitation,
    upsertInstallation, getInstallation, listInstallations, setInstallationState,
    upsertRepositoryBinding, getBindingByRepo, setBindingState, setBindingsStateForInstallation,
    claimWebhookDelivery, finishWebhookDelivery, requeueJob,
  };
}
