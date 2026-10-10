// console/backend/lib/multiuser/store.mjs — MU 持久化层（Developer Edition 最小切片）。
//
// 原则：
//  * 版本化迁移（mu.schema_migrations）——只前进不重放；bootstrap 幂等（ON CONFLICT DO NOTHING）；
//  * 全部读路径按 tenant_id 收窄（resolveRepository/getBinding 等显式带 tenant 维度）；
//  * 凭据红线：本层任何 API 都不接受/不存储 token、密码、密钥。
import crypto from 'node:crypto';
import { MU_MIGRATIONS, MU_FIXTURE_INSTALLATION_BASE as muFixtureInstallationBase,
  muFixtureInstallationIdOf, muFixtureRepoIdOf } from './schema.mjs';

// platform 域审计事件白名单（Wave 2A 首批）：OAuth 流程与会话生命周期
export const PLATFORM_AUDIT_KINDS = [
  'OAUTH_FLOW_STARTED', 'OAUTH_FLOW_CONSUMED', 'OAUTH_FLOW_REJECTED',
  'SESSION_REVOKED', 'SESSIONS_REVOKED_ALL',
];

export async function createMuStore({ pool, env = process.env, migrations = MU_MIGRATIONS } = {}) {
  if (!pool || typeof pool.query !== 'function') {
    throw new Error('createMuStore: pool with .query() required (pg Pool)');
  }
  const q = (text, params) => pool.query(text, params);

  // ── 迁移 runner 加固（rc.10 PR-D；只改 runner 机制，MU_MIGRATIONS v1–v20 逐字不动）──
  //  * advisory lock（会话级，hashtext('mergepilot_mu_schema_init')）全程持有：
  //    lock/unlock 必须落在同一 PG 连接——从 pool.connect() 取专用 client，
  //    finally 保证 pg_advisory_unlock + release（成功/失败两路都不泄漏锁/连接）。
  //    注：PG advisory lock 按 database 隔离——同一 DSN（同一库）的多进程互斥，
  //    恰好覆盖 mu schema 的保护面（schema_migrations 就在该库）。
  //  * 锁等待有上限（MU_SCHEMA_INIT_LOCK_TIMEOUT_MS，默认 30s）：以 pg_try_advisory_lock
  //    轮询代替无限阻塞的 pg_advisory_lock；超时抛 mu_schema_init_locked 快速失败
  //    （api.getMuStore 的 catch 置 muStorePromise=null，下一请求可重试）。
  //  * 单赢家并发：同一时刻至多一个 initSchema 执行 DDL；等锁成功者经既有
  //    "SELECT 1 WHERE version" 版本行守卫整体跳过已应用迁移——多进程冷启 fresh DB
  //    最终状态一致且恰一赢家执行 DDL。
  //  * 每迁移事务包裹：BEGIN → 逐语句 SAVEPOINT mu_mig_stmt_<i> → 版本行 INSERT →
  //    COMMIT（版本行与 DDL 同事务，失败即整体回滚、版本行不落）。PG 事务内 DDL 合法，
  //    v18 单 DO 块、v19 CREATE OR REPLACE FUNCTION+DROP/CREATE TRIGGER、v20
  //    CREATE TABLE+INDEX 均适用。SAVEPOINT 用于失败语句定位：出错 ROLLBACK TO 后整体
  //    ROLLBACK，错误信息带 migration version/name + 语句序号（0 起，即 m.sql 数组下标）
  //    + PG 原始消息——取代旧"非事务包裹便于定位失败语句"的注释语义。
  //  * migrations 形参为测试专用注入点（默认生产 MU_MIGRATIONS，不改任何既有内容）。
  async function initSchema() {
    let client;
    try {
      client = await pool.connect();
    } catch (e) {
      throw new Error(`mu_schema_init: pool.connect failed: ${e?.message ?? e}`);
    }
    let locked = false;
    try {
      const lockWaitMs = Number(env.MU_SCHEMA_INIT_LOCK_TIMEOUT_MS || 30_000);
      const deadline = Date.now() + lockWaitMs;
      for (;;) {
        const r = await client.query(`SELECT pg_try_advisory_lock(hashtext('mergepilot_mu_schema_init')) AS ok`);
        if (r.rows[0]?.ok === true) { locked = true; break; }
        if (Date.now() > deadline) {
          throw new Error(`mu_schema_init_locked: advisory lock not acquired within ${lockWaitMs}ms `
            + `(another initSchema holds mergepilot_mu_schema_init or is stuck)`);
        }
        await new Promise((res) => setTimeout(res, 50));
      }
      await client.query(`CREATE SCHEMA IF NOT EXISTS mu`);
      await client.query(`CREATE TABLE IF NOT EXISTS mu.schema_migrations (
        version INT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
      for (const m of migrations) {
        const r = await client.query(`SELECT 1 FROM mu.schema_migrations WHERE version=$1`, [m.version]);
        if (r.rowCount) continue; // 已记录版本跳过（等锁者据此整体跳过——单赢家语义）
        await client.query(`BEGIN`);
        try {
          for (let i = 0; i < m.sql.length; i++) {
            const sp = `mu_mig_stmt_${i}`;
            await client.query(`SAVEPOINT ${sp}`);
            try {
              await client.query(m.sql[i]);
            } catch (e) {
              // 失败语句定位：回退到该语句前快照（事务恢复可用），再整体 ROLLBACK
              const pgMsg = String(e?.message ?? e);
              try { await client.query(`ROLLBACK TO SAVEPOINT ${sp}`); } catch { /* 连接级故障：交由外层 ROLLBACK */ }
              throw new Error(`mu_schema_init failed at migration ${m.version} (${m.name}) statement#${i}: ${pgMsg}`);
            }
          }
          await client.query(`INSERT INTO mu.schema_migrations (version, name) VALUES ($1,$2)`, [m.version, m.name]);
          await client.query(`COMMIT`);
        } catch (e) {
          try { await client.query(`ROLLBACK`); } catch { /* 连接已断：释放时由池销毁 */ }
          throw e;
        }
      }
      return true;
    } finally {
      // 同一连接上解锁 + 归还（无条件执行，防泄漏；未持锁时 unlock 返回 false 无副作用）
      if (locked) {
        try { await client.query(`SELECT pg_advisory_unlock(hashtext('mergepilot_mu_schema_init'))`); }
        catch { /* 会话已断：会话级锁随连接消亡 */ }
      }
      client.release();
    }
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
    // v18 绑定统一（审计 E-2）：读侧唯一权威 = mu.repository_binding。此前 JOIN mu.binding
    // 导致经 GitHub App 安装回调/webhook 绑定的仓库（写侧只落 repository_binding）在
    // 仓库列表无绑定信息（GHAppPanel"已绑定仓库"为空、onboarding 卡 bind 步）。
    // installation_state 列无新表对应——以 binding_state 别名承载（JOIN 已滤
    // binding_state='active'，别名恒 'active'，前端 hasBinding/激活绑定语义不变）。
    const r = await q(
      `SELECT r.*, b.binding_id, b.installation_id, b.default_branch, b.last_sync_at,
              b.binding_state, b.binding_state AS installation_state,
              (SELECT count(*) FROM mu.pull_request p
                WHERE p.repo_id = r.repo_id AND p.tenant_id = r.tenant_id) AS pr_count
         FROM mu.repository r
         LEFT JOIN mu.repository_binding b
                ON b.repo_id = r.repo_id AND b.tenant_id = r.tenant_id AND b.binding_state='active'
        WHERE r.tenant_id=$1 AND r.state='active' ORDER BY r.created_at`, [tenantId]);
    return r.rows;
  }

  // ── v18 绑定统一：ensureBinding 为演示/fixture 绑定唯一写入口（写 mu.repository_binding）──
  // 形参保持旧签名兼容（api.mjs POST /api/mu/repositories 与既有测试不破）：
  //  * kind → fixture 域等价表达（合成 installation_id 落 MU_FIXTURE_INSTALLATION_BASE 保留
  //    区间 + account_type='fixture'，派生与 v18 迁移 SQL 逐字同式）；
  //  * installationId（旧 TEXT 合成值）不再落库——repository_binding.installation_id 为
  //    BIGINT 且复合 FK，真实绑定以 ghapp-binding/安装回调/webhook 为权威路径；
  //  * installationState/grantedScopes 无对应列（安装态并入 binding_state 生命周期；
  //    授权快照仅 MU_REPO_BOUND 审计留痕），接受不入库；upsert 恒 (re)activation（active）。
  // mu.binding 自 v18 起冻结：新代码零写入（老镜像 ≤v17 仍读，向下兼容）。
  async function ensureBinding({ tenantId, repoId, kind = 'fixture', installationId = null, installationState = 'active', grantedScopes = [], createdBy = null }) {
    const repoRow = await q(
      `SELECT owner, name, default_branch FROM mu.repository WHERE repo_id=$1 AND tenant_id=$2`,
      [repoId, tenantId]);
    const repo = repoRow.rows[0];
    if (!repo) return null; // 仓库不存在/跨租户（旧实现此处由复合 FK 拒绝——前置同形失败）
    const instId = muFixtureInstallationIdOf(tenantId);
    await q(
      `INSERT INTO mu.github_app_installation
           (installation_id, tenant_id, account_id, account_login, account_type, app_id)
       VALUES ($1,$2,0,$3,'fixture',0) ON CONFLICT (installation_id) DO NOTHING`,
      [instId, tenantId, repo.owner]);
    const r = await q(
      `INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name,
           installation_id, default_branch, binding_state, created_by)
       VALUES ($1,$2,$3,$4,$5,$6,$7,'active',$8)
       ON CONFLICT (tenant_id, repo_id) DO UPDATE SET
         owner = EXCLUDED.owner, name = EXCLUDED.name,
         installation_id = EXCLUDED.installation_id, default_branch = EXCLUDED.default_branch,
         binding_state = 'active', revoked_at = NULL, error_code = NULL, updated_at = now()
       RETURNING *`, [tenantId, repoId, muFixtureRepoIdOf(tenantId, repoId),
        repo.owner, repo.name, instId, repo.default_branch, createdBy]);
    return r.rows[0];
  }
  async function getBindingForRepo(tenantId, repoId) {
    // v18 统一：repair 门读 repository_binding（binding_state='active' 已涵盖旧
    // state+installation_state 双 active 语义——installation suspend/delete 生命周期
    // 事件由 setBindingsStateForInstallation 落到同一列）。
    const r = await q(
      `SELECT * FROM mu.repository_binding WHERE tenant_id=$1 AND repo_id=$2 AND binding_state='active'
       ORDER BY created_at LIMIT 1`, [tenantId, repoId]);
    return r.rows[0] ?? null;
  }
  async function revokeBinding(tenantId, repoId) {
    // v18 统一：吊销作用于 repository_binding（(tenant,repo) 唯一行——fixture 域与
    // GHApp 权威域同表同行，不存在旧双表分裂下的漏吊销面）。
    const r = await q(
      `UPDATE mu.repository_binding SET binding_state='revoked', revoked_at=now(), updated_at=now()
        WHERE tenant_id=$1 AND repo_id=$2 AND binding_state='active' RETURNING *`, [tenantId, repoId]);
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
  // rc.10 PR-E（ISO-3）：支持 tenantId 过滤——tick 会话只领取/回显本租户 job
  //（mu.job 自 v1 起有 tenant_id 列；跨租户 job 对该 tick 不可见不可领取）。
  async function claimNextJob({ kinds = null, tenantId = null } = {}) {
    const params = [];
    let where = `state='queued'`;
    if (kinds) { params.push(kinds); where += ` AND kind = ANY($${params.length}::text[])`; }
    if (tenantId) { params.push(tenantId); where += ` AND tenant_id = $${params.length}::uuid`; }
    const r = await q(
      `UPDATE mu.job SET state='running', locked_at=now(), updated_at=now()
        WHERE job_id = (SELECT job_id FROM mu.job WHERE ${where}
          ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
       RETURNING *`, params);
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
  // rc.10 PR-E（ISO-3）：tenantId 过滤——tick 的 processed[] 只回显本租户 job。
  async function requeueOrphanedJobs({ staleMinutes = 10, limit = 10, tenantId = null } = {}) {
    const params = [staleMinutes, limit];
    let tenantClause = '';
    if (tenantId) { params.push(tenantId); tenantClause = ` AND tenant_id = $${params.length}::uuid`; }
    const r = await q(
      `UPDATE mu.job SET state='queued', locked_at=NULL, updated_at=now()
        WHERE job_id IN (SELECT job_id FROM mu.job
          WHERE state='running' AND locked_at IS NOT NULL
            AND locked_at < now() - make_interval(mins => $1::int)${tenantClause}
          ORDER BY locked_at LIMIT $2)
       RETURNING job_id, tenant_id, kind`, params);
    return r.rows;
  }
  // Wave 3.8（HOL）：非 fixture 模式人工 job 无消费者——超过宽限期的孤立人工 job
  // 转终态 rejected（有限批量；reason 写入 result，审计由调用方落）。
  // rc.10 PR-E（ISO-3）：tenantId 过滤——清收回显同样只含本租户。
  async function rejectStaleManualJobs({ expiryHours = 24, limit = 10, tenantId = null } = {}) {
    const params = [expiryHours, limit];
    let tenantClause = '';
    if (tenantId) { params.push(tenantId); tenantClause = ` AND tenant_id = $${params.length}::uuid`; }
    const r = await q(
      `UPDATE mu.job SET state='rejected',
          result = jsonb_build_object('reason','manual_job_expired_unconsumable',
            'detail','manual jobs have no consumer without MU_FIXTURES=1 or a dedicated worker; expired after grace period'),
          updated_at=now()
        WHERE job_id IN (SELECT job_id FROM mu.job
          WHERE state='queued' AND kind <> 'event_sync'
            AND created_at < now() - make_interval(hours => $1::int)${tenantClause}
          ORDER BY created_at LIMIT $2)
       RETURNING job_id, tenant_id, kind, created_at`, params);
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
  // rc.14（MT-ONB-1/2）：全部可认领邀请——与 findClaimableInvitation 同一过滤语义
  // （claimed_at IS NULL / 未过期 / subject 精确匹配；表无 revoked/state 列），
  // 不做 LIMIT：多条有效邀请必须由调用方显式消歧（invitation_ambiguous），
  // 禁止隐式选最旧。
  async function findClaimableInvitationsAll({ subject = null }) {
    if (!subject) return [];
    const r = await q(
      `SELECT * FROM mu.invitation
        WHERE claimed_at IS NULL AND expires_at > now() AND expected_subject = $1
        ORDER BY created_at`, [subject]);
    return r.rows;
  }
  async function claimInvitation(inviteId, userId) {
    const r = await q(
      `UPDATE mu.invitation SET claimed_at=now(), claimed_by_user_id=$2
        WHERE invite_id=$1 AND claimed_at IS NULL AND expires_at > now()
       RETURNING *`, [inviteId, userId]);
    return r.rows[0] ?? null;
  }

  // ── 邀请认领入驻（事务化；2026-10-10 生产角色覆盖事故修复）──
  // 事故：claimInvitation+ensureMembership 两条独立语句，且 ensureMembership 对
  // 已有 membership 无条件覆盖（role 降级 + revoked 复活）——生产实证：maintainer
  // 被 contributor 邀请自动认领覆盖（audit MU_MEMBERSHIP_ROLE_RESTORED 可查）。
  // 语义（单事务、CAS 认领、FOR UPDATE 锁定目标行——非先 SELECT 后写）：
  //  * 无 membership → 正常入驻（role=邀请角色）；
  //  * 已有 active membership → 保留实际角色，邀请记已处理不改写权限；
  //  * revoked membership → 保留 revoked（普通登录不顺带激活；重新加入须管理员授权）；
  //  * 返回 finalRole=最终实际角色——调用方（会话/审计）必须用它，不得用 invitation.role。
  // 正常成员管理（管理员调整/降级/撤销）不经此路径，语义不变。
  async function claimInvitationOnboard({ inviteId, userId, invitedRole }) {
    const client = await pool.connect();
    try {
      await client.query('BEGIN');
      const inv = await client.query(
        `UPDATE mu.invitation SET claimed_at=now(), claimed_by_user_id=$2
          WHERE invite_id=$1 AND claimed_at IS NULL AND expires_at > now()
         RETURNING tenant_id`, [inviteId, userId]);
      if (!inv.rows.length) { await client.query('ROLLBACK'); return { ok: false, reason: 'not_claimable' }; }
      const tenantId = inv.rows[0].tenant_id;
      const existing = await client.query(
        `SELECT role, state FROM mu.membership WHERE tenant_id=$1 AND user_id=$2 FOR UPDATE`,
        [tenantId, userId]);
      let finalRole; let outcome;
      if (!existing.rows.length) {
        const ins = await client.query(
          `INSERT INTO mu.membership (tenant_id, user_id, role) VALUES ($1,$2,$3) RETURNING role`,
          [tenantId, userId, invitedRole]);
        finalRole = ins.rows[0].role; outcome = 'onboarded';
      } else if (existing.rows[0].state === 'active') {
        finalRole = existing.rows[0].role; outcome = 'preserved_active';
      } else {
        finalRole = existing.rows[0].role; outcome = 'preserved_revoked';
      }
      await client.query('COMMIT');
      return { ok: true, tenantId, role: finalRole, outcome };
    } catch (e) {
      try { await client.query('ROLLBACK'); } catch { /* 连接已失效 */ }
      throw e;
    } finally {
      client.release();
    }
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

  // ── ForgeAdapter 连接模型（v24；Gitee 首版 G-1）──
  async function ensureForgeInstance({ instanceId, forgeKind, apiBase, webBase, capability = {} }) {
    await q(
      `INSERT INTO mu.forge_instance (instance_id, forge_kind, api_base, web_base, capability)
       VALUES ($1,$2,$3,$4,$5::jsonb) ON CONFLICT (instance_id) DO NOTHING`,
      [instanceId, forgeKind, apiBase, webBase, JSON.stringify(capability)]);
    const r = await q(`SELECT * FROM mu.forge_instance WHERE instance_id=$1`, [instanceId]);
    return r.rows[0] ?? null;
  }
  async function createForgeConnection({ tenantId, instanceId, credentialRef, webhookMode, status = 'pending' }) {
    const r = await q(
      `INSERT INTO mu.forge_connection (tenant_id, instance_id, credential_ref, webhook_mode, status)
       VALUES ($1,$2,$3,$4,$5)
       ON CONFLICT (tenant_id, instance_id) DO UPDATE
         SET credential_ref = EXCLUDED.credential_ref, webhook_mode = EXCLUDED.webhook_mode,
             status = EXCLUDED.status, status_reason = NULL, revoked_at = NULL, updated_at = now()
       RETURNING *`,
      [tenantId, instanceId, credentialRef, webhookMode, status]);
    return r.rows[0] ?? null;
  }
  async function listForgeConnections(tenantId) {
    const r = await q(
      `SELECT fc.connection_id, fc.tenant_id, fc.instance_id, fi.forge_kind, fi.api_base, fi.web_base,
              fi.capability, fc.credential_ref, fc.webhook_mode, fc.status, fc.status_reason,
              fc.verified_at, fc.revoked_at, fc.created_at, fc.updated_at
         FROM mu.forge_connection fc JOIN mu.forge_instance fi ON fi.instance_id = fc.instance_id
        WHERE fc.tenant_id = $1 ORDER BY fc.created_at`, [tenantId]);
    return r.rows;
  }
  async function getForgeConnection(tenantId, connectionId) {
    // tenant 维度收窄：跨租户连接 id 一律 not_found（不泄露存在性）
    const r = await q(
      `SELECT fc.*, fi.forge_kind, fi.api_base, fi.web_base, fi.capability
         FROM mu.forge_connection fc JOIN mu.forge_instance fi ON fi.instance_id = fc.instance_id
        WHERE fc.tenant_id = $1 AND fc.connection_id = $2`, [tenantId, connectionId]);
    return r.rows[0] ?? null;
  }
  async function setForgeConnectionStatus(tenantId, connectionId, status, { reason = null, verified = false } = {}) {
    const r = await q(
      `UPDATE mu.forge_connection
          SET status=$3, status_reason=$4,
              verified_at = CASE WHEN $5 THEN now() ELSE verified_at END,
              updated_at = now()
        WHERE tenant_id=$1 AND connection_id=$2 RETURNING *`,
      [tenantId, connectionId, status, reason, verified]);
    return r.rows[0] ?? null;
  }
  async function revokeForgeConnection(tenantId, connectionId) {
    const r = await q(
      `UPDATE mu.forge_connection
          SET status='revoked', revoked_at=now(), updated_at=now()
        WHERE tenant_id=$1 AND connection_id=$2 RETURNING *`,
      [tenantId, connectionId]);
    return r.rows[0] ?? null;
  }
  /**
   * 服务端归属解析（webhook/消费者共用）：按实例+原生仓库 id 反查 active 连接与仓库行。
   * 授权面完全来自 DB 行（payload 的 connection_id/tenant_id 不参与定位）——
   * 消费者信任的是服务端解析结果，不是事件自报身份（G-3 纪律）。
   * 返回 {ok, connection, repo} | {ok:false, reason}。
   */
  async function getActiveGiteeConnectionForRepo({ instanceId = 'gitee-cloud', providerRepoId }) {
    const r = await q(
      `SELECT fc.*, fi.forge_kind, fi.api_base, fi.web_base,
              rep.repo_id AS gitee_repo_id, rep.provider_repo_id,
              rep.owner AS repo_owner, rep.name AS repo_name
         FROM mu.forge_connection fc
         JOIN mu.forge_instance fi ON fi.instance_id = fc.instance_id
         JOIN mu.repository rep ON rep.tenant_id = fc.tenant_id
              AND rep.forge_instance_id = fc.instance_id AND rep.provider_repo_id = $2
        WHERE fc.instance_id = $1 AND fc.status = 'valid' AND fc.revoked_at IS NULL
          AND rep.provider = 'gitee'
        LIMIT 1`,
      [instanceId, String(providerRepoId)]);
    const row = r.rows[0];
    if (!row) return { ok: false, reason: 'forge_binding_not_found' };
    return { ok: true, connection: row,
      repo: { repo_id: row.gitee_repo_id, tenant_id: row.tenant_id,
        owner: row.repo_owner, name: row.repo_name, provider_repo_id: String(row.provider_repo_id) } };
  }

  async function listInstallations(tenantId) {
    // v18：fixture 域合成安装（account_type='fixture'，id 落保留区间）为内部脚手架，
    // 不属用户可见 installation——列表过滤（真实 GitHub installation id 远低于保留区间）。
    const r = await q(
      `SELECT * FROM mu.github_app_installation
        WHERE tenant_id=$1 AND installation_id < $2 ORDER BY created_at`,
      [tenantId, muFixtureInstallationBase]);
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
  // rc.10 PR-E（ISO-5）：claim 发生在 installation→tenant 解析之前（tenant NULL 落行）——
  // 解析完成后由 webhook 处理链回填归属（只补 NULL 行，绝不覆盖既有归属）。
  async function backfillWebhookDeliveryTenant(deliveryId, tenantId) {
    if (!deliveryId || !tenantId) return false;
    const r = await q(
      `UPDATE mu.webhook_delivery SET tenant_id=$2 WHERE delivery_id=$1 AND tenant_id IS NULL`,
      [deliveryId, tenantId]);
    return r.rowCount > 0;
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
    createInvitation, listInvitations, getInvitation, findClaimableInvitation, findClaimableInvitationsAll, claimInvitation,
    upsertInstallation, getInstallation, listInstallations, setInstallationState,
    upsertRepositoryBinding, getBindingByRepo, setBindingState, setBindingsStateForInstallation,
    claimWebhookDelivery, backfillWebhookDeliveryTenant, finishWebhookDelivery, requeueJob,
    claimInvitationOnboard,

    // ── ForgeAdapter 连接模型（v24；Gitee 首版 G-1）──
    // 凭据红线：credential_ref 只存引用字符串（'env:MU_GITEE_PAT'），本层任何 API
    // 不接受/不返回令牌值。全部读路径带 tenant 维度（跨租户天然拒绝）。
    ensureForgeInstance, createForgeConnection, listForgeConnections, getForgeConnection,
    setForgeConnectionStatus, revokeForgeConnection,
    getActiveGiteeConnectionForRepo,
  };
}
