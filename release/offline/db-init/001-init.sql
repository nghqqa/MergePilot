-- ===== tools/audit-db/init.sql =====
-- MergePilot 审计库 schema(PolarDB-PG / PostgreSQL 兼容)
-- 把 PR 审修闭环的 agent / task / finding / decision / 审计事件结构化沉淀,可查询、可审计。

-- pgvector:经验沉淀(RAG)向量列(PolarDB-PG 同样支持 vector 扩展)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS agents (
  name        TEXT PRIMARY KEY,
  role        TEXT NOT NULL,
  runtime     TEXT,
  registered_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id     TEXT PRIMARY KEY,          -- e.g. gh-pr1-review / rollback-demo
  repo        TEXT NOT NULL,             -- nghqqa/mergepilot-test
  pr_number   INT,
  pr_url      TEXT,
  branch      TEXT,
  type        TEXT,                       -- review/fix/verify/merge/rollback
  status      TEXT,                       -- pending/done/failed
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS findings (
  id          BIGSERIAL PRIMARY KEY,
  task_id     TEXT REFERENCES tasks(task_id),
  finding_id  TEXT,                       -- F1
  category    TEXT,                       -- security/quality/...
  severity    TEXT,                       -- critical/L1/...
  risk_level  TEXT,                       -- L0/L1/L2
  file        TEXT,
  line        INT,
  description TEXT,
  source      TEXT,                       -- sast-scan / manual
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS decisions (
  id          BIGSERIAL PRIMARY KEY,
  task_id     TEXT REFERENCES tasks(task_id),
  verdict     TEXT,                       -- PASS/FAIL/MERGE/HOLD/REJECT/ROLLBACK
  action      TEXT,                       -- auto-fix/merge/rollback/needs-approval/close-pr
  decided_by  TEXT,                       -- verifier/manager/admin/system
  reason      TEXT,
  pr_url      TEXT,
  commit_sha  TEXT,
  decided_at  TIMESTAMPTZ DEFAULT now()
);

-- 不可变审计事件流:闭环每一步(review/fix/verify/merge/rollback/close_pr)都追加一条
CREATE TABLE IF NOT EXISTS audit_events (
  id          BIGSERIAL PRIMARY KEY,
  task_id     TEXT,
  agent       TEXT,                       -- reviewer/fixer/verifier/manager/system
  action      TEXT,                       -- review/fix/verify/merge/rollback/close_pr/...
  target      TEXT,                       -- repo/branch/file/pr
  detail      TEXT,                       -- 说明或 JSON
  sha         TEXT,                       -- 相关 git commit sha
  via         TEXT,                       -- github-mcp / sast-scan / matrix / pg
  ts          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_events(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts   ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_find_task  ON findings(task_id);
CREATE INDEX IF NOT EXISTS idx_dec_task   ON decisions(task_id);

-- 经验沉淀知识库(RAG):每条 = 一个历史 finding + 其修复,带向量嵌入
-- 维度 384 对应 BAAI/bge-small-en-v1.5(可在 embed 脚本里替换为中文优化模型,迁移=改一处)
CREATE TABLE IF NOT EXISTS knowledge (
  id          BIGSERIAL PRIMARY KEY,
  task_id     TEXT,
  finding_id  TEXT,
  category    TEXT,
  severity    TEXT,
  issue       TEXT,                       -- 问题描述(用于检索 + 喂给 agent)
  fix         TEXT,                       -- 修复方案(召回后直接复用)
  file        TEXT,
  source      TEXT,                       -- sast-scan / manual
  embedding   vector(384),
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_knowledge_vec ON knowledge USING ivfflat (embedding vector_cosine_ops) WITH (lists = 4);
-- ===== tools/audit-db/m3_state.sql =====
-- M3-A: Workflow Controller 状态模型(PolarDB-PG / PostgreSQL 兼容)
-- 可重复执行(idempotent migration)。
-- 权威状态全部在 PG;Controller 内存只做短期缓存。

-- ============================================================
-- 1. task_runs:任务运行级状态
-- ============================================================
CREATE TABLE IF NOT EXISTS task_runs (
  run_id        TEXT PRIMARY KEY,
  room_id       TEXT,
  repo          TEXT,
  pr_number     INT,
  branch        TEXT,
  status        TEXT DEFAULT 'SUBMITTED',
  current_stage TEXT,
  attempt       INT DEFAULT 0,
  verdict       TEXT,
  last_error    TEXT,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS current_stage TEXT;
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS last_error TEXT;

DO $$ BEGIN
  ALTER TABLE task_runs ADD CONSTRAINT chk_task_status CHECK (
    status IN ('SUBMITTED','RUNNING','PASS','FAIL','HOLD','MERGED','ROLLED_BACK')
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- 2. stage_runs:阶段执行级状态
-- ============================================================
CREATE TABLE IF NOT EXISTS stage_runs (
  id            BIGSERIAL PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES task_runs(run_id),
  stage         TEXT NOT NULL,
  agent         TEXT,
  attempt       INT DEFAULT 1,
  status        TEXT DEFAULT 'PENDING_DISPATCH',
  started_at    TIMESTAMPTZ,
  completed_at  TIMESTAMPTZ,
  evidence_path TEXT,
  verdict       TEXT,
  detail        TEXT
);

-- 同一任务+阶段+attempt 只能一条(幂等保证)
CREATE UNIQUE INDEX IF NOT EXISTS uq_stage_attempt
  ON stage_runs(run_id, stage, attempt);

CREATE INDEX IF NOT EXISTS idx_stage_run
  ON stage_runs(run_id, stage);

-- ============================================================
-- 3. stage_events:Matrix 事件去重 + 审计
-- ============================================================
CREATE TABLE IF NOT EXISTS stage_events (
  event_id    TEXT PRIMARY KEY,
  room_id     TEXT NOT NULL,
  run_id      TEXT,
  sender      TEXT,
  event_type  TEXT NOT NULL,
  stage       TEXT,
  body_sha256 TEXT,
  raw_body    TEXT,
  status      TEXT NOT NULL DEFAULT 'RECEIVED',
  error       TEXT,
  received_at TIMESTAMPTZ DEFAULT now(),
  processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_stage_events_run
  ON stage_events(run_id, stage);

-- ============================================================
-- 4. dispatch_outbox:幂等派发(Matrix 发送)
-- ============================================================
CREATE TABLE IF NOT EXISTS dispatch_outbox (
  id              BIGSERIAL PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  run_id          TEXT NOT NULL REFERENCES task_runs(run_id),
  room_id         TEXT NOT NULL,
  target_agent    TEXT NOT NULL,
  target_stage    TEXT NOT NULL,
  attempt         INT NOT NULL,
  body            TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'PENDING',
  matrix_event_id TEXT,
  retry_count     INT NOT NULL DEFAULT 0,
  next_retry_at   TIMESTAMPTZ DEFAULT now(),
  last_error      TEXT,
  created_at      TIMESTAMPTZ DEFAULT now(),
  dispatched_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
  ON dispatch_outbox(status, next_retry_at);

-- ============================================================
-- 5. controller_offsets:Matrix /sync 游标(持久化)
-- ============================================================
CREATE TABLE IF NOT EXISTS controller_offsets (
  consumer_name TEXT PRIMARY KEY,
  sync_token    TEXT,
  updated_at    TIMESTAMPTZ DEFAULT now()
);
-- ===== tools/audit-db/m3b_policy.sql =====
-- m3b_policy.sql — M3-B 最小权限与审批的 schema(幂等)。
-- 复用 audit-pg(mergepilot_audit 库)。
--
-- B1 用: mcp_calls(每次 MCP 调用审计,不可变)
-- B3 强化: 给 gateway 独立 INSERT-only 账号,撤销 UPDATE/DELETE
-- B4 用: approvals(L2 审批票据)+ policy_action_outbox(确定性动作派发,继承 M3-A outbox 模式)
--
-- 备:调用方身份固定 4 角色(reviewer/fixer/verifier/coordinator),不允许自定义。

-- ─── mcp_calls:不可变 MCP 调用审计(B1 起写;B3 加 correlation_id + phase)───
CREATE TABLE IF NOT EXISTS mcp_calls (
    request_id    TEXT PRIMARY KEY,
    correlation_id TEXT,                               -- B3:一次调用的 INTENT/RESULT/ERROR 共享同一 id
    phase         TEXT CHECK (phase IN ('INTENT','RESULT','ERROR')),  -- B3:追加式事件阶段
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    caller_agent  TEXT NOT NULL,                       -- reviewer/fixer/verifier/coordinator/path=..(auth fail 时)
    tool          TEXT NOT NULL,                       -- 工具名或 (list_tools)/(auth)
    decision      TEXT NOT NULL CHECK (decision IN ('ALLOW','DENY','ERROR')),
    reason_code   TEXT,                                -- B1_PERMISSIVE_CALL / BAD_TOKEN / AUDIT_UNAVAILABLE / ...
    policy_version TEXT,                               -- policy.yaml 的 version 字段
    policy_hash   TEXT,                                -- policy.yaml 内容 hash
    ticket_id     TEXT,                                -- L2 动作关联的审批票据(B4)
    args_hash     TEXT,                                -- 入参 sha256 前 16 位(不含敏感原文)
    target_repo   TEXT,
    target_branch TEXT,
    result_status TEXT,                                -- OK / ERROR
    http_status   INTEGER,
    git_sha       TEXT,
    run_id        TEXT,
    error         TEXT
);
-- 迁移:已有库补列(B3)
ALTER TABLE mcp_calls ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE mcp_calls ADD COLUMN IF NOT EXISTS phase TEXT;
CREATE INDEX IF NOT EXISTS idx_mcp_calls_ts      ON mcp_calls(ts);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_caller  ON mcp_calls(caller_agent, ts);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_decision ON mcp_calls(decision, ts);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_corr    ON mcp_calls(correlation_id);  -- B3:按调用聚合 INTENT+RESULT

-- B3.1:幂等补 CHECK 约束。CREATE TABLE IF NOT EXISTS 不修改已存在的表,
-- 迁移只 ADD COLUMN;运行库因此缺 phase CHECK(可插入任意 phase)。此处幂等补齐。
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='mcp_calls_phase_check' AND conrelid='mcp_calls'::regclass) THEN
    ALTER TABLE mcp_calls ADD CONSTRAINT mcp_calls_phase_check
      CHECK (phase IS NULL OR phase IN ('INTENT','RESULT','ERROR'));
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='mcp_calls_decision_check' AND conrelid='mcp_calls'::regclass) THEN
    ALTER TABLE mcp_calls ADD CONSTRAINT mcp_calls_decision_check CHECK (decision IN ('ALLOW','DENY','ERROR'));
  END IF;
END $$;

-- B3:防篡改约束(即便用超管账号也拒绝 UPDATE/DELETE/ALTER 已存在的行)
-- 用触发器拦截 mcp_calls 的 UPDATE/DELETE(INSERT-only)
CREATE OR REPLACE FUNCTION mcp_calls_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'mcp_calls is INSERT-only (immutable audit): % not allowed', TG_OP;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS mcp_calls_no_update ON mcp_calls;
CREATE TRIGGER mcp_calls_no_update BEFORE UPDATE ON mcp_calls
    FOR EACH ROW EXECUTE FUNCTION mcp_calls_immutable();
DROP TRIGGER IF EXISTS mcp_calls_no_delete ON mcp_calls;
CREATE TRIGGER mcp_calls_no_delete BEFORE DELETE ON mcp_calls
    FOR EACH ROW EXECUTE FUNCTION mcp_calls_immutable();

-- ─── approvals:L2 审批票据(B4)───
CREATE TABLE IF NOT EXISTS approvals (
    ticket_id        TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    action           TEXT NOT NULL CHECK (action IN ('merge','revert','close')),
    repo             TEXT NOT NULL,
    pr_number        INTEGER,
    target_branch    TEXT,
    expected_head_sha TEXT,                              -- merge:锁 PR 头,防 TOCTOU
    revert_commit_sha TEXT,                              -- revert:锁要回滚的 commit
    status           TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','APPROVED','EXECUTING','USED','FAILED','UNKNOWN','EXPIRED')),
    approved_by      TEXT,
    approved_at      TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ NOT NULL,
    used_at          TIMESTAMPTZ,
    result_sha       TEXT,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_approvals_run    ON approvals(run_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

-- ─── policy_action_outbox:确定性 L2 动作派发(B4,继承 M3-A Outbox 模式)───
-- 状态转换 + 派发写入同一事务;gateway 异步领取 + 原子 EXECUTING。
CREATE TABLE IF NOT EXISTS policy_action_outbox (
    id              BIGSERIAL PRIMARY KEY,
    ticket_id       TEXT NOT NULL REFERENCES approvals(ticket_id),
    run_id          TEXT NOT NULL,
    action          TEXT NOT NULL,
    repo            TEXT NOT NULL,
    pr_number       INTEGER,
    target_branch   TEXT,
    args_hash       TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,                -- sha256(ticket_id+action+repo+pr),防重复派发
    status          TEXT NOT NULL DEFAULT 'PENDING_DISPATCH'
                    CHECK (status IN ('PENDING_DISPATCH','DISPATCHED','SUCCEEDED','FAILED','UNKNOWN')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_retry_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    result_sha      TEXT,
    matrix_event_id TEXT,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at   TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pao_status ON policy_action_outbox(status, next_retry_at);

-- B4:policy_action_outbox 同样防篡改(已 SUCCEEDED/FAILED 的不可改)
-- (B4 落地时再加约束,避免现在过度限制调试)
-- ===== tools/audit-db/m3b_b4.sql =====
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
-- ===== tools/audit-db/m3b_b4c.sql =====
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
-- ===== tools/audit-db/m3b_b4c1.sql =====
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
-- ===== tools/audit-db/m3b_b4c1_1.sql =====
-- m3b_b4c1_1.sql — B4c.1.1 修正 migration(独立,幂等;不改冻结的 m3b_b4.sql/m3b_b4c.sql)。
--
-- 修复(B4c.1 复审 minor):
--   l2_reject_approved(NULL) 绕过 allowlist —— 旧 `IF p_reason_code NOT IN (...)` 对 NULL 得 NULL(不 RAISE),
--   随后 UPDATE 用 NULL reason 写 error。改为显式拒 NULL(IS NULL OR NOT IN → 22023)。
-- 签名不变(CREATE OR REPLACE),owner/REVOKE/GRANT 收敛(同 B4a 模板)。

CREATE OR REPLACE FUNCTION l2_reject_approved(p_ticket_id TEXT, p_reason_code TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  -- B4c.1.1:显式拒 NULL(旧 NOT IN 对 NULL 得 NULL,绕过 allowlist)
  IF p_reason_code IS NULL OR p_reason_code NOT IN ('CLAIM_MISMATCH','REPO_NOT_ALLOWED','L2_TICKET_REQUIRED','INVALID_ACTION') THEN
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

DO $$ BEGIN
  ALTER FUNCTION l2_reject_approved(text,text) OWNER TO mergepilot_l2_owner;
END $$;
ALTER ROLE mergepilot_l2_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
REVOKE ALL ON FUNCTION l2_reject_approved(TEXT,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION l2_reject_approved(TEXT,TEXT) TO mergepilot;
-- ===== tools/audit-db/m3b_b4d1.sql =====
-- m3b_b4d1.sql — B4d.1 hardening:l2_approve 用 session_user 作 approved_by(强身份认证)。
--
-- 动机(B4d 复审 P1):原 l2_approve(p_ticket_id, p_approved_by) 的 approved_by 由调用方传入,
--   持 approver 密码者可绕过 CLI 直调 SELECT l2_approve('tkt','evil@forged') 冒名。
-- 修复:函数体改用 session_user(认证后的 DB 登录角色)写 approved_by,**忽略** p_approved_by。
--   ⇒ 冒名需受害者的 DB 密码;CLI 仅作便捷入口 + 严格参数校验。按人分配 DB 登录即可得到
--   逐人审批身份(每人一个 LOGIN 角色授予 EXECUTE l2_approve)。
--
-- **不改签名**(仍 l2_approve(text,text),仅给 p_approved_by 加 DEFAULT NULL 便于 1-arg 调用),
--   故 B4a frozen allowlist(m3b_b4.sql 里 l2_approve(text,text) 的 OWNER/REVOKE/GRANT 引用)仍有效。
-- 幂等;OWNER/REVOKE/GRANT 收敛(同 B4a 模板)。

CREATE OR REPLACE FUNCTION l2_approve(p_ticket_id TEXT, p_approved_by TEXT DEFAULT NULL)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_ttl INT;
BEGIN
  -- p_approved_by 参数**保留但忽略**(向后兼容 + 签名不变);approved_by 一律取 session_user。
  -- SECURITY DEFINER 下 current_user=owner, session_user=调用方登录角色 ⇒ 用 session_user。
  SELECT exec_ttl_hours INTO v_ttl FROM public.approvals WHERE ticket_id=p_ticket_id;
  UPDATE public.approvals SET
    status='APPROVED', approved_by=session_user, approved_at=now(),
    expires_at = now() + make_interval(hours => COALESCE(v_ttl,1))
  WHERE ticket_id=p_ticket_id AND status='PENDING' AND approval_expires_at > now();
  RETURN FOUND;
END $$;

-- OWNER 收敛(签名不变;REPLACE 保留原 owner,显式再收敛防漂移)
DO $$ BEGIN
  ALTER FUNCTION l2_approve(text,text) OWNER TO mergepilot_l2_owner;
END $$;
ALTER ROLE mergepilot_l2_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
REVOKE ALL ON FUNCTION l2_approve(TEXT,TEXT) FROM PUBLIC;
-- 生产:每名审批人建独立 LOGIN 角色并 GRANT EXECUTE(此处授予现有 approver + 任何已存在同名角色)
GRANT EXECUTE ON FUNCTION l2_approve(TEXT,TEXT) TO mergepilot_approver;
-- ===== tools/audit-db/m3c_state.sql =====
-- M3-C: 状态感知失败处理 + 回滚(**非破坏性、前向幂等** migration;**fail-fast**)。
-- 架构(决策 2):revert 走 **child run** 模型 —— 原 run 保留原 binding;revert 创建确定性 child task_run,
--   独占 revert binding/ticket/L2 执行链(走正常 review→verify→approve→drain→merge)。
--   run_pr_bindings UNIQUE(run_id) **保留**(revert child run 有独立 run_id,不得作原 run 第二 binding)。
-- 决策 5:不改 task_runs.status CHECK(沿用现有枚举);细粒度状态在 current_stage + rollback_runs.status。
-- 决策 6:rollback_runs UNIQUE(parent_run_id, reverted_merge_sha)。
-- 决策 1/5:回滚清单(changed files)、merge parent、恢复内容一律由 GitHub 权威数据派生
--   (get_commit/get_file_contents),**事件/fixer 提供的内容不作事实来源**。
-- 不动 B4/B5 边界:l2_* 函数 / policy / mcp_calls 触发器一律不改。
--
-- **非破坏性 + 前向幂等**:绝不 DROP TABLE;CREATE TABLE IF NOT EXISTS + ADD COLUMN IF NOT EXISTS +
--   约束/索引一律 DO 块判定已存在;重跑 N 次 = 同结果。fresh DB 建权威 schema;已存在的表只补缺,
--   缺关键列/约束 → 末尾 ASSERT fail-fast(migration 非零退出),绝不静默删数据。

-- ============================================================
-- 0. fail-fast:run_pr_bindings 若有重复 run_id → 拒继续(不静默删)
-- ============================================================
DO $$
DECLARE n_dup INT;
BEGIN
  SELECT count(*) INTO n_dup FROM (
    SELECT run_id FROM run_pr_bindings GROUP BY run_id HAVING count(*) > 1
  ) d;
  IF n_dup > 0 THEN
    RAISE EXCEPTION 'run_pr_bindings 有 % 个重复 run_id,拒绝迁移(需人工清理,不静默删)', n_dup;
  END IF;
END $$;

-- ============================================================
-- 1. task_runs:+verify_attempt / +rollback_id / +parent_run_id(child 回链)
-- ============================================================
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS verify_attempt INT NOT NULL DEFAULT 0;
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS rollback_id TEXT;
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS parent_run_id TEXT;   -- child run → 原 run(revert 链)

-- ============================================================
-- 2. run_pr_bindings:**恢复/确保** UNIQUE(run_id)(决策 2:child run 独占 binding)
--    B4c 原有 uq_run_pr_bindings_run;存在则不动,缺失则建。冗余非唯一索引清掉。
-- ============================================================
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='uq_run_pr_bindings_run' AND tablename='run_pr_bindings') THEN
    CREATE UNIQUE INDEX uq_run_pr_bindings_run ON run_pr_bindings(run_id);
  END IF;
END $$;
DROP INDEX IF EXISTS idx_run_pr_bindings_run;

-- ============================================================
-- 3. rollback_runs:回滚链权威(parent_run + child revert_run + GitHub 派生清单)
--    UNIQUE(parent_run_id, reverted_merge_sha):同一坏提交只建一个回滚流程(决策 6)
--    FK:parent_run_id/revert_run_id → task_runs;CHECK:status/reverify_verdict 枚举;SHA 40hex
--    **非破坏性**:CREATE IF NOT EXISTS 建表;已存在则逐列 ADD COLUMN IF NOT EXISTS 补齐;
--      约束/索引 DO 块判定;缺关键对象 → 末尾 ASSERT fail-fast。
-- ============================================================
CREATE TABLE IF NOT EXISTS rollback_runs (
  rollback_id          TEXT PRIMARY KEY,            -- rb-<UUID>
  parent_run_id        TEXT NOT NULL REFERENCES task_runs(run_id),
  revert_run_id        TEXT REFERENCES task_runs(run_id),   -- child run(建 revert PR 的 run)
  reverted_merge_sha   TEXT NOT NULL,               -- 坏 merge 的 result_sha(40hex)
  repo                 TEXT NOT NULL,
  pr_number            INTEGER NOT NULL,            -- 原始坏 merge 的 PR
  trigger_event_id     TEXT NOT NULL,               -- POST_MERGE_VERIFY_FAILED 的 event_id(溯源)
  status               TEXT NOT NULL DEFAULT 'PENDING',
  fail_reason          TEXT,                        -- CONFLICT / UNSUPPORTED_DIFF / REVERIFY_FAIL / ...
  merge_parent_sha     TEXT,                        -- 坏 merge 的 parent commit(get_commit 权威;还原目标)
  revert_branch        TEXT,                        -- fix/<child_run>-x(revert PR head)
  revert_pr_number     INTEGER,
  revert_ticket_id     TEXT,                        -- child run 的 L2 merge 票
  revert_result_sha    TEXT,                        -- revert merge 的 result_sha
  reverify_verdict     TEXT,                        -- PASS / FAIL
  reverify_event_id    TEXT,
  diff_summary         TEXT,                        -- GitHub 派生 changed-files + 逆向 verdict(JSON)
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 逐列补齐(若表以旧版/部分 schema 存在)
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS parent_run_id TEXT;
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS revert_run_id TEXT;
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS merge_parent_sha TEXT;
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS revert_branch TEXT;
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS revert_pr_number INTEGER;
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS revert_ticket_id TEXT;
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS revert_result_sha TEXT;
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS reverify_verdict TEXT;
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS reverify_event_id TEXT;
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS fail_reason TEXT;
ALTER TABLE rollback_runs ADD COLUMN IF NOT EXISTS diff_summary TEXT;

-- FK/引用约束(既有库补齐;新库 CREATE TABLE 已带,DO 块幂等补缺)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_task_runs_parent_run') THEN
    ALTER TABLE task_runs ADD CONSTRAINT fk_task_runs_parent_run
      FOREIGN KEY (parent_run_id) REFERENCES task_runs(run_id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_rollback_parent_run') THEN
    ALTER TABLE rollback_runs ADD CONSTRAINT fk_rollback_parent_run
      FOREIGN KEY (parent_run_id) REFERENCES task_runs(run_id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_rollback_revert_run') THEN
    ALTER TABLE rollback_runs ADD CONSTRAINT fk_rollback_revert_run
      FOREIGN KEY (revert_run_id) REFERENCES task_runs(run_id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_rollback_revert_ticket') THEN
    ALTER TABLE rollback_runs ADD CONSTRAINT fk_rollback_revert_ticket
      FOREIGN KEY (revert_ticket_id) REFERENCES approvals(ticket_id);
  END IF;
END $$;

-- 约束(幂等:DO 块判定 pg_constraint,已存在不重加)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_rollback_parent_merge') THEN
    ALTER TABLE rollback_runs ADD CONSTRAINT uq_rollback_parent_merge UNIQUE (parent_run_id, reverted_merge_sha);
  END IF;
END $$;
-- chk_rollback_status:DROP IF EXISTS + ADD(权威列表含 AWAITING_APPROVAL;幂等重跑)。
--   旧版约束(无 AWAITING_APPROVAL)会被 DROP 替换;IF NOT EXISTS 形式无法纠正已存在的旧约束,故用 DROP+ADD。
ALTER TABLE rollback_runs DROP CONSTRAINT IF EXISTS chk_rollback_status;
ALTER TABLE rollback_runs ADD CONSTRAINT chk_rollback_status CHECK (
  status IN ('PENDING','CONFLICT','UNSUPPORTED','REVERT_PR_OPEN','AWAITING_APPROVAL','REVERTING',
             'REVERTED','REVERIFYING','RECOVERED','HELD') );
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_rollback_rvsha') THEN
    ALTER TABLE rollback_runs ADD CONSTRAINT chk_rollback_rvsha CHECK (reverted_merge_sha ~ '^[0-9a-f]{40}$');
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_rollback_rvresult') THEN
    ALTER TABLE rollback_runs ADD CONSTRAINT chk_rollback_rvresult CHECK (revert_result_sha IS NULL OR revert_result_sha ~ '^[0-9a-f]{40}$');
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_rollback_verdict') THEN
    ALTER TABLE rollback_runs ADD CONSTRAINT chk_rollback_verdict CHECK (reverify_verdict IS NULL OR reverify_verdict IN ('PASS','FAIL'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_rollback_active
  ON rollback_runs(status)
  WHERE status IN ('PENDING','REVERT_PR_OPEN','REVERTING','REVERIFYING');
CREATE INDEX IF NOT EXISTS idx_rollback_parent ON rollback_runs(parent_run_id);

-- ============================================================
-- 4. task_runs.rollback_id 软指向 rollback_runs(不加 FK 硬约束,避免循环依赖锁)
--    task_runs.parent_run_id 已加(上方)。
-- ============================================================

-- ============================================================
-- 5. fail-fast 自检(幂等重跑安全;任一断言失败 → RAISE EXCEPTION → migration 非零退出)
-- ============================================================
DO $$
BEGIN
  ASSERT (SELECT count(*) FROM information_schema.columns
          WHERE table_name='task_runs' AND column_name IN ('verify_attempt','rollback_id','parent_run_id')) = 3,
    'task_runs M3-C 列缺失';
  ASSERT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='uq_run_pr_bindings_run' AND tablename='run_pr_bindings'),
    'uq_run_pr_bindings_run 必须存在(UNIQUE run_id)';
  ASSERT EXISTS (SELECT 1 FROM pg_tables WHERE tablename='rollback_runs'),
    'rollback_runs 必须存在';
  ASSERT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_task_runs_parent_run'),
    'task_runs.parent_run_id FK 必须存在';
  ASSERT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_rollback_parent_run'),
    'rollback_runs.parent_run_id FK 必须存在';
  ASSERT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_rollback_revert_run'),
    'rollback_runs.revert_run_id FK 必须存在';
  ASSERT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_rollback_revert_ticket'),
    'rollback_runs.revert_ticket_id FK 必须存在';
  ASSERT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_rollback_parent_merge'),
    'rollback_runs UNIQUE(parent_run_id,reverted_merge_sha) 必须存在';
  ASSERT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_rollback_status'),
    'rollback_runs chk_rollback_status 必须存在';
  ASSERT (SELECT count(*) FROM information_schema.columns
          WHERE table_name='rollback_runs' AND column_name IN ('parent_run_id','revert_run_id','merge_parent_sha','diff_summary')) = 4,
    'rollback_runs M3-C 列缺失(parent_run_id/revert_run_id/merge_parent_sha/diff_summary)';
END $$;
-- 不加 EXCEPTION WHEN OTHERS THEN NOTICE:ASSERT 失败直接 RAISE EXCEPTION。
-- ===== tools/audit-db/m4f1_hotfix_1.sql =====
-- ════════════════════════════════════════════════════════════════════════════
-- m4f1_hotfix_1.sql -- M4-F post-release P1 hotfix (additive, idempotent).
--
-- Root cause: public.skill_job_outbox / snapshot_job_outbox each carry TWO
-- unique constraints (job_id and idempotency_key). The producer SD APIs used
-- `INSERT ... ON CONFLICT (job_id) DO NOTHING`, which only absorbs a conflict
-- on the job_id index. Under a real two-connection race on the same
-- deterministic job, PostgreSQL can detect the idempotency_key unique violation
-- first and leak SQLSTATE 23505 (constraint=*_idempotency_key_key) to the
-- caller instead of triggering the ON CONFLICT (job_id) path.
--
-- Fix: switch both enqueue functions to an untargeted `ON CONFLICT DO NOTHING`
-- so ANY unique-index contention is swallowed uniformly, then rely on the
-- existing post-INSERT `SELECT ... FOR UPDATE` re-read to reconcile: identical
-- payload returns the same deterministic job_id; a payload/dependency mismatch
-- is surfaced as a clean P0001 (never 23505).
--
-- This migration is CREATE OR REPLACE only: no signature/owner/ACL/search_path
-- change, no DROP, no data mutation. Applies cleanly on top of m4f1_state.sql
-- (fresh) AND on top of the released m4f1_state.sql at tag m4f-agentteams-demo-
-- closed (upgrade path). Idempotent: applying twice succeeds and re-converges.
-- ════════════════════════════════════════════════════════════════════════════

-- runtime_owner is the SD-API function owner; ensure it exists (no-op if the
-- base chain already created it).
DO $role$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='runtime_owner') THEN
    CREATE ROLE runtime_owner NOLOGIN;
  END IF;
END $role$;
ALTER ROLE runtime_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;

-- ── enqueue_snapshot_job: untargeted ON CONFLICT DO NOTHING ──────────────────
CREATE OR REPLACE FUNCTION public.enqueue_snapshot_job(p_run_id text, p_revision_binding_id text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_sds text; v_job text; v_existing record;
BEGIN
  SELECT skill_data_state INTO v_sds FROM public.task_runs WHERE run_id=p_run_id FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'enqueue_snapshot_job: run not found' USING ERRCODE='P0001'; END IF;
  IF v_sds <> 'ACTIVE' THEN RAISE EXCEPTION 'enqueue_snapshot_job: run not ACTIVE' USING ERRCODE='P0001'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.revision_bindings
                 WHERE binding_id=p_revision_binding_id AND run_id=p_run_id) THEN
    RAISE EXCEPTION 'enqueue_snapshot_job: binding not found or does not belong to run' USING ERRCODE='P0001';
  END IF;

  v_job := 'snapjob-'||p_run_id;
  INSERT INTO public.snapshot_job_outbox(
      job_id,run_id,revision_binding_id,idempotency_key,status,attempts,next_retry_at)
    VALUES (v_job,p_run_id,p_revision_binding_id,v_job,'PENDING',0,now())
    ON CONFLICT DO NOTHING;

  SELECT job_id,run_id,revision_binding_id,idempotency_key INTO v_existing
    FROM public.snapshot_job_outbox WHERE job_id=v_job FOR UPDATE;
  IF NOT FOUND OR v_existing.run_id IS DISTINCT FROM p_run_id
     OR v_existing.revision_binding_id IS DISTINCT FROM p_revision_binding_id
     OR v_existing.idempotency_key IS DISTINCT FROM v_job THEN
    RAISE EXCEPTION 'enqueue_snapshot_job: idempotency conflict' USING ERRCODE='P0001';
  END IF;
  RETURN v_job;
END; $$;

-- ── enqueue_skill_job: untargeted ON CONFLICT DO NOTHING ─────────────────────
CREATE OR REPLACE FUNCTION public.enqueue_skill_job(
  p_run_id text, p_snapshot_id text, p_trace_id text, p_skill_name text, p_skill_version text,
  p_attempt int, p_request_envelope_ref text, p_depends_on_job_ids text[] DEFAULT '{}') RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE
  v_job text; v_env record; v_req jsonb; v_keys text[]; v_dep text; v_cycle int;
  v_sds text; v_existing record; v_deps_existing text[]; v_deps_input text[];
  v_d_in text; v_expected_req_id text; v_inserted int;
BEGIN
  SELECT skill_data_state INTO v_sds FROM public.task_runs WHERE run_id=p_run_id FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'enqueue_skill_job: run not found' USING ERRCODE='P0001'; END IF;
  IF v_sds <> 'ACTIVE' THEN RAISE EXCEPTION 'enqueue_skill_job: run not ACTIVE' USING ERRCODE='P0001'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.skill_version_registry
                 WHERE skill_name=p_skill_name AND skill_version=p_skill_version) THEN
    RAISE EXCEPTION 'enqueue_skill_job: unregistered skill version' USING ERRCODE='P0001';
  END IF;

  SELECT content_type,content_json INTO v_env FROM public.envelope_store
    WHERE content_digest=p_request_envelope_ref;
  IF NOT FOUND OR v_env.content_type <> 'application/vnd.mergepilot.skill-request.v1+json' THEN
    RAISE EXCEPTION 'enqueue_skill_job: request envelope wrong type' USING ERRCODE='P0001';
  END IF;
  v_req := v_env.content_json;
  IF v_req->>'contract_version' IS DISTINCT FROM '1' THEN
    RAISE EXCEPTION 'enqueue_skill_job: contract_version not 1' USING ERRCODE='P0001';
  END IF;
  IF v_req->>'trace_id' IS DISTINCT FROM p_trace_id THEN
    RAISE EXCEPTION 'enqueue_skill_job: trace_id mismatch' USING ERRCODE='P0001';
  END IF;
  IF NOT (v_req ? 'input') THEN
    RAISE EXCEPTION 'enqueue_skill_job: input missing' USING ERRCODE='P0001';
  END IF;
  SELECT array_agg(k) INTO v_keys FROM jsonb_object_keys(v_req) AS k;
  IF EXISTS (SELECT 1 FROM unnest(COALESCE(v_keys,ARRAY[]::text[])) AS k
             WHERE k NOT IN ('contract_version','request_id','trace_id','input','timeout_ms')) THEN
    RAISE EXCEPTION 'enqueue_skill_job: unknown top-level key' USING ERRCODE='P0001';
  END IF;

  v_d_in := encode(public.digest(
    convert_to(public.canonical_json(v_req->'input'),'UTF8'),'sha256'),'hex');
  v_expected_req_id := 'req-'||left(encode(public.digest(
    public._canon_str(p_trace_id)||public._canon_str(p_run_id)||
    public._canon_str(p_skill_name)||public._canon_str(p_attempt::text)||
    public._canon_str(v_d_in),'sha256'),'hex'),24);
  IF v_req->>'request_id' IS DISTINCT FROM v_expected_req_id THEN
    RAISE EXCEPTION 'enqueue_skill_job: request_id mismatch' USING ERRCODE='P0001';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.snapshot_manifest_items
      WHERE snapshot_id=p_snapshot_id AND skill_name=p_skill_name
        AND skill_version=p_skill_version AND request_envelope_ref=p_request_envelope_ref) THEN
    RAISE EXCEPTION 'request not in snapshot manifest' USING ERRCODE='P0001';
  END IF;

  v_job := 'sj-'||left(encode(public.digest(
    public._canon_str(p_run_id)||public._canon_str(COALESCE(p_snapshot_id,''))||
    public._canon_str(p_skill_name)||public._canon_str(p_skill_version)||
    public._canon_str(p_attempt::text)||public._canon_str(p_request_envelope_ref),
    'sha256'),'hex'),32);
  SELECT COALESCE(array_agg(DISTINCT d ORDER BY d),ARRAY[]::text[]) INTO v_deps_input
    FROM unnest(COALESCE(p_depends_on_job_ids,ARRAY[]::text[])) AS u(d);

  INSERT INTO public.skill_job_outbox(
      job_id,run_id,snapshot_id,trace_id,skill_name,skill_version,attempt,
      request_envelope_ref,idempotency_key,status,attempts,next_retry_at)
    VALUES (v_job,p_run_id,p_snapshot_id,p_trace_id,p_skill_name,p_skill_version,p_attempt,
      p_request_envelope_ref,v_job,'PENDING',0,now())
    ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS v_inserted = ROW_COUNT;

  SELECT job_id,run_id,snapshot_id,trace_id,skill_name,skill_version,attempt,
         request_envelope_ref,idempotency_key INTO v_existing
    FROM public.skill_job_outbox WHERE job_id=v_job FOR UPDATE;
  IF NOT FOUND OR v_existing.run_id IS DISTINCT FROM p_run_id
     OR v_existing.snapshot_id IS DISTINCT FROM p_snapshot_id
     OR v_existing.trace_id IS DISTINCT FROM p_trace_id
     OR v_existing.skill_name IS DISTINCT FROM p_skill_name
     OR v_existing.skill_version IS DISTINCT FROM p_skill_version
     OR v_existing.attempt IS DISTINCT FROM p_attempt
     OR v_existing.request_envelope_ref IS DISTINCT FROM p_request_envelope_ref
     OR v_existing.idempotency_key IS DISTINCT FROM v_job THEN
    RAISE EXCEPTION 'enqueue_skill_job: idempotency conflict' USING ERRCODE='P0001';
  END IF;

  SELECT COALESCE(array_agg(depends_on_job_id ORDER BY depends_on_job_id),ARRAY[]::text[])
    INTO v_deps_existing FROM public.skill_job_dependencies WHERE job_id=v_job;
  IF v_inserted = 0 THEN
    IF v_deps_existing IS DISTINCT FROM v_deps_input THEN
      RAISE EXCEPTION 'enqueue_skill_job: dependency set conflict' USING ERRCODE='P0001';
    END IF;
    RETURN v_job;
  END IF;

  FOREACH v_dep IN ARRAY v_deps_input LOOP
    IF v_dep = v_job THEN
      RAISE EXCEPTION 'enqueue_skill_job: self-dependency' USING ERRCODE='P0001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.skill_job_outbox
                   WHERE job_id=v_dep AND run_id=p_run_id
                     AND snapshot_id IS NOT DISTINCT FROM p_snapshot_id) THEN
      RAISE EXCEPTION 'enqueue_skill_job: dependency not found or wrong run/snapshot' USING ERRCODE='P0001';
    END IF;
    INSERT INTO public.skill_job_dependencies(job_id,depends_on_job_id)
      VALUES (v_job,v_dep);
  END LOOP;

  WITH RECURSIVE dependency_closure(ancestor) AS (
    SELECT depends_on_job_id FROM public.skill_job_dependencies WHERE job_id=v_job
    UNION
    SELECT d.depends_on_job_id FROM public.skill_job_dependencies AS d
      JOIN dependency_closure AS c ON d.job_id=c.ancestor
  )
  SELECT count(*) INTO v_cycle FROM dependency_closure WHERE ancestor=v_job;
  IF v_cycle > 0 THEN
    RAISE EXCEPTION 'enqueue_skill_job: dependency cycle' USING ERRCODE='P0001';
  END IF;
  RETURN v_job;
END; $$;

-- ── re-assert owner / REVOKE PUBLIC / GRANT EXECUTE (idempotent) ─────────────
ALTER FUNCTION public.enqueue_snapshot_job(text,text) OWNER TO runtime_owner;
ALTER FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public.enqueue_snapshot_job(text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.enqueue_snapshot_job(text,text) TO mergepilot;
GRANT EXECUTE ON FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) TO mergepilot;

-- ── catalog self-check: SD/owner/search_path/PUBLIC-EXECUTE + ON CONFLICT ──
DO $$
DECLARE
  v_bad int; v_pub int; v_src text;
BEGIN
  -- (1) SECURITY DEFINER + owner=runtime_owner + search_path=pg_catalog
  SELECT count(*) INTO v_bad FROM pg_proc p
    JOIN pg_namespace n ON n.oid=p.pronamespace
    LEFT JOIN pg_roles r ON r.oid=p.proowner
    WHERE n.nspname='public'
      AND p.proname IN ('enqueue_snapshot_job','enqueue_skill_job')
      AND (NOT p.prosecdef
           OR r.rolname IS DISTINCT FROM 'runtime_owner'
           OR p.proconfig IS NULL
           OR array_position(p.proconfig,'search_path=pg_catalog') IS NULL);
  IF v_bad > 0 THEN RAISE EXCEPTION 'hotfix1 catalog: SD/owner/search_path check failed (rows=%)', v_bad; END IF;

  -- (2) PUBLIC must NOT retain EXECUTE (NULL proacl == default == PUBLIC EXECUTE => fail)
  SELECT count(*) INTO v_pub FROM pg_proc p
    JOIN pg_namespace n ON n.oid=p.pronamespace
    LEFT JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a ON true
    WHERE n.nspname='public'
      AND p.proname IN ('enqueue_snapshot_job','enqueue_skill_job')
      AND a.grantee = 0 AND a.privilege_type = 'EXECUTE';
  IF v_pub > 0 THEN RAISE EXCEPTION 'hotfix1 catalog: PUBLIC still holds EXECUTE (rows=%)', v_pub; END IF;

  -- (3) function bodies: no 'ON CONFLICT (job_id)' and yes 'ON CONFLICT DO NOTHING'
  SELECT prosrc INTO v_src FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.proname='enqueue_snapshot_job';
  IF position('ON CONFLICT (job_id)' IN v_src) > 0 THEN RAISE EXCEPTION 'hotfix1: enqueue_snapshot_job still has targeted ON CONFLICT (job_id)'; END IF;
  IF position('ON CONFLICT DO NOTHING' IN v_src) = 0 THEN RAISE EXCEPTION 'hotfix1: enqueue_snapshot_job missing untargeted ON CONFLICT DO NOTHING'; END IF;

  SELECT prosrc INTO v_src FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.proname='enqueue_skill_job';
  IF position('ON CONFLICT (job_id)' IN v_src) > 0 THEN RAISE EXCEPTION 'hotfix1: enqueue_skill_job still has targeted ON CONFLICT (job_id)'; END IF;
  IF position('ON CONFLICT DO NOTHING' IN v_src) = 0 THEN RAISE EXCEPTION 'hotfix1: enqueue_skill_job missing untargeted ON CONFLICT DO NOTHING'; END IF;

  RAISE NOTICE 'm4f1_hotfix_1 catalog self-check PASS';
END $$;
-- ===== tools/audit-db/m4f1_state.sql =====
-- m4f1_state.sql — M4-F1 数据库契约 v2.8 实现。
-- 包含 roles/ACL、tables/task_runs extensions、constraints/composite FK、12-digest registry seed、
-- immutable/writer/revision-guard 触发器、MergePilot JCS Profile v1、完整 producer/worker/
-- completion/purge SECURITY DEFINER API，以及按函数名的 catalog 自检。
-- 单一事务(BEGIN/COMMIT):catalog 自检失败→整事务回滚,无半成品。幂等、非破坏。
-- 依赖:m3_state + m3b_policy(mcp_calls) + m3b_b4(run_pr_bindings) + m3b_b4c/c1/c1_1/d1 + m3c_state。

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ═══ 1. task_runs 扩展(trace_id/active_snapshot_id/skill_data_state) ═══
ALTER TABLE public.task_runs ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE public.task_runs ADD COLUMN IF NOT EXISTS active_snapshot_id TEXT;
ALTER TABLE public.task_runs ADD COLUMN IF NOT EXISTS skill_data_state TEXT NOT NULL DEFAULT 'ACTIVE';
UPDATE public.task_runs SET skill_data_state='ACTIVE' WHERE skill_data_state IS NULL;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_skill_data_state' AND conrelid='public.task_runs'::regclass) THEN
    ALTER TABLE public.task_runs ADD CONSTRAINT chk_skill_data_state CHECK (skill_data_state IN ('ACTIVE','PURGING','PURGED'));
  END IF;
END $$;

-- ═══ 2. envelope_store(内容寻址,不可变) ═══
CREATE TABLE IF NOT EXISTS public.envelope_store (
  content_digest TEXT PRIMARY KEY CHECK (content_digest ~ '^[0-9a-f]{64}$'),
  content_bytes  BYTEA NOT NULL,
  content_json   JSONB,
  content_type   TEXT NOT NULL CHECK (content_type IN (
     'application/vnd.mergepilot.skill-request.v1+json',
     'application/vnd.mergepilot.skill-response.v1+json',
     'application/vnd.mergepilot.snapshot-manifest.v1+json')),
  size_bytes     INTEGER NOT NULL CHECK (size_bytes >= 0 AND size_bytes <= 1048576),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT envelope_size_matches CHECK (size_bytes = octet_length(content_bytes))
);

-- ═══ 3. run_snapshots(不可变;repo/pr 派生;composite-FK 目标) ═══
CREATE TABLE IF NOT EXISTS public.run_snapshots (
  snapshot_id    TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES public.task_runs(run_id),
  repo           TEXT NOT NULL,
  pr_number      INTEGER NOT NULL,
  base_sha       TEXT NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
  head_sha       TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
  manifest_digest TEXT NOT NULL REFERENCES public.envelope_store(content_digest),
  incomplete     BOOLEAN NOT NULL DEFAULT false,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_snapshots_run_digest ON public.run_snapshots(run_id, manifest_digest);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_snapshots_run_snap  ON public.run_snapshots(run_id, snapshot_id);   -- composite-FK target

-- ═══ 4. skill_version_registry(不可变;12-digest seed 在 §10) ═══
CREATE TABLE IF NOT EXISTS public.skill_version_registry (
  skill_name            TEXT NOT NULL CHECK (skill_name IN ('diff-parse','risk-classify','sast-scan','test-runner','case-retrieval','pr-lifecycle')),
  skill_version         TEXT NOT NULL CHECK (skill_version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  output_schema_digest  TEXT NOT NULL CHECK (output_schema_digest ~ '^[0-9a-f]{64}$'),
  request_schema_digest TEXT NOT NULL CHECK (request_schema_digest ~ '^[0-9a-f]{64}$'),
  registered_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (skill_name, skill_version),
  CONSTRAINT uq_registry_output UNIQUE (output_schema_digest),
  CONSTRAINT uq_registry_request UNIQUE (request_schema_digest),
  CONSTRAINT uq_registry_skillver_out UNIQUE (skill_name, skill_version, output_schema_digest)
);

-- ═══ 5. revision_bindings(一 run 一 revision;不可变;provenance) ═══
CREATE TABLE IF NOT EXISTS public.revision_bindings (
  binding_id            TEXT PRIMARY KEY,
  run_id                TEXT NOT NULL UNIQUE REFERENCES public.task_runs(run_id),
  repo                  TEXT NOT NULL,
  pr_number             INTEGER NOT NULL,
  base_sha              TEXT NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
  head_sha              TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
  source_call_id        TEXT NOT NULL REFERENCES public.mcp_calls(request_id),
  source_evidence_digest TEXT NOT NULL CHECK (source_evidence_digest ~ '^[0-9a-f]{64}$'),
  recorded_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ═══ 5.5 purge_requests(单一模型;target_state 仅 PURGED;无 FAILED) ═══
CREATE TABLE IF NOT EXISTS public.purge_requests (
  purge_id      TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES public.task_runs(run_id),
  target_state  TEXT NOT NULL CHECK (target_state = 'PURGED'),
  status        TEXT NOT NULL DEFAULT 'REQUESTED' CHECK (status IN ('REQUESTED','PURGING','PURGED')),
  requested_by  TEXT NOT NULL,
  requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  purging_at    TIMESTAMPTZ, completed_at TIMESTAMPTZ,
  error         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_purge_requests_active_run
  ON public.purge_requests(run_id) WHERE status IN ('REQUESTED','PURGING');

-- ═══ 6. snapshot_job_outbox(claim_id CAS + revision_binding_id) ═══
CREATE TABLE IF NOT EXISTS public.snapshot_job_outbox (
  job_id           TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES public.task_runs(run_id),
  snapshot_id      TEXT,
  revision_binding_id TEXT NOT NULL REFERENCES public.revision_bindings(binding_id),
  idempotency_key  TEXT NOT NULL UNIQUE,
  status           TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','LEASED','SUCCEEDED','FAILED')),
  claim_id         UUID,
  leased_by        TEXT, lease_expires_at TIMESTAMPTZ, last_heartbeat_at TIMESTAMPTZ,
  attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts     INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
  next_retry_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  error            TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at       TIMESTAMPTZ, completed_at TIMESTAMPTZ
);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='snapshot_job_outbox_run_snapshot_fkey' AND conrelid='public.snapshot_job_outbox'::regclass) THEN
    ALTER TABLE public.snapshot_job_outbox ADD CONSTRAINT snapshot_job_outbox_run_snapshot_fkey
      FOREIGN KEY (run_id, snapshot_id) REFERENCES public.run_snapshots(run_id, snapshot_id);
  END IF;
END $$;

-- ═══ 7. skill_job_outbox(claim_id CAS;registry 复合 FK;循环 FK result_invocation_id 后补) ═══
CREATE TABLE IF NOT EXISTS public.skill_job_outbox (
  job_id            TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES public.task_runs(run_id),
  snapshot_id       TEXT,
  trace_id          TEXT NOT NULL,
  skill_name        TEXT NOT NULL,
  skill_version     TEXT NOT NULL,
  attempt           INTEGER NOT NULL CHECK (attempt >= 1),
  request_envelope_ref TEXT NOT NULL REFERENCES public.envelope_store(content_digest),
  idempotency_key   TEXT NOT NULL UNIQUE,
  status            TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','LEASED','SUCCEEDED','FAILED')),
  claim_id          UUID,
  leased_by         TEXT, lease_expires_at TIMESTAMPTZ, last_heartbeat_at TIMESTAMPTZ,
  attempts          INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts      INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
  next_retry_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  result_invocation_id TEXT,
  error             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at        TIMESTAMPTZ, completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_job_outbox_run_job ON public.skill_job_outbox(run_id, job_id);  -- composite-FK target
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_job_outbox_run_snapshot_fkey' AND conrelid='public.skill_job_outbox'::regclass) THEN
    ALTER TABLE public.skill_job_outbox ADD CONSTRAINT skill_job_outbox_run_snapshot_fkey
      FOREIGN KEY (run_id, snapshot_id) REFERENCES public.run_snapshots(run_id, snapshot_id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_job_outbox_registry_fkey' AND conrelid='public.skill_job_outbox'::regclass) THEN
    ALTER TABLE public.skill_job_outbox ADD CONSTRAINT skill_job_outbox_registry_fkey
      FOREIGN KEY (skill_name, skill_version) REFERENCES public.skill_version_registry(skill_name, skill_version);
  END IF;
END $$;

-- ═══ 7.5 skill_job_dependencies(规范化 DAG;双 FK CASCADE;禁自依赖) ═══
CREATE TABLE IF NOT EXISTS public.skill_job_dependencies (
  job_id            TEXT NOT NULL,
  depends_on_job_id TEXT NOT NULL,
  PRIMARY KEY (job_id, depends_on_job_id),
  CONSTRAINT skill_job_dependencies_job_fkey    FOREIGN KEY (job_id)            REFERENCES public.skill_job_outbox(job_id) ON DELETE CASCADE,
  CONSTRAINT skill_job_dependencies_dep_fkey    FOREIGN KEY (depends_on_job_id) REFERENCES public.skill_job_outbox(job_id) ON DELETE CASCADE,
  CONSTRAINT skill_job_dependencies_no_self     CHECK (job_id <> depends_on_job_id)
);
CREATE INDEX IF NOT EXISTS idx_skill_job_dependencies_dep ON public.skill_job_dependencies(depends_on_job_id);

-- ═══ 8. skill_invocations(不可变;status-aware;registry 复合 FK) ═══
CREATE TABLE IF NOT EXISTS public.skill_invocations (
  invocation_id    TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES public.task_runs(run_id),
  snapshot_id      TEXT,
  job_id           TEXT,
  trace_id         TEXT NOT NULL,
  skill_name       TEXT NOT NULL, skill_version TEXT NOT NULL,
  attempt          INTEGER NOT NULL CHECK (attempt >= 1),
  request_id       TEXT NOT NULL,
  contract_version TEXT NOT NULL DEFAULT '1' CHECK (contract_version = '1'),
  status           TEXT NOT NULL CHECK (status IN ('OK','PARTIAL','ERROR')),
  error_code       TEXT CHECK (error_code IS NULL OR error_code IN ('INVALID_INPUT','SCHEMA_VERSION_UNSUPPORTED','TIMEOUT','DENIED','DEPENDENCY_UNAVAILABLE','OUTPUT_TOO_LARGE','INTERNAL_ERROR')),
  verdict          TEXT CHECK (verdict IS NULL OR verdict IN ('PASS','FAIL','TIMEOUT','ERROR')),
  input_digest     TEXT NOT NULL REFERENCES public.envelope_store(content_digest),
  output_digest    TEXT REFERENCES public.envelope_store(content_digest),
  snapshot_manifest_digest TEXT REFERENCES public.envelope_store(content_digest),
  expected_output_schema_digest TEXT NOT NULL,
  output_schema_validated BOOLEAN NOT NULL,
  duration_ms      INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
  started_at       TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
  idempotency_key  TEXT NOT NULL UNIQUE,
  CONSTRAINT sinv_status_err_ok   CHECK (NOT (status IN ('OK','PARTIAL') AND error_code IS NOT NULL)),
  CONSTRAINT sinv_status_err_req  CHECK (NOT (status = 'ERROR' AND error_code IS NULL)),
  CONSTRAINT sinv_status_validated CHECK (NOT (status IN ('OK','PARTIAL') AND output_schema_validated = false)),
  CONSTRAINT sinv_validated_verdict CHECK (NOT (output_schema_validated = false AND verdict IS NOT NULL))
);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_invocations_run_snapshot_fkey' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT skill_invocations_run_snapshot_fkey
      FOREIGN KEY (run_id, snapshot_id) REFERENCES public.run_snapshots(run_id, snapshot_id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_invocations_run_job_fkey' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT skill_invocations_run_job_fkey
      FOREIGN KEY (run_id, job_id) REFERENCES public.skill_job_outbox(run_id, job_id) ON DELETE CASCADE;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_invocations_registry_fkey' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT skill_invocations_registry_fkey
      FOREIGN KEY (skill_name, skill_version, expected_output_schema_digest)
      REFERENCES public.skill_version_registry(skill_name, skill_version, output_schema_digest);
  END IF;
END $$;
-- 循环 FK:result_invocation_id(skill_job_outbox → skill_invocations)ON DELETE SET NULL DEFERRABLE
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_job_outbox_result_invocation_fkey' AND conrelid='public.skill_job_outbox'::regclass) THEN
    ALTER TABLE public.skill_job_outbox ADD CONSTRAINT skill_job_outbox_result_invocation_fkey
      FOREIGN KEY (result_invocation_id) REFERENCES public.skill_invocations(invocation_id)
      ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$;

-- status-aware CK 幂等补齐(既有库兼容;新库 CREATE TABLE 已含)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sinv_status_validated' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT sinv_status_validated CHECK (NOT (status IN ('OK','PARTIAL') AND output_schema_validated = false));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sinv_validated_verdict' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT sinv_validated_verdict CHECK (NOT (output_schema_validated = false AND verdict IS NOT NULL));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sinv_status_err_req' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT sinv_status_err_req CHECK (NOT (status = 'ERROR' AND error_code IS NULL));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sinv_status_err_ok' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT sinv_status_err_ok CHECK (NOT (status IN ('OK','PARTIAL') AND error_code IS NOT NULL));
  END IF;
END $$;

-- ═══ 9. snapshot_manifest_items(规范化;每 (snapshot,skill,version) 至多一项) ═══
CREATE TABLE IF NOT EXISTS public.snapshot_manifest_items (
  snapshot_id          TEXT NOT NULL REFERENCES public.run_snapshots(snapshot_id) ON DELETE CASCADE,
  ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
  skill_name           TEXT NOT NULL, skill_version TEXT NOT NULL,
  request_envelope_ref TEXT NOT NULL REFERENCES public.envelope_store(content_digest),
  PRIMARY KEY (snapshot_id, ordinal),
  CONSTRAINT smi_uniq_skillver UNIQUE (snapshot_id, skill_name, skill_version)
);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='smi_registry_fkey' AND conrelid='public.snapshot_manifest_items'::regclass) THEN
    ALTER TABLE public.snapshot_manifest_items ADD CONSTRAINT smi_registry_fkey
      FOREIGN KEY (skill_name, skill_version) REFERENCES public.skill_version_registry(skill_name, skill_version);
  END IF;
END $$;

-- ═══ 10. task_runs.active_snapshot_id 复合 FK(替换任何旧简单 FK) ═══
DO $$ BEGIN
  ALTER TABLE public.task_runs DROP CONSTRAINT IF EXISTS task_runs_active_snapshot_id_fkey;
  ALTER TABLE public.task_runs DROP CONSTRAINT IF EXISTS task_runs_active_snapshot_run_fkey;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='task_runs_active_snapshot_run_fkey' AND conrelid='public.task_runs'::regclass) THEN
    ALTER TABLE public.task_runs ADD CONSTRAINT task_runs_active_snapshot_run_fkey
      FOREIGN KEY (run_id, active_snapshot_id) REFERENCES public.run_snapshots(run_id, snapshot_id);
  END IF;
END $$;

-- ═══ 11. 12-digest registry seed(实测 sha256 原始文件字节) ═══
INSERT INTO public.skill_version_registry(skill_name, skill_version, request_schema_digest, output_schema_digest) VALUES
 ('diff-parse','1.0.0','89d628502dd726d6dfa1df4f52687bd51a1cea75d81e680a5025852f3b5b7285','e6e0eb2077645007de8115a0be697b27954e34b9a000e9bc7c6de03c27fd355b'),
 ('risk-classify','1.0.0','45ca36e3a5c6ff8146e13d7935918240279f1ffbc28872c8b1c04c81a3111371','b4d8e0519916cc21ea5286a677a94de53af2cb968073c1b06cf8b4d6ccbda09a'),
 ('sast-scan','1.0.0','8d008630393b59e77ed66669c2b5d6a45591dbbed5c3bc5554289035c5813598','fda15df57b9713bf76f95ff0668a8c76a8f7f68cabb40348232d571614e497e1'),
 ('test-runner','1.0.0','a90f67f1c19243582402d8e8b590f9a104a937637442be29a3d980848b9ecda9','461c5f026e01a4641acc0821220f6720361402ee2c3fc802421a6a11c41772d9'),
 ('case-retrieval','1.0.0','549526ab5aa410b67754a52ba7fcd826b2cc7813189eac0f929c5b53e666c3d3','4366b3e76796756158197b10c77c135b7d6443c9262ad9a5be5c03a60f662b57'),
 ('pr-lifecycle','1.0.0','7157df189df14d7128c3fe9f40e749050ed8251f206a7f5a57ca31da9859c424','ee27d6b587ca9b82d9da189ae98ca4a58437110ebe3ff75348506355c075dc1c')
ON CONFLICT (skill_name, skill_version) DO NOTHING;

-- ═══ 12. 角色 + 幂等收敛 + 双向 membership 清理 ═══
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='gate_owner')      THEN CREATE ROLE gate_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='envelope_maint')  THEN CREATE ROLE envelope_maint NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='runtime_owner')   THEN CREATE ROLE runtime_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='skill_runner')    THEN CREATE ROLE skill_runner LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='snapshot_worker') THEN CREATE ROLE snapshot_worker LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='purge_operator')  THEN CREATE ROLE purge_operator LOGIN; END IF;
END $$;
ALTER ROLE gate_owner      NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE envelope_maint  NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE runtime_owner   NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE skill_runner    NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE snapshot_worker NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE purge_operator  NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
-- 双向 membership 清理(gate_owner/envelope_maint/runtime_owner 三个 owner 不应有 membership)
DO $$ DECLARE m record;
BEGIN
  FOR m IN SELECT DISTINCT roleid FROM pg_auth_members
           WHERE member IN ('gate_owner'::regrole,'envelope_maint'::regrole,'runtime_owner'::regrole)
  LOOP EXECUTE format('REVOKE %s FROM gate_owner, envelope_maint, runtime_owner', m.roleid::regrole::text); END LOOP;
  FOR m IN SELECT member FROM pg_auth_members WHERE roleid IN ('gate_owner'::regrole,'envelope_maint'::regrole,'runtime_owner'::regrole)
  LOOP EXECUTE format('REVOKE gate_owner FROM %s', m.member::regrole::text);
       EXECUTE format('REVOKE envelope_maint FROM %s', m.member::regrole::text);
       EXECUTE format('REVOKE runtime_owner FROM %s', m.member::regrole::text); END LOOP;
END $$;

-- ═══ 13. 触发器函数(保留 writer-gate/enforce_transition;新增 immutable_except_purge/guard/digest_check) ═══
CREATE OR REPLACE FUNCTION public._enforce_transition() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.skill_data_state IS DISTINCT FROM OLD.skill_data_state THEN
    IF current_user <> 'envelope_maint' THEN RAISE EXCEPTION 'by %', current_user; END IF;
    IF NOT ((OLD.skill_data_state='ACTIVE'  AND NEW.skill_data_state='PURGING')
         OR (OLD.skill_data_state='PURGING' AND NEW.skill_data_state='PURGED')) THEN
      RAISE EXCEPTION 'invalid';
    END IF;
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public._immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% immutable: % not allowed', TG_TABLE_NAME, TG_OP;
END; $$;

CREATE OR REPLACE FUNCTION public._immutable_except_purge() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION '% immutable: UPDATE not allowed', TG_TABLE_NAME;
  END IF;
  -- RI ON DELETE CASCADE executes nested triggers as the relation owner.
  -- Direct deletes remain envelope_maint-only; only a nested trigger cascade is allowed.
  IF TG_OP = 'DELETE' AND current_user <> 'envelope_maint' AND pg_trigger_depth() <= 1 THEN
    RAISE EXCEPTION '% immutable: DELETE only via purge (envelope_maint)', TG_TABLE_NAME;
  END IF;
  RETURN OLD;
END; $$;

CREATE OR REPLACE FUNCTION public._envelope_digest_check() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF encode(public.digest(NEW.content_bytes,'sha256'),'hex') <> NEW.content_digest THEN
    RAISE EXCEPTION 'envelope digest mismatch';
  END IF;
  IF NEW.size_bytes <> octet_length(NEW.content_bytes) THEN
    RAISE EXCEPTION 'envelope size mismatch';
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public._writer_gate() RETURNS TRIGGER
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v TEXT;
BEGIN
  SELECT t.skill_data_state INTO v FROM public.task_runs t WHERE t.run_id = NEW.run_id FOR KEY SHARE;
  IF v IS NULL THEN RAISE EXCEPTION 'nf'; END IF;
  IF v <> 'ACTIVE' THEN RAISE EXCEPTION 'is %', v; END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public._writer_gate_snapshot_job() RETURNS TRIGGER
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v TEXT; sr TEXT;
BEGIN
  SELECT t.skill_data_state INTO v FROM public.task_runs t WHERE t.run_id = NEW.run_id FOR KEY SHARE;
  IF v IS NULL THEN RAISE EXCEPTION 'nf'; END IF;
  IF v <> 'ACTIVE' THEN RAISE EXCEPTION 'is %', v; END IF;
  IF NEW.snapshot_id IS NOT NULL THEN
    SELECT rs.run_id INTO sr FROM public.run_snapshots rs WHERE rs.snapshot_id = NEW.snapshot_id;
    IF sr IS NULL THEN RAISE EXCEPTION 'snap nf'; END IF;
    IF sr <> NEW.run_id THEN RAISE EXCEPTION 'snap mismatch'; END IF;
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public._writer_gate_rollback() RETURNS TRIGGER
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE e INT; a INT := 0; rr RECORD;
BEGIN
  IF TG_OP='UPDATE' AND (OLD.parent_run_id IS DISTINCT FROM NEW.parent_run_id
     OR OLD.revert_run_id IS DISTINCT FROM NEW.revert_run_id) THEN RAISE EXCEPTION 'kc'; END IF;
  IF NEW.revert_run_id IS NOT NULL AND NEW.revert_run_id <> NEW.parent_run_id THEN e := 2; ELSE e := 1; END IF;
  a := 0;
  FOR rr IN SELECT t.run_id, t.skill_data_state FROM public.task_runs t
             WHERE t.run_id IN (NEW.parent_run_id, NEW.revert_run_id) ORDER BY t.run_id FOR KEY SHARE OF t
  LOOP
    a := a + 1;
    IF rr.skill_data_state IS NULL THEN RAISE EXCEPTION 'nl'; END IF;
    IF rr.skill_data_state <> 'ACTIVE' THEN RAISE EXCEPTION '% is %', rr.run_id, rr.skill_data_state; END IF;
  END LOOP;
  IF a <> e THEN RAISE EXCEPTION 'exp%,fnd%', e, a; END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public._guard_bound_run_pr_revision() RETURNS trigger
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE b boolean;
BEGIN
  SELECT EXISTS (SELECT 1 FROM public.revision_bindings WHERE run_id = NEW.run_id) INTO b;
  IF b AND ( (NEW.repo IS DISTINCT FROM OLD.repo)
          OR (NEW.pr_number IS DISTINCT FROM OLD.pr_number)
          OR (NEW.head_sha IS DISTINCT FROM OLD.head_sha) ) THEN
    RAISE EXCEPTION 'revision already bound';
  END IF;
  RETURN NEW;
END; $$;

ALTER FUNCTION public._writer_gate() OWNER TO gate_owner;
ALTER FUNCTION public._writer_gate_snapshot_job() OWNER TO gate_owner;
ALTER FUNCTION public._writer_gate_rollback() OWNER TO gate_owner;
ALTER FUNCTION public._guard_bound_run_pr_revision() OWNER TO gate_owner;
REVOKE ALL ON FUNCTION public._writer_gate() FROM PUBLIC;
REVOKE ALL ON FUNCTION public._writer_gate_snapshot_job() FROM PUBLIC;
REVOKE ALL ON FUNCTION public._writer_gate_rollback() FROM PUBLIC;
REVOKE ALL ON FUNCTION public._guard_bound_run_pr_revision() FROM PUBLIC;

-- ═══ 14. 触发器 ═══
DROP TRIGGER IF EXISTS trg_transition ON public.task_runs;
CREATE TRIGGER trg_transition BEFORE UPDATE OF skill_data_state ON public.task_runs
  FOR EACH ROW EXECUTE FUNCTION public._enforce_transition();

DROP TRIGGER IF EXISTS trg_envelope_immutable ON public.envelope_store;
CREATE TRIGGER trg_envelope_immutable BEFORE UPDATE OR DELETE ON public.envelope_store
  FOR EACH ROW EXECUTE FUNCTION public._immutable_except_purge();
DROP TRIGGER IF EXISTS trg_envelope_digest_check ON public.envelope_store;
CREATE TRIGGER trg_envelope_digest_check BEFORE INSERT ON public.envelope_store
  FOR EACH ROW EXECUTE FUNCTION public._envelope_digest_check();

DROP TRIGGER IF EXISTS trg_run_snapshots_immutable ON public.run_snapshots;
CREATE TRIGGER trg_run_snapshots_immutable BEFORE UPDATE OR DELETE ON public.run_snapshots
  FOR EACH ROW EXECUTE FUNCTION public._immutable_except_purge();

DROP TRIGGER IF EXISTS trg_skill_invocations_immutable ON public.skill_invocations;
CREATE TRIGGER trg_skill_invocations_immutable BEFORE UPDATE OR DELETE ON public.skill_invocations
  FOR EACH ROW EXECUTE FUNCTION public._immutable_except_purge();

DROP TRIGGER IF EXISTS trg_revision_bindings_immutable ON public.revision_bindings;
CREATE TRIGGER trg_revision_bindings_immutable BEFORE UPDATE OR DELETE ON public.revision_bindings
  FOR EACH ROW EXECUTE FUNCTION public._immutable();

DROP TRIGGER IF EXISTS trg_skill_version_registry_immutable ON public.skill_version_registry;
CREATE TRIGGER trg_skill_version_registry_immutable BEFORE UPDATE OR DELETE ON public.skill_version_registry
  FOR EACH ROW EXECUTE FUNCTION public._immutable();

DROP TRIGGER IF EXISTS trg_run_pr_bindings_revision_guard ON public.run_pr_bindings;
CREATE TRIGGER trg_run_pr_bindings_revision_guard BEFORE UPDATE OF repo, pr_number, head_sha ON public.run_pr_bindings
  FOR EACH ROW EXECUTE FUNCTION public._guard_bound_run_pr_revision();

DO $$ DECLARE t text[]; mapping text[] := ARRAY[
  ['run_snapshots','trg_gate_run_snapshots','_writer_gate'],
  ['snapshot_job_outbox','trg_gate_snapshot_job_outbox','_writer_gate_snapshot_job'],
  ['skill_job_outbox','trg_gate_skill_job_outbox','_writer_gate'],
  ['skill_invocations','trg_gate_skill_invocations','_writer_gate'],
  ['dispatch_outbox','trg_gate_dispatch_outbox','_writer_gate'],
  ['approvals','trg_gate_approvals','_writer_gate'],
  ['policy_action_outbox','trg_gate_policy_action_outbox','_writer_gate'],
  ['stage_runs','trg_gate_stage_runs','_writer_gate'],
  ['rollback_runs','trg_gate_rollback_runs','_writer_gate_rollback']
];
BEGIN
  FOREACH t SLICE 1 IN ARRAY mapping LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON public.%I', t[2], t[1]);
    EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.%I()', t[2], t[1], t[3]);
  END LOOP;
END $$;

-- ═══ 14.5 SD API 函数(Stage 2.1B-1:生产者侧 put_envelope/bind_revision/enqueue_snapshot_job/enqueue_skill_job) ═══

-- canon_str 辅助:长度前缀, NULL→'-1:'
CREATE OR REPLACE FUNCTION public._canon_str(v text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN v IS NULL THEN '-1:' ELSE octet_length(v)::text || ':' || v END $$;
ALTER FUNCTION public._canon_str(text) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public._canon_str(text) FROM PUBLIC;

-- _utf16_sortkey:UTF-16 code unit big-endian sort key(BMP→1 codeunit,non-BMP→surrogate pair)
CREATE OR REPLACE FUNCTION public._utf16_sortkey(p_text text) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE v_result bytea := '\x'::bytea; v_cp int; v_hi int; v_lo int;
BEGIN
  IF p_text IS NULL THEN RETURN '\x'::bytea; END IF;
  FOR i IN 1..char_length(p_text) LOOP
    v_cp := ascii(substr(p_text, i, 1));
    IF v_cp <= 65535 THEN
      v_result := v_result || decode(lpad(to_hex(v_cp), 4, '0'), 'hex');
    ELSE
      v_hi := 55296 + ((v_cp - 65536) >> 10);
      v_lo := 56320 + ((v_cp - 65536) & 1023);
      v_result := v_result || decode(lpad(to_hex(v_hi), 4, '0'), 'hex');
      v_result := v_result || decode(lpad(to_hex(v_lo), 4, '0'), 'hex');
    END IF;
  END LOOP;
  RETURN v_result;
END; $$;
ALTER FUNCTION public._utf16_sortkey(text) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public._utf16_sortkey(text) FROM PUBLIC;

-- _jcs_number:ECMAScript/JCS NumberToString(shortest round-trip, -0→0, lowercase e, no leading zeros)
-- Takes float8 (already validated finite + range by caller); does NOT use float8::text directly as oracle.
-- Parses float8::text for shortest mantissa, then applies ECMA-262 §7.1.12.1 formatting rules.
CREATE OR REPLACE FUNCTION public._jcs_number(p_float float8) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  v_raw text; v_sign text := ''; v_mant text; v_epos int; v_exp int;
  v_dotpos int; v_intpart text; v_fracpart text; v_digits text;
  v_k int; v_n int;
BEGIN
  IF p_float = 0 THEN RETURN '0'; END IF;
  v_raw := p_float::text;
  IF v_raw IN ('Infinity','-Infinity','NaN') THEN
    RAISE EXCEPTION '_jcs_number: non-finite' USING ERRCODE='P0001'; END IF;
  IF left(v_raw,1) = '-' THEN v_sign := '-'; v_raw := substr(v_raw,2); END IF;
  v_epos := position('e' IN lower(v_raw));
  IF v_epos = 0 THEN
    RETURN v_sign || v_raw;
  END IF;
  v_mant := substr(v_raw, 1, v_epos - 1);
  v_exp := substr(v_raw, v_epos + 1)::int;
  v_dotpos := position('.' IN v_mant);
  IF v_dotpos > 0 THEN
    v_intpart := substr(v_mant, 1, v_dotpos - 1);
    v_fracpart := substr(v_mant, v_dotpos + 1);
  ELSE
    v_intpart := v_mant; v_fracpart := '';
  END IF;
  v_digits := v_intpart || v_fracpart;
  v_k := char_length(v_digits);
  v_n := char_length(v_intpart) + v_exp;
  IF v_k <= v_n AND v_n <= 21 THEN
    RETURN v_sign || v_digits || repeat('0', v_n - v_k);
  ELSIF 0 < v_n AND v_n <= 21 THEN
    RETURN v_sign || substr(v_digits,1,v_n) || '.' || substr(v_digits,v_n+1);
  ELSIF -6 < v_n AND v_n <= 0 THEN
    RETURN v_sign || '0.' || repeat('0', -v_n) || v_digits;
  ELSE
    IF v_k = 1 THEN
      RETURN v_sign || v_digits || 'e' || CASE WHEN v_n-1 >= 0 THEN '+' ELSE '-' END || abs(v_n-1)::text;
    ELSE
      RETURN v_sign || substr(v_digits,1,1) || '.' || substr(v_digits,2) || 'e'
        || CASE WHEN v_n-1 >= 0 THEN '+' ELSE '-' END || abs(v_n-1)::text;
    END IF;
  END IF;
END; $$;
ALTER FUNCTION public._jcs_number(float8) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public._jcs_number(float8) FROM PUBLIC;

-- _jcs_escape:JCS §3.2.2.2 string serialization(short escapes for \b\t\n\f\r, \u00xx for other <0x20)
CREATE OR REPLACE FUNCTION public._jcs_escape(p_str text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE v_out text := '"'; v_char int;
BEGIN
  IF p_str IS NULL THEN RETURN '""'; END IF;
  FOR i IN 1..char_length(p_str) LOOP
    v_char := ascii(substr(p_str, i, 1));
    IF v_char = 34 THEN v_out := v_out || chr(92) || '"';        -- \"
    ELSIF v_char = 92 THEN v_out := v_out || chr(92) || chr(92); -- \\
    ELSIF v_char = 8 THEN v_out := v_out || chr(92) || 'b';      -- \b
    ELSIF v_char = 9 THEN v_out := v_out || chr(92) || 't';      -- \t
    ELSIF v_char = 10 THEN v_out := v_out || chr(92) || 'n';     -- \n
    ELSIF v_char = 12 THEN v_out := v_out || chr(92) || 'f';     -- \f
    ELSIF v_char = 13 THEN v_out := v_out || chr(92) || 'r';     -- \r
    ELSIF v_char < 32 THEN v_out := v_out || chr(92) || 'u' || lpad(to_hex(v_char), 4, '0');
    ELSE v_out := v_out || chr(v_char); END IF;
  END LOOP;
  RETURN v_out || '"';
END; $$;
ALTER FUNCTION public._jcs_escape(text) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public._jcs_escape(text) FROM PUBLIC;

-- canonical_json:MergePilot JCS Profile v1(UTF-16 key sort, JCS escape, ECMAScript number, |int|≤2^53 reject)
CREATE OR REPLACE FUNCTION public.canonical_json(p_input jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  v_type text := jsonb_typeof(p_input); v_result text; v_num numeric; v_float float8;
  v_elem jsonb; v_keys text[]; i int;
BEGIN
  IF v_type = 'null' THEN RETURN 'null';
  ELSIF v_type = 'boolean' THEN RETURN p_input::text;
  ELSIF v_type = 'number' THEN
    v_num := p_input::numeric;
    BEGIN v_float := v_num::float8; EXCEPTION WHEN OTHERS THEN
      RAISE EXCEPTION 'canonical_json: number not float8-representable' USING ERRCODE='P0001'; END;
    IF v_float::text IN ('Infinity','-Infinity','NaN') THEN
      RAISE EXCEPTION 'canonical_json: non-finite number' USING ERRCODE='P0001'; END IF;
    IF v_num = trunc(v_num) AND abs(v_num) > 9007199254740992 THEN
      RAISE EXCEPTION 'canonical_json: integer exceeds safe range (|n|>2^53)' USING ERRCODE='P0001'; END IF;
    RETURN public._jcs_number(v_float);
  ELSIF v_type = 'string' THEN
    RETURN public._jcs_escape(p_input #>> '{}');
  ELSIF v_type = 'array' THEN
    v_result := '[';
    FOR v_elem IN SELECT * FROM jsonb_array_elements(p_input) LOOP
      v_result := v_result || CASE WHEN v_result = '[' THEN '' ELSE ',' END || public.canonical_json(v_elem);
    END LOOP;
    RETURN v_result || ']';
  ELSIF v_type = 'object' THEN
    SELECT array_agg(key ORDER BY public._utf16_sortkey(key)) INTO v_keys
      FROM jsonb_object_keys(p_input) AS key;
    v_result := '{';
    IF v_keys IS NOT NULL THEN
      FOR i IN 1..array_length(v_keys, 1) LOOP
        v_result := v_result || CASE WHEN v_result = '{' THEN '' ELSE ',' END
          || public._jcs_escape(v_keys[i]) || ':' || public.canonical_json(p_input -> v_keys[i]);
      END LOOP;
    END IF;
    RETURN v_result || '}';
  ELSE RAISE EXCEPTION 'canonical_json: unknown json type %', v_type USING ERRCODE='P0001';
  END IF;
END; $$;
ALTER FUNCTION public.canonical_json(jsonb) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public.canonical_json(jsonb) FROM PUBLIC;

-- 1. put_envelope:内容寻址存储,幂等,不可覆盖,MergePilot JCS Profile v1 pre-jsonb ingress
-- _check_json_ingress:recursive pre-jsonb validation(dup keys, U+0000, surrogates, profile numbers)
CREATE OR REPLACE FUNCTION public._check_json_ingress(p_json json) RETURNS void
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  v_type text := json_typeof(p_json); v_key text; v_val json;
  v_str text; v_char int; v_num numeric; v_float float8; v_dups int;
BEGIN
  IF v_type = 'object' THEN
    SELECT count(*) INTO v_dups FROM (
      SELECT key, count(*) AS c FROM json_object_keys(p_json) AS key GROUP BY key HAVING count(*) > 1) d;
    IF v_dups > 0 THEN RAISE EXCEPTION 'duplicate object key' USING ERRCODE = 'P0001'; END IF;
    FOR v_key, v_val IN SELECT key, value FROM json_each(p_json) LOOP
      PERFORM public._check_json_ingress(v_val);
    END LOOP;
  ELSIF v_type = 'array' THEN
    FOR v_val IN SELECT value FROM json_array_elements(p_json) LOOP
      PERFORM public._check_json_ingress(v_val);
    END LOOP;
  ELSIF v_type = 'string' THEN
    -- U+0000 cannot reach the recursion: raw 0x00 is rejected by convert_from
    -- (Phase 1a, 22007) and the six-byte U+0000 escape is rejected by the json
    -- parser (Phase 1b, 22P05). chr(0) itself raises 54000 in PG16, so a NUL
    -- check on an already-parsed json value is neither possible nor needed;
    -- only check for lone surrogates (U+D800..U+DFFF) here.
    v_str := p_json #>> '{}';
    FOR i IN 1..char_length(v_str) LOOP
      v_char := ascii(substr(v_str, i, 1));
      IF v_char >= 55296 AND v_char <= 57343 THEN
        RAISE EXCEPTION 'invalid Unicode scalar' USING ERRCODE = 'P0001';
      END IF;
    END LOOP;
  ELSIF v_type = 'number' THEN
    BEGIN
      v_num := (p_json #>> '{}')::numeric;
      v_float := v_num::float8;
      IF v_float::text IN ('Infinity','-Infinity','NaN') THEN
        RAISE EXCEPTION 'number outside MergePilot JCS profile' USING ERRCODE = 'P0001'; END IF;
      IF v_num = trunc(v_num) AND abs(v_num) > 9007199254740992 THEN
        RAISE EXCEPTION 'number outside MergePilot JCS profile' USING ERRCODE = 'P0001'; END IF;
    EXCEPTION WHEN OTHERS THEN
      RAISE EXCEPTION 'number outside MergePilot JCS profile' USING ERRCODE = 'P0001';
    END;
  END IF;
END; $$;
ALTER FUNCTION public._check_json_ingress(json) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public._check_json_ingress(json) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.put_envelope(p_content_bytes bytea, p_content_type text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_digest text; v_size int; v_text text; v_json json; v_jsonb jsonb; v_err text; v_detail text; v_existing record;
BEGIN
  IF p_content_type NOT IN ('application/vnd.mergepilot.skill-request.v1+json',
    'application/vnd.mergepilot.skill-response.v1+json',
    'application/vnd.mergepilot.snapshot-manifest.v1+json') THEN
    RAISE EXCEPTION 'put_envelope: invalid content_type' USING ERRCODE = 'P0001'; END IF;
  v_size := octet_length(p_content_bytes);
  IF v_size IS NULL OR v_size <= 0 OR v_size > 1048576 THEN
    RAISE EXCEPTION 'put_envelope: size out of range (%)', v_size USING ERRCODE = 'P0001'; END IF;
 -- Phase 1a: reject a raw NUL byte before PostgreSQL text conversion.
 IF position(decode('00','hex') IN p_content_bytes) > 0 THEN
   RAISE EXCEPTION 'put_envelope: U+0000 not allowed' USING ERRCODE = 'P0001';
 END IF;
 -- Phase 1a: strict UTF-8
  BEGIN v_text := convert_from(p_content_bytes, 'UTF8');
  EXCEPTION WHEN OTHERS THEN RAISE EXCEPTION 'put_envelope: invalid UTF-8 or JSON' USING ERRCODE = 'P0001'; END;
  -- Phase 1b: parse without classifying raw substrings. A literal "\\u0000"
  -- is valid JSON data; only parser/materialization errors are mapped below.
  BEGIN
    v_json := v_text::json;
  EXCEPTION
    WHEN SQLSTATE '22P05' THEN
      RAISE EXCEPTION 'put_envelope: U+0000 not allowed' USING ERRCODE = 'P0001';
    WHEN invalid_text_representation THEN
      GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT, v_detail = PG_EXCEPTION_DETAIL;
      IF lower(coalesce(v_err,'') || ' ' || coalesce(v_detail,'')) LIKE '%surrogate%' THEN
        RAISE EXCEPTION 'put_envelope: invalid Unicode scalar' USING ERRCODE = 'P0001';
      END IF;
      RAISE EXCEPTION 'put_envelope: invalid UTF-8 or JSON' USING ERRCODE = 'P0001';
    WHEN OTHERS THEN
      GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT, v_detail = PG_EXCEPTION_DETAIL;
      IF lower(coalesce(v_err,'') || ' ' || coalesce(v_detail,'')) LIKE '%surrogate%' THEN
        RAISE EXCEPTION 'put_envelope: invalid Unicode scalar' USING ERRCODE = 'P0001';
      END IF;
      RAISE EXCEPTION 'put_envelope: invalid UTF-8 or JSON' USING ERRCODE = 'P0001';
  END;
  -- Phase 1c: semantic profile validation and jsonb materialization.
  BEGIN
    PERFORM public._check_json_ingress(v_json);
    v_jsonb := v_json::jsonb;
  EXCEPTION
    WHEN SQLSTATE 'P0001' THEN
      RAISE;
    WHEN SQLSTATE '22P05' THEN
      RAISE EXCEPTION 'put_envelope: U+0000 not allowed' USING ERRCODE = 'P0001';
    WHEN invalid_text_representation THEN
      GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT, v_detail = PG_EXCEPTION_DETAIL;
      IF lower(coalesce(v_err,'') || ' ' || coalesce(v_detail,'')) LIKE '%surrogate%' THEN
        RAISE EXCEPTION 'put_envelope: invalid Unicode scalar' USING ERRCODE = 'P0001';
      END IF;
      RAISE EXCEPTION 'put_envelope: invalid UTF-8 or JSON' USING ERRCODE = 'P0001';
    WHEN OTHERS THEN
      GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT, v_detail = PG_EXCEPTION_DETAIL;
      IF lower(coalesce(v_err,'') || ' ' || coalesce(v_detail,'')) LIKE '%surrogate%' THEN
        RAISE EXCEPTION 'put_envelope: invalid Unicode scalar' USING ERRCODE = 'P0001';
      END IF;
      RAISE EXCEPTION 'put_envelope: invalid UTF-8 or JSON' USING ERRCODE = 'P0001';
  END;
  -- Compute digest on raw bytes
  v_digest := encode(public.digest(p_content_bytes,'sha256'),'hex');
  -- INSERT with immutable content_type reconcile
  INSERT INTO public.envelope_store(content_digest,content_bytes,content_json,content_type,size_bytes)
    VALUES (v_digest,p_content_bytes,v_jsonb,p_content_type,v_size)
    ON CONFLICT (content_digest) DO NOTHING;
  SELECT content_bytes,content_json,content_type,size_bytes INTO v_existing
    FROM public.envelope_store WHERE content_digest=v_digest;
  IF NOT FOUND OR v_existing.content_bytes IS DISTINCT FROM p_content_bytes
     OR v_existing.content_json IS DISTINCT FROM v_jsonb
     OR v_existing.content_type IS DISTINCT FROM p_content_type
     OR v_existing.size_bytes IS DISTINCT FROM v_size THEN
    RAISE EXCEPTION 'put_envelope: immutable payload conflict for existing digest' USING ERRCODE='P0001';
  END IF;
  RETURN v_digest;
END; $$;

-- 2. bind_revision:单 revision authority,不泄漏 23505
CREATE OR REPLACE FUNCTION public.bind_revision(
  p_run_id text, p_repo text, p_pr_number int, p_head_sha text, p_base_sha text,
  p_source_call_id text, p_source_evidence_digest text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_sds text; v_rpb record; v_mc record; v_recomputed text; v_bid text; v_existing record; v_ins text;
BEGIN
  -- 1. lock task_runs FOR UPDATE, require ACTIVE
  SELECT skill_data_state INTO v_sds FROM public.task_runs WHERE run_id=p_run_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'bind_revision: run not found' USING ERRCODE='P0001'; END IF;
  IF v_sds <> 'ACTIVE' THEN RAISE EXCEPTION 'skill data is not ACTIVE' USING ERRCODE='P0001'; END IF;
  -- 2. run_pr_bindings FOR SHARE, validate repo/pr/head
  SELECT repo,pr_number,head_sha INTO v_rpb FROM public.run_pr_bindings WHERE run_id=p_run_id FOR SHARE;
  IF NOT FOUND OR v_rpb.repo IS DISTINCT FROM p_repo OR v_rpb.pr_number IS DISTINCT FROM p_pr_number
     OR v_rpb.head_sha IS DISTINCT FROM p_head_sha THEN
    RAISE EXCEPTION 'revision binding mismatch' USING ERRCODE='P0001'; END IF;
  -- 3. mcp_calls RESULT provenance
  SELECT phase,decision,result_status,run_id,target_repo,git_sha,correlation_id,tool INTO v_mc
    FROM public.mcp_calls WHERE request_id=p_source_call_id;
  IF NOT FOUND OR v_mc.phase <> 'RESULT' OR v_mc.decision <> 'ALLOW' OR v_mc.result_status <> 'OK'
     OR v_mc.run_id IS DISTINCT FROM p_run_id OR v_mc.target_repo IS DISTINCT FROM p_repo
     OR v_mc.git_sha IS NULL OR v_mc.git_sha <> p_base_sha THEN
    RAISE EXCEPTION 'revision provenance mismatch' USING ERRCODE='P0001'; END IF;
  -- 4. recompute evidence digest
  v_recomputed := encode(public.digest(
    public._canon_str(p_source_call_id)||public._canon_str(v_mc.correlation_id)||public._canon_str(v_mc.tool)||
    public._canon_str(v_mc.target_repo)||public._canon_str(v_mc.run_id)||public._canon_str(v_mc.git_sha)||
    public._canon_str(v_mc.result_status),'sha256'),'hex');
  IF v_recomputed <> p_source_evidence_digest THEN
    RAISE EXCEPTION 'revision evidence digest mismatch' USING ERRCODE='P0001'; END IF;
  -- 5. compute binding_id (H_32)
  v_bid := 'rev-'||left(encode(public.digest(
    public._canon_str(p_run_id)||public._canon_str(p_repo)||public._canon_str(p_pr_number::text)||
    public._canon_str(p_base_sha)||public._canon_str(p_head_sha)||public._canon_str(p_source_call_id)||
    public._canon_str(p_source_evidence_digest),'sha256'),'hex'),32);
  -- pre-reconcile by run_id
  SELECT binding_id,repo,pr_number,base_sha,head_sha,source_call_id,source_evidence_digest
    INTO v_existing FROM public.revision_bindings WHERE run_id=p_run_id;
  IF FOUND THEN
    IF v_existing.binding_id=v_bid AND v_existing.repo=p_repo AND v_existing.pr_number=p_pr_number
       AND v_existing.base_sha=p_base_sha AND v_existing.head_sha=p_head_sha
       AND v_existing.source_call_id=p_source_call_id AND v_existing.source_evidence_digest=p_source_evidence_digest THEN
      RETURN v_existing.binding_id; -- idempotent replay
    ELSE
      RAISE EXCEPTION 'revision binding conflict' USING ERRCODE='P0001'; END IF;
  END IF;
  -- not found → INSERT ON CONFLICT DO NOTHING RETURNING
  BEGIN
    INSERT INTO public.revision_bindings(binding_id,run_id,repo,pr_number,base_sha,head_sha,source_call_id,source_evidence_digest)
      VALUES (v_bid,p_run_id,p_repo,p_pr_number,p_base_sha,p_head_sha,p_source_call_id,p_source_evidence_digest)
      ON CONFLICT DO NOTHING RETURNING binding_id INTO v_ins;
    IF v_ins IS NOT NULL THEN RETURN v_bid; END IF;
  EXCEPTION WHEN SQLSTATE '23505' THEN NULL; END; -- never propagate 23505
  -- conflict after INSERT (concurrent or H_32 collision): dual-key re-read
  SELECT binding_id,repo,pr_number,base_sha,head_sha,source_call_id,source_evidence_digest
    INTO v_existing FROM public.revision_bindings WHERE run_id=p_run_id OR binding_id=v_bid;
  IF v_existing.binding_id=v_bid AND v_existing.repo=p_repo AND v_existing.pr_number=p_pr_number
     AND v_existing.base_sha=p_base_sha AND v_existing.head_sha=p_head_sha
     AND v_existing.source_call_id=p_source_call_id AND v_existing.source_evidence_digest=p_source_evidence_digest THEN
    RETURN v_bid; -- concurrent idempotent
  ELSE
    RAISE EXCEPTION 'revision binding conflict' USING ERRCODE='P0001'; END IF;
END; $$;

-- Final producer definitions: full immutable reconcile and row-lock serialization.
CREATE OR REPLACE FUNCTION public.enqueue_snapshot_job(p_run_id text, p_revision_binding_id text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_sds text; v_job text; v_existing record;
BEGIN
  SELECT skill_data_state INTO v_sds FROM public.task_runs WHERE run_id=p_run_id FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'enqueue_snapshot_job: run not found' USING ERRCODE='P0001'; END IF;
  IF v_sds <> 'ACTIVE' THEN RAISE EXCEPTION 'enqueue_snapshot_job: run not ACTIVE' USING ERRCODE='P0001'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.revision_bindings
                 WHERE binding_id=p_revision_binding_id AND run_id=p_run_id) THEN
    RAISE EXCEPTION 'enqueue_snapshot_job: binding not found or does not belong to run' USING ERRCODE='P0001';
  END IF;

  v_job := 'snapjob-'||p_run_id;
  INSERT INTO public.snapshot_job_outbox(
      job_id,run_id,revision_binding_id,idempotency_key,status,attempts,next_retry_at)
    VALUES (v_job,p_run_id,p_revision_binding_id,v_job,'PENDING',0,now())
    ON CONFLICT DO NOTHING;

  SELECT job_id,run_id,revision_binding_id,idempotency_key INTO v_existing
    FROM public.snapshot_job_outbox WHERE job_id=v_job FOR UPDATE;
  IF NOT FOUND OR v_existing.run_id IS DISTINCT FROM p_run_id
     OR v_existing.revision_binding_id IS DISTINCT FROM p_revision_binding_id
     OR v_existing.idempotency_key IS DISTINCT FROM v_job THEN
    RAISE EXCEPTION 'enqueue_snapshot_job: idempotency conflict' USING ERRCODE='P0001';
  END IF;
  RETURN v_job;
END; $$;

CREATE OR REPLACE FUNCTION public.enqueue_skill_job(
  p_run_id text, p_snapshot_id text, p_trace_id text, p_skill_name text, p_skill_version text,
  p_attempt int, p_request_envelope_ref text, p_depends_on_job_ids text[] DEFAULT '{}') RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE
  v_job text; v_env record; v_req jsonb; v_keys text[]; v_dep text; v_cycle int;
  v_sds text; v_existing record; v_deps_existing text[]; v_deps_input text[];
  v_d_in text; v_expected_req_id text; v_inserted int;
BEGIN
  SELECT skill_data_state INTO v_sds FROM public.task_runs WHERE run_id=p_run_id FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'enqueue_skill_job: run not found' USING ERRCODE='P0001'; END IF;
  IF v_sds <> 'ACTIVE' THEN RAISE EXCEPTION 'enqueue_skill_job: run not ACTIVE' USING ERRCODE='P0001'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.skill_version_registry
                 WHERE skill_name=p_skill_name AND skill_version=p_skill_version) THEN
    RAISE EXCEPTION 'enqueue_skill_job: unregistered skill version' USING ERRCODE='P0001';
  END IF;

  SELECT content_type,content_json INTO v_env FROM public.envelope_store
    WHERE content_digest=p_request_envelope_ref;
  IF NOT FOUND OR v_env.content_type <> 'application/vnd.mergepilot.skill-request.v1+json' THEN
    RAISE EXCEPTION 'enqueue_skill_job: request envelope wrong type' USING ERRCODE='P0001';
  END IF;
  v_req := v_env.content_json;
  IF v_req->>'contract_version' IS DISTINCT FROM '1' THEN
    RAISE EXCEPTION 'enqueue_skill_job: contract_version not 1' USING ERRCODE='P0001';
  END IF;
  IF v_req->>'trace_id' IS DISTINCT FROM p_trace_id THEN
    RAISE EXCEPTION 'enqueue_skill_job: trace_id mismatch' USING ERRCODE='P0001';
  END IF;
  IF NOT (v_req ? 'input') THEN
    RAISE EXCEPTION 'enqueue_skill_job: input missing' USING ERRCODE='P0001';
  END IF;
  SELECT array_agg(k) INTO v_keys FROM jsonb_object_keys(v_req) AS k;
  IF EXISTS (SELECT 1 FROM unnest(COALESCE(v_keys,ARRAY[]::text[])) AS k
             WHERE k NOT IN ('contract_version','request_id','trace_id','input','timeout_ms')) THEN
    RAISE EXCEPTION 'enqueue_skill_job: unknown top-level key' USING ERRCODE='P0001';
  END IF;

  v_d_in := encode(public.digest(
    convert_to(public.canonical_json(v_req->'input'),'UTF8'),'sha256'),'hex');
  v_expected_req_id := 'req-'||left(encode(public.digest(
    public._canon_str(p_trace_id)||public._canon_str(p_run_id)||
    public._canon_str(p_skill_name)||public._canon_str(p_attempt::text)||
    public._canon_str(v_d_in),'sha256'),'hex'),24);
  IF v_req->>'request_id' IS DISTINCT FROM v_expected_req_id THEN
    RAISE EXCEPTION 'enqueue_skill_job: request_id mismatch' USING ERRCODE='P0001';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.snapshot_manifest_items
      WHERE snapshot_id=p_snapshot_id AND skill_name=p_skill_name
        AND skill_version=p_skill_version AND request_envelope_ref=p_request_envelope_ref) THEN
    RAISE EXCEPTION 'request not in snapshot manifest' USING ERRCODE='P0001';
  END IF;

  v_job := 'sj-'||left(encode(public.digest(
    public._canon_str(p_run_id)||public._canon_str(COALESCE(p_snapshot_id,''))||
    public._canon_str(p_skill_name)||public._canon_str(p_skill_version)||
    public._canon_str(p_attempt::text)||public._canon_str(p_request_envelope_ref),
    'sha256'),'hex'),32);
  SELECT COALESCE(array_agg(DISTINCT d ORDER BY d),ARRAY[]::text[]) INTO v_deps_input
    FROM unnest(COALESCE(p_depends_on_job_ids,ARRAY[]::text[])) AS u(d);

  INSERT INTO public.skill_job_outbox(
      job_id,run_id,snapshot_id,trace_id,skill_name,skill_version,attempt,
      request_envelope_ref,idempotency_key,status,attempts,next_retry_at)
    VALUES (v_job,p_run_id,p_snapshot_id,p_trace_id,p_skill_name,p_skill_version,p_attempt,
      p_request_envelope_ref,v_job,'PENDING',0,now())
    ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS v_inserted = ROW_COUNT;

  SELECT job_id,run_id,snapshot_id,trace_id,skill_name,skill_version,attempt,
         request_envelope_ref,idempotency_key INTO v_existing
    FROM public.skill_job_outbox WHERE job_id=v_job FOR UPDATE;
  IF NOT FOUND OR v_existing.run_id IS DISTINCT FROM p_run_id
     OR v_existing.snapshot_id IS DISTINCT FROM p_snapshot_id
     OR v_existing.trace_id IS DISTINCT FROM p_trace_id
     OR v_existing.skill_name IS DISTINCT FROM p_skill_name
     OR v_existing.skill_version IS DISTINCT FROM p_skill_version
     OR v_existing.attempt IS DISTINCT FROM p_attempt
     OR v_existing.request_envelope_ref IS DISTINCT FROM p_request_envelope_ref
     OR v_existing.idempotency_key IS DISTINCT FROM v_job THEN
    RAISE EXCEPTION 'enqueue_skill_job: idempotency conflict' USING ERRCODE='P0001';
  END IF;

  SELECT COALESCE(array_agg(depends_on_job_id ORDER BY depends_on_job_id),ARRAY[]::text[])
    INTO v_deps_existing FROM public.skill_job_dependencies WHERE job_id=v_job;
  IF v_inserted = 0 THEN
    IF v_deps_existing IS DISTINCT FROM v_deps_input THEN
      RAISE EXCEPTION 'enqueue_skill_job: dependency set conflict' USING ERRCODE='P0001';
    END IF;
    RETURN v_job;
  END IF;

  FOREACH v_dep IN ARRAY v_deps_input LOOP
    IF v_dep = v_job THEN
      RAISE EXCEPTION 'enqueue_skill_job: self-dependency' USING ERRCODE='P0001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.skill_job_outbox
                   WHERE job_id=v_dep AND run_id=p_run_id
                     AND snapshot_id IS NOT DISTINCT FROM p_snapshot_id) THEN
      RAISE EXCEPTION 'enqueue_skill_job: dependency not found or wrong run/snapshot' USING ERRCODE='P0001';
    END IF;
    INSERT INTO public.skill_job_dependencies(job_id,depends_on_job_id)
      VALUES (v_job,v_dep);
  END LOOP;

  WITH RECURSIVE dependency_closure(ancestor) AS (
    SELECT depends_on_job_id FROM public.skill_job_dependencies WHERE job_id=v_job
    UNION
    SELECT d.depends_on_job_id FROM public.skill_job_dependencies AS d
      JOIN dependency_closure AS c ON d.job_id=c.ancestor
  )
  SELECT count(*) INTO v_cycle FROM dependency_closure WHERE ancestor=v_job;
  IF v_cycle > 0 THEN
    RAISE EXCEPTION 'enqueue_skill_job: dependency cycle' USING ERRCODE='P0001';
  END IF;
  RETURN v_job;
END; $$;

-- Internal profile parser used by complete APIs when validation must precede storage.
CREATE OR REPLACE FUNCTION public._profile_json(p_content_bytes bytea) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE v_size int; v_text text; v_json json; v_jsonb jsonb; v_err text; v_detail text;
BEGIN
  v_size:=octet_length(p_content_bytes);
  IF v_size IS NULL OR v_size<=0 OR v_size>1048576 THEN
    RAISE EXCEPTION 'profile JSON: size out of range' USING ERRCODE='P0001';
  END IF;
  IF position(decode('00','hex') IN p_content_bytes)>0 THEN
    RAISE EXCEPTION 'profile JSON: U+0000 not allowed' USING ERRCODE='P0001';
  END IF;
  BEGIN v_text:=convert_from(p_content_bytes,'UTF8');
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'profile JSON: invalid UTF-8 or JSON' USING ERRCODE='P0001';
  END;
  BEGIN v_json:=v_text::json;
  EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS v_err=MESSAGE_TEXT,v_detail=PG_EXCEPTION_DETAIL;
    IF SQLSTATE='22P05' OR lower(coalesce(v_detail,'')) LIKE '%u+0000%' THEN
      RAISE EXCEPTION 'profile JSON: U+0000 not allowed' USING ERRCODE='P0001';
    ELSIF lower(coalesce(v_err,'')||' '||coalesce(v_detail,'')) LIKE '%surrogate%' THEN
      RAISE EXCEPTION 'profile JSON: invalid Unicode scalar' USING ERRCODE='P0001';
    END IF;
    RAISE EXCEPTION 'profile JSON: invalid UTF-8 or JSON' USING ERRCODE='P0001';
  END;
  BEGIN
    PERFORM public._check_json_ingress(v_json);
    v_jsonb:=v_json::jsonb;
  EXCEPTION
    WHEN SQLSTATE 'P0001' THEN RAISE;
    WHEN OTHERS THEN
      GET STACKED DIAGNOSTICS v_err=MESSAGE_TEXT,v_detail=PG_EXCEPTION_DETAIL;
      IF SQLSTATE='22P05' OR lower(coalesce(v_detail,'')) LIKE '%u+0000%' THEN
        RAISE EXCEPTION 'profile JSON: U+0000 not allowed' USING ERRCODE='P0001';
      ELSIF lower(coalesce(v_err,'')||' '||coalesce(v_detail,'')) LIKE '%surrogate%' THEN
        RAISE EXCEPTION 'profile JSON: invalid Unicode scalar' USING ERRCODE='P0001';
      END IF;
      RAISE EXCEPTION 'profile JSON: invalid UTF-8 or JSON' USING ERRCODE='P0001';
  END;
  RETURN v_jsonb;
END; $$;

CREATE OR REPLACE FUNCTION public.complete_skill_job(
  p_job_id text, p_claim_id uuid, p_response_bytes bytea,
  p_expected_output_schema_digest text, p_output_schema_validated boolean) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE
  v_run_id text; v_state text; v_job record; v_live boolean; v_invocation_id text;
  v_response jsonb; v_keys text[]; v_required text[]:=ARRAY[
    'name','version','contract_version','request_id','trace_id','status','error_code',
    'warning_codes','degradations','message','output','evidence','artifacts','started_at',
    'duration_ms','retryable','side_effects','redactions'];
  v_status text; v_error text; v_verdict text; v_request_id text; v_output_digest text;
  v_manifest_digest text; v_duration int; v_started timestamptz; v_registry_digest text;
  v_existing record;
BEGIN
  SELECT run_id INTO v_run_id FROM public.skill_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state<>'ACTIVE' THEN RETURN NULL; END IF;
  SELECT * INTO v_job FROM public.skill_job_outbox
    WHERE job_id=p_job_id AND run_id=v_run_id FOR UPDATE;
  IF NOT FOUND THEN RETURN NULL; END IF;
  v_live:=v_job.status='LEASED' AND v_job.claim_id=p_claim_id AND v_job.lease_expires_at>now();
  v_invocation_id:='inv-'||left(encode(public.digest(
    public._canon_str(p_job_id)||public._canon_str(p_claim_id::text),'sha256'),'hex'),32);
  IF NOT v_live AND NOT (v_job.status='SUCCEEDED' AND v_job.claim_id=p_claim_id
                         AND v_job.result_invocation_id=v_invocation_id) THEN
    RETURN NULL;
  END IF;

  v_response:=public._profile_json(p_response_bytes);
  IF jsonb_typeof(v_response)<>'object' OR NOT (v_response ?& v_required) THEN
    RAISE EXCEPTION 'complete_skill_job: response required fields' USING ERRCODE='P0001';
  END IF;
  SELECT array_agg(k) INTO v_keys FROM jsonb_object_keys(v_response) AS k;
  IF EXISTS (SELECT 1 FROM unnest(v_keys) AS k WHERE k NOT IN (
      'name','version','contract_version','request_id','trace_id','status','error_code',
      'warning_codes','degradations','message','output','truncated','evidence','artifacts',
      'started_at','duration_ms','retryable','side_effects','redactions')) THEN
    RAISE EXCEPTION 'complete_skill_job: response extra top-level key' USING ERRCODE='P0001';
  END IF;
  IF jsonb_typeof(v_response->'name')<>'string'
     OR jsonb_typeof(v_response->'version')<>'string'
     OR jsonb_typeof(v_response->'contract_version')<>'string'
     OR jsonb_typeof(v_response->'request_id')<>'string'
     OR jsonb_typeof(v_response->'trace_id')<>'string'
     OR jsonb_typeof(v_response->'status')<>'string'
     OR jsonb_typeof(v_response->'message')<>'string'
     OR jsonb_typeof(v_response->'started_at')<>'string'
     OR jsonb_typeof(v_response->'duration_ms')<>'number'
     OR jsonb_typeof(v_response->'retryable')<>'boolean'
     OR jsonb_typeof(v_response->'output')<>'object'
     OR jsonb_typeof(v_response->'warning_codes')<>'array'
     OR jsonb_typeof(v_response->'degradations')<>'array'
     OR jsonb_typeof(v_response->'evidence')<>'array'
     OR jsonb_typeof(v_response->'artifacts')<>'array'
     OR jsonb_typeof(v_response->'side_effects')<>'array'
     OR jsonb_typeof(v_response->'redactions')<>'array'
     OR (v_response ? 'truncated' AND jsonb_typeof(v_response->'truncated')<>'boolean')
     OR jsonb_typeof(v_response->'error_code') NOT IN ('string','null') THEN
    RAISE EXCEPTION 'complete_skill_job: response field type mismatch' USING ERRCODE='P0001';
  END IF;

  SELECT content_json->>'request_id' INTO v_request_id FROM public.envelope_store
    WHERE content_digest=v_job.request_envelope_ref;
  IF v_response->>'name' IS DISTINCT FROM v_job.skill_name
     OR v_response->>'version' IS DISTINCT FROM v_job.skill_version
     OR v_response->>'contract_version' IS DISTINCT FROM '1'
     OR v_response->>'trace_id' IS DISTINCT FROM v_job.trace_id
     OR v_response->>'request_id' IS DISTINCT FROM v_request_id THEN
    RAISE EXCEPTION 'complete_skill_job: response/job binding mismatch' USING ERRCODE='P0001';
  END IF;
  BEGIN
    IF (v_response->>'duration_ms')::numeric<>trunc((v_response->>'duration_ms')::numeric)
       OR (v_response->>'duration_ms')::numeric<0 THEN RAISE EXCEPTION 'bad duration'; END IF;
    v_duration:=(v_response->>'duration_ms')::int;
    v_started:=(v_response->>'started_at')::timestamptz;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'complete_skill_job: invalid duration or started_at' USING ERRCODE='P0001';
  END;

  v_status:=v_response->>'status'; v_error:=v_response->>'error_code';
  IF v_status NOT IN ('OK','PARTIAL','ERROR') THEN
    RAISE EXCEPTION 'complete_skill_job: invalid status' USING ERRCODE='P0001';
  END IF;
  IF (v_status IN ('OK','PARTIAL') AND v_error IS NOT NULL)
     OR (v_status='ERROR' AND (v_error IS NULL OR v_error NOT IN (
       'INVALID_INPUT','SCHEMA_VERSION_UNSUPPORTED','TIMEOUT','DENIED',
       'DEPENDENCY_UNAVAILABLE','OUTPUT_TOO_LARGE','INTERNAL_ERROR'))) THEN
    RAISE EXCEPTION 'complete_skill_job: status/error_code mismatch' USING ERRCODE='P0001';
  END IF;
  IF v_status='PARTIAL' AND jsonb_array_length(v_response->'warning_codes')=0
     AND jsonb_array_length(v_response->'degradations')=0 THEN
    RAISE EXCEPTION 'complete_skill_job: PARTIAL requires warning or degradation' USING ERRCODE='P0001';
  END IF;

  SELECT output_schema_digest INTO v_registry_digest FROM public.skill_version_registry
    WHERE skill_name=v_job.skill_name AND skill_version=v_job.skill_version;
  IF NOT FOUND OR v_registry_digest IS DISTINCT FROM p_expected_output_schema_digest THEN
    RAISE EXCEPTION 'complete_skill_job: output schema registry mismatch' USING ERRCODE='P0001';
  END IF;
  IF p_output_schema_validated IS NULL
     OR (v_status IN ('OK','PARTIAL') AND NOT p_output_schema_validated)
     OR (v_status='ERROR' AND NOT p_output_schema_validated AND v_response->'output'<>'{}'::jsonb) THEN
    RAISE EXCEPTION 'complete_skill_job: status-aware validation mismatch' USING ERRCODE='P0001';
  END IF;
  v_verdict:=v_response->'output'->>'verdict';
  IF v_verdict IS NOT NULL AND v_verdict NOT IN ('PASS','FAIL','TIMEOUT','ERROR') THEN
    RAISE EXCEPTION 'complete_skill_job: invalid verdict' USING ERRCODE='P0001';
  END IF;
  IF NOT p_output_schema_validated AND v_verdict IS NOT NULL THEN
    RAISE EXCEPTION 'complete_skill_job: unvalidated output has verdict' USING ERRCODE='P0001';
  END IF;
  IF v_job.skill_name='test-runner' AND p_output_schema_validated AND v_verdict IS NULL THEN
    RAISE EXCEPTION 'complete_skill_job: test-runner verdict required' USING ERRCODE='P0001';
  END IF;

  SELECT manifest_digest INTO v_manifest_digest FROM public.run_snapshots
    WHERE run_id=v_job.run_id AND snapshot_id=v_job.snapshot_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'complete_skill_job: snapshot missing' USING ERRCODE='P0001'; END IF;
  v_output_digest:=public.put_envelope(p_response_bytes,
    'application/vnd.mergepilot.skill-response.v1+json');

  INSERT INTO public.skill_invocations(
    invocation_id,run_id,snapshot_id,job_id,trace_id,skill_name,skill_version,attempt,
    request_id,contract_version,status,error_code,verdict,input_digest,output_digest,
    snapshot_manifest_digest,expected_output_schema_digest,output_schema_validated,
    duration_ms,started_at,finished_at,idempotency_key)
  VALUES (v_invocation_id,v_job.run_id,v_job.snapshot_id,v_job.job_id,v_job.trace_id,
    v_job.skill_name,v_job.skill_version,v_job.attempt,v_request_id,'1',v_status,v_error,v_verdict,
    v_job.request_envelope_ref,v_output_digest,v_manifest_digest,p_expected_output_schema_digest,
    p_output_schema_validated,v_duration,v_started,now(),v_invocation_id)
  ON CONFLICT (invocation_id) DO NOTHING;

  SELECT * INTO v_existing FROM public.skill_invocations WHERE invocation_id=v_invocation_id;
  IF NOT FOUND OR v_existing.run_id IS DISTINCT FROM v_job.run_id
     OR v_existing.snapshot_id IS DISTINCT FROM v_job.snapshot_id
     OR v_existing.job_id IS DISTINCT FROM v_job.job_id
     OR v_existing.trace_id IS DISTINCT FROM v_job.trace_id
     OR v_existing.skill_name IS DISTINCT FROM v_job.skill_name
     OR v_existing.skill_version IS DISTINCT FROM v_job.skill_version
     OR v_existing.attempt IS DISTINCT FROM v_job.attempt
     OR v_existing.request_id IS DISTINCT FROM v_request_id
     OR v_existing.status IS DISTINCT FROM v_status
     OR v_existing.error_code IS DISTINCT FROM v_error
     OR v_existing.verdict IS DISTINCT FROM v_verdict
     OR v_existing.input_digest IS DISTINCT FROM v_job.request_envelope_ref
     OR v_existing.output_digest IS DISTINCT FROM v_output_digest
     OR v_existing.snapshot_manifest_digest IS DISTINCT FROM v_manifest_digest
     OR v_existing.expected_output_schema_digest IS DISTINCT FROM p_expected_output_schema_digest
     OR v_existing.output_schema_validated IS DISTINCT FROM p_output_schema_validated
     OR v_existing.duration_ms IS DISTINCT FROM v_duration
     OR v_existing.started_at IS DISTINCT FROM v_started
     OR v_existing.idempotency_key IS DISTINCT FROM v_invocation_id THEN
    RAISE EXCEPTION 'complete_skill_job: idempotency conflict' USING ERRCODE='P0001';
  END IF;

  IF v_live THEN
    UPDATE public.skill_job_outbox SET status='SUCCEEDED',result_invocation_id=v_invocation_id,
      completed_at=now(),error=NULL WHERE job_id=p_job_id AND status='LEASED' AND claim_id=p_claim_id;
  END IF;
  RETURN v_invocation_id;
END; $$;

CREATE OR REPLACE FUNCTION public.complete_snapshot_job(
  p_job_id text, p_claim_id uuid, p_manifest_bytes bytea, p_set_active boolean DEFAULT true) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE
  v_run_id text; v_state text; v_job record; v_live boolean; v_manifest jsonb; v_keys text[];
  v_binding record; v_item jsonb; v_sorted_items jsonb; v_canonical_manifest jsonb;
  v_canonical_bytes bytea; v_manifest_digest text; v_snapshot_id text; v_existing record;
  v_ordinal int:=0; v_count int; v_produced timestamptz;
BEGIN
  SELECT run_id INTO v_run_id FROM public.snapshot_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state<>'ACTIVE' THEN RETURN NULL; END IF;
  SELECT * INTO v_job FROM public.snapshot_job_outbox
    WHERE job_id=p_job_id AND run_id=v_run_id FOR UPDATE;
  IF NOT FOUND THEN RETURN NULL; END IF;
  v_live:=v_job.status='LEASED' AND v_job.claim_id=p_claim_id AND v_job.lease_expires_at>now();
  IF NOT v_live AND NOT (v_job.status='SUCCEEDED' AND v_job.claim_id=p_claim_id
                         AND v_job.snapshot_id IS NOT NULL) THEN RETURN NULL; END IF;

  v_manifest:=public._profile_json(p_manifest_bytes);
  IF jsonb_typeof(v_manifest)<>'object'
     OR NOT (v_manifest ?& ARRAY['manifest_version','run_id','base_sha','head_sha','produced_at','items']) THEN
    RAISE EXCEPTION 'complete_snapshot_job: manifest required fields' USING ERRCODE='P0001';
  END IF;
  SELECT array_agg(k) INTO v_keys FROM jsonb_object_keys(v_manifest) AS k;
  IF EXISTS (SELECT 1 FROM unnest(v_keys) AS k WHERE k NOT IN
    ('manifest_version','run_id','base_sha','head_sha','produced_at','items'))
     OR jsonb_typeof(v_manifest->'manifest_version')<>'string'
     OR jsonb_typeof(v_manifest->'run_id')<>'string'
     OR jsonb_typeof(v_manifest->'base_sha')<>'string'
     OR jsonb_typeof(v_manifest->'head_sha')<>'string'
     OR jsonb_typeof(v_manifest->'produced_at')<>'string'
     OR jsonb_typeof(v_manifest->'items')<>'array' THEN
    RAISE EXCEPTION 'complete_snapshot_job: manifest shape mismatch' USING ERRCODE='P0001';
  END IF;
  IF v_manifest->>'manifest_version'<>'1' THEN
    RAISE EXCEPTION 'complete_snapshot_job: manifest_version not 1' USING ERRCODE='P0001';
  END IF;
  BEGIN v_produced:=(v_manifest->>'produced_at')::timestamptz;
  EXCEPTION WHEN OTHERS THEN RAISE EXCEPTION 'complete_snapshot_job: invalid produced_at' USING ERRCODE='P0001'; END;

  SELECT * INTO v_binding FROM public.revision_bindings WHERE binding_id=v_job.revision_binding_id;
  IF NOT FOUND OR v_manifest->>'run_id' IS DISTINCT FROM v_job.run_id
     OR v_manifest->>'base_sha' IS DISTINCT FROM v_binding.base_sha
     OR v_manifest->>'head_sha' IS DISTINCT FROM v_binding.head_sha THEN
    RAISE EXCEPTION 'complete_snapshot_job: revision sha mismatch' USING ERRCODE='P0001';
  END IF;

  FOR v_item IN SELECT value FROM jsonb_array_elements(v_manifest->'items') LOOP
    IF jsonb_typeof(v_item)<>'object' OR NOT (v_item ?& ARRAY['kind','skill','skill_version','digest'])
       OR (SELECT count(*) FROM jsonb_object_keys(v_item))<>4
       OR jsonb_typeof(v_item->'kind')<>'string' OR v_item->>'kind'<>'skill-input'
       OR jsonb_typeof(v_item->'skill')<>'string'
       OR jsonb_typeof(v_item->'skill_version')<>'string'
       OR jsonb_typeof(v_item->'digest')<>'string' THEN
      RAISE EXCEPTION 'complete_snapshot_job: invalid manifest item' USING ERRCODE='P0001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.envelope_store
      WHERE content_digest=v_item->>'digest'
        AND content_type='application/vnd.mergepilot.skill-request.v1+json')
       OR NOT EXISTS (SELECT 1 FROM public.skill_version_registry
      WHERE skill_name=v_item->>'skill' AND skill_version=v_item->>'skill_version') THEN
      RAISE EXCEPTION 'complete_snapshot_job: manifest item reference mismatch' USING ERRCODE='P0001';
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_manifest->'items') AS i
             GROUP BY i->>'skill',i->>'skill_version' HAVING count(*)>1) THEN
    RAISE EXCEPTION 'complete_snapshot_job: duplicate manifest skill/version' USING ERRCODE='P0001';
  END IF;
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'kind','skill-input','skill',i->>'skill','skill_version',i->>'skill_version','digest',i->>'digest')
      ORDER BY public._utf16_sortkey(i->>'skill'),i->>'skill_version',i->>'digest'),'[]'::jsonb)
    INTO v_sorted_items FROM jsonb_array_elements(v_manifest->'items') AS i;
  v_canonical_manifest:=jsonb_build_object('manifest_version','1','run_id',v_job.run_id,
    'base_sha',v_binding.base_sha,'head_sha',v_binding.head_sha,
    'produced_at',v_manifest->>'produced_at','items',v_sorted_items);
  v_canonical_bytes:=convert_to(public.canonical_json(v_canonical_manifest),'UTF8');
  v_manifest_digest:=encode(public.digest(v_canonical_bytes,'sha256'),'hex');
  v_snapshot_id:='snap-'||left(encode(public.digest(
    public._canon_str(v_job.run_id)||public._canon_str(v_binding.base_sha)||
    public._canon_str(v_binding.head_sha)||public._canon_str(v_manifest_digest),'sha256'),'hex'),24);

  IF NOT v_live AND v_job.snapshot_id IS DISTINCT FROM v_snapshot_id THEN
    RAISE EXCEPTION 'complete_snapshot_job: idempotency conflict' USING ERRCODE='P0001';
  END IF;
  PERFORM public.put_envelope(v_canonical_bytes,
    'application/vnd.mergepilot.snapshot-manifest.v1+json');
  INSERT INTO public.run_snapshots(snapshot_id,run_id,repo,pr_number,base_sha,head_sha,manifest_digest,incomplete)
    VALUES (v_snapshot_id,v_job.run_id,v_binding.repo,v_binding.pr_number,v_binding.base_sha,
      v_binding.head_sha,v_manifest_digest,false) ON CONFLICT DO NOTHING;
  SELECT * INTO v_existing FROM public.run_snapshots WHERE snapshot_id=v_snapshot_id;
  IF NOT FOUND OR v_existing.run_id IS DISTINCT FROM v_job.run_id
     OR v_existing.repo IS DISTINCT FROM v_binding.repo
     OR v_existing.pr_number IS DISTINCT FROM v_binding.pr_number
     OR v_existing.base_sha IS DISTINCT FROM v_binding.base_sha
     OR v_existing.head_sha IS DISTINCT FROM v_binding.head_sha
     OR v_existing.manifest_digest IS DISTINCT FROM v_manifest_digest
     OR v_existing.incomplete THEN
    RAISE EXCEPTION 'complete_snapshot_job: snapshot idempotency conflict' USING ERRCODE='P0001';
  END IF;

  v_ordinal:=0;
  FOR v_item IN SELECT value FROM jsonb_array_elements(v_sorted_items) LOOP
    INSERT INTO public.snapshot_manifest_items(snapshot_id,ordinal,skill_name,skill_version,request_envelope_ref)
      VALUES (v_snapshot_id,v_ordinal,v_item->>'skill',v_item->>'skill_version',v_item->>'digest')
      ON CONFLICT DO NOTHING;
    v_ordinal:=v_ordinal+1;
  END LOOP;
  SELECT count(*) INTO v_count FROM public.snapshot_manifest_items WHERE snapshot_id=v_snapshot_id;
  IF v_count<>jsonb_array_length(v_sorted_items) OR EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_sorted_items) WITH ORDINALITY AS x(item,ord)
    LEFT JOIN public.snapshot_manifest_items AS r ON r.snapshot_id=v_snapshot_id AND r.ordinal=x.ord-1
    WHERE r.snapshot_id IS NULL OR r.skill_name IS DISTINCT FROM x.item->>'skill'
      OR r.skill_version IS DISTINCT FROM x.item->>'skill_version'
      OR r.request_envelope_ref IS DISTINCT FROM x.item->>'digest') THEN
    RAISE EXCEPTION 'complete_snapshot_job: manifest item relational mismatch' USING ERRCODE='P0001';
  END IF;

  IF v_live THEN
    UPDATE public.snapshot_job_outbox SET status='SUCCEEDED',snapshot_id=v_snapshot_id,
      completed_at=now(),error=NULL WHERE job_id=p_job_id AND status='LEASED' AND claim_id=p_claim_id;
  END IF;
  IF p_set_active AND (SELECT active_snapshot_id FROM public.task_runs WHERE run_id=v_job.run_id) IS NULL THEN
    UPDATE public.task_runs SET active_snapshot_id=v_snapshot_id WHERE run_id=v_job.run_id AND active_snapshot_id IS NULL;
  END IF;
  RETURN v_snapshot_id;
END; $$;

CREATE OR REPLACE FUNCTION public.request_purge(p_run_id text,p_requested_by text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_state text; v_existing record; v_requested_at timestamptz; v_purge_id text;
BEGIN
  IF nullif(btrim(p_requested_by),'') IS NULL THEN
    RAISE EXCEPTION 'request_purge: requested_by required' USING ERRCODE='P0001';
  END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=p_run_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'request_purge: run not found' USING ERRCODE='P0001'; END IF;
  SELECT purge_id,target_state,status,requested_by INTO v_existing FROM public.purge_requests
    WHERE run_id=p_run_id ORDER BY requested_at DESC LIMIT 1 FOR UPDATE;
  IF FOUND AND v_existing.status IN ('REQUESTED','PURGING','PURGED') THEN
    IF v_existing.target_state='PURGED' AND v_existing.requested_by=p_requested_by THEN
      RETURN v_existing.purge_id;
    END IF;
    RAISE EXCEPTION 'request_purge: request conflict' USING ERRCODE='P0001';
  END IF;
  IF v_state<>'ACTIVE' THEN
    RAISE EXCEPTION 'request_purge: skill data not ACTIVE' USING ERRCODE='P0001';
  END IF;
  v_requested_at:=now();
  v_purge_id:='pur-'||left(encode(public.digest(
    public._canon_str(p_run_id)||public._canon_str(p_requested_by)||
    public._canon_str(v_requested_at::text),'sha256'),'hex'),24);
  INSERT INTO public.purge_requests(purge_id,run_id,target_state,status,requested_by,requested_at)
    VALUES(v_purge_id,p_run_id,'PURGED','REQUESTED',p_requested_by,v_requested_at)
    ON CONFLICT DO NOTHING;
  SELECT purge_id,target_state,status,requested_by INTO v_existing FROM public.purge_requests
    WHERE purge_id=v_purge_id;
  IF NOT FOUND OR v_existing.target_state<>'PURGED' OR v_existing.status<>'REQUESTED'
     OR v_existing.requested_by<>p_requested_by THEN
    RAISE EXCEPTION 'request_purge: idempotency conflict' USING ERRCODE='P0001';
  END IF;
  RETURN v_purge_id;
END; $$;

CREATE OR REPLACE FUNCTION public.advance_purge(p_purge_id text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_request record; v_candidates text[];
BEGIN
  SELECT run_id INTO v_run_id FROM public.purge_requests WHERE purge_id=p_purge_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'advance_purge: request not found' USING ERRCODE='P0001'; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'advance_purge: run not found' USING ERRCODE='P0001'; END IF;
  SELECT * INTO v_request FROM public.purge_requests WHERE purge_id=p_purge_id AND run_id=v_run_id FOR UPDATE;
  IF v_request.status='PURGED' AND v_state='PURGED' THEN RETURN 'PURGED'; END IF;
  IF NOT ((v_request.status='REQUESTED' AND v_state='ACTIVE')
       OR (v_request.status='PURGING' AND v_state='PURGING')) THEN
    RAISE EXCEPTION 'advance_purge: state conflict' USING ERRCODE='P0001';
  END IF;

  SELECT COALESCE(array_agg(DISTINCT digest),ARRAY[]::text[]) INTO v_candidates FROM (
    SELECT input_digest AS digest FROM public.skill_invocations WHERE run_id=v_run_id
    UNION ALL SELECT output_digest FROM public.skill_invocations WHERE run_id=v_run_id AND output_digest IS NOT NULL
    UNION ALL SELECT snapshot_manifest_digest FROM public.skill_invocations WHERE run_id=v_run_id AND snapshot_manifest_digest IS NOT NULL
    UNION ALL SELECT request_envelope_ref FROM public.skill_job_outbox WHERE run_id=v_run_id
    UNION ALL SELECT manifest_digest FROM public.run_snapshots WHERE run_id=v_run_id
    UNION ALL SELECT i.request_envelope_ref FROM public.snapshot_manifest_items AS i
      JOIN public.run_snapshots AS s ON s.snapshot_id=i.snapshot_id WHERE s.run_id=v_run_id
  ) AS candidate_set;

  UPDATE public.task_runs SET active_snapshot_id=NULL WHERE run_id=v_run_id;
  IF v_request.status='REQUESTED' THEN
    UPDATE public.purge_requests SET status='PURGING',purging_at=now(),error=NULL
      WHERE purge_id=p_purge_id AND status='REQUESTED';
    UPDATE public.task_runs SET skill_data_state='PURGING'
      WHERE run_id=v_run_id AND skill_data_state='ACTIVE';
  END IF;

  DELETE FROM public.skill_job_outbox WHERE run_id=v_run_id;
  DELETE FROM public.snapshot_job_outbox WHERE run_id=v_run_id;
  DELETE FROM public.run_snapshots WHERE run_id=v_run_id;
  DELETE FROM public.envelope_store AS envelope
    WHERE envelope.content_digest=ANY(v_candidates)
      AND NOT EXISTS (SELECT 1 FROM public.skill_invocations i
        WHERE i.input_digest=envelope.content_digest OR i.output_digest=envelope.content_digest
           OR i.snapshot_manifest_digest=envelope.content_digest)
      AND NOT EXISTS (SELECT 1 FROM public.skill_job_outbox j
        WHERE j.request_envelope_ref=envelope.content_digest)
      AND NOT EXISTS (SELECT 1 FROM public.run_snapshots s
        WHERE s.manifest_digest=envelope.content_digest)
      AND NOT EXISTS (SELECT 1 FROM public.snapshot_manifest_items mi
        WHERE mi.request_envelope_ref=envelope.content_digest);

  UPDATE public.purge_requests SET status='PURGED',completed_at=now(),error=NULL
    WHERE purge_id=p_purge_id AND status='PURGING';
  UPDATE public.task_runs SET skill_data_state='PURGED'
    WHERE run_id=v_run_id AND skill_data_state='PURGING';
  IF NOT FOUND THEN RAISE EXCEPTION 'advance_purge: final state conflict' USING ERRCODE='P0001'; END IF;
  RETURN 'PURGED';
END; $$;

-- Worker lease state machine. Each entry locks task_runs before the outbox row.
CREATE OR REPLACE FUNCTION public.claim_snapshot_job(
  p_job_id text, p_worker text, p_lease_seconds int DEFAULT 60) RETURNS uuid
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_job record; v_claim uuid;
BEGIN
  IF p_lease_seconds NOT BETWEEN 1 AND 3600 OR nullif(btrim(p_worker),'') IS NULL THEN
    RAISE EXCEPTION 'claim_snapshot_job: invalid lease or worker' USING ERRCODE='P0001';
  END IF;
  SELECT run_id INTO v_run_id FROM public.snapshot_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN
    RAISE EXCEPTION 'claim_snapshot_job: skill data not ACTIVE' USING ERRCODE='P0001';
  END IF;
  SELECT status,attempts,max_attempts,next_retry_at,lease_expires_at INTO v_job
    FROM public.snapshot_job_outbox WHERE job_id=p_job_id AND run_id=v_run_id FOR UPDATE;
  IF NOT FOUND THEN RETURN NULL; END IF;
  IF v_job.status='LEASED' AND v_job.lease_expires_at<=now() AND v_job.attempts>=v_job.max_attempts THEN
    UPDATE public.snapshot_job_outbox SET status='FAILED',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,error='lease expired after max attempts',completed_at=now()
      WHERE job_id=p_job_id;
    RETURN NULL;
  END IF;
  IF NOT ((v_job.status='PENDING' AND v_job.next_retry_at<=now() AND v_job.attempts<v_job.max_attempts)
       OR (v_job.status='LEASED' AND v_job.lease_expires_at<=now() AND v_job.attempts<v_job.max_attempts)) THEN
    RETURN NULL;
  END IF;
  v_claim:=public.gen_random_uuid();
  UPDATE public.snapshot_job_outbox SET status='LEASED',claim_id=v_claim,leased_by=p_worker,
    lease_expires_at=now()+make_interval(secs=>p_lease_seconds),last_heartbeat_at=now(),
    attempts=attempts+1,claimed_at=now(),error=NULL,completed_at=NULL
    WHERE job_id=p_job_id;
  RETURN v_claim;
END; $$;

CREATE OR REPLACE FUNCTION public.claim_skill_job(
  p_job_id text, p_worker text, p_lease_seconds int DEFAULT 60) RETURNS uuid
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_job record; v_claim uuid;
BEGIN
  IF p_lease_seconds NOT BETWEEN 1 AND 3600 OR nullif(btrim(p_worker),'') IS NULL THEN
    RAISE EXCEPTION 'claim_skill_job: invalid lease or worker' USING ERRCODE='P0001';
  END IF;
  SELECT run_id INTO v_run_id FROM public.skill_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN
    RAISE EXCEPTION 'claim_skill_job: skill data not ACTIVE' USING ERRCODE='P0001';
  END IF;
  SELECT status,attempts,max_attempts,next_retry_at,lease_expires_at INTO v_job
    FROM public.skill_job_outbox WHERE job_id=p_job_id AND run_id=v_run_id FOR UPDATE;
  IF NOT FOUND THEN RETURN NULL; END IF;
  IF v_job.status='LEASED' AND v_job.lease_expires_at<=now() AND v_job.attempts>=v_job.max_attempts THEN
    UPDATE public.skill_job_outbox SET status='FAILED',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,error='lease expired after max attempts',completed_at=now()
      WHERE job_id=p_job_id;
    RETURN NULL;
  END IF;
  IF NOT ((v_job.status='PENDING' AND v_job.next_retry_at<=now() AND v_job.attempts<v_job.max_attempts
           AND NOT EXISTS (SELECT 1 FROM public.skill_job_dependencies AS d
             JOIN public.skill_job_outbox AS dependency ON dependency.job_id=d.depends_on_job_id
             WHERE d.job_id=p_job_id AND dependency.status<>'SUCCEEDED'))
       OR (v_job.status='LEASED' AND v_job.lease_expires_at<=now() AND v_job.attempts<v_job.max_attempts)) THEN
    RETURN NULL;
  END IF;
  v_claim:=public.gen_random_uuid();
  UPDATE public.skill_job_outbox SET status='LEASED',claim_id=v_claim,leased_by=p_worker,
    lease_expires_at=now()+make_interval(secs=>p_lease_seconds),last_heartbeat_at=now(),
    attempts=attempts+1,claimed_at=now(),error=NULL,completed_at=NULL
    WHERE job_id=p_job_id;
  RETURN v_claim;
END; $$;

CREATE OR REPLACE FUNCTION public.heartbeat_snapshot_job(
  p_job_id text, p_claim_id uuid, p_lease_seconds int DEFAULT 60) RETURNS boolean
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_rows int;
BEGIN
  IF p_lease_seconds NOT BETWEEN 1 AND 3600 THEN
    RAISE EXCEPTION 'heartbeat_snapshot_job: invalid lease' USING ERRCODE='P0001';
  END IF;
  SELECT run_id INTO v_run_id FROM public.snapshot_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN RETURN false; END IF;
  PERFORM 1 FROM public.snapshot_job_outbox WHERE job_id=p_job_id FOR UPDATE;
  UPDATE public.snapshot_job_outbox SET lease_expires_at=now()+make_interval(secs=>p_lease_seconds),
    last_heartbeat_at=now() WHERE job_id=p_job_id AND status='LEASED'
      AND claim_id=p_claim_id AND lease_expires_at>now();
  GET DIAGNOSTICS v_rows=ROW_COUNT;
  RETURN v_rows=1;
END; $$;

CREATE OR REPLACE FUNCTION public.heartbeat_skill_job(
  p_job_id text, p_claim_id uuid, p_lease_seconds int DEFAULT 60) RETURNS boolean
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_rows int;
BEGIN
  IF p_lease_seconds NOT BETWEEN 1 AND 3600 THEN
    RAISE EXCEPTION 'heartbeat_skill_job: invalid lease' USING ERRCODE='P0001';
  END IF;
  SELECT run_id INTO v_run_id FROM public.skill_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN RETURN false; END IF;
  PERFORM 1 FROM public.skill_job_outbox WHERE job_id=p_job_id FOR UPDATE;
  UPDATE public.skill_job_outbox SET lease_expires_at=now()+make_interval(secs=>p_lease_seconds),
    last_heartbeat_at=now() WHERE job_id=p_job_id AND status='LEASED'
      AND claim_id=p_claim_id AND lease_expires_at>now();
  GET DIAGNOSTICS v_rows=ROW_COUNT;
  RETURN v_rows=1;
END; $$;

CREATE OR REPLACE FUNCTION public.fail_snapshot_job(
  p_job_id text, p_claim_id uuid, p_error text) RETURNS boolean
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_job record; v_backoff double precision;
BEGIN
  IF nullif(btrim(p_error),'') IS NULL THEN RAISE EXCEPTION 'fail_snapshot_job: error required' USING ERRCODE='P0001'; END IF;
  SELECT run_id INTO v_run_id FROM public.snapshot_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN RETURN false; END IF;
  SELECT attempts,max_attempts INTO v_job FROM public.snapshot_job_outbox
    WHERE job_id=p_job_id AND status='LEASED' AND claim_id=p_claim_id
      AND lease_expires_at>now() FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  IF v_job.attempts<v_job.max_attempts THEN
    v_backoff:=least(60::double precision,2::double precision*power(2::double precision,v_job.attempts-1));
    UPDATE public.snapshot_job_outbox SET status='PENDING',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,next_retry_at=now()+make_interval(secs=>v_backoff),
      error=p_error,completed_at=NULL WHERE job_id=p_job_id;
  ELSE
    UPDATE public.snapshot_job_outbox SET status='FAILED',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,error=p_error,completed_at=now() WHERE job_id=p_job_id;
  END IF;
  RETURN true;
END; $$;

CREATE OR REPLACE FUNCTION public.fail_skill_job(
  p_job_id text, p_claim_id uuid, p_error text) RETURNS boolean
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_job record; v_backoff double precision;
BEGIN
  IF nullif(btrim(p_error),'') IS NULL THEN RAISE EXCEPTION 'fail_skill_job: error required' USING ERRCODE='P0001'; END IF;
  SELECT run_id INTO v_run_id FROM public.skill_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN RETURN false; END IF;
  SELECT attempts,max_attempts INTO v_job FROM public.skill_job_outbox
    WHERE job_id=p_job_id AND status='LEASED' AND claim_id=p_claim_id
      AND lease_expires_at>now() FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  IF v_job.attempts<v_job.max_attempts THEN
    v_backoff:=least(60::double precision,2::double precision*power(2::double precision,v_job.attempts-1));
    UPDATE public.skill_job_outbox SET status='PENDING',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,next_retry_at=now()+make_interval(secs=>v_backoff),
      error=p_error,completed_at=NULL WHERE job_id=p_job_id;
  ELSE
    UPDATE public.skill_job_outbox SET status='FAILED',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,error=p_error,completed_at=now() WHERE job_id=p_job_id;
  END IF;
  RETURN true;
END; $$;

-- owner + REVOKE PUBLIC for producer and worker SD functions
ALTER FUNCTION public.put_envelope(bytea,text) OWNER TO runtime_owner;
ALTER FUNCTION public.bind_revision(text,text,int,text,text,text,text) OWNER TO runtime_owner;
ALTER FUNCTION public.enqueue_snapshot_job(text,text) OWNER TO runtime_owner;
ALTER FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) OWNER TO runtime_owner;
ALTER FUNCTION public._profile_json(bytea) OWNER TO runtime_owner;
ALTER FUNCTION public.complete_skill_job(text,uuid,bytea,text,boolean) OWNER TO runtime_owner;
ALTER FUNCTION public.complete_snapshot_job(text,uuid,bytea,boolean) OWNER TO runtime_owner;
ALTER FUNCTION public.request_purge(text,text) OWNER TO envelope_maint;
ALTER FUNCTION public.advance_purge(text) OWNER TO envelope_maint;
ALTER FUNCTION public.claim_snapshot_job(text,text,int) OWNER TO runtime_owner;
ALTER FUNCTION public.claim_skill_job(text,text,int) OWNER TO runtime_owner;
ALTER FUNCTION public.heartbeat_snapshot_job(text,uuid,int) OWNER TO runtime_owner;
ALTER FUNCTION public.heartbeat_skill_job(text,uuid,int) OWNER TO runtime_owner;
ALTER FUNCTION public.fail_snapshot_job(text,uuid,text) OWNER TO runtime_owner;
ALTER FUNCTION public.fail_skill_job(text,uuid,text) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public.put_envelope(bytea,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.bind_revision(text,text,int,text,text,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.enqueue_snapshot_job(text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public._profile_json(bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.complete_skill_job(text,uuid,bytea,text,boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.complete_snapshot_job(text,uuid,bytea,boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.request_purge(text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.advance_purge(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.claim_snapshot_job(text,text,int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.claim_skill_job(text,text,int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.heartbeat_snapshot_job(text,uuid,int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.heartbeat_skill_job(text,uuid,int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fail_snapshot_job(text,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fail_skill_job(text,uuid,text) FROM PUBLIC;

-- ═══ 15. ACL 收敛(REVOKE ALL → 精确 GRANT) ═══
-- owner 角色(gate_owner/envelope_maint/runtime_owner):全撤 schema/table/sequence/function
REVOKE ALL PRIVILEGES ON SCHEMA public FROM gate_owner, envelope_maint, runtime_owner;
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA public FROM gate_owner, envelope_maint, runtime_owner;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM gate_owner, envelope_maint, runtime_owner;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM gate_owner, envelope_maint, runtime_owner;
-- runtime worker 角色:全撤 DML(本轮无 SD EXECUTE 授予,后续增量补)
REVOKE ALL PRIVILEGES ON SCHEMA public FROM skill_runner, snapshot_worker, purge_operator;
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA public FROM skill_runner, snapshot_worker, purge_operator;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM skill_runner, snapshot_worker, purge_operator;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM skill_runner, snapshot_worker, purge_operator;

GRANT SELECT ON public.task_runs        TO gate_owner;
GRANT SELECT ON public.run_snapshots    TO gate_owner;
GRANT SELECT ON public.revision_bindings TO gate_owner;
GRANT UPDATE (skill_data_state) ON public.task_runs TO gate_owner;
GRANT EXECUTE ON FUNCTION public._writer_gate()                TO gate_owner;
GRANT EXECUTE ON FUNCTION public._writer_gate_snapshot_job()   TO gate_owner;
GRANT EXECUTE ON FUNCTION public._writer_gate_rollback()       TO gate_owner;

GRANT SELECT ON public.task_runs        TO envelope_maint;
GRANT UPDATE (skill_data_state) ON public.task_runs TO envelope_maint;
GRANT UPDATE (active_snapshot_id) ON public.task_runs TO envelope_maint;
GRANT SELECT ON public.envelope_store   TO envelope_maint;
GRANT INSERT ON public.envelope_store   TO envelope_maint;
GRANT SELECT ON public.purge_requests   TO envelope_maint;
GRANT INSERT ON public.purge_requests   TO envelope_maint;
GRANT UPDATE ON public.purge_requests   TO envelope_maint;
GRANT SELECT ON public.skill_invocations, public.skill_job_outbox,
  public.snapshot_job_outbox, public.run_snapshots, public.snapshot_manifest_items TO envelope_maint;
GRANT DELETE ON public.skill_job_outbox, public.snapshot_job_outbox,
  public.run_snapshots, public.envelope_store TO envelope_maint;
GRANT EXECUTE ON FUNCTION public._canon_str(text) TO envelope_maint;

-- worker 角色:SELECT 读权限;无 DML(SD API 后续增量授予 EXECUTE)
GRANT SELECT ON public.task_runs, public.run_snapshots, public.envelope_store,
  public.snapshot_job_outbox, public.skill_job_outbox, public.skill_invocations,
  public.snapshot_manifest_items, public.skill_version_registry, public.revision_bindings,
  public.skill_job_dependencies
  TO mergepilot, skill_runner, snapshot_worker, purge_operator;
-- runtime_owner (NOLOGIN SD function owner) table access for function bodies
GRANT SELECT ON public.task_runs, public.run_pr_bindings, public.mcp_calls, public.envelope_store,
             public.skill_version_registry, public.revision_bindings, public.snapshot_manifest_items,
             public.run_snapshots, public.skill_job_outbox, public.snapshot_job_outbox,
             public.skill_job_dependencies, public.skill_invocations TO runtime_owner;
GRANT INSERT ON public.envelope_store, public.revision_bindings, public.snapshot_job_outbox,
             public.skill_job_outbox, public.skill_job_dependencies, public.run_snapshots,
             public.snapshot_manifest_items, public.skill_invocations TO runtime_owner;
GRANT UPDATE ON public.snapshot_job_outbox, public.skill_job_outbox TO runtime_owner;
GRANT UPDATE ON public.task_runs, public.run_pr_bindings TO runtime_owner;
-- SD API EXECUTE grants(Stage 2.1B-1)
GRANT EXECUTE ON FUNCTION public._check_json_ingress(json) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public._profile_json(bytea) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public.put_envelope(bytea,text) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public._canon_str(text) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public._utf16_sortkey(text) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public._jcs_number(float8) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public._jcs_escape(text) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public.canonical_json(jsonb) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public.put_envelope(bytea,text) TO mergepilot, skill_runner, snapshot_worker;
GRANT EXECUTE ON FUNCTION public.bind_revision(text,text,int,text,text,text,text) TO mergepilot;
GRANT EXECUTE ON FUNCTION public.enqueue_snapshot_job(text,text) TO mergepilot;
GRANT EXECUTE ON FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) TO mergepilot;
GRANT EXECUTE ON FUNCTION public.complete_snapshot_job(text,uuid,bytea,boolean) TO snapshot_worker;
GRANT EXECUTE ON FUNCTION public.complete_skill_job(text,uuid,bytea,text,boolean) TO skill_runner;
GRANT EXECUTE ON FUNCTION public.claim_snapshot_job(text,text,int) TO snapshot_worker;
GRANT EXECUTE ON FUNCTION public.claim_skill_job(text,text,int) TO skill_runner;
GRANT EXECUTE ON FUNCTION public.heartbeat_snapshot_job(text,uuid,int) TO snapshot_worker;
GRANT EXECUTE ON FUNCTION public.heartbeat_skill_job(text,uuid,int) TO skill_runner;
GRANT EXECUTE ON FUNCTION public.fail_snapshot_job(text,uuid,text) TO snapshot_worker;
GRANT EXECUTE ON FUNCTION public.fail_skill_job(text,uuid,text) TO skill_runner;
GRANT EXECUTE ON FUNCTION public.request_purge(text,text) TO purge_operator;
GRANT EXECUTE ON FUNCTION public.advance_purge(text) TO purge_operator;

-- ═══ 16. catalog 自检(按函数名分别验证;非 owner 总数) ═══
DO $$
DECLARE bad int := 0; n int;
  FN text[] := ARRAY['_writer_gate','_writer_gate_snapshot_job','_writer_gate_rollback'];  -- 3 writer-gate BY NAME
BEGIN
  -- 16.1 3 writer-gate 函数 BY NAME:prosecdef + pronargs=0 + search_path=pg_catalog + owner=gate_owner + 无 PUBLIC EXECUTE
  SELECT count(*) INTO bad FROM unnest(FN) AS f
    WHERE NOT EXISTS (
      SELECT 1 FROM pg_proc p JOIN pg_namespace nn ON nn.oid=p.pronamespace
      WHERE nn.nspname='public' AND p.proname=f AND p.prosecdef AND p.pronargs=0
        AND p.proconfig IS NOT NULL AND array_position(p.proconfig,'search_path=pg_catalog') IS NOT NULL
        AND p.proowner='gate_owner'::regrole
        AND NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                        WHERE a.grantee=0 AND a.privilege_type='EXECUTE'));
  IF bad <> 0 THEN RAISE EXCEPTION 'self-check: % writer-gate fn missing/wrong (by name)', bad; END IF;

  -- 16.2 1 revision guard 函数 BY NAME(RETURNS trigger;prosecdef;owner=gate_owner;search_path;无 PUBLIC EXECUTE)
  IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace nn ON nn.oid=p.pronamespace
        JOIN pg_type rt ON rt.oid=p.prorettype
        WHERE nn.nspname='public' AND p.proname='_guard_bound_run_pr_revision' AND p.prosecdef
          AND p.proowner='gate_owner'::regrole
          AND p.proconfig IS NOT NULL AND array_position(p.proconfig,'search_path=pg_catalog') IS NOT NULL
          AND rt.typname='trigger'
          AND NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                          WHERE a.grantee=0 AND a.privilege_type='EXECUTE')) THEN
    RAISE EXCEPTION 'self-check: revision guard fn _guard_bound_run_pr_revision missing/wrong'; END IF;

  -- 16.3 gate_owner owner-总函数数(信息性 NOTICE,NOT PASS/FAIL 门禁;判定以 §16.1/16.2 by-name 为唯一权威)
  SELECT count(*) INTO n FROM pg_proc p JOIN pg_namespace nn ON nn.oid=p.pronamespace
    WHERE nn.nspname='public' AND p.proowner='gate_owner'::regrole;
  RAISE NOTICE 'self-check (info, not a gate): gate_owner owns % functions; by-name catalog (§16.1/16.2) is authoritative', n;

  -- 16.4 9 trg_gate_* 触发器(tgtype=23, enabled)+ 精确映射
  SELECT count(*) INTO bad FROM (VALUES
    ('run_snapshots','trg_gate_run_snapshots','_writer_gate'),
    ('snapshot_job_outbox','trg_gate_snapshot_job_outbox','_writer_gate_snapshot_job'),
    ('skill_job_outbox','trg_gate_skill_job_outbox','_writer_gate'),
    ('skill_invocations','trg_gate_skill_invocations','_writer_gate'),
    ('dispatch_outbox','trg_gate_dispatch_outbox','_writer_gate'),
    ('approvals','trg_gate_approvals','_writer_gate'),
    ('policy_action_outbox','trg_gate_policy_action_outbox','_writer_gate'),
    ('stage_runs','trg_gate_stage_runs','_writer_gate'),
    ('rollback_runs','trg_gate_rollback_runs','_writer_gate_rollback')
  ) AS m(tbl,trg,fn) WHERE NOT EXISTS (
    SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace nn ON nn.oid=c.relnamespace
      JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace
      WHERE nn.nspname='public' AND pn.nspname='public' AND c.relname=m.tbl AND t.tgname=m.trg
        AND p.proname=m.fn AND NOT t.tgisinternal AND t.tgtype=23 AND t.tgenabled='O');
  IF bad <> 0 THEN RAISE EXCEPTION 'self-check: % gate trigger mapping missing/wrong', bad; END IF;

  -- 16.5 immutable/guard 触发器 BY NAME
  SELECT count(*) INTO bad FROM (VALUES
    ('trg_envelope_immutable'),('trg_envelope_digest_check'),('trg_run_snapshots_immutable'),
    ('trg_skill_invocations_immutable'),('trg_revision_bindings_immutable'),
    ('trg_skill_version_registry_immutable'),
    ('trg_run_pr_bindings_revision_guard'),('trg_transition')) AS v(tn)
    WHERE NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
      JOIN pg_namespace nn ON nn.oid=c.relnamespace WHERE nn.nspname='public' AND t.tgname=v.tn AND NOT t.tgisinternal);
  IF bad <> 0 THEN RAISE EXCEPTION 'self-check: % named trigger missing', bad; END IF;

  -- 16.6 复合 FK + skill_job_dependencies FK/CK BY NAME
  SELECT count(*) INTO bad FROM (VALUES
    ('task_runs_active_snapshot_run_fkey'),('snapshot_job_outbox_run_snapshot_fkey'),
    ('skill_job_outbox_run_snapshot_fkey'),('skill_invocations_run_snapshot_fkey'),
    ('skill_invocations_run_job_fkey'),('skill_job_outbox_registry_fkey'),
    ('skill_invocations_registry_fkey'),('smi_registry_fkey'),
    ('skill_job_outbox_result_invocation_fkey'),
    ('skill_job_dependencies_job_fkey'),('skill_job_dependencies_dep_fkey'),('skill_job_dependencies_no_self')) AS v(cn)
    WHERE NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname=v.cn);
  IF bad <> 0 THEN RAISE EXCEPTION 'self-check: % composite/FK/CK missing', bad; END IF;

  -- 16.6.1 status-aware CK BY NAME(4 项)
  SELECT count(*) INTO bad FROM (VALUES
    ('sinv_status_validated'),('sinv_validated_verdict'),('sinv_status_err_req'),('sinv_status_err_ok')) AS v(cn)
    WHERE NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname=v.cn AND conrelid='public.skill_invocations'::regclass);
  IF bad <> 0 THEN RAISE EXCEPTION 'self-check: % status-aware CK missing', bad; END IF;

  -- 真正双向 EXCEPT(预期 EXCEPT 实际 与 实际 EXCEPT 预期 均为空)
  IF EXISTS (
    SELECT * FROM (VALUES
      ('diff-parse','1.0.0','89d628502dd726d6dfa1df4f52687bd51a1cea75d81e680a5025852f3b5b7285','e6e0eb2077645007de8115a0be697b27954e34b9a000e9bc7c6de03c27fd355b'),
      ('risk-classify','1.0.0','45ca36e3a5c6ff8146e13d7935918240279f1ffbc28872c8b1c04c81a3111371','b4d8e0519916cc21ea5286a677a94de53af2cb968073c1b06cf8b4d6ccbda09a'),
      ('sast-scan','1.0.0','8d008630393b59e77ed66669c2b5d6a45591dbbed5c3bc5554289035c5813598','fda15df57b9713bf76f95ff0668a8c76a8f7f68cabb40348232d571614e497e1'),
      ('test-runner','1.0.0','a90f67f1c19243582402d8e8b590f9a104a937637442be29a3d980848b9ecda9','461c5f026e01a4641acc0821220f6720361402ee2c3fc802421a6a11c41772d9'),
      ('case-retrieval','1.0.0','549526ab5aa410b67754a52ba7fcd826b2cc7813189eac0f929c5b53e666c3d3','4366b3e76796756158197b10c77c135b7d6443c9262ad9a5be5c03a60f662b57'),
      ('pr-lifecycle','1.0.0','7157df189df14d7128c3fe9f40e749050ed8251f206a7f5a57ca31da9859c424','ee27d6b587ca9b82d9da189ae98ca4a58437110ebe3ff75348506355c075dc1c')
    ) AS e(sn,sv,rid,oid)
    EXCEPT SELECT skill_name,skill_version,request_schema_digest,output_schema_digest FROM public.skill_version_registry
  ) THEN RAISE EXCEPTION 'self-check: registry drift (expected-not-actual)'; END IF;
  IF EXISTS (
    SELECT skill_name,skill_version,request_schema_digest,output_schema_digest FROM public.skill_version_registry
    EXCEPT SELECT * FROM (VALUES
      ('diff-parse','1.0.0','89d628502dd726d6dfa1df4f52687bd51a1cea75d81e680a5025852f3b5b7285','e6e0eb2077645007de8115a0be697b27954e34b9a000e9bc7c6de03c27fd355b'),
      ('risk-classify','1.0.0','45ca36e3a5c6ff8146e13d7935918240279f1ffbc28872c8b1c04c81a3111371','b4d8e0519916cc21ea5286a677a94de53af2cb968073c1b06cf8b4d6ccbda09a'),
      ('sast-scan','1.0.0','8d008630393b59e77ed66669c2b5d6a45591dbbed5c3bc5554289035c5813598','fda15df57b9713bf76f95ff0668a8c76a8f7f68cabb40348232d571614e497e1'),
      ('test-runner','1.0.0','a90f67f1c19243582402d8e8b590f9a104a937637442be29a3d980848b9ecda9','461c5f026e01a4641acc0821220f6720361402ee2c3fc802421a6a11c41772d9'),
      ('case-retrieval','1.0.0','549526ab5aa410b67754a52ba7fcd826b2cc7813189eac0f929c5b53e666c3d3','4366b3e76796756158197b10c77c135b7d6443c9262ad9a5be5c03a60f662b57'),
      ('pr-lifecycle','1.0.0','7157df189df14d7128c3fe9f40e749050ed8251f206a7f5a57ca31da9859c424','ee27d6b587ca9b82d9da189ae98ca4a58437110ebe3ff75348506355c075dc1c')
    ) AS e(sn,sv,rid,oid)
  ) THEN RAISE EXCEPTION 'self-check: registry drift (actual-not-expected)'; END IF;

  -- 16.8 角色 + 双向 membership=0(owner 三角色)
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('gate_owner','envelope_maint','runtime_owner')
             AND (rolcanlogin OR rolinherit OR rolbypassrls OR rolsuper OR rolcreatedb OR rolcreaterole)) THEN
    RAISE EXCEPTION 'self-check: owner role has disallowed attribute'; END IF;
  SELECT count(*) INTO n FROM pg_auth_members
    WHERE member IN ('gate_owner'::regrole,'envelope_maint'::regrole,'runtime_owner'::regrole)
       OR roleid IN ('gate_owner'::regrole,'envelope_maint'::regrole,'runtime_owner'::regrole);
  IF n <> 0 THEN RAISE EXCEPTION 'self-check: % disallowed membership', n; END IF;

RAISE NOTICE 'M4-F1 v2.8 implementation self-check PASS (by-name catalog)';
END $$;

COMMIT;
-- ===== tools/audit-db/m8gh1_github_ingress.sql =====
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
  event_name        TEXT NOT NULL CHECK (event_name ~ '^[a-z_]{1,64}$'),  -- 对齐 receiver._EVENT_NAME_RE:交付台账记录一切事件名(push 等记 IGNORED),原 IN('ping','pull_request','other') 与接收端写原始事件名的实现冲突,真实 webhook 流量实证 push 触发 check violation→503
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
  -- installation_id 仅 GitHub App webhook 载荷携带(Phase 2);repo webhook(Phase 1)
  -- 无 installation 对象,实测强约束会把真实 pull_request 交付挡成 503,故不作为必需字段。
  CONSTRAINT gh_deliveries_pull_request_envelope CHECK (
    event_name <> 'pull_request' OR (
      repo IS NOT NULL
      AND pr_number IS NOT NULL AND observed_head_sha IS NOT NULL
      AND observed_base_sha IS NOT NULL)  -- 动作白名单由 receiver.classify 裁决;DB 约束不重复 enforce(closed/assigned 等非映射动作按 IGNORED 记账)
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

-- ===== tools/audit-db/m9_migration_verification.sql =====
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
    -- 绑定了迁移验证的票据:目标数据摘要是必需输入(fail-closed)。它由发布执行方用与基线登记
    -- 相同的规范算法(tools/dbverify/data_digest.py)在目标库上计算;缺省 NULL 会跳过数据比对,
    -- 等于把"验证绑定到数据版本"变成可选 —— 因此拒绝。
    IF p_target_data_digest IS NULL THEN
      RAISE EXCEPTION 'DB_RELEASE_GATE_REFUSED: TARGET_DATA_DIGEST_REQUIRED (bound ticket % must present the target data digest at claim)', p_ticket_id
        USING ERRCODE = 'P0001';
    END IF;
    SELECT g.valid, g.reason, g.bound_head_sha, g.current_head_sha
      INTO v_gate FROM public.db_release_gate(p_ticket_id, p_target_data_digest) g;
    IF v_gate.valid IS DISTINCT FROM TRUE THEN
      -- fail-closed:票据保持 APPROVED(不推进),不产生 execution_id,不写入
      RAISE EXCEPTION 'DB_RELEASE_GATE_REFUSED: % (bound_head=%, current_head=%)',
        COALESCE(v_gate.reason, 'GATE_ERROR'), v_gate.bound_head_sha, v_gate.current_head_sha
        USING ERRCODE = 'P0001';
    END IF;
  ELSIF EXISTS (SELECT 1 FROM public.migration_candidates c JOIN public.approvals a ON a.run_id = c.run_id
                 WHERE a.ticket_id = p_ticket_id) THEN
    -- run 登记过迁移候选却没有把票据绑定到一次 PASS 验证:迁移票据必须绑定验证,否则不得执行。
    RAISE EXCEPTION 'DB_RELEASE_GATE_REFUSED: MIGRATION_VERIFICATION_REQUIRED (run of ticket % registered migration candidates but the ticket is not bound to a verification)', p_ticket_id
      USING ERRCODE = 'P0001';
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
-- and on migration_candidates for the two EXISTS checks, and (b) EXECUTE on db_release_gate.
GRANT SELECT ON public.approval_verification_bindings, public.migration_candidates TO mergepilot_l2_owner;
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
