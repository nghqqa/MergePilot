-- Purge lifecycle, reference counting, rollback, and provenance retention.
\set ON_ERROR_STOP on
CREATE TEMP TABLE qa(test_id text primary key,status text);
CREATE TEMP TABLE qe(k text primary key,v text not null);

CREATE OR REPLACE FUNCTION pg_temp.ffail(tid text,body text,frag text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE ok boolean:=false; got_state text; got_message text;
BEGIN
  BEGIN EXECUTE body;
  EXCEPTION WHEN OTHERS THEN
    got_state:=SQLSTATE; GET STACKED DIAGNOSTICS got_message=MESSAGE_TEXT;
    ok:=got_state='P0001' AND got_message LIKE '%'||frag||'%';
  END;
  IF NOT ok THEN RAISE EXCEPTION '% FAIL state=% message=%',tid,got_state,got_message; END IF;
  INSERT INTO qa VALUES(tid,'PASS'); RAISE NOTICE '% PASS',tid;
END $$;

-- Catalog: only purge_operator receives the two callable SD APIs; no PUBLIC EXECUTE.
DO $$
DECLARE names text[]:=ARRAY['request_purge','advance_purge']; bad int;
BEGIN
  SELECT count(*) INTO bad FROM unnest(names) f(name) WHERE NOT EXISTS(
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.proname=f.name AND p.prosecdef
      AND p.proowner='envelope_maint'::regrole
      AND array_position(p.proconfig,'search_path=pg_catalog') IS NOT NULL
      AND NOT EXISTS(SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                     WHERE a.grantee=0 AND a.privilege_type='EXECUTE')
      AND EXISTS(SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                 WHERE a.grantee='purge_operator'::regrole AND a.privilege_type='EXECUTE'));
  IF bad<>0 THEN RAISE EXCEPTION 'QA-CATALOG FAIL bad=%',bad; END IF;
  INSERT INTO qa VALUES('QA-CATALOG','PASS');
END $$;

-- Record unique and shared candidates before purge.
DO $$
DECLARE shared text; unique_request text; unique_output text; manifest text;
BEGIN
  SELECT request_envelope_ref INTO shared FROM public.skill_job_outbox
    WHERE run_id='pa_run3' AND skill_name='diff-parse';
  SELECT request_envelope_ref INTO unique_request FROM public.skill_job_outbox
    WHERE run_id='pa_run3' AND skill_name='risk-classify';
  SELECT output_digest INTO unique_output FROM public.skill_invocations
    WHERE run_id='pa_run3' ORDER BY invocation_id LIMIT 1;
  SELECT manifest_digest INTO manifest FROM public.run_snapshots WHERE run_id='pa_run3';
  IF shared IS NULL OR unique_request IS NULL OR unique_output IS NULL OR manifest IS NULL THEN
    RAISE EXCEPTION 'QA fixture missing';
  END IF;
  -- A different run/snapshot references one pa_run3 request digest.
  INSERT INTO public.snapshot_manifest_items(snapshot_id,ordinal,skill_name,skill_version,request_envelope_ref)
    VALUES('pa_snap1',3,'test-runner','1.0.0',shared);
  INSERT INTO qe VALUES('shared',shared),('unique_request',unique_request),
    ('unique_output',unique_output),('manifest',manifest);
END $$;

-- Execute through the real runtime role.
SET ROLE purge_operator;
SELECT public.request_purge('pa_run3','demo-operator') AS purge_id \gset
SELECT public.request_purge('pa_run3','demo-operator')=:'purge_id' AS same_request \gset
RESET ROLE;
INSERT INTO qe VALUES('purge_id',:'purge_id'),('same_request',:'same_request');
DO $$ BEGIN
  IF (SELECT v::boolean FROM qe WHERE k='same_request') IS NOT TRUE OR
     (SELECT count(*) FROM public.purge_requests WHERE run_id='pa_run3')<>1 THEN
    RAISE EXCEPTION 'QA-REQUEST-IDEMPOTENT FAIL';
  END IF;
  INSERT INTO qa VALUES('QA-REQUEST-IDEMPOTENT','PASS');
END $$;
SELECT pg_temp.ffail('QA-REQUEST-CONFLICT',
  $$SELECT public.request_purge('pa_run3','other-operator')$$,'request conflict');

SET ROLE purge_operator;
SELECT public.advance_purge(:'purge_id') AS purge_result \gset
SELECT public.advance_purge(:'purge_id') AS purge_replay \gset
RESET ROLE;
INSERT INTO qe VALUES('purge_result',:'purge_result'),('purge_replay',:'purge_replay');

DO $$
DECLARE shared text:=(SELECT v FROM qe WHERE k='shared'); unique_request text:=(SELECT v FROM qe WHERE k='unique_request');
        unique_output text:=(SELECT v FROM qe WHERE k='unique_output'); manifest text:=(SELECT v FROM qe WHERE k='manifest');
BEGIN
  IF (SELECT v FROM qe WHERE k='purge_result')<>'PURGED'
     OR (SELECT v FROM qe WHERE k='purge_replay')<>'PURGED' THEN RAISE EXCEPTION 'QA-ADVANCE result FAIL'; END IF;
  IF NOT EXISTS(SELECT 1 FROM public.task_runs WHERE run_id='pa_run3' AND skill_data_state='PURGED' AND active_snapshot_id IS NULL)
     OR EXISTS(SELECT 1 FROM public.skill_job_outbox WHERE run_id='pa_run3')
     OR EXISTS(SELECT 1 FROM public.snapshot_job_outbox WHERE run_id='pa_run3')
     OR EXISTS(SELECT 1 FROM public.run_snapshots WHERE run_id='pa_run3')
     OR EXISTS(SELECT 1 FROM public.skill_invocations WHERE run_id='pa_run3') THEN
    RAISE EXCEPTION 'QA-ADVANCE state/data FAIL';
  END IF;
  IF NOT EXISTS(SELECT 1 FROM public.envelope_store WHERE content_digest=shared)
     OR EXISTS(SELECT 1 FROM public.envelope_store WHERE content_digest IN(unique_request,unique_output,manifest)) THEN
    RAISE EXCEPTION 'QA-REFERENCE-COUNT FAIL';
  END IF;
  IF NOT EXISTS(SELECT 1 FROM public.revision_bindings WHERE run_id='pa_run3')
     OR NOT EXISTS(SELECT 1 FROM public.mcp_calls WHERE request_id IN('pa_mc3a','pa_mc3b')) THEN
    RAISE EXCEPTION 'QA-PROVENANCE-RETAIN FAIL';
  END IF;
  INSERT INTO qa VALUES('QA-ADVANCE','PASS'),('QA-ADVANCE-REPLAY','PASS'),
    ('QA-REFERENCE-COUNT','PASS'),('QA-PROVENANCE-RETAIN','PASS');
END $$;
SELECT pg_temp.ffail('QA-BIND-AFTER-PURGE',
  $$SELECT public.bind_revision('pa_run3','o/r',43,repeat('a',40),repeat('b',40),'pa_mc3a',repeat('0',64))$$,
  'not ACTIVE');

-- Inject a delete failure late in advance_purge and prove the whole transaction rolls back.
UPDATE public.task_runs SET active_snapshot_id='pa_snap1' WHERE run_id='pa_run1';
SET ROLE purge_operator;
SELECT public.request_purge('pa_run1','rollback-operator') AS rollback_purge_id \gset
RESET ROLE;
INSERT INTO qe VALUES('rollback_purge_id',:'rollback_purge_id');
CREATE OR REPLACE FUNCTION public._qa_fail_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'qa injected purge failure' USING ERRCODE='P0001'; END $$;
CREATE TRIGGER trg_qa_fail_delete BEFORE DELETE ON public.snapshot_job_outbox
  FOR EACH ROW WHEN (OLD.run_id='pa_run1') EXECUTE FUNCTION public._qa_fail_delete();
DO $$
DECLARE jobs_before int; snaps_before int; inv_before int; got text; failed boolean:=false;
BEGIN
  SELECT count(*) INTO jobs_before FROM public.skill_job_outbox WHERE run_id='pa_run1';
  SELECT count(*) INTO snaps_before FROM public.run_snapshots WHERE run_id='pa_run1';
  SELECT count(*) INTO inv_before FROM public.skill_invocations WHERE run_id='pa_run1';
  BEGIN PERFORM public.advance_purge((SELECT v FROM qe WHERE k='rollback_purge_id'));
  EXCEPTION WHEN SQLSTATE 'P0001' THEN GET STACKED DIAGNOSTICS got=MESSAGE_TEXT; failed:=got LIKE '%injected purge failure%'; END;
  IF NOT failed
     OR NOT EXISTS(SELECT 1 FROM public.task_runs WHERE run_id='pa_run1' AND skill_data_state='ACTIVE' AND active_snapshot_id='pa_snap1')
     OR NOT EXISTS(SELECT 1 FROM public.purge_requests WHERE purge_id=(SELECT v FROM qe WHERE k='rollback_purge_id') AND status='REQUESTED')
     OR (SELECT count(*) FROM public.skill_job_outbox WHERE run_id='pa_run1')<>jobs_before
     OR (SELECT count(*) FROM public.run_snapshots WHERE run_id='pa_run1')<>snaps_before
     OR (SELECT count(*) FROM public.skill_invocations WHERE run_id='pa_run1')<>inv_before THEN
    RAISE EXCEPTION 'QA-ROLLBACK FAIL failed=% message=%',failed,got;
  END IF;
  INSERT INTO qa VALUES('QA-ROLLBACK','PASS');
END $$;
DROP TRIGGER trg_qa_fail_delete ON public.snapshot_job_outbox;
DROP FUNCTION public._qa_fail_delete();

DO $$
DECLARE expected text[]:=ARRAY['QA-CATALOG','QA-REQUEST-IDEMPOTENT','QA-REQUEST-CONFLICT','QA-ADVANCE',
 'QA-ADVANCE-REPLAY','QA-REFERENCE-COUNT','QA-PROVENANCE-RETAIN','QA-BIND-AFTER-PURGE','QA-ROLLBACK'];
 missing text[]; extra text[];
BEGIN
 SELECT array_agg(v ORDER BY v) INTO missing FROM(SELECT unnest(expected)v EXCEPT SELECT test_id FROM qa)q;
 SELECT array_agg(v ORDER BY v) INTO extra FROM(SELECT test_id v FROM qa EXCEPT SELECT unnest(expected))q;
 IF missing IS NOT NULL OR extra IS NOT NULL THEN RAISE EXCEPTION 'QA-SET FAIL missing=% extra=%',missing,extra; END IF;
 RAISE NOTICE 'QA-SET PASS: % purge IDs exact match',array_length(expected,1);
END $$;
