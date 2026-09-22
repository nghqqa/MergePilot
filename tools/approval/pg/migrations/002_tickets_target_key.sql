-- 002_tickets_target_key.sql — 审批票据唯一性对齐设计 v2(caf6909 §5.4)
-- 背景:001 应用于隔离库时的 (run_id, action, finding_id) 部分唯一索引存在
--       finding_id=NULL 漏洞(PG NULLS DISTINCT ⇒ run 级审批可插多张活动票)。
--       设计 v2 以非空 target_key 修复;本迁移为显式后续,不回改 001 历史。
-- 目标状态与 DATA-ARCHITECTURE-PG.md@caf6909 §5.4 一致。

ALTER TABLE approval.tickets
  ADD COLUMN IF NOT EXISTS target_key TEXT;

UPDATE approval.tickets
  SET target_key = COALESCE(finding_id, '_run_')
  WHERE target_key IS NULL;

ALTER TABLE approval.tickets
  ALTER COLUMN target_key SET NOT NULL;

DROP INDEX IF EXISTS approval.uq_active_ticket;

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_ticket
  ON approval.tickets (run_id, action, target_key)
  WHERE status IN ('PENDING','APPROVED','EXECUTING');
