-- offline delivery: seed the ISOLATED_LIVE environment marker expected by
-- docker-compose (MERGEPILOT_PG_ENVIRONMENT_ID). Single-row table.
INSERT INTO environment_identity (environment_id)
VALUES ('mergepilot-test-ephemeral')
ON CONFLICT DO NOTHING;
