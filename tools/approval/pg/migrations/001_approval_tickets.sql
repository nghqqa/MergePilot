-- 001_approval_tickets.sql — 审批票据迁移(PostgreSQL)
-- 实现基线: docs/productization/DATA-ARCHITECTURE-PG.md @ 设计分支 e247b80 §5.4
--           (列形状=tools/approval/store_sqlite.py tickets;状态机=M2-APPROVAL-SPEC 不变)
-- 执行环境: 隔离测试实例(mp-pg-contract-test, PG16, 127.0.0.1:55432);共享库禁用。
--
-- 对设计稿的一处偏离(待设计窗口确认,已登记 backend/PROGRESS.md 问题#1):
--   uq_active_ticket 使用 NULLS NOT DISTINCT(PG15+)——否则 finding_id 为 NULL 的
--   活动票在 PG 默认 NULL-distinct 语义下不受唯一约束(设计稿未处理该点)。
--   若设计窗口否决,替代方案=两个部分唯一索引(分别覆盖 NULL/非 NULL)。

CREATE SCHEMA IF NOT EXISTS approval;

-- 父表(夹具级,仅满足 tickets 外键;正式定义见设计文档 §5.2/5.3,由统一迁移建)
CREATE SCHEMA IF NOT EXISTS run;
CREATE TABLE IF NOT EXISTS run.repos (
  repo_id  TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS run.runs (
  run_id   TEXT PRIMARY KEY,
  repo_id  TEXT
);

CREATE TABLE IF NOT EXISTS approval.tickets (
  ticket_id         TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL,
  repo_id           TEXT NOT NULL,
  head_sha          TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
  action            TEXT NOT NULL CHECK (action IN ('generate_patch','run_poc','publish_result')),
  params_hash       TEXT NOT NULL CHECK (params_hash ~ '^[0-9a-f]{64}$'),
  patch_fingerprint TEXT CHECK (patch_fingerprint IS NULL OR patch_fingerprint ~ '^[0-9a-f]{64}$'),
  finding_fingerprint TEXT,
  finding_id        TEXT,
  attempt_no        INTEGER NOT NULL,
  status            TEXT NOT NULL CHECK (status IN
                    ('PENDING','APPROVED','REJECTED','EXECUTING','USED','FAILED',
                     'EXPIRED','INVALIDATED')),
  created_at        TIMESTAMPTZ NOT NULL,
  created_by_run    TEXT,
  approval_expires_at TIMESTAMPTZ,
  approved_by       TEXT,
  approved_at       TIMESTAMPTZ,
  result_fingerprint TEXT,
  error             TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_ticket
  ON approval.tickets (run_id, action, finding_id)
  NULLS NOT DISTINCT                        -- 语法位置:列清单后、WHERE 前(PG15+)
  WHERE status IN ('PENDING','APPROVED','EXECUTING');
  -- 偏离点见文件头(待设计确认)

CREATE TABLE IF NOT EXISTS approval.ticket_audit (   -- append-only,不 UPDATE/DELETE
  id          BIGSERIAL PRIMARY KEY,
  ticket_id   TEXT NOT NULL,
  from_status TEXT,
  to_status   TEXT,
  actor       TEXT,
  request_hash TEXT,
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
