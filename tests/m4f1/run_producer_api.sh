#!/usr/bin/env bash
# tests/m4f1/run_producer_api.sh — Stage 2.1B-1 生产者侧 SD API 门禁。
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DBDIR="$ROOT/tools/audit-db"; SQLDIR="$ROOT/tests/m4f1/sql"
IMG="pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
UNIQ="$$-$(date +%s)"; DB="m4f1-pa-${UNIQ}"; LABEL="m4f1-pa-${UNIQ}"
BASE="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
r1=; r2=; arc=; rc=1
TMPDIR="$(mktemp -d)" || { echo "mktemp failed" >&2; exit 1; }
cleanup() { exit_rc=$?; set +e; docker rm -f "$DB" >/dev/null 2>&1 || true
  if ! c=$(docker ps -aq --filter label="$LABEL" | wc -l); then c=1; fi
  if ! n=$(docker network ls -q --filter label="$LABEL" | wc -l); then n=1; fi
  tmp=0; case "$TMPDIR" in /tmp/*) rm -rf -- "$TMPDIR" ;; *) tmp=1 ;; esac
  [ ! -e "$TMPDIR" ] || tmp=1
  final_rc="$exit_rc"; [ "$exit_rc" -ne 0 ] || final_rc="$rc"
  [ "$c" -ne 0 ] || [ "$n" -ne 0 ] || [ "$tmp" -ne 0 ] && final_rc=1
  echo "RESIDUE containers=$c networks=$n temp_dirs=$tmp"; trap - EXIT; exit "$final_rc"; }
trap cleanup EXIT
docker run -d --name "$DB" --label "$LABEL" -e POSTGRES_USER=mergepilot -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=mergepilot_audit "$IMG" >/dev/null
for i in $(seq 1 60); do docker exec "$DB" psql -U mergepilot -d mergepilot_audit -tAc "SELECT 1" >/dev/null 2>&1 && { sleep 2; break; }; sleep 1; done
docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 -c "DO \$d\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='policy_gateway_l2') THEN CREATE ROLE policy_gateway_l2 NOLOGIN; END IF; IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mergepilot_approver') THEN CREATE ROLE mergepilot_approver NOLOGIN; END IF; END \$d\$;" >/dev/null
echo "=== base chain ==="
for m in $BASE; do docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$DBDIR/${m}.sql" >/dev/null || { echo "MIG $m FAIL"; rc=1; exit 1; }; done
echo "base chain rc=0"
echo "=== m4f1 round 1 ==="
docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_state.sql" >"$TMPDIR/r1.out" 2>&1 && r1=0 || { r1=1; cat "$TMPDIR/r1.out"; }
echo "m4f1 r1 rc=$r1  $(grep -m1 'self-check PASS' "$TMPDIR/r1.out" | head -c 60)"
echo "=== m4f1 round 2 ==="
docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_state.sql" >"$TMPDIR/r2.out" 2>&1 && r2=0 || { r2=1; cat "$TMPDIR/r2.out"; }
echo "m4f1 r2 rc=$r2  $(grep -m1 'self-check PASS' "$TMPDIR/r2.out" | head -c 60)"
echo "=== producer API audit ==="
if [ "$r1" = 0 ] && [ "$r2" = 0 ]; then
  docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$SQLDIR/producer_api_audit.sql" >"$TMPDIR/audit.out" 2>&1 && arc=0 || arc=1
  echo "audit rc=$arc  $(grep -m1 'PA-SET PASS' "$TMPDIR/audit.out" | head -c 60)"
  [ "$arc" = 0 ] || { echo "--- errors ---"; grep -E "^ERROR|FAIL" "$TMPDIR/audit.out" | head -8; }
else echo "audit SKIPPED"; arc=skip; fi
rc=$([ "$r1" = 0 ] && [ "$r2" = 0 ] && [ "$arc" = 0 ] && echo 0 || echo 1)
echo "=== SUMMARY r1=$r1 r2=$r2 audit=$arc overall=$rc ==="
