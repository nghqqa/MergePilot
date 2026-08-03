-- tests/m4f1/sql/verify_no_m4f1.sql — 证明 broken migration 回滚后无半成品 m4f1 对象。
-- 任一 m4f1 对象存在 → RAISE → rc≠0(ON_ERROR_STOP=1)。
\set ON_ERROR_STOP on
DO $$
DECLARE n int;
BEGIN
  -- skill_data_state 列必须不存在
  SELECT count(*) INTO n FROM information_schema.columns
    WHERE table_schema='public' AND table_name='task_runs' AND column_name='skill_data_state';
  IF n <> 0 THEN RAISE EXCEPTION 'ATOMIC-ROLLBACK FAIL: task_runs.skill_data_state still present (%)', n; END IF;

  -- gate_owner / envelope_maint 角色必须不存在
  SELECT count(*) INTO n FROM pg_roles WHERE rolname IN ('gate_owner','envelope_maint');
  IF n <> 0 THEN RAISE EXCEPTION 'ATOMIC-ROLLBACK FAIL: gate roles still present (%)', n; END IF;

  -- 5 张新表必须不存在
  SELECT count(*) INTO n FROM information_schema.tables
    WHERE table_schema='public' AND table_name IN ('run_snapshots','snapshot_job_outbox','skill_job_outbox','skill_invocations','envelope_store');
  IF n <> 0 THEN RAISE EXCEPTION 'ATOMIC-ROLLBACK FAIL: new tables still present (%)', n; END IF;

  -- 3 个 gate function 必须不存在
  SELECT count(*) INTO n FROM pg_proc p JOIN pg_namespace nn ON nn.oid=p.pronamespace
    WHERE nn.nspname='public' AND p.proname IN ('_writer_gate','_writer_gate_snapshot_job','_writer_gate_rollback');
  IF n <> 0 THEN RAISE EXCEPTION 'ATOMIC-ROLLBACK FAIL: gate functions still present (%)', n; END IF;

  -- trg_gate_* / trg_transition 必须不存在
  SELECT count(*) INTO n FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace nn ON nn.oid=c.relnamespace
    WHERE nn.nspname='public' AND (t.tgname LIKE 'trg_gate_%' OR t.tgname='trg_transition') AND NOT t.tgisinternal;
  IF n <> 0 THEN RAISE EXCEPTION 'ATOMIC-ROLLBACK FAIL: triggers still present (%)', n; END IF;

  RAISE NOTICE 'ATOMIC-ROLLBACK PASS: no half-built m4f1 objects (clean rollback)';
END $$;
\echo ===== VERIFY-NO-M4F1 DONE =====
