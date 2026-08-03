-- ════════════════════════════════════════════════════════════════════════════
-- m4f1_hotfix_1.sql -- M4-F post-release P1 hotfix (additive, idempotent).
--
-- Root cause: public.skill_job_outbox / snapshot_job_outbox each carry TWO
-- unique constraints (job_id and idempotency_key). The producer SD APIs used
-- `INSERT ... ON CONFLICT (job_id) DO NOTHING`, which only absorbs a conflict
-- on the job_id index. Under a real two-connection race on the same
-- deterministic job, PostgreSQL can detect the idempotency_key unique violation
-- first and leak SQLSTATE 23505 (constraint=*_idempotency_key_key) to the
-- caller instead of triggering the ON CONFLICT (job_id) path.
--
-- Fix: switch both enqueue functions to an untargeted `ON CONFLICT DO NOTHING`
-- so ANY unique-index contention is swallowed uniformly, then rely on the
-- existing post-INSERT `SELECT ... FOR UPDATE` re-read to reconcile: identical
-- payload returns the same deterministic job_id; a payload/dependency mismatch
-- is surfaced as a clean P0001 (never 23505).
--
-- This migration is CREATE OR REPLACE only: no signature/owner/ACL/search_path
-- change, no DROP, no data mutation. Applies cleanly on top of m4f1_state.sql
-- (fresh) AND on top of the released m4f1_state.sql at tag m4f-agentteams-demo-
-- closed (upgrade path). Idempotent: applying twice succeeds and re-converges.
-- ════════════════════════════════════════════════════════════════════════════

-- runtime_owner is the SD-API function owner; ensure it exists (no-op if the
-- base chain already created it).
DO $role$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='runtime_owner') THEN
    CREATE ROLE runtime_owner NOLOGIN;
  END IF;
END $role$;
ALTER ROLE runtime_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;

-- ── enqueue_snapshot_job: untargeted ON CONFLICT DO NOTHING ──────────────────
CREATE OR REPLACE FUNCTION public.enqueue_snapshot_job(p_run_id text, p_revision_binding_id text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_sds text; v_job text; v_existing record;
BEGIN
  SELECT skill_data_state INTO v_sds FROM public.task_runs WHERE run_id=p_run_id FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'enqueue_snapshot_job: run not found' USING ERRCODE='P0001'; END IF;
  IF v_sds <> 'ACTIVE' THEN RAISE EXCEPTION 'enqueue_snapshot_job: run not ACTIVE' USING ERRCODE='P0001'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.revision_bindings
                 WHERE binding_id=p_revision_binding_id AND run_id=p_run_id) THEN
    RAISE EXCEPTION 'enqueue_snapshot_job: binding not found or does not belong to run' USING ERRCODE='P0001';
  END IF;

  v_job := 'snapjob-'||p_run_id;
  INSERT INTO public.snapshot_job_outbox(
      job_id,run_id,revision_binding_id,idempotency_key,status,attempts,next_retry_at)
    VALUES (v_job,p_run_id,p_revision_binding_id,v_job,'PENDING',0,now())
    ON CONFLICT DO NOTHING;

  SELECT job_id,run_id,revision_binding_id,idempotency_key INTO v_existing
    FROM public.snapshot_job_outbox WHERE job_id=v_job FOR UPDATE;
  IF NOT FOUND OR v_existing.run_id IS DISTINCT FROM p_run_id
     OR v_existing.revision_binding_id IS DISTINCT FROM p_revision_binding_id
     OR v_existing.idempotency_key IS DISTINCT FROM v_job THEN
    RAISE EXCEPTION 'enqueue_snapshot_job: idempotency conflict' USING ERRCODE='P0001';
  END IF;
  RETURN v_job;
END; $$;

-- ── enqueue_skill_job: untargeted ON CONFLICT DO NOTHING ─────────────────────
CREATE OR REPLACE FUNCTION public.enqueue_skill_job(
  p_run_id text, p_snapshot_id text, p_trace_id text, p_skill_name text, p_skill_version text,
  p_attempt int, p_request_envelope_ref text, p_depends_on_job_ids text[] DEFAULT '{}') RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE
  v_job text; v_env record; v_req jsonb; v_keys text[]; v_dep text; v_cycle int;
  v_sds text; v_existing record; v_deps_existing text[]; v_deps_input text[];
  v_d_in text; v_expected_req_id text; v_inserted int;
BEGIN
  SELECT skill_data_state INTO v_sds FROM public.task_runs WHERE run_id=p_run_id FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'enqueue_skill_job: run not found' USING ERRCODE='P0001'; END IF;
  IF v_sds <> 'ACTIVE' THEN RAISE EXCEPTION 'enqueue_skill_job: run not ACTIVE' USING ERRCODE='P0001'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.skill_version_registry
                 WHERE skill_name=p_skill_name AND skill_version=p_skill_version) THEN
    RAISE EXCEPTION 'enqueue_skill_job: unregistered skill version' USING ERRCODE='P0001';
  END IF;

  SELECT content_type,content_json INTO v_env FROM public.envelope_store
    WHERE content_digest=p_request_envelope_ref;
  IF NOT FOUND OR v_env.content_type <> 'application/vnd.mergepilot.skill-request.v1+json' THEN
    RAISE EXCEPTION 'enqueue_skill_job: request envelope wrong type' USING ERRCODE='P0001';
  END IF;
  v_req := v_env.content_json;
  IF v_req->>'contract_version' IS DISTINCT FROM '1' THEN
    RAISE EXCEPTION 'enqueue_skill_job: contract_version not 1' USING ERRCODE='P0001';
  END IF;
  IF v_req->>'trace_id' IS DISTINCT FROM p_trace_id THEN
    RAISE EXCEPTION 'enqueue_skill_job: trace_id mismatch' USING ERRCODE='P0001';
  END IF;
  IF NOT (v_req ? 'input') THEN
    RAISE EXCEPTION 'enqueue_skill_job: input missing' USING ERRCODE='P0001';
  END IF;
  SELECT array_agg(k) INTO v_keys FROM jsonb_object_keys(v_req) AS k;
  IF EXISTS (SELECT 1 FROM unnest(COALESCE(v_keys,ARRAY[]::text[])) AS k
             WHERE k NOT IN ('contract_version','request_id','trace_id','input','timeout_ms')) THEN
    RAISE EXCEPTION 'enqueue_skill_job: unknown top-level key' USING ERRCODE='P0001';
  END IF;

  v_d_in := encode(public.digest(
    convert_to(public.canonical_json(v_req->'input'),'UTF8'),'sha256'),'hex');
  v_expected_req_id := 'req-'||left(encode(public.digest(
    public._canon_str(p_trace_id)||public._canon_str(p_run_id)||
    public._canon_str(p_skill_name)||public._canon_str(p_attempt::text)||
    public._canon_str(v_d_in),'sha256'),'hex'),24);
  IF v_req->>'request_id' IS DISTINCT FROM v_expected_req_id THEN
    RAISE EXCEPTION 'enqueue_skill_job: request_id mismatch' USING ERRCODE='P0001';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.snapshot_manifest_items
      WHERE snapshot_id=p_snapshot_id AND skill_name=p_skill_name
        AND skill_version=p_skill_version AND request_envelope_ref=p_request_envelope_ref) THEN
    RAISE EXCEPTION 'request not in snapshot manifest' USING ERRCODE='P0001';
  END IF;

  v_job := 'sj-'||left(encode(public.digest(
    public._canon_str(p_run_id)||public._canon_str(COALESCE(p_snapshot_id,''))||
    public._canon_str(p_skill_name)||public._canon_str(p_skill_version)||
    public._canon_str(p_attempt::text)||public._canon_str(p_request_envelope_ref),
    'sha256'),'hex'),32);
  SELECT COALESCE(array_agg(DISTINCT d ORDER BY d),ARRAY[]::text[]) INTO v_deps_input
    FROM unnest(COALESCE(p_depends_on_job_ids,ARRAY[]::text[])) AS u(d);

  INSERT INTO public.skill_job_outbox(
      job_id,run_id,snapshot_id,trace_id,skill_name,skill_version,attempt,
      request_envelope_ref,idempotency_key,status,attempts,next_retry_at)
    VALUES (v_job,p_run_id,p_snapshot_id,p_trace_id,p_skill_name,p_skill_version,p_attempt,
      p_request_envelope_ref,v_job,'PENDING',0,now())
    ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS v_inserted = ROW_COUNT;

  SELECT job_id,run_id,snapshot_id,trace_id,skill_name,skill_version,attempt,
         request_envelope_ref,idempotency_key INTO v_existing
    FROM public.skill_job_outbox WHERE job_id=v_job FOR UPDATE;
  IF NOT FOUND OR v_existing.run_id IS DISTINCT FROM p_run_id
     OR v_existing.snapshot_id IS DISTINCT FROM p_snapshot_id
     OR v_existing.trace_id IS DISTINCT FROM p_trace_id
     OR v_existing.skill_name IS DISTINCT FROM p_skill_name
     OR v_existing.skill_version IS DISTINCT FROM p_skill_version
     OR v_existing.attempt IS DISTINCT FROM p_attempt
     OR v_existing.request_envelope_ref IS DISTINCT FROM p_request_envelope_ref
     OR v_existing.idempotency_key IS DISTINCT FROM v_job THEN
    RAISE EXCEPTION 'enqueue_skill_job: idempotency conflict' USING ERRCODE='P0001';
  END IF;

  SELECT COALESCE(array_agg(depends_on_job_id ORDER BY depends_on_job_id),ARRAY[]::text[])
    INTO v_deps_existing FROM public.skill_job_dependencies WHERE job_id=v_job;
  IF v_inserted = 0 THEN
    IF v_deps_existing IS DISTINCT FROM v_deps_input THEN
      RAISE EXCEPTION 'enqueue_skill_job: dependency set conflict' USING ERRCODE='P0001';
    END IF;
    RETURN v_job;
  END IF;

  FOREACH v_dep IN ARRAY v_deps_input LOOP
    IF v_dep = v_job THEN
      RAISE EXCEPTION 'enqueue_skill_job: self-dependency' USING ERRCODE='P0001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.skill_job_outbox
                   WHERE job_id=v_dep AND run_id=p_run_id
                     AND snapshot_id IS NOT DISTINCT FROM p_snapshot_id) THEN
      RAISE EXCEPTION 'enqueue_skill_job: dependency not found or wrong run/snapshot' USING ERRCODE='P0001';
    END IF;
    INSERT INTO public.skill_job_dependencies(job_id,depends_on_job_id)
      VALUES (v_job,v_dep);
  END LOOP;

  WITH RECURSIVE dependency_closure(ancestor) AS (
    SELECT depends_on_job_id FROM public.skill_job_dependencies WHERE job_id=v_job
    UNION
    SELECT d.depends_on_job_id FROM public.skill_job_dependencies AS d
      JOIN dependency_closure AS c ON d.job_id=c.ancestor
  )
  SELECT count(*) INTO v_cycle FROM dependency_closure WHERE ancestor=v_job;
  IF v_cycle > 0 THEN
    RAISE EXCEPTION 'enqueue_skill_job: dependency cycle' USING ERRCODE='P0001';
  END IF;
  RETURN v_job;
END; $$;

-- ── re-assert owner / REVOKE PUBLIC / GRANT EXECUTE (idempotent) ─────────────
ALTER FUNCTION public.enqueue_snapshot_job(text,text) OWNER TO runtime_owner;
ALTER FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public.enqueue_snapshot_job(text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.enqueue_snapshot_job(text,text) TO mergepilot;
GRANT EXECUTE ON FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) TO mergepilot;

-- ── catalog self-check: SD/owner/search_path/PUBLIC-EXECUTE + ON CONFLICT ──
DO $$
DECLARE
  v_bad int; v_pub int; v_src text;
BEGIN
  -- (1) SECURITY DEFINER + owner=runtime_owner + search_path=pg_catalog
  SELECT count(*) INTO v_bad FROM pg_proc p
    JOIN pg_namespace n ON n.oid=p.pronamespace
    LEFT JOIN pg_roles r ON r.oid=p.proowner
    WHERE n.nspname='public'
      AND p.proname IN ('enqueue_snapshot_job','enqueue_skill_job')
      AND (NOT p.prosecdef
           OR r.rolname IS DISTINCT FROM 'runtime_owner'
           OR p.proconfig IS NULL
           OR array_position(p.proconfig,'search_path=pg_catalog') IS NULL);
  IF v_bad > 0 THEN RAISE EXCEPTION 'hotfix1 catalog: SD/owner/search_path check failed (rows=%)', v_bad; END IF;

  -- (2) PUBLIC must NOT retain EXECUTE (NULL proacl == default == PUBLIC EXECUTE => fail)
  SELECT count(*) INTO v_pub FROM pg_proc p
    JOIN pg_namespace n ON n.oid=p.pronamespace
    LEFT JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a ON true
    WHERE n.nspname='public'
      AND p.proname IN ('enqueue_snapshot_job','enqueue_skill_job')
      AND a.grantee = 0 AND a.privilege_type = 'EXECUTE';
  IF v_pub > 0 THEN RAISE EXCEPTION 'hotfix1 catalog: PUBLIC still holds EXECUTE (rows=%)', v_pub; END IF;

  -- (3) function bodies: no 'ON CONFLICT (job_id)' and yes 'ON CONFLICT DO NOTHING'
  SELECT prosrc INTO v_src FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.proname='enqueue_snapshot_job';
  IF position('ON CONFLICT (job_id)' IN v_src) > 0 THEN RAISE EXCEPTION 'hotfix1: enqueue_snapshot_job still has targeted ON CONFLICT (job_id)'; END IF;
  IF position('ON CONFLICT DO NOTHING' IN v_src) = 0 THEN RAISE EXCEPTION 'hotfix1: enqueue_snapshot_job missing untargeted ON CONFLICT DO NOTHING'; END IF;

  SELECT prosrc INTO v_src FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.proname='enqueue_skill_job';
  IF position('ON CONFLICT (job_id)' IN v_src) > 0 THEN RAISE EXCEPTION 'hotfix1: enqueue_skill_job still has targeted ON CONFLICT (job_id)'; END IF;
  IF position('ON CONFLICT DO NOTHING' IN v_src) = 0 THEN RAISE EXCEPTION 'hotfix1: enqueue_skill_job missing untargeted ON CONFLICT DO NOTHING'; END IF;

  RAISE NOTICE 'm4f1_hotfix_1 catalog self-check PASS';
END $$;
