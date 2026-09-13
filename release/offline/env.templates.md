# Offline delivery env files (create next to docker-compose.yml)
# The stack ships with working smoke values in: postgres.env / controller.env /
# gh_webhook.env / demo_console.env / .env (auto-written by start-stack.*).
#
# postgres.env
POSTGRES_USER=mergepilot
POSTGRES_PASSWORD=mergepilot_smoke
POSTGRES_DB=mergepilot_audit
#
# controller.env  (PG_PASS must match POSTGRES_PASSWORD)
PG_PASS=mergepilot_smoke
ADMIN_PW=smoke_admin
#
# gh_webhook.env  (DSN user/password seeded by db-init/003-logins.sql)
GITHUB_WEBHOOK_SECRET=smoke_hook
GITHUB_INGRESS_DSN=postgresql://github_event_ingress:smoke_ingest_2026@postgres:5432/mergepilot_audit
#
# demo_console.env (also consumed by preflight; application_name is PINNED
# by MERGEPILOT_PG_EXPECTED_APPLICATION_NAME in compose)
MERGEPILOT_PG_DSN=postgresql://mergepilot_reader:smoke_reader_2026@postgres:5432/mergepilot_audit?application_name=mergepilot_isolated_live_reader
#
# .env (written automatically by start-stack.bat / start-stack.sh)
MERGEPILOT_RUN_ID=offline-demo
MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES=<measured postgres container IP>
