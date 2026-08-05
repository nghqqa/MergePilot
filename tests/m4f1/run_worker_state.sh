#!/usr/bin/env bash
# Real PG16 worker state-machine and two-connection claim gate.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT}/tools/test-env/mp_guard.sh"  # fail-closed: MergePilot-Test daemon only
DBDIR="$ROOT/tools/audit-db"; SQLDIR="$ROOT/tests/m4f1/sql"
IMG="pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
UNIQ="$$-$(date +%s)"; DB="m4f1-ws-${UNIQ}"; LABEL="m4f1-ws-${UNIQ}"
BASE="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
TMP_ROOT="$(mktemp -d)" || exit 1; rc=1
cleanup() {
  local exit_rc=$? containers=1 networks=1 temp_dirs=1 final_rc
  trap - EXIT; set +e; docker rm -f "$DB" >/dev/null 2>&1
  containers="$(docker ps -aq --filter "label=$LABEL"|wc -l)" || containers=1
  networks="$(docker network ls -q --filter "label=$LABEL"|wc -l)" || networks=1
  case "$TMP_ROOT" in /tmp/*) rm -rf -- "$TMP_ROOT" ;; *) echo "unsafe temp path" >&2 ;; esac
  [ ! -e "$TMP_ROOT" ] && temp_dirs=0
  final_rc=$exit_rc; [ "$final_rc" -ne 0 ] || final_rc=$rc
  if [ "$containers" -ne 0 ] || [ "$networks" -ne 0 ] || [ "$temp_dirs" -ne 0 ]; then final_rc=1; fi
  echo "RESIDUE containers=$containers networks=$networks temp_dirs=$temp_dirs"; exit "$final_rc"
}
trap cleanup EXIT
docker run -d --name "$DB" --label "$LABEL" -e POSTGRES_USER=mergepilot -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=app "$IMG" >/dev/null
ready=0; for _ in $(seq 1 60); do docker exec "$DB" psql -X -U mergepilot -d app -tAc 'SELECT 1' >/dev/null 2>&1 && { ready=1; break; }; sleep 1; done
[ "$ready" -eq 1 ]
docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 >/dev/null <<'EOSQL'
DO $roles$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='policy_gateway_l2') THEN CREATE ROLE policy_gateway_l2 NOLOGIN; END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mergepilot_approver') THEN CREATE ROLE mergepilot_approver NOLOGIN; END IF;
END $roles$;
EOSQL
for migration in $BASE; do docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 <"$DBDIR/${migration}.sql" >/dev/null 2>&1; done
for round in 1 2; do
  if ! docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 <"$DBDIR/m4f1_state.sql" >"$TMP_ROOT/m4f1-r${round}.out" 2>&1; then
    cat "$TMP_ROOT/m4f1-r${round}.out"; exit 1
  fi
  grep -q 'self-check PASS' "$TMP_ROOT/m4f1-r${round}.out"
done
if ! docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 <"$SQLDIR/producer_api_audit.sql" >"$TMP_ROOT/producer.out" 2>&1; then
  cat "$TMP_ROOT/producer.out"; exit 1
fi
grep -q 'PA-SET PASS: 25' "$TMP_ROOT/producer.out"
if ! docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 <"$SQLDIR/worker_state_audit.sql" >"$TMP_ROOT/worker.out" 2>&1; then
  cat "$TMP_ROOT/worker.out"; exit 1
fi
grep -q 'WA-SET PASS: 13' "$TMP_ROOT/worker.out"
echo "WORKER STATE PASS ids=13"

binding="$(docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT binding_id FROM public.revision_bindings WHERE run_id='pa_run3'")"
docker exec "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 -c "SELECT public.enqueue_snapshot_job('pa_run3','$binding')" >/dev/null
for side in a b; do
  cat >"$TMP_ROOT/claim-${side}.sql" <<'EOSQL'
\set ON_ERROR_STOP on
SET statement_timeout='10s'; SET lock_timeout='5s';
BEGIN;
SELECT coalesce(public.claim_snapshot_job('snapjob-pa_run3','race-worker',60)::text,'NULL');
SELECT pg_sleep(1);
COMMIT;
EOSQL
done
set +e
docker exec -i "$DB" psql -X -qAt -U mergepilot -d app -v ON_ERROR_STOP=1 <"$TMP_ROOT/claim-a.sql" >"$TMP_ROOT/claim-a.out" 2>&1 & pa=$!
docker exec -i "$DB" psql -X -qAt -U mergepilot -d app -v ON_ERROR_STOP=1 <"$TMP_ROOT/claim-b.sql" >"$TMP_ROOT/claim-b.out" 2>&1 & pb=$!
wait "$pa"; ra=$?; wait "$pb"; rb=$?; set -e
[ "$ra" -eq 0 ] && [ "$rb" -eq 0 ]
claims="$(grep -hE '^(NULL|[0-9a-f-]{36})$' "$TMP_ROOT/claim-a.out" "$TMP_ROOT/claim-b.out")"
[ "$(printf '%s\n' "$claims" | grep -c '^NULL$')" -eq 1 ]
[ "$(printf '%s\n' "$claims" | grep -cE '^[0-9a-f-]{36}$')" -eq 1 ]
[ "$(docker exec "$DB" psql -X -U mergepilot -d app -tAc "SELECT attempts FROM public.snapshot_job_outbox WHERE job_id='snapjob-pa_run3'")" -eq 1 ]
echo "WORKER CLAIM CONCURRENCY PASS one_claim_one_null attempts=1"
rc=0
