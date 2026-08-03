-- tests/m4f1/sql/negatives.sql — Stage 1.2 反例:
--   64hex digest 接受 / 40hex 拒绝;snapshot UPDATE/DELETE 拒绝;skill_invocations 不可变;
--   四种跨 run 绑定全部拒绝(task_runs.active_snapshot_id / outbox snapshot / invocation snapshot / invocation job);
-- 全 ON_ERROR_STOP=1;fail() 精确 SQLSTATE(非 WHEN OTHERS 笼统 PASS);末尾 TEST-SET 双向 EXCEPT。
\set ON_ERROR_STOP on
CREATE TEMP TABLE test_results(test_id TEXT PRIMARY KEY, status TEXT);

-- setup:两个 ACTIVE run + snapshot/job 属 run A
INSERT INTO task_runs(run_id) VALUES ('n_a'),('n_b');
INSERT INTO run_snapshots(snapshot_id, run_id, manifest_digest)
  VALUES ('snapA','n_a', repeat('a',64));                       -- 64hex 接受(若拒绝则本行 FAIL)
INSERT INTO skill_job_outbox(job_id, run_id, skill_name, skill_input_digest, idempotency_key)
  VALUES ('jobA','n_a','diff-parse', repeat('b',64), 'ik_jobA');

-- 精确拒绝 helper:catch → 校验 SQLSTATE 精确等于 exp → 否则 RAISE FAIL(不笼统 PASS)
CREATE OR REPLACE FUNCTION pg_temp.fail(tid text, body text, exp_state text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE blocked boolean:=false; got text;
BEGIN
  BEGIN
    EXECUTE body;
  EXCEPTION WHEN OTHERS THEN
    got := SQLSTATE;
    IF got = exp_state THEN blocked:=true; END IF;
  END;
  IF NOT blocked THEN RAISE EXCEPTION '% FAIL: expected SQLSTATE % got %', tid, exp_state, got; END IF;
  INSERT INTO test_results VALUES (tid,'PASS') ON CONFLICT DO NOTHING;
  RAISE NOTICE '% PASS (SQLSTATE=% expected %)', tid, exp_state, exp_state;
END $$;

-- 64hex digest 接受(上面 snapA/jobA 已插入成功 → 记录)
DO $$ BEGIN INSERT INTO test_results VALUES ('N-DIGEST64-OK','PASS'); RAISE NOTICE 'N-DIGEST64-OK PASS'; END $$;

-- 40hex digest 拒绝(CHECK 23514)
SELECT pg_temp.fail('N-DIGEST40-REJECT',
  $$INSERT INTO run_snapshots(snapshot_id, run_id, manifest_digest) VALUES ('snapBad','n_a', repeat('a',40))$$,
  '23514');

-- snapshot UPDATE / DELETE 拒绝(不可变触发器 P0001)
SELECT pg_temp.fail('N-SNAP-UPDATE-REJECT',
  $$UPDATE run_snapshots SET manifest_digest = repeat('c',64) WHERE snapshot_id='snapA'$$, 'P0001');
SELECT pg_temp.fail('N-SNAP-DELETE-REJECT',
  $$DELETE FROM run_snapshots WHERE snapshot_id='snapA'$$, 'P0001');

-- skill_invocations 不可变(先插一条合法的,再 UPDATE/DELETE 拒绝)
INSERT INTO skill_invocations(invocation_id, run_id, snapshot_id, job_id, skill_name, status)
  VALUES ('invA','n_a','snapA','jobA','diff-parse','SUCCEEDED');
SELECT pg_temp.fail('N-SINV-UPDATE-REJECT',
  $$UPDATE skill_invocations SET error='x' WHERE invocation_id='invA'$$, 'P0001');
SELECT pg_temp.fail('N-SINV-DELETE-REJECT',
  $$DELETE FROM skill_invocations WHERE invocation_id='invA'$$, 'P0001');

-- 四种跨 run 绑定(用 ACTIVE run 让 gate trigger 通过,由复合 FK / snapshot trigger 拒绝)
-- (a) task_runs.active_snapshot_id = snapA(属 n_a) 但 run=n_b → 复合 FK 23503
SELECT pg_temp.fail('N-BIND-ACTIVE-SNAP',
  $$UPDATE task_runs SET active_snapshot_id='snapA' WHERE run_id='n_b'$$, '23503');
-- (b) skill_job_outbox run=n_b snapshot=snapA(属 n_a) → 复合 FK 23503(skill_job_outbox 用 _writer_gate 不校验 snapshot)
SELECT pg_temp.fail('N-BIND-OUTBOX-SNAP',
  $$INSERT INTO skill_job_outbox(job_id, run_id, snapshot_id, skill_name, skill_input_digest, idempotency_key) VALUES ('jobBad1','n_b','snapA','diff-parse', repeat('d',64), 'ik_bad1')$$, '23503');
-- (c) skill_invocations run=n_b snapshot=snapA(属 n_a) → 复合 FK 23503(_writer_gate 不校验 snapshot 绑定)
SELECT pg_temp.fail('N-BIND-SINV-SNAP',
  $$INSERT INTO skill_invocations(invocation_id, run_id, snapshot_id, job_id, skill_name, status) VALUES ('invBad1','n_b','snapA',NULL,'diff-parse','SUCCEEDED')$$, '23503');
-- (d) skill_invocations run=n_b job=jobA(属 n_a) → 复合 FK 23503
SELECT pg_temp.fail('N-BIND-SINV-JOB',
  $$INSERT INTO skill_invocations(invocation_id, run_id, snapshot_id, job_id, skill_name, status) VALUES ('invBad2','n_b',NULL,'jobA','diff-parse','SUCCEEDED')$$, '23503');

-- TEST-SET
DO $$
DECLARE expected text[] := ARRAY[
  'N-DIGEST64-OK','N-DIGEST40-REJECT','N-SNAP-UPDATE-REJECT','N-SNAP-DELETE-REJECT',
  'N-SINV-UPDATE-REJECT','N-SINV-DELETE-REJECT',
  'N-BIND-ACTIVE-SNAP','N-BIND-OUTBOX-SNAP','N-BIND-SINV-SNAP','N-BIND-SINV-JOB'];
  missing text; extra text;
BEGIN
  SELECT array_agg(x ORDER BY x) INTO missing FROM (SELECT unnest(expected) AS x EXCEPT SELECT test_id FROM test_results) q;
  SELECT array_agg(x ORDER BY x) INTO extra FROM (SELECT test_id AS x FROM test_results EXCEPT SELECT unnest(expected)) q;
  IF missing IS NOT NULL THEN RAISE EXCEPTION 'TEST-SET FAIL missing: %', missing; END IF;
  IF extra IS NOT NULL THEN RAISE EXCEPTION 'TEST-SET FAIL extra: %', extra; END IF;
  RAISE NOTICE 'TEST-SET PASS: % negative IDs exact match', array_length(expected,1);
END $$;
\echo ===== NEGATIVES ALL DONE =====
