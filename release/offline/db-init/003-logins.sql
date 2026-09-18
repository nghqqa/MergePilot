-- offline smoke delivery: runtime LOGIN roles get fixed demo passwords here.
-- Production deployments MUST override via ALTER ROLE at deploy time.
ALTER ROLE github_event_ingress WITH LOGIN PASSWORD 'smoke_ingest_2026';
ALTER ROLE github_check_publisher WITH LOGIN PASSWORD 'smoke_checks_2026';
ALTER ROLE mergepilot_reader WITH LOGIN PASSWORD 'smoke_reader_2026';
-- console contract: the viewer session must be read-only at BOTH the
-- transaction and default level (fail-closed NOT_READ_ONLY otherwise).
ALTER ROLE mergepilot_reader SET default_transaction_read_only = on;
