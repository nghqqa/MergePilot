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
import crypto from 'node:crypto';

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
         provider TEXT NOT NULL CHECK (provider IN ('deterministic','llm','mock','fxv','deterministic_mock','openai_compatible','agentteams')),
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
  {
    // Wave 3.1：agent_attempt.provider 枚举扩 LLM 三态（deterministic_mock/openai_compatible）。
    // 回滚：DELETE FROM mu.schema_migrations WHERE version=10;
    //       ALTER TABLE mu.agent_attempt DROP CONSTRAINT mu_agent_attempt_provider_check2;
    //       ALTER TABLE mu.agent_attempt ADD CONSTRAINT mu_agent_attempt_provider_check2
    //         CHECK (provider IN ('deterministic','llm','mock','fxv'));
    version: 10,
    name: 'mu_llm_provider_values',
    sql: [
      `ALTER TABLE mu.agent_attempt DROP CONSTRAINT IF EXISTS mu_agent_attempt_provider_check2`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_agent_attempt_provider_check2') THEN
           ALTER TABLE mu.agent_attempt ADD CONSTRAINT mu_agent_attempt_provider_check2
             CHECK (provider IN ('deterministic','llm','mock','fxv','deterministic_mock','openai_compatible','agentteams'));
         END IF;
       END $$`,
    ],
  },
  {
    // Wave 3.2：Agent 运行策略控制面（平台级单行；非敏感字段 only——
    // 无 base URL、无 API key、无凭据形状列；endpoint/key 仍部署级 env）。
    // 回滚：DELETE FROM mu.schema_migrations WHERE version=11;
    //       DROP TABLE IF EXISTS mu.agent_policy;
    //       ALTER TABLE mu.review_run DROP COLUMN IF EXISTS agent_policy_version;
    //       ALTER TABLE mu.review_run DROP COLUMN IF EXISTS llm_mode;
    version: 11,
    name: 'mu_agent_policy',
    sql: [
      `CREATE TABLE IF NOT EXISTS mu.agent_policy (
         id INT PRIMARY KEY CHECK (id = 1),
         mode TEXT NOT NULL DEFAULT 'deterministic_only'
           CHECK (mode IN ('deterministic_only','llm_assist')),
         provider TEXT NOT NULL DEFAULT 'openai_compatible'
           CHECK (provider IN ('openai_compatible')),
         model TEXT NOT NULL DEFAULT 'deepseek-flash'
           CHECK (model ~ '^[a-z0-9][a-z0-9._/-]{0,63}$'),
         timeout_ms INT NOT NULL DEFAULT 30000
           CHECK (timeout_ms >= 1000 AND timeout_ms <= 120000),
         max_output_tokens INT NOT NULL DEFAULT 1024
           CHECK (max_output_tokens >= 64 AND max_output_tokens <= 4096),
         enabled BOOLEAN NOT NULL DEFAULT false,
         policy_version INT NOT NULL DEFAULT 1 CHECK (policy_version >= 1),
         updated_by UUID,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
       )`,
      `INSERT INTO mu.agent_policy (id) VALUES (1) ON CONFLICT DO NOTHING`,
      // run 级冻结快照（新列——与 PR-A 的 review_run.policy_version TEXT(Leader 策略)语义分离）
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS agent_policy_version INT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS llm_mode TEXT`,
    ],
  },
  {
    // Wave 3.3：agent_attempt.provider 枚举 += agentteams（外部运行时执行器）。
    // 回滚：DELETE FROM mu.schema_migrations WHERE version=12;
    //       ALTER TABLE mu.agent_attempt DROP CONSTRAINT mu_agent_attempt_provider_check2;
    //       ALTER TABLE mu.agent_attempt ADD CONSTRAINT mu_agent_attempt_provider_check2
    //         CHECK (provider IN ('deterministic','llm','mock','fxv','deterministic_mock','openai_compatible'));
    version: 12,
    name: 'mu_agentteams_provider_value',
    sql: [
      `ALTER TABLE mu.agent_attempt DROP CONSTRAINT IF EXISTS mu_agent_attempt_provider_check2`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_agent_attempt_provider_check2') THEN
           ALTER TABLE mu.agent_attempt ADD CONSTRAINT mu_agent_attempt_provider_check2
             CHECK (provider IN ('deterministic','llm','mock','fxv','deterministic_mock','openai_compatible','agentteams'));
         END IF;
       END $$`,
    ],
  },
  {
    // Wave 3.7 数据源纯化：fxv.attempts 漂移调和。
    // 历史部署存在早期波次遗留的 fxv.attempts（attempt_id BIGSERIAL、无 state/state_detail
    // 或 state_detail 为 TEXT），而现役读取方（lib/fxv/metrics.mjs、lib/fxv/api.mjs）按
    // canonical 形状（state_detail JSONB、fxv.audit_events.meta JSONB）查询——TEXT 列上
    // `->>` 直接报 `operator does not exist: text ->> unknown`，CorePage /api/fxv/* 面板
    // 永久 BACKEND_ERROR（审计裁决 P1：正式导航页面）。
    // 策略：canonical DDL 的唯一权威=lib/fxv/store.mjs FXV_SCHEMA_SQL（fresh DB 由其创建，
    // 本迁移不在 fresh DB 抢先建主表避免 DDL 双源漂移）；本迁移只做「已存在漂移表」的
    // 幂等调和 + 补齐读路径依赖的 fxv.audit_events（缺失时）。
    // 兼容矩阵：fresh DB（无 fxv schema→全跳过）；v12 升级（漂移表→逐列调和）；空数据
    // （循环零行）；旧 text payload（非法 JSON 逐行容错→'{}'）；新 JSONB payload
    // （data_type 守卫直接跳过）；重复 migration（version 幂等 + 全语句可重放）；
    // 回滚重放：DELETE FROM mu.schema_migrations WHERE version=13; 后重放安全（全守卫）。
    version: 13,
    name: 'fxv_attempts_drift_reconcile',
    sql: [
      // 0) schema 存在性（fresh DB 无 fxv schema 时后续 ALTER 不炸；仅建空命名空间，
      //    不抢建任何表——canonical DDL 单一权威仍=fxv store）。
      `CREATE SCHEMA IF NOT EXISTS fxv`,
      // 1) attempt_id BIGSERIAL → TEXT（canonical 主键形状）；旧数字行转文本。
      `DO $$
       BEGIN
         IF EXISTS (SELECT 1 FROM information_schema.columns
           WHERE table_schema='fxv' AND table_name='attempts' AND column_name='attempt_id'
             AND data_type IN ('bigint','integer','smallint')) THEN
           ALTER TABLE fxv.attempts ALTER COLUMN attempt_id DROP DEFAULT;
           ALTER TABLE fxv.attempts ALTER COLUMN attempt_id TYPE text USING attempt_id::text;
         END IF;
       END $$`,
      // 2) state/state_detail 列补齐（旧表可能两者皆缺；fresh DB 无表→跳过）。
      `DO $$ BEGIN
         IF to_regclass('fxv.attempts') IS NOT NULL THEN
           ALTER TABLE fxv.attempts ADD COLUMN IF NOT EXISTS state text;
           ALTER TABLE fxv.attempts ADD COLUMN IF NOT EXISTS state_detail jsonb;
         END IF;
       END $$`,
      // 3) state_detail TEXT → JSONB：逐行容错转换（非法 JSON→'{}'），再改列型。
      `DO $$
       DECLARE r record; v jsonb;
       BEGIN
         IF EXISTS (SELECT 1 FROM information_schema.columns
           WHERE table_schema='fxv' AND table_name='attempts' AND column_name='state_detail'
             AND data_type='text') THEN
           FOR r IN SELECT attempt_id, state_detail AS sd FROM fxv.attempts LOOP
             BEGIN
               v := NULLIF(r.sd, '')::jsonb;
             EXCEPTION WHEN OTHERS THEN v := NULL;
             END;
             UPDATE fxv.attempts SET state_detail = coalesce(v::text, '{}'::text)
               WHERE attempt_id = r.attempt_id;
           END LOOP;
           ALTER TABLE fxv.attempts ALTER COLUMN state_detail TYPE jsonb USING
             coalesce(state_detail::jsonb, '{}'::jsonb);
           ALTER TABLE fxv.attempts ALTER COLUMN state_detail SET DEFAULT '{}'::jsonb;
         END IF;
       END $$`,
      // 4) payload TEXT → JSONB（同策略；早期漂移表的 payload 曾为 TEXT）。
      `DO $$
       DECLARE r record; v jsonb;
       BEGIN
         IF EXISTS (SELECT 1 FROM information_schema.columns
           WHERE table_schema='fxv' AND table_name='attempts' AND column_name='payload'
             AND data_type='text') THEN
           FOR r IN SELECT attempt_id, payload AS p FROM fxv.attempts LOOP
             BEGIN
               v := NULLIF(r.p, '')::jsonb;
             EXCEPTION WHEN OTHERS THEN v := NULL;
             END;
             UPDATE fxv.attempts SET payload = coalesce(v::text, '{}'::text)
               WHERE attempt_id = r.attempt_id;
           END LOOP;
           ALTER TABLE fxv.attempts ALTER COLUMN payload TYPE jsonb USING
             coalesce(payload::jsonb, '{}'::jsonb);
         END IF;
       END $$`,
      // 5) 读路径依赖的 fxv.audit_events（metrics 的 archive 域）缺失时补齐——
      //    仅当 fxv.attempts 存在（子系统在用）才创建，fresh 未启用子系统不抢建。
      `DO $$
       BEGIN
         IF to_regclass('fxv.attempts') IS NOT NULL THEN
           CREATE TABLE IF NOT EXISTS fxv.audit_events (
             seq        BIGSERIAL PRIMARY KEY,
             attempt_id TEXT NOT NULL,
             kind       TEXT NOT NULL,
             from_state TEXT,
             to_state   TEXT,
             actor      TEXT NOT NULL,
             reason     TEXT,
             meta       JSONB NOT NULL DEFAULT '{}'::jsonb,
             created_at TIMESTAMPTZ NOT NULL DEFAULT now()
           );
         END IF;
       END $$`,
    ],
  },
  {
    // Wave 3.8 队首阻塞修复（HOL）：mu.job 增 locked_at（领取时间戳）——
    // worker 崩溃/重启遗留的孤立 running job 可被 tick 定期回收回队（此前
    // 无 lease 记录，running 行永久滞留）。
    // 回滚：DELETE FROM mu.schema_migrations WHERE version=14;
    //       DROP INDEX IF EXISTS mu_job_running_locked_idx;
    //       ALTER TABLE mu.job DROP COLUMN IF EXISTS locked_at;
    version: 14,
    name: 'mu_job_locked_at',
    sql: [
      `ALTER TABLE mu.job ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ`,
      `CREATE INDEX IF NOT EXISTS mu_job_running_locked_idx ON mu.job (state, locked_at)`,
    ],
  },
  {
    // ADR-002 PR A：三档审查架构控制面（v15，additive/幂等/forward-only）。
    // 回滚：forward-only 设计——回滚=应用层 flag MU_REVIEW_ARCH=v1（表保留，v1 不读写新列）。
    // 物理回滚脚本在案但 Beta 不执行：
    //   DELETE FROM mu.schema_migrations WHERE version=15;
    //   DROP TABLE IF EXISTS mu.review_policy_revision, mu.provider_consent, mu.provider_registry, mu.review_policy;
    //   ALTER TABLE mu.review_run DROP COLUMN IF EXISTS architecture_version, provider_id, model_id, ... , policy_snapshot_digest;
    // 纪律：所有新表零凭据列（api_key/token/secret 禁入——服务校验+测试双保险）。
    version: 15,
    name: 'review_arch_v2_control_plane',
    sql: [
      `CREATE TABLE IF NOT EXISTS mu.review_policy (
         tenant_id             UUID PRIMARY KEY REFERENCES mu.tenant(tenant_id),
         review_mode           TEXT NOT NULL DEFAULT 'evidence_only'
           CHECK (review_mode IN ('evidence_only','external_api','local')),
         provider_id           TEXT,
         model_id              TEXT,
         provider_policy_status TEXT
           CHECK (provider_policy_status IS NULL OR provider_policy_status IN ('verified','custom_acknowledged','blocked')),
         code_egress_allowed   BOOLEAN NOT NULL DEFAULT false,
         consent_version      TEXT,
         context_budget       JSONB NOT NULL DEFAULT '{}'::jsonb,
         retention_ack        BOOLEAN NOT NULL DEFAULT false,
         enabled_at           TIMESTAMPTZ,
         enabled_by           UUID,
         policy_version       INT NOT NULL DEFAULT 1,
         created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
         CHECK (review_mode <> 'external_api' OR (provider_id IS NOT NULL AND model_id IS NOT NULL
           AND code_egress_allowed = true AND consent_version IS NOT NULL)),
         CHECK (review_mode <> 'evidence_only' OR code_egress_allowed = false)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.review_policy_revision (
         revision_id   BIGSERIAL PRIMARY KEY,
         tenant_id     UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         policy_version INT NOT NULL,
         review_mode   TEXT NOT NULL,
         provider_id   TEXT, model_id TEXT, provider_policy_status TEXT,
         code_egress_allowed BOOLEAN NOT NULL, consent_version TEXT,
         context_budget JSONB, retention_ack BOOLEAN,
         enabled_by    UUID, enabled_at TIMESTAMPTZ,
         recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
         record_kind   TEXT NOT NULL DEFAULT 'update'
           CHECK (record_kind IN ('initial','update','revoke','restore'))
       )`,
      `CREATE INDEX IF NOT EXISTS mu_review_policy_revision_tenant_idx
         ON mu.review_policy_revision (tenant_id, policy_version DESC)`,
      `CREATE TABLE IF NOT EXISTS mu.provider_registry (
         provider_id   TEXT PRIMARY KEY,
         display_name  TEXT NOT NULL,
         endpoint_origin TEXT NOT NULL,
         policy_status TEXT NOT NULL
           CHECK (policy_status IN ('verified','custom_acknowledged','blocked')),
         retention_summary TEXT NOT NULL DEFAULT 'unknown',
         training_summary  TEXT NOT NULL DEFAULT 'unknown',
         region_summary    TEXT NOT NULL DEFAULT 'unknown',
         policy_reference  TEXT,
         reviewed_at   TIMESTAMPTZ,
         reviewed_by   TEXT,
         state         TEXT NOT NULL DEFAULT 'restricted_experiment'
           CHECK (state IN ('verified_ok','custom_acknowledged','restricted_experiment','blocked','retired')),
         created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
         CHECK (NOT (policy_status = 'verified' AND state = 'restricted_experiment'))
       )`,
      `CREATE TABLE IF NOT EXISTS mu.provider_consent (
         consent_id   BIGSERIAL PRIMARY KEY,
         tenant_id    UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         provider_id  TEXT NOT NULL,
         consent_version TEXT NOT NULL,
         policy_version  INT NOT NULL,
         accepted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         accepted_by  UUID NOT NULL,
         revoked_at   TIMESTAMPTZ,
         revoked_by   UUID,
         acknowledgement_digest TEXT NOT NULL,
         code_egress_allowed  BOOLEAN NOT NULL DEFAULT true,
         UNIQUE (tenant_id, provider_id, consent_version)
       )`,
      `CREATE INDEX IF NOT EXISTS mu_provider_consent_active_idx
         ON mu.provider_consent (tenant_id, provider_id) WHERE revoked_at IS NULL`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS architecture_version TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS review_mode TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS review_scope TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS execution_mode TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS review_verdict TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS verification_verdict TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS tests_status TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS merge_eligibility TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS provider_id TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS model_id TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS provider_policy_status TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS code_egress INT NOT NULL DEFAULT 0`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS consent_version TEXT`,
      `ALTER TABLE mu.review_run ADD COLUMN IF NOT EXISTS policy_snapshot_digest TEXT`,
      `ALTER TABLE mu.agent_attempt DROP CONSTRAINT IF EXISTS mu_agent_attempt_provider_check2`,
      `ALTER TABLE mu.agent_attempt DROP CONSTRAINT IF EXISTS agent_attempt_provider_check`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_attempt_provider_check') THEN
           ALTER TABLE mu.agent_attempt ADD CONSTRAINT agent_attempt_provider_check
             CHECK (provider IN ('deterministic','llm','mock','fxv','deterministic_mock','openai_compatible','agentteams','external_api'));
         END IF;
       END $$`,
      `ALTER TABLE mu.agent_finding ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'precheck'
         CHECK (source IN ('precheck','reviewer'))`,
      `ALTER TABLE mu.agent_finding DROP CONSTRAINT IF EXISTS agent_finding_run_key`,
      `ALTER TABLE mu.agent_finding DROP CONSTRAINT IF EXISTS mu_agent_finding_unique`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_agent_finding_unique') THEN
           ALTER TABLE mu.agent_finding ADD CONSTRAINT mu_agent_finding_unique
             UNIQUE (run_id, rule_id, path, line_start, source);
         END IF;
       END $$`,
      `CREATE TABLE IF NOT EXISTS mu.code_egress_event (
         event_id      BIGSERIAL PRIMARY KEY,
         tenant_id     UUID NOT NULL,
         repo_id       UUID,
         run_id        UUID NOT NULL,
         attempt_id    UUID,
         provider_id   TEXT NOT NULL,
         model_id      TEXT,
         head_sha      TEXT NOT NULL,
         diff_digest   TEXT,
         input_digest  TEXT NOT NULL,
         files         TEXT[] NOT NULL DEFAULT '{}',
         bytes_sent    INT NOT NULL DEFAULT 0,
         tokens_sent   INT NOT NULL DEFAULT 0,
         redactions_applied INT NOT NULL DEFAULT 0,
         policy_version INT,
         consent_version TEXT,
         response_digest TEXT,
         timeout       BOOLEAN NOT NULL DEFAULT false,
         retry_count   INT NOT NULL DEFAULT 0,
         created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
       )`,
      `CREATE INDEX IF NOT EXISTS mu_code_egress_event_run_idx
         ON mu.code_egress_event (run_id, input_digest)`,
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_review_run_v2_mode_check') THEN
           ALTER TABLE mu.review_run ADD CONSTRAINT mu_review_run_v2_mode_check CHECK (
             (architecture_version IS NULL) OR (
               architecture_version = 'v2'
               AND review_mode IN ('evidence_only','external_api','local')
               AND review_scope IN ('evidence_only','full_code')
               AND execution_mode IN ('none','external_api','local')
               AND (review_verdict IS NULL OR review_verdict IN ('not_run','no_blocking_findings','changes_requested','inconclusive'))
               AND (verification_verdict IS NULL OR verification_verdict IN ('not_run','passed','failed','inconclusive'))
               AND (tests_status IS NULL OR tests_status IN ('not_run','passed','failed','unavailable'))
               AND (merge_eligibility IS NULL OR merge_eligibility IN ('unknown','eligible','ineligible'))
               AND (review_mode <> 'evidence_only' OR review_scope = 'evidence_only')
             )
           );
         END IF;
       END $$`,
      // PR D/E：fix_attempt 状态机扩展（STALE=新 head 旧 patch 过期；APPLIED=人工应用，
      // 本版本永不自动到达）+ verification_attempt 判定加 INCONCLUSIVE（模型不可用 fail-closed 落行可审计）。
      // 旧约束名来自 v1/v7 建表内联 CHECK（无显式名）——按定义 pattern 查找后替换。
      `DO $$ DECLARE c text; BEGIN
         SELECT conname INTO c FROM pg_constraint
          WHERE conrelid = 'mu.fix_attempt'::regclass AND contype = 'c'
            AND pg_get_constraintdef(oid) ILIKE '%PLANNED%';
         IF c IS NOT NULL THEN EXECUTE format('ALTER TABLE mu.fix_attempt DROP CONSTRAINT %I', c); END IF;
         ALTER TABLE mu.fix_attempt ADD CONSTRAINT mu_fix_attempt_status_check
           CHECK (status IN ('PLANNED','DRY_RUN','FAILED','SKIPPED','STALE','APPLIED'));
       END $$`,
      `DO $$ DECLARE c text; BEGIN
         SELECT conname INTO c FROM pg_constraint
          WHERE conrelid = 'mu.verification_attempt'::regclass AND contype = 'c'
            AND pg_get_constraintdef(oid) ILIKE '%PASS%';
         IF c IS NOT NULL THEN EXECUTE format('ALTER TABLE mu.verification_attempt DROP CONSTRAINT %I', c); END IF;
         ALTER TABLE mu.verification_attempt ADD CONSTRAINT mu_verification_attempt_verdict_check
           CHECK (verdict IN ('PASS','FAIL','BLOCKED','INCONCLUSIVE'));
       END $$`,
    ],
  },
  {
    // v16 高危修复审批门（fix/high-risk-fix-approval-gate）：P0/P1 修复前必须具名人工批准。
    //  * mu.fix_approval：逐 finding 审批票（服务端唯一创建方=Leader 路径；客户端不可提交
    //    tenant/severity/head_sha/finding_id 作可信来源——全部由服务端从 run/finding 解析）；
    //  * 活票唯一性：同一 finding 至多一张 PENDING/APPROVED/CONSUMED 票（重复 webhook 幂等；
    //    REJECTED 终局——同一 finding 不得反复要票绕门；EXPIRED/STALE 后可重新生成）；
    //  * review_run.status 扩 WAITING_FOR_HUMAN_APPROVAL（REVIEWED→WAITING→FIX_QUEUED/BLOCKED）。
    // 回滚：DELETE FROM mu.schema_migrations WHERE version=16;
    //       DROP TABLE IF EXISTS mu.fix_approval;
    //       （状态 CHECK 换回旧枚举须先确保无 WAITING 行——见迁移 SQL 注释）
    version: 16,
    name: 'mu_fix_approval_gate',
    sql: [
      `CREATE TABLE IF NOT EXISTS mu.fix_approval (
         approval_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id     UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         repo_id       UUID NOT NULL,
         pr_id         UUID NOT NULL,
         pr_number     TEXT NOT NULL,
         run_id        UUID NOT NULL,
         finding_id    UUID NOT NULL,
         severity      TEXT NOT NULL CHECK (severity IN ('P0','P1')),
         head_sha      TEXT NOT NULL,
         diff_digest   TEXT NOT NULL,
         requested_action TEXT NOT NULL DEFAULT 'fixer_dry_run'
           CHECK (requested_action IN ('fixer_dry_run')),
         requested_by  TEXT NOT NULL DEFAULT 'system:leader',
         created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
         expires_at    TIMESTAMPTZ NOT NULL,
         status        TEXT NOT NULL DEFAULT 'PENDING'
           CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED','STALE','CONSUMED')),
         decided_by    TEXT,
         decided_at    TIMESTAMPTZ,
         decision_reason TEXT,
         decision_digest TEXT,
         consumed_at   TIMESTAMPTZ,
         FOREIGN KEY (tenant_id, repo_id, pr_id)
           REFERENCES mu.pull_request (tenant_id, repo_id, pr_id)
       )`,
      // 活票唯一：同 finding 至多一张未落负态的票（PENDING/APPROVED/CONSUMED）
      `CREATE UNIQUE INDEX IF NOT EXISTS mu_fix_approval_live_uk
         ON mu.fix_approval (finding_id) WHERE status IN ('PENDING','APPROVED','CONSUMED')`,
      `CREATE INDEX IF NOT EXISTS mu_fix_approval_run_idx
         ON mu.fix_approval (run_id, status)`,
      `CREATE INDEX IF NOT EXISTS mu_fix_approval_pending_idx
         ON mu.fix_approval (tenant_id, status, expires_at)`,
      // review_run 状态 CHECK 换枚举（旧约束为 v1 建表内联无名 CHECK——按定义 pattern 查找替换）
      `DO $$ DECLARE c text; BEGIN
         SELECT conname INTO c FROM pg_constraint
          WHERE conrelid = 'mu.review_run'::regclass AND contype = 'c'
            AND pg_get_constraintdef(oid) ILIKE '%REVIEWED%FIX_QUEUED%';
         IF c IS NOT NULL THEN EXECUTE format('ALTER TABLE mu.review_run DROP CONSTRAINT %I', c); END IF;
         ALTER TABLE mu.review_run ADD CONSTRAINT mu_review_run_status_check
           CHECK (status IN
             ('RECEIVED','REVIEW_QUEUED','REVIEWING','REVIEWED','WAITING_FOR_HUMAN_APPROVAL',
              'FIX_QUEUED','FIXING','VERIFY_QUEUED','VERIFYING','VERIFIED',
              'REWORK_REQUIRED','BLOCKED','FAILED','COMPLETED'));
       END $$`,
    ],
  },
  {
    // ── v17 技能版本治理面（B 波；源自 mu-2b1 未提交设计 v7，按当前主线重设计）──
    // 回滚/前向兼容：
    //  * 纯 additive（两张新表+一个索引，零改既有表）——回滚 = DROP TABLE
    //    mu.skill_version, mu.skill; DELETE FROM mu.schema_migrations WHERE version=17;
    //    审批/review/run 数据完全不受影响；v16 代码见到 version 17 行会因
    //    schema_migrations 逐版本重放幂等（CREATE TABLE IF NOT EXISTS）而安全共存。
    //  * 前向：老镜像（≤v16）连到已升 v17 的库——initSchema 重放全量迁移全部
    //    IF NOT EXISTS 幂等，不报错不降级（MU_SCHEMA_LATEST 断言仅在更旧库触发）。
    // 版本不可变合同：
    //  * 版本行只在发布时写入，此后任何 API 不 UPDATE/DELETE mu.skill_version；
    //  * UNIQUE(skill_id, version) 钉死同租户同技能版本号唯一；
    //  * 「回滚」= 只切 mu.skill.current_version 指针，历史版本原样保留。
    version: 17,
    name: 'mu_skill_registry',
    sql: [
      // 技能注册表：治理面只决定「哪个版本生效」，不执行技能（执行在审查执行栈）。
      // manifest_sha256 = 技能工件完整性指纹（64 hex，发布时钉死防替换）；
      // artifact_ref = 工件引用（执行面消费；MU 面只做治理不执行）。
      `CREATE TABLE IF NOT EXISTS mu.skill (
         skill_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         skill_key TEXT NOT NULL,
         display_name TEXT NOT NULL,
         description TEXT NOT NULL DEFAULT '',
         current_version TEXT,
         state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','disabled')),
         created_by UUID,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (tenant_id, skill_key)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.skill_version (
         version_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         skill_id UUID NOT NULL REFERENCES mu.skill(skill_id) ON DELETE CASCADE,
         version TEXT NOT NULL,
         changelog TEXT NOT NULL DEFAULT '',
         manifest_sha256 TEXT NOT NULL,
         artifact_ref TEXT NOT NULL DEFAULT '',
         created_by UUID,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (skill_id, version)
       )`,
      `CREATE INDEX IF NOT EXISTS mu_skill_version_skill_idx
         ON mu.skill_version (skill_id, created_at DESC)`,
    ],
  },
  {
    // ── v18 绑定读写来源统一（审计 E-2；fix/binding-source-unify）──
    // 缺陷：写侧走 mu.repository_binding（安装回调/webhook/manual unbind），读侧走
    // mu.binding（listRepositories LEFT JOIN / getBindingForRepo repair 门 / revokeBinding）
    // ——经 GitHub App 绑定的仓库在仓库列表无绑定信息、repair 403 binding_required、
    // 两表数据漂移不可收敛。
    // 本迁移：把 mu.binding 现存绑定行【幂等回填】进 mu.repository_binding（列映射见下），
    // 之后读侧统一切到 repository_binding；mu.binding 表【保留不删】（老镜像 ≤v17 仍读它，
    // 向下兼容），新代码对其零写入（冻结为老镜像兼容只读域）。
    //
    // 列映射（mu.binding → mu.repository_binding）：
    //   tenant_id/repo_id/owner/name/default_branch/created_by/created_at/updated_at → 同名直拷；
    //   state + installation_state → binding_state（active+active→active；任一 revoked→revoked；
    //     任一 suspended→suspended；其他未知值→'error' + error_code='v18_unmapped_binding_state'
    //     ——mu.binding.state 无 CHECK 约束，fail-visible 不中断迁移）；
    //   revoked 语义行 → revoked_at=updated_at；
    //   granted_scopes → 无对应列，丢弃（授权快照无任何读方，MU_REPO_BOUND 审计已留 count）；
    //   kind → 无对应列。等价表达 = 「fixture 域」：合成 installation_id 落于
    //     MU_FIXTURE_INSTALLATION_BASE 保留区间且 github_app_installation.account_type='fixture'
    //     （真实 GitHub installation id 与 github repo id 远低于该区间；派生函数
    //     muFixtureInstallationIdOf/muFixtureRepoIdOf 与 store.mjs 运行时逐字同式）。
    //     不用 error_code 表达 kind（error_code 属失败语义域，ES/运维按其判异常）。
    // installation 解析：installation_id 为纯数字且 (tenant_id, installation_id) 已在
    //   github_app_installation 登记 → 沿用真实安装；否则建/用 fixture 域合成安装（幂等）。
    // github_repo_id 解析：真实安装行且 provider_repo_id 为纯数字且全局未被占用 → 沿用
    //   真实 id（保 webhook 按 github_repo_id 关联）；否则 fixture 保留区间合成（全局唯一，
    //   UNIQUE(github_repo_id) 极小概率撞车时 ON CONFLICT DO NOTHING 降级跳过，不中断）。
    // 幂等：NOT EXISTS (tenant_id, repo_id) 防重——已有新表行（权威 GHApp 绑定在位）的仓库
    //   跳过；重放（DELETE version=18 后 initSchema）零重复、已 revoke 的新表行不被复活。
    // 回滚（向下兼容，mu.binding 未写未删无需恢复）：
    //   DELETE FROM mu.schema_migrations WHERE version = 18;
    //   DELETE FROM mu.repository_binding rb USING mu.github_app_installation i
    //     WHERE rb.installation_id = i.installation_id AND i.account_type = 'fixture'
    //       AND rb.installation_id >= 8400000000000000;   -- 仅移除 fixture 域行
    //   DELETE FROM mu.github_app_installation WHERE account_type = 'fixture'
    //     AND installation_id >= 8400000000000000;        -- 合成安装随回滚清除
    version: 18,
    name: 'mu_binding_source_unify_backfill',
    sql: [
      `DO $mv18$
       DECLARE
         b record;
         v_inst bigint; v_gid bigint; v_state text;
         FIXTURE_INSTALLATION_BASE constant bigint := 8400000000000000;
         FIXTURE_REPO_BASE constant bigint := 8700000000000000;
       BEGIN
         FOR b IN
           SELECT bi.tenant_id, bi.repo_id, bi.kind, bi.installation_id, bi.installation_state,
                  bi.state, bi.created_by, bi.created_at, bi.updated_at,
                  r.provider_repo_id, r.owner, r.name, r.default_branch
             FROM mu.binding bi JOIN mu.repository r ON r.repo_id = bi.repo_id
             ORDER BY bi.created_at, bi.repo_id
         LOOP
           -- 幂等防重：同 (tenant, repo) 新表已有行（权威绑定在位）→ 跳过，绝不重复/复活
           CONTINUE WHEN EXISTS (SELECT 1 FROM mu.repository_binding x
                                  WHERE x.tenant_id = b.tenant_id AND x.repo_id = b.repo_id);
           -- installation 解析（数字且已登记 → 真实安装域；否则 fixture 合成安装域）
           IF b.installation_id ~ '^[0-9]{1,18}$'
              AND EXISTS (SELECT 1 FROM mu.github_app_installation i
                           WHERE i.tenant_id = b.tenant_id
                             AND i.installation_id = b.installation_id::bigint) THEN
             v_inst := b.installation_id::bigint;
             IF b.provider_repo_id ~ '^[0-9]{1,18}$'
                AND NOT EXISTS (SELECT 1 FROM mu.repository_binding x
                                 WHERE x.github_repo_id = b.provider_repo_id::bigint) THEN
               v_gid := b.provider_repo_id::bigint;      -- 真实 id（保 webhook 关联）
             ELSE
               v_gid := FIXTURE_REPO_BASE
                 + (('x'||substr(md5('fixture-repo:'||b.tenant_id::text||':'||b.repo_id::text),1,10))::bit(40)::bigint);
             END IF;
           ELSE
             v_inst := FIXTURE_INSTALLATION_BASE
               + (('x'||substr(md5('fixture-installation:'||b.tenant_id::text),1,10))::bit(40)::bigint);
             INSERT INTO mu.github_app_installation
                 (installation_id, tenant_id, account_id, account_login, account_type, app_id)
               VALUES (v_inst, b.tenant_id, 0, left(b.owner, 80), 'fixture', 0)
               ON CONFLICT (installation_id) DO NOTHING;
             v_gid := FIXTURE_REPO_BASE
               + (('x'||substr(md5('fixture-repo:'||b.tenant_id::text||':'||b.repo_id::text),1,10))::bit(40)::bigint);
           END IF;
           -- state 映射（未知值 fail-visible → error + error_code，绝不中断迁移）
           IF b.state = 'revoked' OR b.installation_state = 'revoked' THEN v_state := 'revoked';
           ELSIF b.state = 'suspended' OR b.installation_state = 'suspended' THEN v_state := 'suspended';
           ELSIF b.state = 'active' AND b.installation_state = 'active' THEN v_state := 'active';
           ELSE v_state := 'error';
           END IF;
           BEGIN
             INSERT INTO mu.repository_binding
                 (tenant_id, repo_id, github_repo_id, owner, name, installation_id,
                  default_branch, binding_state, error_code, revoked_at,
                  created_by, created_at, updated_at)
             VALUES (b.tenant_id, b.repo_id, v_gid, b.owner, b.name, v_inst,
                  b.default_branch, v_state,
                  CASE WHEN v_state = 'error' THEN 'v18_unmapped_binding_state' END,
                  CASE WHEN v_state = 'revoked' THEN b.updated_at END,
                  b.created_by, b.created_at, b.updated_at)
             ON CONFLICT (github_repo_id) DO NOTHING;
           EXCEPTION WHEN unique_violation THEN
             NULL; -- 理论不可达（前置 NOT EXISTS）；防御性跳过保迁移幂等
           END;
         END LOOP;
       END $mv18$`,
    ],
  },
  {
    // ── v19 Skill/RAG 调用留痕（C 波 C1；feat/c1-invocation-events）──
    // 目标：为 Agent/Skill/RAG 每一次真实执行建立服务端唯一留痕面（recorder 是唯一
    // 写入口，lib/multiuser/invocation-recorder.mjs），支撑"哪个技能/被哪个角色/在哪轮
    // run/以何版本/成功与否/耗时几何"的只读审计查询（API 见 api.mjs /runs/:id/*）。
    // 设计纪律（沿用 v16/v17/v18：版本化、幂等重放、零 destructive DDL、向下兼容说明）：
    //  * 两张新表纯 additive——不触碰 v18 及更早任何表/约束/数据（零数据丢失面）；
    //  * 复合 FK 锚定：(tenant,repo,pr,run)→mu.review_run、(attempt,run)→mu.agent_attempt、
    //    (tenant,repo)→mu.repository——DB 层直接拒绝跨租户/跨 run/attempt 组合
    //    （recorder 服务端另做前置校验，双层防御；rag 表 pr/run 可空→NULL 不触发 FK）；
    //  * UNIQUE (tenant_id, idempotency_key)：webhook/重试不重复写（重试=新事件新键）；
    //  * 已完成事件不可变：触发器封印——status 非 RUNNING 的行 UPDATE/DELETE 一律拒绝
    //    （仅允许 RUNNING→终态一次流转；重放安全：CREATE OR REPLACE TRIGGER 幂等）；
    //  * 敏感数据边界：无 prompt/response/query 原文/代码正文/secret 形状列——只存
    //    digest（hex CHECK）/计数/脱敏错误码（recorder 运行时守卫二次拦截）；
    //    idempotency_key 不进任何 API 读出白名单；
    //  * rag_retrieval_event 的 pr_id/run_id 可空：唯一真实执行入口
    //    GET /api/mu/repositories/:id/rag-search 是用户会话域（无 run 上下文）——
    //    按诚实原则不虚构 pr/run（有 run 的调用方必须传且过复合 FK）。
    // 幂等：CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE FUNCTION/TRIGGER /
    //   CREATE INDEX IF NOT EXISTS 全语句可重放；删版本行重放零重复；initSchema 双重跑安全。
    // 回滚（向下兼容；新表无既有读方，回滚零影响旧面）：
    //   DROP TABLE IF EXISTS mu.rag_retrieval_event;
    //   DROP TABLE IF EXISTS mu.skill_invocation_event;
    //   DROP FUNCTION IF EXISTS mu.mu_invocation_event_seal();
    //   DELETE FROM mu.schema_migrations WHERE version = 19;
    version: 19,
    name: 'mu_invocation_events',
    sql: [
      `CREATE TABLE IF NOT EXISTS mu.skill_invocation_event (
         event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id       UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         repo_id         UUID NOT NULL,
         pr_id           UUID NOT NULL,
         run_id          UUID NOT NULL,
         attempt_id      UUID,
         agent_role      TEXT NOT NULL CHECK (agent_role IN
                         ('leader','reviewer','fixer','verifier','system')),
         skill_key       TEXT NOT NULL CHECK (char_length(skill_key) BETWEEN 1 AND 64),
         skill_version   TEXT CHECK (skill_version IS NULL OR char_length(skill_version) <= 64),
         invocation_kind TEXT NOT NULL CHECK (invocation_kind IN
                         ('verifier_tool','agentteams_round','skill_mcp','rag_query','other')),
         status          TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN
                         ('RUNNING','SUCCEEDED','FAILED','TIMEOUT','CANCELLED','INTERRUPTED')),
         started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
         completed_at    TIMESTAMPTZ,
         latency_ms      INT,
         input_digest    TEXT CHECK (input_digest IS NULL OR input_digest ~ '^[0-9a-f]{8,64}$'),
         output_digest   TEXT CHECK (output_digest IS NULL OR output_digest ~ '^[0-9a-f]{8,64}$'),
         error_code      TEXT CHECK (error_code IS NULL OR char_length(error_code) <= 120),
         idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
         created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
         CONSTRAINT mu_skill_invocation_event_idem_uk UNIQUE (tenant_id, idempotency_key),
         FOREIGN KEY (tenant_id, repo_id) REFERENCES mu.repository (tenant_id, repo_id),
         FOREIGN KEY (tenant_id, repo_id, pr_id, run_id)
           REFERENCES mu.review_run (tenant_id, repo_id, pr_id, run_id),
         FOREIGN KEY (attempt_id, run_id)
           REFERENCES mu.agent_attempt (attempt_id, run_id)
       )`,
      `CREATE TABLE IF NOT EXISTS mu.rag_retrieval_event (
         event_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id         UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         repo_id           UUID NOT NULL,
         pr_id             UUID,
         run_id            UUID,
         attempt_id        UUID,
         agent_role        TEXT NOT NULL CHECK (agent_role IN
                           ('leader','reviewer','fixer','verifier','system')),
         skill_key         TEXT NOT NULL CHECK (char_length(skill_key) BETWEEN 1 AND 64),
         query_digest      TEXT NOT NULL CHECK (query_digest ~ '^[0-9a-f]{64}$'),
         result_count      INT NOT NULL DEFAULT 0 CHECK (result_count >= 0),
         source_digest_list JSONB NOT NULL DEFAULT '[]'::jsonb
                           CHECK (jsonb_typeof(source_digest_list) = 'array'
                                  AND jsonb_array_length(source_digest_list) <= 64),
         status            TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN
                           ('RUNNING','SUCCEEDED','FAILED','TIMEOUT','CANCELLED','INTERRUPTED')),
         started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
         completed_at      TIMESTAMPTZ,
         latency_ms        INT,
         error_code        TEXT CHECK (error_code IS NULL OR char_length(error_code) <= 120),
         idempotency_key   TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
         created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
         CONSTRAINT mu_rag_retrieval_event_idem_uk UNIQUE (tenant_id, idempotency_key),
         FOREIGN KEY (tenant_id, repo_id) REFERENCES mu.repository (tenant_id, repo_id),
         FOREIGN KEY (tenant_id, repo_id, pr_id, run_id)
           REFERENCES mu.review_run (tenant_id, repo_id, pr_id, run_id),
         FOREIGN KEY (attempt_id, run_id)
           REFERENCES mu.agent_attempt (attempt_id, run_id)
       )`,
      // 封印触发器：OLD.status 非 RUNNING 的行拒绝 UPDATE/DELETE（函数体无表名——两表共用）
      `CREATE OR REPLACE FUNCTION mu.mu_invocation_event_seal() RETURNS trigger LANGUAGE plpgsql AS $fn19$
       BEGIN
         RAISE EXCEPTION 'invocation_event_sealed: terminal events are immutable (retry with a new idempotency_key)';
       END
       $fn19$`,
      `DROP TRIGGER IF EXISTS mu_skill_invocation_event_seal ON mu.skill_invocation_event`,
      `CREATE TRIGGER mu_skill_invocation_event_seal
         BEFORE UPDATE OR DELETE ON mu.skill_invocation_event
         FOR EACH ROW WHEN (OLD.status <> 'RUNNING')
         EXECUTE FUNCTION mu.mu_invocation_event_seal()`,
      `DROP TRIGGER IF EXISTS mu_rag_retrieval_event_seal ON mu.rag_retrieval_event`,
      `CREATE TRIGGER mu_rag_retrieval_event_seal
         BEFORE UPDATE OR DELETE ON mu.rag_retrieval_event
         FOR EACH ROW WHEN (OLD.status <> 'RUNNING')
         EXECUTE FUNCTION mu.mu_invocation_event_seal()`,
      `CREATE INDEX IF NOT EXISTS mu_skill_invocation_tenant_run_idx
         ON mu.skill_invocation_event (tenant_id, run_id)`,
      `CREATE INDEX IF NOT EXISTS mu_skill_invocation_run_idx
         ON mu.skill_invocation_event (run_id)`,
      `CREATE INDEX IF NOT EXISTS mu_skill_invocation_skill_idx
         ON mu.skill_invocation_event (skill_key)`,
      `CREATE INDEX IF NOT EXISTS mu_skill_invocation_status_idx
         ON mu.skill_invocation_event (status)`,
      `CREATE INDEX IF NOT EXISTS mu_rag_retrieval_tenant_run_idx
         ON mu.rag_retrieval_event (tenant_id, run_id)`,
      `CREATE INDEX IF NOT EXISTS mu_rag_retrieval_run_idx
         ON mu.rag_retrieval_event (run_id)`,
      `CREATE INDEX IF NOT EXISTS mu_rag_retrieval_skill_idx
         ON mu.rag_retrieval_event (skill_key)`,
      `CREATE INDEX IF NOT EXISTS mu_rag_retrieval_status_idx
         ON mu.rag_retrieval_event (status)`,
    ],
  },
  {
    // ── v20 RAG 模型安装控制面（v19 已被 C 波 mu_invocation_events 占用，本迁移顺延，DDL 零改动）（RAG-model-install 波；自托管 ModelScope 官方 bge-m3）──
    // 回滚/前向兼容：纯 additive（一张新表+一个索引）——回滚 = DROP TABLE
    // mu.rag_model_install; DELETE FROM mu.schema_migrations WHERE version=20;
    // 审批/Skill v17/review/run 状态机零触碰；老镜像（≤v19）重放迁移幂等安全。
    // 状态机（服务端强制，CHECK 为最后防线）：UNINSTALLED→DOWNLOADING→VERIFYING→
    // READY→ACTIVE；失败态 DOWNLOAD_FAILED/HASH_MISMATCH/INSUFFICIENT_DISK/
    // SIDECAR_START_FAILED/ACTIVATION_FAILED；回退=active_provider 切回 local-hash-v1。
    version: 20,
    name: 'mu_rag_model_install',
    sql: [
      `CREATE TABLE IF NOT EXISTS mu.rag_model_install (
         install_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         tenant_id UUID NOT NULL REFERENCES mu.tenant(tenant_id),
         model_key TEXT NOT NULL,
         manifest_version TEXT NOT NULL,
         source_url TEXT NOT NULL,
         revision TEXT NOT NULL,
         license TEXT NOT NULL,
         expected_files JSONB NOT NULL,
         total_bytes BIGINT NOT NULL,
         downloaded_bytes BIGINT NOT NULL DEFAULT 0,
         state TEXT NOT NULL DEFAULT 'UNINSTALLED'
           CHECK (state IN ('UNINSTALLED','DOWNLOADING','VERIFYING','READY','ACTIVE',
                            'DOWNLOAD_FAILED','HASH_MISMATCH','INSUFFICIENT_DISK',
                            'SIDECAR_START_FAILED','ACTIVATION_FAILED')),
         active_provider TEXT NOT NULL DEFAULT 'local-hash-v1',
         activated_at TIMESTAMPTZ,
         last_error_code TEXT,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (tenant_id, model_key)
       )`,
      `CREATE INDEX IF NOT EXISTS mu_rag_model_install_tenant_idx
         ON mu.rag_model_install (tenant_id, state)`,
    ],
  },
  {
    // ── v21 审计事件不可变封印（rc.10 安全收敛 SEC-7；PR-B）──
    // mu.audit_event 是 append-only 审计面（store.audit 只 INSERT）——升级为 DB 级
    // 硬封印：UPDATE/DELETE 一律 RAISE EXCEPTION（对齐 v19 留痕表封印纪律；v19 允许
    // RUNNING 在途更新，audit_event 无状态机故全封）。纯 additive：回滚 =
    // DROP TRIGGER mu_audit_event_no_update; DROP FUNCTION mu.mu_audit_event_seal();
    // DELETE FROM mu.schema_migrations WHERE version=21; 既有数据零触碰。
    version: 21,
    name: 'mu_audit_event_seal',
    sql: [
      `CREATE OR REPLACE FUNCTION mu.mu_audit_event_seal() RETURNS trigger LANGUAGE plpgsql AS $fn21$
       BEGIN
         RAISE EXCEPTION 'audit_event_sealed: mu.audit_event is append-only (op % not permitted)', TG_OP;
       END
       $fn21$`,
      `DROP TRIGGER IF EXISTS mu_audit_event_no_update ON mu.audit_event`,
      `CREATE TRIGGER mu_audit_event_no_update
         BEFORE UPDATE OR DELETE ON mu.audit_event
         FOR EACH ROW EXECUTE FUNCTION mu.mu_audit_event_seal()`,
    ],
  },
  {
    // ── v22 rc.10 PR-E 租户边界（外部租户准入前收窄；additive/幂等/预检 fail-visible）──
    // 覆盖四块（v21 已由 PR-B mu_audit_event_seal 占用，本迁移顺延至 v22；
    // 与 v21 相互独立、先合后合均可独立工作）：
    //  ① ISO-2/SEC-4 ragtrial.query_log 收窄：新列 query_digest（64-hex sha256，CHECK
    //     定形）+ tenant_id（UUID，可空）+ 租户索引。写入侧已改为"原文停写、只写
    //     digest、MU 桥接会话带租户"（ragtrial/schema.mjs 有同款幂等语句——fresh DB
    //     上 ragtrial 表在 mu v22 之后才建，两个入口任一先到都收敛；query_text 历史
    //     行只读保留，不删列不迁移，语义变更在此登记）。
    //  ② ISO-6 mu.skill_version 单列 FK 升级复合 (tenant_id, skill_id)：预检失配行
    //     （tenant 与 skill 不同租户）>0 → RAISE 清晰错误（fail-visible，数据修复后
    //     重放）；复合 FK 需被引用列 UNIQUE 索引（skill_id 是 PK，仍需显式
    //     UNIQUE(tenant_id, skill_id)）。旧单列 FK 按 conkey 动态定位后 DROP（v17
    //     内联无名约束）；ON DELETE CASCADE 语义保留。
    //  ③ ISO-6 mu.code_egress_event.tenant_id 补归属约束：预检全部命中 mu.tenant →
    //     加 FK；存在失配行 → 只加索引不加强约束（RAISE NOTICE 登记，数据修复后
    //     重放可补 FK）——选与数据现状安全的方案。
    //  ④ 幂等：全部语句可重放（to_regclass/pg_constraint/IF NOT EXISTS 守卫；fresh
    //     DB 无 ragtrial schema 时该块整体跳过）。
    // 回滚（向下兼容；forward-only 设计，物理回滚脚本在案但 Beta 不执行）：
    //   DROP INDEX IF EXISTS ragtrial_query_log_tenant_idx;
    //   ALTER TABLE ragtrial.query_log DROP CONSTRAINT IF EXISTS ragtrial_query_log_digest_shape;
    //   ALTER TABLE ragtrial.query_log DROP COLUMN IF EXISTS tenant_id, DROP COLUMN IF EXISTS query_digest;
    //   ALTER TABLE mu.skill_version DROP CONSTRAINT IF EXISTS mu_skill_version_tenant_skill_fk;
    //   ALTER TABLE mu.skill_version ADD CONSTRAINT <旧名> FOREIGN KEY (skill_id)
    //     REFERENCES mu.skill(skill_id) ON DELETE CASCADE;   -- 旧名为 v17 自动命名，需按目录回填
    //   DROP INDEX IF EXISTS mu_skill_tenant_skill_uk;
    //   ALTER TABLE mu.code_egress_event DROP CONSTRAINT IF EXISTS mu_code_egress_event_tenant_fk;
    //   DROP INDEX IF EXISTS mu_code_egress_event_tenant_idx;
    //   DELETE FROM mu.schema_migrations WHERE version = 22;
    version: 22,
    name: 'mu_tenant_boundary_pr_e',
    sql: [
      // ① ragtrial.query_log：digest-only + 租户归属（表未建则跳过——ragtrial init 补齐）
      `DO $mv22ql$ BEGIN
         IF to_regclass('ragtrial.query_log') IS NOT NULL THEN
           ALTER TABLE ragtrial.query_log ADD COLUMN IF NOT EXISTS query_digest TEXT;
           ALTER TABLE ragtrial.query_log ADD COLUMN IF NOT EXISTS tenant_id UUID;
           IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ragtrial_query_log_digest_shape') THEN
             ALTER TABLE ragtrial.query_log ADD CONSTRAINT ragtrial_query_log_digest_shape
               CHECK (query_digest IS NULL OR query_digest ~ '^[0-9a-f]{64}$');
           END IF;
           CREATE INDEX IF NOT EXISTS ragtrial_query_log_tenant_idx
             ON ragtrial.query_log (tenant_id, created_at DESC);
         END IF;
       END $mv22ql$`,
      // ② skill_version 复合 FK（预检 fail-visible）
      `DO $mv22sv$
       DECLARE v_mismatch int; v_old_fk text;
       BEGIN
         SELECT count(*)::int INTO v_mismatch FROM mu.skill_version sv
          WHERE NOT EXISTS (SELECT 1 FROM mu.skill s
                             WHERE s.tenant_id = sv.tenant_id AND s.skill_id = sv.skill_id);
         IF v_mismatch > 0 THEN
           RAISE EXCEPTION 'v22_precheck_failed: mu.skill_version 有 % 行的 tenant_id 与所引 skill 不同租户（复合 FK 前置预检失败）——先修正 tenant_id 或清理失配行后重放迁移', v_mismatch;
         END IF;
         CREATE UNIQUE INDEX IF NOT EXISTS mu_skill_tenant_skill_uk ON mu.skill (tenant_id, skill_id);
         -- 旧单列 FK（v17 内联无名）：按「引用 mu.skill 且 conkey 仅含 skill_id」动态定位
         SELECT conname INTO v_old_fk FROM pg_constraint c
          WHERE c.conrelid = 'mu.skill_version'::regclass AND c.contype = 'f'
            AND c.confrelid = 'mu.skill'::regclass
            AND c.conkey = ARRAY[(SELECT a.attnum::smallint FROM pg_attribute a
              WHERE a.attrelid = 'mu.skill_version'::regclass AND a.attname = 'skill_id')]::smallint[];
         IF v_old_fk IS NOT NULL THEN
           EXECUTE format('ALTER TABLE mu.skill_version DROP CONSTRAINT %I', v_old_fk);
         END IF;
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_skill_version_tenant_skill_fk') THEN
           ALTER TABLE mu.skill_version ADD CONSTRAINT mu_skill_version_tenant_skill_fk
             FOREIGN KEY (tenant_id, skill_id) REFERENCES mu.skill (tenant_id, skill_id) ON DELETE CASCADE;
         END IF;
       END $mv22sv$`,
      // ③ code_egress_event 租户归属：零失配才加强约束（否则索引 + NOTICE 登记）
      `DO $mv22eg$
       DECLARE v_mismatch int;
       BEGIN
         SELECT count(*)::int INTO v_mismatch FROM mu.code_egress_event e
          WHERE NOT EXISTS (SELECT 1 FROM mu.tenant t WHERE t.tenant_id = e.tenant_id);
         IF v_mismatch = 0 THEN
           IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mu_code_egress_event_tenant_fk') THEN
             ALTER TABLE mu.code_egress_event ADD CONSTRAINT mu_code_egress_event_tenant_fk
               FOREIGN KEY (tenant_id) REFERENCES mu.tenant (tenant_id);
           END IF;
         ELSE
           RAISE NOTICE 'v22: mu.code_egress_event 有 % 行 tenant_id 不在 mu.tenant——仅加索引不加强制 FK（登记：数据修复后删版本行重放可补约束）', v_mismatch;
         END IF;
       END $mv22eg$`,
      `CREATE INDEX IF NOT EXISTS mu_code_egress_event_tenant_idx
         ON mu.code_egress_event (tenant_id)`,
    ],
  },
  {
    // ── v23 invitation_role_check 收紧（D-3：排除 platform_admin）──
    // 纯 additive：DROP 旧 CHECK + ADD 新 CHECK（枚举排除 platform_admin）。
    // 理由：invitation 是外部成员唯一准入路径——platform_admin 仅限内部
    // （DBA SQL 直接授权），invitation 一律不授予（对齐 API 层 D-3 403 排除）。
    // migration 前预检：若存量 role=platform_admin invitation 存在则 fail-closed
    // 并 RAISE EXCEPTION（事务回滚，不删除、不修改数据）。
    // 回滚：DROP 旧 CHECK + ADD 回全枚举 CHECK。
    version: 23,
    name: 'mu_invitation_role_tighten',
    sql: [
      // 预检：存量 platform_admin invitation 存在则 fail-closed
      `DO $mv23chk$ BEGIN
         IF EXISTS (SELECT 1 FROM mu.invitation WHERE role = 'platform_admin') THEN
           RAISE EXCEPTION 'v23_precheck_failed: 存量 role=platform_admin invitation 存在——先清理后重放迁移';
         END IF;
       END $mv23chk$`,
      `ALTER TABLE mu.invitation DROP CONSTRAINT IF EXISTS invitation_role_check`,
      `ALTER TABLE mu.invitation ADD CONSTRAINT invitation_role_check
         CHECK (role IN ('contributor','reviewer','maintainer','auditor'))`,
    ],
  },
];

export const MU_SCHEMA_LATEST = MU_MIGRATIONS[MU_MIGRATIONS.length - 1].version;

// ── v18 fixture 绑定域（kind 语义的等价表达）──
// mu.repository_binding 无 kind 列（且 installation_id BIGINT NOT NULL 复合 FK 到
// github_app_installation、github_repo_id 全局 UNIQUE）——fixture/演示绑定以「保留 id 区间」
// 表达：installation_id = BASE + md5 前 10 hex（bit40，0..1099511627775），
// account_type='fixture' 为人读标记。真实 GitHub installation/repo id（当前 <1e13 量级）
// 与保留区间 [8.4e15, 8.7011e15] 隔离数十个数量级；上限 8701099511627775 <
// Number.MAX_SAFE_INTEGER(9007199254740991)，JS/PG 数值安全。
// 派生表达式必须与 v18 迁移 SQL 逐字一致（'fixture-installation:'/'fixture-repo:' 键前缀、
// md5 取前 10 hex）——两处任一改动须同步。
export const MU_FIXTURE_INSTALLATION_BASE = 8400000000000000;
export const MU_FIXTURE_REPO_BASE = 8700000000000000;

const md5Hex40 = (s) => {
  const h = crypto.createHash('md5').update(s).digest('hex');
  return BigInt('0x' + h.slice(0, 10));
};
export function muFixtureInstallationIdOf(tenantId) {
  return MU_FIXTURE_INSTALLATION_BASE + Number(md5Hex40(`fixture-installation:${tenantId}`));
}
export function muFixtureRepoIdOf(tenantId, repoId) {
  return MU_FIXTURE_REPO_BASE + Number(md5Hex40(`fixture-repo:${tenantId}:${repoId}`));
}
