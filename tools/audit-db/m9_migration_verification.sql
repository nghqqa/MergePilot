-- ============================================================================
-- m9_migration_verification.sql — 数据库迁移验证纳入 PR 验收(决赛 D2)
-- ----------------------------------------------------------------------------
-- 设计原则(评委意见二 + 决赛工作令 §四):
--   * 不新建平行状态机:只在现有 revision_bindings(不可变,一 run 一 revision)、
--     approvals(l2_* 票据)之上挂子表;run 的状态仍由 task_runs / controller 管理。
--   * 验证对象完整绑定:候选提交 SHA(revision_bindings.head_sha)、迁移脚本摘要、
--     数据基线摘要(schema+data)、试验实例身份(容器/分支)、验证报告摘要、环境版本。
--   * 回写与授权执行都检查当前版本:db_release_gate() 每次调用重算 10 项匹配关系,
--     结果不落库、不能被旧回调"写回有效"。
--   * 事务 + 唯一约束处理重复回调 / 重试 / 并发审批:
--       - migration_verifications UNIQUE(candidate,baseline,attempt):同键同摘要 → 幂等 no-op;
--         同键异摘要 → 拒绝(必须开新 attempt)。
--       - approval_verification_bindings PK(ticket_id):并发绑定只有一个成功。
--       - 四张表 UPDATE/DELETE 触发器拒绝(复用 m4f1 的 public._immutable)。
--   * 试验实例种类如实标注:AGENTIC_DB_BRANCH / ISOLATED_POSTGRES / SIMULATED。
-- 幂等:可重复执行(IF NOT EXISTS / OR REPLACE / DROP TRIGGER IF EXISTS)。
-- 依赖:m4f1_state.sql(revision_bindings, _immutable)、m3b_b4.sql(approvals.binding_id 等)。
-- ============================================================================
BEGIN;

-- ═══ 1. data_baselines(脱敏数据基线登记;不可变)═══
CREATE TABLE IF NOT EXISTS public.data_baselines (
  baseline_id    TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  source_kind    TEXT NOT NULL CHECK (source_kind IN ('SYNTHETIC','REDACTED_SNAPSHOT')),
  schema_digest  TEXT NOT NULL CHECK (schema_digest ~ '^[0-9a-f]{64}$'),
  data_digest    TEXT NOT NULL CHECK (data_digest ~ '^[0-9a-f]{64}$'),
  row_counts     JSONB NOT NULL DEFAULT '{}'::jsonb,
  pg_version     TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_data_baselines_digest UNIQUE (schema_digest, data_digest)
);

-- ═══ 2. migration_candidates(一份迁移脚本版本 ↔ 一个不可变 revision)═══
-- candidate_key 是逻辑候选("candidate-a"),revision_no 是同一候选的修订序号:
-- "修订同一候选并重新验证" = 同 candidate_key、revision_no+1、parent 指向上一版。
CREATE TABLE IF NOT EXISTS public.migration_candidates (
  candidate_id         TEXT PRIMARY KEY,
  run_id               TEXT NOT NULL REFERENCES public.task_runs(run_id),
  revision_binding_id  TEXT NOT NULL REFERENCES public.revision_bindings(binding_id),
  head_sha             TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
  candidate_key        TEXT NOT NULL CHECK (candidate_key ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
  revision_no          INTEGER NOT NULL CHECK (revision_no >= 1),
  parent_candidate_id  TEXT REFERENCES public.migration_candidates(candidate_id),
  script_path          TEXT NOT NULL,
  script_digest        TEXT NOT NULL CHECK (script_digest ~ '^[0-9a-f]{64}$'),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_migration_candidates_rev UNIQUE (run_id, candidate_key, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_migration_candidates_run ON public.migration_candidates(run_id);

-- head_sha 必须等于所绑定 revision 的 head_sha,且 run 一致(fail-closed)。
CREATE OR REPLACE FUNCTION public._mv_candidate_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_rb record;
BEGIN
  SELECT run_id, head_sha INTO v_rb FROM public.revision_bindings WHERE binding_id = NEW.revision_binding_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'migration_candidates: revision binding % not found', NEW.revision_binding_id; END IF;
  IF v_rb.run_id <> NEW.run_id THEN
    RAISE EXCEPTION 'migration_candidates: revision % belongs to run %, not %', NEW.revision_binding_id, v_rb.run_id, NEW.run_id;
  END IF;
  IF v_rb.head_sha <> NEW.head_sha THEN
    RAISE EXCEPTION 'migration_candidates: head_sha % != revision head %', NEW.head_sha, v_rb.head_sha;
  END IF;
  IF NEW.parent_candidate_id IS NOT NULL THEN
    IF NOT EXISTS (SELECT 1 FROM public.migration_candidates p
                   WHERE p.candidate_id = NEW.parent_candidate_id AND p.candidate_key = NEW.candidate_key) THEN
      RAISE EXCEPTION 'migration_candidates: parent % must share candidate_key %', NEW.parent_candidate_id, NEW.candidate_key;
    END IF;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_migration_candidates_guard ON public.migration_candidates;
CREATE TRIGGER trg_migration_candidates_guard BEFORE INSERT ON public.migration_candidates
  FOR EACH ROW EXECUTE FUNCTION public._mv_candidate_guard();

-- ═══ 3. migration_verifications(验证回写;不可变;同键同摘要幂等)═══
CREATE TABLE IF NOT EXISTS public.migration_verifications (
  verification_id     TEXT PRIMARY KEY,
  candidate_id        TEXT NOT NULL REFERENCES public.migration_candidates(candidate_id),
  baseline_id         TEXT NOT NULL REFERENCES public.data_baselines(baseline_id),
  attempt             INTEGER NOT NULL CHECK (attempt >= 1),
  trial_kind          TEXT NOT NULL CHECK (trial_kind IN ('AGENTIC_DB_BRANCH','ISOLATED_POSTGRES','SIMULATED')),
  trial_instance      TEXT NOT NULL,
  env_versions        JSONB NOT NULL,
  code_tests_verdict  TEXT NOT NULL CHECK (code_tests_verdict IN ('PASS','FAIL','NOT_RUN')),
  migration_verdict   TEXT NOT NULL CHECK (migration_verdict IN ('PASS','FAIL','ERROR')),
  failure_class       TEXT CHECK (failure_class IN ('HISTORICAL_DATA_INCOMPATIBLE','OLD_APP_INCOMPATIBLE','SCRIPT_ERROR','ASSERTION_FAILED')),
  assertions          JSONB NOT NULL DEFAULT '[]'::jsonb,
  report_digest       TEXT NOT NULL CHECK (report_digest ~ '^[0-9a-f]{64}$'),
  recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_migration_verifications_attempt UNIQUE (candidate_id, baseline_id, attempt),
  CONSTRAINT chk_mv_fail_needs_class CHECK (migration_verdict = 'PASS' OR failure_class IS NOT NULL),
  CONSTRAINT chk_mv_pass_no_class CHECK (migration_verdict <> 'PASS' OR failure_class IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_migration_verifications_candidate ON public.migration_verifications(candidate_id);

-- ═══ 4. approval_verification_bindings(票据 ↔ 验证 1:1;不可变)═══
-- 绑定时把四个摘要快照进来:闸门用它们与"当前"值逐项比对。
CREATE TABLE IF NOT EXISTS public.approval_verification_bindings (
  ticket_id             TEXT PRIMARY KEY REFERENCES public.approvals(ticket_id),
  verification_id       TEXT NOT NULL REFERENCES public.migration_verifications(verification_id),
  head_sha              TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
  script_digest         TEXT NOT NULL,
  baseline_data_digest  TEXT NOT NULL,
  report_digest         TEXT NOT NULL,
  bound_by              TEXT NOT NULL,
  bound_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 不可变触发器(复用 m4f1 的 public._immutable:UPDATE/DELETE 一律拒绝)
DROP TRIGGER IF EXISTS trg_data_baselines_immutable ON public.data_baselines;
CREATE TRIGGER trg_data_baselines_immutable BEFORE UPDATE OR DELETE ON public.data_baselines
  FOR EACH ROW EXECUTE FUNCTION public._immutable();
DROP TRIGGER IF EXISTS trg_migration_candidates_immutable ON public.migration_candidates;
CREATE TRIGGER trg_migration_candidates_immutable BEFORE UPDATE OR DELETE ON public.migration_candidates
  FOR EACH ROW EXECUTE FUNCTION public._immutable();
DROP TRIGGER IF EXISTS trg_migration_verifications_immutable ON public.migration_verifications;
CREATE TRIGGER trg_migration_verifications_immutable BEFORE UPDATE OR DELETE ON public.migration_verifications
  FOR EACH ROW EXECUTE FUNCTION public._immutable();
DROP TRIGGER IF EXISTS trg_approval_verification_bindings_immutable ON public.approval_verification_bindings;
CREATE TRIGGER trg_approval_verification_bindings_immutable BEFORE UPDATE OR DELETE ON public.approval_verification_bindings
  FOR EACH ROW EXECUTE FUNCTION public._immutable();

-- ═══ 5. 函数 ═══

-- 5.1 登记基线:同 (schema_digest, data_digest) 幂等返回已有 id。
CREATE OR REPLACE FUNCTION public.mv_register_baseline(
  p_name TEXT, p_source_kind TEXT, p_schema_digest TEXT, p_data_digest TEXT,
  p_row_counts JSONB, p_pg_version TEXT) RETURNS TEXT
SECURITY DEFINER SET search_path = pg_catalog, public LANGUAGE plpgsql AS $$
DECLARE v_id TEXT;
BEGIN
  SELECT baseline_id INTO v_id FROM public.data_baselines
   WHERE schema_digest = p_schema_digest AND data_digest = p_data_digest;
  IF FOUND THEN RETURN v_id; END IF;
  v_id := 'bl-' || left(p_data_digest, 16);
  INSERT INTO public.data_baselines(baseline_id, name, source_kind, schema_digest, data_digest, row_counts, pg_version)
  VALUES (v_id, p_name, p_source_kind, p_schema_digest, p_data_digest, COALESCE(p_row_counts,'{}'::jsonb), p_pg_version)
  ON CONFLICT (schema_digest, data_digest) DO NOTHING;
  SELECT baseline_id INTO v_id FROM public.data_baselines
   WHERE schema_digest = p_schema_digest AND data_digest = p_data_digest;
  RETURN v_id;
END $$;

-- 5.2 登记候选:以 run 的不可变 revision 为锚;同 (run, key, revision_no) 同脚本摘要幂等,
--     异摘要拒绝(修订序号一旦登记不可改脚本 —— 要改就开下一个 revision_no)。
CREATE OR REPLACE FUNCTION public.mv_register_candidate(
  p_run_id TEXT, p_candidate_key TEXT, p_revision_no INTEGER, p_parent_candidate_id TEXT,
  p_script_path TEXT, p_script_digest TEXT) RETURNS TEXT
SECURITY DEFINER SET search_path = pg_catalog, public LANGUAGE plpgsql AS $$
DECLARE v_rb record; v_existing record; v_id TEXT;
BEGIN
  SELECT binding_id, head_sha INTO v_rb FROM public.revision_bindings WHERE run_id = p_run_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'mv_register_candidate: run % has no revision binding', p_run_id USING ERRCODE='P0001'; END IF;
  SELECT candidate_id, script_digest INTO v_existing FROM public.migration_candidates
   WHERE run_id = p_run_id AND candidate_key = p_candidate_key AND revision_no = p_revision_no;
  IF FOUND THEN
    IF v_existing.script_digest = p_script_digest THEN RETURN v_existing.candidate_id; END IF;
    RAISE EXCEPTION 'mv_register_candidate: % rev % already registered with a different script digest', p_candidate_key, p_revision_no
      USING ERRCODE='23505';
  END IF;
  v_id := 'mc-' || left(encode(digest(p_run_id || ':' || p_candidate_key || ':' || p_revision_no::text, 'sha256'), 'hex'), 24);
  INSERT INTO public.migration_candidates(candidate_id, run_id, revision_binding_id, head_sha, candidate_key,
                                          revision_no, parent_candidate_id, script_path, script_digest)
  VALUES (v_id, p_run_id, v_rb.binding_id, v_rb.head_sha, p_candidate_key,
          p_revision_no, p_parent_candidate_id, p_script_path, p_script_digest);
  RETURN v_id;
END $$;

-- 5.3 回写验证结果:幂等重复回调;同 attempt 异摘要 → 拒绝;行一旦写入不可变。
CREATE OR REPLACE FUNCTION public.mv_record_verification(
  p_candidate_id TEXT, p_baseline_id TEXT, p_attempt INTEGER, p_trial_kind TEXT, p_trial_instance TEXT,
  p_env_versions JSONB, p_code_tests_verdict TEXT, p_migration_verdict TEXT, p_failure_class TEXT,
  p_assertions JSONB, p_report_digest TEXT) RETURNS TEXT
SECURITY DEFINER SET search_path = pg_catalog, public LANGUAGE plpgsql AS $$
DECLARE v_id TEXT; v_existing record;
BEGIN
  v_id := 'mv-' || left(encode(digest(p_candidate_id || ':' || p_baseline_id || ':' || p_attempt::text, 'sha256'), 'hex'), 24);
  SELECT verification_id, report_digest INTO v_existing FROM public.migration_verifications
   WHERE candidate_id = p_candidate_id AND baseline_id = p_baseline_id AND attempt = p_attempt;
  IF FOUND THEN
    IF v_existing.report_digest = p_report_digest THEN RETURN v_existing.verification_id; END IF;  -- duplicate callback: no-op
    RAISE EXCEPTION 'mv_record_verification: attempt % of % already recorded with a different report; use a new attempt',
      p_attempt, p_candidate_id USING ERRCODE='23505';
  END IF;
  INSERT INTO public.migration_verifications(verification_id, candidate_id, baseline_id, attempt, trial_kind, trial_instance,
    env_versions, code_tests_verdict, migration_verdict, failure_class, assertions, report_digest)
  VALUES (v_id, p_candidate_id, p_baseline_id, p_attempt, p_trial_kind, p_trial_instance,
    p_env_versions, p_code_tests_verdict, p_migration_verdict, p_failure_class, COALESCE(p_assertions,'[]'::jsonb), p_report_digest)
  ON CONFLICT (candidate_id, baseline_id, attempt) DO NOTHING;   -- 并发同键:让先到者生效
  SELECT verification_id, report_digest INTO v_existing FROM public.migration_verifications
   WHERE candidate_id = p_candidate_id AND baseline_id = p_baseline_id AND attempt = p_attempt;
  IF v_existing.report_digest <> p_report_digest THEN
    RAISE EXCEPTION 'mv_record_verification: lost race to a different report for attempt %', p_attempt USING ERRCODE='23505';
  END IF;
  RETURN v_existing.verification_id;
END $$;

-- 5.4 把验证绑定到 L2 票据(审批人动作):所有匹配关系在同一事务内校验;PK 保证并发只有一个成功。
CREATE OR REPLACE FUNCTION public.l2_bind_verification(p_ticket_id TEXT, p_verification_id TEXT) RETURNS BOOLEAN
SECURITY DEFINER SET search_path = pg_catalog, public LANGUAGE plpgsql AS $$
DECLARE v_t record; v_v record; v_c record; v_b record; v_rb TEXT;
BEGIN
  SELECT ticket_id, run_id, action, status, expected_head_sha INTO v_t FROM public.approvals WHERE ticket_id = p_ticket_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'l2_bind_verification: ticket % not found', p_ticket_id; END IF;
  IF v_t.status NOT IN ('PENDING','APPROVED') THEN
    RAISE EXCEPTION 'l2_bind_verification: ticket % is %, cannot bind', p_ticket_id, v_t.status;
  END IF;
  SELECT v.verification_id, v.candidate_id, v.baseline_id, v.migration_verdict, v.code_tests_verdict, v.report_digest
    INTO v_v FROM public.migration_verifications v WHERE v.verification_id = p_verification_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'l2_bind_verification: verification % not found', p_verification_id; END IF;
  IF v_v.migration_verdict <> 'PASS' OR v_v.code_tests_verdict <> 'PASS' THEN
    RAISE EXCEPTION 'l2_bind_verification: verification % is not PASS/PASS (migration=%, code_tests=%)',
      p_verification_id, v_v.migration_verdict, v_v.code_tests_verdict;
  END IF;
  SELECT run_id, head_sha, script_digest INTO v_c FROM public.migration_candidates WHERE candidate_id = v_v.candidate_id;
  IF v_c.run_id <> v_t.run_id THEN
    RAISE EXCEPTION 'l2_bind_verification: verification belongs to run %, ticket to run %', v_c.run_id, v_t.run_id;
  END IF;
  IF v_t.expected_head_sha IS DISTINCT FROM v_c.head_sha THEN
    RAISE EXCEPTION 'l2_bind_verification: ticket head % != verified head %', v_t.expected_head_sha, v_c.head_sha;
  END IF;
  SELECT head_sha INTO v_rb FROM public.revision_bindings WHERE run_id = v_t.run_id;
  IF v_rb IS DISTINCT FROM v_c.head_sha THEN
    RAISE EXCEPTION 'l2_bind_verification: revision head % != verified head %', v_rb, v_c.head_sha;
  END IF;
  SELECT data_digest INTO v_b FROM public.data_baselines WHERE baseline_id = v_v.baseline_id;
  BEGIN
    INSERT INTO public.approval_verification_bindings(ticket_id, verification_id, head_sha, script_digest,
                                                       baseline_data_digest, report_digest, bound_by)
    VALUES (p_ticket_id, p_verification_id, v_c.head_sha, v_c.script_digest, v_b.data_digest, v_v.report_digest, session_user);
  EXCEPTION WHEN unique_violation THEN
    RAISE EXCEPTION 'l2_bind_verification: ticket % already bound', p_ticket_id USING ERRCODE='23505';
  END;
  RETURN TRUE;
END $$;

-- 5.5 发布闸门:每次调用重算,结果不落库。授权执行(gateway)与回写路径都必须先过这一关。
--     p_target_data_digest:执行方传入"即将被迁移的目标数据"摘要;与绑定时快照的基线摘要不符 →
--     数据版本已变化,旧批准失效(TARGET_DATA_DIGEST_MISMATCH)。
DROP FUNCTION IF EXISTS public.db_release_gate(TEXT);
CREATE OR REPLACE FUNCTION public.db_release_gate(p_ticket_id TEXT, p_target_data_digest TEXT DEFAULT NULL)
RETURNS TABLE(valid BOOLEAN, reason TEXT, ticket_status TEXT, bound_head_sha TEXT, current_head_sha TEXT, verification_id TEXT)
SECURITY DEFINER SET search_path = pg_catalog, public LANGUAGE plpgsql AS $$
DECLARE v_t record; v_b record; v_rev TEXT; v_prb TEXT; v_latest TEXT; v_v record; v_bl TEXT;
BEGIN
  SELECT a.ticket_id, a.run_id, a.repo, a.pr_number, a.status, a.expected_head_sha, a.expires_at
    INTO v_t FROM public.approvals a WHERE a.ticket_id = p_ticket_id;
  IF NOT FOUND THEN RETURN QUERY SELECT FALSE, 'TICKET_NOT_FOUND', NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT; RETURN; END IF;
  SELECT * INTO v_b FROM public.approval_verification_bindings WHERE ticket_id = p_ticket_id;
  IF NOT FOUND THEN RETURN QUERY SELECT FALSE, 'NOT_BOUND_TO_VERIFICATION', v_t.status, NULL::TEXT, NULL::TEXT, NULL::TEXT; RETURN; END IF;
  SELECT head_sha INTO v_rev FROM public.revision_bindings WHERE run_id = v_t.run_id;
  SELECT head_sha INTO v_prb FROM public.run_pr_bindings WHERE run_id = v_t.run_id;
  SELECT rb.head_sha INTO v_latest FROM public.revision_bindings rb
   WHERE rb.repo = v_t.repo AND rb.pr_number = v_t.pr_number ORDER BY rb.recorded_at DESC, rb.binding_id DESC LIMIT 1;
  IF v_t.status NOT IN ('APPROVED','EXECUTING') THEN
    RETURN QUERY SELECT FALSE, 'TICKET_NOT_APPROVED', v_t.status, v_b.head_sha, v_latest, v_b.verification_id; RETURN; END IF;
  IF v_t.expires_at IS NOT NULL AND v_t.expires_at <= now() THEN
    RETURN QUERY SELECT FALSE, 'TICKET_EXPIRED', v_t.status, v_b.head_sha, v_latest, v_b.verification_id; RETURN; END IF;
  IF v_t.expected_head_sha IS DISTINCT FROM v_b.head_sha THEN
    RETURN QUERY SELECT FALSE, 'TICKET_HEAD_MISMATCH', v_t.status, v_b.head_sha, v_t.expected_head_sha, v_b.verification_id; RETURN; END IF;
  IF v_rev IS DISTINCT FROM v_b.head_sha THEN
    RETURN QUERY SELECT FALSE, 'REVISION_HEAD_MISMATCH', v_t.status, v_b.head_sha, v_rev, v_b.verification_id; RETURN; END IF;
  IF v_prb IS DISTINCT FROM v_b.head_sha THEN
    RETURN QUERY SELECT FALSE, 'PR_BINDING_HEAD_MISMATCH', v_t.status, v_b.head_sha, v_prb, v_b.verification_id; RETURN; END IF;
  IF v_latest IS DISTINCT FROM v_b.head_sha THEN
    RETURN QUERY SELECT FALSE, 'STALE_SUPERSEDED_BY_NEW_REVISION', v_t.status, v_b.head_sha, v_latest, v_b.verification_id; RETURN; END IF;
  SELECT v.migration_verdict, v.code_tests_verdict, v.report_digest, v.baseline_id, c.script_digest
    INTO v_v FROM public.migration_verifications v JOIN public.migration_candidates c ON c.candidate_id = v.candidate_id
   WHERE v.verification_id = v_b.verification_id;
  IF v_v.migration_verdict <> 'PASS' OR v_v.code_tests_verdict <> 'PASS' OR v_v.report_digest <> v_b.report_digest
     OR v_v.script_digest <> v_b.script_digest THEN
    RETURN QUERY SELECT FALSE, 'VERIFICATION_MISMATCH', v_t.status, v_b.head_sha, v_latest, v_b.verification_id; RETURN; END IF;
  SELECT data_digest INTO v_bl FROM public.data_baselines WHERE baseline_id = v_v.baseline_id;
  IF v_bl IS DISTINCT FROM v_b.baseline_data_digest THEN
    RETURN QUERY SELECT FALSE, 'BASELINE_MISMATCH', v_t.status, v_b.head_sha, v_latest, v_b.verification_id; RETURN; END IF;
  IF p_target_data_digest IS NOT NULL AND p_target_data_digest <> v_b.baseline_data_digest THEN
    RETURN QUERY SELECT FALSE, 'TARGET_DATA_DIGEST_MISMATCH', v_t.status, v_b.head_sha, v_latest, v_b.verification_id; RETURN; END IF;
  RETURN QUERY SELECT TRUE, 'OK', v_t.status, v_b.head_sha, v_latest, v_b.verification_id;
END $$;

-- 5.6 run 视角的迁移验证状态(控制台 / 演示平台只读消费)
CREATE OR REPLACE FUNCTION public.mv_run_status(p_run_id TEXT)
RETURNS TABLE(candidate_key TEXT, revision_no INTEGER, candidate_id TEXT, head_sha TEXT, script_digest TEXT,
              attempt INTEGER, migration_verdict TEXT, code_tests_verdict TEXT, failure_class TEXT,
              trial_kind TEXT, baseline_id TEXT, report_digest TEXT, recorded_at TIMESTAMPTZ)
SECURITY DEFINER SET search_path = pg_catalog, public LANGUAGE sql STABLE AS $$
  SELECT c.candidate_key, c.revision_no, c.candidate_id, c.head_sha, c.script_digest,
         v.attempt, v.migration_verdict, v.code_tests_verdict, v.failure_class,
         v.trial_kind, v.baseline_id, v.report_digest, v.recorded_at
    FROM public.migration_candidates c
    LEFT JOIN public.migration_verifications v ON v.candidate_id = c.candidate_id
   WHERE c.run_id = p_run_id
   ORDER BY c.candidate_key, c.revision_no, v.attempt;
$$;

-- ═══ 5.7 授权执行路径强制过闸(claim-path enforcement)═══
-- Policy Gateway 的最终授权执行 = l2_claim_ticket()(APPROVED → EXECUTING 的 CAS)。
-- 这里把 db_release_gate 接进 claim 本身:凡绑定了迁移验证的票据,闸门不 valid 就**不能**进入
-- EXECUTING —— 不写入、不推进状态;抛异常(P0001)让网关按 DB 侧拒绝处理。未绑定迁移验证的票据
-- (普通代码 PR)行为与 m3b_b4 原函数逐字节一致。第 6 个参数 p_target_data_digest 可选:
-- 发布执行方传入"即将被迁移的目标数据"摘要;网关透传 args.release_data_digest(不进 args_hash)。
-- 旧的 5 参签名被替换为带默认值的 6 参签名(既有 5 参调用方无需改动)。
DROP FUNCTION IF EXISTS public.l2_claim_ticket(TEXT,TEXT,TEXT,INTEGER,TEXT);
CREATE OR REPLACE FUNCTION public.l2_claim_ticket(
    p_ticket_id TEXT, p_action TEXT, p_repo TEXT, p_pr_number INTEGER, p_args_hash TEXT,
    p_target_data_digest TEXT DEFAULT NULL)
RETURNS TABLE(execution_id UUID, canonical_payload JSONB, expected_head_sha TEXT, target_branch TEXT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_gate record;
BEGIN
  IF EXISTS (SELECT 1 FROM public.approval_verification_bindings b WHERE b.ticket_id = p_ticket_id) THEN
    SELECT g.valid, g.reason, g.bound_head_sha, g.current_head_sha
      INTO v_gate FROM public.db_release_gate(p_ticket_id, p_target_data_digest) g;
    IF v_gate.valid IS DISTINCT FROM TRUE THEN
      -- fail-closed:票据保持 APPROVED(不推进),不产生 execution_id,不写入
      RAISE EXCEPTION 'DB_RELEASE_GATE_REFUSED: % (bound_head=%, current_head=%)',
        COALESCE(v_gate.reason, 'GATE_ERROR'), v_gate.bound_head_sha, v_gate.current_head_sha
        USING ERRCODE = 'P0001';
    END IF;
  END IF;
  UPDATE public.approvals SET
    status='EXECUTING', execution_id=gen_random_uuid(), executing_at=now()
  WHERE ticket_id=p_ticket_id AND status='APPROVED'
    AND action=p_action AND repo=p_repo AND pr_number=p_pr_number AND args_hash=p_args_hash
    AND expires_at IS NOT NULL AND expires_at > now()
  RETURNING approvals.execution_id, approvals.canonical_payload, approvals.expected_head_sha, approvals.target_branch
  INTO execution_id, canonical_payload, expected_head_sha, target_branch;
  IF NOT FOUND THEN RETURN; END IF;
  RETURN NEXT;
END $$;
-- owner / 权限与 m3b_b4 的 l2_* 约定一致:owner mergepilot_l2_owner,REVOKE PUBLIC,
-- 网关登录角色 policy_gateway_l2 若存在则 GRANT(该角色由部署脚本 m3b-b4-create-roles.sh 创建)。
DO $$ BEGIN
  ALTER FUNCTION public.l2_claim_ticket(TEXT,TEXT,TEXT,INTEGER,TEXT,TEXT) OWNER TO mergepilot_l2_owner;
END $$;
REVOKE ALL ON FUNCTION public.l2_claim_ticket(TEXT,TEXT,TEXT,INTEGER,TEXT,TEXT) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'policy_gateway_l2') THEN
    GRANT EXECUTE ON FUNCTION public.l2_claim_ticket(TEXT,TEXT,TEXT,INTEGER,TEXT,TEXT) TO policy_gateway_l2;
  END IF;
END $$;

-- ═══ 6. 权限(deny-by-not-granted;与 002-console / m4f1 角色矩阵一致)═══
REVOKE ALL ON public.data_baselines, public.migration_candidates, public.migration_verifications,
              public.approval_verification_bindings FROM PUBLIC;
GRANT SELECT ON public.data_baselines, public.migration_candidates, public.migration_verifications,
                public.approval_verification_bindings TO mergepilot_reader;
REVOKE ALL ON FUNCTION public.mv_register_baseline(TEXT,TEXT,TEXT,TEXT,JSONB,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mv_register_candidate(TEXT,TEXT,INTEGER,TEXT,TEXT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mv_record_verification(TEXT,TEXT,INTEGER,TEXT,TEXT,JSONB,TEXT,TEXT,TEXT,JSONB,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.l2_bind_verification(TEXT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.db_release_gate(TEXT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mv_run_status(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.mv_register_baseline(TEXT,TEXT,TEXT,TEXT,JSONB,TEXT) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public.mv_register_candidate(TEXT,TEXT,INTEGER,TEXT,TEXT,TEXT) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public.mv_record_verification(TEXT,TEXT,INTEGER,TEXT,TEXT,JSONB,TEXT,TEXT,TEXT,JSONB,TEXT) TO runtime_owner, skill_runner;
GRANT EXECUTE ON FUNCTION public.l2_bind_verification(TEXT,TEXT) TO mergepilot_approver;
GRANT EXECUTE ON FUNCTION public.db_release_gate(TEXT,TEXT) TO gate_owner, mergepilot_approver, mergepilot_reader, runtime_owner;
GRANT EXECUTE ON FUNCTION public.mv_run_status(TEXT) TO mergepilot_reader, runtime_owner;
-- claim-path enforcement runs inside l2_claim_ticket, whose SECURITY DEFINER owner is the
-- least-privilege role mergepilot_l2_owner: it needs exactly (a) SELECT on the binding table
-- for the EXISTS check and (b) EXECUTE on db_release_gate. Nothing else.
GRANT SELECT ON public.approval_verification_bindings TO mergepilot_l2_owner;
GRANT EXECUTE ON FUNCTION public.db_release_gate(TEXT,TEXT) TO mergepilot_l2_owner;

-- ═══ 7. 自检(fail-closed)═══
DO $$
DECLARE v_tables INT; v_funcs INT; v_claim INT;
BEGIN
  SELECT count(*) INTO v_tables FROM pg_tables WHERE schemaname='public'
   AND tablename IN ('data_baselines','migration_candidates','migration_verifications','approval_verification_bindings');
  SELECT count(*) INTO v_funcs FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'
   AND p.proname IN ('mv_register_baseline','mv_register_candidate','mv_record_verification','l2_bind_verification','db_release_gate','mv_run_status');
  -- exactly one l2_claim_ticket, the gate-enforcing 6-parameter signature
  SELECT count(*) INTO v_claim FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname='public' AND p.proname='l2_claim_ticket'
     AND pg_get_function_identity_arguments(p.oid) LIKE '%p_target_data_digest text%';
  IF v_tables <> 4 OR v_funcs <> 6 OR v_claim <> 1
     OR (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname='l2_claim_ticket') <> 1 THEN
    RAISE EXCEPTION 'm9 self-check failed: tables=% funcs=% claim_overloads_ok=%', v_tables, v_funcs, v_claim;
  END IF;
END $$;

COMMIT;
