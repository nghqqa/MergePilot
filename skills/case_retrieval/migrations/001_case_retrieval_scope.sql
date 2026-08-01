-- M4-E CaseRetrieval forward-only, idempotent migration.
-- Existing rows remain unscoped and therefore cannot be retrieved.

ALTER TABLE public.knowledge ADD COLUMN IF NOT EXISTS repo_scope TEXT;
ALTER TABLE public.knowledge ADD COLUMN IF NOT EXISTS source_pr_url TEXT;
ALTER TABLE public.knowledge ADD COLUMN IF NOT EXISTS source_commit_sha VARCHAR(40);
ALTER TABLE public.knowledge ADD COLUMN IF NOT EXISTS source_version TEXT;
ALTER TABLE public.knowledge ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE public.knowledge ADD COLUMN IF NOT EXISTS embedding_version TEXT;
ALTER TABLE public.knowledge ADD COLUMN IF NOT EXISTS adopted BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_knowledge_repo
    ON public.knowledge (repo_scope);

DO $case_retrieval_role$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'case_retrieval_reader'
    ) THEN
        CREATE ROLE case_retrieval_reader LOGIN;
    END IF;

    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO case_retrieval_reader',
        current_database()
    );
END
$case_retrieval_role$;

ALTER ROLE case_retrieval_reader
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

REVOKE ALL PRIVILEGES ON TABLE public.knowledge
    FROM case_retrieval_reader;
REVOKE CREATE ON SCHEMA public FROM case_retrieval_reader;
GRANT USAGE ON SCHEMA public TO case_retrieval_reader;
GRANT SELECT ON TABLE public.knowledge TO case_retrieval_reader;

ALTER ROLE case_retrieval_reader SET default_transaction_read_only = on;
ALTER ROLE case_retrieval_reader SET statement_timeout = '10s';
ALTER ROLE case_retrieval_reader SET lock_timeout = '5s';
ALTER ROLE case_retrieval_reader SET search_path = public;
ALTER ROLE case_retrieval_reader SET idle_in_transaction_session_timeout = '15s';
