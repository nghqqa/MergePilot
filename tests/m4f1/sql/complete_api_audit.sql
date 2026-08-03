-- Atomic snapshot and Skill completion audit.
\set ON_ERROR_STOP on
CREATE TEMP TABLE ca(test_id text primary key, status text);
CREATE TEMP TABLE cx(k text primary key, v text not null);

CREATE OR REPLACE FUNCTION pg_temp.cs(v text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN v IS NULL THEN '-1:' ELSE octet_length(v)::text||':'||v END
$$;

CREATE OR REPLACE FUNCTION pg_temp.make_request(
  p_run text,p_trace text,p_skill text,p_input jsonb) RETURNS text LANGUAGE plpgsql AS $$
DECLARE din text; rid text; body bytea;
BEGIN
  din:=encode(digest(convert_to(public.canonical_json(p_input),'UTF8'),'sha256'),'hex');
  rid:='req-'||left(encode(digest(pg_temp.cs(p_trace)||pg_temp.cs(p_run)||pg_temp.cs(p_skill)||
    pg_temp.cs('1')||pg_temp.cs(din),'sha256'),'hex'),24);
  body:=convert_to(jsonb_build_object('contract_version','1','request_id',rid,
    'trace_id',p_trace,'input',p_input)::text,'UTF8');
  RETURN public.put_envelope(body,'application/vnd.mergepilot.skill-request.v1+json');
END $$;

CREATE OR REPLACE FUNCTION pg_temp.response_bytes(
  p_name text,p_request_id text,p_trace text,p_status text,p_output jsonb,
  p_duration int DEFAULT 1) RETURNS bytea LANGUAGE sql AS $$
SELECT convert_to(jsonb_build_object(
  'name',p_name,'version','1.0.0','contract_version','1','request_id',p_request_id,
  'trace_id',p_trace,'status',p_status,
  'error_code',CASE WHEN p_status='ERROR' THEN to_jsonb('INTERNAL_ERROR'::text) ELSE 'null'::jsonb END,
  'warning_codes','[]'::jsonb,'degradations','[]'::jsonb,'message','fixture',
  'output',p_output,'evidence','[]'::jsonb,'artifacts','[]'::jsonb,
  'started_at','2026-08-02T00:00:00Z','duration_ms',p_duration,'retryable',false,
  'side_effects','[]'::jsonb,'redactions','[]'::jsonb)::text,'UTF8') $$;

-- Producer audit created pa_run3 and its immutable revision binding.
DO $$
DECLARE binding text; snap_job text; claim uuid; diff_d text; risk_d text; test_d text;
BEGIN
  SELECT binding_id INTO binding FROM public.revision_bindings WHERE run_id='pa_run3';
  diff_d:=pg_temp.make_request('pa_run3','ca_trace','diff-parse','{"f":1}'::jsonb);
  risk_d:=pg_temp.make_request('pa_run3','ca_trace','risk-classify','{"f":2}'::jsonb);
  test_d:=pg_temp.make_request('pa_run3','ca_trace','test-runner','{"f":3}'::jsonb);
  INSERT INTO cx VALUES ('binding',binding),('diff_digest',diff_d),('risk_digest',risk_d),('test_digest',test_d);
  snap_job:=public.enqueue_snapshot_job('pa_run3',binding);
  claim:=public.claim_snapshot_job(snap_job,'snapshot-completer',120);
  IF claim IS NULL THEN RAISE EXCEPTION 'CA-SETUP claim failed'; END IF;
  INSERT INTO cx VALUES ('snapshot_job',snap_job),('snapshot_claim',claim::text);
END $$;

-- Invalid revision and duplicate skill/version must fail without partial snapshot rows.
DO $$
DECLARE job text:=(SELECT v FROM cx WHERE k='snapshot_job'); claim uuid:=(SELECT v::uuid FROM cx WHERE k='snapshot_claim');
        d text:=(SELECT v FROM cx WHERE k='diff_digest'); body bytea; got text; ok boolean:=false;
BEGIN
  body:=convert_to(jsonb_build_object('manifest_version','1','run_id','pa_run3',
    'base_sha',repeat('b',40),'head_sha',repeat('c',40),'produced_at','2026-08-02T00:00:00Z',
    'items',jsonb_build_array(jsonb_build_object('kind','skill-input','skill','diff-parse','skill_version','1.0.0','digest',d)))::text,'UTF8');
  BEGIN PERFORM public.complete_snapshot_job(job,claim,body,true);
  EXCEPTION WHEN SQLSTATE 'P0001' THEN GET STACKED DIAGNOSTICS got=MESSAGE_TEXT; ok:=got LIKE '%revision sha mismatch%'; END;
  IF NOT ok OR EXISTS(SELECT 1 FROM public.run_snapshots WHERE run_id='pa_run3') THEN RAISE EXCEPTION 'CA-SNAPSHOT-REV FAIL %',got; END IF;
  INSERT INTO ca VALUES('CA-SNAPSHOT-REV','PASS');

  body:=convert_to(jsonb_build_object('manifest_version','1','run_id','pa_run3',
    'base_sha',repeat('b',40),'head_sha',repeat('a',40),'produced_at','2026-08-02T00:00:00Z',
    'items',jsonb_build_array(
      jsonb_build_object('kind','skill-input','skill','diff-parse','skill_version','1.0.0','digest',d),
      jsonb_build_object('kind','skill-input','skill','diff-parse','skill_version','1.0.0','digest',d)))::text,'UTF8');
  ok:=false; got:=NULL;
  BEGIN PERFORM public.complete_snapshot_job(job,claim,body,true);
  EXCEPTION WHEN SQLSTATE 'P0001' THEN GET STACKED DIAGNOSTICS got=MESSAGE_TEXT; ok:=got LIKE '%duplicate manifest skill/version%'; END;
  IF NOT ok OR EXISTS(SELECT 1 FROM public.run_snapshots WHERE run_id='pa_run3') THEN RAISE EXCEPTION 'CA-SNAPSHOT-DUP FAIL %',got; END IF;
  INSERT INTO ca VALUES('CA-SNAPSHOT-DUP','PASS');
END $$;

-- Correct unsorted manifest is canonicalized; reordered replay is identical.
DO $$
DECLARE job text:=(SELECT v FROM cx WHERE k='snapshot_job'); claim uuid:=(SELECT v::uuid FROM cx WHERE k='snapshot_claim');
  dd text:=(SELECT v FROM cx WHERE k='diff_digest'); rd text:=(SELECT v FROM cx WHERE k='risk_digest');
  td text:=(SELECT v FROM cx WHERE k='test_digest'); body bytea; replay_body bytea; snap text; replay text;
BEGIN
  body:=convert_to(jsonb_build_object('manifest_version','1','run_id','pa_run3',
    'base_sha',repeat('b',40),'head_sha',repeat('a',40),'produced_at','2026-08-02T00:00:00Z',
    'items',jsonb_build_array(
      jsonb_build_object('kind','skill-input','skill','test-runner','skill_version','1.0.0','digest',td),
      jsonb_build_object('kind','skill-input','skill','risk-classify','skill_version','1.0.0','digest',rd),
      jsonb_build_object('kind','skill-input','skill','diff-parse','skill_version','1.0.0','digest',dd)))::text,'UTF8');
  snap:=public.complete_snapshot_job(job,claim,body,true);
  replay_body:=convert_to(jsonb_build_object('items',jsonb_build_array(
      jsonb_build_object('digest',dd,'skill_version','1.0.0','skill','diff-parse','kind','skill-input'),
      jsonb_build_object('digest',rd,'skill_version','1.0.0','skill','risk-classify','kind','skill-input'),
      jsonb_build_object('digest',td,'skill_version','1.0.0','skill','test-runner','kind','skill-input')),
    'produced_at','2026-08-02T00:00:00Z','head_sha',repeat('a',40),'base_sha',repeat('b',40),
    'run_id','pa_run3','manifest_version','1')::text,'UTF8');
  replay:=public.complete_snapshot_job(job,claim,replay_body,true);
  IF snap IS NULL OR replay<>snap OR (SELECT active_snapshot_id FROM public.task_runs WHERE run_id='pa_run3')<>snap
     OR (SELECT count(*) FROM public.snapshot_manifest_items WHERE snapshot_id=snap)<>3
     OR (SELECT string_agg(skill_name,',' ORDER BY ordinal) FROM public.snapshot_manifest_items WHERE snapshot_id=snap)<>'diff-parse,risk-classify,test-runner'
     OR EXISTS(SELECT 1 FROM public.envelope_store e JOIN public.run_snapshots s ON s.manifest_digest=e.content_digest
               WHERE s.snapshot_id=snap AND e.content_bytes<>convert_to(public.canonical_json(e.content_json),'UTF8')) THEN
    RAISE EXCEPTION 'CA-SNAPSHOT-COMPLETE FAIL snap=% replay=%',snap,replay;
  END IF;
  INSERT INTO cx VALUES('snapshot_id',snap);
  INSERT INTO ca VALUES('CA-SNAPSHOT-COMPLETE','PASS'),('CA-SNAPSHOT-REPLAY','PASS'),('CA-MANIFEST-REL','PASS');
END $$;

-- Prepare three Skill jobs from the relational manifest.
DO $$
DECLARE snap text:=(SELECT v FROM cx WHERE k='snapshot_id'); d text; j text; c uuid;
BEGIN
  FOR d,j IN SELECT request_envelope_ref,skill_name FROM public.snapshot_manifest_items WHERE snapshot_id=snap LOOP
    j:=public.enqueue_skill_job('pa_run3',snap,'ca_trace',j,'1.0.0',1,d,'{}');
    c:=public.claim_skill_job(j,'skill-completer',120);
    IF c IS NULL THEN RAISE EXCEPTION 'CA skill claim failed %',j; END IF;
    INSERT INTO cx VALUES ('job:'||(SELECT skill_name FROM public.skill_job_outbox WHERE job_id=j),j),
      ('claim:'||(SELECT skill_name FROM public.skill_job_outbox WHERE job_id=j),c::text);
  END LOOP;
END $$;

-- diff-parse: binding/schema/status-aware negatives, then atomic success and replay.
DO $$
DECLARE job text:=(SELECT v FROM cx WHERE k='job:diff-parse'); claim uuid:=(SELECT v::uuid FROM cx WHERE k='claim:diff-parse');
  req text; schema_d text; response bytea; inv text; replay text; got text; ok boolean;
BEGIN
  SELECT e.content_json->>'request_id',r.output_schema_digest INTO req,schema_d
    FROM public.skill_job_outbox j JOIN public.envelope_store e ON e.content_digest=j.request_envelope_ref
    JOIN public.skill_version_registry r ON r.skill_name=j.skill_name AND r.skill_version=j.skill_version WHERE j.job_id=job;
  response:=pg_temp.response_bytes('diff-parse',req,'ca_trace','OK','{}'::jsonb);
  ok:=false; BEGIN PERFORM public.complete_skill_job(job,claim,response,repeat('0',64),true);
    EXCEPTION WHEN SQLSTATE 'P0001' THEN GET STACKED DIAGNOSTICS got=MESSAGE_TEXT; ok:=got LIKE '%schema registry mismatch%'; END;
  IF NOT ok THEN RAISE EXCEPTION 'CA-SKILL-SCHEMA FAIL %',got; END IF;
  ok:=false; BEGIN PERFORM public.complete_skill_job(job,claim,response,schema_d,false);
    EXCEPTION WHEN SQLSTATE 'P0001' THEN GET STACKED DIAGNOSTICS got=MESSAGE_TEXT; ok:=got LIKE '%status-aware%'; END;
  IF NOT ok THEN RAISE EXCEPTION 'CA-SKILL-STATUS FAIL %',got; END IF;
  ok:=false; BEGIN PERFORM public.complete_skill_job(job,claim,
      pg_temp.response_bytes('sast-scan',req,'ca_trace','OK','{}'::jsonb),schema_d,true);
    EXCEPTION WHEN SQLSTATE 'P0001' THEN GET STACKED DIAGNOSTICS got=MESSAGE_TEXT; ok:=got LIKE '%binding mismatch%'; END;
  IF NOT ok THEN RAISE EXCEPTION 'CA-SKILL-BIND FAIL %',got; END IF;
  inv:=public.complete_skill_job(job,claim,response,schema_d,true);
  replay:=public.complete_skill_job(job,claim,response,schema_d,true);
  IF inv IS NULL OR replay<>inv OR NOT EXISTS(SELECT 1 FROM public.skill_job_outbox WHERE job_id=job AND status='SUCCEEDED' AND result_invocation_id=inv)
     OR (SELECT count(*) FROM public.skill_invocations WHERE job_id=job)<>1 THEN RAISE EXCEPTION 'CA-SKILL-COMPLETE FAIL'; END IF;
  INSERT INTO ca VALUES('CA-SKILL-SCHEMA','PASS'),('CA-SKILL-STATUS','PASS'),('CA-SKILL-BIND','PASS'),
    ('CA-SKILL-COMPLETE','PASS'),('CA-SKILL-REPLAY','PASS');
END $$;

-- Generic runtime ERROR and structured test-runner ERROR.
DO $$
DECLARE job text; claim uuid; req text; schema_d text; inv text; got text; ok boolean;
BEGIN
  job:=(SELECT v FROM cx WHERE k='job:risk-classify'); claim:=(SELECT v::uuid FROM cx WHERE k='claim:risk-classify');
  SELECT e.content_json->>'request_id',r.output_schema_digest INTO req,schema_d
    FROM public.skill_job_outbox j JOIN public.envelope_store e ON e.content_digest=j.request_envelope_ref
    JOIN public.skill_version_registry r ON r.skill_name=j.skill_name AND r.skill_version=j.skill_version WHERE j.job_id=job;
  inv:=public.complete_skill_job(job,claim,pg_temp.response_bytes('risk-classify',req,'ca_trace','ERROR','{}'::jsonb),schema_d,false);
  IF inv IS NULL OR EXISTS(SELECT 1 FROM public.skill_invocations WHERE invocation_id=inv AND (output_schema_validated OR verdict IS NOT NULL)) THEN
    RAISE EXCEPTION 'CA-GENERIC-ERROR FAIL'; END IF;
  INSERT INTO ca VALUES('CA-GENERIC-ERROR','PASS');

  job:=(SELECT v FROM cx WHERE k='job:test-runner'); claim:=(SELECT v::uuid FROM cx WHERE k='claim:test-runner');
  SELECT e.content_json->>'request_id',r.output_schema_digest INTO req,schema_d
    FROM public.skill_job_outbox j JOIN public.envelope_store e ON e.content_digest=j.request_envelope_ref
    JOIN public.skill_version_registry r ON r.skill_name=j.skill_name AND r.skill_version=j.skill_version WHERE j.job_id=job;
  ok:=false; BEGIN PERFORM public.complete_skill_job(job,claim,pg_temp.response_bytes('test-runner',req,'ca_trace','ERROR','{}'::jsonb),schema_d,true);
    EXCEPTION WHEN SQLSTATE 'P0001' THEN GET STACKED DIAGNOSTICS got=MESSAGE_TEXT; ok:=got LIKE '%verdict required%'; END;
  IF NOT ok THEN RAISE EXCEPTION 'CA-VERDICT-REQUIRED FAIL %',got; END IF;
  inv:=public.complete_skill_job(job,claim,pg_temp.response_bytes('test-runner',req,'ca_trace','ERROR','{"verdict":"ERROR"}'::jsonb),schema_d,true);
  IF inv IS NULL OR NOT EXISTS(SELECT 1 FROM public.skill_invocations WHERE invocation_id=inv AND verdict='ERROR' AND output_schema_validated) THEN
    RAISE EXCEPTION 'CA-STRUCTURED-ERROR FAIL'; END IF;
  INSERT INTO ca VALUES('CA-VERDICT-REQUIRED','PASS'),('CA-STRUCTURED-ERROR','PASS');
END $$;

DO $$
DECLARE expected text[]:=ARRAY['CA-SNAPSHOT-REV','CA-SNAPSHOT-DUP','CA-SNAPSHOT-COMPLETE','CA-SNAPSHOT-REPLAY','CA-MANIFEST-REL',
 'CA-SKILL-SCHEMA','CA-SKILL-STATUS','CA-SKILL-BIND','CA-SKILL-COMPLETE','CA-SKILL-REPLAY',
 'CA-GENERIC-ERROR','CA-VERDICT-REQUIRED','CA-STRUCTURED-ERROR']; missing text[]; extra text[];
BEGIN
 SELECT array_agg(v ORDER BY v) INTO missing FROM (SELECT unnest(expected) v EXCEPT SELECT test_id FROM ca) q;
 SELECT array_agg(v ORDER BY v) INTO extra FROM (SELECT test_id v FROM ca EXCEPT SELECT unnest(expected)) q;
 IF missing IS NOT NULL OR extra IS NOT NULL THEN RAISE EXCEPTION 'CA-SET FAIL missing=% extra=%',missing,extra; END IF;
 RAISE NOTICE 'CA-SET PASS: % complete API IDs exact match',array_length(expected,1);
END $$;
