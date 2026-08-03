-- m4f1_state.sql — M4-F1 数据库契约 v2.8 实现。
-- 包含 roles/ACL、tables/task_runs extensions、constraints/composite FK、12-digest registry seed、
-- immutable/writer/revision-guard 触发器、MergePilot JCS Profile v1、完整 producer/worker/
-- completion/purge SECURITY DEFINER API，以及按函数名的 catalog 自检。
-- 单一事务(BEGIN/COMMIT):catalog 自检失败→整事务回滚,无半成品。幂等、非破坏。
-- 依赖:m3_state + m3b_policy(mcp_calls) + m3b_b4(run_pr_bindings) + m3b_b4c/c1/c1_1/d1 + m3c_state。

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ═══ 1. task_runs 扩展(trace_id/active_snapshot_id/skill_data_state) ═══
ALTER TABLE public.task_runs ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE public.task_runs ADD COLUMN IF NOT EXISTS active_snapshot_id TEXT;
ALTER TABLE public.task_runs ADD COLUMN IF NOT EXISTS skill_data_state TEXT NOT NULL DEFAULT 'ACTIVE';
UPDATE public.task_runs SET skill_data_state='ACTIVE' WHERE skill_data_state IS NULL;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_skill_data_state' AND conrelid='public.task_runs'::regclass) THEN
    ALTER TABLE public.task_runs ADD CONSTRAINT chk_skill_data_state CHECK (skill_data_state IN ('ACTIVE','PURGING','PURGED'));
  END IF;
END $$;

-- ═══ 2. envelope_store(内容寻址,不可变) ═══
CREATE TABLE IF NOT EXISTS public.envelope_store (
  content_digest TEXT PRIMARY KEY CHECK (content_digest ~ '^[0-9a-f]{64}$'),
  content_bytes  BYTEA NOT NULL,
  content_json   JSONB,
  content_type   TEXT NOT NULL CHECK (content_type IN (
     'application/vnd.mergepilot.skill-request.v1+json',
     'application/vnd.mergepilot.skill-response.v1+json',
     'application/vnd.mergepilot.snapshot-manifest.v1+json')),
  size_bytes     INTEGER NOT NULL CHECK (size_bytes >= 0 AND size_bytes <= 1048576),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT envelope_size_matches CHECK (size_bytes = octet_length(content_bytes))
);

-- ═══ 3. run_snapshots(不可变;repo/pr 派生;composite-FK 目标) ═══
CREATE TABLE IF NOT EXISTS public.run_snapshots (
  snapshot_id    TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES public.task_runs(run_id),
  repo           TEXT NOT NULL,
  pr_number      INTEGER NOT NULL,
  base_sha       TEXT NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
  head_sha       TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
  manifest_digest TEXT NOT NULL REFERENCES public.envelope_store(content_digest),
  incomplete     BOOLEAN NOT NULL DEFAULT false,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_snapshots_run_digest ON public.run_snapshots(run_id, manifest_digest);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_snapshots_run_snap  ON public.run_snapshots(run_id, snapshot_id);   -- composite-FK target

-- ═══ 4. skill_version_registry(不可变;12-digest seed 在 §10) ═══
CREATE TABLE IF NOT EXISTS public.skill_version_registry (
  skill_name            TEXT NOT NULL CHECK (skill_name IN ('diff-parse','risk-classify','sast-scan','test-runner','case-retrieval','pr-lifecycle')),
  skill_version         TEXT NOT NULL CHECK (skill_version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  output_schema_digest  TEXT NOT NULL CHECK (output_schema_digest ~ '^[0-9a-f]{64}$'),
  request_schema_digest TEXT NOT NULL CHECK (request_schema_digest ~ '^[0-9a-f]{64}$'),
  registered_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (skill_name, skill_version),
  CONSTRAINT uq_registry_output UNIQUE (output_schema_digest),
  CONSTRAINT uq_registry_request UNIQUE (request_schema_digest),
  CONSTRAINT uq_registry_skillver_out UNIQUE (skill_name, skill_version, output_schema_digest)
);

-- ═══ 5. revision_bindings(一 run 一 revision;不可变;provenance) ═══
CREATE TABLE IF NOT EXISTS public.revision_bindings (
  binding_id            TEXT PRIMARY KEY,
  run_id                TEXT NOT NULL UNIQUE REFERENCES public.task_runs(run_id),
  repo                  TEXT NOT NULL,
  pr_number             INTEGER NOT NULL,
  base_sha              TEXT NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
  head_sha              TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
  source_call_id        TEXT NOT NULL REFERENCES public.mcp_calls(request_id),
  source_evidence_digest TEXT NOT NULL CHECK (source_evidence_digest ~ '^[0-9a-f]{64}$'),
  recorded_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ═══ 5.5 purge_requests(单一模型;target_state 仅 PURGED;无 FAILED) ═══
CREATE TABLE IF NOT EXISTS public.purge_requests (
  purge_id      TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES public.task_runs(run_id),
  target_state  TEXT NOT NULL CHECK (target_state = 'PURGED'),
  status        TEXT NOT NULL DEFAULT 'REQUESTED' CHECK (status IN ('REQUESTED','PURGING','PURGED')),
  requested_by  TEXT NOT NULL,
  requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  purging_at    TIMESTAMPTZ, completed_at TIMESTAMPTZ,
  error         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_purge_requests_active_run
  ON public.purge_requests(run_id) WHERE status IN ('REQUESTED','PURGING');

-- ═══ 6. snapshot_job_outbox(claim_id CAS + revision_binding_id) ═══
CREATE TABLE IF NOT EXISTS public.snapshot_job_outbox (
  job_id           TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES public.task_runs(run_id),
  snapshot_id      TEXT,
  revision_binding_id TEXT NOT NULL REFERENCES public.revision_bindings(binding_id),
  idempotency_key  TEXT NOT NULL UNIQUE,
  status           TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','LEASED','SUCCEEDED','FAILED')),
  claim_id         UUID,
  leased_by        TEXT, lease_expires_at TIMESTAMPTZ, last_heartbeat_at TIMESTAMPTZ,
  attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts     INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
  next_retry_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  error            TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at       TIMESTAMPTZ, completed_at TIMESTAMPTZ
);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='snapshot_job_outbox_run_snapshot_fkey' AND conrelid='public.snapshot_job_outbox'::regclass) THEN
    ALTER TABLE public.snapshot_job_outbox ADD CONSTRAINT snapshot_job_outbox_run_snapshot_fkey
      FOREIGN KEY (run_id, snapshot_id) REFERENCES public.run_snapshots(run_id, snapshot_id);
  END IF;
END $$;

-- ═══ 7. skill_job_outbox(claim_id CAS;registry 复合 FK;循环 FK result_invocation_id 后补) ═══
CREATE TABLE IF NOT EXISTS public.skill_job_outbox (
  job_id            TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES public.task_runs(run_id),
  snapshot_id       TEXT,
  trace_id          TEXT NOT NULL,
  skill_name        TEXT NOT NULL,
  skill_version     TEXT NOT NULL,
  attempt           INTEGER NOT NULL CHECK (attempt >= 1),
  request_envelope_ref TEXT NOT NULL REFERENCES public.envelope_store(content_digest),
  idempotency_key   TEXT NOT NULL UNIQUE,
  status            TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','LEASED','SUCCEEDED','FAILED')),
  claim_id          UUID,
  leased_by         TEXT, lease_expires_at TIMESTAMPTZ, last_heartbeat_at TIMESTAMPTZ,
  attempts          INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts      INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
  next_retry_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  result_invocation_id TEXT,
  error             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at        TIMESTAMPTZ, completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_job_outbox_run_job ON public.skill_job_outbox(run_id, job_id);  -- composite-FK target
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_job_outbox_run_snapshot_fkey' AND conrelid='public.skill_job_outbox'::regclass) THEN
    ALTER TABLE public.skill_job_outbox ADD CONSTRAINT skill_job_outbox_run_snapshot_fkey
      FOREIGN KEY (run_id, snapshot_id) REFERENCES public.run_snapshots(run_id, snapshot_id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_job_outbox_registry_fkey' AND conrelid='public.skill_job_outbox'::regclass) THEN
    ALTER TABLE public.skill_job_outbox ADD CONSTRAINT skill_job_outbox_registry_fkey
      FOREIGN KEY (skill_name, skill_version) REFERENCES public.skill_version_registry(skill_name, skill_version);
  END IF;
END $$;

-- ═══ 7.5 skill_job_dependencies(规范化 DAG;双 FK CASCADE;禁自依赖) ═══
CREATE TABLE IF NOT EXISTS public.skill_job_dependencies (
  job_id            TEXT NOT NULL,
  depends_on_job_id TEXT NOT NULL,
  PRIMARY KEY (job_id, depends_on_job_id),
  CONSTRAINT skill_job_dependencies_job_fkey    FOREIGN KEY (job_id)            REFERENCES public.skill_job_outbox(job_id) ON DELETE CASCADE,
  CONSTRAINT skill_job_dependencies_dep_fkey    FOREIGN KEY (depends_on_job_id) REFERENCES public.skill_job_outbox(job_id) ON DELETE CASCADE,
  CONSTRAINT skill_job_dependencies_no_self     CHECK (job_id <> depends_on_job_id)
);
CREATE INDEX IF NOT EXISTS idx_skill_job_dependencies_dep ON public.skill_job_dependencies(depends_on_job_id);

-- ═══ 8. skill_invocations(不可变;status-aware;registry 复合 FK) ═══
CREATE TABLE IF NOT EXISTS public.skill_invocations (
  invocation_id    TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES public.task_runs(run_id),
  snapshot_id      TEXT,
  job_id           TEXT,
  trace_id         TEXT NOT NULL,
  skill_name       TEXT NOT NULL, skill_version TEXT NOT NULL,
  attempt          INTEGER NOT NULL CHECK (attempt >= 1),
  request_id       TEXT NOT NULL,
  contract_version TEXT NOT NULL DEFAULT '1' CHECK (contract_version = '1'),
  status           TEXT NOT NULL CHECK (status IN ('OK','PARTIAL','ERROR')),
  error_code       TEXT CHECK (error_code IS NULL OR error_code IN ('INVALID_INPUT','SCHEMA_VERSION_UNSUPPORTED','TIMEOUT','DENIED','DEPENDENCY_UNAVAILABLE','OUTPUT_TOO_LARGE','INTERNAL_ERROR')),
  verdict          TEXT CHECK (verdict IS NULL OR verdict IN ('PASS','FAIL','TIMEOUT','ERROR')),
  input_digest     TEXT NOT NULL REFERENCES public.envelope_store(content_digest),
  output_digest    TEXT REFERENCES public.envelope_store(content_digest),
  snapshot_manifest_digest TEXT REFERENCES public.envelope_store(content_digest),
  expected_output_schema_digest TEXT NOT NULL,
  output_schema_validated BOOLEAN NOT NULL,
  duration_ms      INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
  started_at       TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
  idempotency_key  TEXT NOT NULL UNIQUE,
  CONSTRAINT sinv_status_err_ok   CHECK (NOT (status IN ('OK','PARTIAL') AND error_code IS NOT NULL)),
  CONSTRAINT sinv_status_err_req  CHECK (NOT (status = 'ERROR' AND error_code IS NULL)),
  CONSTRAINT sinv_status_validated CHECK (NOT (status IN ('OK','PARTIAL') AND output_schema_validated = false)),
  CONSTRAINT sinv_validated_verdict CHECK (NOT (output_schema_validated = false AND verdict IS NOT NULL))
);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_invocations_run_snapshot_fkey' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT skill_invocations_run_snapshot_fkey
      FOREIGN KEY (run_id, snapshot_id) REFERENCES public.run_snapshots(run_id, snapshot_id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_invocations_run_job_fkey' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT skill_invocations_run_job_fkey
      FOREIGN KEY (run_id, job_id) REFERENCES public.skill_job_outbox(run_id, job_id) ON DELETE CASCADE;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_invocations_registry_fkey' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT skill_invocations_registry_fkey
      FOREIGN KEY (skill_name, skill_version, expected_output_schema_digest)
      REFERENCES public.skill_version_registry(skill_name, skill_version, output_schema_digest);
  END IF;
END $$;
-- 循环 FK:result_invocation_id(skill_job_outbox → skill_invocations)ON DELETE SET NULL DEFERRABLE
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='skill_job_outbox_result_invocation_fkey' AND conrelid='public.skill_job_outbox'::regclass) THEN
    ALTER TABLE public.skill_job_outbox ADD CONSTRAINT skill_job_outbox_result_invocation_fkey
      FOREIGN KEY (result_invocation_id) REFERENCES public.skill_invocations(invocation_id)
      ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$;

-- status-aware CK 幂等补齐(既有库兼容;新库 CREATE TABLE 已含)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sinv_status_validated' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT sinv_status_validated CHECK (NOT (status IN ('OK','PARTIAL') AND output_schema_validated = false));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sinv_validated_verdict' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT sinv_validated_verdict CHECK (NOT (output_schema_validated = false AND verdict IS NOT NULL));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sinv_status_err_req' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT sinv_status_err_req CHECK (NOT (status = 'ERROR' AND error_code IS NULL));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sinv_status_err_ok' AND conrelid='public.skill_invocations'::regclass) THEN
    ALTER TABLE public.skill_invocations ADD CONSTRAINT sinv_status_err_ok CHECK (NOT (status IN ('OK','PARTIAL') AND error_code IS NOT NULL));
  END IF;
END $$;

-- ═══ 9. snapshot_manifest_items(规范化;每 (snapshot,skill,version) 至多一项) ═══
CREATE TABLE IF NOT EXISTS public.snapshot_manifest_items (
  snapshot_id          TEXT NOT NULL REFERENCES public.run_snapshots(snapshot_id) ON DELETE CASCADE,
  ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
  skill_name           TEXT NOT NULL, skill_version TEXT NOT NULL,
  request_envelope_ref TEXT NOT NULL REFERENCES public.envelope_store(content_digest),
  PRIMARY KEY (snapshot_id, ordinal),
  CONSTRAINT smi_uniq_skillver UNIQUE (snapshot_id, skill_name, skill_version)
);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='smi_registry_fkey' AND conrelid='public.snapshot_manifest_items'::regclass) THEN
    ALTER TABLE public.snapshot_manifest_items ADD CONSTRAINT smi_registry_fkey
      FOREIGN KEY (skill_name, skill_version) REFERENCES public.skill_version_registry(skill_name, skill_version);
  END IF;
END $$;

-- ═══ 10. task_runs.active_snapshot_id 复合 FK(替换任何旧简单 FK) ═══
DO $$ BEGIN
  ALTER TABLE public.task_runs DROP CONSTRAINT IF EXISTS task_runs_active_snapshot_id_fkey;
  ALTER TABLE public.task_runs DROP CONSTRAINT IF EXISTS task_runs_active_snapshot_run_fkey;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='task_runs_active_snapshot_run_fkey' AND conrelid='public.task_runs'::regclass) THEN
    ALTER TABLE public.task_runs ADD CONSTRAINT task_runs_active_snapshot_run_fkey
      FOREIGN KEY (run_id, active_snapshot_id) REFERENCES public.run_snapshots(run_id, snapshot_id);
  END IF;
END $$;

-- ═══ 11. 12-digest registry seed(实测 sha256 原始文件字节) ═══
INSERT INTO public.skill_version_registry(skill_name, skill_version, request_schema_digest, output_schema_digest) VALUES
 ('diff-parse','1.0.0','89d628502dd726d6dfa1df4f52687bd51a1cea75d81e680a5025852f3b5b7285','e6e0eb2077645007de8115a0be697b27954e34b9a000e9bc7c6de03c27fd355b'),
 ('risk-classify','1.0.0','45ca36e3a5c6ff8146e13d7935918240279f1ffbc28872c8b1c04c81a3111371','b4d8e0519916cc21ea5286a677a94de53af2cb968073c1b06cf8b4d6ccbda09a'),
 ('sast-scan','1.0.0','8d008630393b59e77ed66669c2b5d6a45591dbbed5c3bc5554289035c5813598','fda15df57b9713bf76f95ff0668a8c76a8f7f68cabb40348232d571614e497e1'),
 ('test-runner','1.0.0','a90f67f1c19243582402d8e8b590f9a104a937637442be29a3d980848b9ecda9','461c5f026e01a4641acc0821220f6720361402ee2c3fc802421a6a11c41772d9'),
 ('case-retrieval','1.0.0','549526ab5aa410b67754a52ba7fcd826b2cc7813189eac0f929c5b53e666c3d3','4366b3e76796756158197b10c77c135b7d6443c9262ad9a5be5c03a60f662b57'),
 ('pr-lifecycle','1.0.0','7157df189df14d7128c3fe9f40e749050ed8251f206a7f5a57ca31da9859c424','ee27d6b587ca9b82d9da189ae98ca4a58437110ebe3ff75348506355c075dc1c')
ON CONFLICT (skill_name, skill_version) DO NOTHING;

-- ═══ 12. 角色 + 幂等收敛 + 双向 membership 清理 ═══
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='gate_owner')      THEN CREATE ROLE gate_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='envelope_maint')  THEN CREATE ROLE envelope_maint NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='runtime_owner')   THEN CREATE ROLE runtime_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='skill_runner')    THEN CREATE ROLE skill_runner LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='snapshot_worker') THEN CREATE ROLE snapshot_worker LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='purge_operator')  THEN CREATE ROLE purge_operator LOGIN; END IF;
END $$;
ALTER ROLE gate_owner      NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE envelope_maint  NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE runtime_owner   NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE skill_runner    NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE snapshot_worker NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE purge_operator  NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
-- 双向 membership 清理(gate_owner/envelope_maint/runtime_owner 三个 owner 不应有 membership)
DO $$ DECLARE m record;
BEGIN
  FOR m IN SELECT DISTINCT roleid FROM pg_auth_members
           WHERE member IN ('gate_owner'::regrole,'envelope_maint'::regrole,'runtime_owner'::regrole)
  LOOP EXECUTE format('REVOKE %s FROM gate_owner, envelope_maint, runtime_owner', m.roleid::regrole::text); END LOOP;
  FOR m IN SELECT member FROM pg_auth_members WHERE roleid IN ('gate_owner'::regrole,'envelope_maint'::regrole,'runtime_owner'::regrole)
  LOOP EXECUTE format('REVOKE gate_owner FROM %s', m.member::regrole::text);
       EXECUTE format('REVOKE envelope_maint FROM %s', m.member::regrole::text);
       EXECUTE format('REVOKE runtime_owner FROM %s', m.member::regrole::text); END LOOP;
END $$;

-- ═══ 13. 触发器函数(保留 writer-gate/enforce_transition;新增 immutable_except_purge/guard/digest_check) ═══
CREATE OR REPLACE FUNCTION public._enforce_transition() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.skill_data_state IS DISTINCT FROM OLD.skill_data_state THEN
    IF current_user <> 'envelope_maint' THEN RAISE EXCEPTION 'by %', current_user; END IF;
    IF NOT ((OLD.skill_data_state='ACTIVE'  AND NEW.skill_data_state='PURGING')
         OR (OLD.skill_data_state='PURGING' AND NEW.skill_data_state='PURGED')) THEN
      RAISE EXCEPTION 'invalid';
    END IF;
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public._immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% immutable: % not allowed', TG_TABLE_NAME, TG_OP;
END; $$;

CREATE OR REPLACE FUNCTION public._immutable_except_purge() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION '% immutable: UPDATE not allowed', TG_TABLE_NAME;
  END IF;
  -- RI ON DELETE CASCADE executes nested triggers as the relation owner.
  -- Direct deletes remain envelope_maint-only; only a nested trigger cascade is allowed.
  IF TG_OP = 'DELETE' AND current_user <> 'envelope_maint' AND pg_trigger_depth() <= 1 THEN
    RAISE EXCEPTION '% immutable: DELETE only via purge (envelope_maint)', TG_TABLE_NAME;
  END IF;
  RETURN OLD;
END; $$;

CREATE OR REPLACE FUNCTION public._envelope_digest_check() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF encode(public.digest(NEW.content_bytes,'sha256'),'hex') <> NEW.content_digest THEN
    RAISE EXCEPTION 'envelope digest mismatch';
  END IF;
  IF NEW.size_bytes <> octet_length(NEW.content_bytes) THEN
    RAISE EXCEPTION 'envelope size mismatch';
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public._writer_gate() RETURNS TRIGGER
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v TEXT;
BEGIN
  SELECT t.skill_data_state INTO v FROM public.task_runs t WHERE t.run_id = NEW.run_id FOR KEY SHARE;
  IF v IS NULL THEN RAISE EXCEPTION 'nf'; END IF;
  IF v <> 'ACTIVE' THEN RAISE EXCEPTION 'is %', v; END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public._writer_gate_snapshot_job() RETURNS TRIGGER
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v TEXT; sr TEXT;
BEGIN
  SELECT t.skill_data_state INTO v FROM public.task_runs t WHERE t.run_id = NEW.run_id FOR KEY SHARE;
  IF v IS NULL THEN RAISE EXCEPTION 'nf'; END IF;
  IF v <> 'ACTIVE' THEN RAISE EXCEPTION 'is %', v; END IF;
  IF NEW.snapshot_id IS NOT NULL THEN
    SELECT rs.run_id INTO sr FROM public.run_snapshots rs WHERE rs.snapshot_id = NEW.snapshot_id;
    IF sr IS NULL THEN RAISE EXCEPTION 'snap nf'; END IF;
    IF sr <> NEW.run_id THEN RAISE EXCEPTION 'snap mismatch'; END IF;
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public._writer_gate_rollback() RETURNS TRIGGER
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE e INT; a INT := 0; rr RECORD;
BEGIN
  IF TG_OP='UPDATE' AND (OLD.parent_run_id IS DISTINCT FROM NEW.parent_run_id
     OR OLD.revert_run_id IS DISTINCT FROM NEW.revert_run_id) THEN RAISE EXCEPTION 'kc'; END IF;
  IF NEW.revert_run_id IS NOT NULL AND NEW.revert_run_id <> NEW.parent_run_id THEN e := 2; ELSE e := 1; END IF;
  a := 0;
  FOR rr IN SELECT t.run_id, t.skill_data_state FROM public.task_runs t
             WHERE t.run_id IN (NEW.parent_run_id, NEW.revert_run_id) ORDER BY t.run_id FOR KEY SHARE OF t
  LOOP
    a := a + 1;
    IF rr.skill_data_state IS NULL THEN RAISE EXCEPTION 'nl'; END IF;
    IF rr.skill_data_state <> 'ACTIVE' THEN RAISE EXCEPTION '% is %', rr.run_id, rr.skill_data_state; END IF;
  END LOOP;
  IF a <> e THEN RAISE EXCEPTION 'exp%,fnd%', e, a; END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public._guard_bound_run_pr_revision() RETURNS trigger
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE b boolean;
BEGIN
  SELECT EXISTS (SELECT 1 FROM public.revision_bindings WHERE run_id = NEW.run_id) INTO b;
  IF b AND ( (NEW.repo IS DISTINCT FROM OLD.repo)
          OR (NEW.pr_number IS DISTINCT FROM OLD.pr_number)
          OR (NEW.head_sha IS DISTINCT FROM OLD.head_sha) ) THEN
    RAISE EXCEPTION 'revision already bound';
  END IF;
  RETURN NEW;
END; $$;

ALTER FUNCTION public._writer_gate() OWNER TO gate_owner;
ALTER FUNCTION public._writer_gate_snapshot_job() OWNER TO gate_owner;
ALTER FUNCTION public._writer_gate_rollback() OWNER TO gate_owner;
ALTER FUNCTION public._guard_bound_run_pr_revision() OWNER TO gate_owner;
REVOKE ALL ON FUNCTION public._writer_gate() FROM PUBLIC;
REVOKE ALL ON FUNCTION public._writer_gate_snapshot_job() FROM PUBLIC;
REVOKE ALL ON FUNCTION public._writer_gate_rollback() FROM PUBLIC;
REVOKE ALL ON FUNCTION public._guard_bound_run_pr_revision() FROM PUBLIC;

-- ═══ 14. 触发器 ═══
DROP TRIGGER IF EXISTS trg_transition ON public.task_runs;
CREATE TRIGGER trg_transition BEFORE UPDATE OF skill_data_state ON public.task_runs
  FOR EACH ROW EXECUTE FUNCTION public._enforce_transition();

DROP TRIGGER IF EXISTS trg_envelope_immutable ON public.envelope_store;
CREATE TRIGGER trg_envelope_immutable BEFORE UPDATE OR DELETE ON public.envelope_store
  FOR EACH ROW EXECUTE FUNCTION public._immutable_except_purge();
DROP TRIGGER IF EXISTS trg_envelope_digest_check ON public.envelope_store;
CREATE TRIGGER trg_envelope_digest_check BEFORE INSERT ON public.envelope_store
  FOR EACH ROW EXECUTE FUNCTION public._envelope_digest_check();

DROP TRIGGER IF EXISTS trg_run_snapshots_immutable ON public.run_snapshots;
CREATE TRIGGER trg_run_snapshots_immutable BEFORE UPDATE OR DELETE ON public.run_snapshots
  FOR EACH ROW EXECUTE FUNCTION public._immutable_except_purge();

DROP TRIGGER IF EXISTS trg_skill_invocations_immutable ON public.skill_invocations;
CREATE TRIGGER trg_skill_invocations_immutable BEFORE UPDATE OR DELETE ON public.skill_invocations
  FOR EACH ROW EXECUTE FUNCTION public._immutable_except_purge();

DROP TRIGGER IF EXISTS trg_revision_bindings_immutable ON public.revision_bindings;
CREATE TRIGGER trg_revision_bindings_immutable BEFORE UPDATE OR DELETE ON public.revision_bindings
  FOR EACH ROW EXECUTE FUNCTION public._immutable();

DROP TRIGGER IF EXISTS trg_skill_version_registry_immutable ON public.skill_version_registry;
CREATE TRIGGER trg_skill_version_registry_immutable BEFORE UPDATE OR DELETE ON public.skill_version_registry
  FOR EACH ROW EXECUTE FUNCTION public._immutable();

DROP TRIGGER IF EXISTS trg_run_pr_bindings_revision_guard ON public.run_pr_bindings;
CREATE TRIGGER trg_run_pr_bindings_revision_guard BEFORE UPDATE OF repo, pr_number, head_sha ON public.run_pr_bindings
  FOR EACH ROW EXECUTE FUNCTION public._guard_bound_run_pr_revision();

DO $$ DECLARE t text[]; mapping text[] := ARRAY[
  ['run_snapshots','trg_gate_run_snapshots','_writer_gate'],
  ['snapshot_job_outbox','trg_gate_snapshot_job_outbox','_writer_gate_snapshot_job'],
  ['skill_job_outbox','trg_gate_skill_job_outbox','_writer_gate'],
  ['skill_invocations','trg_gate_skill_invocations','_writer_gate'],
  ['dispatch_outbox','trg_gate_dispatch_outbox','_writer_gate'],
  ['approvals','trg_gate_approvals','_writer_gate'],
  ['policy_action_outbox','trg_gate_policy_action_outbox','_writer_gate'],
  ['stage_runs','trg_gate_stage_runs','_writer_gate'],
  ['rollback_runs','trg_gate_rollback_runs','_writer_gate_rollback']
];
BEGIN
  FOREACH t SLICE 1 IN ARRAY mapping LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON public.%I', t[2], t[1]);
    EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.%I()', t[2], t[1], t[3]);
  END LOOP;
END $$;

-- ═══ 14.5 SD API 函数(Stage 2.1B-1:生产者侧 put_envelope/bind_revision/enqueue_snapshot_job/enqueue_skill_job) ═══

-- canon_str 辅助:长度前缀, NULL→'-1:'
CREATE OR REPLACE FUNCTION public._canon_str(v text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN v IS NULL THEN '-1:' ELSE octet_length(v)::text || ':' || v END $$;
ALTER FUNCTION public._canon_str(text) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public._canon_str(text) FROM PUBLIC;

-- _utf16_sortkey:UTF-16 code unit big-endian sort key(BMP→1 codeunit,non-BMP→surrogate pair)
CREATE OR REPLACE FUNCTION public._utf16_sortkey(p_text text) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE v_result bytea := '\x'::bytea; v_cp int; v_hi int; v_lo int;
BEGIN
  IF p_text IS NULL THEN RETURN '\x'::bytea; END IF;
  FOR i IN 1..char_length(p_text) LOOP
    v_cp := ascii(substr(p_text, i, 1));
    IF v_cp <= 65535 THEN
      v_result := v_result || decode(lpad(to_hex(v_cp), 4, '0'), 'hex');
    ELSE
      v_hi := 55296 + ((v_cp - 65536) >> 10);
      v_lo := 56320 + ((v_cp - 65536) & 1023);
      v_result := v_result || decode(lpad(to_hex(v_hi), 4, '0'), 'hex');
      v_result := v_result || decode(lpad(to_hex(v_lo), 4, '0'), 'hex');
    END IF;
  END LOOP;
  RETURN v_result;
END; $$;
ALTER FUNCTION public._utf16_sortkey(text) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public._utf16_sortkey(text) FROM PUBLIC;

-- _jcs_number:ECMAScript/JCS NumberToString(shortest round-trip, -0→0, lowercase e, no leading zeros)
-- Takes float8 (already validated finite + range by caller); does NOT use float8::text directly as oracle.
-- Parses float8::text for shortest mantissa, then applies ECMA-262 §7.1.12.1 formatting rules.
CREATE OR REPLACE FUNCTION public._jcs_number(p_float float8) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  v_raw text; v_sign text := ''; v_mant text; v_epos int; v_exp int;
  v_dotpos int; v_intpart text; v_fracpart text; v_digits text;
  v_k int; v_n int;
BEGIN
  IF p_float = 0 THEN RETURN '0'; END IF;
  v_raw := p_float::text;
  IF v_raw IN ('Infinity','-Infinity','NaN') THEN
    RAISE EXCEPTION '_jcs_number: non-finite' USING ERRCODE='P0001'; END IF;
  IF left(v_raw,1) = '-' THEN v_sign := '-'; v_raw := substr(v_raw,2); END IF;
  v_epos := position('e' IN lower(v_raw));
  IF v_epos = 0 THEN
    RETURN v_sign || v_raw;
  END IF;
  v_mant := substr(v_raw, 1, v_epos - 1);
  v_exp := substr(v_raw, v_epos + 1)::int;
  v_dotpos := position('.' IN v_mant);
  IF v_dotpos > 0 THEN
    v_intpart := substr(v_mant, 1, v_dotpos - 1);
    v_fracpart := substr(v_mant, v_dotpos + 1);
  ELSE
    v_intpart := v_mant; v_fracpart := '';
  END IF;
  v_digits := v_intpart || v_fracpart;
  v_k := char_length(v_digits);
  v_n := char_length(v_intpart) + v_exp;
  IF v_k <= v_n AND v_n <= 21 THEN
    RETURN v_sign || v_digits || repeat('0', v_n - v_k);
  ELSIF 0 < v_n AND v_n <= 21 THEN
    RETURN v_sign || substr(v_digits,1,v_n) || '.' || substr(v_digits,v_n+1);
  ELSIF -6 < v_n AND v_n <= 0 THEN
    RETURN v_sign || '0.' || repeat('0', -v_n) || v_digits;
  ELSE
    IF v_k = 1 THEN
      RETURN v_sign || v_digits || 'e' || CASE WHEN v_n-1 >= 0 THEN '+' ELSE '-' END || abs(v_n-1)::text;
    ELSE
      RETURN v_sign || substr(v_digits,1,1) || '.' || substr(v_digits,2) || 'e'
        || CASE WHEN v_n-1 >= 0 THEN '+' ELSE '-' END || abs(v_n-1)::text;
    END IF;
  END IF;
END; $$;
ALTER FUNCTION public._jcs_number(float8) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public._jcs_number(float8) FROM PUBLIC;

-- _jcs_escape:JCS §3.2.2.2 string serialization(short escapes for \b\t\n\f\r, \u00xx for other <0x20)
CREATE OR REPLACE FUNCTION public._jcs_escape(p_str text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE v_out text := '"'; v_char int;
BEGIN
  IF p_str IS NULL THEN RETURN '""'; END IF;
  FOR i IN 1..char_length(p_str) LOOP
    v_char := ascii(substr(p_str, i, 1));
    IF v_char = 34 THEN v_out := v_out || chr(92) || '"';        -- \"
    ELSIF v_char = 92 THEN v_out := v_out || chr(92) || chr(92); -- \\
    ELSIF v_char = 8 THEN v_out := v_out || chr(92) || 'b';      -- \b
    ELSIF v_char = 9 THEN v_out := v_out || chr(92) || 't';      -- \t
    ELSIF v_char = 10 THEN v_out := v_out || chr(92) || 'n';     -- \n
    ELSIF v_char = 12 THEN v_out := v_out || chr(92) || 'f';     -- \f
    ELSIF v_char = 13 THEN v_out := v_out || chr(92) || 'r';     -- \r
    ELSIF v_char < 32 THEN v_out := v_out || chr(92) || 'u' || lpad(to_hex(v_char), 4, '0');
    ELSE v_out := v_out || chr(v_char); END IF;
  END LOOP;
  RETURN v_out || '"';
END; $$;
ALTER FUNCTION public._jcs_escape(text) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public._jcs_escape(text) FROM PUBLIC;

-- canonical_json:MergePilot JCS Profile v1(UTF-16 key sort, JCS escape, ECMAScript number, |int|≤2^53 reject)
CREATE OR REPLACE FUNCTION public.canonical_json(p_input jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  v_type text := jsonb_typeof(p_input); v_result text; v_num numeric; v_float float8;
  v_elem jsonb; v_keys text[]; i int;
BEGIN
  IF v_type = 'null' THEN RETURN 'null';
  ELSIF v_type = 'boolean' THEN RETURN p_input::text;
  ELSIF v_type = 'number' THEN
    v_num := p_input::numeric;
    BEGIN v_float := v_num::float8; EXCEPTION WHEN OTHERS THEN
      RAISE EXCEPTION 'canonical_json: number not float8-representable' USING ERRCODE='P0001'; END;
    IF v_float::text IN ('Infinity','-Infinity','NaN') THEN
      RAISE EXCEPTION 'canonical_json: non-finite number' USING ERRCODE='P0001'; END IF;
    IF v_num = trunc(v_num) AND abs(v_num) > 9007199254740992 THEN
      RAISE EXCEPTION 'canonical_json: integer exceeds safe range (|n|>2^53)' USING ERRCODE='P0001'; END IF;
    RETURN public._jcs_number(v_float);
  ELSIF v_type = 'string' THEN
    RETURN public._jcs_escape(p_input #>> '{}');
  ELSIF v_type = 'array' THEN
    v_result := '[';
    FOR v_elem IN SELECT * FROM jsonb_array_elements(p_input) LOOP
      v_result := v_result || CASE WHEN v_result = '[' THEN '' ELSE ',' END || public.canonical_json(v_elem);
    END LOOP;
    RETURN v_result || ']';
  ELSIF v_type = 'object' THEN
    SELECT array_agg(key ORDER BY public._utf16_sortkey(key)) INTO v_keys
      FROM jsonb_object_keys(p_input) AS key;
    v_result := '{';
    IF v_keys IS NOT NULL THEN
      FOR i IN 1..array_length(v_keys, 1) LOOP
        v_result := v_result || CASE WHEN v_result = '{' THEN '' ELSE ',' END
          || public._jcs_escape(v_keys[i]) || ':' || public.canonical_json(p_input -> v_keys[i]);
      END LOOP;
    END IF;
    RETURN v_result || '}';
  ELSE RAISE EXCEPTION 'canonical_json: unknown json type %', v_type USING ERRCODE='P0001';
  END IF;
END; $$;
ALTER FUNCTION public.canonical_json(jsonb) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public.canonical_json(jsonb) FROM PUBLIC;

-- 1. put_envelope:内容寻址存储,幂等,不可覆盖,MergePilot JCS Profile v1 pre-jsonb ingress
-- _check_json_ingress:recursive pre-jsonb validation(dup keys, U+0000, surrogates, profile numbers)
CREATE OR REPLACE FUNCTION public._check_json_ingress(p_json json) RETURNS void
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  v_type text := json_typeof(p_json); v_key text; v_val json;
  v_str text; v_char int; v_num numeric; v_float float8; v_dups int;
BEGIN
  IF v_type = 'object' THEN
    SELECT count(*) INTO v_dups FROM (
      SELECT key, count(*) AS c FROM json_object_keys(p_json) AS key GROUP BY key HAVING count(*) > 1) d;
    IF v_dups > 0 THEN RAISE EXCEPTION 'duplicate object key' USING ERRCODE = 'P0001'; END IF;
    FOR v_key, v_val IN SELECT key, value FROM json_each(p_json) LOOP
      PERFORM public._check_json_ingress(v_val);
    END LOOP;
  ELSIF v_type = 'array' THEN
    FOR v_val IN SELECT value FROM json_array_elements(p_json) LOOP
      PERFORM public._check_json_ingress(v_val);
    END LOOP;
  ELSIF v_type = 'string' THEN
    -- U+0000 cannot reach the recursion: raw 0x00 is rejected by convert_from
    -- (Phase 1a, 22007) and the six-byte U+0000 escape is rejected by the json
    -- parser (Phase 1b, 22P05). chr(0) itself raises 54000 in PG16, so a NUL
    -- check on an already-parsed json value is neither possible nor needed;
    -- only check for lone surrogates (U+D800..U+DFFF) here.
    v_str := p_json #>> '{}';
    FOR i IN 1..char_length(v_str) LOOP
      v_char := ascii(substr(v_str, i, 1));
      IF v_char >= 55296 AND v_char <= 57343 THEN
        RAISE EXCEPTION 'invalid Unicode scalar' USING ERRCODE = 'P0001';
      END IF;
    END LOOP;
  ELSIF v_type = 'number' THEN
    BEGIN
      v_num := (p_json #>> '{}')::numeric;
      v_float := v_num::float8;
      IF v_float::text IN ('Infinity','-Infinity','NaN') THEN
        RAISE EXCEPTION 'number outside MergePilot JCS profile' USING ERRCODE = 'P0001'; END IF;
      IF v_num = trunc(v_num) AND abs(v_num) > 9007199254740992 THEN
        RAISE EXCEPTION 'number outside MergePilot JCS profile' USING ERRCODE = 'P0001'; END IF;
    EXCEPTION WHEN OTHERS THEN
      RAISE EXCEPTION 'number outside MergePilot JCS profile' USING ERRCODE = 'P0001';
    END;
  END IF;
END; $$;
ALTER FUNCTION public._check_json_ingress(json) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public._check_json_ingress(json) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.put_envelope(p_content_bytes bytea, p_content_type text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_digest text; v_size int; v_text text; v_json json; v_jsonb jsonb; v_err text; v_detail text; v_existing record;
BEGIN
  IF p_content_type NOT IN ('application/vnd.mergepilot.skill-request.v1+json',
    'application/vnd.mergepilot.skill-response.v1+json',
    'application/vnd.mergepilot.snapshot-manifest.v1+json') THEN
    RAISE EXCEPTION 'put_envelope: invalid content_type' USING ERRCODE = 'P0001'; END IF;
  v_size := octet_length(p_content_bytes);
  IF v_size IS NULL OR v_size <= 0 OR v_size > 1048576 THEN
    RAISE EXCEPTION 'put_envelope: size out of range (%)', v_size USING ERRCODE = 'P0001'; END IF;
 -- Phase 1a: reject a raw NUL byte before PostgreSQL text conversion.
 IF position(decode('00','hex') IN p_content_bytes) > 0 THEN
   RAISE EXCEPTION 'put_envelope: U+0000 not allowed' USING ERRCODE = 'P0001';
 END IF;
 -- Phase 1a: strict UTF-8
  BEGIN v_text := convert_from(p_content_bytes, 'UTF8');
  EXCEPTION WHEN OTHERS THEN RAISE EXCEPTION 'put_envelope: invalid UTF-8 or JSON' USING ERRCODE = 'P0001'; END;
  -- Phase 1b: parse without classifying raw substrings. A literal "\\u0000"
  -- is valid JSON data; only parser/materialization errors are mapped below.
  BEGIN
    v_json := v_text::json;
  EXCEPTION
    WHEN SQLSTATE '22P05' THEN
      RAISE EXCEPTION 'put_envelope: U+0000 not allowed' USING ERRCODE = 'P0001';
    WHEN invalid_text_representation THEN
      GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT, v_detail = PG_EXCEPTION_DETAIL;
      IF lower(coalesce(v_err,'') || ' ' || coalesce(v_detail,'')) LIKE '%surrogate%' THEN
        RAISE EXCEPTION 'put_envelope: invalid Unicode scalar' USING ERRCODE = 'P0001';
      END IF;
      RAISE EXCEPTION 'put_envelope: invalid UTF-8 or JSON' USING ERRCODE = 'P0001';
    WHEN OTHERS THEN
      GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT, v_detail = PG_EXCEPTION_DETAIL;
      IF lower(coalesce(v_err,'') || ' ' || coalesce(v_detail,'')) LIKE '%surrogate%' THEN
        RAISE EXCEPTION 'put_envelope: invalid Unicode scalar' USING ERRCODE = 'P0001';
      END IF;
      RAISE EXCEPTION 'put_envelope: invalid UTF-8 or JSON' USING ERRCODE = 'P0001';
  END;
  -- Phase 1c: semantic profile validation and jsonb materialization.
  BEGIN
    PERFORM public._check_json_ingress(v_json);
    v_jsonb := v_json::jsonb;
  EXCEPTION
    WHEN SQLSTATE 'P0001' THEN
      RAISE;
    WHEN SQLSTATE '22P05' THEN
      RAISE EXCEPTION 'put_envelope: U+0000 not allowed' USING ERRCODE = 'P0001';
    WHEN invalid_text_representation THEN
      GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT, v_detail = PG_EXCEPTION_DETAIL;
      IF lower(coalesce(v_err,'') || ' ' || coalesce(v_detail,'')) LIKE '%surrogate%' THEN
        RAISE EXCEPTION 'put_envelope: invalid Unicode scalar' USING ERRCODE = 'P0001';
      END IF;
      RAISE EXCEPTION 'put_envelope: invalid UTF-8 or JSON' USING ERRCODE = 'P0001';
    WHEN OTHERS THEN
      GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT, v_detail = PG_EXCEPTION_DETAIL;
      IF lower(coalesce(v_err,'') || ' ' || coalesce(v_detail,'')) LIKE '%surrogate%' THEN
        RAISE EXCEPTION 'put_envelope: invalid Unicode scalar' USING ERRCODE = 'P0001';
      END IF;
      RAISE EXCEPTION 'put_envelope: invalid UTF-8 or JSON' USING ERRCODE = 'P0001';
  END;
  -- Compute digest on raw bytes
  v_digest := encode(public.digest(p_content_bytes,'sha256'),'hex');
  -- INSERT with immutable content_type reconcile
  INSERT INTO public.envelope_store(content_digest,content_bytes,content_json,content_type,size_bytes)
    VALUES (v_digest,p_content_bytes,v_jsonb,p_content_type,v_size)
    ON CONFLICT (content_digest) DO NOTHING;
  SELECT content_bytes,content_json,content_type,size_bytes INTO v_existing
    FROM public.envelope_store WHERE content_digest=v_digest;
  IF NOT FOUND OR v_existing.content_bytes IS DISTINCT FROM p_content_bytes
     OR v_existing.content_json IS DISTINCT FROM v_jsonb
     OR v_existing.content_type IS DISTINCT FROM p_content_type
     OR v_existing.size_bytes IS DISTINCT FROM v_size THEN
    RAISE EXCEPTION 'put_envelope: immutable payload conflict for existing digest' USING ERRCODE='P0001';
  END IF;
  RETURN v_digest;
END; $$;

-- 2. bind_revision:单 revision authority,不泄漏 23505
CREATE OR REPLACE FUNCTION public.bind_revision(
  p_run_id text, p_repo text, p_pr_number int, p_head_sha text, p_base_sha text,
  p_source_call_id text, p_source_evidence_digest text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_sds text; v_rpb record; v_mc record; v_recomputed text; v_bid text; v_existing record; v_ins text;
BEGIN
  -- 1. lock task_runs FOR UPDATE, require ACTIVE
  SELECT skill_data_state INTO v_sds FROM public.task_runs WHERE run_id=p_run_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'bind_revision: run not found' USING ERRCODE='P0001'; END IF;
  IF v_sds <> 'ACTIVE' THEN RAISE EXCEPTION 'skill data is not ACTIVE' USING ERRCODE='P0001'; END IF;
  -- 2. run_pr_bindings FOR SHARE, validate repo/pr/head
  SELECT repo,pr_number,head_sha INTO v_rpb FROM public.run_pr_bindings WHERE run_id=p_run_id FOR SHARE;
  IF NOT FOUND OR v_rpb.repo IS DISTINCT FROM p_repo OR v_rpb.pr_number IS DISTINCT FROM p_pr_number
     OR v_rpb.head_sha IS DISTINCT FROM p_head_sha THEN
    RAISE EXCEPTION 'revision binding mismatch' USING ERRCODE='P0001'; END IF;
  -- 3. mcp_calls RESULT provenance
  SELECT phase,decision,result_status,run_id,target_repo,git_sha,correlation_id,tool INTO v_mc
    FROM public.mcp_calls WHERE request_id=p_source_call_id;
  IF NOT FOUND OR v_mc.phase <> 'RESULT' OR v_mc.decision <> 'ALLOW' OR v_mc.result_status <> 'OK'
     OR v_mc.run_id IS DISTINCT FROM p_run_id OR v_mc.target_repo IS DISTINCT FROM p_repo
     OR v_mc.git_sha IS NULL OR v_mc.git_sha <> p_base_sha THEN
    RAISE EXCEPTION 'revision provenance mismatch' USING ERRCODE='P0001'; END IF;
  -- 4. recompute evidence digest
  v_recomputed := encode(public.digest(
    public._canon_str(p_source_call_id)||public._canon_str(v_mc.correlation_id)||public._canon_str(v_mc.tool)||
    public._canon_str(v_mc.target_repo)||public._canon_str(v_mc.run_id)||public._canon_str(v_mc.git_sha)||
    public._canon_str(v_mc.result_status),'sha256'),'hex');
  IF v_recomputed <> p_source_evidence_digest THEN
    RAISE EXCEPTION 'revision evidence digest mismatch' USING ERRCODE='P0001'; END IF;
  -- 5. compute binding_id (H_32)
  v_bid := 'rev-'||left(encode(public.digest(
    public._canon_str(p_run_id)||public._canon_str(p_repo)||public._canon_str(p_pr_number::text)||
    public._canon_str(p_base_sha)||public._canon_str(p_head_sha)||public._canon_str(p_source_call_id)||
    public._canon_str(p_source_evidence_digest),'sha256'),'hex'),32);
  -- pre-reconcile by run_id
  SELECT binding_id,repo,pr_number,base_sha,head_sha,source_call_id,source_evidence_digest
    INTO v_existing FROM public.revision_bindings WHERE run_id=p_run_id;
  IF FOUND THEN
    IF v_existing.binding_id=v_bid AND v_existing.repo=p_repo AND v_existing.pr_number=p_pr_number
       AND v_existing.base_sha=p_base_sha AND v_existing.head_sha=p_head_sha
       AND v_existing.source_call_id=p_source_call_id AND v_existing.source_evidence_digest=p_source_evidence_digest THEN
      RETURN v_existing.binding_id; -- idempotent replay
    ELSE
      RAISE EXCEPTION 'revision binding conflict' USING ERRCODE='P0001'; END IF;
  END IF;
  -- not found → INSERT ON CONFLICT DO NOTHING RETURNING
  BEGIN
    INSERT INTO public.revision_bindings(binding_id,run_id,repo,pr_number,base_sha,head_sha,source_call_id,source_evidence_digest)
      VALUES (v_bid,p_run_id,p_repo,p_pr_number,p_base_sha,p_head_sha,p_source_call_id,p_source_evidence_digest)
      ON CONFLICT DO NOTHING RETURNING binding_id INTO v_ins;
    IF v_ins IS NOT NULL THEN RETURN v_bid; END IF;
  EXCEPTION WHEN SQLSTATE '23505' THEN NULL; END; -- never propagate 23505
  -- conflict after INSERT (concurrent or H_32 collision): dual-key re-read
  SELECT binding_id,repo,pr_number,base_sha,head_sha,source_call_id,source_evidence_digest
    INTO v_existing FROM public.revision_bindings WHERE run_id=p_run_id OR binding_id=v_bid;
  IF v_existing.binding_id=v_bid AND v_existing.repo=p_repo AND v_existing.pr_number=p_pr_number
     AND v_existing.base_sha=p_base_sha AND v_existing.head_sha=p_head_sha
     AND v_existing.source_call_id=p_source_call_id AND v_existing.source_evidence_digest=p_source_evidence_digest THEN
    RETURN v_bid; -- concurrent idempotent
  ELSE
    RAISE EXCEPTION 'revision binding conflict' USING ERRCODE='P0001'; END IF;
END; $$;

-- Final producer definitions: full immutable reconcile and row-lock serialization.
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

-- Internal profile parser used by complete APIs when validation must precede storage.
CREATE OR REPLACE FUNCTION public._profile_json(p_content_bytes bytea) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE v_size int; v_text text; v_json json; v_jsonb jsonb; v_err text; v_detail text;
BEGIN
  v_size:=octet_length(p_content_bytes);
  IF v_size IS NULL OR v_size<=0 OR v_size>1048576 THEN
    RAISE EXCEPTION 'profile JSON: size out of range' USING ERRCODE='P0001';
  END IF;
  IF position(decode('00','hex') IN p_content_bytes)>0 THEN
    RAISE EXCEPTION 'profile JSON: U+0000 not allowed' USING ERRCODE='P0001';
  END IF;
  BEGIN v_text:=convert_from(p_content_bytes,'UTF8');
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'profile JSON: invalid UTF-8 or JSON' USING ERRCODE='P0001';
  END;
  BEGIN v_json:=v_text::json;
  EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS v_err=MESSAGE_TEXT,v_detail=PG_EXCEPTION_DETAIL;
    IF SQLSTATE='22P05' OR lower(coalesce(v_detail,'')) LIKE '%u+0000%' THEN
      RAISE EXCEPTION 'profile JSON: U+0000 not allowed' USING ERRCODE='P0001';
    ELSIF lower(coalesce(v_err,'')||' '||coalesce(v_detail,'')) LIKE '%surrogate%' THEN
      RAISE EXCEPTION 'profile JSON: invalid Unicode scalar' USING ERRCODE='P0001';
    END IF;
    RAISE EXCEPTION 'profile JSON: invalid UTF-8 or JSON' USING ERRCODE='P0001';
  END;
  BEGIN
    PERFORM public._check_json_ingress(v_json);
    v_jsonb:=v_json::jsonb;
  EXCEPTION
    WHEN SQLSTATE 'P0001' THEN RAISE;
    WHEN OTHERS THEN
      GET STACKED DIAGNOSTICS v_err=MESSAGE_TEXT,v_detail=PG_EXCEPTION_DETAIL;
      IF SQLSTATE='22P05' OR lower(coalesce(v_detail,'')) LIKE '%u+0000%' THEN
        RAISE EXCEPTION 'profile JSON: U+0000 not allowed' USING ERRCODE='P0001';
      ELSIF lower(coalesce(v_err,'')||' '||coalesce(v_detail,'')) LIKE '%surrogate%' THEN
        RAISE EXCEPTION 'profile JSON: invalid Unicode scalar' USING ERRCODE='P0001';
      END IF;
      RAISE EXCEPTION 'profile JSON: invalid UTF-8 or JSON' USING ERRCODE='P0001';
  END;
  RETURN v_jsonb;
END; $$;

CREATE OR REPLACE FUNCTION public.complete_skill_job(
  p_job_id text, p_claim_id uuid, p_response_bytes bytea,
  p_expected_output_schema_digest text, p_output_schema_validated boolean) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE
  v_run_id text; v_state text; v_job record; v_live boolean; v_invocation_id text;
  v_response jsonb; v_keys text[]; v_required text[]:=ARRAY[
    'name','version','contract_version','request_id','trace_id','status','error_code',
    'warning_codes','degradations','message','output','evidence','artifacts','started_at',
    'duration_ms','retryable','side_effects','redactions'];
  v_status text; v_error text; v_verdict text; v_request_id text; v_output_digest text;
  v_manifest_digest text; v_duration int; v_started timestamptz; v_registry_digest text;
  v_existing record;
BEGIN
  SELECT run_id INTO v_run_id FROM public.skill_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state<>'ACTIVE' THEN RETURN NULL; END IF;
  SELECT * INTO v_job FROM public.skill_job_outbox
    WHERE job_id=p_job_id AND run_id=v_run_id FOR UPDATE;
  IF NOT FOUND THEN RETURN NULL; END IF;
  v_live:=v_job.status='LEASED' AND v_job.claim_id=p_claim_id AND v_job.lease_expires_at>now();
  v_invocation_id:='inv-'||left(encode(public.digest(
    public._canon_str(p_job_id)||public._canon_str(p_claim_id::text),'sha256'),'hex'),32);
  IF NOT v_live AND NOT (v_job.status='SUCCEEDED' AND v_job.claim_id=p_claim_id
                         AND v_job.result_invocation_id=v_invocation_id) THEN
    RETURN NULL;
  END IF;

  v_response:=public._profile_json(p_response_bytes);
  IF jsonb_typeof(v_response)<>'object' OR NOT (v_response ?& v_required) THEN
    RAISE EXCEPTION 'complete_skill_job: response required fields' USING ERRCODE='P0001';
  END IF;
  SELECT array_agg(k) INTO v_keys FROM jsonb_object_keys(v_response) AS k;
  IF EXISTS (SELECT 1 FROM unnest(v_keys) AS k WHERE k NOT IN (
      'name','version','contract_version','request_id','trace_id','status','error_code',
      'warning_codes','degradations','message','output','truncated','evidence','artifacts',
      'started_at','duration_ms','retryable','side_effects','redactions')) THEN
    RAISE EXCEPTION 'complete_skill_job: response extra top-level key' USING ERRCODE='P0001';
  END IF;
  IF jsonb_typeof(v_response->'name')<>'string'
     OR jsonb_typeof(v_response->'version')<>'string'
     OR jsonb_typeof(v_response->'contract_version')<>'string'
     OR jsonb_typeof(v_response->'request_id')<>'string'
     OR jsonb_typeof(v_response->'trace_id')<>'string'
     OR jsonb_typeof(v_response->'status')<>'string'
     OR jsonb_typeof(v_response->'message')<>'string'
     OR jsonb_typeof(v_response->'started_at')<>'string'
     OR jsonb_typeof(v_response->'duration_ms')<>'number'
     OR jsonb_typeof(v_response->'retryable')<>'boolean'
     OR jsonb_typeof(v_response->'output')<>'object'
     OR jsonb_typeof(v_response->'warning_codes')<>'array'
     OR jsonb_typeof(v_response->'degradations')<>'array'
     OR jsonb_typeof(v_response->'evidence')<>'array'
     OR jsonb_typeof(v_response->'artifacts')<>'array'
     OR jsonb_typeof(v_response->'side_effects')<>'array'
     OR jsonb_typeof(v_response->'redactions')<>'array'
     OR (v_response ? 'truncated' AND jsonb_typeof(v_response->'truncated')<>'boolean')
     OR jsonb_typeof(v_response->'error_code') NOT IN ('string','null') THEN
    RAISE EXCEPTION 'complete_skill_job: response field type mismatch' USING ERRCODE='P0001';
  END IF;

  SELECT content_json->>'request_id' INTO v_request_id FROM public.envelope_store
    WHERE content_digest=v_job.request_envelope_ref;
  IF v_response->>'name' IS DISTINCT FROM v_job.skill_name
     OR v_response->>'version' IS DISTINCT FROM v_job.skill_version
     OR v_response->>'contract_version' IS DISTINCT FROM '1'
     OR v_response->>'trace_id' IS DISTINCT FROM v_job.trace_id
     OR v_response->>'request_id' IS DISTINCT FROM v_request_id THEN
    RAISE EXCEPTION 'complete_skill_job: response/job binding mismatch' USING ERRCODE='P0001';
  END IF;
  BEGIN
    IF (v_response->>'duration_ms')::numeric<>trunc((v_response->>'duration_ms')::numeric)
       OR (v_response->>'duration_ms')::numeric<0 THEN RAISE EXCEPTION 'bad duration'; END IF;
    v_duration:=(v_response->>'duration_ms')::int;
    v_started:=(v_response->>'started_at')::timestamptz;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'complete_skill_job: invalid duration or started_at' USING ERRCODE='P0001';
  END;

  v_status:=v_response->>'status'; v_error:=v_response->>'error_code';
  IF v_status NOT IN ('OK','PARTIAL','ERROR') THEN
    RAISE EXCEPTION 'complete_skill_job: invalid status' USING ERRCODE='P0001';
  END IF;
  IF (v_status IN ('OK','PARTIAL') AND v_error IS NOT NULL)
     OR (v_status='ERROR' AND (v_error IS NULL OR v_error NOT IN (
       'INVALID_INPUT','SCHEMA_VERSION_UNSUPPORTED','TIMEOUT','DENIED',
       'DEPENDENCY_UNAVAILABLE','OUTPUT_TOO_LARGE','INTERNAL_ERROR'))) THEN
    RAISE EXCEPTION 'complete_skill_job: status/error_code mismatch' USING ERRCODE='P0001';
  END IF;
  IF v_status='PARTIAL' AND jsonb_array_length(v_response->'warning_codes')=0
     AND jsonb_array_length(v_response->'degradations')=0 THEN
    RAISE EXCEPTION 'complete_skill_job: PARTIAL requires warning or degradation' USING ERRCODE='P0001';
  END IF;

  SELECT output_schema_digest INTO v_registry_digest FROM public.skill_version_registry
    WHERE skill_name=v_job.skill_name AND skill_version=v_job.skill_version;
  IF NOT FOUND OR v_registry_digest IS DISTINCT FROM p_expected_output_schema_digest THEN
    RAISE EXCEPTION 'complete_skill_job: output schema registry mismatch' USING ERRCODE='P0001';
  END IF;
  IF p_output_schema_validated IS NULL
     OR (v_status IN ('OK','PARTIAL') AND NOT p_output_schema_validated)
     OR (v_status='ERROR' AND NOT p_output_schema_validated AND v_response->'output'<>'{}'::jsonb) THEN
    RAISE EXCEPTION 'complete_skill_job: status-aware validation mismatch' USING ERRCODE='P0001';
  END IF;
  v_verdict:=v_response->'output'->>'verdict';
  IF v_verdict IS NOT NULL AND v_verdict NOT IN ('PASS','FAIL','TIMEOUT','ERROR') THEN
    RAISE EXCEPTION 'complete_skill_job: invalid verdict' USING ERRCODE='P0001';
  END IF;
  IF NOT p_output_schema_validated AND v_verdict IS NOT NULL THEN
    RAISE EXCEPTION 'complete_skill_job: unvalidated output has verdict' USING ERRCODE='P0001';
  END IF;
  IF v_job.skill_name='test-runner' AND p_output_schema_validated AND v_verdict IS NULL THEN
    RAISE EXCEPTION 'complete_skill_job: test-runner verdict required' USING ERRCODE='P0001';
  END IF;

  SELECT manifest_digest INTO v_manifest_digest FROM public.run_snapshots
    WHERE run_id=v_job.run_id AND snapshot_id=v_job.snapshot_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'complete_skill_job: snapshot missing' USING ERRCODE='P0001'; END IF;
  v_output_digest:=public.put_envelope(p_response_bytes,
    'application/vnd.mergepilot.skill-response.v1+json');

  INSERT INTO public.skill_invocations(
    invocation_id,run_id,snapshot_id,job_id,trace_id,skill_name,skill_version,attempt,
    request_id,contract_version,status,error_code,verdict,input_digest,output_digest,
    snapshot_manifest_digest,expected_output_schema_digest,output_schema_validated,
    duration_ms,started_at,finished_at,idempotency_key)
  VALUES (v_invocation_id,v_job.run_id,v_job.snapshot_id,v_job.job_id,v_job.trace_id,
    v_job.skill_name,v_job.skill_version,v_job.attempt,v_request_id,'1',v_status,v_error,v_verdict,
    v_job.request_envelope_ref,v_output_digest,v_manifest_digest,p_expected_output_schema_digest,
    p_output_schema_validated,v_duration,v_started,now(),v_invocation_id)
  ON CONFLICT (invocation_id) DO NOTHING;

  SELECT * INTO v_existing FROM public.skill_invocations WHERE invocation_id=v_invocation_id;
  IF NOT FOUND OR v_existing.run_id IS DISTINCT FROM v_job.run_id
     OR v_existing.snapshot_id IS DISTINCT FROM v_job.snapshot_id
     OR v_existing.job_id IS DISTINCT FROM v_job.job_id
     OR v_existing.trace_id IS DISTINCT FROM v_job.trace_id
     OR v_existing.skill_name IS DISTINCT FROM v_job.skill_name
     OR v_existing.skill_version IS DISTINCT FROM v_job.skill_version
     OR v_existing.attempt IS DISTINCT FROM v_job.attempt
     OR v_existing.request_id IS DISTINCT FROM v_request_id
     OR v_existing.status IS DISTINCT FROM v_status
     OR v_existing.error_code IS DISTINCT FROM v_error
     OR v_existing.verdict IS DISTINCT FROM v_verdict
     OR v_existing.input_digest IS DISTINCT FROM v_job.request_envelope_ref
     OR v_existing.output_digest IS DISTINCT FROM v_output_digest
     OR v_existing.snapshot_manifest_digest IS DISTINCT FROM v_manifest_digest
     OR v_existing.expected_output_schema_digest IS DISTINCT FROM p_expected_output_schema_digest
     OR v_existing.output_schema_validated IS DISTINCT FROM p_output_schema_validated
     OR v_existing.duration_ms IS DISTINCT FROM v_duration
     OR v_existing.started_at IS DISTINCT FROM v_started
     OR v_existing.idempotency_key IS DISTINCT FROM v_invocation_id THEN
    RAISE EXCEPTION 'complete_skill_job: idempotency conflict' USING ERRCODE='P0001';
  END IF;

  IF v_live THEN
    UPDATE public.skill_job_outbox SET status='SUCCEEDED',result_invocation_id=v_invocation_id,
      completed_at=now(),error=NULL WHERE job_id=p_job_id AND status='LEASED' AND claim_id=p_claim_id;
  END IF;
  RETURN v_invocation_id;
END; $$;

CREATE OR REPLACE FUNCTION public.complete_snapshot_job(
  p_job_id text, p_claim_id uuid, p_manifest_bytes bytea, p_set_active boolean DEFAULT true) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE
  v_run_id text; v_state text; v_job record; v_live boolean; v_manifest jsonb; v_keys text[];
  v_binding record; v_item jsonb; v_sorted_items jsonb; v_canonical_manifest jsonb;
  v_canonical_bytes bytea; v_manifest_digest text; v_snapshot_id text; v_existing record;
  v_ordinal int:=0; v_count int; v_produced timestamptz;
BEGIN
  SELECT run_id INTO v_run_id FROM public.snapshot_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state<>'ACTIVE' THEN RETURN NULL; END IF;
  SELECT * INTO v_job FROM public.snapshot_job_outbox
    WHERE job_id=p_job_id AND run_id=v_run_id FOR UPDATE;
  IF NOT FOUND THEN RETURN NULL; END IF;
  v_live:=v_job.status='LEASED' AND v_job.claim_id=p_claim_id AND v_job.lease_expires_at>now();
  IF NOT v_live AND NOT (v_job.status='SUCCEEDED' AND v_job.claim_id=p_claim_id
                         AND v_job.snapshot_id IS NOT NULL) THEN RETURN NULL; END IF;

  v_manifest:=public._profile_json(p_manifest_bytes);
  IF jsonb_typeof(v_manifest)<>'object'
     OR NOT (v_manifest ?& ARRAY['manifest_version','run_id','base_sha','head_sha','produced_at','items']) THEN
    RAISE EXCEPTION 'complete_snapshot_job: manifest required fields' USING ERRCODE='P0001';
  END IF;
  SELECT array_agg(k) INTO v_keys FROM jsonb_object_keys(v_manifest) AS k;
  IF EXISTS (SELECT 1 FROM unnest(v_keys) AS k WHERE k NOT IN
    ('manifest_version','run_id','base_sha','head_sha','produced_at','items'))
     OR jsonb_typeof(v_manifest->'manifest_version')<>'string'
     OR jsonb_typeof(v_manifest->'run_id')<>'string'
     OR jsonb_typeof(v_manifest->'base_sha')<>'string'
     OR jsonb_typeof(v_manifest->'head_sha')<>'string'
     OR jsonb_typeof(v_manifest->'produced_at')<>'string'
     OR jsonb_typeof(v_manifest->'items')<>'array' THEN
    RAISE EXCEPTION 'complete_snapshot_job: manifest shape mismatch' USING ERRCODE='P0001';
  END IF;
  IF v_manifest->>'manifest_version'<>'1' THEN
    RAISE EXCEPTION 'complete_snapshot_job: manifest_version not 1' USING ERRCODE='P0001';
  END IF;
  BEGIN v_produced:=(v_manifest->>'produced_at')::timestamptz;
  EXCEPTION WHEN OTHERS THEN RAISE EXCEPTION 'complete_snapshot_job: invalid produced_at' USING ERRCODE='P0001'; END;

  SELECT * INTO v_binding FROM public.revision_bindings WHERE binding_id=v_job.revision_binding_id;
  IF NOT FOUND OR v_manifest->>'run_id' IS DISTINCT FROM v_job.run_id
     OR v_manifest->>'base_sha' IS DISTINCT FROM v_binding.base_sha
     OR v_manifest->>'head_sha' IS DISTINCT FROM v_binding.head_sha THEN
    RAISE EXCEPTION 'complete_snapshot_job: revision sha mismatch' USING ERRCODE='P0001';
  END IF;

  FOR v_item IN SELECT value FROM jsonb_array_elements(v_manifest->'items') LOOP
    IF jsonb_typeof(v_item)<>'object' OR NOT (v_item ?& ARRAY['kind','skill','skill_version','digest'])
       OR (SELECT count(*) FROM jsonb_object_keys(v_item))<>4
       OR jsonb_typeof(v_item->'kind')<>'string' OR v_item->>'kind'<>'skill-input'
       OR jsonb_typeof(v_item->'skill')<>'string'
       OR jsonb_typeof(v_item->'skill_version')<>'string'
       OR jsonb_typeof(v_item->'digest')<>'string' THEN
      RAISE EXCEPTION 'complete_snapshot_job: invalid manifest item' USING ERRCODE='P0001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.envelope_store
      WHERE content_digest=v_item->>'digest'
        AND content_type='application/vnd.mergepilot.skill-request.v1+json')
       OR NOT EXISTS (SELECT 1 FROM public.skill_version_registry
      WHERE skill_name=v_item->>'skill' AND skill_version=v_item->>'skill_version') THEN
      RAISE EXCEPTION 'complete_snapshot_job: manifest item reference mismatch' USING ERRCODE='P0001';
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_manifest->'items') AS i
             GROUP BY i->>'skill',i->>'skill_version' HAVING count(*)>1) THEN
    RAISE EXCEPTION 'complete_snapshot_job: duplicate manifest skill/version' USING ERRCODE='P0001';
  END IF;
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'kind','skill-input','skill',i->>'skill','skill_version',i->>'skill_version','digest',i->>'digest')
      ORDER BY public._utf16_sortkey(i->>'skill'),i->>'skill_version',i->>'digest'),'[]'::jsonb)
    INTO v_sorted_items FROM jsonb_array_elements(v_manifest->'items') AS i;
  v_canonical_manifest:=jsonb_build_object('manifest_version','1','run_id',v_job.run_id,
    'base_sha',v_binding.base_sha,'head_sha',v_binding.head_sha,
    'produced_at',v_manifest->>'produced_at','items',v_sorted_items);
  v_canonical_bytes:=convert_to(public.canonical_json(v_canonical_manifest),'UTF8');
  v_manifest_digest:=encode(public.digest(v_canonical_bytes,'sha256'),'hex');
  v_snapshot_id:='snap-'||left(encode(public.digest(
    public._canon_str(v_job.run_id)||public._canon_str(v_binding.base_sha)||
    public._canon_str(v_binding.head_sha)||public._canon_str(v_manifest_digest),'sha256'),'hex'),24);

  IF NOT v_live AND v_job.snapshot_id IS DISTINCT FROM v_snapshot_id THEN
    RAISE EXCEPTION 'complete_snapshot_job: idempotency conflict' USING ERRCODE='P0001';
  END IF;
  PERFORM public.put_envelope(v_canonical_bytes,
    'application/vnd.mergepilot.snapshot-manifest.v1+json');
  INSERT INTO public.run_snapshots(snapshot_id,run_id,repo,pr_number,base_sha,head_sha,manifest_digest,incomplete)
    VALUES (v_snapshot_id,v_job.run_id,v_binding.repo,v_binding.pr_number,v_binding.base_sha,
      v_binding.head_sha,v_manifest_digest,false) ON CONFLICT DO NOTHING;
  SELECT * INTO v_existing FROM public.run_snapshots WHERE snapshot_id=v_snapshot_id;
  IF NOT FOUND OR v_existing.run_id IS DISTINCT FROM v_job.run_id
     OR v_existing.repo IS DISTINCT FROM v_binding.repo
     OR v_existing.pr_number IS DISTINCT FROM v_binding.pr_number
     OR v_existing.base_sha IS DISTINCT FROM v_binding.base_sha
     OR v_existing.head_sha IS DISTINCT FROM v_binding.head_sha
     OR v_existing.manifest_digest IS DISTINCT FROM v_manifest_digest
     OR v_existing.incomplete THEN
    RAISE EXCEPTION 'complete_snapshot_job: snapshot idempotency conflict' USING ERRCODE='P0001';
  END IF;

  v_ordinal:=0;
  FOR v_item IN SELECT value FROM jsonb_array_elements(v_sorted_items) LOOP
    INSERT INTO public.snapshot_manifest_items(snapshot_id,ordinal,skill_name,skill_version,request_envelope_ref)
      VALUES (v_snapshot_id,v_ordinal,v_item->>'skill',v_item->>'skill_version',v_item->>'digest')
      ON CONFLICT DO NOTHING;
    v_ordinal:=v_ordinal+1;
  END LOOP;
  SELECT count(*) INTO v_count FROM public.snapshot_manifest_items WHERE snapshot_id=v_snapshot_id;
  IF v_count<>jsonb_array_length(v_sorted_items) OR EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_sorted_items) WITH ORDINALITY AS x(item,ord)
    LEFT JOIN public.snapshot_manifest_items AS r ON r.snapshot_id=v_snapshot_id AND r.ordinal=x.ord-1
    WHERE r.snapshot_id IS NULL OR r.skill_name IS DISTINCT FROM x.item->>'skill'
      OR r.skill_version IS DISTINCT FROM x.item->>'skill_version'
      OR r.request_envelope_ref IS DISTINCT FROM x.item->>'digest') THEN
    RAISE EXCEPTION 'complete_snapshot_job: manifest item relational mismatch' USING ERRCODE='P0001';
  END IF;

  IF v_live THEN
    UPDATE public.snapshot_job_outbox SET status='SUCCEEDED',snapshot_id=v_snapshot_id,
      completed_at=now(),error=NULL WHERE job_id=p_job_id AND status='LEASED' AND claim_id=p_claim_id;
  END IF;
  IF p_set_active AND (SELECT active_snapshot_id FROM public.task_runs WHERE run_id=v_job.run_id) IS NULL THEN
    UPDATE public.task_runs SET active_snapshot_id=v_snapshot_id WHERE run_id=v_job.run_id AND active_snapshot_id IS NULL;
  END IF;
  RETURN v_snapshot_id;
END; $$;

CREATE OR REPLACE FUNCTION public.request_purge(p_run_id text,p_requested_by text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_state text; v_existing record; v_requested_at timestamptz; v_purge_id text;
BEGIN
  IF nullif(btrim(p_requested_by),'') IS NULL THEN
    RAISE EXCEPTION 'request_purge: requested_by required' USING ERRCODE='P0001';
  END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=p_run_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'request_purge: run not found' USING ERRCODE='P0001'; END IF;
  SELECT purge_id,target_state,status,requested_by INTO v_existing FROM public.purge_requests
    WHERE run_id=p_run_id ORDER BY requested_at DESC LIMIT 1 FOR UPDATE;
  IF FOUND AND v_existing.status IN ('REQUESTED','PURGING','PURGED') THEN
    IF v_existing.target_state='PURGED' AND v_existing.requested_by=p_requested_by THEN
      RETURN v_existing.purge_id;
    END IF;
    RAISE EXCEPTION 'request_purge: request conflict' USING ERRCODE='P0001';
  END IF;
  IF v_state<>'ACTIVE' THEN
    RAISE EXCEPTION 'request_purge: skill data not ACTIVE' USING ERRCODE='P0001';
  END IF;
  v_requested_at:=now();
  v_purge_id:='pur-'||left(encode(public.digest(
    public._canon_str(p_run_id)||public._canon_str(p_requested_by)||
    public._canon_str(v_requested_at::text),'sha256'),'hex'),24);
  INSERT INTO public.purge_requests(purge_id,run_id,target_state,status,requested_by,requested_at)
    VALUES(v_purge_id,p_run_id,'PURGED','REQUESTED',p_requested_by,v_requested_at)
    ON CONFLICT DO NOTHING;
  SELECT purge_id,target_state,status,requested_by INTO v_existing FROM public.purge_requests
    WHERE purge_id=v_purge_id;
  IF NOT FOUND OR v_existing.target_state<>'PURGED' OR v_existing.status<>'REQUESTED'
     OR v_existing.requested_by<>p_requested_by THEN
    RAISE EXCEPTION 'request_purge: idempotency conflict' USING ERRCODE='P0001';
  END IF;
  RETURN v_purge_id;
END; $$;

CREATE OR REPLACE FUNCTION public.advance_purge(p_purge_id text) RETURNS text
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_request record; v_candidates text[];
BEGIN
  SELECT run_id INTO v_run_id FROM public.purge_requests WHERE purge_id=p_purge_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'advance_purge: request not found' USING ERRCODE='P0001'; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'advance_purge: run not found' USING ERRCODE='P0001'; END IF;
  SELECT * INTO v_request FROM public.purge_requests WHERE purge_id=p_purge_id AND run_id=v_run_id FOR UPDATE;
  IF v_request.status='PURGED' AND v_state='PURGED' THEN RETURN 'PURGED'; END IF;
  IF NOT ((v_request.status='REQUESTED' AND v_state='ACTIVE')
       OR (v_request.status='PURGING' AND v_state='PURGING')) THEN
    RAISE EXCEPTION 'advance_purge: state conflict' USING ERRCODE='P0001';
  END IF;

  SELECT COALESCE(array_agg(DISTINCT digest),ARRAY[]::text[]) INTO v_candidates FROM (
    SELECT input_digest AS digest FROM public.skill_invocations WHERE run_id=v_run_id
    UNION ALL SELECT output_digest FROM public.skill_invocations WHERE run_id=v_run_id AND output_digest IS NOT NULL
    UNION ALL SELECT snapshot_manifest_digest FROM public.skill_invocations WHERE run_id=v_run_id AND snapshot_manifest_digest IS NOT NULL
    UNION ALL SELECT request_envelope_ref FROM public.skill_job_outbox WHERE run_id=v_run_id
    UNION ALL SELECT manifest_digest FROM public.run_snapshots WHERE run_id=v_run_id
    UNION ALL SELECT i.request_envelope_ref FROM public.snapshot_manifest_items AS i
      JOIN public.run_snapshots AS s ON s.snapshot_id=i.snapshot_id WHERE s.run_id=v_run_id
  ) AS candidate_set;

  UPDATE public.task_runs SET active_snapshot_id=NULL WHERE run_id=v_run_id;
  IF v_request.status='REQUESTED' THEN
    UPDATE public.purge_requests SET status='PURGING',purging_at=now(),error=NULL
      WHERE purge_id=p_purge_id AND status='REQUESTED';
    UPDATE public.task_runs SET skill_data_state='PURGING'
      WHERE run_id=v_run_id AND skill_data_state='ACTIVE';
  END IF;

  DELETE FROM public.skill_job_outbox WHERE run_id=v_run_id;
  DELETE FROM public.snapshot_job_outbox WHERE run_id=v_run_id;
  DELETE FROM public.run_snapshots WHERE run_id=v_run_id;
  DELETE FROM public.envelope_store AS envelope
    WHERE envelope.content_digest=ANY(v_candidates)
      AND NOT EXISTS (SELECT 1 FROM public.skill_invocations i
        WHERE i.input_digest=envelope.content_digest OR i.output_digest=envelope.content_digest
           OR i.snapshot_manifest_digest=envelope.content_digest)
      AND NOT EXISTS (SELECT 1 FROM public.skill_job_outbox j
        WHERE j.request_envelope_ref=envelope.content_digest)
      AND NOT EXISTS (SELECT 1 FROM public.run_snapshots s
        WHERE s.manifest_digest=envelope.content_digest)
      AND NOT EXISTS (SELECT 1 FROM public.snapshot_manifest_items mi
        WHERE mi.request_envelope_ref=envelope.content_digest);

  UPDATE public.purge_requests SET status='PURGED',completed_at=now(),error=NULL
    WHERE purge_id=p_purge_id AND status='PURGING';
  UPDATE public.task_runs SET skill_data_state='PURGED'
    WHERE run_id=v_run_id AND skill_data_state='PURGING';
  IF NOT FOUND THEN RAISE EXCEPTION 'advance_purge: final state conflict' USING ERRCODE='P0001'; END IF;
  RETURN 'PURGED';
END; $$;

-- Worker lease state machine. Each entry locks task_runs before the outbox row.
CREATE OR REPLACE FUNCTION public.claim_snapshot_job(
  p_job_id text, p_worker text, p_lease_seconds int DEFAULT 60) RETURNS uuid
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_job record; v_claim uuid;
BEGIN
  IF p_lease_seconds NOT BETWEEN 1 AND 3600 OR nullif(btrim(p_worker),'') IS NULL THEN
    RAISE EXCEPTION 'claim_snapshot_job: invalid lease or worker' USING ERRCODE='P0001';
  END IF;
  SELECT run_id INTO v_run_id FROM public.snapshot_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN
    RAISE EXCEPTION 'claim_snapshot_job: skill data not ACTIVE' USING ERRCODE='P0001';
  END IF;
  SELECT status,attempts,max_attempts,next_retry_at,lease_expires_at INTO v_job
    FROM public.snapshot_job_outbox WHERE job_id=p_job_id AND run_id=v_run_id FOR UPDATE;
  IF NOT FOUND THEN RETURN NULL; END IF;
  IF v_job.status='LEASED' AND v_job.lease_expires_at<=now() AND v_job.attempts>=v_job.max_attempts THEN
    UPDATE public.snapshot_job_outbox SET status='FAILED',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,error='lease expired after max attempts',completed_at=now()
      WHERE job_id=p_job_id;
    RETURN NULL;
  END IF;
  IF NOT ((v_job.status='PENDING' AND v_job.next_retry_at<=now() AND v_job.attempts<v_job.max_attempts)
       OR (v_job.status='LEASED' AND v_job.lease_expires_at<=now() AND v_job.attempts<v_job.max_attempts)) THEN
    RETURN NULL;
  END IF;
  v_claim:=public.gen_random_uuid();
  UPDATE public.snapshot_job_outbox SET status='LEASED',claim_id=v_claim,leased_by=p_worker,
    lease_expires_at=now()+make_interval(secs=>p_lease_seconds),last_heartbeat_at=now(),
    attempts=attempts+1,claimed_at=now(),error=NULL,completed_at=NULL
    WHERE job_id=p_job_id;
  RETURN v_claim;
END; $$;

CREATE OR REPLACE FUNCTION public.claim_skill_job(
  p_job_id text, p_worker text, p_lease_seconds int DEFAULT 60) RETURNS uuid
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_job record; v_claim uuid;
BEGIN
  IF p_lease_seconds NOT BETWEEN 1 AND 3600 OR nullif(btrim(p_worker),'') IS NULL THEN
    RAISE EXCEPTION 'claim_skill_job: invalid lease or worker' USING ERRCODE='P0001';
  END IF;
  SELECT run_id INTO v_run_id FROM public.skill_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN
    RAISE EXCEPTION 'claim_skill_job: skill data not ACTIVE' USING ERRCODE='P0001';
  END IF;
  SELECT status,attempts,max_attempts,next_retry_at,lease_expires_at INTO v_job
    FROM public.skill_job_outbox WHERE job_id=p_job_id AND run_id=v_run_id FOR UPDATE;
  IF NOT FOUND THEN RETURN NULL; END IF;
  IF v_job.status='LEASED' AND v_job.lease_expires_at<=now() AND v_job.attempts>=v_job.max_attempts THEN
    UPDATE public.skill_job_outbox SET status='FAILED',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,error='lease expired after max attempts',completed_at=now()
      WHERE job_id=p_job_id;
    RETURN NULL;
  END IF;
  IF NOT ((v_job.status='PENDING' AND v_job.next_retry_at<=now() AND v_job.attempts<v_job.max_attempts
           AND NOT EXISTS (SELECT 1 FROM public.skill_job_dependencies AS d
             JOIN public.skill_job_outbox AS dependency ON dependency.job_id=d.depends_on_job_id
             WHERE d.job_id=p_job_id AND dependency.status<>'SUCCEEDED'))
       OR (v_job.status='LEASED' AND v_job.lease_expires_at<=now() AND v_job.attempts<v_job.max_attempts)) THEN
    RETURN NULL;
  END IF;
  v_claim:=public.gen_random_uuid();
  UPDATE public.skill_job_outbox SET status='LEASED',claim_id=v_claim,leased_by=p_worker,
    lease_expires_at=now()+make_interval(secs=>p_lease_seconds),last_heartbeat_at=now(),
    attempts=attempts+1,claimed_at=now(),error=NULL,completed_at=NULL
    WHERE job_id=p_job_id;
  RETURN v_claim;
END; $$;

CREATE OR REPLACE FUNCTION public.heartbeat_snapshot_job(
  p_job_id text, p_claim_id uuid, p_lease_seconds int DEFAULT 60) RETURNS boolean
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_rows int;
BEGIN
  IF p_lease_seconds NOT BETWEEN 1 AND 3600 THEN
    RAISE EXCEPTION 'heartbeat_snapshot_job: invalid lease' USING ERRCODE='P0001';
  END IF;
  SELECT run_id INTO v_run_id FROM public.snapshot_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN RETURN false; END IF;
  PERFORM 1 FROM public.snapshot_job_outbox WHERE job_id=p_job_id FOR UPDATE;
  UPDATE public.snapshot_job_outbox SET lease_expires_at=now()+make_interval(secs=>p_lease_seconds),
    last_heartbeat_at=now() WHERE job_id=p_job_id AND status='LEASED'
      AND claim_id=p_claim_id AND lease_expires_at>now();
  GET DIAGNOSTICS v_rows=ROW_COUNT;
  RETURN v_rows=1;
END; $$;

CREATE OR REPLACE FUNCTION public.heartbeat_skill_job(
  p_job_id text, p_claim_id uuid, p_lease_seconds int DEFAULT 60) RETURNS boolean
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_rows int;
BEGIN
  IF p_lease_seconds NOT BETWEEN 1 AND 3600 THEN
    RAISE EXCEPTION 'heartbeat_skill_job: invalid lease' USING ERRCODE='P0001';
  END IF;
  SELECT run_id INTO v_run_id FROM public.skill_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN RETURN false; END IF;
  PERFORM 1 FROM public.skill_job_outbox WHERE job_id=p_job_id FOR UPDATE;
  UPDATE public.skill_job_outbox SET lease_expires_at=now()+make_interval(secs=>p_lease_seconds),
    last_heartbeat_at=now() WHERE job_id=p_job_id AND status='LEASED'
      AND claim_id=p_claim_id AND lease_expires_at>now();
  GET DIAGNOSTICS v_rows=ROW_COUNT;
  RETURN v_rows=1;
END; $$;

CREATE OR REPLACE FUNCTION public.fail_snapshot_job(
  p_job_id text, p_claim_id uuid, p_error text) RETURNS boolean
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_job record; v_backoff double precision;
BEGIN
  IF nullif(btrim(p_error),'') IS NULL THEN RAISE EXCEPTION 'fail_snapshot_job: error required' USING ERRCODE='P0001'; END IF;
  SELECT run_id INTO v_run_id FROM public.snapshot_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN RETURN false; END IF;
  SELECT attempts,max_attempts INTO v_job FROM public.snapshot_job_outbox
    WHERE job_id=p_job_id AND status='LEASED' AND claim_id=p_claim_id
      AND lease_expires_at>now() FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  IF v_job.attempts<v_job.max_attempts THEN
    v_backoff:=least(60::double precision,2::double precision*power(2::double precision,v_job.attempts-1));
    UPDATE public.snapshot_job_outbox SET status='PENDING',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,next_retry_at=now()+make_interval(secs=>v_backoff),
      error=p_error,completed_at=NULL WHERE job_id=p_job_id;
  ELSE
    UPDATE public.snapshot_job_outbox SET status='FAILED',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,error=p_error,completed_at=now() WHERE job_id=p_job_id;
  END IF;
  RETURN true;
END; $$;

CREATE OR REPLACE FUNCTION public.fail_skill_job(
  p_job_id text, p_claim_id uuid, p_error text) RETURNS boolean
SECURITY DEFINER SET search_path=pg_catalog LANGUAGE plpgsql AS $$
DECLARE v_run_id text; v_state text; v_job record; v_backoff double precision;
BEGIN
  IF nullif(btrim(p_error),'') IS NULL THEN RAISE EXCEPTION 'fail_skill_job: error required' USING ERRCODE='P0001'; END IF;
  SELECT run_id INTO v_run_id FROM public.skill_job_outbox WHERE job_id=p_job_id;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT skill_data_state INTO v_state FROM public.task_runs WHERE run_id=v_run_id FOR SHARE;
  IF NOT FOUND OR v_state <> 'ACTIVE' THEN RETURN false; END IF;
  SELECT attempts,max_attempts INTO v_job FROM public.skill_job_outbox
    WHERE job_id=p_job_id AND status='LEASED' AND claim_id=p_claim_id
      AND lease_expires_at>now() FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  IF v_job.attempts<v_job.max_attempts THEN
    v_backoff:=least(60::double precision,2::double precision*power(2::double precision,v_job.attempts-1));
    UPDATE public.skill_job_outbox SET status='PENDING',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,next_retry_at=now()+make_interval(secs=>v_backoff),
      error=p_error,completed_at=NULL WHERE job_id=p_job_id;
  ELSE
    UPDATE public.skill_job_outbox SET status='FAILED',claim_id=NULL,leased_by=NULL,
      lease_expires_at=NULL,last_heartbeat_at=NULL,error=p_error,completed_at=now() WHERE job_id=p_job_id;
  END IF;
  RETURN true;
END; $$;

-- owner + REVOKE PUBLIC for producer and worker SD functions
ALTER FUNCTION public.put_envelope(bytea,text) OWNER TO runtime_owner;
ALTER FUNCTION public.bind_revision(text,text,int,text,text,text,text) OWNER TO runtime_owner;
ALTER FUNCTION public.enqueue_snapshot_job(text,text) OWNER TO runtime_owner;
ALTER FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) OWNER TO runtime_owner;
ALTER FUNCTION public._profile_json(bytea) OWNER TO runtime_owner;
ALTER FUNCTION public.complete_skill_job(text,uuid,bytea,text,boolean) OWNER TO runtime_owner;
ALTER FUNCTION public.complete_snapshot_job(text,uuid,bytea,boolean) OWNER TO runtime_owner;
ALTER FUNCTION public.request_purge(text,text) OWNER TO envelope_maint;
ALTER FUNCTION public.advance_purge(text) OWNER TO envelope_maint;
ALTER FUNCTION public.claim_snapshot_job(text,text,int) OWNER TO runtime_owner;
ALTER FUNCTION public.claim_skill_job(text,text,int) OWNER TO runtime_owner;
ALTER FUNCTION public.heartbeat_snapshot_job(text,uuid,int) OWNER TO runtime_owner;
ALTER FUNCTION public.heartbeat_skill_job(text,uuid,int) OWNER TO runtime_owner;
ALTER FUNCTION public.fail_snapshot_job(text,uuid,text) OWNER TO runtime_owner;
ALTER FUNCTION public.fail_skill_job(text,uuid,text) OWNER TO runtime_owner;
REVOKE ALL ON FUNCTION public.put_envelope(bytea,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.bind_revision(text,text,int,text,text,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.enqueue_snapshot_job(text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public._profile_json(bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.complete_skill_job(text,uuid,bytea,text,boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.complete_snapshot_job(text,uuid,bytea,boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.request_purge(text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.advance_purge(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.claim_snapshot_job(text,text,int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.claim_skill_job(text,text,int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.heartbeat_snapshot_job(text,uuid,int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.heartbeat_skill_job(text,uuid,int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fail_snapshot_job(text,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fail_skill_job(text,uuid,text) FROM PUBLIC;

-- ═══ 15. ACL 收敛(REVOKE ALL → 精确 GRANT) ═══
-- owner 角色(gate_owner/envelope_maint/runtime_owner):全撤 schema/table/sequence/function
REVOKE ALL PRIVILEGES ON SCHEMA public FROM gate_owner, envelope_maint, runtime_owner;
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA public FROM gate_owner, envelope_maint, runtime_owner;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM gate_owner, envelope_maint, runtime_owner;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM gate_owner, envelope_maint, runtime_owner;
-- runtime worker 角色:全撤 DML(本轮无 SD EXECUTE 授予,后续增量补)
REVOKE ALL PRIVILEGES ON SCHEMA public FROM skill_runner, snapshot_worker, purge_operator;
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA public FROM skill_runner, snapshot_worker, purge_operator;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM skill_runner, snapshot_worker, purge_operator;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM skill_runner, snapshot_worker, purge_operator;

GRANT SELECT ON public.task_runs        TO gate_owner;
GRANT SELECT ON public.run_snapshots    TO gate_owner;
GRANT SELECT ON public.revision_bindings TO gate_owner;
GRANT UPDATE (skill_data_state) ON public.task_runs TO gate_owner;
GRANT EXECUTE ON FUNCTION public._writer_gate()                TO gate_owner;
GRANT EXECUTE ON FUNCTION public._writer_gate_snapshot_job()   TO gate_owner;
GRANT EXECUTE ON FUNCTION public._writer_gate_rollback()       TO gate_owner;

GRANT SELECT ON public.task_runs        TO envelope_maint;
GRANT UPDATE (skill_data_state) ON public.task_runs TO envelope_maint;
GRANT UPDATE (active_snapshot_id) ON public.task_runs TO envelope_maint;
GRANT SELECT ON public.envelope_store   TO envelope_maint;
GRANT INSERT ON public.envelope_store   TO envelope_maint;
GRANT SELECT ON public.purge_requests   TO envelope_maint;
GRANT INSERT ON public.purge_requests   TO envelope_maint;
GRANT UPDATE ON public.purge_requests   TO envelope_maint;
GRANT SELECT ON public.skill_invocations, public.skill_job_outbox,
  public.snapshot_job_outbox, public.run_snapshots, public.snapshot_manifest_items TO envelope_maint;
GRANT DELETE ON public.skill_job_outbox, public.snapshot_job_outbox,
  public.run_snapshots, public.envelope_store TO envelope_maint;
GRANT EXECUTE ON FUNCTION public._canon_str(text) TO envelope_maint;

-- worker 角色:SELECT 读权限;无 DML(SD API 后续增量授予 EXECUTE)
GRANT SELECT ON public.task_runs, public.run_snapshots, public.envelope_store,
  public.snapshot_job_outbox, public.skill_job_outbox, public.skill_invocations,
  public.snapshot_manifest_items, public.skill_version_registry, public.revision_bindings,
  public.skill_job_dependencies
  TO mergepilot, skill_runner, snapshot_worker, purge_operator;
-- runtime_owner (NOLOGIN SD function owner) table access for function bodies
GRANT SELECT ON public.task_runs, public.run_pr_bindings, public.mcp_calls, public.envelope_store,
             public.skill_version_registry, public.revision_bindings, public.snapshot_manifest_items,
             public.run_snapshots, public.skill_job_outbox, public.snapshot_job_outbox,
             public.skill_job_dependencies, public.skill_invocations TO runtime_owner;
GRANT INSERT ON public.envelope_store, public.revision_bindings, public.snapshot_job_outbox,
             public.skill_job_outbox, public.skill_job_dependencies, public.run_snapshots,
             public.snapshot_manifest_items, public.skill_invocations TO runtime_owner;
GRANT UPDATE ON public.snapshot_job_outbox, public.skill_job_outbox TO runtime_owner;
GRANT UPDATE ON public.task_runs, public.run_pr_bindings TO runtime_owner;
-- SD API EXECUTE grants(Stage 2.1B-1)
GRANT EXECUTE ON FUNCTION public._check_json_ingress(json) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public._profile_json(bytea) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public.put_envelope(bytea,text) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public._canon_str(text) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public._utf16_sortkey(text) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public._jcs_number(float8) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public._jcs_escape(text) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public.canonical_json(jsonb) TO runtime_owner;
GRANT EXECUTE ON FUNCTION public.put_envelope(bytea,text) TO mergepilot, skill_runner, snapshot_worker;
GRANT EXECUTE ON FUNCTION public.bind_revision(text,text,int,text,text,text,text) TO mergepilot;
GRANT EXECUTE ON FUNCTION public.enqueue_snapshot_job(text,text) TO mergepilot;
GRANT EXECUTE ON FUNCTION public.enqueue_skill_job(text,text,text,text,text,int,text,text[]) TO mergepilot;
GRANT EXECUTE ON FUNCTION public.complete_snapshot_job(text,uuid,bytea,boolean) TO snapshot_worker;
GRANT EXECUTE ON FUNCTION public.complete_skill_job(text,uuid,bytea,text,boolean) TO skill_runner;
GRANT EXECUTE ON FUNCTION public.claim_snapshot_job(text,text,int) TO snapshot_worker;
GRANT EXECUTE ON FUNCTION public.claim_skill_job(text,text,int) TO skill_runner;
GRANT EXECUTE ON FUNCTION public.heartbeat_snapshot_job(text,uuid,int) TO snapshot_worker;
GRANT EXECUTE ON FUNCTION public.heartbeat_skill_job(text,uuid,int) TO skill_runner;
GRANT EXECUTE ON FUNCTION public.fail_snapshot_job(text,uuid,text) TO snapshot_worker;
GRANT EXECUTE ON FUNCTION public.fail_skill_job(text,uuid,text) TO skill_runner;
GRANT EXECUTE ON FUNCTION public.request_purge(text,text) TO purge_operator;
GRANT EXECUTE ON FUNCTION public.advance_purge(text) TO purge_operator;

-- ═══ 16. catalog 自检(按函数名分别验证;非 owner 总数) ═══
DO $$
DECLARE bad int := 0; n int;
  FN text[] := ARRAY['_writer_gate','_writer_gate_snapshot_job','_writer_gate_rollback'];  -- 3 writer-gate BY NAME
BEGIN
  -- 16.1 3 writer-gate 函数 BY NAME:prosecdef + pronargs=0 + search_path=pg_catalog + owner=gate_owner + 无 PUBLIC EXECUTE
  SELECT count(*) INTO bad FROM unnest(FN) AS f
    WHERE NOT EXISTS (
      SELECT 1 FROM pg_proc p JOIN pg_namespace nn ON nn.oid=p.pronamespace
      WHERE nn.nspname='public' AND p.proname=f AND p.prosecdef AND p.pronargs=0
        AND p.proconfig IS NOT NULL AND array_position(p.proconfig,'search_path=pg_catalog') IS NOT NULL
        AND p.proowner='gate_owner'::regrole
        AND NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                        WHERE a.grantee=0 AND a.privilege_type='EXECUTE'));
  IF bad <> 0 THEN RAISE EXCEPTION 'self-check: % writer-gate fn missing/wrong (by name)', bad; END IF;

  -- 16.2 1 revision guard 函数 BY NAME(RETURNS trigger;prosecdef;owner=gate_owner;search_path;无 PUBLIC EXECUTE)
  IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace nn ON nn.oid=p.pronamespace
        JOIN pg_type rt ON rt.oid=p.prorettype
        WHERE nn.nspname='public' AND p.proname='_guard_bound_run_pr_revision' AND p.prosecdef
          AND p.proowner='gate_owner'::regrole
          AND p.proconfig IS NOT NULL AND array_position(p.proconfig,'search_path=pg_catalog') IS NOT NULL
          AND rt.typname='trigger'
          AND NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                          WHERE a.grantee=0 AND a.privilege_type='EXECUTE')) THEN
    RAISE EXCEPTION 'self-check: revision guard fn _guard_bound_run_pr_revision missing/wrong'; END IF;

  -- 16.3 gate_owner owner-总函数数(信息性 NOTICE,NOT PASS/FAIL 门禁;判定以 §16.1/16.2 by-name 为唯一权威)
  SELECT count(*) INTO n FROM pg_proc p JOIN pg_namespace nn ON nn.oid=p.pronamespace
    WHERE nn.nspname='public' AND p.proowner='gate_owner'::regrole;
  RAISE NOTICE 'self-check (info, not a gate): gate_owner owns % functions; by-name catalog (§16.1/16.2) is authoritative', n;

  -- 16.4 9 trg_gate_* 触发器(tgtype=23, enabled)+ 精确映射
  SELECT count(*) INTO bad FROM (VALUES
    ('run_snapshots','trg_gate_run_snapshots','_writer_gate'),
    ('snapshot_job_outbox','trg_gate_snapshot_job_outbox','_writer_gate_snapshot_job'),
    ('skill_job_outbox','trg_gate_skill_job_outbox','_writer_gate'),
    ('skill_invocations','trg_gate_skill_invocations','_writer_gate'),
    ('dispatch_outbox','trg_gate_dispatch_outbox','_writer_gate'),
    ('approvals','trg_gate_approvals','_writer_gate'),
    ('policy_action_outbox','trg_gate_policy_action_outbox','_writer_gate'),
    ('stage_runs','trg_gate_stage_runs','_writer_gate'),
    ('rollback_runs','trg_gate_rollback_runs','_writer_gate_rollback')
  ) AS m(tbl,trg,fn) WHERE NOT EXISTS (
    SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace nn ON nn.oid=c.relnamespace
      JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace
      WHERE nn.nspname='public' AND pn.nspname='public' AND c.relname=m.tbl AND t.tgname=m.trg
        AND p.proname=m.fn AND NOT t.tgisinternal AND t.tgtype=23 AND t.tgenabled='O');
  IF bad <> 0 THEN RAISE EXCEPTION 'self-check: % gate trigger mapping missing/wrong', bad; END IF;

  -- 16.5 immutable/guard 触发器 BY NAME
  SELECT count(*) INTO bad FROM (VALUES
    ('trg_envelope_immutable'),('trg_envelope_digest_check'),('trg_run_snapshots_immutable'),
    ('trg_skill_invocations_immutable'),('trg_revision_bindings_immutable'),
    ('trg_skill_version_registry_immutable'),
    ('trg_run_pr_bindings_revision_guard'),('trg_transition')) AS v(tn)
    WHERE NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
      JOIN pg_namespace nn ON nn.oid=c.relnamespace WHERE nn.nspname='public' AND t.tgname=v.tn AND NOT t.tgisinternal);
  IF bad <> 0 THEN RAISE EXCEPTION 'self-check: % named trigger missing', bad; END IF;

  -- 16.6 复合 FK + skill_job_dependencies FK/CK BY NAME
  SELECT count(*) INTO bad FROM (VALUES
    ('task_runs_active_snapshot_run_fkey'),('snapshot_job_outbox_run_snapshot_fkey'),
    ('skill_job_outbox_run_snapshot_fkey'),('skill_invocations_run_snapshot_fkey'),
    ('skill_invocations_run_job_fkey'),('skill_job_outbox_registry_fkey'),
    ('skill_invocations_registry_fkey'),('smi_registry_fkey'),
    ('skill_job_outbox_result_invocation_fkey'),
    ('skill_job_dependencies_job_fkey'),('skill_job_dependencies_dep_fkey'),('skill_job_dependencies_no_self')) AS v(cn)
    WHERE NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname=v.cn);
  IF bad <> 0 THEN RAISE EXCEPTION 'self-check: % composite/FK/CK missing', bad; END IF;

  -- 16.6.1 status-aware CK BY NAME(4 项)
  SELECT count(*) INTO bad FROM (VALUES
    ('sinv_status_validated'),('sinv_validated_verdict'),('sinv_status_err_req'),('sinv_status_err_ok')) AS v(cn)
    WHERE NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname=v.cn AND conrelid='public.skill_invocations'::regclass);
  IF bad <> 0 THEN RAISE EXCEPTION 'self-check: % status-aware CK missing', bad; END IF;

  -- 真正双向 EXCEPT(预期 EXCEPT 实际 与 实际 EXCEPT 预期 均为空)
  IF EXISTS (
    SELECT * FROM (VALUES
      ('diff-parse','1.0.0','89d628502dd726d6dfa1df4f52687bd51a1cea75d81e680a5025852f3b5b7285','e6e0eb2077645007de8115a0be697b27954e34b9a000e9bc7c6de03c27fd355b'),
      ('risk-classify','1.0.0','45ca36e3a5c6ff8146e13d7935918240279f1ffbc28872c8b1c04c81a3111371','b4d8e0519916cc21ea5286a677a94de53af2cb968073c1b06cf8b4d6ccbda09a'),
      ('sast-scan','1.0.0','8d008630393b59e77ed66669c2b5d6a45591dbbed5c3bc5554289035c5813598','fda15df57b9713bf76f95ff0668a8c76a8f7f68cabb40348232d571614e497e1'),
      ('test-runner','1.0.0','a90f67f1c19243582402d8e8b590f9a104a937637442be29a3d980848b9ecda9','461c5f026e01a4641acc0821220f6720361402ee2c3fc802421a6a11c41772d9'),
      ('case-retrieval','1.0.0','549526ab5aa410b67754a52ba7fcd826b2cc7813189eac0f929c5b53e666c3d3','4366b3e76796756158197b10c77c135b7d6443c9262ad9a5be5c03a60f662b57'),
      ('pr-lifecycle','1.0.0','7157df189df14d7128c3fe9f40e749050ed8251f206a7f5a57ca31da9859c424','ee27d6b587ca9b82d9da189ae98ca4a58437110ebe3ff75348506355c075dc1c')
    ) AS e(sn,sv,rid,oid)
    EXCEPT SELECT skill_name,skill_version,request_schema_digest,output_schema_digest FROM public.skill_version_registry
  ) THEN RAISE EXCEPTION 'self-check: registry drift (expected-not-actual)'; END IF;
  IF EXISTS (
    SELECT skill_name,skill_version,request_schema_digest,output_schema_digest FROM public.skill_version_registry
    EXCEPT SELECT * FROM (VALUES
      ('diff-parse','1.0.0','89d628502dd726d6dfa1df4f52687bd51a1cea75d81e680a5025852f3b5b7285','e6e0eb2077645007de8115a0be697b27954e34b9a000e9bc7c6de03c27fd355b'),
      ('risk-classify','1.0.0','45ca36e3a5c6ff8146e13d7935918240279f1ffbc28872c8b1c04c81a3111371','b4d8e0519916cc21ea5286a677a94de53af2cb968073c1b06cf8b4d6ccbda09a'),
      ('sast-scan','1.0.0','8d008630393b59e77ed66669c2b5d6a45591dbbed5c3bc5554289035c5813598','fda15df57b9713bf76f95ff0668a8c76a8f7f68cabb40348232d571614e497e1'),
      ('test-runner','1.0.0','a90f67f1c19243582402d8e8b590f9a104a937637442be29a3d980848b9ecda9','461c5f026e01a4641acc0821220f6720361402ee2c3fc802421a6a11c41772d9'),
      ('case-retrieval','1.0.0','549526ab5aa410b67754a52ba7fcd826b2cc7813189eac0f929c5b53e666c3d3','4366b3e76796756158197b10c77c135b7d6443c9262ad9a5be5c03a60f662b57'),
      ('pr-lifecycle','1.0.0','7157df189df14d7128c3fe9f40e749050ed8251f206a7f5a57ca31da9859c424','ee27d6b587ca9b82d9da189ae98ca4a58437110ebe3ff75348506355c075dc1c')
    ) AS e(sn,sv,rid,oid)
  ) THEN RAISE EXCEPTION 'self-check: registry drift (actual-not-expected)'; END IF;

  -- 16.8 角色 + 双向 membership=0(owner 三角色)
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('gate_owner','envelope_maint','runtime_owner')
             AND (rolcanlogin OR rolinherit OR rolbypassrls OR rolsuper OR rolcreatedb OR rolcreaterole)) THEN
    RAISE EXCEPTION 'self-check: owner role has disallowed attribute'; END IF;
  SELECT count(*) INTO n FROM pg_auth_members
    WHERE member IN ('gate_owner'::regrole,'envelope_maint'::regrole,'runtime_owner'::regrole)
       OR roleid IN ('gate_owner'::regrole,'envelope_maint'::regrole,'runtime_owner'::regrole);
  IF n <> 0 THEN RAISE EXCEPTION 'self-check: % disallowed membership', n; END IF;

RAISE NOTICE 'M4-F1 v2.8 implementation self-check PASS (by-name catalog)';
END $$;

COMMIT;
