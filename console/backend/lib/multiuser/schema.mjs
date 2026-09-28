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
];

export const MU_SCHEMA_LATEST = MU_MIGRATIONS[MU_MIGRATIONS.length - 1].version;
