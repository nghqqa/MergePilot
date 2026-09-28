// console/backend/lib/multiuser/schema.mjs — Developer Edition 多用户最小 schema（MU Phase 1/3）。
//
// 设计合同（对齐 MULTI_USER_RBAC_AND_REPO_BINDING_DESIGN_REPORT §1，缩小为 Developer 最小切片）：
//  * 全部实体带 tenant_id；Repository/Binding 用复合唯一约束（含 tenant 维度），
//    从 schema 层杜绝跨 tenant 关联/撞键；
//  * Membership 角色词汇 = Contributor/Reviewer/Maintainer/PlatformAdmin/Auditor（CHECK 约束）；
//  * 凭据红线：本 schema 不存任何 token/密码/密钥——ExternalIdentity 只存 provider+subject
//    （OAuth 身份标识），Binding 只存 installation id 与权限快照（granted_scopes）；
//  * 版本化迁移（mu.schema_migrations），替换"幂等 DDL 重放"的演进方式；
//  * 迁移用 tenant：bootstrap 创建 slug='default' + pilot 操作员 → PlatformAdmin 映射。
//
// 不做（Enterprise/后续阶段）：RLS、tenant 生命周期 API、SCIM/OIDC、配额、HA。

export const MU_MIGRATIONS = [
  {
    version: 1,
    name: 'mu_core_entities',
    sql: [
      `CREATE SCHEMA IF NOT EXISTS mu`,
      `CREATE TABLE IF NOT EXISTS mu.tenant (
         tenant_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         slug        TEXT NOT NULL UNIQUE,
         display_name TEXT NOT NULL,
         is_migration_tenant BOOLEAN NOT NULL DEFAULT false,
         state       TEXT NOT NULL DEFAULT 'active',
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
       )`,
      `CREATE TABLE IF NOT EXISTS mu.app_user (
         user_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         login       TEXT NOT NULL UNIQUE,
         display_name TEXT,
         state       TEXT NOT NULL DEFAULT 'active',
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
       )`,
      // 外部身份 → 用户的映射。仅存 provider+subject（稳定标识），绝不存访问令牌。
      `CREATE TABLE IF NOT EXISTS mu.external_identity (
         identity_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         user_id     UUID NOT NULL REFERENCES mu.app_user(user_id),
         provider    TEXT NOT NULL,
         subject     TEXT NOT NULL,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (provider, subject)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.membership (
         membership_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id   UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         user_id     UUID NOT NULL REFERENCES mu.app_user(user_id),
         role        TEXT NOT NULL CHECK (role IN
                     ('contributor','reviewer','maintainer','platform_admin','auditor')),
         state       TEXT NOT NULL DEFAULT 'active',
         granted_by  UUID,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (tenant_id, user_id)
       )`,
      `CREATE INDEX IF NOT EXISTS mu_membership_user_idx ON mu.membership (user_id, state)`,
      // provider_repo_id 允许跨 tenant 重复（同名/同 id 仓库可属不同 tenant）——
      // 唯一性在 (tenant_id, provider, provider_repo_id) 复合层，天然隔离。
      `CREATE TABLE IF NOT EXISTS mu.repository (
         repo_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id   UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         provider    TEXT NOT NULL,
         provider_repo_id TEXT NOT NULL,
         owner       TEXT NOT NULL,
         name        TEXT NOT NULL,
         default_branch TEXT,
         state       TEXT NOT NULL DEFAULT 'active',
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (tenant_id, provider, provider_repo_id)
       )`,
      // 仓库↔provider 连接。installation_id 为 GitHub App 安装标识（测试用合成值）；
      // granted_scopes 是授权快照（对齐设计报告 §6 最小权限清单）——不含任何令牌。
      `CREATE TABLE IF NOT EXISTS mu.binding (
         binding_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id   UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         repo_id     UUID NOT NULL REFERENCES mu.repository(repo_id),
         kind        TEXT NOT NULL CHECK (kind IN ('github_app_installation','oauth_user','fixture')),
         installation_id TEXT,
         installation_state TEXT NOT NULL DEFAULT 'active',
         granted_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
         state       TEXT NOT NULL DEFAULT 'active',
         created_by  UUID,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (tenant_id, repo_id, kind)
       )`,
      // 审计（tenant 维度；metadata only——调用方保证不写代码内容/查询正文）
      `CREATE TABLE IF NOT EXISTS mu.audit_event (
         seq         BIGSERIAL PRIMARY KEY,
         tenant_id   UUID,
         actor_user_id UUID,
         kind        TEXT NOT NULL,
         detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
       )`,
      `CREATE INDEX IF NOT EXISTS mu_audit_tenant_idx ON mu.audit_event (tenant_id, seq)`,
    ],
  },
  {
    version: 2,
    name: 'mu_pr_review_job',
    sql: [
      // PR 快照（append 语义：新 head_sha = 新行；唯一键含 tenant/repo——同 number+head_sha
      // 在不同 tenant 各自独立，不可能共享行）。
      `CREATE TABLE IF NOT EXISTS mu.pull_request (
         pr_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id   UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         repo_id     UUID NOT NULL REFERENCES mu.repository(repo_id),
         provider_pr_number BIGINT NOT NULL,
         head_sha    TEXT NOT NULL,
         head_ref    TEXT,
         base_ref    TEXT,
         title       TEXT,
         state       TEXT NOT NULL DEFAULT 'open',
         branch_protection_status TEXT NOT NULL DEFAULT 'unknown',
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (tenant_id, repo_id, provider_pr_number, head_sha)
       )`,
      // 审查记录（append-only）：绑定 tenant/repo/pr/head_sha；merge 相关结论必须
      // 携带 branch_protection_status（unknown 禁止可合并结论——服务层强制）。
      `CREATE TABLE IF NOT EXISTS mu.review_record (
         review_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id   UUID NOT NULL,
         repo_id     UUID NOT NULL,
         pr_id       UUID NOT NULL REFERENCES mu.pull_request(pr_id),
         kind        TEXT NOT NULL CHECK (kind IN
                     ('ai_review','human_decision','merge_decision','repair_record')),
         actor_user_id UUID,
         decision    TEXT,
         head_sha    TEXT NOT NULL,
         branch_protection_status TEXT NOT NULL DEFAULT 'unknown',
         payload_sha256 TEXT,
         detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
       )`,
      `CREATE INDEX IF NOT EXISTS mu_review_pr_idx ON mu.review_record (tenant_id, pr_id, created_at)`,
      // 受控写任务（review_run / repair_push）。执行器必须在认领时复查
      // 请求者 Membership 与 Binding——撤销后拒绝执行（fail-closed）。
      `CREATE TABLE IF NOT EXISTS mu.job (
         job_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id   UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         repo_id     UUID NOT NULL REFERENCES mu.repository(repo_id),
         pr_id       UUID REFERENCES mu.pull_request(pr_id),
         kind        TEXT NOT NULL CHECK (kind IN ('review_run','repair_push')),
         state       TEXT NOT NULL DEFAULT 'queued' CHECK (state IN
                     ('queued','running','done','rejected','failed')),
         requested_by UUID NOT NULL,
         requested_role TEXT NOT NULL,
         payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
         result      JSONB,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
       )`,
      `CREATE INDEX IF NOT EXISTS mu_job_state_idx ON mu.job (state, created_at)`,
    ],
  },
  {
    // Beta Hardening W1：数据库级跨租户一致性 + 审计归属。
    // 设计要点：
    //  * 父表先建复合 UNIQUE（repository(tenant,repo) / pull_request(tenant,pr)），
    //    子表挂复合 FOREIGN KEY——DB 直接拒绝跨租户 Repository/Binding/PR/
    //    ReviewRecord/Job 组合，不依赖应用层检查；
    //  * 既有单列 FK 保留（加法迁移，不 DROP 任何既有约束）；
    //  * audit_event.tenant_id 收紧 NOT NULL（tenant 域事件必须归属）；
    //    platform 域事件独立表 mu.platform_audit_event 建模——不用 nullable
    //    表达歧义、不造虚假 tenant；
    //  * 全部语句幂等（pg_constraint 目录守卫 / IF NOT EXISTS / SET NOT NULL 天然幂等），
    //    迁移框架按版本只放行一次，部分失败后可直接重放本版本。
    // 回滚说明（需 DBA 执行，向下兼容应用层）：
    //   ALTER TABLE mu.binding        DROP CONSTRAINT mu_binding_tenant_repo_fk;
    //   ALTER TABLE mu.pull_request   DROP CONSTRAINT mu_pr_tenant_repo_fk;
    //   ALTER TABLE mu.review_record  DROP CONSTRAINT mu_review_tenant_repo_fk, DROP CONSTRAINT mu_review_tenant_pr_fk, DROP CONSTRAINT mu_review_tenant_repo_pr_fk;
    //   ALTER TABLE mu.job            DROP CONSTRAINT mu_job_tenant_repo_fk,  DROP CONSTRAINT mu_job_tenant_pr_fk, DROP CONSTRAINT mu_job_tenant_repo_pr_fk;
    //   ALTER TABLE mu.repository     DROP CONSTRAINT mu_repository_tenant_repo_uk;
    //   ALTER TABLE mu.pull_request   DROP CONSTRAINT mu_pull_request_tenant_pr_uk, DROP CONSTRAINT mu_pull_request_tenant_repo_pr_uk;
    //   ALTER TABLE mu.audit_event    ALTER COLUMN tenant_id DROP NOT NULL;
    //   DROP TABLE IF EXISTS mu.platform_audit_event;
    //   DELETE FROM mu.schema_migrations WHERE version = 3;
    version: 3,
    name: 'mu_cross_tenant_constraints',
    sql: [
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_repository_tenant_repo_uk') THEN
           ALTER TABLE mu.repository ADD CONSTRAINT mu_repository_tenant_repo_uk UNIQUE (tenant_id, repo_id);
         END IF;
       END $$`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_pull_request_tenant_pr_uk') THEN
           ALTER TABLE mu.pull_request ADD CONSTRAINT mu_pull_request_tenant_pr_uk UNIQUE (tenant_id, pr_id);
         END IF;
       END $$`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_pull_request_tenant_repo_pr_uk') THEN
           ALTER TABLE mu.pull_request ADD CONSTRAINT mu_pull_request_tenant_repo_pr_uk UNIQUE (tenant_id, repo_id, pr_id);
         END IF;
       END $$`,
      `ALTER TABLE mu.audit_event ALTER COLUMN tenant_id SET NOT NULL`,
      `CREATE TABLE IF NOT EXISTS mu.platform_audit_event (
         seq         BIGSERIAL PRIMARY KEY,
         actor_user_id UUID,
         kind        TEXT NOT NULL,
         detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
       )`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_binding_tenant_repo_fk') THEN
           ALTER TABLE mu.binding ADD CONSTRAINT mu_binding_tenant_repo_fk
             FOREIGN KEY (tenant_id, repo_id) REFERENCES mu.repository (tenant_id, repo_id);
         END IF;
       END $$`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_pr_tenant_repo_fk') THEN
           ALTER TABLE mu.pull_request ADD CONSTRAINT mu_pr_tenant_repo_fk
             FOREIGN KEY (tenant_id, repo_id) REFERENCES mu.repository (tenant_id, repo_id);
         END IF;
       END $$`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_review_tenant_repo_fk') THEN
           ALTER TABLE mu.review_record ADD CONSTRAINT mu_review_tenant_repo_fk
             FOREIGN KEY (tenant_id, repo_id) REFERENCES mu.repository (tenant_id, repo_id);
         END IF;
       END $$`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_review_tenant_pr_fk') THEN
           ALTER TABLE mu.review_record ADD CONSTRAINT mu_review_tenant_pr_fk
             FOREIGN KEY (tenant_id, pr_id) REFERENCES mu.pull_request (tenant_id, pr_id);
         END IF;
       END $$`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_job_tenant_repo_fk') THEN
           ALTER TABLE mu.job ADD CONSTRAINT mu_job_tenant_repo_fk
             FOREIGN KEY (tenant_id, repo_id) REFERENCES mu.repository (tenant_id, repo_id);
         END IF;
       END $$`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_job_tenant_pr_fk') THEN
           ALTER TABLE mu.job ADD CONSTRAINT mu_job_tenant_pr_fk
             FOREIGN KEY (tenant_id, pr_id) REFERENCES mu.pull_request (tenant_id, pr_id);
         END IF;
       END $$`,
      // PR250 复核 P1 修复：三列复合 FK——同 tenant 内 repo/pr 交叉组合也由 DB 拒绝
      // （业务模型要求 review/job 的 repo 与 pr 一致：应用层全部写入方均由 pr 行派生
      // repo_id；此前 (tenant,repo)+(tenant,pr) 两两校验放行了 (repoA, prB) 组合）
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_review_tenant_repo_pr_fk') THEN
           ALTER TABLE mu.review_record ADD CONSTRAINT mu_review_tenant_repo_pr_fk
             FOREIGN KEY (tenant_id, repo_id, pr_id) REFERENCES mu.pull_request (tenant_id, repo_id, pr_id);
         END IF;
       END $$`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_job_tenant_repo_pr_fk') THEN
           ALTER TABLE mu.job ADD CONSTRAINT mu_job_tenant_repo_pr_fk
             FOREIGN KEY (tenant_id, repo_id, pr_id) REFERENCES mu.pull_request (tenant_id, repo_id, pr_id);
         END IF;
       END $$`,
    ],
  },
  {
    // Beta Identity Wave 2A：GitHub OAuth 登录 + 数据库持久化安全会话 + 邀请。
    // 红线：
    //  * session/oauth_flow/invitation 一律只存摘要（sha256），绝不存明文 token/state；
    //  * oauth_flow.state 高熵单次消费（consumed_at CAS）+ 短 TTL；
    //  * invitation 短期单次（claimed_at CAS）绑定 tenant 与预期 GitHub 身份
    //    （expected_subject=github-oauth:<数字id> 或 expected_login 句柄）；
    //  * 身份键 = GitHub 数字 user id（subject），不按 login/email 合并；
    //    login 仅作邀请匹配句柄，改名后身份不变；
    //  * 回滚：DROP TABLE mu.oauth_flow/mu.session/mu.invitation; DELETE FROM
    //    mu.schema_migrations WHERE version=4;（均为新表，向下兼容——移除后
    //    fixture 登录与 legacy 面不受影响，仅 OAuth/持久会话能力消失）。
    version: 4,
    name: 'mu_oauth_session_invitation',
    sql: [
      `CREATE TABLE IF NOT EXISTS mu.oauth_flow (
         flow_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         state_hash  TEXT NOT NULL UNIQUE,
         invite_id   UUID,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         expires_at  TIMESTAMPTZ NOT NULL,
         consumed_at TIMESTAMPTZ
       )`,
      `CREATE INDEX IF NOT EXISTS mu_oauth_flow_exp_idx ON mu.oauth_flow (expires_at)`,
      `CREATE TABLE IF NOT EXISTS mu.session (
         session_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         token_hash  TEXT NOT NULL UNIQUE,
         user_id     UUID NOT NULL REFERENCES mu.app_user(user_id),
         tenant_id   UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         csrf_hash   TEXT NOT NULL,
         login       TEXT NOT NULL,
         role_snapshot TEXT NOT NULL,
         provider    TEXT NOT NULL,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         expires_at  TIMESTAMPTZ NOT NULL,
         revoked_at  TIMESTAMPTZ,
         revoke_reason TEXT
       )`,
      `CREATE INDEX IF NOT EXISTS mu_session_user_idx ON mu.session (user_id, revoked_at)`,
      `CREATE TABLE IF NOT EXISTS mu.invitation (
         invite_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id   UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         role        TEXT NOT NULL CHECK (role IN
                     ('contributor','reviewer','maintainer','platform_admin','auditor')),
         expected_subject TEXT,
         expected_login   TEXT,
         note        TEXT,
         created_by  UUID,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         expires_at  TIMESTAMPTZ NOT NULL,
         claimed_at  TIMESTAMPTZ,
         claimed_by_user_id UUID
       )`,
      `CREATE INDEX IF NOT EXISTS mu_invitation_claim_idx ON mu.invitation (tenant_id, claimed_at)`,
    ],
  },
  {
    // Beta Identity Wave 2A.1：login-CSRF 收尾——OAuth flow 增 correlation cookie 摘要。
    //  * start 时签发一次性 mu_oauth_corr cookie（HttpOnly/SameSite=Lax/Path=/，
    //    prod 强制 Secure），其 sha256 存 corr_hash；callback 必须同时匹配
    //    state 摘要与 correlation 摘要——跨浏览器/缺失/错配/重放一律 state_invalid；
    //  * 存量 2A flow（corr_hash NULL）：callback 拒绝（fail-closed——旧 flow 仅存活
    //    ≤10min，重发起即可，无迁移负担）；
    //  * 回滚：ALTER TABLE mu.oauth_flow DROP COLUMN IF EXISTS corr_hash;
    //    DELETE FROM mu.schema_migrations WHERE version=5;（移除后 callback 校验
    //    分支自然失效需同版本代码回滚——纯加列向下兼容存储层）。
    version: 5,
    name: 'mu_oauth_correlation',
    sql: [
      `ALTER TABLE mu.oauth_flow ADD COLUMN IF NOT EXISTS corr_hash TEXT`,
    ],
  },
  {
    // Wave 2B：GitHub App 只读安装 + 仓库绑定 + webhook 验签/去重。
    // 红线：
    //  * github_app_installation / repository_binding 零凭据列（不存 private key/
    //    token/webhook secret——仅数字 id 与展示字段）；
    //  * installation 为 tenant 域资产（安装回调时由发起租户认领）；复合 FK
    //    (tenant_id, installation_id) 保证绑定不得指向他租户 installation；
    //  * github_repo_id 全局唯一——同一 GitHub 仓库默认只允许一个租户绑定
    //    （跨租户双绑定为设计禁止；DB 层 UNIQUE 直接拒绝）；
    //  * 数字 id 为稳定主标识：owner/name 仅展示缓存，改名不产生第二逻辑仓库
    //    （repository.provider_repo_id 存 String(github_repo_id)，ensure 幂等）；
    //  * webhook_delivery.delivery_id PK 去重——重复 delivery 不重复写入/入队/审计；
    //  * oauth_flow 增 purpose 列区分登录流与安装流（复用 state+corr 安全机制）；
    //  * job kind 增 'event_sync'（webhook 仓库级事件异步处理）。
    // 回滚：DROP TABLE mu.webhook_delivery, mu.repository_binding, mu.github_app_installation;
    //   ALTER TABLE mu.oauth_flow DROP COLUMN IF EXISTS purpose;
    //   ALTER TABLE mu.job DROP CONSTRAINT mu_job_kind_check2, ADD CONSTRAINT ... 原词表（或保留——加法枚举向下兼容）;
    //   DELETE FROM mu.schema_migrations WHERE version=6;
    version: 6,
    name: 'mu_github_app_binding',
    sql: [
      `ALTER TABLE mu.oauth_flow ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'oauth_login'`,
      `CREATE TABLE IF NOT EXISTS mu.github_app_installation (
         installation_id BIGINT PRIMARY KEY,
         tenant_id   UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         account_id  BIGINT NOT NULL,
         account_login TEXT NOT NULL,
         account_type  TEXT NOT NULL DEFAULT 'User',
         app_id      BIGINT NOT NULL,
         suspended_at TIMESTAMPTZ,
         revoked_at  TIMESTAMPTZ,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (tenant_id, installation_id)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.repository_binding (
         binding_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id   UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         repo_id     UUID NOT NULL REFERENCES mu.repository(repo_id),
         github_repo_id BIGINT NOT NULL,
         owner       TEXT NOT NULL,
         name        TEXT NOT NULL,
         installation_id BIGINT NOT NULL,
         default_branch TEXT,
         binding_state TEXT NOT NULL DEFAULT 'active' CHECK (binding_state IN
                     ('active','suspended','revoked','error')),
         last_sync_at TIMESTAMPTZ,
         revoked_at  TIMESTAMPTZ,
         error_code  TEXT,
         created_by  UUID,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (tenant_id, repo_id),
         UNIQUE (github_repo_id),
         FOREIGN KEY (tenant_id, installation_id)
           REFERENCES mu.github_app_installation (tenant_id, installation_id)
       )`,
      `CREATE INDEX IF NOT EXISTS mu_binding_install_idx ON mu.repository_binding (installation_id, binding_state)`,
      `CREATE TABLE IF NOT EXISTS mu.webhook_delivery (
         delivery_id TEXT PRIMARY KEY,
         tenant_id   UUID,
         installation_id BIGINT,
         event      TEXT NOT NULL,
         received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         processed_at TIMESTAMPTZ,
         state      TEXT NOT NULL DEFAULT 'received' CHECK (state IN
                     ('received','processed','duplicate','rejected'))
       )`,
      `ALTER TABLE mu.job DROP CONSTRAINT IF EXISTS job_kind_check`,
      `ALTER TABLE mu.job DROP CONSTRAINT IF EXISTS mu_job_kind_check`,
      `ALTER TABLE mu.job ALTER COLUMN requested_by DROP NOT NULL`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_job_kind_check2') THEN
           ALTER TABLE mu.job ADD CONSTRAINT mu_job_kind_check2 CHECK (kind IN
             ('review_run','repair_push','event_sync'));
         END IF;
       END $$`,
    ],
  },
  {
    // Wave 3 PR-A：审查编排数据模型（七个实体 + 复合 FK 防跨 tenant/repo/pr 组合）。
    // 回滚：DELETE FROM mu.schema_migrations WHERE version = 7; 再 DROP 七表与
    // mu_pull_request_composite_uk（纯新增，向下兼容）。
    // 契约要点：
    //  * review_run UNIQUE(tenant,repo,pr,head_sha) —— 同 PR 新 head 必建新 run，
    //    旧 head 结果永不被覆盖（查询按 head 隔离）；
    //  * 状态机 13 态见 orchestration.mjs RUN_STATES（DB CHECK 双保险）；
    //  * 所有子表经 (tenant_id, repo_id, pr_id, run_id) 复合 FK 锚定父 run——
    //    跨 tenant/repo/pr 的行在数据库层直接拒绝；
    //  * dead_letter 只存引用（payload_ref），不存 payload 正文/代码/diff。
    version: 7,
    name: 'mu_review_orchestration',
    sql: [
      `ALTER TABLE mu.pull_request DROP CONSTRAINT IF EXISTS mu_pr_composite_uk`,
      `ALTER TABLE mu.pull_request ADD CONSTRAINT mu_pr_composite_uk UNIQUE (tenant_id, repo_id, pr_id)`,
      `CREATE TABLE IF NOT EXISTS mu.review_run (
         run_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         repo_id UUID NOT NULL REFERENCES mu.repository(repo_id),
         pr_id   UUID NOT NULL,
         head_sha TEXT NOT NULL,
         status  TEXT NOT NULL DEFAULT 'RECEIVED' CHECK (status IN
                 ('RECEIVED','REVIEW_QUEUED','REVIEWING','REVIEWED','FIX_QUEUED','FIXING',
                  'VERIFY_QUEUED','VERIFYING','VERIFIED','REWORK_REQUIRED','BLOCKED',
                  'FAILED','COMPLETED')),
         trigger_source TEXT NOT NULL DEFAULT 'webhook' CHECK (trigger_source IN ('webhook','manual')),
         requested_by UUID,
         policy_version TEXT NOT NULL DEFAULT 'v1',
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (tenant_id, repo_id, pr_id, head_sha),
         UNIQUE (tenant_id, repo_id, pr_id, run_id),
         FOREIGN KEY (tenant_id, repo_id, pr_id)
           REFERENCES mu.pull_request (tenant_id, repo_id, pr_id)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.agent_attempt (
         attempt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         run_id  UUID NOT NULL,
         tenant_id UUID NOT NULL, repo_id UUID NOT NULL, pr_id UUID NOT NULL,
         head_sha TEXT NOT NULL,
         agent_role TEXT NOT NULL CHECK (agent_role IN ('leader','reviewer','fixer','verifier')),
         attempt INT NOT NULL CHECK (attempt >= 1),
         status TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING','DONE','FAILED','TIMEOUT','SKIPPED')),
         provider TEXT NOT NULL CHECK (provider IN ('deterministic','llm','mock','fxv')),
         actor_principal TEXT NOT NULL DEFAULT 'system:leader',
         input_digest TEXT, output_digest TEXT,
         model_id TEXT, prompt_version TEXT,
         latency_ms INT, token_count INT,
         error_code TEXT,
         evidence_ref TEXT NOT NULL DEFAULT '',
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (run_id, agent_role, attempt),
         UNIQUE (attempt_id, run_id),
         FOREIGN KEY (tenant_id, repo_id, pr_id, run_id)
           REFERENCES mu.review_run (tenant_id, repo_id, pr_id, run_id)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.agent_finding (
         finding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         attempt_id UUID NOT NULL,
         run_id  UUID NOT NULL,
         tenant_id UUID NOT NULL, repo_id UUID NOT NULL, pr_id UUID NOT NULL,
         head_sha TEXT NOT NULL,
         rule_id TEXT NOT NULL,
         severity TEXT NOT NULL CHECK (severity IN ('P0','P1','P2','P3')),
         confidence REAL NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
         path TEXT NOT NULL,
         line_start INT, line_end INT,
         title TEXT NOT NULL,
         evidence_ref TEXT NOT NULL DEFAULT '',
         remediation TEXT NOT NULL DEFAULT '',
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (run_id, rule_id, path, line_start),
         FOREIGN KEY (tenant_id, repo_id, pr_id, run_id)
           REFERENCES mu.review_run (tenant_id, repo_id, pr_id, run_id),
         FOREIGN KEY (attempt_id, run_id)
           REFERENCES mu.agent_attempt (attempt_id, run_id)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.fix_attempt (
         fix_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         run_id UUID NOT NULL,
         tenant_id UUID NOT NULL, repo_id UUID NOT NULL, pr_id UUID NOT NULL,
         head_sha TEXT NOT NULL,
         attempt INT NOT NULL CHECK (attempt >= 1),
         status TEXT NOT NULL DEFAULT 'PLANNED' CHECK (status IN ('PLANNED','DRY_RUN','FAILED','SKIPPED')),
         mode TEXT NOT NULL DEFAULT 'dry_run' CHECK (mode IN ('dry_run')),
         patch_digest TEXT,
         artifact_ref TEXT,
         evidence_ref TEXT NOT NULL DEFAULT '',
         error_code TEXT,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (run_id, attempt),
         UNIQUE (fix_id, run_id),
         FOREIGN KEY (tenant_id, repo_id, pr_id, run_id)
           REFERENCES mu.review_run (tenant_id, repo_id, pr_id, run_id)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.verification_attempt (
         verify_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         run_id UUID NOT NULL,
         fix_id UUID NOT NULL,
         tenant_id UUID NOT NULL, repo_id UUID NOT NULL, pr_id UUID NOT NULL,
         head_sha TEXT NOT NULL,
         attempt INT NOT NULL CHECK (attempt >= 1),
         verdict TEXT NOT NULL CHECK (verdict IN ('PASS','FAIL','BLOCKED')),
         evidence_ref TEXT NOT NULL DEFAULT '',
         error_code TEXT,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (run_id, attempt),
         FOREIGN KEY (tenant_id, repo_id, pr_id, run_id)
           REFERENCES mu.review_run (tenant_id, repo_id, pr_id, run_id),
         FOREIGN KEY (fix_id, run_id)
           REFERENCES mu.fix_attempt (fix_id, run_id)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.orchestration_decision (
         decision_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         run_id UUID NOT NULL,
         tenant_id UUID NOT NULL, repo_id UUID NOT NULL, pr_id UUID NOT NULL,
         head_sha TEXT NOT NULL,
         stage TEXT NOT NULL,
         decision TEXT NOT NULL,
         rationale_ref TEXT NOT NULL DEFAULT '',
         actor_principal TEXT NOT NULL DEFAULT 'system:leader',
         policy_version TEXT NOT NULL DEFAULT 'v1',
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         FOREIGN KEY (tenant_id, repo_id, pr_id, run_id)
           REFERENCES mu.review_run (tenant_id, repo_id, pr_id, run_id)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.dead_letter (
         dlq_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         run_id UUID, tenant_id UUID, repo_id UUID, pr_id UUID, head_sha TEXT,
         agent_role TEXT,
         job_id UUID,
         kind TEXT NOT NULL,
         reason TEXT NOT NULL,
         retry_count INT NOT NULL DEFAULT 0,
         payload_ref TEXT,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         resolved_at TIMESTAMPTZ
       )`,
      `CREATE INDEX IF NOT EXISTS mu_review_run_pr_idx ON mu.review_run (tenant_id, repo_id, pr_id, created_at DESC)`,
      `CREATE INDEX IF NOT EXISTS mu_agent_attempt_run_idx ON mu.agent_attempt (run_id, agent_role, attempt)`,
      `CREATE INDEX IF NOT EXISTS mu_agent_finding_run_idx ON mu.agent_finding (run_id, severity)`,
      `CREATE INDEX IF NOT EXISTS mu_dead_letter_open_idx ON mu.dead_letter (created_at DESC) WHERE resolved_at IS NULL`,
    ],
  },
  {
    // Wave 3 PR-B：finding 脱敏摘要列（任务书 PR-B §6 必填——evidence 脱敏纪律：
    // 只存打码后的行摘要，原始行/diff/源码不入库）。
    // 回滚：DELETE FROM mu.schema_migrations WHERE version=8;
    //       ALTER TABLE mu.agent_finding DROP COLUMN IF EXISTS summary_masked;
    version: 8,
    name: 'mu_review_finding_summary',
    sql: [
      `ALTER TABLE mu.agent_finding ADD COLUMN IF NOT EXISTS summary_masked TEXT NOT NULL DEFAULT ''`,
    ],
  },
  {
    // Wave 3 PR-C：死信上下文约束（PR-A P2-3 修复）——run_id 与 job_id 至少其一
    // 非空，杜绝无上下文死信（reason 必填已保底）。
    // 回滚：DELETE FROM mu.schema_migrations WHERE version=9;
    //       ALTER TABLE mu.dead_letter DROP CONSTRAINT IF EXISTS mu_dead_letter_ctx_check;
    version: 9,
    name: 'mu_dead_letter_context',
    sql: [
      `ALTER TABLE mu.dead_letter DROP CONSTRAINT IF EXISTS mu_dead_letter_ctx_check`,
      `ALTER TABLE mu.dead_letter ADD CONSTRAINT mu_dead_letter_ctx_check CHECK (run_id IS NOT NULL OR job_id IS NOT NULL)`,
    ],
  },
];

export const MU_SCHEMA_LATEST = MU_MIGRATIONS[MU_MIGRATIONS.length - 1].version;
