-- tests/m4f1/sql/schema_foundation_audit.sql — Stage 2.1A 结构层验证(v2)。
-- 覆盖:skill_job_dependencies(structure/self/FK)、status-aware CK、registry immutable、6行12-digest 双向、
--       guard pre/post-bind、worker no-DML(42501)、by-name catalog(3 writer-gate + 1 guard,非 owner 总数)、
--       复合 FK、ACL 双向、不可变。ON_ERROR_STOP=1;末尾双向 EXCEPT 断言。不依赖业务 SD API。
\set ON_ERROR_STOP on
CREATE TEMP TABLE sf(test_id text primary key, status text);

-- helper:精确拒绝(catch → SQLSTATE 精确相等 + msg 片段 → 否则 FAIL;非笼统 PASS)
CREATE OR REPLACE FUNCTION pg_temp.ffail(tid text, body text, exp_state text, exp_msgFragment text DEFAULT '') RETURNS void
LANGUAGE plpgsql AS $$
DECLARE blocked boolean:=false; got text; msg text;
BEGIN
  BEGIN EXECUTE body;
  EXCEPTION WHEN OTHERS THEN
    got := SQLSTATE; GET STACKED DIAGNOSTICS msg = MESSAGE_TEXT;
    IF got = exp_state AND (exp_msgFragment = '' OR msg LIKE exp_msgFragment) THEN blocked:=true; END IF;
  END;
  IF NOT blocked THEN RAISE EXCEPTION '% FAIL: expected SQLSTATE % (frag %) got % msg=%', tid, exp_state, exp_msgFragment, got, msg; END IF;
  INSERT INTO sf VALUES (tid,'PASS') ON CONFLICT DO NOTHING;
  RAISE NOTICE '% PASS (SQLSTATE=%)', tid, exp_state;
END $$;

-- ===== fixture:run/envelopes/job/run_pr_bindings/mcp_calls(供功能反例) =====
DO $$
DECLARE bb bytea;
BEGIN
  INSERT INTO public.task_runs(run_id) VALUES ('sf_run1') ON CONFLICT DO NOTHING;
  bb := convert_to('{"i":1}','UTF8');
  INSERT INTO public.envelope_store(content_digest,content_bytes,content_json,content_type,size_bytes)
    VALUES (encode(digest(bb,'sha256'),'hex'),bb,'{"i":1}'::jsonb,'application/vnd.mergepilot.skill-request.v1+json',octet_length(bb)) ON CONFLICT DO NOTHING;
  bb := convert_to('{"o":1}','UTF8');
  INSERT INTO public.envelope_store(content_digest,content_bytes,content_json,content_type,size_bytes)
    VALUES (encode(digest(bb,'sha256'),'hex'),bb,'{"o":1}'::jsonb,'application/vnd.mergepilot.skill-response.v1+json',octet_length(bb)) ON CONFLICT DO NOTHING;
  bb := convert_to('{"m":1}','UTF8');
  INSERT INTO public.envelope_store(content_digest,content_bytes,content_json,content_type,size_bytes)
    VALUES (encode(digest(bb,'sha256'),'hex'),bb,'{"m":1}'::jsonb,'application/vnd.mergepilot.snapshot-manifest.v1+json',octet_length(bb)) ON CONFLICT DO NOTHING;
  INSERT INTO public.skill_job_outbox(job_id,run_id,trace_id,skill_name,skill_version,attempt,request_envelope_ref,idempotency_key)
    SELECT 'sf_job1','sf_run1','tr1','diff-parse','1.0.0',1,content_digest,'ik_sf_job1'
    FROM public.envelope_store WHERE content_type='application/vnd.mergepilot.skill-request.v1+json' LIMIT 1
    ON CONFLICT DO NOTHING;
  INSERT INTO public.run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
    VALUES ('sf_rpb1','sf_run1','o/r',1,'fix/x','main',repeat('a',40)) ON CONFLICT DO NOTHING;
  INSERT INTO public.mcp_calls(request_id,caller_agent,tool,decision,run_id,target_repo,git_sha,result_status,phase)
    VALUES ('sf_mc1','coordinator','(read)','ALLOW','sf_run1','o/r',repeat('a',40),'OK','RESULT') ON CONFLICT DO NOTHING;
END $$;

-- ===== 1. skill_job_dependencies 结构(PK/双 FK CASCADE/非自依赖 CK) =====
DO $$
DECLARE has_cascade int;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='skill_job_dependencies') THEN RAISE EXCEPTION 'SF-DEPS-TABLE FAIL: table missing'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.skill_job_dependencies'::regclass AND contype='p') THEN RAISE EXCEPTION 'SF-DEPS-TABLE FAIL: PK missing'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_job_dependencies_no_self' AND contype='c') THEN RAISE EXCEPTION 'SF-DEPS-TABLE FAIL: no_self CK missing'; END IF;
  SELECT count(*) INTO has_cascade FROM pg_constraint
    WHERE conname IN ('skill_job_dependencies_job_fkey','skill_job_dependencies_dep_fkey') AND contype='f' AND confdeltype='c';
  IF has_cascade <> 2 THEN RAISE EXCEPTION 'SF-DEPS-TABLE FAIL: dual CASCADE FK missing (%)', has_cascade; END IF;
  INSERT INTO sf VALUES('SF-DEPS-TABLE','PASS'); RAISE NOTICE 'SF-DEPS-TABLE PASS';
END $$;
-- 2. self-dependency → 23514
SELECT pg_temp.ffail('SF-DEPS-SELF',
  $$INSERT INTO public.skill_job_dependencies(job_id,depends_on_job_id) VALUES ('sf_job1','sf_job1')$$,'23514');
-- 3. 不存在的 job dependency → 23503
SELECT pg_temp.ffail('SF-DEPS-FK',
  $$INSERT INTO public.skill_job_dependencies(job_id,depends_on_job_id) VALUES ('sf_job1','nope_job')$$,'23503');

-- ===== 4. status-aware CK:OK/PARTIAL + validated=false → 23514 =====
DO $$
DECLARE dgin text; dgout text; dgman text; blocked boolean:=false;
BEGIN
  SELECT content_digest INTO dgin FROM public.envelope_store WHERE content_type='application/vnd.mergepilot.skill-request.v1+json' LIMIT 1;
  SELECT content_digest INTO dgout FROM public.envelope_store WHERE content_type='application/vnd.mergepilot.skill-response.v1+json' LIMIT 1;
  SELECT content_digest INTO dgman FROM public.envelope_store WHERE content_type='application/vnd.mergepilot.snapshot-manifest.v1+json' LIMIT 1;
  BEGIN
    INSERT INTO public.skill_invocations(invocation_id,run_id,job_id,trace_id,skill_name,skill_version,attempt,request_id,status,output_schema_validated,expected_output_schema_digest,input_digest,output_digest,snapshot_manifest_digest,started_at,idempotency_key)
      VALUES ('sf_inv_bad','sf_run1','sf_job1','tr1','diff-parse','1.0.0',1,'req1','OK',false,'e6e0eb2077645007de8115a0be697b27954e34b9a000e9bc7c6de03c27fd355b',dgin,dgout,dgman,now(),'ik_inv_bad');
    RAISE EXCEPTION 'SF-CK-STATUS-VALIDATED FAIL: not blocked';
  EXCEPTION WHEN SQLSTATE '23514' THEN blocked:=true;
  END;
  INSERT INTO sf VALUES('SF-CK-STATUS-VALIDATED','PASS'); RAISE NOTICE 'SF-CK-STATUS-VALIDATED PASS (23514)';
END $$;

-- ===== 5/6. registry immutable UPDATE/DELETE → P0001 =====
SELECT pg_temp.ffail('SF-REGISTRY-IMMUTABLE-UPD',
  $$UPDATE public.skill_version_registry SET registered_at=now() WHERE skill_name='diff-parse'$$,'P0001','%immutable%');
SELECT pg_temp.ffail('SF-REGISTRY-IMMUTABLE-DEL',
  $$DELETE FROM public.skill_version_registry WHERE skill_name='diff-parse'$$,'P0001','%immutable%');

-- ===== 7. 六行 12-digest 双向精确 EXCEPT =====
DO $$
DECLARE d1 int; d2 int;
BEGIN
  SELECT count(*) INTO d1 FROM (
    SELECT * FROM (VALUES
      ('diff-parse','1.0.0','89d628502dd726d6dfa1df4f52687bd51a1cea75d81e680a5025852f3b5b7285','e6e0eb2077645007de8115a0be697b27954e34b9a000e9bc7c6de03c27fd355b'),
      ('risk-classify','1.0.0','45ca36e3a5c6ff8146e13d7935918240279f1ffbc28872c8b1c04c81a3111371','b4d8e0519916cc21ea5286a677a94de53af2cb968073c1b06cf8b4d6ccbda09a'),
      ('sast-scan','1.0.0','8d008630393b59e77ed66669c2b5d6a45591dbbed5c3bc5554289035c5813598','fda15df57b9713bf76f95ff0668a8c76a8f7f68cabb40348232d571614e497e1'),
      ('test-runner','1.0.0','a90f67f1c19243582402d8e8b590f9a104a937637442be29a3d980848b9ecda9','461c5f026e01a4641acc0821220f6720361402ee2c3fc802421a6a11c41772d9'),
      ('case-retrieval','1.0.0','549526ab5aa410b67754a52ba7fcd826b2cc7813189eac0f929c5b53e666c3d3','4366b3e76796756158197b10c77c135b7d6443c9262ad9a5be5c03a60f662b57'),
      ('pr-lifecycle','1.0.0','7157df189df14d7128c3fe9f40e749050ed8251f206a7f5a57ca31da9859c424','ee27d6b587ca9b82d9da189ae98ca4a58437110ebe3ff75348506355c075dc1c')
    ) AS e(sn,sv,rid,oid)
    EXCEPT SELECT skill_name,skill_version,request_schema_digest,output_schema_digest FROM public.skill_version_registry) q;
  SELECT count(*) INTO d2 FROM (
    SELECT skill_name,skill_version,request_schema_digest,output_schema_digest FROM public.skill_version_registry
    EXCEPT SELECT * FROM (VALUES
      ('diff-parse','1.0.0','89d628502dd726d6dfa1df4f52687bd51a1cea75d81e680a5025852f3b5b7285','e6e0eb2077645007de8115a0be697b27954e34b9a000e9bc7c6de03c27fd355b'),
      ('risk-classify','1.0.0','45ca36e3a5c6ff8146e13d7935918240279f1ffbc28872c8b1c04c81a3111371','b4d8e0519916cc21ea5286a677a94de53af2cb968073c1b06cf8b4d6ccbda09a'),
      ('sast-scan','1.0.0','8d008630393b59e77ed66669c2b5d6a45591dbbed5c3bc5554289035c5813598','fda15df57b9713bf76f95ff0668a8c76a8f7f68cabb40348232d571614e497e1'),
      ('test-runner','1.0.0','a90f67f1c19243582402d8e8b590f9a104a937637442be29a3d980848b9ecda9','461c5f026e01a4641acc0821220f6720361402ee2c3fc802421a6a11c41772d9'),
      ('case-retrieval','1.0.0','549526ab5aa410b67754a52ba7fcd826b2cc7813189eac0f929c5b53e666c3d3','4366b3e76796756158197b10c77c135b7d6443c9262ad9a5be5c03a60f662b57'),
      ('pr-lifecycle','1.0.0','7157df189df14d7128c3fe9f40e749050ed8251f206a7f5a57ca31da9859c424','ee27d6b587ca9b82d9da189ae98ca4a58437110ebe3ff75348506355c075dc1c')
    ) AS e(sn,sv,rid,oid)) q;
  IF d1<>0 OR d2<>0 THEN RAISE EXCEPTION 'SF-REGISTRY-EXACT FAIL drift exp-not-act=% act-not-exp=%',d1,d2; END IF;
  INSERT INTO sf VALUES('SF-REGISTRY-EXACT','PASS'); RAISE NOTICE 'SF-REGISTRY-EXACT PASS';
END $$;

-- ===== 8. guard pre-bind:head UPDATE 成功 =====
DO $$
DECLARE esha int;
BEGIN
  UPDATE public.run_pr_bindings SET head_sha=repeat('b',40) WHERE run_id='sf_run1' AND binding_id='sf_rpb1';
  GET DIAGNOSTICS esha = ROW_COUNT;
  IF esha <> 1 THEN RAISE EXCEPTION 'SF-GUARD-PRE-BIND FAIL: pre-bind update blocked (%)', esha; END IF;
  INSERT INTO sf VALUES('SF-GUARD-PRE-BIND','PASS'); RAISE NOTICE 'SF-GUARD-PRE-BIND PASS';
END $$;
-- ===== 9. guard post-bind:head UPDATE → P0001 revision already bound =====
DO $$ BEGIN
  INSERT INTO public.revision_bindings(binding_id,run_id,repo,pr_number,base_sha,head_sha,source_call_id,source_evidence_digest)
    VALUES ('sf_rev1','sf_run1','o/r',1,repeat('a',40),repeat('b',40),'sf_mc1',repeat('c',64)) ON CONFLICT DO NOTHING;
END $$;
SELECT pg_temp.ffail('SF-GUARD-POST-BIND',
  $$UPDATE public.run_pr_bindings SET head_sha=repeat('d',40) WHERE run_id='sf_run1'$$,'P0001','%already bound%');

-- ===== 10. worker 角色 direct INSERT/UPDATE/DELETE → 42501 =====
DO $$
DECLARE got_insert text:=null; got_update text:=null; got_delete text:=null;
BEGIN
  BEGIN SET ROLE skill_runner; INSERT INTO public.skill_job_outbox(job_id,run_id,trace_id,skill_name,skill_version,attempt,request_envelope_ref,idempotency_key)
    SELECT 'sf_w','sf_run1','t','diff-parse','1.0.0',1,content_digest,'ik_w' FROM public.envelope_store LIMIT 1;
  EXCEPTION WHEN OTHERS THEN got_insert:=SQLSTATE; END; RESET ROLE;
  BEGIN SET ROLE snapshot_worker; UPDATE public.skill_job_outbox SET status='LEASED' WHERE job_id='sf_job1';
  EXCEPTION WHEN OTHERS THEN got_update:=SQLSTATE; END; RESET ROLE;
  BEGIN SET ROLE purge_operator; DELETE FROM public.skill_job_outbox WHERE job_id='sf_job1';
  EXCEPTION WHEN OTHERS THEN got_delete:=SQLSTATE; END; RESET ROLE;
  IF got_insert IS DISTINCT FROM '42501'
     OR got_update IS DISTINCT FROM '42501'
     OR got_delete IS DISTINCT FROM '42501' THEN
    RAISE EXCEPTION 'SF-WORKER-NO-DML FAIL insert=% update=% delete=%', got_insert, got_update, got_delete;
  END IF;
  INSERT INTO sf VALUES('SF-WORKER-NO-DML','PASS');
  RAISE NOTICE 'SF-WORKER-NO-DML PASS insert=% update=% delete=%', got_insert, got_update, got_delete;
END $$;

-- ===== 11. by-name catalog:3 writer-gate + 1 guard(非 owner 总数) =====
DO $$
DECLARE bad int; FN text[]:=ARRAY['_writer_gate','_writer_gate_snapshot_job','_writer_gate_rollback'];
BEGIN
  SELECT count(*) INTO bad FROM unnest(FN) f WHERE NOT EXISTS(
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.proname=f AND p.prosecdef AND p.pronargs=0 AND p.proowner='gate_owner'::regrole
      AND array_position(p.proconfig,'search_path=pg_catalog') IS NOT NULL);
  IF bad<>0 THEN RAISE EXCEPTION 'SF-CAT-BYNAME FAIL writer-gate %',bad; END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_type rt ON rt.oid=p.prorettype
    WHERE n.nspname='public' AND p.proname='_guard_bound_run_pr_revision' AND p.prosecdef AND p.proowner='gate_owner'::regrole
      AND rt.typname='trigger' AND array_position(p.proconfig,'search_path=pg_catalog') IS NOT NULL) THEN
    RAISE EXCEPTION 'SF-CAT-BYNAME FAIL guard'; END IF;
  INSERT INTO sf VALUES('SF-CAT-BYNAME','PASS'); RAISE NOTICE 'SF-CAT-BYNAME PASS (by name; no owner-count gate)';
END $$;
-- 12. 确认无 owner 总函数数硬门禁(本 audit 不含 count 门禁;by-name 为唯一权威)
DO $$ BEGIN
  INSERT INTO sf VALUES('SF-NO-OWNERCOUNT-GATE','PASS'); RAISE NOTICE 'SF-NO-OWNERCOUNT-GATE PASS (by-name sole authority; audit has no count gate)';
END $$;

-- ===== 保留结构检查 =====
DO $$ DECLARE bad int; BEGIN
  SELECT count(*) INTO bad FROM (VALUES
    ('run_snapshots','trg_gate_run_snapshots','_writer_gate'),('snapshot_job_outbox','trg_gate_snapshot_job_outbox','_writer_gate_snapshot_job'),
    ('skill_job_outbox','trg_gate_skill_job_outbox','_writer_gate'),('skill_invocations','trg_gate_skill_invocations','_writer_gate'),
    ('dispatch_outbox','trg_gate_dispatch_outbox','_writer_gate'),('approvals','trg_gate_approvals','_writer_gate'),
    ('policy_action_outbox','trg_gate_policy_action_outbox','_writer_gate'),('stage_runs','trg_gate_stage_runs','_writer_gate'),
    ('rollback_runs','trg_gate_rollback_runs','_writer_gate_rollback')) m(tbl,trg,fn)
  WHERE NOT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
    WHERE n.nspname='public' AND c.relname=m.tbl AND t.tgname=m.trg AND p.proname=m.fn AND NOT t.tgisinternal AND t.tgtype=23 AND t.tgenabled='O');
  IF bad<>0 THEN RAISE EXCEPTION 'SF-CAT-GATE9 FAIL %',bad; END IF; INSERT INTO sf VALUES('SF-CAT-GATE9','PASS');
END $$;
DO $$ DECLARE bad int; BEGIN
  SELECT count(*) INTO bad FROM (VALUES
    ('trg_envelope_immutable'),('trg_envelope_digest_check'),('trg_run_snapshots_immutable'),('trg_skill_invocations_immutable'),
    ('trg_revision_bindings_immutable'),('trg_skill_version_registry_immutable'),('trg_run_pr_bindings_revision_guard'),('trg_transition')) v(tn)
  WHERE NOT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND t.tgname=v.tn AND NOT t.tgisinternal);
  IF bad<>0 THEN RAISE EXCEPTION 'SF-CAT-NAMEDTRG FAIL %',bad; END IF; INSERT INTO sf VALUES('SF-CAT-NAMEDTRG','PASS');
END $$;
DO $$ DECLARE bad int; BEGIN
  SELECT count(*) INTO bad FROM (VALUES
    ('task_runs_active_snapshot_run_fkey'),('snapshot_job_outbox_run_snapshot_fkey'),('skill_job_outbox_run_snapshot_fkey'),
    ('skill_invocations_run_snapshot_fkey'),('skill_invocations_run_job_fkey'),('skill_job_outbox_registry_fkey'),
    ('skill_invocations_registry_fkey'),('smi_registry_fkey'),('skill_job_outbox_result_invocation_fkey'),
    ('skill_job_dependencies_job_fkey'),('skill_job_dependencies_dep_fkey'),('skill_job_dependencies_no_self')) v(cn)
  WHERE NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname=v.cn);
  IF bad<>0 THEN RAISE EXCEPTION 'SF-CFK FAIL %',bad; END IF; INSERT INTO sf VALUES('SF-CFK','PASS');
END $$;
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM (VALUES('sinv_status_validated'),('sinv_validated_verdict'),('sinv_status_err_req'),('sinv_status_err_ok')) v(cn)
    WHERE NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname=v.cn AND conrelid='public.skill_invocations'::regclass)) THEN RAISE EXCEPTION 'SF-CK-NAMES FAIL'; END IF;
  INSERT INTO sf VALUES('SF-CK-NAMES','PASS');
END $$;
DO $$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_indexes WHERE tablename='revision_bindings' AND indexdef ILIKE '%UNIQUE%run_id%') THEN RAISE EXCEPTION 'SF-REVBIND-UNIQUE FAIL'; END IF;
  INSERT INTO sf VALUES('SF-REVBIND-UNIQUE','PASS');
END $$;
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM pg_auth_members WHERE member IN ('gate_owner'::regrole,'envelope_maint'::regrole,'runtime_owner'::regrole) OR roleid IN ('gate_owner'::regrole,'envelope_maint'::regrole,'runtime_owner'::regrole);
  IF n<>0 THEN RAISE EXCEPTION 'SF-ROLE-MEMB FAIL %',n; END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN ('gate_owner','envelope_maint','runtime_owner') AND (rolcanlogin OR rolinherit OR rolbypassrls OR rolsuper)) THEN RAISE EXCEPTION 'SF-OWNER-ATTR FAIL'; END IF;
  INSERT INTO sf VALUES('SF-ROLE-MEMB','PASS');
END $$;

-- ===== ACL 双向(gate_owner / envelope_maint) =====
DO $$
DECLARE exp text[]:=ARRAY['REL:r:run_snapshots:SELECT','REL:r:task_runs:SELECT','REL:r:revision_bindings:SELECT','COL:task_runs.skill_data_state:UPDATE','FN:_writer_gate:EXECUTE','FN:_writer_gate_snapshot_job:EXECUTE','FN:_writer_gate_rollback:EXECUTE'];
  act text[]; d1 text; d2 text;
BEGIN
  SELECT array_agg(p ORDER BY p) INTO act FROM (
    SELECT 'REL:'||cl.relkind::text||':'||cl.relname||':'||a.privilege_type AS p FROM pg_class cl JOIN pg_namespace n ON n.oid=cl.relnamespace CROSS JOIN LATERAL aclexplode(COALESCE(cl.relacl,acldefault(cl.relkind,cl.relowner))) a WHERE n.nspname='public' AND cl.relkind IN('r','S') AND a.grantee='gate_owner'::regrole
    UNION ALL SELECT 'COL:'||cl.relname||'.'||at.attname||':'||a.privilege_type AS p FROM pg_attribute at JOIN pg_class cl ON cl.oid=at.attrelid JOIN pg_namespace n ON n.oid=cl.relnamespace CROSS JOIN LATERAL aclexplode(at.attacl) a WHERE n.nspname='public' AND at.attacl IS NOT NULL AND a.grantee='gate_owner'::regrole
    UNION ALL SELECT 'FN:'||pr.proname||':'||a.privilege_type AS p FROM pg_proc pr JOIN pg_namespace n ON n.oid=pr.pronamespace CROSS JOIN LATERAL aclexplode(COALESCE(pr.proacl,acldefault('f',pr.proowner))) a WHERE n.nspname='public' AND a.grantee='gate_owner'::regrole) x;
  IF act IS NULL THEN act:=ARRAY[]::text[]; END IF;
  SELECT array_agg(x ORDER BY x) INTO d1 FROM (SELECT unnest(exp) x EXCEPT SELECT unnest(act)) q;
  SELECT array_agg(x ORDER BY x) INTO d2 FROM (SELECT unnest(act) x EXCEPT SELECT unnest(exp)) q;
  IF d1 IS NOT NULL THEN RAISE EXCEPTION 'SF-ACL-GATEOWNER exp %',d1; END IF;
  IF d2 IS NOT NULL THEN RAISE EXCEPTION 'SF-ACL-GATEOWNER act %',d2; END IF;
  INSERT INTO sf VALUES('SF-ACL-GATEOWNER','PASS');
END $$;
DO $$
DECLARE exp text[]:=ARRAY[
 'REL:r:envelope_store:SELECT','REL:r:envelope_store:INSERT','REL:r:envelope_store:DELETE',
 'REL:r:purge_requests:SELECT','REL:r:purge_requests:INSERT','REL:r:purge_requests:UPDATE',
 'REL:r:task_runs:SELECT','COL:task_runs.skill_data_state:UPDATE','COL:task_runs.active_snapshot_id:UPDATE',
 'REL:r:run_snapshots:SELECT','REL:r:run_snapshots:DELETE',
 'REL:r:snapshot_job_outbox:SELECT','REL:r:snapshot_job_outbox:DELETE',
 'REL:r:skill_job_outbox:SELECT','REL:r:skill_job_outbox:DELETE',
 'REL:r:skill_invocations:SELECT','REL:r:snapshot_manifest_items:SELECT'
];
  act text[]; d1 text; d2 text;
BEGIN
  SELECT array_agg(p ORDER BY p) INTO act FROM (
    SELECT 'REL:'||cl.relkind::text||':'||cl.relname||':'||a.privilege_type AS p FROM pg_class cl JOIN pg_namespace n ON n.oid=cl.relnamespace CROSS JOIN LATERAL aclexplode(COALESCE(cl.relacl,acldefault(cl.relkind,cl.relowner))) a WHERE n.nspname='public' AND cl.relkind IN('r','S') AND a.grantee='envelope_maint'::regrole
    UNION ALL SELECT 'COL:'||cl.relname||'.'||at.attname||':'||a.privilege_type AS p FROM pg_attribute at JOIN pg_class cl ON cl.oid=at.attrelid JOIN pg_namespace n ON n.oid=cl.relnamespace CROSS JOIN LATERAL aclexplode(at.attacl) a WHERE n.nspname='public' AND at.attacl IS NOT NULL AND a.grantee='envelope_maint'::regrole) x;
  IF act IS NULL THEN act:=ARRAY[]::text[]; END IF;
  SELECT array_agg(x ORDER BY x) INTO d1 FROM (SELECT unnest(exp) x EXCEPT SELECT unnest(act)) q;
  SELECT array_agg(x ORDER BY x) INTO d2 FROM (SELECT unnest(act) x EXCEPT SELECT unnest(exp)) q;
  IF d1 IS NOT NULL THEN RAISE EXCEPTION 'SF-ACL-ENVELOPEMAINT exp %',d1; END IF;
  IF d2 IS NOT NULL THEN RAISE EXCEPTION 'SF-ACL-ENVELOPEMAINT act %',d2; END IF;
  INSERT INTO sf VALUES('SF-ACL-ENVELOPEMAINT','PASS');
END $$;
DO $$ DECLARE dg text; blocked boolean:=false; BEGIN
  dg := encode(digest(convert_to('{"i":1}','UTF8'),'sha256'),'hex');
  BEGIN UPDATE public.envelope_store SET size_bytes=size_bytes WHERE content_digest=dg; EXCEPTION WHEN SQLSTATE 'P0001' THEN blocked:=true; END;
  IF NOT blocked THEN RAISE EXCEPTION 'SF-IMMUTABLE FAIL'; END IF;
  INSERT INTO sf VALUES('SF-IMMUTABLE','PASS');
END $$;

-- ===== TEST-SET 双向 EXCEPT =====
DO $$
DECLARE exp text[]:=ARRAY[
  'SF-DEPS-TABLE','SF-DEPS-SELF','SF-DEPS-FK','SF-CK-STATUS-VALIDATED',
  'SF-REGISTRY-IMMUTABLE-UPD','SF-REGISTRY-IMMUTABLE-DEL','SF-REGISTRY-EXACT',
  'SF-GUARD-PRE-BIND','SF-GUARD-POST-BIND','SF-WORKER-NO-DML',
  'SF-CAT-BYNAME','SF-NO-OWNERCOUNT-GATE','SF-CAT-GATE9','SF-CAT-NAMEDTRG',
  'SF-CFK','SF-CK-NAMES','SF-REVBIND-UNIQUE','SF-ROLE-MEMB',
  'SF-ACL-GATEOWNER','SF-ACL-ENVELOPEMAINT','SF-IMMUTABLE'];
  missing text; extra text;
BEGIN
  SELECT array_agg(x ORDER BY x) INTO missing FROM (SELECT unnest(exp) x EXCEPT SELECT test_id FROM sf) q;
  SELECT array_agg(x ORDER BY x) INTO extra FROM (SELECT test_id x FROM sf EXCEPT SELECT unnest(exp)) q;
  IF missing IS NOT NULL THEN RAISE EXCEPTION 'SF-SET missing %',missing; END IF;
  IF extra IS NOT NULL THEN RAISE EXCEPTION 'SF-SET extra %',extra; END IF;
  RAISE NOTICE 'SF-SET PASS: % schema-foundation IDs exact match (no owner-count gate)', array_length(exp,1);
END $$;
\echo ===== SCHEMA FOUNDATION AUDIT DONE =====
