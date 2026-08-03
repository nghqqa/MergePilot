-- Worker claim/lease/heartbeat/fail state machine audit.
\set ON_ERROR_STOP on
CREATE TEMP TABLE wa(test_id text primary key, status text);

CREATE OR REPLACE FUNCTION pg_temp.ffail(tid text, body text, exp_state text, exp_frag text DEFAULT '') RETURNS void
LANGUAGE plpgsql AS $$
DECLARE ok boolean:=false; got_state text; got_message text;
BEGIN
  BEGIN EXECUTE body;
  EXCEPTION WHEN OTHERS THEN
    got_state:=SQLSTATE; GET STACKED DIAGNOSTICS got_message=MESSAGE_TEXT;
    ok:=got_state=exp_state AND (exp_frag='' OR got_message LIKE '%'||exp_frag||'%');
  END;
  IF NOT ok THEN RAISE EXCEPTION '% FAIL state=% message=%',tid,got_state,got_message; END IF;
  INSERT INTO wa VALUES(tid,'PASS'); RAISE NOTICE '% PASS',tid;
END $$;

-- Six callable worker APIs are SD/runtime_owner/search_path locked/no PUBLIC EXECUTE.
DO $$
DECLARE names text[]:=ARRAY['claim_snapshot_job','claim_skill_job','heartbeat_snapshot_job',
  'heartbeat_skill_job','fail_snapshot_job','fail_skill_job']; bad int;
BEGIN
  SELECT count(*) INTO bad FROM unnest(names) AS f(name) WHERE NOT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.proname=f.name AND p.prosecdef
      AND p.proowner='runtime_owner'::regrole
      AND array_position(p.proconfig,'search_path=pg_catalog') IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                      WHERE a.grantee=0 AND a.privilege_type='EXECUTE'));
  IF bad<>0 THEN RAISE EXCEPTION 'WA-CATALOG FAIL bad=%',bad; END IF;
  INSERT INTO wa VALUES('WA-CATALOG','PASS'); RAISE NOTICE 'WA-CATALOG PASS';
END $$;

SELECT pg_temp.ffail('WA-LEASE-LOW',
  $$SELECT public.claim_snapshot_job('snapjob-pa_run1','worker',0)$$,'P0001','invalid lease');
SELECT pg_temp.ffail('WA-LEASE-HIGH',
  $$SELECT public.claim_skill_job('missing','worker',3601)$$,'P0001','invalid lease');
SELECT pg_temp.ffail('WA-WORKER-EMPTY',
  $$SELECT public.claim_skill_job('missing','',60)$$,'P0001','invalid lease or worker');

DO $$
DECLARE c1 uuid; c2 uuid; c3 uuid; wrong uuid:=gen_random_uuid(); attempts_now int; delay_s double precision;
BEGIN
  c1:=public.claim_snapshot_job('snapjob-pa_run1','snapshot-a',60);
  IF c1 IS NULL OR public.claim_snapshot_job('snapjob-pa_run1','snapshot-b',60) IS NOT NULL THEN
    RAISE EXCEPTION 'WA-SNAPSHOT-CLAIM FAIL';
  END IF;
  SELECT attempts INTO attempts_now FROM public.snapshot_job_outbox WHERE job_id='snapjob-pa_run1';
  IF attempts_now<>1 OR public.heartbeat_snapshot_job('snapjob-pa_run1',wrong,60) THEN
    RAISE EXCEPTION 'WA-SNAPSHOT-CAS FAIL';
  END IF;
  IF NOT public.heartbeat_snapshot_job('snapjob-pa_run1',c1,60) THEN
    RAISE EXCEPTION 'WA-SNAPSHOT-HEARTBEAT FAIL';
  END IF;
  IF public.fail_snapshot_job('snapjob-pa_run1',wrong,'wrong') OR
     NOT public.fail_snapshot_job('snapjob-pa_run1',c1,'attempt one') THEN
    RAISE EXCEPTION 'WA-SNAPSHOT-FAIL1 FAIL';
  END IF;
  SELECT extract(epoch FROM next_retry_at-now()) INTO delay_s
    FROM public.snapshot_job_outbox WHERE job_id='snapjob-pa_run1';
  IF delay_s NOT BETWEEN 1.0 AND 3.0 THEN RAISE EXCEPTION 'WA-BACKOFF-2 FAIL delay=%',delay_s; END IF;

  UPDATE public.snapshot_job_outbox SET next_retry_at=now()-interval '1 second' WHERE job_id='snapjob-pa_run1';
  c2:=public.claim_snapshot_job('snapjob-pa_run1','snapshot-a',60);
  IF c2 IS NULL OR c2=c1 THEN RAISE EXCEPTION 'WA-SNAPSHOT-CLAIM2 FAIL'; END IF;
  IF NOT public.fail_snapshot_job('snapjob-pa_run1',c2,'attempt two') THEN RAISE EXCEPTION 'WA-SNAPSHOT-FAIL2 FAIL'; END IF;
  SELECT extract(epoch FROM next_retry_at-now()) INTO delay_s
    FROM public.snapshot_job_outbox WHERE job_id='snapjob-pa_run1';
  IF delay_s NOT BETWEEN 3.0 AND 5.0 THEN RAISE EXCEPTION 'WA-BACKOFF-4 FAIL delay=%',delay_s; END IF;

  UPDATE public.snapshot_job_outbox SET next_retry_at=now()-interval '1 second' WHERE job_id='snapjob-pa_run1';
  c3:=public.claim_snapshot_job('snapjob-pa_run1','snapshot-a',60);
  IF c3 IS NULL OR c3 IN (c1,c2) THEN RAISE EXCEPTION 'WA-SNAPSHOT-CLAIM3 FAIL'; END IF;
  IF NOT public.fail_snapshot_job('snapjob-pa_run1',c3,'attempt three') THEN RAISE EXCEPTION 'WA-SNAPSHOT-FAIL3 FAIL'; END IF;
  IF EXISTS (SELECT 1 FROM public.snapshot_job_outbox WHERE job_id='snapjob-pa_run1'
             AND (status<>'FAILED' OR attempts<>3 OR claim_id IS NOT NULL)) OR
     public.claim_snapshot_job('snapjob-pa_run1','snapshot-a',60) IS NOT NULL THEN
    RAISE EXCEPTION 'WA-SNAPSHOT-TERMINAL FAIL';
  END IF;
  INSERT INTO wa VALUES
    ('WA-SNAPSHOT-CLAIM','PASS'),('WA-SNAPSHOT-CAS','PASS'),
    ('WA-BACKOFF-2','PASS'),('WA-BACKOFF-4','PASS'),('WA-SNAPSHOT-TERMINAL','PASS');
  RAISE NOTICE 'WA-SNAPSHOT STATE MACHINE PASS';
END $$;

DO $$
DECLARE dep_job text; risk_job text; sast_job text; c uuid; stale uuid:=gen_random_uuid(); before_attempts int;
        stale_result uuid; stale_status text; stale_claim uuid; stale_attempts int;
BEGIN
  SELECT job_id INTO dep_job FROM public.skill_job_outbox WHERE run_id='pa_run1' AND skill_name='diff-parse';
  SELECT job_id INTO risk_job FROM public.skill_job_outbox WHERE run_id='pa_run1' AND skill_name='risk-classify';
  SELECT job_id INTO sast_job FROM public.skill_job_outbox WHERE run_id='pa_run1' AND skill_name='sast-scan';
  IF public.claim_skill_job(risk_job,'skill-a',60) IS NOT NULL THEN
    RAISE EXCEPTION 'WA-DEPS-BLOCK FAIL';
  END IF;
  c:=public.claim_skill_job(dep_job,'skill-a',60);
  IF c IS NULL THEN RAISE EXCEPTION 'WA-SKILL-CLAIM FAIL'; END IF;
  SELECT attempts INTO before_attempts FROM public.skill_job_outbox WHERE job_id=dep_job;
  IF NOT public.heartbeat_skill_job(dep_job,c,60) OR
     (SELECT attempts FROM public.skill_job_outbox WHERE job_id=dep_job)<>before_attempts THEN
    RAISE EXCEPTION 'WA-SKILL-HEARTBEAT FAIL';
  END IF;
  UPDATE public.skill_job_outbox SET status='SUCCEEDED',claim_id=NULL,leased_by=NULL,
    lease_expires_at=NULL,completed_at=now() WHERE job_id=dep_job;
  c:=public.claim_skill_job(risk_job,'skill-b',60);
  IF c IS NULL THEN RAISE EXCEPTION 'WA-DEPS-UNBLOCK FAIL'; END IF;

  UPDATE public.skill_job_outbox SET status='LEASED',claim_id=stale,leased_by='stale',
    lease_expires_at=now()-interval '1 second',attempts=max_attempts WHERE job_id=sast_job;
  stale_result:=public.claim_skill_job(sast_job,'skill-c',60);
  SELECT status,claim_id,attempts INTO stale_status,stale_claim,stale_attempts
    FROM public.skill_job_outbox WHERE job_id=sast_job;
  IF stale_result IS NOT NULL OR stale_status<>'FAILED' OR stale_claim IS NOT NULL THEN
    RAISE EXCEPTION 'WA-STALE-EXHAUSTED FAIL result=% status=% claim=% attempts=%',
      stale_result,stale_status,stale_claim,stale_attempts;
  END IF;
  INSERT INTO wa VALUES('WA-DEPS-BLOCK','PASS'),('WA-SKILL-HEARTBEAT','PASS'),
    ('WA-DEPS-UNBLOCK','PASS'),('WA-STALE-EXHAUSTED','PASS');
  RAISE NOTICE 'WA-SKILL STATE MACHINE PASS';
END $$;

DO $$
DECLARE expected text[]:=ARRAY['WA-CATALOG','WA-LEASE-LOW','WA-LEASE-HIGH','WA-WORKER-EMPTY',
 'WA-SNAPSHOT-CLAIM','WA-SNAPSHOT-CAS','WA-BACKOFF-2','WA-BACKOFF-4','WA-SNAPSHOT-TERMINAL',
 'WA-DEPS-BLOCK','WA-SKILL-HEARTBEAT','WA-DEPS-UNBLOCK','WA-STALE-EXHAUSTED']; missing text[]; extra text[];
BEGIN
  SELECT array_agg(v ORDER BY v) INTO missing FROM (SELECT unnest(expected) v EXCEPT SELECT test_id FROM wa) q;
  SELECT array_agg(v ORDER BY v) INTO extra FROM (SELECT test_id v FROM wa EXCEPT SELECT unnest(expected)) q;
  IF missing IS NOT NULL OR extra IS NOT NULL THEN RAISE EXCEPTION 'WA-SET FAIL missing=% extra=%',missing,extra; END IF;
  RAISE NOTICE 'WA-SET PASS: % worker IDs exact match',array_length(expected,1);
END $$;
