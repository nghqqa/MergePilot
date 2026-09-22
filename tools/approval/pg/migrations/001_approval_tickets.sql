-- 001_approval_tickets.sql — 审批票据迁移(PostgreSQL)
-- 实现基线: docs/productization/DATA-ARCHITECTURE-PG.md @ 设计分支 e247b80 §5.4
--           (列形状=tools/approval/store_sqlite.py tickets;状态机=M2-APPROVAL-SPEC 不变)
-- 执行环境: 隔离测试实例(mp-pg-contract-test, PG16, 127.0.0.1:55432);共享库禁用。
--
-- 历史:本文件首版曾含 NULLS NOT DISTINCT 偏离与夹具父表;
-- 设计 v2(caf6909)改用非空 target_key,由 002 迁移对齐。001 现为纯 v1 形状。

-- 形式:无 IF NOT EXISTS——执行器(approval/pg/apply_migrations.py)按
-- schema_migrations 跟踪幂等;未登记的既有同名表 = 明确失败,不被静默掩盖。
CREATE SCHEMA IF NOT EXISTS approval;

CREATE TABLE approval.tickets (
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

CREATE UNIQUE INDEX uq_active_ticket
  ON approval.tickets (run_id, action, finding_id)
  NULLS NOT DISTINCT                        -- 语法位置:列清单后、WHERE 前(PG15+)
  WHERE status IN ('PENDING','APPROVED','EXECUTING');

CREATE TABLE IF NOT EXISTS approval.ticket_audit (   -- append-only,不 UPDATE/DELETE
  id          BIGSERIAL PRIMARY KEY,
  ticket_id   TEXT NOT NULL,
  from_status TEXT,
  to_status   TEXT,
  actor       TEXT,
  request_hash TEXT,
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
