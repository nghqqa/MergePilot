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
