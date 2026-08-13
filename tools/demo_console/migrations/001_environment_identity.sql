-- MergePilot environment identity marker for ISOLATED_LIVE.
-- Single-row table; reader role gets SELECT only.
CREATE TABLE IF NOT EXISTS environment_identity (
    environment_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
-- Only one row allowed
CREATE UNIQUE INDEX IF NOT EXISTS environment_identity_single_row
    ON environment_identity ((1));
-- Reader gets SELECT only
GRANT SELECT ON environment_identity TO PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON environment_identity FROM PUBLIC;
