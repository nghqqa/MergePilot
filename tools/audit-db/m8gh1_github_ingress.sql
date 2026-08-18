-- ═══ M8-GH-1: GitHub App PR 入口 —— 交付队列 + Checks outbox + 最小权限角色 ═══
--
-- 设计冻结(2026-08-18 GitHub App PR 入口设计收口):
--   * github_deliveries: 入口交付队列状态机
--     PENDING → RUNNING → PROCESSED | ERROR;IGNORED 由 receiver 直接落。
--     claim/确认全部以 claim_id CAS;lease 过期可回收;attempt 达上限终局 ERROR。
--   * github_check_outbox: Checks 发布状态机
--     PENDING → LEASED → PUBLISHED | TERMINAL;desired_version 只随
--     (desired_status, desired_conclusion, observed_head_sha) 实际变化 +1;
--     published_version 单调递增;SHA 变更清空旧 check_run_id。
--   * 角色: NOLOGIN capability 角色持表权限;LOGIN runtime 角色仅作成员。
--     密码一律运行时生成注入(ALTER ROLE),迁移中零密码/零 token/零私钥。
--   * receiver(github_event_ingress)仅 INSERT ON github_deliveries;
--     reporter(github_check_publisher)仅 SELECT/UPDATE ON github_check_outbox;
--     治理表(task_runs/stage_runs/dispatch_outbox/stage_events/…)不授予任何
--     上述角色 —— deny-by-not-granted。
--
-- 非破坏性: 仅 CREATE IF NOT EXISTS / DO 幂等块,与 m3c_state.sql 惯例一致。

-- ═══ 1. github_deliveries(入口交付队列) ═══

CREATE TABLE IF NOT EXISTS public.github_deliveries (
  delivery_id       TEXT PRIMARY KEY
                    CHECK (delivery_id ~ '^[A-Za-z0-9][A-Za-z0-9-]{7,63}$'),
  event_name        TEXT NOT NULL CHECK (event_name IN ('ping','pull_request','other')),
  action            TEXT NOT NULL CHECK (action ~ '^[a-z_]{1,64}$'),
  installation_id   BIGINT CHECK (installation_id IS NULL OR installation_id > 0),
  repo              TEXT CHECK (repo IS NULL OR repo ~ '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$'),
  pr_number         INTEGER CHECK (pr_number IS NULL OR pr_number >= 1),
  observed_head_sha TEXT CHECK (observed_head_sha IS NULL
                                 OR observed_head_sha ~ '^[0-9a-f]{40}$'),
  observed_base_sha TEXT CHECK (observed_base_sha IS NULL
                                 OR observed_base_sha ~ '^[0-9a-f]{40}$'),
  body_sha256       TEXT NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
  canonical_payload JSONB NOT NULL,
  status            TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN
                      ('PENDING','RUNNING','PROCESSED','IGNORED','ERROR')),
  claim_id          TEXT,
  claimed_at        TIMESTAMPTZ,
  lease_expires_at  TIMESTAMPTZ,
  attempt_count     INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_retry_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  error             TEXT,
  derived_run_id    TEXT,
  received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at      TIMESTAMPTZ,
  -- 映射事件必须携带完整最小 envelope(ping/other 允许缺省):
  CONSTRAINT gh_deliveries_pull_request_envelope CHECK (
    event_name <> 'pull_request' OR (
      installation_id IS NOT NULL AND repo IS NOT NULL
      AND pr_number IS NOT NULL AND observed_head_sha IS NOT NULL
      AND observed_base_sha IS NOT NULL AND action IN
        ('opened','synchronize','reopened'))
  )
);

CREATE INDEX IF NOT EXISTS idx_gh_deliveries_claim
  ON public.github_deliveries (status, next_retry_at, received_at);

-- ═══ 2. github_check_outbox(Checks 发布 outbox) ═══

CREATE TABLE IF NOT EXISTS public.github_check_outbox (
  outbox_id          TEXT PRIMARY KEY,
  run_id             TEXT NOT NULL REFERENCES public.task_runs(run_id),
  repo               TEXT NOT NULL
                     CHECK (repo ~ '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$'),
  pr_number          INTEGER NOT NULL CHECK (pr_number >= 1),
  observed_head_sha  TEXT NOT NULL CHECK (observed_head_sha ~ '^[0-9a-f]{40}$'),
  external_id        TEXT NOT NULL,
  check_run_id       BIGINT,
  desired_status     TEXT NOT NULL CHECK (desired_status IN
                       ('queued','in_progress','completed')),
  desired_conclusion TEXT CHECK (desired_conclusion IS NULL OR desired_conclusion IN
                       ('success','failure','neutral','action_required')),
  published_status   TEXT CHECK (published_status IS NULL OR published_status IN
                       ('queued','in_progress','completed')),
  published_conclusion TEXT CHECK (published_conclusion IS NULL
                         OR published_conclusion IN
                         ('success','failure','neutral','action_required')),
  publish_state      TEXT NOT NULL DEFAULT 'PENDING' CHECK (publish_state IN
                       ('PENDING','LEASED','PUBLISHED','TERMINAL')),
  claim_id           TEXT,
  claimed_at         TIMESTAMPTZ,
  lease_expires_at   TIMESTAMPTZ,
  desired_version    INTEGER NOT NULL DEFAULT 1 CHECK (desired_version >= 1),
  published_version  INTEGER NOT NULL DEFAULT 0 CHECK (published_version >= 0),
  attempt_count      INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_retry_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error         TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at       TIMESTAMPTZ,
  CONSTRAINT gh_check_version_order CHECK (published_version <= desired_version)
);

-- 一个 run(⇒一个 observed SHA)恰一行;external_id 派生自 run_id。
CREATE UNIQUE INDEX IF NOT EXISTS uq_gh_check_external
  ON public.github_check_outbox (external_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_gh_check_run
  ON public.github_check_outbox (run_id);

CREATE INDEX IF NOT EXISTS idx_gh_check_claim
  ON public.github_check_outbox (publish_state, next_retry_at);

-- ═══ 3. 角色: NOLOGIN capability + LOGIN runtime(密码运行时注入,迁移零秘密) ═══

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'github_ingress_writer') THEN
    CREATE ROLE github_ingress_writer NOLOGIN;      -- capability: 仅 INSERT 交付队列
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'github_event_ingress') THEN
    CREATE ROLE github_event_ingress LOGIN;          -- runtime(密码由部署方运行时 ALTER ROLE 设置)
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'github_checks_publisher') THEN
    CREATE ROLE github_checks_publisher NOLOGIN;    -- capability: 仅读写 Checks outbox
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'github_check_publisher') THEN
    CREATE ROLE github_check_publisher LOGIN;        -- runtime(密码同上)
  END IF;
END $$;

-- 成员关系(runtime ∈ capability);无密码、无秘密值出现在本迁移。
GRANT github_ingress_writer TO github_event_ingress;
GRANT github_checks_publisher TO github_check_publisher;

-- receiver: 仅 INSERT(ON CONFLICT rowcount 方案不需要 SELECT;
-- healthz 仅 SELECT 1,不涉表权限)。显式不授予任何治理表权限。
GRANT INSERT ON public.github_deliveries TO github_ingress_writer;

-- reporter: 仅 SELECT/UPDATE Checks outbox。
GRANT SELECT, UPDATE ON public.github_check_outbox TO github_checks_publisher;

-- 用法与 USAGE 模式(schema 已存在,幂等补授)。
GRANT USAGE ON SCHEMA public TO github_ingress_writer, github_checks_publisher;
