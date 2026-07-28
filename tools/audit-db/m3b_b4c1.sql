-- m3b_b4c1.sql — B4c.1 收敛与调度加固(独立 migration;不改冻结的 m3b_b4c.sql)。
--
-- 目标:确定性拒绝(claim 前)+ 队列公平性 + 单循环工作预算 + Gateway 降级运行的 DB 侧基础。
-- 全部幂等。依赖 m3b_b4.sql(task_runs/approvals/policy_action_outbox + mergepilot_l2_owner)
--   与 m3b_b4c.sql(task_runs.approval_required/l2_discovery_attempts)。
--
-- 内容:
--   1. task_runs 调度字段(l2_next_attempt_at/l2_retry_count/l2_retry_reason/l2_discovery_deadline_at)
--      + 非负 CHECK + ready 部分索引。
--   2. policy_action_outbox.last_error_code(复用现有 next_retry_at,不造重复字段)。
--   3. l2_reject_approved(ticket, reason_code):claim 前确定性拒绝(allowlist reason)。

-- ════════════ 1. task_runs 调度字段 + CHECK + ready 索引 ════════════
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS l2_next_attempt_at       TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS l2_retry_count           INTEGER     NOT NULL DEFAULT 0;
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS l2_retry_reason          TEXT;
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS l2_discovery_deadline_at TIMESTAMPTZ;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_l2_retry_count_nonneg') THEN
    ALTER TABLE task_runs ADD CONSTRAINT chk_l2_retry_count_nonneg CHECK (l2_retry_count >= 0);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_l2_discovery_attempts_nonneg') THEN
    ALTER TABLE task_runs ADD CONSTRAINT chk_l2_discovery_attempts_nonneg CHECK (l2_discovery_attempts >= 0);
  END IF;
END $$;

-- ready 部分索引:仅 APPROVAL_PENDING 且在 binding/ticket 阶段且到期的 run(公平调度的候选集)
CREATE INDEX IF NOT EXISTS idx_task_runs_l2_ready
  ON task_runs (l2_next_attempt_at, updated_at, run_id)
  WHERE approval_required
    AND status = 'APPROVAL_PENDING'
    AND current_stage IN ('l2_binding', 'l2_awaiting_ticket');

-- ════════════ 2. outbox 结构化错误码(复用 next_retry_at)════════════
ALTER TABLE policy_action_outbox ADD COLUMN IF NOT EXISTS last_error_code TEXT;

-- ════════════ 3. l2_reject_approved:claim 前确定性拒绝 ════════════
-- 仅处理"未 claim 且未过期"的 APPROVED 票(execution_id IS NULL ⇒ 尚未进入 EXECUTING)。
-- reason_code 走 allowlist(票据级确定性拒绝);未知 → 22023(非重试,编程错误)。
-- 成功:approval APPROVED → FAILED,error='preclaim denied:<reason>'。
-- EXECUTING/UNKNOWN/USED/FAILED/EXPIRED 一律不动(CAS 不匹配 → FALSE)。
-- Controller 在同事务(已 SELECT task_runs FOR UPDATE + 完整 CAS)内调本函数,再更新 outbox/task。
CREATE OR REPLACE FUNCTION l2_reject_approved(p_ticket_id TEXT, p_reason_code TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF p_reason_code NOT IN ('CLAIM_MISMATCH','REPO_NOT_ALLOWED','L2_TICKET_REQUIRED','INVALID_ACTION') THEN
    RAISE EXCEPTION 'l2_reject_approved: reason_code 不在 allowlist(%)', p_reason_code
      USING ERRCODE = '22023';
  END IF;
  UPDATE public.approvals SET
    status='FAILED', error='preclaim denied:' || p_reason_code
  WHERE ticket_id = p_ticket_id
    AND status='APPROVED'
    AND execution_id IS NULL
    AND expires_at > now();
  RETURN FOUND;
END $$;

-- ════════════ OWNER 收敛 + REVOKE PUBLIC + GRANT(顺序同 B4a 模板)════════════
DO $$ BEGIN
  ALTER FUNCTION l2_reject_approved(text,text) OWNER TO mergepilot_l2_owner;
END $$;
ALTER ROLE mergepilot_l2_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
REVOKE ALL ON FUNCTION l2_reject_approved(TEXT,TEXT) FROM PUBLIC;
-- 仅 Controller(mergepilot)可调;approver / policy_gateway_l2 不可(反向测试验证)
GRANT EXECUTE ON FUNCTION l2_reject_approved(TEXT,TEXT) TO mergepilot;
