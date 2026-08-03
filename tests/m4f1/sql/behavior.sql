-- tests/m4f1/sql/behavior.sql — ACTIVE/PURGING/PURGED + snapshot binding + rollback key-change.
-- 精确 SQLSTATE + MESSAGE_TEXT(blocked flag);ON_ERROR_STOP=1;末尾 TEST-SET 双向 EXCEPT。
\set ON_ERROR_STOP on
CREATE TEMP TABLE test_results(test_id TEXT PRIMARY KEY, status TEXT);

-- 测试数据(独立 run,互不干扰)
INSERT INTO task_runs(run_id) VALUES ('m4f1_b_active');                       -- ACTIVE(default)
INSERT INTO task_runs(run_id, skill_data_state) VALUES ('m4f1_b_purg','PURGING'),('m4f1_b_purged','PURGED');
INSERT INTO task_runs(run_id) VALUES ('m4f1_b_a'),('m4f1_b_b'),('m4f1_b_snap');
INSERT INTO run_snapshots(snapshot_id, run_id, manifest_digest)
  VALUES ('m4f1_snap1','m4f1_b_snap', repeat('a',64));

-- 临时 writer(写 skill_job_outbox / snapshot_job_outbox / rollback_runs)
-- 给 UPDATE(skill_data_state) 以便穿过权限检查、让 transition trigger 抛 'by <user>'(证明 trigger 是权威)
CREATE ROLE _b_writer LOGIN NOSUPERUSER;
GRANT INSERT ON skill_job_outbox TO _b_writer;
GRANT INSERT ON snapshot_job_outbox TO _b_writer;
GRANT INSERT ON rollback_runs TO _b_writer;
GRANT SELECT ON task_runs TO _b_writer;
GRANT UPDATE (skill_data_state) ON task_runs TO _b_writer;

-- helper:精确拒绝(blocked flag,无 WHEN OTHERS PASS)
CREATE OR REPLACE FUNCTION pg_temp.rej(tid text, body text, exp_state text, exp_msg text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE blocked boolean:=false; msg text;
BEGIN
  BEGIN
    EXECUTE body;
  EXCEPTION WHEN SQLSTATE '42501' OR SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS msg = MESSAGE_TEXT;
    IF left(msg, length(exp_msg)) = exp_msg THEN blocked:=true; END IF;
  END;
  IF NOT blocked THEN RAISE EXCEPTION '% FAIL: expected SQLSTATE % msg=%, got blocked=% msg=%', tid, exp_state, exp_msg, blocked, msg; END IF;
  INSERT INTO test_results VALUES (tid,'PASS') ON CONFLICT DO NOTHING;
  RAISE NOTICE '% PASS: msg=%', tid, msg;
END $$;

-- ===== ACTIVE: 写 skill_job_outbox 成功 =====
DO $$ BEGIN
  SET ROLE _b_writer;
  INSERT INTO skill_job_outbox(job_id, run_id, skill_name, skill_input_digest, idempotency_key)
    VALUES ('m4f1_sj1','m4f1_b_active','diff-parse', repeat('a',64), 'ik_sj1');
  RESET ROLE;
  INSERT INTO test_results VALUES ('B-ACTIVE-WRITE','PASS');
  RAISE NOTICE 'B-ACTIVE-WRITE PASS';
END $$;

-- ===== PURGING: 写被拦 'is PURGING' =====
DO $$ DECLARE blocked boolean:=false; msg text;
BEGIN
  SET ROLE _b_writer;
  BEGIN
    INSERT INTO skill_job_outbox(job_id, run_id, skill_name, skill_input_digest, idempotency_key)
      VALUES ('m4f1_sj2','m4f1_b_purg','diff-parse', repeat('b',64), 'ik_sj2');
  EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS msg = MESSAGE_TEXT;
    IF msg = 'is PURGING' THEN blocked:=true; END IF;
  END;
  RESET ROLE;
  IF NOT blocked THEN RAISE EXCEPTION 'B-PURGING-BLOCK FAIL: blocked=% msg=%', blocked, msg; END IF;
  INSERT INTO test_results VALUES ('B-PURGING-BLOCK','PASS');
  RAISE NOTICE 'B-PURGING-BLOCK PASS: msg=%', msg;
END $$;

-- ===== PURGED: 写被拦 'is PURGED' =====
DO $$ DECLARE blocked boolean:=false; msg text;
BEGIN
  SET ROLE _b_writer;
  BEGIN
    INSERT INTO skill_job_outbox(job_id, run_id, skill_name, skill_input_digest, idempotency_key)
      VALUES ('m4f1_sj3','m4f1_b_purged','diff-parse', repeat('c',64), 'ik_sj3');
  EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS msg = MESSAGE_TEXT;
    IF msg = 'is PURGED' THEN blocked:=true; END IF;
  END;
  RESET ROLE;
  IF NOT blocked THEN RAISE EXCEPTION 'B-PURGED-BLOCK FAIL: blocked=% msg=%', blocked, msg; END IF;
  INSERT INTO test_results VALUES ('B-PURGED-BLOCK','PASS');
  RAISE NOTICE 'B-PURGED-BLOCK PASS: msg=%', msg;
END $$;

-- ===== 非 envelope_maint 转 skill_data_state 被拦 'by <user>' =====
DO $$ DECLARE blocked boolean:=false; msg text; v text;
BEGIN
  SET ROLE _b_writer;
  BEGIN
    UPDATE task_runs SET skill_data_state='PURGING' WHERE run_id='m4f1_b_active';
  EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS msg = MESSAGE_TEXT;
    IF msg LIKE 'by %' THEN blocked:=true; END IF;
  END;
  RESET ROLE;
  SELECT skill_data_state INTO v FROM task_runs WHERE run_id='m4f1_b_active';
  IF NOT blocked THEN RAISE EXCEPTION 'B-TRANSITION-BLOCK FAIL: blocked=% msg=%', blocked, msg; END IF;
  IF v <> 'ACTIVE' THEN RAISE EXCEPTION 'B-TRANSITION-BLOCK FAIL: state mutated to %', v; END IF;
  INSERT INTO test_results VALUES ('B-TRANSITION-BLOCK','PASS');
  RAISE NOTICE 'B-TRANSITION-BLOCK PASS: msg=% val=%', msg, v;
END $$;

-- ===== envelope_maint 合法转换 ACTIVE→PURGING→PURGED =====
DO $$
DECLARE v1 text; v2 text;
BEGIN
  SET ROLE envelope_maint;
  UPDATE task_runs SET skill_data_state='PURGING' WHERE run_id='m4f1_b_active';
  GET DIAGNOSTICS v1 = ROW_COUNT;  -- not used; just to consume
  SELECT skill_data_state INTO v1 FROM task_runs WHERE run_id='m4f1_b_active';
  UPDATE task_runs SET skill_data_state='PURGED' WHERE run_id='m4f1_b_active' AND skill_data_state='PURGING';
  RESET ROLE;
  SELECT skill_data_state INTO v2 FROM task_runs WHERE run_id='m4f1_b_active';
  IF v1 <> 'PURGING' OR v2 <> 'PURGED' THEN RAISE EXCEPTION 'B-TRANSITION-OK FAIL: v1=% v2=%', v1, v2; END IF;
  INSERT INTO test_results VALUES ('B-TRANSITION-OK','PASS');
  RAISE NOTICE 'B-TRANSITION-OK PASS: PURGING→PURGED';
END $$;

-- ===== snapshot binding:匹配 OK / 不匹配 'snap mismatch' / 缺失 'snap nf' =====
DO $$ BEGIN
  SET ROLE _b_writer;
  INSERT INTO snapshot_job_outbox(job_id, run_id, snapshot_id, idempotency_key)
    VALUES ('m4f1_sjo1','m4f1_b_snap','m4f1_snap1','ik_sjo1');  -- 匹配 OK
  RESET ROLE;
  INSERT INTO test_results VALUES ('B-SNAP-OK','PASS');
  RAISE NOTICE 'B-SNAP-OK PASS';
END $$;
DO $$ DECLARE blocked boolean:=false; msg text;
BEGIN
  SET ROLE _b_writer;
  BEGIN
    INSERT INTO snapshot_job_outbox(job_id, run_id, snapshot_id, idempotency_key)
      VALUES ('m4f1_sjo2','m4f1_b_a','m4f1_snap1','ik_sjo2');  -- snapshot 属 m4f1_b_snap,不属于 m4f1_b_a
  EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS msg = MESSAGE_TEXT;
    IF msg = 'snap mismatch' THEN blocked:=true; END IF;
  END;
  RESET ROLE;
  IF NOT blocked THEN RAISE EXCEPTION 'B-SNAP-MISMATCH FAIL: blocked=% msg=%', blocked, msg; END IF;
  INSERT INTO test_results VALUES ('B-SNAP-MISMATCH','PASS');
  RAISE NOTICE 'B-SNAP-MISMATCH PASS: msg=%', msg;
END $$;
DO $$ DECLARE blocked boolean:=false; msg text;
BEGIN
  SET ROLE _b_writer;
  BEGIN
    INSERT INTO snapshot_job_outbox(job_id, run_id, snapshot_id, idempotency_key)
      VALUES ('m4f1_sjo3','m4f1_b_a','m4f1_snap_nope','ik_sjo3');  -- snapshot 不存在
  EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS msg = MESSAGE_TEXT;
    IF msg = 'snap nf' THEN blocked:=true; END IF;
  END;
  RESET ROLE;
  IF NOT blocked THEN RAISE EXCEPTION 'B-SNAP-NF FAIL: blocked=% msg=%', blocked, msg; END IF;
  INSERT INTO test_results VALUES ('B-SNAP-NF','PASS');
  RAISE NOTICE 'B-SNAP-NF PASS: msg=%', msg;
END $$;

-- ===== rollback key-change: UPDATE revert_run_id 被拦 'kc' =====
DO $$
DECLARE rid text; blocked boolean:=false; msg text; nr text;
BEGIN
  SET ROLE _b_writer;
  INSERT INTO rollback_runs(rollback_id, parent_run_id, revert_run_id, reverted_merge_sha, repo, pr_number, trigger_event_id)
    VALUES ('m4f1_rb1','m4f1_b_a','m4f1_b_b', repeat('a',40), 'o/r', 1, 'ev-rb1');
  RESET ROLE;
  SELECT rollback_id INTO rid FROM rollback_runs WHERE rollback_id='m4f1_rb1';
  GRANT UPDATE, SELECT ON rollback_runs TO _b_writer;
  SET ROLE _b_writer;
  BEGIN
    UPDATE rollback_runs SET revert_run_id='m4f1_b_active' WHERE rollback_id=rid;
  EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS msg = MESSAGE_TEXT;
    IF msg = 'kc' THEN blocked:=true; END IF;
  END;
  RESET ROLE;
  SELECT revert_run_id INTO nr FROM rollback_runs WHERE rollback_id=rid;
  IF NOT blocked THEN RAISE EXCEPTION 'B-RB-KEYCHANGE FAIL: blocked=% msg=%', blocked, msg; END IF;
  IF nr IS NULL OR nr <> 'm4f1_b_b' THEN RAISE EXCEPTION 'B-RB-KEYCHANGE FAIL: key changed to %', nr; END IF;
  INSERT INTO test_results VALUES ('B-RB-KEYCHANGE','PASS');
  RAISE NOTICE 'B-RB-KEYCHANGE PASS: msg=% new_r=%', msg, nr;
END $$;

-- ===== TEST-SET 双向 EXCEPT =====
DO $$
DECLARE expected text[] := ARRAY[
  'B-ACTIVE-WRITE','B-PURGING-BLOCK','B-PURGED-BLOCK','B-TRANSITION-BLOCK','B-TRANSITION-OK',
  'B-SNAP-OK','B-SNAP-MISMATCH','B-SNAP-NF','B-RB-KEYCHANGE'];
  missing text; extra text;
BEGIN
  SELECT array_agg(x ORDER BY x) INTO missing FROM (SELECT unnest(expected) AS x EXCEPT SELECT test_id FROM test_results) d;
  SELECT array_agg(x ORDER BY x) INTO extra FROM (SELECT test_id AS x FROM test_results EXCEPT SELECT unnest(expected)) d;
  IF missing IS NOT NULL THEN RAISE EXCEPTION 'TEST-SET FAIL missing: %', missing; END IF;
  IF extra IS NOT NULL THEN RAISE EXCEPTION 'TEST-SET FAIL extra: %', extra; END IF;
  RAISE NOTICE 'TEST-SET PASS: % behavior IDs exact match', array_length(expected,1);
END $$;

-- cleanup test roles/grants(DROP OWNED BY 清所有依赖,再 DROP ROLE)
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM _b_writer;
DROP OWNED BY _b_writer;
DROP ROLE IF EXISTS _b_writer;

\echo ===== BEHAVIOR ALL DONE =====
