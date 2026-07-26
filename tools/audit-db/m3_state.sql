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
