-- tests/m4f1/sql/producer_api_audit.sql — Stage 2.1B-1 生产者侧 SD API 验证。
-- put_envelope/bind_revision/enqueue_snapshot_job/enqueue_skill_job 正向/负向/并发。
\set ON_ERROR_STOP on
CREATE TEMP TABLE pa(test_id text primary key, status text);

-- local canon_str (replicates public._canon_str for evidence digest computation)
CREATE OR REPLACE FUNCTION pg_temp.cs(v text) RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN v IS NULL THEN '-1:' ELSE octet_length(v)::text || ':' || v END $$;

CREATE OR REPLACE FUNCTION pg_temp.ffail(tid text, body text, exp_state text, exp_frag text DEFAULT '') RETURNS void
LANGUAGE plpgsql AS $$
DECLARE b boolean:=false; g text; m text;
BEGIN BEGIN EXECUTE body; EXCEPTION WHEN OTHERS THEN g:=SQLSTATE; GET STACKED DIAGNOSTICS m=MESSAGE_TEXT;
  IF g=exp_state AND (exp_frag='' OR m LIKE exp_frag) THEN b:=true; END IF; END;
  IF NOT b THEN RAISE EXCEPTION '% FAIL exp=% frag=% got=% msg=%',tid,exp_state,exp_frag,g,m; END IF;
  INSERT INTO pa VALUES(tid,'PASS'); RAISE NOTICE '% PASS (%)',tid,exp_state;
END $$;

CREATE OR REPLACE FUNCTION pg_temp.enqueue_reject(tid text, raw_request text, exp_frag text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE dg text; before_jobs bigint; before_deps bigint; after_jobs bigint; after_deps bigint;
        got_state text; got_message text; rejected boolean:=false;
BEGIN
  dg := public.put_envelope(convert_to(raw_request,'UTF8'),
    'application/vnd.mergepilot.skill-request.v1+json');
  SELECT count(*) INTO before_jobs FROM public.skill_job_outbox;
  SELECT count(*) INTO before_deps FROM public.skill_job_dependencies;
  BEGIN
    PERFORM public.enqueue_skill_job(
      'pa_run1','pa_snap1','tr1','diff-parse','1.0.0',1,dg,'{}');
  EXCEPTION WHEN OTHERS THEN
    got_state:=SQLSTATE; GET STACKED DIAGNOSTICS got_message=MESSAGE_TEXT;
    rejected := got_state='P0001' AND got_message LIKE '%'||exp_frag||'%';
  END;
  SELECT count(*) INTO after_jobs FROM public.skill_job_outbox;
  SELECT count(*) INTO after_deps FROM public.skill_job_dependencies;
  IF NOT rejected OR before_jobs<>after_jobs OR before_deps<>after_deps THEN
    RAISE EXCEPTION '% FAIL state=% message=% jobs=%/% deps=%/%',
      tid,got_state,got_message,before_jobs,after_jobs,before_deps,after_deps;
  END IF;
  INSERT INTO pa VALUES(tid,'PASS'); RAISE NOTICE '% PASS (P0001, zero writes)',tid;
END $$;

-- ===== fixtures =====
DO $$
DECLARE bb bytea; dg text;
BEGIN
  INSERT INTO public.task_runs(run_id) VALUES ('pa_run1'),('pa_run2') ON CONFLICT DO NOTHING;
  -- run_pr_bindings for pa_run1
  INSERT INTO public.run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
    VALUES ('pa_bnd1','pa_run1','o/r',42,'fix/x','main',repeat('a',40)) ON CONFLICT DO NOTHING;
  -- mcp_calls RESULT for pa_run1 (provenance)
  INSERT INTO public.mcp_calls(request_id,correlation_id,phase,ts,caller_agent,tool,decision,run_id,target_repo,git_sha,result_status)
    VALUES ('pa_mc1','pa_corr1','RESULT',now(),'coordinator','github.get_commit','ALLOW','pa_run1','o/r',repeat('b',40),'OK') ON CONFLICT DO NOTHING;
  -- request envelope
  bb := convert_to('{"contract_version":"1","request_id":"req-523b4899a7f81fd7ecb8e16c","trace_id":"tr1","input":{"f":1}}','UTF8');
  dg := encode(digest(bb,'sha256'),'hex');
  PERFORM public.put_envelope(bb,'application/vnd.mergepilot.skill-request.v1+json');
  -- manifest envelope
  bb := convert_to('{"manifest_version":"1","run_id":"pa_run1","base_sha":"' || repeat('b',40) || '","head_sha":"' || repeat('a',40) || '","items":[]}','UTF8');
  PERFORM public.put_envelope(bb,'application/vnd.mergepilot.snapshot-manifest.v1+json');
END $$;

-- ===== 1. SD 函数 owner/prosecdef/search_path/PUBLIC EXECUTE/GRANT =====
DO $$
DECLARE bad int:=0; fn text[];
BEGIN
  fn := ARRAY['put_envelope','bind_revision','enqueue_snapshot_job','enqueue_skill_job'];
  SELECT count(*) INTO bad FROM unnest(fn) f WHERE NOT EXISTS(
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.proname=f AND p.prosecdef=true AND p.proowner='runtime_owner'::regrole
      AND array_position(p.proconfig,'search_path=pg_catalog') IS NOT NULL
      AND NOT EXISTS(SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE'));
  IF bad<>0 THEN RAISE EXCEPTION 'PA-SDCAT FAIL %',bad; END IF;
  INSERT INTO pa VALUES('PA-SDCAT','PASS'); RAISE NOTICE 'PA-SDCAT PASS';
END $$;

-- ===== 2. put_envelope tests =====
DO $$ DECLARE dg text; bb bytea; BEGIN
  -- 3 valid MIME with DIFFERENT bytes (same bytes + different MIME → P0001 per reconcile)
  bb := convert_to('{"x":1}','UTF8'); dg := encode(digest(bb,'sha256'),'hex');
  PERFORM public.put_envelope(bb,'application/vnd.mergepilot.skill-request.v1+json');
  -- idempotent (same bytes + same MIME → same digest)
  IF public.put_envelope(bb,'application/vnd.mergepilot.skill-request.v1+json') <> dg THEN RAISE EXCEPTION 'PA-PUT-IDEMPOTENT FAIL'; END IF;
  -- content_json derived
  IF NOT EXISTS(SELECT 1 FROM public.envelope_store WHERE content_digest=dg AND content_json::text LIKE '%x%') THEN RAISE EXCEPTION 'PA-PUT-JSON FAIL'; END IF;
  -- different bytes for other MIME types
  PERFORM public.put_envelope(convert_to('{"y":2}','UTF8'),'application/vnd.mergepilot.skill-response.v1+json');
  PERFORM public.put_envelope(convert_to('{"z":3}','UTF8'),'application/vnd.mergepilot.snapshot-manifest.v1+json');
  INSERT INTO pa VALUES('PA-PUT-OK','PASS'); RAISE NOTICE 'PA-PUT-OK PASS';
END $$;
-- invalid MIME → P0001
SELECT pg_temp.ffail('PA-PUT-BADMIME', $$SELECT public.put_envelope(convert_to('{}','UTF8'),'text/plain')$$,'P0001');
-- non-UTF-8 → P0001
SELECT pg_temp.ffail('PA-PUT-BADUTF8', $$SELECT public.put_envelope(decode('fffe00','hex'),'application/vnd.mergepilot.skill-request.v1+json')$$,'P0001');
-- non-JSON → P0001
SELECT pg_temp.ffail('PA-PUT-BADJSON', $$SELECT public.put_envelope(convert_to('not json','UTF8'),'application/vnd.mergepilot.skill-request.v1+json')$$,'P0001');
-- >1 MiB → P0001
SELECT pg_temp.ffail('PA-PUT-BIG', $$SELECT public.put_envelope(convert_to(repeat('x',1048577),'UTF8'),'application/vnd.mergepilot.skill-request.v1+json')$$,'P0001');

-- ===== 3. bind_revision tests =====
-- compute evidence digest for the fixture
DO $$
DECLARE v_corr text; v_tool text; v_trepo text; v_run text; v_git text; v_rs text; v_d text;
BEGIN
  SELECT correlation_id,tool,target_repo,run_id,git_sha,result_status INTO v_corr,v_tool,v_trepo,v_run,v_git,v_rs
    FROM public.mcp_calls WHERE request_id='pa_mc1';
  v_d := encode(digest(
    pg_temp.cs('pa_mc1')||pg_temp.cs(v_corr)||pg_temp.cs(v_tool)||
    pg_temp.cs(v_trepo)||pg_temp.cs(v_run)||pg_temp.cs(v_git)||pg_temp.cs(v_rs),'sha256'),'hex');
  -- first bind success
  PERFORM public.bind_revision('pa_run1','o/r',42,repeat('a',40),repeat('b',40),'pa_mc1',v_d);
  -- replay → same binding_id
  PERFORM public.bind_revision('pa_run1','o/r',42,repeat('a',40),repeat('b',40),'pa_mc1',v_d);
  INSERT INTO pa VALUES('PA-BIND-OK','PASS'); RAISE NOTICE 'PA-BIND-OK PASS';
END $$;
-- same run different payload → P0001 (validation chain rejects before conflict; provenance mismatch is correct)
SELECT pg_temp.ffail('PA-BIND-CONFLICT',
  $$SELECT public.bind_revision('pa_run1','o/r',42,repeat('a',40),repeat('c',40),'pa_mc1',repeat('d',64))$$,'P0001');

-- Two independently valid provenance payloads for one run: the second must reach
-- the revision conflict branch and must never leak 23505.
DO $$
DECLARE d1 text; d2 text; got_state text; got_message text; rejected boolean:=false;
BEGIN
  INSERT INTO public.task_runs(run_id) VALUES ('pa_run3') ON CONFLICT DO NOTHING;
  INSERT INTO public.run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
    VALUES ('pa_bnd3','pa_run3','o/r',43,'fix/z','main',repeat('a',40)) ON CONFLICT DO NOTHING;
  INSERT INTO public.mcp_calls(request_id,correlation_id,phase,ts,caller_agent,tool,decision,run_id,target_repo,git_sha,result_status)
    VALUES
      ('pa_mc3a','pa_corr3a','RESULT',now(),'coordinator','github.get_commit','ALLOW','pa_run3','o/r',repeat('b',40),'OK'),
      ('pa_mc3b','pa_corr3b','RESULT',now(),'coordinator','github.get_commit','ALLOW','pa_run3','o/r',repeat('c',40),'OK')
    ON CONFLICT DO NOTHING;
  d1 := encode(digest(pg_temp.cs('pa_mc3a')||pg_temp.cs('pa_corr3a')||pg_temp.cs('github.get_commit')||
    pg_temp.cs('o/r')||pg_temp.cs('pa_run3')||pg_temp.cs(repeat('b',40))||pg_temp.cs('OK'),'sha256'),'hex');
  d2 := encode(digest(pg_temp.cs('pa_mc3b')||pg_temp.cs('pa_corr3b')||pg_temp.cs('github.get_commit')||
    pg_temp.cs('o/r')||pg_temp.cs('pa_run3')||pg_temp.cs(repeat('c',40))||pg_temp.cs('OK'),'sha256'),'hex');
  PERFORM public.bind_revision('pa_run3','o/r',43,repeat('a',40),repeat('b',40),'pa_mc3a',d1);
  BEGIN
    PERFORM public.bind_revision('pa_run3','o/r',43,repeat('a',40),repeat('c',40),'pa_mc3b',d2);
  EXCEPTION WHEN OTHERS THEN
    got_state:=SQLSTATE; GET STACKED DIAGNOSTICS got_message=MESSAGE_TEXT;
    rejected := got_state='P0001' AND got_message LIKE '%revision binding conflict%';
  END;
  IF NOT rejected OR (SELECT count(*) FROM public.revision_bindings WHERE run_id='pa_run3')<>1 THEN
    RAISE EXCEPTION 'PA-BIND-TRUE-CONFLICT FAIL state=% message=%',got_state,got_message;
  END IF;
  INSERT INTO pa VALUES('PA-BIND-TRUE-CONFLICT','PASS');
  RAISE NOTICE 'PA-BIND-TRUE-CONFLICT PASS (P0001, one row)';
END $$;

-- ===== 4. enqueue_snapshot_job =====
DO $$ DECLARE v_job text; v_bid text; BEGIN
  SELECT binding_id INTO v_bid FROM public.revision_bindings WHERE run_id='pa_run1';
  v_job := public.enqueue_snapshot_job('pa_run1',v_bid);
  IF v_job <> 'snapjob-pa_run1' THEN RAISE EXCEPTION 'PA-ENQSNAP FAIL job=%',v_job; END IF;
  -- replay
  IF public.enqueue_snapshot_job('pa_run1',v_bid) <> v_job THEN RAISE EXCEPTION 'PA-ENQSNAP-REPLAY FAIL'; END IF;
  INSERT INTO pa VALUES('PA-ENQSNAP','PASS'); RAISE NOTICE 'PA-ENQSNAP PASS';
END $$;
-- wrong binding → P0001
SELECT pg_temp.ffail('PA-ENQSNAP-WRONG', $$SELECT public.enqueue_snapshot_job('pa_run1','nonexistent')$$,'P0001');
-- cross-run binding → P0001
DO $$ DECLARE v_bid text; BEGIN
  SELECT binding_id INTO v_bid FROM public.revision_bindings WHERE run_id='pa_run1';
  PERFORM public.enqueue_snapshot_job('pa_run2',v_bid);
  RAISE EXCEPTION 'PA-ENQSNAP-XRUN FAIL';
  EXCEPTION WHEN SQLSTATE 'P0001' THEN NULL;
END $$;

-- ===== 5. enqueue_skill_job (requires snapshot_manifest_items fixture) =====
DO $$
DECLARE v_snap text; v_req text; v_job text;
BEGIN
  -- create run_snapshots + manifest_items for enqueue_skill_job
  v_req := (SELECT content_digest FROM public.envelope_store WHERE content_type='application/vnd.mergepilot.skill-request.v1+json'
    AND content_json->>'request_id'='req-523b4899a7f81fd7ecb8e16c' LIMIT 1);
  v_snap := 'pa_snap1';
  INSERT INTO public.run_snapshots(snapshot_id,run_id,repo,pr_number,base_sha,head_sha,manifest_digest)
    SELECT v_snap,'pa_run1','o/r',42,repeat('b',40),repeat('a',40),content_digest
    FROM public.envelope_store WHERE content_type='application/vnd.mergepilot.snapshot-manifest.v1+json' LIMIT 1
    ON CONFLICT DO NOTHING;
  INSERT INTO public.snapshot_manifest_items(snapshot_id,ordinal,skill_name,skill_version,request_envelope_ref)
    VALUES (v_snap,0,'diff-parse','1.0.0',v_req) ON CONFLICT DO NOTHING;
  -- enqueue success
  v_job := public.enqueue_skill_job('pa_run1',v_snap,'tr1','diff-parse','1.0.0',1,v_req,'{}');
  IF v_job IS NULL OR left(v_job,3) <> 'sj-' THEN RAISE EXCEPTION 'PA-ENQSKILL FAIL job=%',v_job; END IF;
  -- replay
  IF public.enqueue_skill_job('pa_run1',v_snap,'tr1','diff-parse','1.0.0',1,v_req,'{}') <> v_job THEN RAISE EXCEPTION 'PA-ENQSKILL-REPLAY FAIL'; END IF;
  INSERT INTO pa VALUES('PA-ENQSKILL','PASS'); RAISE NOTICE 'PA-ENQSKILL PASS';
END $$;
-- registry not found → P0001
SELECT pg_temp.ffail('PA-ENQSKILL-NOREG',
  $$SELECT public.enqueue_skill_job('pa_run1','pa_snap1','tr1','unknown-skill','9.9.9',1,(SELECT content_digest FROM public.envelope_store LIMIT 1),'{}')$$,'P0001');
-- request not in manifest → P0001 (use different request_envelope_ref)
SELECT pg_temp.ffail('PA-ENQSKILL-NOMANIFEST',
  $$SELECT public.enqueue_skill_job('pa_run1','pa_snap1','tr1','sast-scan','1.0.0',1,(SELECT content_digest FROM public.envelope_store WHERE content_type='application/vnd.mergepilot.skill-response.v1+json' LIMIT 1),'{}')$$,'P0001');

-- request_id binds trace/run/skill/attempt/canonical input; every negative must
-- leave both the job and dependency tables unchanged.
SELECT pg_temp.enqueue_reject('PA-REQ-WEAK',
  '{"contract_version":"1","request_id":"req-","trace_id":"tr1","input":{"f":1}}','request_id mismatch');
SELECT pg_temp.enqueue_reject('PA-REQ-TRACE-DRIFT',
  '{"contract_version":"1","request_id":"req-b6fe33765e77a163265aceea","trace_id":"tr1","input":{"f":1}}','request_id mismatch');
SELECT pg_temp.enqueue_reject('PA-REQ-RUN-DRIFT',
  '{"contract_version":"1","request_id":"req-c60cb8339c0e378627cf22aa","trace_id":"tr1","input":{"f":1}}','request_id mismatch');
SELECT pg_temp.enqueue_reject('PA-REQ-SKILL-DRIFT',
  '{"contract_version":"1","request_id":"req-f05ec465f50afb2033d7eca2","trace_id":"tr1","input":{"f":1}}','request_id mismatch');
SELECT pg_temp.enqueue_reject('PA-REQ-ATTEMPT-DRIFT',
  '{"contract_version":"1","request_id":"req-3421b4ec2c4b804b1ce088a7","trace_id":"tr1","input":{"f":1}}','request_id mismatch');
SELECT pg_temp.enqueue_reject('PA-REQ-DIN-DRIFT',
  '{"contract_version":"1","request_id":"req-1874cd965398189eb2b06a6d","trace_id":"tr1","input":{"f":1}}','request_id mismatch');
SELECT pg_temp.enqueue_reject('PA-REQ-EXTRA',
  '{"contract_version":"1","request_id":"req-523b4899a7f81fd7ecb8e16c","trace_id":"tr1","input":{"f":1},"extra":true}','unknown top-level key');
SELECT pg_temp.enqueue_reject('PA-REQ-NOINPUT',
  '{"contract_version":"1","request_id":"req-523b4899a7f81fd7ecb8e16c","trace_id":"tr1"}','input missing');

-- Reordered input hashes to its canonical form and is accepted.
DO $$
DECLARE bb bytea; dg text; job text;
BEGIN
  bb:=convert_to('{"contract_version":"1","request_id":"req-2b87f9f6bf231a2652fccc2b","trace_id":"tr1","input":{"b":2,"a":1}}','UTF8');
  dg:=public.put_envelope(bb,'application/vnd.mergepilot.skill-request.v1+json');
  INSERT INTO public.snapshot_manifest_items(snapshot_id,ordinal,skill_name,skill_version,request_envelope_ref)
    VALUES ('pa_snap1',1,'sast-scan','1.0.0',dg);
  job:=public.enqueue_skill_job('pa_run1','pa_snap1','tr1','sast-scan','1.0.0',1,dg,'{}');
  IF job IS NULL THEN RAISE EXCEPTION 'PA-REQ-REORDER FAIL'; END IF;
  INSERT INTO pa VALUES('PA-REQ-REORDER','PASS'); RAISE NOTICE 'PA-REQ-REORDER PASS';
END $$;

-- Dependency sets are part of the immutable enqueue payload.
DO $$
DECLARE bb bytea; dg text; base_job text; job text; before_deps bigint;
        got_state text; got_message text; rejected boolean:=false;
BEGIN
  SELECT job_id INTO base_job FROM public.skill_job_outbox
    WHERE run_id='pa_run1' AND skill_name='diff-parse';
  bb:=convert_to('{"contract_version":"1","request_id":"req-e323e712ef2f01234033199d","trace_id":"tr1","input":{"f":3}}','UTF8');
  dg:=public.put_envelope(bb,'application/vnd.mergepilot.skill-request.v1+json');
  INSERT INTO public.snapshot_manifest_items(snapshot_id,ordinal,skill_name,skill_version,request_envelope_ref)
    VALUES ('pa_snap1',2,'risk-classify','1.0.0',dg);
  job:=public.enqueue_skill_job('pa_run1','pa_snap1','tr1','risk-classify','1.0.0',1,dg,ARRAY[base_job]);
  IF public.enqueue_skill_job('pa_run1','pa_snap1','tr1','risk-classify','1.0.0',1,dg,ARRAY[base_job])<>job THEN
    RAISE EXCEPTION 'PA-DEPS-REPLAY FAIL';
  END IF;
  SELECT count(*) INTO before_deps FROM public.skill_job_dependencies;
  BEGIN
    PERFORM public.enqueue_skill_job('pa_run1','pa_snap1','tr1','risk-classify','1.0.0',1,dg,'{}');
  EXCEPTION WHEN OTHERS THEN
    got_state:=SQLSTATE; GET STACKED DIAGNOSTICS got_message=MESSAGE_TEXT;
    rejected:=got_state='P0001' AND got_message LIKE '%dependency set conflict%';
  END;
  IF NOT rejected OR (SELECT count(*) FROM public.skill_job_dependencies)<>before_deps THEN
    RAISE EXCEPTION 'PA-DEPS-CONFLICT FAIL state=% message=%',got_state,got_message;
  END IF;
  INSERT INTO pa VALUES('PA-DEPS-REPLAY','PASS'),('PA-DEPS-CONFLICT','PASS');
  RAISE NOTICE 'PA-DEPS-REPLAY PASS'; RAISE NOTICE 'PA-DEPS-CONFLICT PASS';
END $$;

-- ===== TEST-SET =====
DO $$
DECLARE exp text[]:=ARRAY['PA-SDCAT','PA-PUT-OK','PA-PUT-BADMIME','PA-PUT-BADUTF8','PA-PUT-BADJSON','PA-PUT-BIG',
  'PA-BIND-OK','PA-BIND-CONFLICT','PA-BIND-TRUE-CONFLICT','PA-ENQSNAP','PA-ENQSNAP-WRONG',
  'PA-ENQSKILL','PA-ENQSKILL-NOREG','PA-ENQSKILL-NOMANIFEST',
  'PA-REQ-WEAK','PA-REQ-TRACE-DRIFT','PA-REQ-RUN-DRIFT','PA-REQ-SKILL-DRIFT',
  'PA-REQ-ATTEMPT-DRIFT','PA-REQ-DIN-DRIFT','PA-REQ-EXTRA','PA-REQ-NOINPUT','PA-REQ-REORDER',
  'PA-DEPS-REPLAY','PA-DEPS-CONFLICT'];
  m text; x text;
BEGIN
  SELECT array_agg(v ORDER BY v) INTO m FROM (SELECT unnest(exp) v EXCEPT SELECT test_id FROM pa) q;
  SELECT array_agg(v ORDER BY v) INTO x FROM (SELECT test_id v FROM pa EXCEPT SELECT unnest(exp)) q;
  IF m IS NOT NULL THEN RAISE EXCEPTION 'PA-SET missing %',m; END IF;
  IF x IS NOT NULL THEN RAISE EXCEPTION 'PA-SET extra %',x; END IF;
  RAISE NOTICE 'PA-SET PASS: % producer API IDs exact match', array_length(exp,1);
END $$;
\echo ===== PRODUCER API AUDIT DONE =====
