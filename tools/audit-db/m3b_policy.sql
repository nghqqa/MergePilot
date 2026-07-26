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
