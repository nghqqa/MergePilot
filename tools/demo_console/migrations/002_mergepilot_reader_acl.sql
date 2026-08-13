-- MergePilot ISOLATED_LIVE viewer role deployment ACL.
-- This migration assumes the role 'mergepilot_reader' already exists
-- (created by the deploy script or DBA). If it does not exist, all
-- GRANT statements will fail and the migration must abort (fail-closed).
--
-- The role receives:
--   USAGE on schema public
--   SELECT on all 9 queried tables
--   No INSERT/UPDATE/DELETE/TRUNCATE/CREATE privileges

-- Fail-closed: role must pre-exist
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mergepilot_reader') THEN
        RAISE EXCEPTION 'mergepilot_reader role does not exist; create it before applying this migration';
    END IF;
END $$;

-- Schema access
GRANT USAGE ON SCHEMA public TO mergepilot_reader;

-- SELECT on all 9 queried tables (and ONLY SELECT)
GRANT SELECT ON task_runs TO mergepilot_reader;
GRANT SELECT ON stage_runs TO mergepilot_reader;
GRANT SELECT ON stage_events TO mergepilot_reader;
GRANT SELECT ON revision_bindings TO mergepilot_reader;
GRANT SELECT ON run_pr_bindings TO mergepilot_reader;
GRANT SELECT ON mcp_calls TO mergepilot_reader;
GRANT SELECT ON rollback_runs TO mergepilot_reader;
GRANT SELECT ON audit_events TO mergepilot_reader;
GRANT SELECT ON environment_identity TO mergepilot_reader;

-- Explicitly revoke all write privileges (defense in depth)
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON task_runs FROM mergepilot_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON stage_runs FROM mergepilot_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON stage_events FROM mergepilot_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON revision_bindings FROM mergepilot_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON run_pr_bindings FROM mergepilot_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON mcp_calls FROM mergepilot_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON rollback_runs FROM mergepilot_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON audit_events FROM mergepilot_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON environment_identity FROM mergepilot_reader;

-- No schema-level write
REVOKE CREATE ON SCHEMA public FROM mergepilot_reader;
