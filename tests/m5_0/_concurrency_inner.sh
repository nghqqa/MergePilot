#!/usr/bin/env bash
# Inner: temp PG16 + run the M5-0B concurrency/negative Python suite (P1-1/2/3/4).
# Two real candidate-role connections drive controller reconcile functions
# directly. hiclaw_live=false (isolated test PG, not shared production).
set -euo pipefail

ROOT="/mnt/d/goai/mergepilot-os"
# MergePilot test-env isolation guard (fail-closed: MergePilot-Test daemon only).
source "${ROOT}/tools/test-env/mp_guard.sh"
DBDIR="$ROOT/tools/audit-db"
PG_IMAGE="pgvector/pgvector:pg16"
RUNTIME_IMAGE="mergepilot-m4f-runtime:demo"

UNIQ="m5c-$$-$(date +%s)"
LABEL="mergepilot.m5c=${UNIQ}"
NET="m5c-net-${UNIQ}"
DB="m5c-pg-${UNIQ}"
DBNAME="m5ctest"

cleanup() {
  set +e
  docker rm -f "$DB" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  local C N
  C=$(docker ps -aq --filter "label=$LABEL" | wc -l)
  N=$(docker network ls --filter "label=$LABEL" -q | wc -l)
  echo "residue: containers=$C networks=$N"
}
trap cleanup EXIT

rand_hex() { head -c "$1" /dev/urandom | od -An -v -tx1 | tr -d ' \n'; }
printf -v M5C_PG_PW '%s' "$(rand_hex 16)"; export M5C_PG_PW

echo "=== M5-0B concurrency + negative suite (UNIQ=$UNIQ) ==="
docker network create --label "$LABEL" "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" --network-alias m5c-pg --label "$LABEL" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=fixture_admin -e POSTGRES_DB="$DBNAME" \
  "$PG_IMAGE" >/dev/null
for _ in $(seq 1 90); do
  docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c "SELECT 1" >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$DB" psql -U fixture_admin -d "$DBNAME" -c "SELECT 1" >/dev/null

docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
DO $r$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot') THEN CREATE ROLE mergepilot LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='policy_gateway_l2') THEN CREATE ROLE policy_gateway_l2 NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mergepilot_approver') THEN CREATE ROLE mergepilot_approver NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='policy_gateway_audit') THEN CREATE ROLE policy_gateway_audit LOGIN; END IF;
END $r$;
SQL

BASE_MIGS="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
for m in $BASE_MIGS; do
  docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 < "$DBDIR/${m}.sql" >/dev/null
done
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_state.sql" >/dev/null
docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_hotfix_1.sql" >/dev/null 2>&1 || true

docker exec -i "$DB" psql -U fixture_admin -d "$DBNAME" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
GRANT CONNECT ON DATABASE m5ctest TO mergepilot, policy_gateway_audit;
GRANT USAGE ON SCHEMA public TO mergepilot, policy_gateway_audit;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mergepilot;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO mergepilot;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO mergepilot;
SQL

ADMIN_DSN="host=m5c-pg dbname=$DBNAME user=fixture_admin"
CAND_DSN="host=m5c-pg dbname=$DBNAME user=mergepilot application_name=m5-conc-test"

docker run --rm --network "$NET" -v "$ROOT:/workspace:ro" \
  -e M5B_ADMIN_DSN="$ADMIN_DSN" -e M5B_CAND_DSN="$CAND_DSN" \
  -e M5B_PREFIX="m5con-" -e M5B_ROOM="!room:conc-hs" \
  --entrypoint python "$RUNTIME_IMAGE" \
  /workspace/tests/m5_0/fixtures/run_m5_concurrency.py
RC=$?
echo "CONCURRENCY_SUITE_RC=$RC"
exit $RC
