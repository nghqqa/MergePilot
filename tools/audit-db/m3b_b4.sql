-- m3b_b4.sql — M3-B4 审批票据 schema + 受约束 DB 函数 + NOLOGIN owner(B4a)。
-- 依赖 m3_state.sql(task_runs 等)+ m3b_policy.sql(approvals/policy_action_outbox 雏形)。
-- 全部幂等。函数 SECURITY DEFINER 硬化:固定 search_path + 完全限定 public. 表名 + REVOKE PUBLIC EXECUTE。
-- 实现修正:args_hash 完整 64hex;attempt_no 用 pg_advisory_xact_lock(MAX+1)+ UNIQUE 兜底;
--          PENDING 阶段 expires_at=NULL(DROP NOT NULL);l2_owner 需 policy_action_outbox_id_seq 序列权限。

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid / digest

-- ════════════ 1. schema 迁移 ════════════

-- 1.1 run_pr_bindings:Controller 写的 GitHub 权威绑定(FIX 完成时读回,不信任 LLM)
CREATE TABLE IF NOT EXISTS run_pr_bindings (
    binding_id   TEXT PRIMARY KEY,          -- bnd-<UUIDv4>
    run_id       TEXT NOT NULL REFERENCES task_runs(run_id),
    repo         TEXT NOT NULL,             -- owner/repo
    pr_number    INTEGER NOT NULL,
    fix_branch   TEXT NOT NULL,             -- head.ref,如 fix/<run_id>-xxx
    base_branch  TEXT NOT NULL,             -- base.ref(merge 目标,如 main)
    head_sha     TEXT NOT NULL,             -- FIX 完成时 GitHub 实际 head(执行前 TOCTOU 比对)
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_pr_bindings_run ON run_pr_bindings(run_id);

-- 1.2 approvals v2:加列 + DROP expires_at NOT NULL + UNIQUE(run,action,attempt)
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS binding_id          TEXT REFERENCES run_pr_bindings(binding_id);
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS attempt_no          INTEGER NOT NULL DEFAULT 1;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS canonical_payload   JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS args_hash           TEXT NOT NULL DEFAULT '';  -- 完整 64hex(由调用方算,PG 存/比对)
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS execution_id        UUID;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS executing_at        TIMESTAMPTZ;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approval_expires_at TIMESTAMPTZ;               -- PENDING 审批期(24h)
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS exec_ttl_hours      INTEGER NOT NULL DEFAULT 1;
ALTER TABLE approvals ALTER COLUMN expires_at DROP NOT NULL;                                 -- PENDING=NULL,l2_approve 写 approved_at+ttl
ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_run_action_attempt_key;
ALTER TABLE approvals ADD CONSTRAINT approvals_run_action_attempt_key UNIQUE (run_id, action, attempt_no);

-- 1.3 policy_action_outbox:加 lease_expires_at(status CHECK 不变,不加 EXECUTING)
ALTER TABLE policy_action_outbox ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

-- 1.4 task_runs:加 APPROVAL_PENDING 状态(B4 决策)
ALTER TABLE task_runs DROP CONSTRAINT IF EXISTS chk_task_status;
ALTER TABLE task_runs ADD CONSTRAINT chk_task_status CHECK (
    status IN ('SUBMITTED','RUNNING','PASS','FAIL','HOLD','MERGED','ROLLED_BACK','APPROVAL_PENDING'));

-- 1.5 mcp_calls:加 execution_id(L2 审计行串票据)
ALTER TABLE mcp_calls ADD COLUMN IF NOT EXISTS execution_id UUID;

-- ════════════ 2. NOLOGIN owner + 序列权限(实现修正 #4)════════════
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot_l2_owner') THEN
    CREATE ROLE mergepilot_l2_owner NOLOGIN;
  END IF;
END $$;
GRANT SELECT, INSERT, UPDATE ON run_pr_bindings, approvals, policy_action_outbox TO mergepilot_l2_owner;
-- BIGSERIAL 序列:owner 插 outbox 必须有 USAGE(否则 INSERT 失败)
GRANT USAGE, SELECT ON SEQUENCE policy_action_outbox_id_seq TO mergepilot_l2_owner;

-- ════════════ 3. l2_* 函数(SECURITY DEFINER 硬化)════════════
-- 模板:SECURITY DEFINER + SET search_path=pg_catalog,public + 完全限定 public. + REVOKE PUBLIC + 按 role GRANT。

-- ── Controller:建票(原子 attempt_no via pg_advisory_xact_lock + UNIQUE 兜底)──
CREATE OR REPLACE FUNCTION l2_create_ticket(
    p_binding_id TEXT, p_action TEXT, p_canonical_payload JSONB, p_args_hash TEXT,
    p_approval_ttl_hours INT DEFAULT 24, p_exec_ttl_hours INT DEFAULT 1)
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  v_run TEXT; v_repo TEXT; v_pr INT; v_fix TEXT; v_base TEXT; v_sha TEXT;
  v_attempt INT; v_ticket TEXT; v_idem TEXT;
BEGIN
  SELECT run_id, repo, pr_number, fix_branch, base_branch, head_sha
    INTO v_run, v_repo, v_pr, v_fix, v_base, v_sha
  FROM public.run_pr_bindings WHERE binding_id=p_binding_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'binding % not found', p_binding_id; END IF;

  -- B4a.1 P1#2:canonical_payload 的 owner/repo/pullNumber 必须与 binding 一致
  -- (防"票据列批准 PR A、payload 实际指向 PR B")。注意每个 ->> 提取必须显式括号,
  -- 否则 PG 把 || 和 ->> 错误组合成 "payload ->> ('owner'||'/'||payload) ->> 'repo'"(text ->> unknown)。
  IF (p_canonical_payload->>'owner') || '/' || (p_canonical_payload->>'repo') IS DISTINCT FROM v_repo THEN
    RAISE EXCEPTION 'canonical_payload repo (%/%) != binding repo (%)',
      (p_canonical_payload->>'owner'), (p_canonical_payload->>'repo'), v_repo;
  END IF;
  IF COALESCE((p_canonical_payload->>'pullNumber')::int, -1) IS DISTINCT FROM v_pr THEN
    RAISE EXCEPTION 'canonical_payload pullNumber (%) != binding pr (%)',
      (p_canonical_payload->>'pullNumber'), v_pr;
  END IF;

  -- B4a.2 P1#2:封闭 action-specific payload + args_hash 格式 + TTL 边界(不只校验身份)
  IF p_action NOT IN ('merge','close') THEN
    RAISE EXCEPTION 'action 必须 merge/close(revert 走 PR 路径)';
  END IF;
  IF p_args_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'args_hash 必须 64hex(完整 sha256),实际: %', p_args_hash;
  END IF;
  IF p_approval_ttl_hours IS NULL OR p_approval_ttl_hours NOT BETWEEN 1 AND 24 THEN
    RAISE EXCEPTION 'approval TTL 须 1..24h,实际: %', p_approval_ttl_hours;
  END IF;
  IF p_exec_ttl_hours IS NULL OR p_exec_ttl_hours NOT BETWEEN 1 AND 24 THEN
    RAISE EXCEPTION 'exec TTL 须 1..24h,实际: %', p_exec_ttl_hours;
  END IF;
  IF p_action = 'merge' THEN
    -- B4a.3 P1#B:JSON 类型校验(jsonb_typeof,不靠 ->> 隐式转文本)
    IF jsonb_typeof(p_canonical_payload->'merge_method') IS DISTINCT FROM 'string' THEN
      RAISE EXCEPTION 'merge_method 必须是字符串';
    END IF;
    IF jsonb_typeof(p_canonical_payload->'commit_title') IS DISTINCT FROM 'string'
       OR (p_canonical_payload->>'commit_title') = '' THEN
      RAISE EXCEPTION 'commit_title 必须是非空字符串';
    END IF;
    IF (p_canonical_payload->>'merge_method') NOT IN ('merge','squash','rebase') THEN
      RAISE EXCEPTION 'merge_method 非法(%),允许 merge/squash/rebase', (p_canonical_payload->>'merge_method');
    END IF;
    IF p_canonical_payload ? 'state' THEN RAISE EXCEPTION 'merge payload 不该含 state'; END IF;
    IF EXISTS (SELECT 1 FROM jsonb_object_keys(p_canonical_payload) k
               WHERE k NOT IN ('owner','repo','pullNumber','commit_title','merge_method')) THEN
      RAISE EXCEPTION 'merge payload 含未知字段';
    END IF;
  ELSIF p_action = 'close' THEN
    IF jsonb_typeof(p_canonical_payload->'state') IS DISTINCT FROM 'string' THEN
      RAISE EXCEPTION 'state 必须是字符串';
    END IF;
    -- IS DISTINCT FROM 正确处理 NULL(缺 state 时 NULL <> 'closed' 是 NULL 不会触发)
    IF (p_canonical_payload->>'state') IS DISTINCT FROM 'closed' THEN RAISE EXCEPTION 'close 需 state=closed'; END IF;
    IF p_canonical_payload ? 'merge_method' THEN RAISE EXCEPTION 'close payload 不该含 merge_method'; END IF;
    IF EXISTS (SELECT 1 FROM jsonb_object_keys(p_canonical_payload) k
               WHERE k NOT IN ('owner','repo','pullNumber','state','title')) THEN
      RAISE EXCEPTION 'close payload 含未知字段';
    END IF;
  END IF;
  -- B4a.3 P1#B:公共字段类型 + 正整数(owner/repo 非空字符串;pullNumber 数字且正整数)
  IF jsonb_typeof(p_canonical_payload->'owner') IS DISTINCT FROM 'string'
     OR (p_canonical_payload->>'owner') = '' THEN RAISE EXCEPTION 'owner 必须是非空字符串'; END IF;
  IF jsonb_typeof(p_canonical_payload->'repo') IS DISTINCT FROM 'string'
     OR (p_canonical_payload->>'repo') = '' THEN RAISE EXCEPTION 'repo 必须是非空字符串'; END IF;
  IF jsonb_typeof(p_canonical_payload->'pullNumber') IS DISTINCT FROM 'number' THEN
    RAISE EXCEPTION 'pullNumber 必须是数字(非字符串)';
  END IF;
  IF (p_canonical_payload->>'pullNumber') !~ '^[0-9]+$' THEN
    RAISE EXCEPTION 'pullNumber 必须是正整数';
  END IF;

  -- 实现修正 #2:advisory 锁 per (run_id, action),再 MAX+1;UNIQUE 兜底
  PERFORM pg_advisory_xact_lock(hashtext(v_run || ':' || p_action));
  SELECT COALESCE(MAX(attempt_no),0)+1 INTO v_attempt
  FROM public.approvals WHERE binding_id=p_binding_id AND action=p_action;

  v_ticket := 'tkt-' || gen_random_uuid()::text;
  v_idem   := encode(digest(v_run || p_action || p_binding_id || v_attempt::text, 'sha256'),'hex');

  INSERT INTO public.approvals(
    ticket_id, binding_id, run_id, action, repo, pr_number, target_branch,
    expected_head_sha, status, canonical_payload, args_hash, attempt_no,
    approval_expires_at, exec_ttl_hours, expires_at, created_at)
  VALUES (
    v_ticket, p_binding_id, v_run, p_action, v_repo, v_pr, v_base,
    v_sha, 'PENDING', p_canonical_payload, p_args_hash, v_attempt,
    now() + make_interval(hours => p_approval_ttl_hours), p_exec_ttl_hours, NULL, now());

  INSERT INTO public.policy_action_outbox(
    ticket_id, run_id, action, repo, pr_number, target_branch, args_hash, idempotency_key, status, created_at)
  VALUES (
    v_ticket, v_run, p_action, v_repo, v_pr, v_base, p_args_hash, v_idem, 'PENDING_DISPATCH', now());

  RETURN v_ticket;
END $$;
REVOKE ALL ON FUNCTION l2_create_ticket(TEXT,TEXT,JSONB,TEXT,INT,INT) FROM PUBLIC;

-- ── Approver:列 PENDING 票(返回完整 payload,审批人能看清 merge_method/commit_title 等)──
-- B4a.1 改了 RETURNS 列,CREATE OR REPLACE 不能改返回类型,先 DROP(IF EXISTS 幂等)。
DROP FUNCTION IF EXISTS l2_pending_list();
CREATE OR REPLACE FUNCTION l2_pending_list()
RETURNS TABLE(ticket_id TEXT, run_id TEXT, action TEXT, repo TEXT, pr_number INTEGER,
              canonical_payload JSONB, args_hash TEXT, expected_head_sha TEXT,
              target_branch TEXT, attempt_no INTEGER,
              created_at TIMESTAMPTZ, approval_expires_at TIMESTAMPTZ)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT ticket_id, run_id, action, repo, pr_number, canonical_payload, args_hash,
         expected_head_sha, target_branch, attempt_no, created_at, approval_expires_at
  FROM public.approvals WHERE status='PENDING' ORDER BY created_at;
$$;
REVOKE ALL ON FUNCTION l2_pending_list() FROM PUBLIC;

-- ── Approver:审批 PENDING→APPROVED(写 approved_at + expires_at=approved_at+exec_ttl)──
CREATE OR REPLACE FUNCTION l2_approve(p_ticket_id TEXT, p_approved_by TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_ttl INT;
BEGIN
  SELECT exec_ttl_hours INTO v_ttl FROM public.approvals WHERE ticket_id=p_ticket_id;
  UPDATE public.approvals SET
    status='APPROVED', approved_by=p_approved_by, approved_at=now(),
    expires_at = now() + make_interval(hours => COALESCE(v_ttl,1))
  WHERE ticket_id=p_ticket_id AND status='PENDING' AND approval_expires_at > now();
  RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION l2_approve(TEXT,TEXT) FROM PUBLIC;

-- ── Gateway:claim(一次 CAS 全校验;不匹配票据保持 APPROVED)──
-- 实现修正 #1:args_hash 完整 64hex 比对(由 Gateway 调用方算好传入)
CREATE OR REPLACE FUNCTION l2_claim_ticket(
    p_ticket_id TEXT, p_action TEXT, p_repo TEXT, p_pr_number INTEGER, p_args_hash TEXT)
RETURNS TABLE(execution_id UUID, canonical_payload JSONB, expected_head_sha TEXT, target_branch TEXT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  UPDATE public.approvals SET
    status='EXECUTING', execution_id=gen_random_uuid(), executing_at=now()
  WHERE ticket_id=p_ticket_id AND status='APPROVED'
    AND action=p_action AND repo=p_repo AND pr_number=p_pr_number AND args_hash=p_args_hash
    AND expires_at IS NOT NULL AND expires_at > now()
  RETURNING approvals.execution_id, approvals.canonical_payload, approvals.expected_head_sha, approvals.target_branch
  INTO execution_id, canonical_payload, expected_head_sha, target_branch;
  -- 无匹配(票据保持 APPROVED):返回 0 行,Gateway 据此 POLICY_DENIED CLAIM_MISMATCH
  IF NOT FOUND THEN RETURN; END IF;
  RETURN NEXT;
END $$;
REVOKE ALL ON FUNCTION l2_claim_ticket(TEXT,TEXT,TEXT,INTEGER,TEXT) FROM PUBLIC;

-- ── Gateway:complete/fail/mark_unknown(CAS EXECUTING + execution_id 匹配)──
CREATE OR REPLACE FUNCTION l2_complete_ticket(p_ticket_id TEXT, p_execution_id UUID, p_result_sha TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  UPDATE public.approvals SET status='USED', used_at=now(), result_sha=p_result_sha
  WHERE ticket_id=p_ticket_id AND status='EXECUTING' AND execution_id=p_execution_id;
  RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION l2_complete_ticket(TEXT,UUID,TEXT) FROM PUBLIC;

CREATE OR REPLACE FUNCTION l2_fail_ticket(p_ticket_id TEXT, p_execution_id UUID, p_reason TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  UPDATE public.approvals SET status='FAILED', error=p_reason
  WHERE ticket_id=p_ticket_id AND status='EXECUTING' AND execution_id=p_execution_id;
  RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION l2_fail_ticket(TEXT,UUID,TEXT) FROM PUBLIC;

CREATE OR REPLACE FUNCTION l2_mark_unknown(p_ticket_id TEXT, p_execution_id UUID, p_reason TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  UPDATE public.approvals SET status='UNKNOWN', error=p_reason
  WHERE ticket_id=p_ticket_id AND status='EXECUTING' AND execution_id=p_execution_id;
  RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION l2_mark_unknown(TEXT,UUID,TEXT) FROM PUBLIC;

-- ── Controller:对账(UNKNOWN / 超时 EXECUTING)+ 过期 ──
-- p_effect_applied:merge=已 merged;close=PR state=closed(Controller 按 action 判定后传入)
-- B4a.1 改了参数名(p_merged→p_effect_applied),先 DROP(参数名变更 REPLACE 不支持)。
DROP FUNCTION IF EXISTS l2_reconcile_unknown(TEXT,BOOLEAN,TEXT);
DROP FUNCTION IF EXISTS l2_reconcile_executing(TEXT,BOOLEAN,TEXT);
CREATE OR REPLACE FUNCTION l2_reconcile_unknown(p_ticket_id TEXT, p_effect_applied BOOLEAN, p_actual_sha TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF p_effect_applied THEN
    UPDATE public.approvals SET status='USED', used_at=now(), result_sha=COALESCE(p_actual_sha,result_sha)
    WHERE ticket_id=p_ticket_id AND status='UNKNOWN';
  ELSE
    UPDATE public.approvals SET status='FAILED', error='reconcile: effect not applied'
    WHERE ticket_id=p_ticket_id AND status='UNKNOWN';
  END IF;
  RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION l2_reconcile_unknown(TEXT,BOOLEAN,TEXT) FROM PUBLIC;

-- B4a.1 P2#7:仅对账超时 EXECUTING(executing_at < now()-120s),防提前对账竞态
CREATE OR REPLACE FUNCTION l2_reconcile_executing(p_ticket_id TEXT, p_effect_applied BOOLEAN, p_actual_sha TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF p_effect_applied THEN
    UPDATE public.approvals SET status='USED', used_at=now(), result_sha=COALESCE(p_actual_sha,result_sha)
    WHERE ticket_id=p_ticket_id AND status='EXECUTING'
      AND executing_at < now() - interval '120 seconds';
  ELSE
    UPDATE public.approvals SET status='FAILED', error='reconcile: effect not applied after timeout'
    WHERE ticket_id=p_ticket_id AND status='EXECUTING'
      AND executing_at < now() - interval '120 seconds';
  END IF;
  RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION l2_reconcile_executing(TEXT,BOOLEAN,TEXT) FROM PUBLIC;

CREATE OR REPLACE FUNCTION l2_expire_pending(p_ticket_id TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  UPDATE public.approvals SET status='EXPIRED'
  WHERE ticket_id=p_ticket_id AND status='PENDING' AND approval_expires_at <= now();
  RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION l2_expire_pending(TEXT) FROM PUBLIC;

-- ════════════ OWNER 收敛 + 精确 GRANT(顺序:OWNER → REVOKE PUBLIC → GRANT)════════════
-- B4a.2 P1#4:GRANT 必须在 ALTER OWNER 之后。否则 CREATE by mergepilot → GRANT TO mergepilot
--   = grant-to-self 空操作 → ALTER OWNER 后 mergepilot 丢执行权(只能靠 superuser 旁路)。

-- 1. 业务函数 OWNER → mergepilot_l2_owner(NOLOGIN)。**完整 regprocedure 签名 allowlist**
--    (B4a.3:不再按 proname,避免未来同名 overload 被误伤;签名错会 cast 失败报警)。
DO $$ DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY[
    'l2_create_ticket(text,text,jsonb,text,integer,integer)',
    'l2_claim_ticket(text,text,text,integer,text)',
    'l2_complete_ticket(text,uuid,text)',
    'l2_fail_ticket(text,uuid,text)',
    'l2_mark_unknown(text,uuid,text)',
    'l2_approve(text,text)',
    'l2_pending_list()',
    'l2_reconcile_unknown(text,boolean,text)',
    'l2_reconcile_executing(text,boolean,text)',
    'l2_expire_pending(text)'
  ] LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO mergepilot_l2_owner', f::regprocedure::text);
  END LOOP;
END $$;

-- 2. 恢复 vector 扩展成员函数 owner(通过 pg_depend deptype='e' 定位,不按名匹配)
DO $$ DECLARE r record; v_owner text;
BEGIN
  SELECT rolname INTO v_owner FROM pg_roles WHERE oid=(SELECT extowner FROM pg_extension WHERE extname='vector');
  IF v_owner IS NULL THEN RETURN; END IF;
  FOR r IN SELECT p.oid::regprocedure::text AS f FROM pg_proc p
    JOIN pg_depend d ON d.classid='pg_proc'::regclass AND d.objid=p.oid
                     AND d.refclassid='pg_extension'::regclass AND d.deptype='e'
    JOIN pg_extension e ON d.refobjid=e.oid
    WHERE e.extname='vector'
  LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO %I', r.f, v_owner);
  END LOOP;
END $$;

-- 3. 收敛 mergepilot_l2_owner 属性(每次跑都收敛,不只创建时)
ALTER ROLE mergepilot_l2_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;

-- 4. REVOKE PUBLIC(完整签名 allowlist)
DO $$ DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY[
    'l2_create_ticket(text,text,jsonb,text,integer,integer)',
    'l2_claim_ticket(text,text,text,integer,text)',
    'l2_complete_ticket(text,uuid,text)',
    'l2_fail_ticket(text,uuid,text)',
    'l2_mark_unknown(text,uuid,text)',
    'l2_approve(text,text)',
    'l2_pending_list()',
    'l2_reconcile_unknown(text,boolean,text)',
    'l2_reconcile_executing(text,boolean,text)',
    'l2_expire_pending(text)'
  ] LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f::regprocedure::text);
  END LOOP;
END $$;

-- 5. GRANT(OWNER 之后,非 grant-to-self)。Controller(mergepilot)调 create/reconcile/expire。
GRANT EXECUTE ON FUNCTION l2_create_ticket(TEXT,TEXT,JSONB,TEXT,INT,INT) TO mergepilot;
GRANT EXECUTE ON FUNCTION l2_reconcile_unknown(TEXT,BOOLEAN,TEXT) TO mergepilot;
GRANT EXECUTE ON FUNCTION l2_reconcile_executing(TEXT,BOOLEAN,TEXT) TO mergepilot;
GRANT EXECUTE ON FUNCTION l2_expire_pending(TEXT) TO mergepilot;
-- 注:policy_gateway_l2 / mergepilot_approver 的 EXECUTE 授权在 m3b-b4-create-roles.sh(账号建好后,同样在 OWNER 之后)
