#!/usr/bin/env bash
# tests/m4f1/run_schema_foundation.sh — Stage 2.1A:迁移连续 2 轮(幂等)+ by-name catalog/ACL/FK/registry 自检 + 不可变/guard 功能点测。
# 仅结构层(无业务 SD API)。一次性 Docker PG16;trap EXIT 清理。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DBDIR="$ROOT/tools/audit-db"; SQLDIR="$ROOT/tests/m4f1/sql"
IMG="pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
UNIQ="$$-$(date +%s)"; DB="m4f1-sf-${UNIQ}"; LABEL="m4f1-sf-${UNIQ}"
BASE="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
r1=; r2=; hf1=; hf2=; arc=; rc=1
TMPDIR="$(mktemp -d)" || { echo "mktemp failed" >&2; exit 1; }
R1_OUT="$TMPDIR/m4f1_r1.out"
R2_OUT="$TMPDIR/m4f1_r2.out"
HF1_OUT="$TMPDIR/hotfix_r1.out"
HF2_OUT="$TMPDIR/hotfix_r2.out"
AUDIT_OUT="$TMPDIR/sf.out"

cleanup() {
  exit_rc=$?
  set +e
  docker rm -f "$DB" >/dev/null 2>&1 || true
  if ! c=$(docker ps -aq --filter label="$LABEL" | wc -l); then c=1; fi
  if ! n=$(docker network ls -q --filter label="$LABEL" | wc -l); then n=1; fi
  tmp=0
  case "$TMPDIR" in
    /tmp/*) rm -rf -- "$TMPDIR" ;;
    *) echo "unsafe temp path: $TMPDIR" >&2; tmp=1 ;;
  esac
  [ ! -e "$TMPDIR" ] || tmp=1
  final_rc="$exit_rc"
  [ "$exit_rc" -ne 0 ] || final_rc="$rc"
  if [ "$c" -ne 0 ] || [ "$n" -ne 0 ] || [ "$tmp" -ne 0 ]; then final_rc=1; fi
  echo "RESIDUE containers=$c networks=$n temp_dirs=$tmp"
  trap - EXIT
  exit "$final_rc"
}
trap cleanup EXIT

docker run -d --name "$DB" --label "$LABEL" -e POSTGRES_USER=mergepilot -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=mergepilot_audit "$IMG" >/dev/null
for i in $(seq 1 60); do docker exec "$DB" psql -U mergepilot -d mergepilot_audit -tAc "SELECT 1" >/dev/null 2>&1 && { sleep 2; break; }; sleep 1; done
docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 -c "DO \$d\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='policy_gateway_l2') THEN CREATE ROLE policy_gateway_l2 NOLOGIN; END IF; IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mergepilot_approver') THEN CREATE ROLE mergepilot_approver NOLOGIN; END IF; END \$d\$;" >/dev/null
echo "=== base chain ==="
for m in $BASE; do docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$DBDIR/${m}.sql" >/dev/null || { echo "MIG $m FAIL"; rc=1; exit 1; }; done
echo "base chain rc=0"

echo "=== m4f1 round 1 ==="
if docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_state.sql" >"$R1_OUT" 2>&1; then r1=0; else r1=1; cat "$R1_OUT"; fi
echo "m4f1 r1 rc=$r1  $(grep -m1 'self-check PASS' "$R1_OUT" || grep -m1 ERROR "$R1_OUT" | head -c 100)"
echo "=== m4f1 round 2 (idempotency) ==="
if docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_state.sql" >"$R2_OUT" 2>&1; then r2=0; else r2=1; cat "$R2_OUT"; fi
echo "m4f1 r2 rc=$r2  $(grep -m1 'self-check PASS' "$R2_OUT" | head -c 60)"

echo "=== m4f1_hotfix_1 round 1 (post-release P1 concurrency fix) ==="
if docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_hotfix_1.sql" >"$HF1_OUT" 2>&1; then hf1=0; else hf1=1; cat "$HF1_OUT"; fi
echo "hotfix r1 rc=$hf1  $(grep -m1 'hotfix_1 catalog self-check PASS' "$HF1_OUT" | head -c 60)"
echo "=== m4f1_hotfix_1 round 2 (idempotency) ==="
if docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$DBDIR/m4f1_hotfix_1.sql" >"$HF2_OUT" 2>&1; then hf2=0; else hf2=1; cat "$HF2_OUT"; fi
echo "hotfix r2 rc=$hf2  $(grep -m1 'hotfix_1 catalog self-check PASS' "$HF2_OUT" | head -c 60)"

echo "=== schema-foundation audit ==="
if [ "$r1" = 0 ] && [ "$r2" = 0 ] && [ "$hf1" = 0 ] && [ "$hf2" = 0 ]; then
  if docker exec -i "$DB" psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$SQLDIR/schema_foundation_audit.sql" >"$AUDIT_OUT" 2>&1; then arc=0; else arc=1; fi
  echo "audit rc=$arc  $(grep -m1 'SF-SET PASS' "$AUDIT_OUT" | head -c 60)"
  grep -m1 'SF-WORKER-NO-DML PASS' "$AUDIT_OUT" || true
  [ "$arc" = 0 ] || { echo "--- audit errors ---"; grep -E "^ERROR|FAIL" "$AUDIT_OUT" | head -8; }
else
  echo "audit SKIPPED (m4f1 r1/r2 failed)"; arc=skip
fi
rc=$([ "$r1" = 0 ] && [ "$r2" = 0 ] && [ "$hf1" = 0 ] && [ "$hf2" = 0 ] && [ "$arc" = 0 ] && echo 0 || echo 1)
echo "=== SUMMARY r1=$r1 r2=$r2 hf1=$hf1 hf2=$hf2 audit=$arc overall=$rc ==="
