#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; DBDIR="$ROOT/tools/audit-db"; SQLDIR="$ROOT/tests/m4f1/sql"
IMG="pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
UNIQ="$$-$(date +%s)"; DB="m4f1-ca-${UNIQ}"; LABEL="m4f1-ca-${UNIQ}"; TMP_ROOT="$(mktemp -d)"; rc=1
BASE="init m3_state m3b_policy m3b_b4 m3b_b4c m3b_b4c1 m3b_b4c1_1 m3b_b4d1 m3c_state"
cleanup(){ local e=$? c=1 n=1 t=1 f; trap - EXIT; set +e; docker rm -f "$DB" >/dev/null 2>&1; c="$(docker ps -aq --filter "label=$LABEL"|wc -l)"||c=1; n="$(docker network ls -q --filter "label=$LABEL"|wc -l)"||n=1; case "$TMP_ROOT" in /tmp/*) rm -rf -- "$TMP_ROOT";; esac; [ ! -e "$TMP_ROOT" ]&&t=0; f=$e; [ "$f" -ne 0 ]||f=$rc; if [ "$c" -ne 0 ]||[ "$n" -ne 0 ]||[ "$t" -ne 0 ];then f=1;fi; echo "RESIDUE containers=$c networks=$n temp_dirs=$t";exit "$f";}; trap cleanup EXIT
docker run -d --name "$DB" --label "$LABEL" -e POSTGRES_USER=mergepilot -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=app "$IMG" >/dev/null
ready=0;for _ in $(seq 1 60);do docker exec "$DB" psql -X -U mergepilot -d app -tAc 'select 1' >/dev/null 2>&1&&{ ready=1;break;};sleep 1;done;[ "$ready" -eq 1 ]
docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 >/dev/null <<'EOSQL'
DO $r$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='policy_gateway_l2')THEN CREATE ROLE policy_gateway_l2 NOLOGIN;END IF;IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='mergepilot_approver')THEN CREATE ROLE mergepilot_approver NOLOGIN;END IF;END $r$;
EOSQL
for m in $BASE;do docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 <"$DBDIR/$m.sql" >/dev/null 2>&1;done
for r in 1 2;do if ! docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 <"$DBDIR/m4f1_state.sql" >"$TMP_ROOT/m$r" 2>&1;then cat "$TMP_ROOT/m$r";exit 1;fi;grep -q 'self-check PASS' "$TMP_ROOT/m$r";done
if ! docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 <"$SQLDIR/producer_api_audit.sql" >"$TMP_ROOT/p" 2>&1;then cat "$TMP_ROOT/p";exit 1;fi
if ! docker exec -i "$DB" psql -X -U mergepilot -d app -v ON_ERROR_STOP=1 <"$SQLDIR/complete_api_audit.sql" >"$TMP_ROOT/c" 2>&1;then cat "$TMP_ROOT/c";exit 1;fi
grep -q 'CA-SET PASS: 13' "$TMP_ROOT/c"; echo 'COMPLETE API PASS ids=13';rc=0
