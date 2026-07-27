-- m3b_b4c.sql — M3-B4c Controller 侧 migration(幂等)。
-- 依赖 m3b_b4.sql(B4a 最新 m3b-b4a.3-closed)。复审 9 条修正的 DB 侧落地:
--   #2 run 级 gating(approval_required)+ #4 绑定 0-PR 有界重试计数(l2_discovery_attempts)
--   #4 幂等建票 l2_ensure_ticket + 活动票据唯一索引(defense-in-depth)
--   #5 APPROVED 执行期过期迁移 l2_expire_approved(此前只有 PENDING 过期)
-- 全部幂等。函数 SECURITY DEFINER 硬化(同 B4a 模板:NOLOGIN owner + 固定 search_path +
--   完全限定 public. + REVOKE PUBLIC + GRANT-after-OWNER;mergepilot 为超管,与 B4a 同路)。

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ════════════ 1. task_runs:run 级 gating + 绑定发现重试计数(复审 #2/#4) ════════════
-- approval_required:TASK_SUBMITTED 时按 L2_MERGE_ENABLED env 写入;后续 verify 只读此字段,
--   防 Controller 重启或开关变更后同一 run 中途切换语义。
-- l2_discovery_attempts:绑定发现"查询成功但 0 PR"的累计次数(网络/认证错误不累加);
--   达阈值 → task HOLD("无 fix PR")。
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS approval_required     BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS l2_discovery_attempts INTEGER NOT NULL DEFAULT 0;

-- ════════════ 2. 未终结票据唯一索引(复审 #4 + B4c-0.1 #1 + B4c-0.2 原子迁移) ════════════
-- 一个 (binding_id, action) 同时只能有一张"未终结"票。阻塞新建 attempt 的态:
--   PENDING/APPROVED/EXECUTING(活动)+ UNKNOWN(未对账,可能已成功→二次 merge 风险)+ USED(已生效)。
-- **只有 FAILED/EXPIRED(终态失败)允许建下一 attempt。** USED/UNKNOWN 绝不自动重建。
-- B4c-0.2 原子性(复审):preflight 查新阻塞集内 (binding,action) 重复 → 拒绝迁移(旧索引保留);
--   DROP+CREATE 包在单一事务,CREATE 失败回滚 DROP,旧保护不消失。
DO $$
DECLARE dups INT;
BEGIN
  SELECT count(*) INTO dups FROM (
    SELECT binding_id, action FROM approvals
      WHERE status IN ('PENDING','APPROVED','EXECUTING','UNKNOWN','USED')
      GROUP BY binding_id, action HAVING count(*) > 1
  ) s;
  IF dups > 0 THEN
    RAISE EXCEPTION 'preflight: 新阻塞集内 % 组 (binding,action) 重复——先清理再迁移(旧索引保留)', dups;
  END IF;
END $$;

BEGIN;
DROP INDEX IF EXISTS uq_active_ticket_per_binding_action;
CREATE UNIQUE INDEX uq_active_ticket_per_binding_action
  ON approvals(binding_id, action) WHERE status IN ('PENDING','APPROVED','EXECUTING','UNKNOWN','USED');
COMMIT;

-- ════════════ 3. l2_ensure_ticket:幂等建票(复审 #4 + B4c-0.1 #2 + B4c-0.2 P2) ════════════
-- 同 (binding, action) 已有"未终结"票 → 返回旧 ticket_id,且校验 **payload/args_hash/双 TTL** 全一致
--   (B4c-0.2 P2:加 exec_ttl_hours 存列 + approval_ttl 由 approval_expires_at-created_at 派生比较;
--    不同 TTL 的同幂等请求拒绝)。不匹配抛 **SQLSTATE 22023**(invalid_parameter_value,非重试——
--    B4c-0.2 P2:初版误用 40001 序列化冲突会让 B4c-2 无限重试确定性冲突)。
-- 无未终结票(前次 FAILED/EXPIRED 或首建)→ 委托 l2_create_ticket。
-- advisory_xact_lock per (run,action) 与 l2_create_ticket 同 → 并发 ensure 串行化,必得同一张票。
CREATE OR REPLACE FUNCTION l2_ensure_ticket(
    p_binding_id TEXT, p_action TEXT, p_canonical_payload JSONB, p_args_hash TEXT,
    p_approval_ttl_hours INT DEFAULT 24, p_exec_ttl_hours INT DEFAULT 1)
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  v_existing TEXT;
  v_run TEXT;
  v_payload JSONB;
  v_hash TEXT;
  v_exec_ttl INT;
  v_approval_interval INTERVAL;
BEGIN
  SELECT run_id INTO v_run FROM public.run_pr_bindings WHERE binding_id=p_binding_id;
  IF v_run IS NULL THEN RAISE EXCEPTION 'binding % not found', p_binding_id; END IF;
  PERFORM pg_advisory_xact_lock(hashtext(v_run || ':' || p_action));

  SELECT ticket_id, canonical_payload, args_hash, exec_ttl_hours,
         (approval_expires_at - created_at)
    INTO v_existing, v_payload, v_hash, v_exec_ttl, v_approval_interval
    FROM public.approvals
    WHERE binding_id=p_binding_id AND action=p_action
      AND status IN ('PENDING','APPROVED','EXECUTING','UNKNOWN','USED')
    ORDER BY attempt_no DESC LIMIT 1
    FOR UPDATE;
  IF v_existing IS NOT NULL THEN
    IF v_payload IS DISTINCT FROM p_canonical_payload
       OR v_hash IS DISTINCT FROM p_args_hash
       OR v_exec_ttl IS DISTINCT FROM p_exec_ttl_hours
       OR v_approval_interval IS DISTINCT FROM make_interval(hours => p_approval_ttl_hours) THEN
      RAISE EXCEPTION 'ensure_ticket: existing ticket % payload/hash/TTL mismatch (status unconsumed; refuse to shadow)', v_existing
        USING ERRCODE = '22023';
    END IF;
    RETURN v_existing;
  END IF;
  RETURN public.l2_create_ticket(p_binding_id, p_action, p_canonical_payload, p_args_hash,
                                  p_approval_ttl_hours, p_exec_ttl_hours);
END $$;
REVOKE ALL ON FUNCTION l2_ensure_ticket(TEXT,TEXT,JSONB,TEXT,INT,INT) FROM PUBLIC;

-- ════════════ 4. l2_expire_approved:APPROVED 执行期过期 → EXPIRED(复审 #5) ════════════
-- B4a 只有 l2_expire_pending(PENDING 超审批期)。APPROVED 起的执行期(expires_at)过期
--   同样需迁移:票 EXPIRED → Controller 把 outbox 标 FAILED + task HOLD。
-- 仅迁移 APPROVED(不动 EXECUTING;EXECUTING 超时走 l2_reconcile_executing)。
CREATE OR REPLACE FUNCTION l2_expire_approved(p_ticket_id TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  UPDATE public.approvals SET status='EXPIRED'
  WHERE ticket_id=p_ticket_id AND status='APPROVED'
    AND expires_at IS NOT NULL AND expires_at < now();
  RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION l2_expire_approved(TEXT) FROM PUBLIC;

-- ════════════ 5. OWNER 收敛 + GRANT(顺序:OWNER → REVOKE PUBLIC → GRANT;与 B4a 同) ════════════
-- B4a 注:GRANT 必须在 ALTER OWNER 之后(否则 CREATE by mergepilot → GRANT TO mergepilot
--   = grant-to-self 空操作 → ALTER OWNER 后 mergepilot 丢执行权)。mergepilot 为超管。
DO $$ BEGIN
  ALTER FUNCTION l2_ensure_ticket(text,text,jsonb,text,integer,integer) OWNER TO mergepilot_l2_owner;
  ALTER FUNCTION l2_expire_approved(text)                          OWNER TO mergepilot_l2_owner;
END $$;

-- 收敛 owner 角色属性(每次跑都收敛,防漂移)
ALTER ROLE mergepilot_l2_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;

-- REVOKE PUBLIC(完整签名)
REVOKE ALL ON FUNCTION l2_ensure_ticket(TEXT,TEXT,JSONB,TEXT,INT,INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION l2_expire_approved(TEXT)                          FROM PUBLIC;

-- Controller(mergepilot)调用入口(B4c 用 ensure/expire,不裸调 l2_create_ticket)
GRANT EXECUTE ON FUNCTION l2_ensure_ticket(TEXT,TEXT,JSONB,TEXT,INT,INT) TO mergepilot;
GRANT EXECUTE ON FUNCTION l2_expire_approved(TEXT)                       TO mergepilot;
